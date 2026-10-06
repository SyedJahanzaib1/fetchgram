"""
FetchGram profile listing provider.

Lists a public profile's posts (shortcode, type, caption, likes, timestamp).
This uses the `instagram-cli` tool, which talks to Instagram through the
site owner's own linked Instagram account — visitors of the website never
need to log in.

Why not scrape anonymously? Instagram serves a login wall to datacenter
server IPs for profile pages, and yt-dlp's instagram:user extractor is
currently marked broken. This provider is intentionally swappable: implement
the two functions below against any other source to change backends.

Deployment note: the server needs `instagram-cli` installed and one
Instagram account linked (see README). Instagram rate-limits these calls
(roughly a few dozen per window), so results are cached (see app.py).
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field


class ListingError(Exception):
    pass


class ProfileNotFound(ListingError):
    pass


class ProfilePrivate(ListingError):
    pass


class ListingRateLimited(ListingError):
    pass


@dataclass
class ProfileInfo:
    username: str
    full_name: str = ""
    bio: str = ""
    avatar: str = ""
    followers: int = 0
    following: int = 0
    post_count: int = 0
    is_private: bool = False
    is_verified: bool = False
    website: str = ""


@dataclass
class ListedPost:
    shortcode: str
    url: str
    kind: str          # "photo" | "video" | "carousel"  (video covers reels)
    is_reel: bool = False
    caption: str = ""
    likes: int = 0
    comments: int = 0
    taken_at: str = ""  # ISO-ish string from provider
    thumbnail: str = ""  # filled in by app.py via ig_client


SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


def shortcode_from_url(url: str) -> str | None:
    m = SHORTCODE_RE.search(url or "")
    return m.group(1) if m else None


def _looks_rate_limited(stderr: str, data: dict | None) -> bool:
    """Detect a real rate limit. NOTE: never substring-search the raw stdout —
    it contains fbids that can include the digit sequence '429'."""
    if re.search(r"429|too many requests|rate.?limit", stderr or "", re.I):
        return True
    if isinstance(data, dict):
        err = data.get("error")
        if err:
            txt = json.dumps(err)
            if re.search(r"429|too many requests|rate.?limit", txt, re.I):
                return True
    return False


def _run_cli(args: list[str], timeout: int = 60) -> dict:
    """Run instagram-cli and return parsed JSON. Maps failures to errors."""
    try:
        p = subprocess.run(
            ["instagram-cli", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise ListingError("instagram-cli is not installed on this server.")
    except subprocess.TimeoutExpired:
        raise ListingError("Instagram request timed out. Try again.")
    out = (p.stdout or "").strip()
    if not out:
        if _looks_rate_limited(p.stderr, None):
            raise ListingRateLimited(
                "Instagram is rate-limiting profile lookups right now. "
                "Please wait a few minutes and try again.")
        raise ListingError(f"instagram-cli failed: {(p.stderr or '')[:200]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        if "not connected" in out.lower():
            raise ListingError("Server Instagram account is not linked. "
                               "The site owner needs to connect it (see README).")
        if _looks_rate_limited(p.stderr, None):
            raise ListingRateLimited(
                "Instagram is rate-limiting profile lookups right now. "
                "Please wait a few minutes and try again.")
        raise ListingError("Unexpected response from Instagram service.")
    if _looks_rate_limited(p.stderr, data):
        raise ListingRateLimited(
            "Instagram is rate-limiting profile lookups right now. "
            "Please wait a few minutes and try again.")
    return data


# One account id, resolved once. Thread-safe lazy init.
_account_id: str | None = None
_account_lock = threading.Lock()


def account_id() -> str:
    global _account_id
    if _account_id:
        return _account_id
    with _account_lock:
        if _account_id:
            return _account_id
        data = _run_cli(["accounts"])
        accounts = data.get("accounts") or []
        if not accounts:
            raise ListingError("No Instagram account is linked on this server. "
                               "The site owner needs to connect one (see README).")
        _account_id = str(accounts[0]["user_fbid"])
        return _account_id


def get_profile(username: str) -> ProfileInfo:
    username = username.strip().lstrip("@")
    data = _run_cli(["user-profile", "--account-id", account_id(),
                     "--username", username])
    profiles = data.get("profiles") or []
    if not profiles:
        raise ProfileNotFound(
            f'No public profile found for "{username}". Check the spelling — '
            "it may be renamed, deactivated, or private.")
    p = profiles[0]
    if p.get("is_private"):
        raise ProfilePrivate(
            f'@{username} is private. Only public profiles can be fetched — '
            "this is a limit of how Instagram works, not of this tool.")
    return ProfileInfo(
        username=p.get("username", username),
        full_name=p.get("name", ""),
        bio=p.get("bio", ""),
        avatar=p.get("profile_pic_url", ""),
        followers=p.get("follower_count") or 0,
        following=p.get("following_count") or 0,
        post_count=p.get("post_count") or 0,
        is_private=False,
        is_verified=bool(p.get("is_verified")),
        website=p.get("website", ""),
    )


@dataclass
class PostPage:
    posts: list[ListedPost] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


def list_posts(username: str, after: str | None = None,
               limit: int = 12) -> PostPage:
    username = username.strip().lstrip("@")
    args = ["posts", "--account-id", account_id(),
            "--username", username, "--limit", str(limit)]
    if after:
        args += ["--after", after]
    data = _run_cli(args, timeout=90)
    posts: list[ListedPost] = []
    for p in data.get("posts") or []:
        url = p.get("url") or ""
        sc = shortcode_from_url(url)
        if not sc:
            continue  # e.g. story/highlight links, not downloadable posts
        mtype = (p.get("media_type") or "").lower()
        is_reel = "/reel" in url
        kind = "video" if mtype == "video" else ("carousel" if mtype == "carousel" else "photo")
        posts.append(ListedPost(
            shortcode=sc,
            url=url,
            kind=kind,
            is_reel=is_reel,
            caption=(p.get("post_caption") or "")[:220],
            likes=p.get("likes") or 0,
            comments=p.get("comments") or 0,
            taken_at=p.get("post_created_at", {}).get("user_local", "")
                     if isinstance(p.get("post_created_at"), dict)
                     else str(p.get("created_at", "")),
        ))
    return PostPage(
        posts=posts,
        next_cursor=data.get("next_cursor"),
        has_more=bool(data.get("has_next_page")),
    )


# TTL caches (profile: 10 min, listings: 5 min)
_profile_cache: dict[str, tuple[float, ProfileInfo]] = {}
_listing_cache: dict[str, tuple[float, PostPage]] = {}
_cache_lock = threading.Lock()


def get_profile_cached(username: str) -> ProfileInfo:
    key = username.lower()
    now = time.time()
    with _cache_lock:
        hit = _profile_cache.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    info = get_profile(username)
    with _cache_lock:
        _profile_cache[key] = (now, info)
    return info


def list_posts_cached(username: str, after: str | None = None,
                      limit: int = 12) -> PostPage:
    key = f"{username.lower()}|{after}|{limit}"
    now = time.time()
    with _cache_lock:
        hit = _listing_cache.get(key)
        if hit and now - hit[0] < 300:
            return hit[1]
    page = list_posts(username, after, limit)
    with _cache_lock:
        _listing_cache[key] = (now, page)
    return page
