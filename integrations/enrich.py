"""
Everything the configured services know about ONE recommendation -- the data behind a
recommendation's own detail page (`vibes.views.rec_detail`) and the htmx panels that
page lazy-loads (`integrations.views.rec_card` / `rec_artist` / `rec_slskd_*`).

Every function here degrades rather than raises: a page about a film must still render
when Jellyfin is down, and "TMDB has never heard of this title" is a normal outcome, not
an error. Errors are returned as strings for the template to show inline.
"""

from django.utils import timezone

from .clients.base import ServiceError, similarity
from .clients.jellyfin import JellyfinClient
from .clients.lastfm import LastfmClient
from .clients.lidarr import LidarrClient
from .clients.musicbrainz import MusicBrainzClient, cover_art_urls
from .clients.navidrome import NavidromeClient
from .clients.radarr import RadarrClient
from .clients.slskd import SlskdClient, describe_file
from .clients.tmdb import TmdbClient
from .models import ServiceConfig
from .push import _record, push_one

S = ServiceConfig.Service
LABELS = {S.RADARR: "Radarr", S.LIDARR: "Lidarr", S.JELLYFIN: "Jellyfin", S.NAVIDROME: "Navidrome"}


def _client(service, cls):
    config = ServiceConfig.get(service)
    return cls(config) if config.is_configured else None


def _gb(size_bytes):
    return f"{(size_bytes or 0) / 1073741824:.1f} GB" if size_bytes else ""


# -- the page itself --------------------------------------------------------------------


def movie_page(rec):
    out = {"tmdb": None, "tmdb_configured": False, "error": ""}
    client = _client(S.TMDB, TmdbClient)
    if not client:
        return out
    out["tmdb_configured"] = True
    try:
        out["tmdb"] = client.movie_for(rec.parsed_title, rec.parsed_year)
    except ServiceError as exc:
        out["error"] = str(exc)
    return out


def music_page(rec):
    """MusicBrainz pins the recording (and gives us the artist MBID + cover art keys);
    Last.fm adds the social layer for the track. The artist section is loaded separately
    (`artist_panel`) so MusicBrainz's 1 req/s limit isn't hit twice in one request."""
    out = {"mb": None, "mb_artist": None, "track": None, "lastfm_configured": False, "cover_urls": [], "errors": []}
    mb = MusicBrainzClient()
    try:
        if rec.parsed_title:
            out["mb"] = mb.search_recording(rec.parsed_artist, rec.parsed_title)
        elif rec.parsed_artist:
            out["mb_artist"] = mb.search_artist(rec.parsed_artist)
    except ServiceError as exc:
        out["errors"].append(str(exc))

    lastfm = _client(S.LASTFM, LastfmClient)
    if lastfm:
        out["lastfm_configured"] = True
        if rec.parsed_title:
            try:
                out["track"] = lastfm.track_info(rec.parsed_artist, rec.parsed_title)
            except ServiceError as exc:
                if "not found" not in str(exc).lower():
                    out["errors"].append(str(exc))

    mbd = out["mb"] or {}
    out["cover_urls"] = cover_art_urls(mbd.get("release_group_mbid"), mbd.get("release_mbid"))
    if (out["track"] or {}).get("album_image"):
        out["cover_urls"].append(out["track"]["album_image"])
    # Hints the lazy panels use for exact matching -- resolved here rather than in the
    # template, where a lookup on a None (e.g. mb_artist.mbid) inside a filter argument
    # raises instead of falling back like a bare variable would.
    out["artist_mbid"] = mbd.get("artist_mbid") or (out["mb_artist"] or {}).get("mbid") or ""
    out["album_hint"] = mbd.get("album") or (out["track"] or {}).get("album") or ""
    return out


def artist_panel(rec, mbid=""):
    """The "about the artist" section: MusicBrainz for facts + external links, Last.fm
    for the bio, listener stats, similar artists and top tracks."""
    name = rec.parsed_artist
    out = {"mb": None, "lastfm": None, "top": [], "errors": []}
    if not name and not mbid:
        return out
    mb = MusicBrainzClient()
    try:
        found = mb.artist(mbid) if mbid else mb.search_artist(name)
        if found and not mbid and found.get("mbid"):
            found = mb.artist(found["mbid"])   # the search result carries no external links
        out["mb"] = found
    except ServiceError as exc:
        out["errors"].append(str(exc))
    lastfm = _client(S.LASTFM, LastfmClient)
    if lastfm and name:
        try:
            out["lastfm"] = lastfm.artist_info(name)
            out["top"] = lastfm.artist_top_tracks(name)
        except ServiceError as exc:
            if "not found" not in str(exc).lower():
                out["errors"].append(str(exc))
    return out


# -- "in your library" cards ----------------------------------------------------------


def service_card(rec, service, action=None, playlist_name=None, hints=None):
    """One library card: is this recommendation in <service>, where, and what can you do
    about it. `action` ("add" / "playlist") performs it first via the same push_one the
    row's dropdown uses, then re-checks. `hints` are ids the page already resolved
    (TMDB id, MusicBrainz artist id, album title) so a match can be exact, not fuzzy."""
    hints = hints or {}
    card = {"service": service, "label": LABELS.get(service, service), "configured": False, "present": None,
            "url": "", "album_url": "", "album_label": "", "title": "", "sub": "", "thumb": "", "actions": [], "error": ""}
    handler = {S.RADARR: _radarr_card, S.LIDARR: _lidarr_card, S.JELLYFIN: _jellyfin_card, S.NAVIDROME: _navidrome_card}.get(service)
    if handler is None:
        card["error"] = "unknown service"
        return card
    if action in ("add", "playlist"):
        push_one(rec, service, playlist_name=playlist_name)
        rec.refresh_from_db()
    card["state"] = (rec.integration_state or {}).get(service)
    try:
        handler(rec, card, hints)
    except ServiceError as exc:
        card["error"] = str(exc)
    return card


def _radarr_card(rec, card, hints):
    client = _client(S.RADARR, RadarrClient)
    if not client:
        return
    card["configured"] = True
    movie = None
    if hints.get("tmdb_id"):
        movie = client.existing(int(hints["tmdb_id"]))
    if not movie:
        found, _score = client.lookup(rec.parsed_title, rec.parsed_year)
        if found:
            movie = found if found.get("id") else client.existing(found["tmdbId"])
    if not movie:
        card.update(present=False, sub="Not in Radarr", actions=[{"key": "add", "label": "＋ Add to Radarr"}])
        return
    quality = (((movie.get("movieFile") or {}).get("quality") or {}).get("quality") or {}).get("name")
    if movie.get("hasFile"):
        sub = "Downloaded" + (f" · {quality}" if quality else "") + (f" · {_gb(movie.get('sizeOnDisk'))}" if movie.get("sizeOnDisk") else "")
    else:
        sub = "Monitored, not downloaded yet" if movie.get("monitored") else "In Radarr, unmonitored"
    card.update(present=True, title=f"{movie.get('title')} ({movie.get('year')})", sub=sub,
                url=f"{client.base_url}/movie/{movie.get('titleSlug') or movie.get('tmdbId')}")


def _lidarr_card(rec, card, hints):
    client = _client(S.LIDARR, LidarrClient)
    if not client:
        return
    card["configured"] = True
    artists = client.artists()
    artist = artists.get(hints.get("mbid") or "")
    if not artist and rec.parsed_artist:
        best, best_sim = None, 0.0
        for a in artists.values():
            sim = similarity(a.get("artistName", ""), rec.parsed_artist)
            if sim > best_sim:
                best, best_sim = a, sim
        if best_sim >= 0.85:
            artist = best
    if not artist:
        card.update(present=False, sub="Artist not in Lidarr", actions=[{"key": "add", "label": "＋ Add to Lidarr"}])
        return
    stats = artist.get("statistics") or {}
    parts = [f"{stats.get('trackFileCount', 0)} track{'s' if stats.get('trackFileCount', 0) != 1 else ''} on disk", f"{stats.get('albumCount', 0)} albums"]
    parts.append("monitored" if artist.get("monitored") else "unmonitored")
    if stats.get("sizeOnDisk"):
        parts.append(_gb(stats["sizeOnDisk"]))
    card.update(present=True, title=artist.get("artistName"), sub=" · ".join(parts),
                url=f"{client.base_url}/artist/{artist.get('foreignArtistId')}")
    target = hints.get("album") or ""
    if target:
        albums = client.get(f"{client.api}/album", artistId=artist["id"]) or []
        album, sim = None, 0.0
        for a in albums:
            s = similarity(a.get("title", ""), target)
            if s > sim:
                album, sim = a, s
        if album and sim >= 0.75:
            st = album.get("statistics") or {}
            have = st.get("trackFileCount", 0)
            card["album_url"] = f"{client.base_url}/album/{album.get('foreignAlbumId')}"
            card["album_label"] = f"{album.get('title')} ({(album.get('releaseDate') or '')[:4]}) · {have}/{st.get('trackCount', '?')} tracks"
            if not have:
                card["actions"] = [{"key": "add", "label": "＋ Add album + search"}]
        else:
            card["actions"] = [{"key": "add", "label": "＋ Add album + search"}]
    elif rec.parsed_title:
        card["actions"] = [{"key": "add", "label": "＋ Add album + search"}]


def _jellyfin_card(rec, card, hints):
    client = _client(S.JELLYFIN, JellyfinClient)
    if not client:
        return
    card["configured"] = True
    kind = rec.post.source.kind
    item, status, detail = client.resolve(rec, kind)
    if not item:
        card.update(present=False, sub=detail)
        return
    if kind == "movies":
        sub = str(item.get("ProductionYear") or "")
    else:
        sub = " · ".join(x for x in (item.get("Album"), str(item.get("ProductionYear") or "")) if x)
    card.update(present=True, title=detail, sub=sub or "In the library",
                url=f"{client.base_url}/web/#/details?id={item['Id']}",
                thumb=f"{client.base_url}/Items/{item['Id']}/Images/Primary?maxHeight=160&quality=80",
                actions=[{"key": "playlist", "label": "▶ Add to playlist", "prompt": "Jellyfin playlist name (an existing one is added to, not replaced)"}])


def _navidrome_card(rec, card, hints):
    client = _client(S.NAVIDROME, NavidromeClient)
    if not client:
        return
    card["configured"] = True
    song, status, detail = client.resolve(rec)
    if not song:
        card.update(present=False, sub=detail)
        return
    duration = song.get("duration") or 0
    bits = [song.get("album"), str(song.get("year") or ""), f"{(song.get('suffix') or '').upper()} {song.get('bitRate') or ''}kbps".strip(),
            f"{duration // 60}:{duration % 60:02d}" if duration else ""]
    card.update(present=True, title=detail, sub=" · ".join(b for b in bits if b),
                url=f"{client.base_url}/app/#/album/{song.get('albumId')}/show",
                thumb=client.cover_art_url(song.get("coverArt") or song.get("albumId"), 160),
                actions=[{"key": "playlist", "label": "▶ Add to playlist", "prompt": "Navidrome playlist name (an existing one is added to, not replaced)"}])


# -- Soulseek -------------------------------------------------------------------------


def slskd_search(rec):
    out = {"rows": [], "error": "", "configured": False}
    client = _client(S.SLSKD, SlskdClient)
    if not client:
        return out
    out["configured"] = True
    try:
        rows = client.ranked(rec.parsed_artist, rec.parsed_title, options=client.config.options, retries=1)
    except ServiceError as exc:
        out["error"] = str(exc)
        return out
    for r in rows:
        r["info"] = describe_file(r["file"])
    out["rows"] = rows
    return out


def slskd_download(rec, username, filename, size):
    client = _client(S.SLSKD, SlskdClient)
    if not client:
        return _record(rec, S.SLSKD, "error", "slskd is not configured")
    try:
        client.enqueue(username, {"filename": filename, "size": int(size or 0)})
    except (ServiceError, ValueError) as exc:
        return _record(rec, S.SLSKD, "error", str(exc))
    label = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return _record(rec, S.SLSKD, "queued", f"queued from {username}: {label} (picked by hand, {timezone.now():%H:%M})")
