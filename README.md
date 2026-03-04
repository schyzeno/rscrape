# rscrape

Link aggregator for tech short-form video content. Given a video URL, extracts metadata: title, description, keywords, hashtags, author, and AI-generated summaries.

## Supported platforms

| Platform | Status |
|---|---|
| TikTok | ✅ |
| YouTube Shorts | Planned |
| Instagram Reels | Planned |

## TikTok metadata

| Field | Source | Notes |
|---|---|---|
| `author` | oEmbed / embed page | Display name |
| `author_url` | oEmbed / embed page | `tiktok.com/@handle` |
| `title` | oEmbed | Raw caption text (may be truncated) |
| `ai_title` | Full page `<title>` | Server-generated descriptive title, always present |
| `description` | Embed page JSON | Full caption text |
| `hashtags` | Embed page JSON | Parsed from `textExtra` and `challengeInfoList` |
| `keywords` | Full page JSON | `suggestedWords` — TikTok's search keyword suggestions |
| `categories` | Full page JSON | `diversificationLabels` (e.g. `["Travel", "Lifestyle"]`) |
| `ai_summary` | Full page JSON | `creatorAIComment` — only on eligible videos (long, speech-heavy) |
| `thumbnail_url` | oEmbed | Cover image URL |
| `embed_html` | oEmbed | Standard oEmbed blockquote HTML |

## Extraction pipeline

```
URL
 │
 ├─ 1. Resolve short URL   (vm.tiktok.com → canonical via HTTP HEAD)
 ├─ 2. oEmbed              (fast, no browser — author, thumbnail, embed HTML)
 ├─ 3. Embed page          (tiktok.com/embed/v2/<id> — caption, hashtags)
 └─ 4. Full page           (tiktok.com/@user/video/<id> — AI title, keywords, categories)
```

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
python tiktok_scraper.py <tiktok_url>

# Short URLs work too
python tiktok_scraper.py "https://vm.tiktok.com/XXXXX/"

# Debug mode: saves full HTML to /tmp/
python tiktok_scraper.py <tiktok_url> --debug
```

### Example output

```json
{
  "url": "https://www.tiktok.com/@wakeupitaru/video/7593497773480119565",
  "video_id": "7593497773480119565",
  "author": "Itaruuuuu",
  "author_url": "https://www.tiktok.com/@wakeupitaru",
  "title": "#travel #japan ",
  "ai_title": "Essential Tips for Budget Travel in Japan",
  "description": "#travel #japan",
  "hashtags": ["travel", "japan"],
  "keywords": [
    "Japan Travel", "Traveling To Japan", "Visiting Japan",
    "Japan Vacation Tips", "Things To Know Before Going To Japan"
  ],
  "categories": ["Travel", "Lifestyle"],
  "ai_summary": null,
  "source": ["oembed", "page_json", "full_page"]
}
```

## Notes

- `ai_summary` is gated by TikTok on video eligibility (`creatorAIComment.eligibleVideo`). Videos that are short, music-only, or have minimal captions typically return `null`. The `ai_title` is always generated.
- CDN URLs in `thumbnail_url` are signed and expire.
- No API keys or authentication required.
