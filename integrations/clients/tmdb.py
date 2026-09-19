"""
TheMovieDB (TMDB) v3 API -- read-only movie metadata. Two consumers:

  * the recommendation row's "watch trailer here" toggle (`best_trailer`), and
  * the recommendation's own detail page (`movie_for`), which pulls everything TMDB
    knows about a film in a single call via `append_to_response`.

    GET /3/search/movie?query=...&primary_release_year=...   -> candidate movies
    GET /3/movie/{id}?append_to_response=videos,credits,...   -> the lot
    GET /3/movie/{id}/videos                                  -> {"results": [{"key",
                                                                   "site", "type",
                                                                   "official", "size"}]}

Authenticates with a v4 "API Read Access Token" (a JWT, sent as a Bearer token) --
TMDB's current recommended approach for read-only access, replacing the older v3
`api_key` query-string param. Image paths are relative; `image_url` prefixes TMDB's
fixed CDN base (documented as stable, so no need to hit /configuration for it).
"""

from .base import BaseClient, best_match, similarity

IMAGE_BASE = "https://image.tmdb.org/t/p/"
DETAIL_APPEND = "videos,credits,images,release_dates,keywords,similar,external_ids"
PREFERRED_CERT_REGIONS = ("GB", "US")


def image_url(path, size="w500"):
    return f"{IMAGE_BASE}{size}{path}" if path else ""


def pick_trailer(videos):
    """Best embeddable video from a TMDB videos list: YouTube only, official > not,
    Trailer > Teaser > anything else, then biggest. None if nothing qualifies."""
    candidates = [v for v in videos or [] if v.get("site") == "YouTube" and v.get("key")]
    if not candidates:
        return None
    candidates.sort(
        key=lambda v: (v.get("official") is True, v.get("type") == "Trailer", v.get("type") == "Teaser", v.get("size") or 0),
        reverse=True,
    )
    return candidates[0]


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
        if not results and year:
            results = (self.get(f"{self.api}/search/movie", query=title) or {}).get("results") or []
        match, _score = best_match(results, lambda m: similarity(m.get("title") or m.get("original_title") or "", title), threshold=0.5)
        return match

    def videos(self, movie_id):
        return (self.get(f"{self.api}/movie/{movie_id}/videos") or {}).get("results") or []

    def best_trailer(self, title, year=None):
        """{key, name} of the best YouTube trailer for a title, or None. A movie with no
        listed videos, or no match at all, is a plain None rather than an error: the
        caller already has a YouTube-search fallback."""
        movie = self.search_movie(title, year)
        if not movie:
            return None
        best = pick_trailer(self.videos(movie["id"]))
        return {"key": best["key"], "name": best.get("name") or title} if best else None

    # -- detail page --------------------------------------------------------------------

    def movie_details(self, movie_id):
        return self.get(f"{self.api}/movie/{movie_id}", append_to_response=DETAIL_APPEND) or {}

    def movie_for(self, title, year=None):
        """Everything the detail page shows for a title, shaped for the template, or
        None when TMDB has no confident match."""
        movie = self.search_movie(title, year)
        if not movie:
            return None
        return shape_movie(self.movie_details(movie["id"]))


def _certification(release_dates):
    """The film's age rating for a preferred region (GB, then US, then whatever's first)."""
    by_region = {}
    for entry in (release_dates or {}).get("results") or []:
        certs = [r.get("certification") for r in entry.get("release_dates") or [] if r.get("certification")]
        if certs:
            by_region[entry.get("iso_3166_1")] = certs[0]
    for region in PREFERRED_CERT_REGIONS:
        if by_region.get(region):
            return f"{by_region[region]} ({region})"
    return next(iter(by_region.values()), "")


def shape_movie(m):
    """Flatten TMDB's detail payload into what the template needs. Everything optional --
    a sparse TMDB entry (an obscure short, say) still renders as a sane page."""
    credits = m.get("credits") or {}
    cast = [
        {"name": c.get("name"), "character": c.get("character"), "photo": image_url(c.get("profile_path"), "w185")}
        for c in (credits.get("cast") or [])[:10]
    ]
    crew_by_job = {}
    for c in credits.get("crew") or []:
        crew_by_job.setdefault(c.get("job"), []).append(c.get("name"))
    crew = [(job, ", ".join(dict.fromkeys(crew_by_job[job]))) for job in ("Director", "Screenplay", "Writer", "Original Music Composer", "Director of Photography") if crew_by_job.get(job)]
    videos = (m.get("videos") or {}).get("results") or []
    trailer = pick_trailer(videos)
    other_videos = [
        {"key": v["key"], "name": v.get("name"), "type": v.get("type")}
        for v in sorted(videos, key=lambda v: (v.get("official") is not True, v.get("type") != "Trailer"))
        if v.get("site") == "YouTube" and v.get("key") and (not trailer or v["key"] != trailer["key"])
    ][:8]
    similar = [
        {"title": s.get("title"), "year": (s.get("release_date") or "")[:4], "poster": image_url(s.get("poster_path"), "w185"),
         "url": f"https://www.themoviedb.org/movie/{s.get('id')}", "rating": s.get("vote_average")}
        for s in ((m.get("similar") or {}).get("results") or [])[:10] if s.get("poster_path")
    ]
    external = m.get("external_ids") or {}
    imdb_id = m.get("imdb_id") or external.get("imdb_id")
    return {
        "tmdb_id": m.get("id"),
        "title": m.get("title") or m.get("original_title"),
        "original_title": m.get("original_title") if m.get("original_title") != m.get("title") else "",
        "tagline": m.get("tagline") or "",
        "overview": m.get("overview") or "",
        "year": (m.get("release_date") or "")[:4],
        "release_date": m.get("release_date") or "",
        "runtime": m.get("runtime") or 0,
        "genres": [g.get("name") for g in m.get("genres") or []],
        "rating": round(m.get("vote_average") or 0, 1),
        "votes": m.get("vote_count") or 0,
        "certification": _certification(m.get("release_dates")),
        "status": m.get("status") or "",
        "budget": m.get("budget") or 0,
        "revenue": m.get("revenue") or 0,
        "companies": [c.get("name") for c in (m.get("production_companies") or [])[:4]],
        "countries": [c.get("name") for c in m.get("production_countries") or []],
        "languages": [lang.get("english_name") or lang.get("name") for lang in m.get("spoken_languages") or []],
        "keywords": [k.get("name") for k in ((m.get("keywords") or {}).get("keywords") or [])[:12]],
        "poster": image_url(m.get("poster_path"), "w500"),
        "backdrop": image_url(m.get("backdrop_path"), "w1280"),
        "trailer": {"key": trailer["key"], "name": trailer.get("name") or "Trailer"} if trailer else None,
        "other_videos": other_videos,
        "cast": cast,
        "crew": crew,
        "similar": similar,
        "homepage": m.get("homepage") or "",
        "imdb_url": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "",
        "tmdb_url": f"https://www.themoviedb.org/movie/{m.get('id')}" if m.get("id") else "",
    }
