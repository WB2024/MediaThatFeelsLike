"""
Download a post's included recommendations as CSV, TXT, M3U or M3U8.

M3U entries carry a real library path when Navidrome (music) or Jellyfin (movies) is
configured and knows the item -- so the file imports straight into a player. Items the
library doesn't have get a search URL instead, so the playlist still lists them.
"""

import csv
import io
from urllib.parse import quote_plus

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.text import slugify

from integrations.clients.base import ServiceError
from integrations.models import ServiceConfig
from integrations.push import CLIENTS
from vibes.models import Post

FORMATS = {"csv": "text/csv", "txt": "text/plain", "m3u": "audio/x-mpegurl", "m3u8": "audio/x-mpegurl"}


def download(request, post_pk, fmt):
    if fmt not in FORMATS:
        raise Http404
    post = get_object_or_404(Post.objects.select_related("source"), pk=post_pk)
    recs = list(post.recommendations.filter(included=True).order_by("order"))
    kind = post.source.kind
    resolve = request.GET.get("resolve", "1") != "0"

    if fmt == "csv":
        body = _csv(recs, kind)
    elif fmt == "txt":
        body = _txt(post, recs)
    else:
        body = _m3u(post, recs, kind, resolve=resolve)

    filename = f"{slugify(post.title)[:60] or post.reddit_id}.{fmt}"
    charset = "utf-8"
    response = HttpResponse(body.encode(charset), content_type=f"{FORMATS[fmt]}; charset={charset}")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _csv(recs, kind):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if kind == "music":
        writer.writerow(["artist", "title", "year", "url", "confidence", "mentions", "comment_score", "source_comment_url"])
        for r in recs:
            writer.writerow([r.parsed_artist, r.parsed_title, r.parsed_year or "", r.parsed_url, r.confidence, r.mention_count, r.comment_score, r.comment_url])
    else:
        writer.writerow(["title", "year", "url", "confidence", "mentions", "comment_score", "source_comment_url"])
        for r in recs:
            writer.writerow([r.parsed_title, r.parsed_year or "", r.parsed_url, r.confidence, r.mention_count, r.comment_score, r.comment_url])
    return buf.getvalue()


def _txt(post, recs):
    lines = [post.title, f"r/{post.source.subreddit} · {post.reddit_url}", ""]
    lines += [r.display_label for r in recs]
    return "\n".join(lines) + "\n"


def _m3u(post, recs, kind, resolve=True):
    resolver = _resolver(kind) if resolve else None
    lines = ["#EXTM3U", f"#PLAYLIST:{post.title}"]
    for r in recs:
        label = r.display_label
        path = None
        if resolver:
            try:
                path = resolver(r)
            except ServiceError:
                resolver = None  # service went away mid-export; fall back for the rest
        if not path:
            path = _search_url(r, kind)
        lines.append(f"#EXTINF:-1,{label}")
        lines.append(path)
    return "\n".join(lines) + "\n"


def _resolver(kind):
    """Callable rec -> library path (or None), using whichever service knows the item."""
    if kind == "music":
        config = ServiceConfig.get(ServiceConfig.Service.NAVIDROME)
        if config.is_configured:
            client = CLIENTS[config.service](config)

            def resolve(rec):
                song, _, _ = client.resolve(rec)
                return song.get("path") if song else None

            return resolve
    config = ServiceConfig.get(ServiceConfig.Service.JELLYFIN)
    if config.is_configured:
        client = CLIENTS[config.service](config)

        def resolve(rec):
            item, _, _ = client.resolve(rec, kind)
            return item.get("Path") if item else None

        return resolve
    return None


def _search_url(rec, kind):
    if rec.parsed_url:
        return rec.parsed_url
    term = quote_plus(rec.search_term or rec.display_label)
    if kind == "music":
        return f"https://open.spotify.com/search/{term}"
    return f"https://letterboxd.com/search/{term}/"
