"""Integration clients with the HTTP layer stubbed out."""

from types import SimpleNamespace

import pytest

from integrations.clients.base import ServiceError, similarity
from integrations.clients.jellyfin import JellyfinClient
from integrations.clients.lidarr import LidarrClient
from integrations.clients.navidrome import NavidromeClient
from integrations.clients.radarr import RadarrClient
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
    lookup = [{"title": "Heat", "year": 1995, "tmdbId": 949}, {"title": "Heat", "year": 1972, "tmdbId": 111}]
    calls = stub(c, {("GET", "/api/v3/movie/lookup"): lookup, ("GET", "/api/v3/movie"): [], ("POST", "/api/v3/movie"): {"id": 1}})
    status, detail = c.push(rec("Heat", year=1972))
    assert status == "added" and "1972" in detail
    payload = [k["json"] for m, p, k in calls if m == "POST"][0]
    assert payload["tmdbId"] == 111 and payload["qualityProfileId"] == 3 and payload["rootFolderPath"] == "/movies"
    assert payload["addOptions"]["searchForMovie"] is True


def test_radarr_reports_existing_and_not_found():
    c = RadarrClient(cfg("radarr", root_folder="/m", quality_profile_id=1))
    stub(c, {("GET", "/api/v3/movie/lookup"): [{"title": "Drive", "year": 2011, "tmdbId": 64690, "id": 7}]})
    assert c.push(rec("Drive", year=2011))[0] == "exists"
    stub(c, {("GET", "/api/v3/movie/lookup"): [{"title": "Something Else Entirely", "year": 1999, "tmdbId": 1}]})
    assert c.push(rec("Drive"))[0] == "not_found"


def test_radarr_requires_setup():
    c = RadarrClient(cfg("radarr"))
    stub(c, {("GET", "/api/v3/movie/lookup"): [{"title": "Drive", "year": 2011, "tmdbId": 1}], ("GET", "/api/v3/movie"): []})
    with pytest.raises(ServiceError):
        c.push(rec("Drive"))


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
