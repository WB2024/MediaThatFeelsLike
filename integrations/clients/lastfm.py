"""
Last.fm -- the "social" layer for a music recommendation's detail page: how many people
listen to it, what tags they give it, a written blurb for the track and the artist,
similar artists and the artist's most-played tracks. Complements MusicBrainz, which is
canonical about *what* a recording is but says nothing about how popular it is.

Read-only public methods only need the API key as a query parameter (no secret, no
request signing -- that's for scrobbling). Every response is `{"error", "message"}` on a
miss (e.g. error 6 "Track not found"), delivered with an HTTP 400 that `BaseClient`
already turns into a ServiceError.

    GET /2.0/?method=track.getInfo&artist=..&track=..&autocorrect=1
    GET /2.0/?method=artist.getInfo&artist=..   (or &mbid=.. when MusicBrainz gave us one)
    GET /2.0/?method=artist.getTopTracks&artist=..&limit=8

Last.fm no longer serves real artwork for most entries -- the `image` arrays come back
as the same grey placeholder (hash 2a96cbd8...) -- so images are dropped unless they're
something else. Cover art comes from the Cover Art Archive via MusicBrainz instead.
"""

import re

from .base import BaseClient

PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f"
_TAGS = re.compile(r"<[^>]+>")
_READ_MORE = re.compile(r"\s*Read more on Last\.fm\.?\s*$", re.I)


def _text(html):
    """Last.fm's wiki/bio fields are HTML with a trailing 'Read more on Last.fm' link;
    the template renders its own link, so strip both."""
    return _READ_MORE.sub("", _TAGS.sub("", html or "")).strip()


def _image(images):
    for img in reversed(images or []):     # largest last
        url = img.get("#text") or ""
        if url and PLACEHOLDER not in url:
            return url
    return ""


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class LastfmClient(BaseClient):
    api = "/2.0/"

    def __init__(self, config):
        super().__init__(config)
        self.session.params = {"api_key": config.api_key or "", "format": "json"}

    def test(self):
        info = self.get(self.api, method="artist.getInfo", artist="Cher")
        name = ((info or {}).get("artist") or {}).get("name")
        return f"Last.fm: connected (artist.getInfo → {name})" if name else "Last.fm: unexpected response"

    def refresh_choices(self):
        return None

    def track_info(self, artist, track):
        data = (self.get(self.api, method="track.getInfo", artist=artist, track=track, autocorrect=1) or {}).get("track")
        return shape_track(data) if data else None

    def artist_info(self, artist, mbid=None):
        params = {"mbid": mbid} if mbid else {"artist": artist, "autocorrect": 1}
        data = (self.get(self.api, method="artist.getInfo", **params) or {}).get("artist")
        return shape_artist(data) if data else None

    def artist_top_tracks(self, artist, mbid=None, limit=8):
        params = {"mbid": mbid} if mbid else {"artist": artist, "autocorrect": 1}
        data = (self.get(self.api, method="artist.getTopTracks", limit=limit, **params) or {}).get("toptracks") or {}
        return [
            {"name": t.get("name"), "playcount": _int(t.get("playcount")), "listeners": _int(t.get("listeners")), "url": t.get("url")}
            for t in data.get("track") or [] if isinstance(t, dict)
        ]


def shape_track(t):
    album = t.get("album") or {}
    duration_ms = _int(t.get("duration"))
    return {
        "name": t.get("name"),
        "artist": (t.get("artist") or {}).get("name") or "",
        "url": t.get("url") or "",
        "listeners": _int(t.get("listeners")),
        "playcount": _int(t.get("playcount")),
        "length": f"{duration_ms // 60000}:{(duration_ms // 1000) % 60:02d}" if duration_ms else "",
        "album": album.get("title") or "",
        "album_url": album.get("url") or "",
        "album_image": _image(album.get("image")),
        "tags": [tag.get("name") for tag in ((t.get("toptags") or {}).get("tag") or []) if isinstance(tag, dict) and tag.get("name")][:10],
        "wiki": _text((t.get("wiki") or {}).get("summary")),
    }


def shape_artist(a):
    stats = a.get("stats") or {}
    return {
        "name": a.get("name"),
        "url": a.get("url") or "",
        "listeners": _int(stats.get("listeners")),
        "playcount": _int(stats.get("playcount")),
        "tags": [tag.get("name") for tag in ((a.get("tags") or {}).get("tag") or []) if isinstance(tag, dict) and tag.get("name")][:10],
        "bio": _text((a.get("bio") or {}).get("summary")),
        "similar": [
            {"name": s.get("name"), "url": s.get("url")}
            for s in ((a.get("similar") or {}).get("artist") or []) if isinstance(s, dict) and s.get("name")
        ][:8],
        "image": _image(a.get("image")),
    }
