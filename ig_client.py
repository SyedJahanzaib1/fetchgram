"""
FetchGram Instagram client — logged-out media extraction.

Instagram blocks plain scraping of profile/post pages (login wall) from
server IPs, and yt-dlp's instagram:user extractor is currently marked broken.
What *does* work without any login is the same logged-out GraphQL flow that
yt-dlp itself uses for single posts:

  1. GET https://www.instagram.com/            -> session cookies + LSD token
  2. GET /api/v1/web/get_ruling_for_content/   -> CSRF token / access ruling
  3. POST /api/graphql (PolarisLoggedOutDesktopWWWPostRootContentQuery)
     with variables {"media_id": <pk>}         -> full media info

The GraphQL response contains image_versions2 (full-quality photo URLs),
video_versions (progressive mp4 URLs) and carousel_media — so photos,
videos, reels AND carousels are all supported, which yt-dlp alone cannot do
(it errors on photo posts).

Rate limiting: Instagram throttles this endpoint under heavy use. Callers
should cache results (see app.py) and surface IGRateLimited cleanly.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

from curl_cffi import requests as creq

APP_ID = "936619743392459"  # Instagram web app id (public, same as yt-dlp uses)
ASBD_ID = "359341"
GRAPHQL_DOC_ID = "27130156389949648"  # PolarisLoggedOutDesktopWWWPostRootContentQuery
GRAPHQL_FRIENDLY = "PolarisLoggedOutDesktopWWWPostRootContentQuery"

_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class IGError(Exception):
    """Base Instagram extraction error."""


class IGNotFound(IGError):
    """The shortcode/username does not exist or was deleted."""


class IGPrivate(IGError):
    """The content is private / gated for logged-out viewers."""


class IGRateLimited(IGError):
    """Instagram is throttling anonymous requests right now."""


def shortcode_to_pk(shortcode: str) -> int:
    pk = 0
    for ch in shortcode:
        pk = pk * 64 + _SHORTCODE_ALPHABET.index(ch)
    return pk


def pk_to_shortcode(pk: int) -> str:
    if pk <= 0:
        return ""
    out = ""
    while pk:
        pk, rem = divmod(pk, 64)
        out = _SHORTCODE_ALPHABET[rem] + out
    return out


@dataclass
class MediaSlide:
    kind: str          # "photo" | "video"
    url: str
    width: int | None = None
    height: int | None = None


@dataclass
class MediaInfo:
    shortcode: str
    url: str
    media_type: str    # "photo" | "video" | "carousel"
    username: str = ""
    full_name: str = ""
    caption: str = ""
    taken_at: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    thumbnail: str = ""
    slides: list[MediaSlide] = field(default_factory=list)


def _img_size_hint(url: str) -> int:
    """Extract the downscale width from an Instagram CDN image URL.

    URLs look like ...?stp=dst-jpg_e35_p1080x... (1080px wide) or
    ...?stp=dst-jpg_e35_tt6 (no _p marker = original, full quality).
    """
    m = re.search(r"_p(\d+)", url)
    return int(m.group(1)) if m else 10**9


def _is_crop(url: str) -> bool:
    # stp=c0.250... = cropped variant; stp=dst-jpg... = full frame
    return "stp=c" in url


def _best_image(candidates: list[dict], small: bool = False) -> dict | None:
    """Pick the best photo URL. small=True picks a grid-friendly ~640px one."""
    scored = []
    for c in candidates or []:
        url = c.get("url")
        if not url:
            continue
        size = _img_size_hint(url)
        scored.append((size, _is_crop(url), url, c))
    if not scored:
        return None
    if small:
        # closest to 640px wide, preferring non-crops
        scored.sort(key=lambda t: (t[1], abs(t[0] - 640)))
    else:
        # biggest first, preferring non-crops
        scored.sort(key=lambda t: (t[1], -t[0]))
    return scored[0][3]


def _best_video(versions: list[dict], session=None) -> dict | None:
    """Pick the best video URL.

    GraphQL video_versions carry no dimensions, so HEAD each candidate and
    take the largest file — reliably the highest quality rendition.
    """
    cands = [v for v in versions or [] if v.get("url")]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    s = session or creq.Session(impersonate="chrome")
    best, best_len = cands[0], -1
    for v in cands:
        try:
            r = s.head(v["url"], headers={"Referer": "https://www.instagram.com/"},
                       timeout=20)
            ln = int(r.headers.get("content-length") or 0)
        except Exception:  # noqa: BLE001 - fall back to first candidate
            ln = 0
        if ln > best_len:
            best, best_len = v, ln
    return best


def _slides_from_product(prod: dict, session=None) -> tuple[str, list[MediaSlide], str]:
    """Extract (media_type, slides, thumbnail) from a GraphQL product dict."""
    thumb = ""
    iv = (prod.get("image_versions2") or {}).get("candidates") or []
    if iv:
        b = _best_image(iv, small=True)
        if b:
            thumb = b["url"]

    def photo_slide(node: dict) -> MediaSlide | None:
        b = _best_image((node.get("image_versions2") or {}).get("candidates") or [])
        return MediaSlide("photo", b["url"], None, None) if b else None

    def video_slide(node: dict) -> MediaSlide | None:
        b = _best_video(node.get("video_versions") or [], session=session)
        if b:
            return MediaSlide("video", b["url"], None, None)
        return photo_slide(node)  # video gone but poster image remains

    carousel = prod.get("carousel_media") or []
    if carousel:
        slides: list[MediaSlide] = []
        for child in carousel:
            mt = child.get("media_type")
            s = video_slide(child) if mt == 2 else photo_slide(child)
            if s:
                slides.append(s)
        return "carousel", slides, thumb

    mt = prod.get("media_type")
    if mt == 2:  # video / reel
        s = video_slide(prod)
        return "video", [s] if s else [], thumb
    # photo (media_type 1) — also covers anything else with image candidates
    s = photo_slide(prod)
    return "photo", [s] if s else [], thumb


class IGClient:
    """Logged-out Instagram media client. Create one per thread/task."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._s = creq.Session(impersonate="chrome")
        self._lock = threading.Lock()
        self._ready = False
        self._lsd = ""

    # -- session setup ----------------------------------------------------
    def _ensure_session(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            r = self._s.get("https://www.instagram.com/", timeout=self.timeout)
            if r.status_code != 200:
                raise IGError("Could not start an Instagram session")
            m = re.search(r'\["LSD",\[\],\{"token":"([^"]+)"\}', r.text)
            if not m:
                raise IGError("Could not start an Instagram session")
            self._lsd = m.group(1)
            self._ready = True

    def _base_headers(self, referer: str = "https://www.instagram.com/") -> dict:
        return {
            "X-IG-App-ID": APP_ID,
            "X-ASBD-ID": ASBD_ID,
            "X-IG-WWW-Claim": "0",
            "X-FB-LSD": self._lsd,
            "Origin": "https://www.instagram.com",
            "Referer": referer,
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        }

    # -- public API --------------------------------------------------------
    def get_media(self, shortcode: str) -> MediaInfo:
        """Resolve a post/reel shortcode to full media info + download URLs."""
        self._ensure_session()
        shortcode = shortcode.strip().strip("/")
        pk = str(shortcode_to_pk(shortcode))
        page_url = f"https://www.instagram.com/p/{shortcode}/"

        with self._lock:
            # 1) access ruling -> csrf token
            rc = self._s.get(
                "https://www.instagram.com/api/v1/web/get_ruling_for_content/",
                params={"content_type": "MEDIA", "target_id": pk},
                headers={**self._base_headers(page_url),
                         "X-CSRFToken": self._s.cookies.get("csrftoken", "")},
                timeout=self.timeout,
            )
            if rc.status_code == 429:
                raise IGRateLimited("Instagram is rate-limiting requests right now. "
                                    "Please wait a few minutes and try again.")
            csrf = ""
            if rc.headers.get("content-type", "").startswith("application/json"):
                csrf = rc.json().get("csrf_token") or ""
            csrf = csrf or self._s.cookies.get("csrftoken", "")
            if not csrf:
                raise IGRateLimited("Instagram is not granting access right now. "
                                    "Please wait a few minutes and try again.")

            # 2) logged-out GraphQL media query
            payload = {
                "lsd": self._lsd,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": GRAPHQL_FRIENDLY,
                "server_timestamps": "true",
                "variables": json.dumps({"media_id": pk}, separators=(",", ":")),
                "doc_id": GRAPHQL_DOC_ID,
            }
            g = self._s.post(
                "https://www.instagram.com/api/graphql",
                headers={**self._base_headers(page_url),
                         "X-FB-Friendly-Name": GRAPHQL_FRIENDLY,
                         "X-CSRFToken": csrf,
                         "Content-Type": "application/x-www-form-urlencoded"},
                data=payload,
                timeout=self.timeout,
            )

        if g.status_code == 429:
            raise IGRateLimited("Instagram is rate-limiting requests right now. "
                                "Please wait a few minutes and try again.")
        try:
            data = g.json()
        except Exception:
            raise IGError("Unexpected response from Instagram")

        media = (data.get("data") or {}).get("xig_polaris_media") or {}
        prod = media.get("if_not_gated_logged_out") or {}
        if not prod:
            title = ((data.get("data") or {}).get("xig_polaris_media") or {}).get("__typename", "")
            # Distinguish private/gated vs missing
            ruling = ""
            try:
                ruling = rc.json().get("description", "")
            except Exception:
                pass
            if "private" in ruling.lower() or "follower" in ruling.lower():
                raise IGPrivate("This post is private or only visible to followers.")
            raise IGNotFound("Post not found. It may be deleted, private, or the link is wrong.")

        media_type, slides, thumb = _slides_from_product(prod, session=self._s)
        if not slides:
            raise IGNotFound("No downloadable media found in this post.")

        user = prod.get("user") or {}
        caption = (prod.get("caption") or {}).get("text", "")
        return MediaInfo(
            shortcode=shortcode,
            url=page_url,
            media_type=media_type,
            username=user.get("username", ""),
            full_name=user.get("full_name", ""),
            caption=caption,
            taken_at=prod.get("taken_at"),
            like_count=prod.get("like_count"),
            comment_count=prod.get("comment_count"),
            thumbnail=thumb,
            slides=slides,
        )


# Simple TTL cache for media info (shared across threads)
_cache: dict[str, tuple[float, MediaInfo]] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 600  # 10 minutes


def get_media_cached(shortcode: str, timeout: int = 30) -> MediaInfo:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(shortcode)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    info = IGClient(timeout=timeout).get_media(shortcode)
    with _cache_lock:
        _cache[shortcode] = (now, info)
    return info
