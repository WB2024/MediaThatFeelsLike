"""Integration clients with the HTTP layer stubbed out."""

from types import SimpleNamespace

import pytest

from integrations.clients.base import ServiceError, similarity
from integrations.clients.jellyfin import JellyfinClient
from integrations.clients.lidarr import LidarrClient
from integrations.clients.navidrome import NavidromeClient
from integrations.clients.radarr import RadarrClient
from integrations.clients.slskd import SlskdClient
from integrations.clients.tmdb import TmdbClient
from integrations.models import ServiceConfig


def cfg(service, **options):
    c = ServiceConfig(service=service, url="http://svc.test", api_key="k", username="u", password="p", options=options)
    c.save = lambda *a, **k: None  # tests never touch the DB
    return c


def rec(title, artist="", year=None):
    return SimpleNamespace(parsed_title=title, parsed_artist=artist, parsed_year=year)


def stub(client, routes):
    """routes: {(METHOD, path): response or callable(params/json) -> response}"""
    calls = []

    def _request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if (method, path) not in routes:
            raise AssertionError(f"unexpected {method} {path}")
        handler = routes[(method, path)]
        return handler(kwargs) if callable(handler) else handler

    client._request = _request
    return calls


def test_similarity_normalises():
    assert similarity("The Cure", "cure") == 1.0
    assert similarity("Fade Into You (Remastered)", "fade into you") == 1.0
    assert similarity("Drive", "Driven") > 0.8
    assert similarity("Heat", "Heathers") < 0.85


def test_radarr_push_adds_with_year_preference():
    c = RadarrClient(cfg("radarr", root_folder="/movies", quality_profile_id=3))
    lookup = [{"title": "Heat", "year": 1995, "tmdbId": 949, "titleSlug": "heat-1995"}, {"title": "Heat", "year": 1972, "tmdbId": 111, "titleSlug": "heat-1972"}]
    calls = stub(c, {("GET", "/api/v3/movie/lookup"): lookup, ("GET", "/api/v3/movie"): [], ("POST", "/api/v3/movie"): {"id": 1}})
    status, detail, url = c.push(rec("Heat", year=1972))
    assert status == "added" and "1972" in detail
    assert url == "http://svc.test/movie/heat-1972"  # links the chip straight to the new movie's Radarr page
    payload = [k["json"] for m, p, k in calls if m == "POST"][0]
    assert payload["tmdbId"] == 111 and payload["qualityProfileId"] == 3 and payload["rootFolderPath"] == "/movies"
    assert payload["addOptions"]["searchForMovie"] is True


def test_radarr_reports_existing_and_not_found():
    c = RadarrClient(cfg("radarr", root_folder="/m", quality_profile_id=1))
    stub(c, {("GET", "/api/v3/movie/lookup"): [{"title": "Drive", "year": 2011, "tmdbId": 64690, "id": 7, "titleSlug": "drive-2011"}]})
    status, detail, url = c.push(rec("Drive", year=2011))
    assert status == "exists" and url == "http://svc.test/movie/drive-2011"
    stub(c, {("GET", "/api/v3/movie/lookup"): [{"title": "Something Else Entirely", "year": 1999, "tmdbId": 1}]})
    status, detail, url = c.push(rec("Drive"))
    assert status == "not_found" and url is None


def test_radarr_requires_setup():
    c = RadarrClient(cfg("radarr"))
    stub(c, {("GET", "/api/v3/movie/lookup"): [{"title": "Drive", "year": 2011, "tmdbId": 1}], ("GET", "/api/v3/movie"): []})
    with pytest.raises(ServiceError):
        c.push(rec("Drive"))


# -- TheMovieDB -----------------------------------------------------------------------


def test_tmdb_best_trailer_prefers_official_trailer_over_teaser():
    c = TmdbClient(cfg("tmdb"))
    stub(c, {
        ("GET", "/3/search/movie"): {"results": [{"id": 42, "title": "Drive"}]},
        ("GET", "/3/movie/42/videos"): {"results": [
            {"key": "teaser1", "site": "YouTube", "type": "Teaser", "official": True, "size": 1080},
            {"key": "vimeo1", "site": "Vimeo", "type": "Trailer", "official": True, "size": 1080},
            {"key": "trailer1", "site": "YouTube", "type": "Trailer", "official": True, "size": 1080, "name": "Official Trailer"},
            {"key": "fan1", "site": "YouTube", "type": "Trailer", "official": False, "size": 1080},
        ]},
    })
    trailer = c.best_trailer("Drive", 2011)
    assert trailer == {"key": "trailer1", "name": "Official Trailer"}


def test_tmdb_best_trailer_none_when_no_match_or_no_youtube_video():
    c = TmdbClient(cfg("tmdb"))
    stub(c, {("GET", "/3/search/movie"): {"results": []}})
    assert c.best_trailer("Some Extremely Obscure Rarity") is None

    stub(c, {
        ("GET", "/3/search/movie"): {"results": [{"id": 7, "title": "Drive"}]},
        ("GET", "/3/movie/7/videos"): {"results": [{"key": "x", "site": "Vimeo", "type": "Trailer"}]},
    })
    assert c.best_trailer("Drive") is None


TMDB_DETAIL = {
    "id": 587, "title": "Big Fish", "original_title": "Big Fish", "tagline": "An adventure as big as life itself.",
    "overview": "Edward Bloom...", "release_date": "2003-12-10", "runtime": 125, "vote_average": 7.71, "vote_count": 7939,
    "genres": [{"name": "Adventure"}, {"name": "Fantasy"}], "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
    "budget": 70000000, "revenue": 123200000, "status": "Released", "homepage": "", "imdb_id": "tt0319061",
    "production_companies": [{"name": "Columbia Pictures"}], "production_countries": [{"name": "United States of America"}],
    "spoken_languages": [{"english_name": "English"}],
    "videos": {"results": [
        {"key": "clip1", "site": "YouTube", "type": "Clip", "official": True, "name": "A clip"},
        {"key": "tr1", "site": "YouTube", "type": "Trailer", "official": True, "size": 1080, "name": "Official Trailer"},
    ]},
    "credits": {
        "cast": [{"name": "Ewan McGregor", "character": "Ed Bloom (young)", "profile_path": "/e.jpg"}],
        "crew": [{"name": "Tim Burton", "job": "Director"}, {"name": "John August", "job": "Screenplay"}, {"name": "Someone", "job": "Gaffer"}],
    },
    "release_dates": {"results": [
        {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
        {"iso_3166_1": "GB", "release_dates": [{"certification": ""}, {"certification": "PG"}]},
    ]},
    "keywords": {"keywords": [{"name": "witch"}, {"name": "circus"}]},
    "similar": {"results": [{"id": 1, "title": "Edward Scissorhands", "release_date": "1990-12-07", "poster_path": "/s.jpg", "vote_average": 7.7}, {"id": 2, "title": "No poster", "poster_path": None}]},
    "external_ids": {"imdb_id": "tt0319061"},
}


def test_tmdb_shape_movie_flattens_the_detail_payload():
    from integrations.clients.tmdb import shape_movie

    m = shape_movie(TMDB_DETAIL)
    assert m["title"] == "Big Fish" and m["year"] == "2003" and m["original_title"] == ""  # same as title -> not repeated
    assert m["rating"] == 7.7 and m["votes"] == 7939 and m["runtime"] == 125
    assert m["certification"] == "PG (GB)"  # GB preferred over US; blank certification entries skipped
    assert m["poster"] == "https://image.tmdb.org/t/p/w500/p.jpg" and m["backdrop"].endswith("w1280/b.jpg")
    assert m["trailer"] == {"key": "tr1", "name": "Official Trailer"}
    assert m["other_videos"] == [{"key": "clip1", "name": "A clip", "type": "Clip"}]  # the trailer itself isn't repeated
    assert m["cast"][0]["photo"].endswith("w185/e.jpg") and m["cast"][0]["character"] == "Ed Bloom (young)"
    assert m["crew"] == [("Director", "Tim Burton"), ("Screenplay", "John August")]  # gaffer isn't a headline job
    assert m["similar"] == [{"title": "Edward Scissorhands", "year": "1990", "poster": "https://image.tmdb.org/t/p/w185/s.jpg", "url": "https://www.themoviedb.org/movie/1", "rating": 7.7}]
    assert m["imdb_url"] == "https://www.imdb.com/title/tt0319061/" and m["tmdb_url"] == "https://www.themoviedb.org/movie/587"
    assert m["keywords"] == ["witch", "circus"] and m["companies"] == ["Columbia Pictures"]


def test_tmdb_movie_for_searches_then_fetches_details_in_one_call():
    c = TmdbClient(cfg("tmdb"))
    calls = stub(c, {("GET", "/3/search/movie"): {"results": [{"id": 587, "title": "Big Fish"}]}, ("GET", "/3/movie/587"): TMDB_DETAIL})
    assert c.movie_for("Big Fish", 2003)["title"] == "Big Fish"
    detail_call = [k for m, p, k in calls if p == "/3/movie/587"][0]
    assert "videos" in detail_call["params"]["append_to_response"] and "credits" in detail_call["params"]["append_to_response"]
    assert calls[0][2]["params"] == {"query": "Big Fish", "primary_release_year": 2003}


# -- MusicBrainz ----------------------------------------------------------------------


def _mb_recording(rid, title, artist, releases):
    return {
        "id": rid, "score": 100, "title": title, "length": 204000,
        "artist-credit": [{"name": artist, "artist": {"id": "artist-mbid", "name": artist}}],
        "releases": releases,
    }


def _mb_release(title, status="Official", primary="Album", secondary=None, date="2003-03-25", rg="rg-1"):
    return {"id": f"rel-{title}-{date}", "title": title, "status": status, "date": date, "country": "US",
            "release-group": {"id": rg, "primary-type": primary, "secondary-types": secondary or []}}


def test_musicbrainz_prefers_the_canonical_recording_and_its_plain_album(monkeypatch):
    from integrations.clients.musicbrainz import MusicBrainzClient

    c = MusicBrainzClient()
    payload = {"recordings": [
        # A piano-instrumental bootleg version matches the title just as well -- must lose.
        _mb_recording("rec-bootleg", "Easier to Run", "Linkin Park", [_mb_release("Piano Instrumentals", status="Bootleg", secondary=["Remix"], rg="rg-b")]),
        _mb_recording("rec-canon", "Easier to Run", "Linkin Park", [
            _mb_release("Meteora (20th anniversary edition)", secondary=["Compilation"], date="2023-04-07", rg="rg-20"),
            _mb_release("Numb", primary="Single", date="2003-09-08", rg="rg-single"),
            _mb_release("Meteora", date="2006-09-26", rg="rg-meteora"),
            _mb_release("Meteora", date="2003-03-25", rg="rg-meteora"),
        ]),
        _mb_recording("rec-cover", "Easier to Run", "Some Cover Band", [_mb_release("Covers")]),
    ]}
    monkeypatch.setattr(c, "get", lambda path, **params: payload)
    r = c.search_recording("Linkin Park", "Easier to Run")
    assert r["mbid"] == "rec-canon" and r["artist_mbid"] == "artist-mbid"
    assert r["album"] == "Meteora" and r["album_year"] == "2003" and r["album_type"] == "Album"  # earliest official plain album
    assert r["release_group_mbid"] == "rg-meteora" and r["length"] == "3:24"
    assert [x["title"] for x in r["releases"]] == ["Meteora", "Numb", "Meteora (20th anniversary edition)"]  # one row per release group, by date


def test_musicbrainz_search_recording_returns_none_when_nothing_matches_both_halves(monkeypatch):
    from integrations.clients.musicbrainz import MusicBrainzClient

    c = MusicBrainzClient()
    monkeypatch.setattr(c, "get", lambda path, **params: {"recordings": [_mb_recording("x", "Easier to Run", "Some Cover Band", [])]})
    assert c.search_recording("Linkin Park", "Easier to Run") is None


def test_musicbrainz_get_backs_off_through_503s_then_gives_up(monkeypatch):
    from integrations.clients import musicbrainz as mbmod

    naps = []
    monkeypatch.setattr(mbmod.time, "sleep", naps.append)
    monkeypatch.setattr(mbmod, "_last_call", 0.0)

    class Resp:
        def __init__(self, code, body=None):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    class Session:
        headers = {}

        def __init__(self, codes):
            self.codes, self.calls = list(codes), 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            code = self.codes.pop(0)
            return Resp(code, {"ok": True} if code == 200 else None)

    s = Session([503, 503, 200])
    c = mbmod.MusicBrainzClient(session=s, min_interval=0)
    assert c.get("/recording/", query="x") == {"ok": True} and s.calls == 3
    assert [n for n in naps if n >= 2.0] == [2.0, 5.0]           # the backoff sleeps (pacing sleeps are ~0 here)

    s = Session([503, 503, 503, 503, 503])
    c = mbmod.MusicBrainzClient(session=s, min_interval=0)
    with pytest.raises(ServiceError, match="rate limited"):
        c.get("/recording/", query="x")
    assert s.calls == 1 + len(mbmod.RETRY_BACKOFF)


def test_musicbrainz_shape_artist_labels_links_and_years():
    from integrations.clients.musicbrainz import shape_artist

    a = shape_artist({
        "id": "a1", "name": "Linkin Park", "type": "Group", "country": "US", "life-span": {"begin": "1996", "ended": False},
        "tags": [{"name": "rock", "count": 3}, {"name": "nu metal", "count": 9}],
        "relations": [
            {"type": "official homepage", "url": {"resource": "https://linkinpark.com"}},
            {"type": "streaming", "url": {"resource": "https://open.spotify.com/artist/x"}},
            {"type": "social network", "url": {"resource": "https://www.instagram.com/linkinpark"}},
            {"type": "streaming", "url": {"resource": "https://open.spotify.com/artist/dupe"}},   # second Spotify link -> dropped
            {"type": "purchase for mail-order", "url": {"resource": "https://shop.example"}},    # no label for this type -> dropped
        ],
    })
    assert a["years"] == "1996 – present" and a["tags"] == ["nu metal", "rock"]
    assert [link["label"] for link in a["links"]] == ["Website", "Spotify", "Instagram"]
    assert a["url"] == "https://musicbrainz.org/artist/a1"


def test_musicbrainz_cover_art_urls_release_group_first():
    from integrations.clients.musicbrainz import cover_art_urls

    assert cover_art_urls("rg", "rel") == ["https://coverartarchive.org/release-group/rg/front-500", "https://coverartarchive.org/release/rel/front-500"]
    assert cover_art_urls(None, None) == []


# -- Last.fm ----------------------------------------------------------------------------


def test_lastfm_track_info_shapes_stats_and_strips_placeholder_art_and_read_more():
    from integrations.clients.lastfm import LastfmClient

    c = LastfmClient(cfg("lastfm"))
    calls = stub(c, {("GET", "/2.0/"): {"track": {
        "name": "Easier to Run", "url": "https://www.last.fm/music/Linkin+Park/_/Easier+to+Run", "duration": "204000",
        "listeners": "900130", "playcount": "7355834", "artist": {"name": "Linkin Park"},
        "album": {"title": "Meteora", "url": "https://www.last.fm/music/Linkin+Park/Meteora",
                  "image": [{"#text": "https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png", "size": "large"}]},
        "toptags": {"tag": [{"name": "nu metal"}, {"name": "rock"}]},
        "wiki": {"summary": 'A song by <b>Linkin Park</b>. <a href="https://www.last.fm/music/Linkin+Park/_/Easier+to+Run">Read more on Last.fm</a>'},
    }}})
    t = c.track_info("Linkin Park", "Easier to Run")
    assert t["listeners"] == 900130 and t["playcount"] == 7355834 and t["length"] == "3:24"
    assert t["album"] == "Meteora" and t["album_image"] == ""  # the grey placeholder is not art
    assert t["tags"] == ["nu metal", "rock"] and t["wiki"] == "A song by Linkin Park."
    assert calls[0][2]["params"] == {"method": "track.getInfo", "artist": "Linkin Park", "track": "Easier to Run", "autocorrect": 1}
    assert c.session.params == {"api_key": "k", "format": "json"}  # key rides on every request, never in a path


def test_lastfm_artist_info_and_top_tracks():
    from integrations.clients.lastfm import LastfmClient

    c = LastfmClient(cfg("lastfm"))
    stub(c, {("GET", "/2.0/"): lambda kw: (
        {"artist": {"name": "Linkin Park", "url": "u", "stats": {"listeners": "7117741", "playcount": "779634202"},
                    "similar": {"artist": [{"name": "Papa Roach", "url": "p"}]}, "tags": {"tag": [{"name": "rock"}]}, "bio": {"summary": "Bio text. <a href='x'>Read more on Last.fm</a>"}}}
        if kw["params"]["method"] == "artist.getInfo" else
        {"toptracks": {"track": [{"name": "In the End", "playcount": "41501727", "listeners": "1", "url": "t"}]}}
    )})
    a = c.artist_info("Linkin Park")
    assert a["listeners"] == 7117741 and a["bio"] == "Bio text." and a["similar"] == [{"name": "Papa Roach", "url": "p"}]
    assert c.artist_top_tracks("Linkin Park") == [{"name": "In the End", "playcount": 41501727, "listeners": 1, "url": "t"}]


def test_slskd_ranked_lists_candidates_best_first_and_retries_an_empty_search(monkeypatch):
    from integrations.clients.slskd import describe_file

    c = SlskdClient(cfg("slskd"))
    monkeypatch.setattr("integrations.clients.slskd.time.sleep", lambda s: None)
    attempts = {"n": 0}

    def search(query, max_wait=15):
        attempts["n"] += 1
        return [] if attempts["n"] == 1 else MAZZY_STAR_RESPONSES   # the Soulseek server dropped the first one

    c.search = search
    rows = c.ranked("Mazzy Star", "Fade Into You", retries=1)
    assert attempts["n"] == 2
    assert [r["username"] for r in rows][:2] == ["free_slot_user", "flac_hoarder"]  # free slot beats the queued flac
    assert rows[-1]["username"] == "irrelevant"  # a same-artist different track scrapes past the floor but ranks last
    assert rows[1]["queue"] == 12 and rows[0]["free_slot"] is True
    info = describe_file(rows[1]["file"])
    assert info == {"name": "01 Fade Into You.flac", "folder": "So Tonight", "ext": "FLAC", "quality": "16-bit/44.1kHz", "size_mb": 30.0, "length": "4:55"}

    attempts["n"] = 0
    assert c.ranked("Mazzy Star", "Fade Into You") == [] and attempts["n"] == 1   # no retry by default (the batch push path)


def test_lidarr_album_add_for_new_artist_then_remonitors(monkeypatch):
    monkeypatch.setattr("integrations.clients.lidarr.time.sleep", lambda s: None)
    c = LidarrClient(cfg("lidarr", root_folder="/music", quality_profile_id=5, metadata_profile_id=4))
    album = {"title": "Sparrow", "albumType": "Single", "foreignAlbumId": "alb", "artist": {"artistName": "Dol Ikara", "foreignArtistId": "art"}}
    calls = stub(c, {
        ("GET", "/api/v1/album/lookup"): [album],
        ("GET", "/api/v1/artist"): [],
        ("POST", "/api/v1/album"): {"id": 9, "artistId": 77},
        ("GET", "/api/v1/command"): [],
        ("PUT", "/api/v1/artist/editor"): None,
        ("GET", "/api/v1/artist/77"): {"id": 77, "foreignArtistId": "art", "monitored": True},
    })
    status, detail = c.push(rec("Sparrow", artist="Dol Ikara"))
    assert status == "added" and "Sparrow" in detail
    posted = [k["json"] for m, p, k in calls if (m, p) == ("POST", "/api/v1/album")][0]
    assert posted["monitored"] is True and posted["artist"]["qualityProfileId"] == 5 and posted["artist"]["rootFolderPath"] == "/music"
    assert ("PUT", "/api/v1/artist/editor") in [(m, p) for m, p, _ in calls]


def test_lidarr_swapped_title_artist_and_artist_only_fallback():
    c = LidarrClient(cfg("lidarr", root_folder="/m", quality_profile_id=1, metadata_profile_id=1))
    album = {"title": "The King Will Come", "albumType": "Album", "artist": {"artistName": "Wishbone Ash", "foreignArtistId": "wa"}}

    def lookup(kwargs):
        return [album] if "wishbone" in kwargs["params"]["term"].lower() else []

    stub(c, {("GET", "/api/v1/album/lookup"): lookup, ("GET", "/api/v1/artist"): [{"id": 1, "foreignArtistId": "wa"}], ("POST", "/api/v1/album"): {"id": 2, "artistId": 1}})
    assert c.push(rec("wishbone ash", artist="The king will come"))[0] == "added"

    # No album match at all, artist known -> artist_only without adding anything
    stub(c, {("GET", "/api/v1/album/lookup"): [], ("GET", "/api/v1/artist/lookup"): [{"artistName": "Rainbow", "foreignArtistId": "rb"}], ("GET", "/api/v1/artist"): [{"id": 3, "foreignArtistId": "rb"}]})
    c._artists = None
    status, detail = c.push(rec("Temple of the King", artist="Rainbow"))
    assert status == "artist_only" and "Rainbow" in detail


def test_jellyfin_resolve_and_playlist():
    c = JellyfinClient(cfg("jellyfin", user_id="uid"))
    calls = stub(c, {
        ("GET", "/Items"): {"Items": [{"Id": "i1", "Name": "Heat", "ProductionYear": 1995}, {"Id": "i2", "Name": "Heathers", "ProductionYear": 1989}]},
        ("POST", "/Playlists"): {"Id": "pl1"},
    })
    item, status, detail = c.resolve(rec("Heat", year=1995), "movies")
    assert status == "matched" and item["Id"] == "i1"
    assert c.create_playlist("x", ["i1"], "Video") == "pl1"
    assert [k["json"] for m, p, k in calls if m == "POST"][0]["UserId"] == "uid"
    assert c.resolve(rec("", artist="Radiohead"), "music")[1] == "skipped"


def test_jellyfin_add_or_create_playlist():
    c = JellyfinClient(cfg("jellyfin", user_id="uid"))

    def items(kwargs):
        params = kwargs["params"]
        if params.get("IncludeItemTypes") == "Playlist":
            match = params.get("searchTerm", "").casefold() == "existing"
            return {"Items": [{"Id": "pl-existing", "Name": "Existing"}] if match else []}
        return {"Items": []}

    calls = stub(c, {
        ("GET", "/Items"): items,
        ("POST", "/Playlists/pl-existing/Items"): None,
        ("POST", "/Playlists"): {"Id": "pl-new"},
    })
    pid, created = c.add_or_create_playlist("Existing", ["i1"], "Audio")
    assert pid == "pl-existing" and created is False
    add_call = [k for m, p, k in calls if p == "/Playlists/pl-existing/Items"][0]
    assert add_call["params"] == {"ids": "i1", "userId": "uid"}

    pid2, created2 = c.add_or_create_playlist("New One", ["i2"], "Audio")
    assert pid2 == "pl-new" and created2 is True


def test_jellyfin_find_playlist_requires_exact_name():
    c = JellyfinClient(cfg("jellyfin", user_id="uid"))
    stub(c, {("GET", "/Items"): {"Items": [{"Id": "pl1", "Name": "MTFL Picks 2"}]}})
    assert c.find_playlist("MTFL Picks") is None  # substring match must not count


def test_navidrome_auth_and_search():
    c = NavidromeClient(cfg("navidrome"))
    seen = {}

    def search(kwargs):
        seen.update(dict(kwargs["params"]))
        return {"subsonic-response": {"status": "ok", "searchResult3": {"song": [{"id": "s1", "title": "Fade Into You", "artist": "Mazzy Star", "path": "Mazzy Star/x.flac"}]}}}

    stub(c, {("GET", "/rest/search3"): search, ("GET", "/rest/createPlaylist"): {"subsonic-response": {"status": "ok", "playlist": {"id": "p1"}}}})
    song, status, _ = c.resolve(rec("fade into you", artist="mazzy star"))
    assert status == "matched" and song["path"].endswith(".flac")
    assert seen["u"] == "u" and len(seen["t"]) == 32 and seen["c"] == "MediaThatFeelsLike"
    assert c.create_playlist("p", ["s1"]) == "p1"


def test_navidrome_error_surfaces():
    c = NavidromeClient(cfg("navidrome"))
    stub(c, {("GET", "/rest/ping"): {"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}}})
    with pytest.raises(ServiceError, match="Wrong username"):
        c.test()


def test_navidrome_add_or_create_playlist():
    c = NavidromeClient(cfg("navidrome"))
    stub(c, {
        ("GET", "/rest/getPlaylists"): {"subsonic-response": {"status": "ok", "playlists": {"playlist": [{"id": "p-existing", "name": "Existing"}]}}},
        ("GET", "/rest/updatePlaylist"): {"subsonic-response": {"status": "ok"}},
        ("GET", "/rest/createPlaylist"): {"subsonic-response": {"status": "ok", "playlist": {"id": "p-new"}}},
    })
    pid, created = c.add_or_create_playlist("Existing", ["s1"])
    assert pid == "p-existing" and created is False
    pid2, created2 = c.add_or_create_playlist("brand new name", ["s2"])
    assert pid2 == "p-new" and created2 is True  # no case-insensitive collision with "Existing"


# -- slskd --------------------------------------------------------------------------------

MAZZY_STAR_RESPONSES = [
    {  # busy peer, lossless -- best quality but has to queue behind others
        "username": "flac_hoarder", "hasFreeUploadSlot": False, "queueLength": 12,
        "files": [{"filename": r"Mazzy Star\So Tonight\01 Fade Into You.flac", "extension": "flac", "size": 31504876, "length": 295, "bitDepth": 16, "sampleRate": 44100}],
    },
    {  # free slot, decent mp3 -- should win over the queued flac
        "username": "free_slot_user", "hasFreeUploadSlot": True, "queueLength": 0,
        "files": [{"filename": r"Music\Mazzy Star - Fade Into You.mp3", "extension": "mp3", "size": 9440706, "length": 295, "bitRate": 256, "isVariableBitRate": False}],
    },
    {  # unrelated track by the same artist -- must not be picked for this query
        "username": "irrelevant", "hasFreeUploadSlot": True, "queueLength": 0,
        "files": [{"filename": r"Mazzy Star - Halah.mp3", "extension": "mp3", "size": 8000000, "length": 250, "bitRate": 320}],
    },
    {  # a cover art / tab file that happens to mention the title -- not audio
        "username": "junk", "hasFreeUploadSlot": True, "queueLength": 0,
        "files": [{"filename": r"Fade Into You lyrics.txt", "extension": "txt", "size": 500}],
    },
    {  # a 12-second sample -- too short to be the real track
        "username": "sample_only", "hasFreeUploadSlot": True, "queueLength": 0,
        "files": [{"filename": r"Fade Into You (sample).mp3", "extension": "mp3", "size": 200000, "length": 12, "bitRate": 320}],
    },
]


def fixed_uuid(monkeypatch, value="11111111-1111-1111-1111-111111111111"):
    monkeypatch.setattr("integrations.clients.slskd.uuid.uuid4", lambda: value)
    monkeypatch.setattr("integrations.clients.slskd.time.sleep", lambda s: None)
    return value


def test_slskd_search_polls_until_complete_and_cleans_up(monkeypatch):
    sid = fixed_uuid(monkeypatch)
    c = SlskdClient(cfg("slskd"))
    poll_calls = {"n": 0}

    def status(kwargs):
        poll_calls["n"] += 1
        return {"isComplete": poll_calls["n"] >= 2}

    calls = stub(c, {
        ("POST", "/api/v0/searches"): {"id": sid},
        ("GET", f"/api/v0/searches/{sid}"): status,
        ("GET", f"/api/v0/searches/{sid}/responses"): MAZZY_STAR_RESPONSES,
        ("DELETE", f"/api/v0/searches/{sid}"): None,
    })
    responses = c.search("Mazzy Star Fade Into You")
    assert responses == MAZZY_STAR_RESPONSES
    assert poll_calls["n"] == 2  # polled twice before isComplete
    assert ("DELETE", f"/api/v0/searches/{sid}") in [(m, p) for m, p, _ in calls]


def test_slskd_search_cancels_early_when_deadline_hits_before_slskd_finishes(monkeypatch):
    """Regression: slskd's GET .../responses returns [] -- not partial results -- until
    the search itself reports isComplete. An obscure query can run for slskd's own
    internal timeout (~25-30s observed live), well past a sane UI wait, so hitting our
    own deadline first must PUT (cancel) the search to unlock whatever arrived so far --
    otherwise every search that doesn't finish inside `max_wait` silently returns
    nothing, which is exactly what happened live against real (findable) tracks."""
    sid = fixed_uuid(monkeypatch)
    c = SlskdClient(cfg("slskd"))
    calls = stub(c, {
        ("POST", "/api/v0/searches"): {"id": sid},
        ("GET", f"/api/v0/searches/{sid}"): {"isComplete": False},  # never completes on its own
        ("PUT", f"/api/v0/searches/{sid}"): None,
        ("GET", f"/api/v0/searches/{sid}/responses"): MAZZY_STAR_RESPONSES,
        ("DELETE", f"/api/v0/searches/{sid}"): None,
    })
    responses = c.search("Mazzy Star Fade Into You", max_wait=0.05, poll_interval=0.01)
    assert responses == MAZZY_STAR_RESPONSES
    methods = [(m, p) for m, p, _ in calls]
    assert ("PUT", f"/api/v0/searches/{sid}") in methods
    # PUT (cancel) must happen before fetching responses, not after.
    assert methods.index(("PUT", f"/api/v0/searches/{sid}")) < methods.index(("GET", f"/api/v0/searches/{sid}/responses"))


def test_slskd_find_best_prefers_free_slot_and_rejects_bad_matches(monkeypatch):
    sid = fixed_uuid(monkeypatch)
    c = SlskdClient(cfg("slskd"))
    stub(c, {
        ("POST", "/api/v0/searches"): {"id": sid},
        ("GET", f"/api/v0/searches/{sid}"): {"isComplete": True},
        ("GET", f"/api/v0/searches/{sid}/responses"): MAZZY_STAR_RESPONSES,
        ("DELETE", f"/api/v0/searches/{sid}"): None,
    })
    username, file, score = c.find_best("Mazzy Star", "Fade Into You")
    assert username == "free_slot_user"
    assert file["extension"] == "mp3" and file["bitRate"] == 256
    assert score > 0


def test_slskd_find_best_returns_nothing_when_only_junk_matches(monkeypatch):
    sid = fixed_uuid(monkeypatch)
    c = SlskdClient(cfg("slskd"))
    stub(c, {
        ("POST", "/api/v0/searches"): {"id": sid},
        ("GET", f"/api/v0/searches/{sid}"): {"isComplete": True},
        ("GET", f"/api/v0/searches/{sid}/responses"): [MAZZY_STAR_RESPONSES[3], MAZZY_STAR_RESPONSES[4]],  # only the lyrics txt and the sample
        ("DELETE", f"/api/v0/searches/{sid}"): None,
    })
    username, file, score = c.find_best("Mazzy Star", "Fade Into You")
    assert username is None and file is None


def test_slskd_push_queues_the_download(monkeypatch):
    sid = fixed_uuid(monkeypatch)
    c = SlskdClient(cfg("slskd"))
    calls = stub(c, {
        ("POST", "/api/v0/searches"): {"id": sid},
        ("GET", f"/api/v0/searches/{sid}"): {"isComplete": True},
        ("GET", f"/api/v0/searches/{sid}/responses"): MAZZY_STAR_RESPONSES,
        ("DELETE", f"/api/v0/searches/{sid}"): None,
        ("POST", "/api/v0/transfers/downloads/free_slot_user"): {"enqueued": [{"id": "x"}], "failed": []},
    })
    status, detail = c.push(rec("Fade Into You", artist="Mazzy Star"))
    assert status == "queued" and "free_slot_user" in detail and "MP3" in detail
    payload = [k["json"] for m, p, k in calls if p == "/api/v0/transfers/downloads/free_slot_user"][0]
    assert payload == [{"filename": r"Music\Mazzy Star - Fade Into You.mp3", "size": 9440706}]


def test_slskd_push_search_only_when_auto_download_off(monkeypatch):
    sid = fixed_uuid(monkeypatch)
    c = SlskdClient(cfg("slskd", auto_download=False))
    calls = stub(c, {
        ("POST", "/api/v0/searches"): {"id": sid},
        ("GET", f"/api/v0/searches/{sid}"): {"isComplete": True},
        ("GET", f"/api/v0/searches/{sid}/responses"): MAZZY_STAR_RESPONSES,
        ("DELETE", f"/api/v0/searches/{sid}"): None,
    })
    status, detail = c.push(rec("Fade Into You", artist="Mazzy Star"), options={"auto_download": False})
    assert status == "found" and "not downloaded" in detail
    assert not any(p.startswith("/api/v0/transfers/downloads/") for _, p, _ in calls)


def test_slskd_push_not_found_and_skipped():
    c = SlskdClient(cfg("slskd"))
    c.search = lambda query, **kw: []  # no peers responded at all
    assert c.push(rec("Some Extremely Obscure Rarity", artist="Nobody"))[0] == "not_found"
    assert c.push(rec("", artist=""))[0] == "skipped"


def test_slskd_enqueue_raises_on_failed_download():
    c = SlskdClient(cfg("slskd"))
    stub(c, {("POST", "/api/v0/transfers/downloads/someuser"): {"enqueued": [], "failed": [{"exception": "File not shared."}]}})
    with pytest.raises(ServiceError, match="File not shared"):
        c.enqueue("someuser", {"filename": "x.mp3", "size": 1})


def test_slskd_test_reports_connection_state():
    c = SlskdClient(cfg("slskd"))
    stub(c, {("GET", "/api/v0/application"): {"version": {"current": "0.26.0.0"}, "server": {"isConnected": True, "state": "Connected, LoggedIn"}}})
    assert "0.26.0.0" in c.test()
    stub(c, {("GET", "/api/v0/application"): {"version": {"current": "0.26.0.0"}, "server": {"isConnected": False}}})
    assert "not connected" in c.test()


def _peer(username, directory, files):
    return {"username": username, "directories": [{"directory": directory, "files": files}]}


def _file(state, size=1000, transferred=0, speed=0):
    return {"state": state, "size": size, "bytesTransferred": transferred, "averageSpeed": speed}


def test_slskd_summarize_counts_are_per_file():
    from integrations.api import summarize

    transfers = [
        _peer("a", r"Music\Album One", [_file("InProgress", transferred=500, speed=1024), _file("Queued, Remotely")]),
        _peer("b", r"Music\Album Two", [_file("Completed, Succeeded", transferred=1000), _file("Completed, Rejected")]),
    ]
    out = summarize(transfers)
    assert out["counts"] == {"downloading": 1, "queued": 1, "completed": 1, "failed": 1}


def test_slskd_summarize_groups_active_by_peer_and_directory():
    from integrations.api import summarize

    # A big multi-file grab, half done, still going -- must collapse to one row, not many.
    files = [_file("Completed, Succeeded", size=1000, transferred=1000) for _ in range(75)]
    files += [_file("Queued, Remotely", size=1000) for _ in range(75)]
    transfers = [_peer("Tymemage", r"Music\Buddy Holly\Not Fade Away", files)]
    out = summarize(transfers)
    assert len(out["active"]) == 1
    assert out["active"][0]["label"] == "Tymemage · Not Fade Away"
    assert out["active"][0]["percent"] == 50


def test_slskd_summarize_excludes_fully_resolved_directories():
    from integrations.api import summarize

    transfers = [
        _peer("a", r"Music\Done Album", [_file("Completed, Succeeded", transferred=1000)]),
        _peer("b", r"Music\Dead Album", [_file("Completed, Rejected")]),
    ]
    out = summarize(transfers)
    assert out["active"] == []  # nothing left to show progress on -- everything's resolved


def test_slskd_summarize_sorts_active_by_percent_and_caps_the_list():
    from integrations.api import ACTIVE_LIMIT, summarize

    transfers = [
        _peer(f"user{i}", f"Music\\Album {i}", [_file("InProgress", size=100, transferred=i)])
        for i in range(ACTIVE_LIMIT + 3)
    ]
    out = summarize(transfers)
    assert len(out["active"]) == ACTIVE_LIMIT
    percents = [a["percent"] for a in out["active"]]
    assert percents == sorted(percents, reverse=True)


def test_slskd_summarize_formats_speed_human_readable():
    from integrations.api import summarize

    transfers = [_peer("a", r"Music\Album", [_file("InProgress", size=100, transferred=1, speed=2_500_000)])]
    out = summarize(transfers)
    assert out["active"][0]["speed"] == "2.4 MB/s"
    transfers = [_peer("a", r"Music\Album", [_file("Queued, Remotely", size=100)])]
    assert summarize(transfers)["active"][0]["speed"] == ""


def test_push_slskd_caps_batch_size_and_skips_already_queued(db):

    from integrations.push import SLSKD_MAX_PER_PUSH, _push_slskd
    from vibes.models import Post, Recommendation, Source

    src = Source.objects.create(subreddit="SongsThatFeelLikeThis", kind=Source.Kind.MUSIC)
    from django.utils import timezone

    post = Post.objects.create(source=src, reddit_id="p1", title="t", permalink="/x/", created_utc=timezone.now())
    recs = [
        Recommendation.objects.create(post=post, parsed_artist=f"Artist {i}", parsed_title=f"Track {i}", method="short_comment", confidence=0.9, included=True, order=i)
        for i in range(SLSKD_MAX_PER_PUSH + 3)
    ]
    recs[0].integration_state = {"slskd": {"status": "queued", "detail": "already got this one"}}
    recs[0].save()

    calls = {"n": 0}

    class FakeClient:
        config = cfg("slskd")

        def push(self, rec, options=None):
            calls["n"] += 1
            return "queued", f"queued {rec.parsed_title}"

    result = _push_slskd(FakeClient(), recs)
    assert calls["n"] == SLSKD_MAX_PER_PUSH  # capped, and the already-queued one wasn't retried
    assert "already queued from an earlier push" in result["summary"]
    assert "more waiting" in result["summary"]
    processed_titles = {r["rec"].parsed_title for r in result["results"]}
    assert recs[0].parsed_title not in processed_titles
    recs[0].refresh_from_db()
    assert recs[0].integration_state["slskd"]["detail"] == "already got this one"  # untouched, not re-pushed


def test_slskd_batch_size_shrinks_as_search_timeout_grows():
    """A longer configured search timeout must not let a batch exceed the gunicorn
    worker timeout -- the batch shrinks to compensate rather than staying fixed."""
    from integrations.push import SLSKD_MAX_PER_PUSH, _slskd_batch_size

    assert _slskd_batch_size(15) == SLSKD_MAX_PER_PUSH  # default: fits the normal cap
    assert _slskd_batch_size(25) * 25 <= 100
    assert _slskd_batch_size(25) < SLSKD_MAX_PER_PUSH
    assert _slskd_batch_size(1000) == 1  # never zero, however extreme the timeout


# -- push_one: per-recommendation actions --------------------------------------------------


def _music_rec(db, **overrides):
    from vibes.models import Post, Recommendation, Source

    src = Source.objects.create(subreddit="SongsThatFeelLikeThis", kind=Source.Kind.MUSIC)
    from django.utils import timezone

    post = Post.objects.create(source=src, reddit_id=overrides.pop("reddit_id", "p1"), title="t", permalink="/x/", created_utc=timezone.now())
    defaults = {"post": post, "parsed_artist": "MIKA", "parsed_title": "Love Today", "method": "artist_title", "confidence": 0.9, "included": True, "order": 0}
    defaults.update(overrides)
    return Recommendation.objects.create(**defaults)


def test_push_one_playlist_adds_to_existing_playlist(db):
    from integrations.push import _push_one_playlist

    rec_obj = _music_rec(db)

    class FakeNavidromeClient:
        config = cfg("navidrome")

        def resolve(self, r, kind):
            return {"id": "song1"}, "matched", "MIKA - Love Today"

        def add_or_create_playlist(self, name, ids):
            assert ids == ["song1"]
            return ("existing-id", False) if name == "My Playlist" else ("new-id", True)

    outcome = _push_one_playlist(FakeNavidromeClient(), "navidrome", rec_obj, "My Playlist")
    assert outcome["error"] is None
    assert "Added to playlist" in outcome["summary"] and "My Playlist" in outcome["summary"]
    rec_obj.refresh_from_db()
    assert rec_obj.integration_state["navidrome"]["status"] == "playlisted"


def test_push_one_playlist_creates_when_no_default_name_given(db):
    from integrations.push import DEFAULT_PLAYLIST_NAME, _push_one_playlist

    rec_obj = _music_rec(db)

    class FakeClient:
        config = cfg("navidrome")

        def resolve(self, r, kind):
            return {"id": "song1"}, "matched", "MIKA - Love Today"

        def add_or_create_playlist(self, name, ids):
            assert name == DEFAULT_PLAYLIST_NAME  # blank prompt -> falls back to the default
            return "new-id", True

    outcome = _push_one_playlist(FakeClient(), "navidrome", rec_obj, "")
    assert "Created playlist" in outcome["summary"] and DEFAULT_PLAYLIST_NAME in outcome["summary"]


def test_push_one_playlist_not_found_records_chip_without_creating_anything(db):
    from integrations.push import _push_one_playlist

    rec_obj = _music_rec(db)

    class FakeClient:
        config = cfg("navidrome")
        create_called = False

        def resolve(self, r, kind):
            return None, "not_found", "no match"

        def add_or_create_playlist(self, *a, **k):
            raise AssertionError("must not be called when nothing resolved")

    outcome = _push_one_playlist(FakeClient(), "navidrome", rec_obj, "My Playlist")
    assert outcome["results"][0]["status"] == "not_found"
    rec_obj.refresh_from_db()
    assert rec_obj.integration_state["navidrome"]["status"] == "not_found"


def test_push_one_dispatches_lidarr_and_slskd_as_single_item_batches(db, monkeypatch):
    from integrations import push as push_module

    rec_obj = _music_rec(db)

    class FakeLidarr:
        config = cfg("lidarr")

        def push(self, r):
            return "added", "added ok"

    monkeypatch.setattr(push_module, "client_for", lambda service: FakeLidarr())
    outcome = push_module.push_one(rec_obj, "lidarr")
    assert outcome["results"][0]["status"] == "added"
    rec_obj.refresh_from_db()
    assert rec_obj.integration_state["lidarr"]["status"] == "added"

    class FakeSlskd:
        config = cfg("slskd")

        def push(self, r, options=None):
            return "queued", "queued ok"

    monkeypatch.setattr(push_module, "client_for", lambda service: FakeSlskd())
    outcome = push_module.push_one(rec_obj, "slskd")
    assert outcome["results"][0]["status"] == "queued"
    rec_obj.refresh_from_db()
    assert rec_obj.integration_state["slskd"]["status"] == "queued"


def test_push_one_records_an_error_chip_when_service_not_configured(db):
    from integrations.models import ServiceConfig
    from integrations.push import push_one

    ServiceConfig.objects.create(service="radarr", enabled=False)  # deliberately unconfigured
    rec_obj = _music_rec(db, reddit_id="p2")  # the fixture's kind is irrelevant to this failure path
    outcome = push_one(rec_obj, "radarr")
    assert outcome["error"] is None  # doesn't blow up the request
    assert outcome["results"][0]["status"] == "error"
    rec_obj.refresh_from_db()
    assert rec_obj.integration_state["radarr"]["status"] == "error"
    assert "not configured" in rec_obj.integration_state["radarr"]["detail"]
