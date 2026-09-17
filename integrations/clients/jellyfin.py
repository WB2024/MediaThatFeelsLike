"""Jellyfin: find items in the library and build a playlist from them."""

from .base import BaseClient, ServiceError, best_match, similarity


class JellyfinClient(BaseClient):
    def __init__(self, config):
        super().__init__(config)
        self.session.headers["Authorization"] = (
            f'MediaBrowser Client="MediaThatFeelsLike", Device="server", DeviceId="mediathatfeelslike", '
            f'Version="0.1", Token="{config.api_key or ""}"'
        )

    def test(self):
        info = self.get("/System/Info")
        return f"Jellyfin {info.get('Version', '?')} ({info.get('ServerName', '')})".strip()

    # -- setup ------------------------------------------------------------------------

    def users(self):
        return [
            {"id": u["Id"], "name": u["Name"], "admin": bool((u.get("Policy") or {}).get("IsAdministrator"))}
            for u in self.get("/Users") or []
            if not (u.get("Policy") or {}).get("IsDisabled")
        ]

    def refresh_choices(self):
        opts = self.config.options
        opts["users"] = self.users()
        if not opts.get("user_id") and opts["users"]:
            admins = [u for u in opts["users"] if u["admin"]]
            opts["user_id"] = (admins or opts["users"])[0]["id"]
        self.config.save(update_fields=["options"])

    @property
    def user_id(self):
        uid = self.config.options.get("user_id")
        if not uid:
            raise ServiceError("Jellyfin: choose a user on the Settings page (press Test to load the list)")
        return uid

    # -- lookups ----------------------------------------------------------------------

    def _items(self, **params):
        params.setdefault("Recursive", "true")
        params.setdefault("Limit", 25)
        params["userId"] = self.user_id
        return (self.get("/Items", **params) or {}).get("Items", [])

    def find_movie(self, title, year=None):
        items = self._items(searchTerm=title, IncludeItemTypes="Movie", Fields="ProductionYear,Path")

        def score(item):
            s = similarity(item.get("Name", ""), title)
            if year and item.get("ProductionYear"):
                s += 0.15 if abs(int(item["ProductionYear"]) - int(year)) <= 1 else -0.25
            return s

        return best_match(items, score, threshold=0.6)

    def find_song(self, artist, title, _swapped=False):
        items = self._items(searchTerm=title, IncludeItemTypes="Audio", Fields="Artists,AlbumArtist,Album,Path")

        def score(item):
            t = similarity(item.get("Name", ""), title)
            if not artist:
                return t
            names = list(item.get("Artists") or []) + [item.get("AlbumArtist") or ""]
            a = max((similarity(n, artist) for n in names if n), default=0.0)
            return 0.55 * t + 0.45 * a if a >= 0.6 else 0.0

        match, value = best_match(items, score, threshold=0.65)
        if match is None and artist and title and not _swapped:
            match, value = self.find_song(title, artist, _swapped=True)
        return match, value

    # -- playlists --------------------------------------------------------------------

    def create_playlist(self, name, item_ids, media_type):
        payload = {"Name": name, "Ids": item_ids, "UserId": self.user_id, "MediaType": media_type}
        result = self.post("/Playlists", json=payload)
        return (result or {}).get("Id")

    def find_playlist(self, name):
        """Exact (case-insensitive) name match, or None. `searchTerm` is a substring
        match on Jellyfin's side, so the exact check still has to happen here."""
        items = self._items(searchTerm=name, IncludeItemTypes="Playlist")
        return next((i for i in items if i.get("Name", "").casefold() == name.casefold()), None)

    def add_to_playlist(self, playlist_id, item_ids):
        self._request("POST", f"/Playlists/{playlist_id}/Items", params={"ids": ",".join(item_ids), "userId": self.user_id})

    def add_or_create_playlist(self, name, item_ids, media_type):
        """Add to the existing playlist of this name, or create it. Returns (playlist_id, created)."""
        existing = self.find_playlist(name)
        if existing:
            self.add_to_playlist(existing["Id"], item_ids)
            return existing["Id"], False
        return self.create_playlist(name, item_ids, media_type), True

    def resolve(self, rec, kind):
        """(item, status, detail) for one recommendation."""
        if kind == "movies":
            item, score = self.find_movie(rec.parsed_title, rec.parsed_year)
            label = lambda i: f"{i.get('Name')} ({i.get('ProductionYear', '')})"  # noqa: E731
        else:
            if rec.parsed_artist and not rec.parsed_title:
                return None, "skipped", "artist-only recommendation; playlists need a track"
            item, score = self.find_song(rec.parsed_artist, rec.parsed_title)
            label = lambda i: f"{', '.join(i.get('Artists') or [])} - {i.get('Name')}"  # noqa: E731
        if not item:
            return None, "not_found", f"not in the Jellyfin library (best match {score:.2f})"
        return item, "matched", label(item)
