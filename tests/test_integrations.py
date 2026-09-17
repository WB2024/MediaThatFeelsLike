"""Integration clients with the HTTP layer stubbed out."""

from types import SimpleNamespace

import pytest

from integrations.clients.base import ServiceError, similarity
from integrations.clients.jellyfin import JellyfinClient
from integrations.clients.lidarr import LidarrClient
from integrations.clients.navidrome import NavidromeClient
from integrations.clients.radarr import RadarrClient
from integrations.clients.slskd import SlskdClient
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
