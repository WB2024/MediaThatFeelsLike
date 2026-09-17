"""
Lidarr v1 API. Lidarr manages artists and albums, not individual tracks, so a
"Artist - Song" recommendation is pushed as: find the album/single that carries the
song (album lookup by "Artist Title"), add it monitored and search; if no album can be
identified, add the artist unmonitored so it can be picked through in Lidarr's UI.
"""

import time

from .base import BaseClient, ServiceError, best_match, similarity


class LidarrClient(BaseClient):
    api = "/api/v1"

    def __init__(self, config):
        super().__init__(config)
        self.session.headers["X-Api-Key"] = config.api_key or ""
        self._artists = None

    def test(self):
        status = self.get(f"{self.api}/system/status")
        return f"Lidarr {status.get('version', '?')} on {status.get('osName', '')}".strip()

    # -- setup choices ----------------------------------------------------------------

    def refresh_choices(self):
        opts = self.config.options
        opts["root_folders"] = [{"id": r["id"], "path": r["path"]} for r in self.get(f"{self.api}/rootfolder") or []]
        opts["quality_profiles"] = [{"id": p["id"], "name": p["name"]} for p in self.get(f"{self.api}/qualityprofile") or []]
        opts["metadata_profiles"] = [{"id": p["id"], "name": p["name"]} for p in self.get(f"{self.api}/metadataprofile") or []]
        for key, choices, field in (("root_folder", "root_folders", "path"), ("quality_profile_id", "quality_profiles", "id"), ("metadata_profile_id", "metadata_profiles", "id")):
            if not opts.get(key) and opts[choices]:
                opts[key] = opts[choices][0][field]
        self.config.save(update_fields=["options"])

    def _require_setup(self):
        opts = self.config.options
        if not (opts.get("root_folder") and opts.get("quality_profile_id") and opts.get("metadata_profile_id")):
            raise ServiceError("Lidarr: pick a root folder, quality profile and metadata profile on the Settings page first (press Test to load them)")
        return opts

    # -- lookups ----------------------------------------------------------------------

    def artists(self):
        if self._artists is None:
            self._artists = {a["foreignArtistId"]: a for a in self.get(f"{self.api}/artist") or []}
        return self._artists

    def album_lookup(self, term):
        return self.get(f"{self.api}/album/lookup", term=term) or []

    def artist_lookup(self, term):
        return self.get(f"{self.api}/artist/lookup", term=term) or []

    def find_album(self, artist, title, _swapped=False):
        results = self.album_lookup(f"{artist} {title}".strip())

        def score(album):
            a_name = (album.get("artist") or {}).get("artistName", "")
            a_sim = similarity(a_name, artist) if artist else 0.7
            t_sim = similarity(album.get("title", ""), title)
            # Both halves have to look right: an artist's random EP is not "the album
            # with this song on it".
            if a_sim < 0.7 or t_sim < 0.45:
                return 0.0
            bonus = 0.05 if album.get("albumType") in ("Single", "EP") else 0.0
            return 0.5 * a_sim + 0.5 * t_sim + bonus

        match, value = best_match(results, score, threshold=0.6)
        if match is None and artist and title and not _swapped:
            # Commenters write "Title - Artist" about as often as the other way round.
            match, value = self.find_album(title, artist, _swapped=True)
        return match, value

    def find_artist(self, artist):
        results = self.artist_lookup(artist)
        return best_match(results, lambda a: similarity(a.get("artistName", ""), artist), threshold=0.8)

    # -- adds -------------------------------------------------------------------------

    def _artist_payload(self, artist, monitored, monitor_option, search):
        opts = self._require_setup()
        payload = dict(artist)
        payload.update(
            {
                "qualityProfileId": int(opts["quality_profile_id"]),
                "metadataProfileId": int(opts["metadata_profile_id"]),
                "rootFolderPath": opts["root_folder"],
                "monitored": monitored,
                "monitorNewItems": "none",
                "addOptions": {"monitor": monitor_option, "searchForMissingAlbums": bool(search)},
            }
        )
        return payload

    def add_album(self, album, search=True):
        artist = album.get("artist") or {}
        existing = self.artists().get(artist.get("foreignArtistId"))
        payload = dict(album)
        payload["monitored"] = True
        payload["anyReleaseOk"] = True
        payload["addOptions"] = {"searchForNewAlbum": bool(search)}
        if existing:
            payload["artist"] = existing
            payload["artistId"] = existing["id"]
        else:
            payload["artist"] = self._artist_payload(artist, monitored=True, monitor_option="none", search=False)
        created = self.post(f"{self.api}/album", json=payload)
        artist_id = (created or {}).get("artistId")
        if artist_id and not existing:
            self._ensure_artist_monitored(artist_id)
        return created

    def _ensure_artist_monitored(self, artist_id, wait=30):
        """Lidarr's album-add path creates the artist unmonitored and immediately runs
        RefreshArtist, which overwrites any change made while it runs. An unmonitored
        artist's albums are skipped by RSS sync, so: wait for the refresh to finish,
        then re-monitor through the editor endpoint."""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            active = [c for c in self.get(f"{self.api}/command") or [] if c.get("name") == "RefreshArtist" and c.get("status") in ("queued", "started")]
            if not active:
                break
            time.sleep(1.5)
        self._request("PUT", f"{self.api}/artist/editor", json={"artistIds": [artist_id], "monitored": True})
        row = self.get(f"{self.api}/artist/{artist_id}")
        if row:
            self.artists()[row["foreignArtistId"]] = row

    # -- push -------------------------------------------------------------------------

    def push(self, rec):
        opts = self.config.options
        search = opts.get("search_on_add", True)
        artist_name, title = rec.parsed_artist, rec.parsed_title

        if artist_name and not title:
            artist, score = self.find_artist(artist_name)
            if not artist:
                return "not_found", f"no confident artist match (best {score:.2f})"
            if artist["foreignArtistId"] in self.artists():
                return "exists", f"artist already in Lidarr: {artist['artistName']}"
            monitor = opts.get("artist_monitor", "none")
            self.add_artist(artist, monitor_option=monitor, search=search and monitor != "none")
            return "added", f"artist added ({'monitoring ' + monitor if monitor != 'none' else 'unmonitored -- pick albums in Lidarr'}): {artist['artistName']}"

        album, score = self.find_album(artist_name, title)
        if album:
            label = f"{(album.get('artist') or {}).get('artistName', '')} - {album.get('title')} [{album.get('albumType', '')}]"
            try:
                self.add_album(album, search=search)
            except ServiceError as exc:
                if "already" in str(exc).lower():
                    return "exists", f"already in Lidarr: {label}"
                raise
            return "added", f"album added + searching: {label}"

        if not artist_name:
            return "not_found", f"no album matched '{title}' and no artist given"
        artist, a_score = self.find_artist(artist_name)
        if not artist:
            return "not_found", f"no album match for '{title}' (best {score:.2f}) and no confident artist match (best {a_score:.2f})"
        if artist["foreignArtistId"] in self.artists():
            return "artist_only", f"artist already in Lidarr but no album identified for '{title}' -- monitor it there: {artist['artistName']}"
        self.add_artist(artist, monitor_option="none", search=False)
        return "artist_only", f"artist added unmonitored (album for '{title}' not identified): {artist['artistName']}"
