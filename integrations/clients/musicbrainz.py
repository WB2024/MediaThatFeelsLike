"""
MusicBrainz -- the open, canonical music metadata database. Used on a music
recommendation's detail page to pin a loose "Artist - Title" to a real recording and
artist, each with a MusicBrainz ID (MBID). That ID is worth having on its own: it's what
Lidarr keys artists by internally (`foreignArtistId`), so it lets the Lidarr card match
exactly instead of fuzzily, and it's what the Cover Art Archive indexes by.

No API key -- but their etiquette is non-negotiable: a descriptive User-Agent with a
contact URL, and no more than ~1 request/second per IP for unauthenticated clients
(exceeding it returns HTTP 503). Both are handled here, so callers don't have to think
about it. Not a `ServiceConfig` service for the same reason: there's nothing to configure.

    GET /ws/2/recording/?query=<lucene>&fmt=json         -> recordings (+artist-credit, releases)
    GET /ws/2/artist/?query=<lucene>&fmt=json            -> artists
    GET /ws/2/artist/{mbid}?inc=url-rels+tags&fmt=json   -> one artist with external links
"""

import threading
import time
from urllib.parse import urlparse

import requests

from .base import ServiceError, similarity

BASE = "https://musicbrainz.org/ws/2"
COVER_ART = "https://coverartarchive.org"
USER_AGENT = "MediaThatFeelsLike/0.1 (+https://github.com/WB2024/MediaThatFeelsLike)"
MIN_INTERVAL = 1.05  # seconds between calls -- MusicBrainz's published limit is 1/s

_lock = threading.Lock()
_last_call = 0.0

# MusicBrainz relation type -> label; "streaming"/"social network" are resolved by domain.
LINK_TYPES = {
    "official homepage": "Website", "wikipedia": "Wikipedia", "discogs": "Discogs", "allmusic": "AllMusic",
    "bandcamp": "Bandcamp", "youtube": "YouTube", "last.fm": "Last.fm", "songkick": "Songkick",
    "soundcloud": "SoundCloud", "setlistfm": "setlist.fm", "IMDb": "IMDb",
}
DOMAIN_LABELS = {
    "open.spotify.com": "Spotify", "music.apple.com": "Apple Music", "tidal.com": "Tidal", "deezer.com": "Deezer",
    "twitter.com": "Twitter", "x.com": "Twitter", "instagram.com": "Instagram", "facebook.com": "Facebook",
    "en.wikipedia.org": "Wikipedia",
}


def _phrase(text):
    """Quote a value for a Lucene phrase query."""
    return '"' + (text or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


class MusicBrainzClient:
    """`min_interval` is the per-process pacing; the background verifier runs at half
    speed so the web container's page loads (same public IP, same 1 req/s budget at
    MusicBrainz's end) still get through."""

    def __init__(self, session=None, min_interval=MIN_INTERVAL):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.min_interval = min_interval

    def get(self, path, **params):
        params["fmt"] = "json"
        resp = self._paced_get(path, params)
        if resp.status_code == 503:
            # The rate limit is per IP and shared with everything else on the LAN; one
            # polite retry covers the usual collision.
            time.sleep(max(2.0, self.min_interval))
            resp = self._paced_get(path, params)
        if resp.status_code == 503:
            raise ServiceError("MusicBrainz: rate limited (503) -- try again in a moment")
        if resp.status_code >= 400:
            raise ServiceError(f"MusicBrainz: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ServiceError("MusicBrainz: non-JSON response") from exc

    def _paced_get(self, path, params):
        global _last_call
        with _lock:
            wait = self.min_interval - (time.monotonic() - _last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                return self.session.get(f"{BASE}{path}", params=params, timeout=15)
            except requests.RequestException as exc:
                raise ServiceError(f"MusicBrainz: {exc.__class__.__name__}: {exc}") from exc
            finally:
                _last_call = time.monotonic()

    # -- lookups --------------------------------------------------------------------------

    def search_recording(self, artist, title):
        """Best recording for "Artist - Title", shaped, or None. Both halves have to
        actually look right -- MusicBrainz's own `score` is relevance, not correctness,
        and will happily rank a cover version first."""
        query = f"recording:{_phrase(title)}"
        if artist:
            query += f" AND artist:{_phrase(artist)}"
        recordings = (self.get("/recording/", query=query, limit=10) or {}).get("recordings") or []
        best, best_score = None, 0.0
        for r in recordings:
            credit = r.get("artist-credit") or []
            credited = " ".join(c.get("name") or "" for c in credit if isinstance(c, dict))
            a_sim = similarity(credited, artist) if artist else 0.8
            t_sim = similarity(r.get("title") or "", title)
            if a_sim < 0.6 or t_sim < 0.6:
                continue
            # A popular track exists as several "recordings" (album master, live take,
            # piano version...) that all match the title equally. The canonical one is
            # the one that appears on the most official, plain albums -- weight that.
            releases = r.get("releases") or []
            official_albums = sum(
                1 for rel in releases
                if rel.get("status") == "Official" and (rel.get("release-group") or {}).get("primary-type") == "Album"
                and not (rel.get("release-group") or {}).get("secondary-types")
            )
            score = 0.5 * a_sim + 0.5 * t_sim + min(official_albums, 10) / 50 + min(len(releases), 20) / 200 + (r.get("score") or 0) / 1000
            if score > best_score:
                best, best_score = r, score
        return shape_recording(best) if best else None

    def search_artist(self, name):
        artists = (self.get("/artist/", query=f"artist:{_phrase(name)}", limit=5) or {}).get("artists") or []
        best, best_score = None, 0.0
        for a in artists:
            sim = similarity(a.get("name") or "", name)
            if sim >= 0.8 and sim + (a.get("score") or 0) / 1000 > best_score:
                best, best_score = a, sim + (a.get("score") or 0) / 1000
        return shape_artist(best) if best else None

    def artist(self, mbid):
        """One artist with its external links, tags and dates."""
        return shape_artist(self.get(f"/artist/{mbid}", inc="url-rels+tags+genres") or {})


def cover_art_urls(release_group_id=None, release_id=None, size=500):
    """Candidate Cover Art Archive URLs, most likely first; each 404s if there's no art
    (the template tries them in order)."""
    urls = []
    if release_group_id:
        urls.append(f"{COVER_ART}/release-group/{release_group_id}/front-{size}")
    if release_id:
        urls.append(f"{COVER_ART}/release/{release_id}/front-{size}")
    return urls


def _pick_release(releases):
    """The release most worth showing for a recording: an official album first, then
    an official anything, then whatever's earliest."""
    def rank(r):
        rg = r.get("release-group") or {}
        return (
            r.get("status") == "Official",
            rg.get("primary-type") == "Album",
            not (rg.get("secondary-types") or []),   # plain album over compilation/live/remix
            -(int((r.get("date") or "9999")[:4]) if (r.get("date") or "")[:4].isdigit() else 9999),
        )
    return max(releases, key=rank) if releases else None


def shape_recording(r):
    credit = [c for c in (r.get("artist-credit") or []) if isinstance(c, dict) and c.get("artist")]
    releases = r.get("releases") or []
    primary = _pick_release(releases)
    seen, release_rows = set(), []
    for rel in sorted(releases, key=lambda x: (x.get("date") or "9999")):
        rg = rel.get("release-group") or {}
        key = rg.get("id") or rel.get("id")
        if key in seen:
            continue
        seen.add(key)
        kind = rg.get("primary-type") or ""
        if rg.get("secondary-types"):
            kind = f"{kind} · {', '.join(rg['secondary-types'])}"
        release_rows.append({"title": rel.get("title"), "year": (rel.get("date") or "")[:4], "type": kind, "country": rel.get("country") or "",
                             "url": f"https://musicbrainz.org/release/{rel.get('id')}"})
    length_ms = r.get("length") or 0
    return {
        "mbid": r.get("id"),
        "title": r.get("title"),
        "artist": credit[0]["artist"].get("name") if credit else "",
        "artist_mbid": credit[0]["artist"].get("id") if credit else "",
        "length": f"{length_ms // 60000}:{(length_ms // 1000) % 60:02d}" if length_ms else "",
        "album": (primary or {}).get("title") or "",
        "album_year": ((primary or {}).get("date") or "")[:4],
        "album_type": ((primary or {}).get("release-group") or {}).get("primary-type") or "",
        "release_group_mbid": ((primary or {}).get("release-group") or {}).get("id") or "",
        "release_mbid": (primary or {}).get("id") or "",
        "releases": release_rows[:8],
        "release_count": len(release_rows),
        "isrcs": r.get("isrcs") or [],
        "url": f"https://musicbrainz.org/recording/{r.get('id')}",
    }


def shape_artist(a):
    links, seen = [], set()
    for rel in a.get("relations") or []:
        url = (rel.get("url") or {}).get("resource")
        if not url:
            continue
        label = LINK_TYPES.get(rel.get("type")) or DOMAIN_LABELS.get(urlparse(url).netloc.replace("www.", ""))
        if label and label not in seen:
            seen.add(label)
            links.append({"label": label, "url": url})
    span = a.get("life-span") or {}
    years = span.get("begin") or ""
    if years:
        years += f" – {span.get('end') or ('' if not span.get('ended') else '?')}" if (span.get("end") or span.get("ended")) else " – present"
    tags = sorted(a.get("tags") or [], key=lambda t: -(t.get("count") or 0))
    return {
        "mbid": a.get("id"),
        "name": a.get("name"),
        "type": a.get("type") or "",
        "country": a.get("country") or ((a.get("area") or {}).get("name") or ""),
        "years": years,
        "disambiguation": a.get("disambiguation") or "",
        "tags": [t.get("name") for t in tags[:10] if t.get("name")],
        "links": links[:10],
        "url": f"https://musicbrainz.org/artist/{a.get('id')}" if a.get("id") else "",
    }
