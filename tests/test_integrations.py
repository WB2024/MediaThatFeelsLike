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
