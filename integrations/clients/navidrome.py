"""Navidrome via the Subsonic API (token auth: md5(password + salt))."""

import hashlib
import secrets
from urllib.parse import urlencode

from .base import BaseClient, ServiceError, best_match, similarity


class NavidromeClient(BaseClient):
    def _auth(self):
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.config.password or "").encode("utf-8") + salt.encode("ascii")).hexdigest()
        return {"u": self.config.username, "t": token, "s": salt, "v": "1.16.1", "c": "MediaThatFeelsLike", "f": "json"}

    def call(self, endpoint, **params):
        # Subsonic accepts repeated params (songId=a&songId=b) as a list of tuples.
        items = list(self._auth().items())
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                items.extend((key, v) for v in value)
            elif value is not None:
                items.append((key, value))
        body = self._request("GET", f"/rest/{endpoint}", params=items) or {}
        resp = body.get("subsonic-response", {})
        if resp.get("status") != "ok":
            err = resp.get("error", {})
            raise ServiceError(f"Navidrome: {err.get('message', 'unknown error')} (code {err.get('code', '?')})")
        return resp

    def test(self):
        resp = self.call("ping")
        return f"Navidrome OK (Subsonic API {resp.get('version', '?')}, {resp.get('type', '')} {resp.get('serverVersion', '')})".strip()

    def refresh_choices(self):
        return None

    def cover_art_url(self, art_id, size=200):
        """A browser-loadable getCoverArt URL. Carries a fresh salted token, not the
        password -- the same thing every Subsonic client puts in every request."""
        if not art_id:
            return ""
        return f"{self.base_url}/rest/getCoverArt.view?" + urlencode({**self._auth(), "id": art_id, "size": size})

    def find_song(self, artist, title, _swapped=False):
        query = f"{artist} {title}".strip() if artist else title
        songs = (self.call("search3", query=query, songCount=30, albumCount=0, artistCount=0).get("searchResult3") or {}).get("song") or []
        if not songs and artist:
            songs = (self.call("search3", query=title, songCount=30, albumCount=0, artistCount=0).get("searchResult3") or {}).get("song") or []

        def score(song):
            t = similarity(song.get("title", ""), title)
            if not artist:
                return t
            a = max(similarity(song.get("artist", ""), artist), similarity(song.get("albumArtist", ""), artist))
            return 0.55 * t + 0.45 * a if a >= 0.6 else 0.0

        match, value = best_match(songs, score, threshold=0.65)
        if match is None and artist and title and not _swapped:
            match, value = self.find_song(title, artist, _swapped=True)
        return match, value

    def create_playlist(self, name, song_ids):
        resp = self.call("createPlaylist", name=name, songId=list(song_ids))
        return (resp.get("playlist") or {}).get("id")

    def find_playlist(self, name):
        """Exact (case-insensitive) name match, or None."""
        playlists = self.call("getPlaylists").get("playlists", {}).get("playlist") or []
        return next((p for p in playlists if p.get("name", "").casefold() == name.casefold()), None)

    def add_to_playlist(self, playlist_id, song_ids):
        self.call("updatePlaylist", playlistId=playlist_id, songIdToAdd=list(song_ids))

    def add_or_create_playlist(self, name, song_ids):
        """Add to the existing playlist of this name, or create it. Returns (playlist_id, created)."""
        existing = self.find_playlist(name)
        if existing:
            self.add_to_playlist(existing["id"], song_ids)
            return existing["id"], False
        return self.create_playlist(name, song_ids), True

    def resolve(self, rec, kind="music"):
        if rec.parsed_artist and not rec.parsed_title:
            return None, "skipped", "artist-only recommendation; playlists need a track"
        song, score = self.find_song(rec.parsed_artist, rec.parsed_title)
        if not song:
            return None, "not_found", f"not in the Navidrome library (best match {score:.2f})"
        return song, "matched", f"{song.get('artist')} - {song.get('title')}"
