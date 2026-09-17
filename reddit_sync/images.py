"""
Pulling image URLs out of a Reddit post and caching local copies.

Reddit posts carry images in several shapes: a direct i.redd.it / imgur link, a gallery
(`media_metadata` + `gallery_data`), or -- for link/video/text posts -- only a `preview`
block. We take the best we can get in that order.
"""

import html
import io
import logging
import mimetypes
import time
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
THUMB_MAX_WIDTH = 640
IMAGE_HOSTS = ("i.redd.it", "preview.redd.it", "i.imgur.com", "imgur.com", "external-preview.redd.it")


def _unescape(url):
    return html.unescape(url) if url else url


def extract_images(post):
    """Return a list of {"url", "width", "height", "caption"} for a post dict, best first.
    Empty when the post has nothing we can show (pure text posts)."""
    images = []

    if post.get("is_gallery") and isinstance(post.get("media_metadata"), dict):
        order = [item.get("media_id") for item in (post.get("gallery_data") or {}).get("items", [])]
        captions = {
            item.get("media_id"): item.get("caption", "")
            for item in (post.get("gallery_data") or {}).get("items", [])
        }
        meta = post["media_metadata"]
        ids = [i for i in order if i in meta] or list(meta.keys())
        for media_id in ids:
            entry = meta.get(media_id) or {}
            if entry.get("status") != "valid":
                continue
            source = entry.get("s") or {}
            url = source.get("u") or source.get("gif") or source.get("mp4")
            if not url:
                continue
            images.append(
                {
                    "url": _unescape(url),
                    "width": source.get("x"),
                    "height": source.get("y"),
                    "caption": captions.get(media_id, "") or "",
                }
            )
        if images:
            return images

    url = post.get("url_overridden_by_dest") or post.get("url") or ""
    parsed = urlparse(url)
    ext = PurePosixPath(parsed.path).suffix.lower()
    if parsed.netloc in IMAGE_HOSTS and ext in IMAGE_EXTENSIONS or post.get("post_hint") == "image":
        dims = _preview_dims(post)
        images.append({"url": _unescape(url), "width": dims[0], "height": dims[1], "caption": ""})
        return images

    # Link / video / text posts: fall back to the preview Reddit generated.
    preview = (post.get("preview") or {}).get("images") or []
    if preview:
        source = preview[0].get("source") or {}
        if source.get("url"):
            images.append(
                {
                    "url": _unescape(source["url"]),
                    "width": source.get("width"),
                    "height": source.get("height"),
                    "caption": "",
                }
            )
            return images

    thumb = post.get("thumbnail") or ""
    if thumb.startswith("http"):
        images.append({"url": _unescape(thumb), "width": None, "height": None, "caption": ""})
    return images


def _preview_dims(post):
    preview = (post.get("preview") or {}).get("images") or []
    if preview:
        source = preview[0].get("source") or {}
        return source.get("width"), source.get("height")
    return None, None


class ImageCacher:
    """Downloads post images into MEDIA_ROOT and writes a JPEG thumbnail for the grid.
    Uses its own light pacing -- the image CDN isn't the API, but there's no reason to
    hammer it either."""

    def __init__(self, user_agent=None, interval=0.75, timeout=30):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent or settings.REDDIT_FETCH_USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": "https://www.reddit.com/",
            }
        )
        self.interval = interval
        self.timeout = timeout
        self._last = 0.0

    def fetch(self, url):
        gap = self.interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        try:
            resp = self.session.get(url, timeout=self.timeout)
        finally:
            self._last = time.monotonic()
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "")

    def cache(self, image):
        """Populate PostImage.file / .thumb. Returns True on success. Failures are
        recorded on the row so the next sync doesn't retry them forever."""
        try:
            content, ctype = self.fetch(image.source_url)
            with Image.open(io.BytesIO(content)) as im:
                im.load()
                fmt = (im.format or "JPEG").lower()
                width, height = im.size
                thumb_bytes = self._make_thumb(im)
        except (requests.RequestException, UnidentifiedImageError, OSError, ValueError) as exc:
            log.warning("image cache failed for %s: %s", image.source_url, exc)
            image.cache_failed = True
            image.save(update_fields=["cache_failed"])
            return False

        ext = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}.get(fmt) or (
            mimetypes.guess_extension(ctype.split(";")[0]) or ".jpg"
        )
        stem = f"{image.post.reddit_id}_{image.order}"
        image.file.save(f"{stem}{ext}", ContentFile(content), save=False)
        image.thumb.save(f"{stem}.jpg", ContentFile(thumb_bytes), save=False)
        image.width = image.width or width
        image.height = image.height or height
        image.cached_at = timezone.now()
        image.cache_failed = False
        image.save()
        return True

    @staticmethod
    def _make_thumb(im):
        thumb = im.convert("RGB")
        if thumb.width > THUMB_MAX_WIDTH:
            ratio = THUMB_MAX_WIDTH / thumb.width
            thumb = thumb.resize((THUMB_MAX_WIDTH, max(1, int(thumb.height * ratio))), Image.LANCZOS)
        buf = io.BytesIO()
        thumb.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue()
