"""
Reddit access. Three interchangeable backends (see docs/ARCHITECTURE.md → "Reddit access"):

  ArchiveClient  (default)  Arctic Shift, a public archive of Reddit with a documented
                            JSON API. Full post JSON incl. gallery metadata, full comment
                            trees with real scores. No Reddit rate-limit games.
  FetchClient    (optional) Unauthenticated JSON straight from www.reddit.com with a
                            browser User-Agent. Reddit blocks this per-IP for long
                            stretches once it notices you; kept for when it works.
  PrawClient     (optional) The official OAuth API, only if REDDIT_CLIENT_ID/SECRET are
                            set (registration is effectively closed since Nov 2025).

All three return the same plain-dict shapes so the sync doesn't care which is in use:

    listing(...)            -> list[dict]   (each dict = a post's `data` block)
    post_with_comments(...) -> (post_dict, list[comment_dict])

Comment dicts carry: id, author, body, score, depth, permalink, created_utc.
"""

import logging
import random
import time
from datetime import UTC, datetime

import requests
from django.conf import settings

log = logging.getLogger(__name__)

BASE_URL = "https://www.reddit.com"
ARCHIVE_URL = "https://arctic-shift.photon-reddit.com/api"


class RedditError(Exception):
    """Base class for anything that should stop the current sync gracefully."""


class RedditBlocked(RedditError):
    """Reddit served an interstitial / 403 / 429: we've been noticed. Stop the run and
    let cron try again later rather than hammering through it."""


class RedditNotFound(RedditError):
    pass


def _clean_comment(data, depth):
    return {
        "id": data.get("id", ""),
        "author": data.get("author") or "",
        "body": data.get("body") or "",
        "score": data.get("score") or 0,
        "depth": depth,
        "permalink": data.get("permalink") or "",
        "created_utc": data.get("created_utc") or 0,
        "distinguished": data.get("distinguished") or "",
        "stickied": bool(data.get("stickied")),
    }


def flatten_comments(children, depth=0, max_depth=3, out=None):
    """Walk Reddit's nested comment listing into a flat list. `more` stubs are skipped
    on purpose -- expanding them costs another request per stub and the parsing
    value of deep threads is low."""
    if out is None:
        out = []
    for child in children or []:
        if child.get("kind") != "t1":
            continue
        data = child.get("data", {})
        if data.get("body") in (None, "[deleted]", "[removed]"):
            continue
        out.append(_clean_comment(data, depth))
        replies = data.get("replies")
        if depth < max_depth:
            if isinstance(replies, dict):
                flatten_comments(replies.get("data", {}).get("children", []), depth + 1, max_depth, out)
            elif isinstance(replies, list):
                flatten_comments(replies, depth + 1, max_depth, out)
    return out


class FetchClient:
    """Unauthenticated old.reddit.com JSON client with pacing and block detection."""

    supports_backfill = False

    def __init__(self, user_agent=None, min_interval=None, jitter=None, timeout=None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent or settings.REDDIT_FETCH_USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )
        self.min_interval = settings.REDDIT_MIN_REQUEST_INTERVAL if min_interval is None else min_interval
        self.jitter = settings.REDDIT_REQUEST_JITTER if jitter is None else jitter
        self.timeout = timeout or settings.REDDIT_REQUEST_TIMEOUT
        self._last_request_at = 0.0
        self.requests_made = 0

    # -- plumbing -------------------------------------------------------------------

    def _pace(self):
        wait = self.min_interval + random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)

    def get_json(self, path, params=None):
        self._pace()
        url = f"{BASE_URL}{path}"
        params = {"raw_json": 1, **(params or {})}
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        finally:
            self._last_request_at = time.monotonic()
            self.requests_made += 1

        if resp.status_code == 404:
            raise RedditNotFound(f"{path} -> 404")
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            raise RedditBlocked(f"{path} -> HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise RedditError(f"{path} -> HTTP {resp.status_code}")

        ctype = resp.headers.get("Content-Type", "")
        if "json" not in ctype:
            # The soft block is a 200 with an HTML "Welcome to Reddit" page.
            raise RedditBlocked(f"{path} -> non-JSON response ({ctype.split(';')[0] or 'unknown'}); likely interstitial")
        try:
            return resp.json()
        except ValueError as exc:
            raise RedditError(f"{path} -> invalid JSON: {exc}") from exc

    # -- public API -----------------------------------------------------------------

    def listing(self, subreddit, sort="hot", limit=50, time_filter=None, before=None):
        # `before` (a backfill cursor) isn't meaningful for reddit.com's own listings --
        # only the archive backend can page arbitrarily far back in time.
        params = {"limit": min(int(limit), 100)}
        if sort == "top" and time_filter:
            params["t"] = time_filter
        data = self.get_json(f"/r/{subreddit}/{sort}.json", params)
        children = data.get("data", {}).get("children", [])
        return [c["data"] for c in children if c.get("kind") == "t3"]

    def post_with_comments(self, subreddit, post_id, limit=500):
        data = self.get_json(
            f"/r/{subreddit}/comments/{post_id}.json",
            {"limit": limit, "sort": "top", "depth": 4},
        )
        if not isinstance(data, list) or len(data) < 2:
            raise RedditError(f"unexpected comments payload for {post_id}")
        post_children = data[0].get("data", {}).get("children", [])
        post = post_children[0]["data"] if post_children else {}
        comments = flatten_comments(data[1].get("data", {}).get("children", []))
        return post, comments


class PrawClient:
    """Same interface as FetchClient, over the official API. Only constructed when
    OAuth credentials are present."""

    supports_backfill = False

    def __init__(self):
        import praw  # imported lazily so the package is only needed on this path

        self.reddit = praw.Reddit(
            client_id=settings.REDDIT_CLIENT_ID,
            client_secret=settings.REDDIT_CLIENT_SECRET,
            user_agent=settings.REDDIT_USER_AGENT,
            check_for_async=False,
        )
        self.reddit.read_only = True
        self.requests_made = 0

    @staticmethod
    def _submission_dict(s):
        d = {
            "id": s.id,
            "title": s.title,
            "author": str(s.author) if s.author else "",
            "permalink": s.permalink,
            "url": getattr(s, "url_overridden_by_dest", None) or s.url,
            "selftext": s.selftext or "",
            "link_flair_text": s.link_flair_text or "",
            "score": s.score,
            "upvote_ratio": s.upvote_ratio,
            "num_comments": s.num_comments,
            "created_utc": s.created_utc,
            "is_gallery": getattr(s, "is_gallery", False),
            "is_video": s.is_video,
            "over_18": s.over_18,
            "post_hint": getattr(s, "post_hint", ""),
            "thumbnail": s.thumbnail,
        }
        for key in ("media_metadata", "gallery_data", "preview"):
            if hasattr(s, key):
                d[key] = getattr(s, key)
        return d

    def listing(self, subreddit, sort="hot", limit=50, time_filter=None, before=None):
        # PRAW's own pagination (`.params={"after": ...}`) doesn't map onto a simple
        # timestamp cursor; only the archive backend backfills.
        self.requests_made += 1
        sub = self.reddit.subreddit(subreddit)
        if sort == "top":
            gen = sub.top(limit=limit, time_filter=time_filter or "month")
        else:
            gen = getattr(sub, sort)(limit=limit)
        return [self._submission_dict(s) for s in gen]

    def post_with_comments(self, subreddit, post_id, limit=500):
        self.requests_made += 1
        s = self.reddit.submission(id=post_id)
        s.comment_sort = "top"
        s.comments.replace_more(limit=0)
        comments = []

        def walk(forest, depth):
            for c in forest:
                if c.body in ("[deleted]", "[removed]"):
                    continue
                comments.append(
                    {
                        "id": c.id,
                        "author": str(c.author) if c.author else "",
                        "body": c.body,
                        "score": c.score,
                        "depth": depth,
                        "permalink": c.permalink,
                        "created_utc": c.created_utc,
                        "distinguished": c.distinguished or "",
                        "stickied": c.stickied,
                    }
                )
                if depth < 3:
                    walk(c.replies, depth + 1)

        walk(s.comments, 0)
        return self._submission_dict(s), comments


class ArchiveClient:
    """Arctic Shift (https://arctic-shift.photon-reddit.com) -- a public Reddit archive
    with a JSON API. Posts appear within the hour; a second retrieval pass ~a day later
    refreshes scores, comment counts and the comment tree. Limits: 100 items per page,
    recency sort only (popularity ordering is done in our own grid)."""

    supports_backfill = True

    def __init__(self, min_interval=1.0, jitter=0.5, timeout=60):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.ARCHIVE_USER_AGENT, "Accept": "application/json"})
        self.min_interval, self.jitter, self.timeout = min_interval, jitter, timeout
        self._last_request_at = 0.0
        self.requests_made = 0

    def _pace(self):
        wait = self.min_interval + random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)

    def get_json(self, path, params=None):
        self._pace()
        try:
            resp = self.session.get(f"{ARCHIVE_URL}{path}", params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RedditError(f"archive {path}: {exc}") from exc
        finally:
            self._last_request_at = time.monotonic()
            self.requests_made += 1
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RedditBlocked(f"archive {path} -> HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RedditError(f"archive {path} -> invalid JSON") from exc
        if resp.status_code != 200 or payload.get("error"):
            raise RedditError(f"archive {path} -> HTTP {resp.status_code}: {payload.get('error')}")
        return payload.get("data")

    def listing(self, subreddit, sort="hot", limit=50, time_filter=None, before=None):
        """Most recent posts. `before` (unix ts) pages further back."""
        params = {"subreddit": subreddit, "limit": min(int(limit), 100), "sort": "desc", "sort_type": "created_utc"}
        if before:
            params["before"] = int(before)
        posts = self.get_json("/posts/search", params) or []
        return [p for p in posts if p.get("id")]

    def post_with_comments(self, subreddit, post_id, limit=1000):
        tree = self.get_json("/comments/tree", {"link_id": post_id, "limit": limit}) or []
        comments = flatten_comments(tree, max_depth=4)
        # The listing already carries the (periodically refreshed) post record; one
        # request per post is enough.
        return {}, comments


def get_client():
    backend = settings.REDDIT_BACKEND
    if backend == "auto":
        backend = "praw" if (settings.REDDIT_CLIENT_ID and settings.REDDIT_CLIENT_SECRET) else "archive"
    log.info("Reddit backend: %s", backend)
    if backend == "praw":
        return PrawClient()
    if backend == "direct":
        return FetchClient()
    return ArchiveClient()


def utc_datetime(ts):
    return datetime.fromtimestamp(float(ts or 0), tz=UTC)
