"""Radarr v3 API: look a film up, add it (monitored) and kick off a search."""

from .base import BaseClient, ServiceError, best_match, similarity


class RadarrClient(BaseClient):
    api = "/api/v3"

    def __init__(self, config):
        super().__init__(config)
        self.session.headers["X-Api-Key"] = config.api_key or ""

    def test(self):
        status = self.get(f"{self.api}/system/status")
        return f"Radarr {status.get('version', '?')} on {status.get('osName', '')}".strip()

    # -- setup choices ----------------------------------------------------------------

    def root_folders(self):
        return [{"id": r["id"], "path": r["path"], "free": r.get("freeSpace")} for r in self.get(f"{self.api}/rootfolder") or []]

    def quality_profiles(self):
        return [{"id": p["id"], "name": p["name"]} for p in self.get(f"{self.api}/qualityprofile") or []]

    def refresh_choices(self):
        """Store selectable knobs on the config so the Settings page can render them."""
        opts = self.config.options
        opts["root_folders"] = self.root_folders()
        opts["quality_profiles"] = self.quality_profiles()
        if not opts.get("root_folder") and opts["root_folders"]:
            opts["root_folder"] = opts["root_folders"][0]["path"]
        if not opts.get("quality_profile_id") and opts["quality_profiles"]:
            opts["quality_profile_id"] = opts["quality_profiles"][0]["id"]
        self.config.save(update_fields=["options"])

    # -- the actual work --------------------------------------------------------------

    def lookup(self, title, year=None):
        term = f"{title} {year}" if year else title
        results = self.get(f"{self.api}/movie/lookup", term=term) or []
        if not results and year:
            results = self.get(f"{self.api}/movie/lookup", term=title) or []

        def score(m):
            s = similarity(m.get("title", ""), title)
            if year and m.get("year"):
                if abs(int(m["year"]) - int(year)) <= 1:
                    s += 0.15
                else:
                    s -= 0.25
            return s

        match, score_value = best_match(results, score, threshold=0.6)
        return match, score_value

    def existing(self, tmdb_id):
        found = self.get(f"{self.api}/movie", tmdbId=tmdb_id) or []
        return found[0] if found else None

    def add(self, movie, search=True):
        opts = self.config.options
        root = opts.get("root_folder")
        profile = opts.get("quality_profile_id")
        if not root or not profile:
            raise ServiceError("Radarr: pick a root folder and quality profile on the Settings page first (press Test to load them)")
        payload = {
            "title": movie["title"],
            "tmdbId": movie["tmdbId"],
            "year": movie.get("year"),
            "titleSlug": movie.get("titleSlug"),
            "images": movie.get("images", []),
            "qualityProfileId": int(profile),
            "rootFolderPath": root,
            "monitored": True,
            "minimumAvailability": opts.get("minimum_availability", "released"),
            "addOptions": {"searchForMovie": bool(search), "monitor": "movieOnly"},
        }
        return self.post(f"{self.api}/movie", json=payload)

    def push(self, rec):
        """Add one recommendation. Returns (status, detail)."""
        movie, score = self.lookup(rec.parsed_title, rec.parsed_year)
        if not movie:
            return "not_found", f"no confident match in Radarr's lookup (best {score:.2f})"
        label = f"{movie['title']} ({movie.get('year')})"
        if movie.get("id") or self.existing(movie["tmdbId"]):
            return "exists", f"already in Radarr: {label}"
        self.add(movie, search=self.config.options.get("search_on_add", True))
        return "added", f"added + searching: {label}"
