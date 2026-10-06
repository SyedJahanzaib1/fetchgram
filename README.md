# FetchGram — Instagram bulk downloader

Paste an Instagram **@username** or post link → browse a media grid →
select posts → download as a **ZIP** (photos as JPG, videos as MP4 with audio,
carousels as all slides). No login, no accounts, no credits — free.

## How it works

- **Media extraction** (`ig_client.py`): Instagram login-walls plain page
  scraping from server IPs, and yt-dlp's `instagram:user` extractor is
  currently broken. FetchGram uses the same **logged-out GraphQL flow**
  yt-dlp itself uses for single posts (`PolarisLoggedOutDesktopWWWPostRootContentQuery`),
  which returns full-quality `image_versions2` (photos) and `video_versions`
  (videos) plus `carousel_media`. This is why photos, reels and carousels all work.
- **Profile listing** (`listing.py`): via `instagram-cli` (the server's own
  linked Instagram account — site visitors never log in). This provider is
  swappable; only `get_profile()` / `list_posts()` need reimplementing.
- **Downloads** (`downloader.py`): direct CDN URLs fetched with a
  browser-impersonating client, bundled into a ZIP (single file downloads directly).

## Run locally

```bash
cd ig-bulk-downloader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000

> **instagram-cli**: profile search (`@username` mode) needs the
> `instagram-cli` tool installed and one Instagram account linked on the
> server (`instagram-cli accounts` should list it). Single/bulk **link**
> mode works without it.

## Deploy on a VPS (systemd)

```bash
sudo mkdir -p /opt/fetchgram
sudo cp -r . /opt/fetchgram/          # app.py, ig_client.py, listing.py, downloader.py, static/
cd /opt/fetchgram
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# install instagram-cli on the server and link one Instagram account
sudo cp fetchgram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fetchgram
```

Put nginx/Caddy in front for HTTPS, e.g. proxy `https://dl.example.com` →
`127.0.0.1:8000`.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/resolve?input=` | username → profile info; link(s) → post cards |
| GET | `/api/profile/{u}?cursor=&limit=` | paginated post grid (thumbnails included) |
| POST | `/api/download` | `{items: [shortcode\|url…]}` → ZIP (or single file) |
| GET | `/api/thumb?url=` | thumbnail proxy |
| GET | `/api/health` | health check |

Errors are JSON: 400 bad input, 403 private, 404 not found,
429 Instagram rate-limit, 502 upstream failure.

## Honest limitations

- **Instagram rate-limits anonymous requests.** Under heavy use the API
  returns 429 ("wait a few minutes") — the UI surfaces this cleanly and
  results are cached to reduce pressure.
- **Public content only.** Private profiles/posts can never be fetched —
  that's how Instagram works, not a tool limitation.
- **Profile grid pages** come through the server's linked Instagram account
  (`instagram-cli`), which has its own rate limits (~dozens of calls/window).
- **Stories/highlights** are not supported (they require a logged-in viewer).

## Legal note

Only download content you own or have the rights to use. Downloading does
not transfer any rights; reposting or commercial use without the rights
holder's permission can infringe copyright. Not affiliated with Instagram
or Meta.
