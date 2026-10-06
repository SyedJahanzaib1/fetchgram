"""
FetchGram — Instagram bulk downloader (FastAPI).

Routes:
  GET  /                          -> frontend (static/index.html)
  GET  /api/resolve?input=...     -> profile info OR post info(s)
  GET  /api/profile/{u}?cursor=&limit=  -> paginated post grid (with thumbnails)
  POST /api/download               -> {items:[shortcode|url]} -> ZIP or single file
  GET  /api/thumb?url=...          -> thumbnail proxy (avoids hotlink issues)

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import io
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ig_client import (
    IGError, IGNotFound, IGPrivate, IGRateLimited, get_media_cached,
)
from listing import (
    ListedPost, ListingError, ProfileNotFound, ProfilePrivate,
    ListingRateLimited, get_profile_cached, list_posts_cached,
    shortcode_from_url,
)
from downloader import build_zip, download_items, resolve_inputs

app = FastAPI(title="FetchGram", version="1.0.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

USERNAME_RE = re.compile(r"^@?[A-Za-z0-9._]{1,30}$")
PROFILE_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:[?#]|$)")

# -- thumbnail proxy cache (url -> (ts, bytes, ctype)) ----------------------
_thumb_cache: dict[str, tuple[float, bytes, str]] = {}
_thumb_lock = __import__("threading").Lock()
THUMB_TTL = 3600


def _classify_input(raw: str) -> tuple[str, list[str] | str]:
    """Return ('username', username) | ('posts', [shortcodes])."""
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(400, "Type a username or paste an Instagram link.")
    # multiple whitespace/comma separated tokens -> bulk mode
    parts = [p.strip() for p in re.split(r"[\s,]+", raw) if p.strip()]
    codes: list[str] = []
    usernames: list[str] = []
    for p in parts:
        sc = shortcode_from_url(p)
        if sc:
            codes.append(sc)
            continue
        m = PROFILE_URL_RE.match(p)
        if m:
            usernames.append(m.group(1))
            continue
        bare = p.strip("/").lstrip("@")
        if USERNAME_RE.match(bare) and "/" not in p:
            if len(parts) == 1:
                usernames.append(bare)   # single token: treat as username
            else:
                codes.append(bare)       # in a list: treat as shortcode
    if codes and not usernames:
        return "posts", codes
    if usernames and not codes:
        return "username", usernames[0]
    if codes:
        return "posts", codes
    raise HTTPException(400, "That doesn't look like an Instagram username or link.")


def _ig_http_error(e: Exception) -> HTTPException:
    if isinstance(e, (IGNotFound, ProfileNotFound)):
        return HTTPException(404, str(e))
    if isinstance(e, (IGPrivate, ProfilePrivate)):
        return HTTPException(403, str(e))
    if isinstance(e, (IGRateLimited, ListingRateLimited)):
        return HTTPException(429, str(e))
    if isinstance(e, (IGError, ListingError)):
        return HTTPException(502, str(e))
    return HTTPException(500, "Something went wrong. Try again.")


def _fill_thumbnails(posts: list[ListedPost]) -> None:
    """Fill post thumbnails via ig_client (parallel, best-effort)."""
    def _one(p: ListedPost):
        try:
            info = get_media_cached(p.shortcode)
            p.thumbnail = info.thumbnail
        except Exception:  # noqa: BLE001 - thumbnail is best-effort
            p.thumbnail = ""
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_one, posts))


def _post_dict(p: ListedPost) -> dict:
    kind = p.kind
    if kind == "video" or p.is_reel:
        kind = "reel"  # all IG videos are reels now; one bucket in the UI
    return {
        "shortcode": p.shortcode,
        "url": p.url,
        "kind": kind,
        "caption": p.caption,
        "likes": p.likes,
        "comments": p.comments,
        "taken_at": p.taken_at,
        "thumbnail": p.thumbnail,
    }


# -- API --------------------------------------------------------------------
@app.get("/api/resolve")
def api_resolve(input: str = Query(..., min_length=1, max_length=4000)):
    kind, value = _classify_input(input)
    if kind == "username":
        try:
            prof = get_profile_cached(value)
        except ProfileNotFound:
            # maybe it was a bare shortcode rather than a username — try as post
            if re.fullmatch(r"[A-Za-z0-9_-]{5,40}", value):
                kind, value = "posts", [value]
            else:
                raise _ig_http_error(ProfileNotFound(
                    f'No public profile found for "{value}". Check the spelling — '
                    "it may be renamed, deactivated, or private."))
        except Exception as e:  # noqa: BLE001
            raise _ig_http_error(e)
        else:
            return {
                "type": "profile",
                "profile": {
                    "username": prof.username,
                    "full_name": prof.full_name,
                    "bio": prof.bio,
                    "avatar": prof.avatar,
                    "followers": prof.followers,
                    "following": prof.following,
                    "post_count": prof.post_count,
                    "is_verified": prof.is_verified,
                },
            }
    # one or more post URLs -> resolve each to a grid card
    posts = []
    for sc in value[:20]:
        try:
            info = get_media_cached(sc)
            kind = "reel" if info.media_type == "video" else info.media_type
            posts.append({
                "shortcode": info.shortcode,
                "url": info.url,
                "kind": kind,
                "caption": (info.caption or "")[:220],
                "likes": info.like_count or 0,
                "comments": info.comment_count or 0,
                "taken_at": info.taken_at or "",
                "thumbnail": info.thumbnail,
                "slides": len(info.slides),
            })
        except Exception as e:  # noqa: BLE001
            posts.append({"shortcode": sc, "error": str(e)
                          if isinstance(e, IGError) else "Could not load this post."})
    return {"type": "posts", "posts": posts}


@app.get("/api/profile/{username}")
def api_profile(username: str,
                cursor: str | None = None,
                limit: int = Query(12, ge=1, le=24)):
    try:
        page = list_posts_cached(username, after=cursor, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise _ig_http_error(e)
    _fill_thumbnails(page.posts)
    return {
        "posts": [_post_dict(p) for p in page.posts],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


class DownloadRequest(BaseModel):
    items: list[str]
    # quality kept for API compatibility; always best available
    quality: str = "best"


@app.post("/api/download")
def api_download(req: DownloadRequest):
    codes = resolve_inputs(req.items)
    if not codes:
        raise HTTPException(400, "No valid Instagram links found.")
    try:
        files, errors = download_items(codes)
    except IGError as e:
        raise HTTPException(400, str(e))
    if not files:
        msg = errors[0]["error"] if errors else "Nothing could be downloaded."
        raise HTTPException(422, msg)

    if len(files) == 1 and not errors:
        f = files[0]
        return StreamingResponse(
            io.BytesIO(f.data),
            media_type=f.content_type or "application/octet-stream",
            headers={"Content-Disposition":
                     f'attachment; filename="{f.filename}"'},
        )
    path = build_zip(files, errors)

    def _cleanup(p: str):
        try:
            os.unlink(p)
        except OSError:
            pass

    from starlette.background import BackgroundTask
    return FileResponse(path, media_type="application/zip",
                        filename=f"fetchgram_{int(time.time())}.zip",
                        background=BackgroundTask(_cleanup, path))


@app.get("/api/thumb")
def api_thumb(url: str = Query(..., max_length=2000)):
    """Proxy thumbnails so the grid never hits hotlink/CORS issues."""
    pu = urlparse(url)
    if pu.netloc not in ("scontent.cdninstagram.com",) and \
            not pu.netloc.endswith(".cdninstagram.com"):
        raise HTTPException(400, "Invalid thumbnail host.")
    now = time.time()
    with _thumb_lock:
        hit = _thumb_cache.get(url)
        if hit and now - hit[0] < THUMB_TTL:
            data, ctype = hit[1], hit[2]
            return StreamingResponse(io.BytesIO(data), media_type=ctype)
    from curl_cffi import requests as creq
    try:
        # curl_cffi honors this environment's egress proxy, so it is used
        # for all outbound fetches.
        s = creq.Session(impersonate="chrome")
        r = s.get(url, headers={"Referer": "https://www.instagram.com/"},
                  timeout=30)
        if r.status_code != 200:
            raise IOError(f"HTTP {r.status_code}")
        data = r.content
        ctype = r.headers.get("content-type", "image/jpeg")
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Could not load thumbnail.")
    with _thumb_lock:
        if len(_thumb_cache) > 500:
            _thumb_cache.clear()
        _thumb_cache[url] = (now, data, ctype)
    return StreamingResponse(io.BytesIO(data), media_type=ctype)


@app.get("/api/health")
def api_health():
    return {"ok": True, "service": "FetchGram"}


# -- static frontend (mounted last) ------------------------------------------
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
