"""
TheMovieDB (TMDB) v3 API -- read-only movie metadata. The one thing this app uses it
for: find a movie's official trailer and embed it directly on the vibe page, instead of
only ever linking out to a YouTube search (see vibes/views.py's rec_trailer).

    GET /3/search/movie?query=...&primary_release_year=...  -> candidate movies
    GET /3/movie/{id}/videos                                -> {"results": [{"key",
                                                                 "site", "type",
                                                                 "official", "size"}]}

Authenticates with a v4 "API Read Access Token" (a JWT, sent as a Bearer token) --
TMDB's current recommended approach for read-only access, replacing the older v3
`api_key` query-string param.
"""

from .base import BaseClient, best_match, similarity


class TmdbClient(BaseClient):
    api = "/3"

    def __init__(self, config):
        super().__init__(config)
        self.session.headers["Authorization"] = f"Bearer {config.api_key or ''}"

    def test(self):
        info = self.get(f"{self.api}/configuration")
        images = (info or {}).get("images") or {}
        return f"TheMovieDB: connected (image base {images.get('secure_base_url', '?')})"

    def refresh_choices(self):
        return None

    # -- lookup -------------------------------------------------------------------------

    def search_movie(self, title, year=None):
        params = {"query": title}
        if year:
            params["primary_release_year"] = year
        results = (self.get(f"{self.api}/search/movie", **params) or {}).get("results") or []
        match, _score = best_match(results, lambda m: similarity(m.get("title") or m.get("original_title") or "", title), threshold=0.5)
        return match

    def videos(self, movie_id):
        return (self.get(f"{self.api}/movie/{movie_id}/videos") or {}).get("results") or []

    def best_trailer(self, title, year=None):
        """(key, name) of the best YouTube trailer for a title, or None. Prefers an
        official trailer, then an official teaser, then whatever YouTube video is
        listed -- a movie with no listed videos, or no match at all, is a plain None
        rather than an error: the caller already has a YouTube-search fallback."""
        movie = self.search_movie(title, year)
        if not movie:
            return None
        candidates = [v for v in self.videos(movie["id"]) if v.get("site") == "YouTube" and v.get("key")]
        if not candidates:
            return None
        candidates.sort(key=lambda v: (v.get("official") is True, v.get("type") == "Trailer", v.get("type") == "Teaser", v.get("size") or 0), reverse=True)
        best = candidates[0]
        return {"key": best["key"], "name": best.get("name") or title}
