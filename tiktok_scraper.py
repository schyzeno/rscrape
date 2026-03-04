"""
TikTok metadata extractor.

Strategy
--------
1. Resolve any short URL (vm.tiktok.com) to canonical form via HTTP HEAD.
2. Pull basic metadata from the TikTok oEmbed endpoint (no auth required).
3. Embed page (tiktok.com/embed/v2/<video_id>) via headless Chromium:
     a. Parse the inline <script> JSON (videoData) — caption, author, hashtags.
     b. Intercept /api/item/detail/ XHR if fired.
4. Full page (tiktok.com/@user/video/<id>) via headless Chromium:
     Extracts __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON which contains:
     - itemStruct.suggestedWords  → keywords
     - itemStruct.diversificationLabels → content categories
     - <meta name="description"> → AI-expanded description (ai_summary)
     - Page <title> → server-generated AI title (always present)
     - Clicks "more" button to confirm expanded description from DOM
"""

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, Page, Response
from playwright.async_api import TimeoutError as PWTimeout


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TikTokMetadata:
    url: str
    video_id: Optional[str] = None
    author: Optional[str] = None
    author_url: Optional[str] = None
    title: Optional[str] = None           # oEmbed caption (may be truncated)
    ai_title: Optional[str] = None        # server-generated AI title from <title>
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)   # suggestedWords
    categories: list[str] = field(default_factory=list) # diversificationLabels
    ai_summary: Optional[str] = None      # creatorAIComment (when eligible)
    embed_html: Optional[str] = None
    source: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

TIKTOK_VIDEO_RE = re.compile(r"/video/(\d+)")
SHORT_TIKTOK_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com"}


def extract_video_id(url: str) -> Optional[str]:
    m = TIKTOK_VIDEO_RE.search(url)
    return m.group(1) if m else None


async def resolve_url(url: str, client: httpx.AsyncClient) -> str:
    parsed = urlparse(url)
    if parsed.netloc in SHORT_TIKTOK_HOSTS:
        try:
            resp = await client.head(url, follow_redirects=True, timeout=10)
            return str(resp.url)
        except Exception:
            pass
    return url


# ---------------------------------------------------------------------------
# oEmbed
# ---------------------------------------------------------------------------

OEMBED_ENDPOINT = "https://www.tiktok.com/oembed"


async def fetch_oembed(url: str, client: httpx.AsyncClient) -> dict:
    try:
        resp = await client.get(
            OEMBED_ENDPOINT,
            params={"url": url},
            timeout=15,
            headers={"User-Agent": "rscrape/0.1"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[oembed] failed: {exc}", file=sys.stderr)
        return {}


def parse_oembed(data: dict, meta: TikTokMetadata) -> None:
    if not data:
        return
    meta.author = data.get("author_name") or meta.author
    meta.author_url = data.get("author_url") or meta.author_url
    meta.thumbnail_url = data.get("thumbnail_url") or meta.thumbnail_url
    meta.embed_html = data.get("html") or meta.embed_html
    # oEmbed title on TikTok is the caption (may be truncated)
    title = data.get("title") or ""
    meta.title = title
    meta.source.append("oembed")


# ---------------------------------------------------------------------------
# Inline page JSON parser
# ---------------------------------------------------------------------------

def parse_page_json(html: str, video_id: str, meta: TikTokMetadata) -> bool:
    """
    TikTok's embed page embeds a JSON blob in a <script> tag containing
    the full videoData for the requested video. Structure:
      source.data./embed/v2/<id>.videoData:
        itemInfos.text        — caption
        authorInfos.nickName  — display name
        authorInfos.uniqueId  — @handle
        textExtra[]           — {HashtagName, ...}
        challengeInfoList[]   — {challengeName, text, ...}
        itemInfos.coversOrigin[] — cover image URLs
    """
    path_key = f"/embed/v2/{video_id}"
    for m in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        s = m.group(1)
        if video_id not in s or '{' not in s:
            continue
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            continue

        try:
            video_data = data["source"]["data"][path_key]["videoData"]
        except (KeyError, TypeError):
            continue

        item_infos = video_data.get("itemInfos", {})
        author_infos = video_data.get("authorInfos", {})
        text_extra = video_data.get("textExtra", [])
        challenges = video_data.get("challengeInfoList", [])

        # Caption / description
        caption = item_infos.get("text", "").strip()
        if caption:
            meta.description = caption

        # Author
        nick = author_infos.get("nickName") or author_infos.get("uniqueId")
        handle = author_infos.get("uniqueId")
        if nick and not meta.author:
            meta.author = nick
        if handle and not meta.author_url:
            meta.author_url = f"https://www.tiktok.com/@{handle}"

        # Hashtags from textExtra (inline tags in caption)
        for tag in text_extra:
            ht = tag.get("HashtagName", "").strip()
            if ht and ht not in meta.hashtags:
                meta.hashtags.append(ht)

        # Additional hashtags from challenge list
        for ch in challenges:
            ht = ch.get("challengeName", "").strip()
            if ht and ht not in meta.hashtags:
                meta.hashtags.append(ht)

        # Cover image (if oEmbed thumbnail is missing or lower quality)
        covers = item_infos.get("coversOrigin") or item_infos.get("covers") or []
        if covers and not meta.thumbnail_url:
            meta.thumbnail_url = covers[0]

        meta.source.append("page_json")
        return True

    return False


# ---------------------------------------------------------------------------
# XHR API response parser (for AI summary / keywords)
# ---------------------------------------------------------------------------

def _extract_item_struct(item: dict, video_id: str, meta: TikTokMetadata) -> bool:
    """
    Parse a single item dict from /api/item/detail/ or /api/recommend/.
    Only processes if the item's ID matches video_id (avoids recommended-video
    data polluting metadata for the requested video).
    Returns True if the item matched and was useful.
    """
    item_id = str(item.get("id") or item.get("aweme_id") or "")
    if item_id and item_id != video_id:
        return False

    desc = (item.get("desc") or item.get("video_description") or "").strip()
    if desc and (not meta.description or len(desc) > len(meta.description)):
        meta.description = desc

    # AI-generated summary (field name varies by region/version)
    for key in ("videoSummary", "AIGCDescription", "aigcDescription", "aiDynamicCover"):
        val = item.get(key)
        if isinstance(val, str) and val and not meta.ai_summary:
            meta.ai_summary = val

    # Keywords
    for key in ("keywords", "videoKeywords", "suggestWords"):
        val = item.get(key)
        if isinstance(val, list):
            for kw in val:
                word = kw if isinstance(kw, str) else (kw.get("word") or kw.get("keyword") or "")
                if word and word not in meta.keywords:
                    meta.keywords.append(word)

    return True


def parse_api_response(body: dict, video_id: str, meta: TikTokMetadata) -> bool:
    found = False

    # /api/item/detail/ shape
    item_struct = (body.get("itemInfo") or {}).get("itemStruct") or body.get("itemStruct")
    if item_struct:
        if _extract_item_struct(item_struct, video_id, meta):
            found = True

    # List-style responses (/api/recommend/, etc.)
    for key in ("items", "itemList", "data"):
        items = body.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and _extract_item_struct(it, video_id, meta):
                    found = True

    return found


# ---------------------------------------------------------------------------
# Embed page scraping via Playwright
# ---------------------------------------------------------------------------

EMBED_BASE = "https://www.tiktok.com/embed/v2/"

API_INTERCEPT_PATTERNS = ("/api/item/detail", "/api/recommend")

MORE_BTN_SELECTORS = [
    "button:has-text('More')",
    "button:has-text('more')",
    "[data-e2e='more-btn']",
    "button[aria-label*='more' i]",
]

SUMMARY_SELECTORS = [
    "[data-e2e='video-summary']",
    "[class*='summary']",
    "[class*='aiDescription']",
]

KEYWORD_SELECTORS = [
    "[data-e2e='search-common-word']",
    "[data-e2e='keyword']",
    "[class*='keyword']",
]


async def _try_click_more(page: Page) -> bool:
    for sel in MORE_BTN_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


async def _dom_fallback(page: Page, meta: TikTokMetadata) -> None:
    for sel in SUMMARY_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                text = (await loc.inner_text()).strip()
                if text:
                    meta.ai_summary = text
                    break
        except Exception:
            continue

    kws: list[str] = []
    for sel in KEYWORD_SELECTORS:
        try:
            locs = page.locator(sel)
            count = await locs.count()
            for i in range(count):
                t = (await locs.nth(i).inner_text()).strip()
                if t:
                    kws.append(t)
        except Exception:
            continue
    if kws:
        meta.keywords = list(dict.fromkeys(kws))


async def scrape_embed(video_id: str, meta: TikTokMetadata) -> None:
    embed_url = f"{EMBED_BASE}{video_id}"
    intercepted_apis: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        async def on_response(response: Response) -> None:
            url = response.url
            if not any(p in url for p in API_INTERCEPT_PATTERNS):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                body = await response.json()
                if parse_api_response(body, video_id, meta):
                    intercepted_apis.append(url.split("?")[0])
                    print(f"[intercept] {url[:80]}", file=sys.stderr)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            print(f"[embed] loading {embed_url}", file=sys.stderr)
            await page.goto(embed_url, wait_until="networkidle", timeout=30_000)

            # Primary: parse inline page JSON
            html = await page.content()
            if parse_page_json(html, video_id, meta):
                print("[embed] parsed inline page JSON", file=sys.stderr)
            else:
                print("[embed] inline page JSON not found", file=sys.stderr)

            if "--debug" in sys.argv:
                debug_path = f"/tmp/tiktok_embed_{video_id}.html"
                with open(debug_path, "w") as f:
                    f.write(html)
                print(f"[debug] HTML saved to {debug_path}", file=sys.stderr)
                print(f"[debug] intercepted APIs: {intercepted_apis}", file=sys.stderr)

        except PWTimeout:
            print("[embed] timed out", file=sys.stderr)
        except Exception as exc:
            print(f"[embed] error: {exc}", file=sys.stderr)
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Full page scraping (tiktok.com/@user/video/<id>)
# ---------------------------------------------------------------------------

REHYDRATION_KEY = "__UNIVERSAL_DATA_FOR_REHYDRATION__"


def parse_full_page_html(html: str, meta: TikTokMetadata) -> bool:
    """
    Extract metadata from the __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON blob
    embedded in the full tiktok.com video page.

    Key fields extracted:
      itemStruct.suggestedWords       → keywords
      itemStruct.diversificationLabels → categories
      itemStruct.creatorAIComment     → AI topic summary (when hasAITopic=True)
      Page <title>                    → AI-generated title (strip "| TikTok")
    """
    # AI title from <title> tag (server-generated, always present)
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html)
    if title_m:
        raw = title_m.group(1).strip()
        ai_title = re.sub(r"\s*\|\s*TikTok\s*$", "", raw).strip()
        if ai_title:
            meta.ai_title = ai_title

    # Expanded description from <meta name="description">
    # Format: "{N} Likes, {N} Comments. TikTok video from {Name} (@handle): '{expanded_desc}'. {hashtags}"
    og_desc = re.search(r'<meta name="description" content="([^"]+)"', html)
    if og_desc:
        raw = og_desc.group(1)
        desc_m = re.search(r":\s+'(.+?)'\.", raw, re.DOTALL)
        if desc_m:
            expanded = desc_m.group(1).strip()
            if expanded and expanded != meta.description:
                meta.ai_summary = expanded

    # Parse the rehydration JSON
    m = re.search(
        rf'id="{REHYDRATION_KEY}"[^>]*>(\{{.*?\}})</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return bool(meta.ai_title)

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return bool(meta.ai_title)

    item = (
        data.get("__DEFAULT_SCOPE__", {})
        .get("webapp.video-detail", {})
        .get("itemInfo", {})
        .get("itemStruct", {})
    )
    if not item:
        return bool(meta.ai_title)

    # suggestedWords → keywords
    for w in item.get("suggestedWords", []):
        if isinstance(w, str) and w and w not in meta.keywords:
            meta.keywords.append(w)

    # diversificationLabels → categories (deduplicated)
    for label in item.get("diversificationLabels", []):
        if isinstance(label, str) and label and label not in meta.categories:
            meta.categories.append(label)

    # creatorAIComment — full AI topic summary when eligible
    ai_comment = item.get("creatorAIComment", {})
    if ai_comment.get("hasAITopic"):
        # categoryList contains the topic summaries
        for cat in ai_comment.get("categoryList", []):
            text = cat.get("categoryDesc") or cat.get("title") or ""
            if text and not meta.ai_summary:
                meta.ai_summary = text

    # AIGCDescription — present on AI-generated videos
    aigc = item.get("AIGCDescription", "").strip()
    if aigc and not meta.ai_summary:
        meta.ai_summary = aigc

    # Fill in any gaps missed by the embed scrape
    if not meta.description:
        desc = item.get("desc", "").strip()
        if desc:
            meta.description = desc

    if not meta.author:
        author = item.get("author", {})
        meta.author = author.get("nickname") or author.get("uniqueId")

    meta.source.append("full_page")
    return True


async def scrape_full_page(canonical_url: str, meta: TikTokMetadata) -> None:
    """Navigate to the full tiktok.com video page and extract metadata."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()
        try:
            print(f"[full_page] loading {canonical_url}", file=sys.stderr)
            await page.goto(canonical_url, wait_until="networkidle", timeout=30_000)
            html = await page.content()

            if parse_full_page_html(html, meta):
                print(f"[full_page] ai_title={meta.ai_title!r}", file=sys.stderr)
            else:
                print("[full_page] rehydration JSON not found", file=sys.stderr)

            # Click "more" on the full page to confirm/extend expanded description
            clicked = await _try_click_more(page)
            if clicked:
                print("[full_page] clicked 'more' button", file=sys.stderr)
                try:
                    loc = page.locator("[data-e2e='video-desc']").first
                    expanded = (await loc.inner_text(timeout=3000)).strip()
                    if expanded and (not meta.description or len(expanded) > len(meta.description)):
                        meta.description = expanded
                        print(f"[full_page] updated description from DOM: {expanded[:60]!r}", file=sys.stderr)
                except Exception:
                    pass

            if "--debug" in sys.argv:
                debug_path = f"/tmp/tiktok_full_{meta.video_id}.html"
                with open(debug_path, "w") as f:
                    f.write(html)
                print(f"[debug] full page HTML saved to {debug_path}", file=sys.stderr)

        except PWTimeout:
            print("[full_page] timed out", file=sys.stderr)
        except Exception as exc:
            print(f"[full_page] error: {exc}", file=sys.stderr)
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract(url: str) -> TikTokMetadata:
    meta = TikTokMetadata(url=url)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        canonical = await resolve_url(url, client)
        if canonical != url:
            print(f"[url] resolved to {canonical}", file=sys.stderr)
        meta.url = canonical

        meta.video_id = extract_video_id(canonical)
        if not meta.video_id:
            print("[url] could not extract video ID", file=sys.stderr)

        oembed_data = await fetch_oembed(canonical, client)
        parse_oembed(oembed_data, meta)

    if meta.video_id:
        # Run embed scrape and full-page scrape sequentially to avoid
        # launching two Chromium instances simultaneously.
        await scrape_embed(meta.video_id, meta)
        await scrape_full_page(meta.url, meta)
    else:
        print("[embed/full_page] skipped — no video ID", file=sys.stderr)

    return meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Usage: python tiktok_scraper.py <tiktok_url> [--debug]")
        sys.exit(1)

    meta = await extract(args[0])
    print(json.dumps(asdict(meta), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
