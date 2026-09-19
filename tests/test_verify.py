"""MusicBrainz as the referee for artist/title orientation: the detail page path
(`enrich.resolve_recording`) and the sidecar's `verify_recommendations` command."""

from io import StringIO

import pytest
from django.core.management import call_command

from integrations.clients.base import ServiceError
from integrations.clients.musicbrainz import MusicBrainzClient
from integrations.enrich import resolve_recording
from integrations.models import ServiceConfig
from vibes.models import KnownArtist, Post, Recommendation, Source

MB = {("sheryl crow", "all i wanna do"): {"mbid": "rec-1", "title": "All I Wanna Do", "artist": "Sheryl Crow", "artist_mbid": "a-1"},
      ("mika", "love today"): {"mbid": "rec-2", "title": "Love Today", "artist": "MIKA", "artist_mbid": "a-2"}}


@pytest.fixture
def fake_mb(monkeypatch):
    calls = []

    def search_recording(self, artist, title):
        calls.append((artist, title))
        return MB.get((artist.lower(), title.lower()))

    monkeypatch.setattr(MusicBrainzClient, "search_recording", search_recording)
    return calls


def _rec(reddit_id="p1", **overrides):
    src, _ = Source.objects.get_or_create(subreddit="SongsThatFeelLikeThis", defaults={"kind": Source.Kind.MUSIC})
    from django.utils import timezone

    post, _ = Post.objects.get_or_create(reddit_id=reddit_id, defaults={"source": src, "title": "t", "permalink": "/x/", "created_utc": timezone.now()})
    fields = {"post": post, "parsed_artist": "All I wanna do", "parsed_title": "Sheryl crow", "method": "artist_title", "confidence": 0.85}
    fields.update(overrides)
    return Recommendation.objects.create(**fields)


def test_resolve_swaps_persists_and_learns_the_artist(db, fake_mb):
    rec = _rec()
    found, swapped = resolve_recording(rec)
    assert swapped and found["mbid"] == "rec-1"
    assert fake_mb == [("All I wanna do", "Sheryl crow"), ("Sheryl crow", "All I wanna do")]   # as stored first, then reversed
    rec.refresh_from_db()
    assert (rec.parsed_artist, rec.parsed_title) == ("Sheryl crow", "All I wanna do")
    assert rec.verified_at is not None
    assert "sheryl crow" in KnownArtist.norms()


def test_resolve_keeps_a_correct_row_and_only_hits_musicbrainz_once(db, fake_mb):
    rec = _rec(parsed_artist="MIKA", parsed_title="Love Today")
    found, swapped = resolve_recording(rec)
    assert found["mbid"] == "rec-2" and not swapped and fake_mb == [("MIKA", "Love Today")]
    rec.refresh_from_db()
    assert (rec.parsed_artist, rec.parsed_title, rec.verified_at is not None) == ("MIKA", "Love Today", True)


def test_resolve_never_rewrites_a_human_edit(db, fake_mb):
    rec = _rec(edited=True)
    found, swapped = resolve_recording(rec)
    assert swapped and found["mbid"] == "rec-1"      # the page still shows the right recording...
    rec.refresh_from_db()
    assert (rec.parsed_artist, rec.parsed_title) == ("All I wanna do", "Sheryl crow")   # ...but the row is theirs


def test_resolve_marks_unknown_recordings_verified_but_not_on_a_network_failure(db, monkeypatch):
    rec = _rec(parsed_artist="Nobody", parsed_title="Nothing")
    monkeypatch.setattr(MusicBrainzClient, "search_recording", lambda self, a, t: None)
    assert resolve_recording(rec) == (None, False)
    rec.refresh_from_db()
    assert rec.verified_at is not None and KnownArtist.objects.count() == 0

    other = _rec(reddit_id="p2", parsed_artist="Nobody", parsed_title="Nothing")

    def boom(self, a, t):
        raise ServiceError("503 from musicbrainz")

    monkeypatch.setattr(MusicBrainzClient, "search_recording", boom)
    with pytest.raises(ServiceError):
        resolve_recording(other)
    other.refresh_from_db()
    assert other.verified_at is None


def test_command_seeds_from_lidarr_then_verifies_the_backlog(db, fake_mb, monkeypatch):
    from integrations.clients.lidarr import LidarrClient

    ServiceConfig.objects.update_or_create(
        service=ServiceConfig.Service.LIDARR, defaults={"enabled": True, "url": "http://lidarr.test", "api_key": "k"},
    )
    monkeypatch.setattr(LidarrClient, "artists", lambda self: {"mb-1": {"artistName": "Michelle Branch"}, "mb-2": {"artistName": "Faith Hill"}})
    wrong = _rec()
    right = _rec(reddit_id="p2", parsed_artist="MIKA", parsed_title="Love Today")
    done = _rec(reddit_id="p3", parsed_artist="Done", parsed_title="Already")
    done.verified_at = done.created_at
    done.save()
    artist_only = _rec(reddit_id="p4", parsed_artist="Portishead", parsed_title="")

    out = StringIO()
    call_command("verify_recommendations", stdout=out)
    text = out.getvalue()
    assert "2 Lidarr artists, 2 new" in text
    assert "swapped: All I wanna do - Sheryl crow  ->  Sheryl crow - All I wanna do" in text
    assert "Verified 2 recommendations: 2 matched (1 swapped), 0 unknown" in text
    assert KnownArtist.norms() == {"michelle branch", "faith hill", "sheryl crow", "mika"}
    wrong.refresh_from_db()
    assert (wrong.parsed_artist, wrong.parsed_title) == ("Sheryl crow", "All I wanna do")
    for r in (right, done, artist_only):
        r.refresh_from_db()
    assert right.verified_at is not None and artist_only.verified_at is None
    assert done.verified_at == done.created_at                      # not re-checked without --recheck
    assert ("Done", "Already") not in fake_mb


def test_command_rides_out_a_rate_limit_hiccup_but_stops_on_a_real_outage(db, monkeypatch):
    from reddit_sync.management.commands import verify_recommendations as cmd

    # the dev box's .env may seed a real Lidarr; keep the seeding step out of this one
    ServiceConfig.objects.update_or_create(service=ServiceConfig.Service.LIDARR, defaults={"enabled": False})
    older = [_rec(reddit_id=f"p{i}", parsed_artist=f"Band {i}", parsed_title=f"Song {i}") for i in (1, 2, 3)]
    fine = _rec(reddit_id="p4", parsed_artist="MIKA", parsed_title="Love Today")
    newest = _rec(reddit_id="p5", parsed_artist="Sheryl Crow", parsed_title="All I Wanna Do")
    naps, seen = [], []
    monkeypatch.setattr(cmd.time, "sleep", naps.append)

    def flaky(self, artist, title):
        seen.append(artist)
        if len(seen) == 1:                      # newest post first: p5 gets one 503
            raise ServiceError("rate limited (503)")
        if artist == "MIKA":
            return MB[("mika", "love today")]
        raise ServiceError("timed out")         # then MusicBrainz goes away for good

    monkeypatch.setattr(MusicBrainzClient, "search_recording", flaky)
    out, err = StringIO(), StringIO()
    call_command("verify_recommendations", stdout=out, stderr=err)
    # p5 hiccup (pause), p4 ok (counter resets), p3 + p2 hiccups (pause), p1 = third in a row -> stop
    assert err.getvalue().count("MusicBrainz hiccup") == 3 and naps == [cmd.PAUSE_AFTER_FAILURE] * 3
    assert "MusicBrainz unavailable (timed out); verified 1 of 5 before stopping" in err.getvalue()
    for r in older + [fine, newest]:
        r.refresh_from_db()
    assert fine.verified_at is not None
    assert newest.verified_at is None           # a failed attempt is skipped, not marked; the next run picks it up
    assert all(r.verified_at is None for r in older)
