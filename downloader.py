"""
FetchGram download engine.

Resolves shortcodes to direct media URLs (via ig_client), downloads the
files with a browser-impersonating client, and packages multi-file results
into a ZIP. Single-file results are returned directly (no ZIP to unpack).

Safety limits: max 50 items per request, max ~800 MB total download.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from curl_cffi import requests as creq

from ig_client import (
    IGError, IGNotFound, IGPrivate, IGRateLimited,
    MediaInfo, get_media_cached,
)
from listing import shortcode_from_url

MAX_ITEMS = 50
MAX_TOTAL_BYTES = 800 * 1024 * 1024
CHUNK = 1024 * 256

_DL_HEADERS = {
    "Referer": "https://www.instagram.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
}


@dataclass
class DownloadedFile:
    filename: str
    content_type: str
    data: bytes
    shortcode: str


def _ext_for(kind: str, content_type: str) -> str:
    if "mp4" in content_type or kind == "video":
        return "mp4"
    if "jpeg" in content_type or "jpg" in content_type:
        return "jpg"
    if "png" in content_type:
        return "png"
    if "webp" in content_type:
        return "webp"
    return "mp4" if kind == "video" else "jpg"


def _download_one(url: str, kind: str, filename: str) -> DownloadedFile:
    # curl_cffi is used (not httpx): it honors this environment's egress
    # proxy settings, while httpx chokes on the proxy env vars here.
    s = creq.Session(impersonate="chrome")
    r = s.get(url, headers=_DL_HEADERS, timeout=60, stream=True)
    if r.status_code != 200:
        raise IGError(f"Instagram refused this file (HTTP {r.status_code}).")
    ctype = r.headers.get("content-type", "")
    buf = io.BytesIO()
    total = 0
    for chunk in r.iter_content(CHUNK):
        buf.write(chunk)
        total += len(chunk)
        if total > MAX_TOTAL_BYTES:
            raise IGError("Download too large, aborted.")
    data = buf.getvalue()
    r.close()
    if not data:
        raise IGError("Downloaded file was empty.")
    ext = _ext_for(kind, ctype)
    if not filename.endswith("." + ext):
        filename = f"{filename}.{ext}"
    return DownloadedFile(filename, ctype, data, "")


def resolve_inputs(items: list[str]) -> list[str]:
    """Normalize user input (shortcodes or URLs) to a deduped shortcode list."""
    codes: list[str] = []
    seen: set[str] = set()
    for raw in items or []:
        raw = (raw or "").strip()
        if not raw:
            continue
        sc = shortcode_from_url(raw) or re_shortcode(raw)
        if sc and sc not in seen:
            seen.add(sc)
            codes.append(sc)
    return codes


def re_shortcode(raw: str) -> str | None:
    raw = raw.strip().strip("/")
    # bare shortcode (no slashes, plausible charset)
    if re.fullmatch(r"[A-Za-z0-9_-]{5,40}", raw):
        return raw
    return None


def download_items(shortcodes: list[str]) -> tuple[list[DownloadedFile], list[dict]]:
    """
    Download all slides for the given shortcodes.
    Returns (files, errors). Errors are per-shortcode dicts for the UI.
    """
    if len(shortcodes) > MAX_ITEMS:
        raise IGError(f"Too many items: max {MAX_ITEMS} per download.")

    # 1) resolve media info (cached, parallel)
    infos: dict[str, MediaInfo | Exception] = {}
    def _resolve(sc: str):
        try:
            infos[sc] = get_media_cached(sc)
        except Exception as e:  # noqa: BLE001 - collected per item
            infos[sc] = e

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_resolve, shortcodes))

    # 2) build download jobs
    jobs: list[tuple[str, str, str, str]] = []  # url, kind, filename, shortcode
    errors: list[dict] = []
    for sc in shortcodes:
        info = infos[sc]
        if isinstance(info, Exception):
            errors.append({"shortcode": sc, "error": _friendly(info)})
            continue
        base = f"{info.username or 'instagram'}_{sc}"
        for i, slide in enumerate(info.slides):
            suffix = f"_{i+1}" if len(info.slides) > 1 else ""
            jobs.append((slide.url, slide.kind, f"{base}{suffix}", sc))

    # 3) download in parallel
    files: list[DownloadedFile] = []
    lock = threading.Lock()
    total = 0

    def _fetch(job):
        nonlocal total
        url, kind, filename, sc = job
        try:
            f = _download_one(url, kind, filename)
            f.shortcode = sc
            with lock:
                total += len(f.data)
                if total > MAX_TOTAL_BYTES:
                    raise IGError("Total download size exceeded the limit.")
                files.append(f)
        except Exception as e:  # noqa: BLE001 - collected per item
            with lock:
                errors.append({"shortcode": sc, "error": _friendly(e)})

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_fetch, jobs))

    # keep original order
    order = {sc: i for i, sc in enumerate(shortcodes)}
    files.sort(key=lambda f: order.get(f.shortcode, 999))
    return files, errors


def _friendly(e: Exception) -> str:
    if isinstance(e, IGNotFound):
        return str(e)
    if isinstance(e, IGPrivate):
        return str(e)
    if isinstance(e, IGRateLimited):
        return str(e)
    if "refused" in str(e) or "403" in str(e):
        return "Instagram refused this file. Try again in a few minutes."
    return "Could not download this item."


def build_zip(files: list[DownloadedFile], errors: list[dict]) -> str:
    """Write files (+ an errors note if any) to a temp ZIP. Returns path."""
    fd, path = tempfile.mkstemp(prefix="fetchgram_", suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.writestr(f.filename, f.data)
        if errors:
            note = "\n".join(
                f"- {e['shortcode']}: {e['error']}" for e in errors)
            z.writestr("_skipped.txt",
                       "Some items could not be downloaded:\n" + note + "\n")
    return path
