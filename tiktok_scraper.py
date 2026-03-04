"""
TikTok metadata extractor.

Strategy
--------
1. Resolve any short URL (vm.tiktok.com) to canonical form via HTTP HEAD.
2. Pull basic metadata from the TikTok oEmbed endpoint (no auth required).
3. Launch a headless Chromium browser, load tiktok.com/embed/v2/<video_id>,
   and intercept the XHR/fetch calls the embed page makes to the TikTok API.
   Those responses contain the full caption, hashtags, AI-generated summary,
   and keywords as structured JSON — much more reliable than scraping the DOM.
4. If the network interception misses the summary panel, fall back to
   clicking the "more" button and scraping the rendered DOM.
"""

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, Page, Request, Response
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
    title: Optional[str] = None
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    ai_summary: Optional[str] = None
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
    """Follow redirects to resolve short URLs to canonical form."""
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
    meta.title = data.get("title") or meta.title
    meta.author = data.get("author_name") or meta.author
    meta.author_url = data.get("author_url") or meta.author_url
    meta.thumbnail_url = data.get("thumbnail_url") or meta.thumbnail_url
    meta.embed_html = data.get("html") or meta.embed_html
    # oEmbed title on TikTok is actually the caption text
    title = meta.title or ""
    meta.description = title
    meta.hashtags = re.findall(r"#(\w+)", title)
    meta.source.append("oembed")


# ---------------------------------------------------------------------------
# Helpers for pulling metadata out of TikTok API JSON responses
# ---------------------------------------------------------------------------

def _extract_from_item_struct(item: dict, meta: TikTokMetadata) -> None:
    """
    Parse a single video item dict from any TikTok API response shape.
    Handles both /api/item/detail/ and the embedded itemInfo patterns.
    """
    desc = item.get("desc") or item.get("video_description") or ""
    if desc and (not meta.description or len(desc) > len(meta.description)):
        meta.description = desc
        tags = [t.strip("#") for t in re.findall(r"#(\w+)", desc)]
        meta.hashtags = list(dict.fromkeys(meta.hashtags + tags))

    # Author
    author = item.get("author") or {}
    if isinstance(author, dict):
        nick = author.get("nickname") or author.get("uniqueId")
        if nick and not meta.author:
            meta.author = nick

    # Hashtags from challengeInfoList / textExtra
    for tag_obj in item.get("challenges", []) + item.get("textExtra", []):
        ht = tag_obj.get("hashtagName") or tag_obj.get("title") or ""
        if ht and ht not in meta.hashtags:
            meta.hashtags.append(ht)

    # AI-generated summary (newer API field names vary)
    for key in ("aiDynamicCover", "videoSummary", "AIGCDescription", "aigcDescription"):
        val = item.get(key)
        if isinstance(val, str) and val and not meta.ai_summary:
            meta.ai_summary = val

    # Keywords / tags
    for key in ("keywords", "videoKeywords", "suggestWords"):
        val = item.get(key)
        if isinstance(val, list):
            for kw in val:
                word = kw if isinstance(kw, str) else kw.get("word") or kw.get("keyword") or ""
                if word and word not in meta.keywords:
                    meta.keywords.append(word)


def _parse_api_response(body: dict, meta: TikTokMetadata) -> bool:
    """
    Try to extract metadata from any TikTok API JSON response body.
    Returns True if something useful was found.
    """
    found = False

    # /api/item/detail/ shape
    item_info = body.get("itemInfo") or {}
    item_struct = item_info.get("itemStruct") or body.get("itemStruct") or {}
    if item_struct:
        _extract_from_item_struct(item_struct, meta)
        found = True

    # /api/recommend/ or list-style responses
    for key in ("items", "itemList", "data"):
        items = body.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    _extract_from_item_struct(it, meta)
                    found = True

    return found


# ---------------------------------------------------------------------------
# Embed page scraping via Playwright
# ---------------------------------------------------------------------------

EMBED_BASE = "https://www.tiktok.com/embed/v2/"

# API URL fragments to intercept
API_PATTERNS = (
    "/api/item/detail",
    "/api/recommend",
    "/embed/v2/",          # some metadata comes back in the page's own fetch
)

# Fallback DOM selectors (used only if API interception yields nothing)
MORE_BTN_SELECTORS = [
    "button:has-text('More')",
    "button:has-text('more')",
    "[data-e2e='more-btn']",
    "button[aria-label*='more' i]",
]

CAPTION_SELECTORS = [
    "[data-e2e='browse-video-desc']",
    "[data-e2e='video-desc']",
    "[class*='caption']",
    "[class*='desc']",
    "h1",
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
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


async def _scrape_dom(page: Page, meta: TikTokMetadata) -> None:
    """Last-resort DOM scraping after API interception."""
    for sel in CAPTION_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                text = (await loc.inner_text()).strip()
                if text and (not meta.description or len(text) > len(meta.description)):
                    meta.description = text
                    tags = re.findall(r"#(\w+)", text)
                    meta.hashtags = list(dict.fromkeys(meta.hashtags + tags))
                    break
        except Exception:
            continue

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

    kw_texts: list[str] = []
    for sel in KEYWORD_SELECTORS:
        try:
            locs = page.locator(sel)
            count = await locs.count()
            for i in range(count):
                t = (await locs.nth(i).inner_text()).strip()
                if t:
                    kw_texts.append(t)
        except Exception:
            continue
    if kw_texts:
        meta.keywords = list(dict.fromkeys(kw_texts))


async def scrape_embed(video_id: str, meta: TikTokMetadata) -> None:
    """
    Load the TikTok embed player in a headless browser.
    Primary strategy: intercept XHR/fetch JSON responses from TikTok's own API.
    Fallback: click 'more' button and scrape the DOM.
    """
    embed_url = f"{EMBED_BASE}{video_id}"
    intercepted: list[dict] = []

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

        # Intercept API responses before the page finishes loading
        async def handle_response(response: Response) -> None:
            url = response.url
            if not any(pat in url for pat in API_PATTERNS):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                if _parse_api_response(body, meta):
                    intercepted.append({"url": url, "keys": list(body.keys())})
                    meta.source.append("api_intercept")
                    print(f"[intercept] captured {url[:80]}", file=sys.stderr)
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            print(f"[embed] loading {embed_url}", file=sys.stderr)
            await page.goto(embed_url, wait_until="networkidle", timeout=30_000)

            # Detect error pages
            page_title = await page.title()
            if "error" in page_title.lower() or not await page.locator("video").count():
                print("[embed] warning: video may be unavailable", file=sys.stderr)

            # Try to expand the "more" panel for AI summary / keywords
            clicked = await _try_click_more(page)
            if clicked:
                print("[embed] clicked 'more' button", file=sys.stderr)
                # Give the panel time to load and fire its own API call
                await page.wait_for_timeout(2000)

            # DOM fallback if API interception found nothing
            if not intercepted or not meta.description:
                print("[embed] falling back to DOM scraping", file=sys.stderr)
                await _scrape_dom(page, meta)
                if meta.description or meta.ai_summary:
                    meta.source.append("embed_dom")

            # Debug dump
            if "--debug" in sys.argv:
                html = await page.content()
                debug_path = f"/tmp/tiktok_embed_{video_id}.html"
                with open(debug_path, "w") as f:
                    f.write(html)
                print(f"[embed] debug HTML saved to {debug_path}", file=sys.stderr)
                if intercepted:
                    print(f"[embed] intercepted APIs: {json.dumps(intercepted, indent=2)}", file=sys.stderr)

        except PWTimeout:
            print("[embed] timed out loading page", file=sys.stderr)
        except Exception as exc:
            print(f"[embed] error: {exc}", file=sys.stderr)
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract(url: str) -> TikTokMetadata:
    """Full extraction pipeline for a TikTok URL."""
    meta = TikTokMetadata(url=url)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # 1. Resolve short URL
        canonical = await resolve_url(url, client)
        if canonical != url:
            print(f"[url] resolved to {canonical}", file=sys.stderr)
        meta.url = canonical

        # 2. Extract video ID
        meta.video_id = extract_video_id(canonical)
        if not meta.video_id:
            print("[url] could not extract video ID", file=sys.stderr)

        # 3. oEmbed (fast, no browser required)
        oembed_data = await fetch_oembed(canonical, client)
        parse_oembed(oembed_data, meta)

    # 4. Headless browser: API interception + DOM fallback
    if meta.video_id:
        await scrape_embed(meta.video_id, meta)
    else:
        print("[embed] skipped — no video ID", file=sys.stderr)

    return meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Usage: python tiktok_scraper.py <tiktok_url> [--debug]")
        sys.exit(1)

    url = args[0]
    print(f"Extracting metadata for: {url}\n", file=sys.stderr)

    meta = await extract(url)
    print(json.dumps(asdict(meta), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
