import contextlib
import re
from urllib.parse import unquote

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from vibes.models import Post, Recommendation, Source


def youtube_query(content):
    m = re.search(rb"youtube\.com/results\?search_query=([^\"&]+)", content)
    return unquote(m.group(1).decode()) if m else None


def spotify_query(content):
    m = re.search(rb"open\.spotify\.com/search/([^\"&]+)", content)
    return unquote(m.group(1).decode()) if m else None


@pytest.fixture
def post(db):
    src = Source.objects.create(subreddit="SongsThatFeelLikeThis", kind=Source.Kind.MUSIC)
    p = Post.objects.create(source=src, reddit_id="abc", title="songs that feel like this", permalink="/r/x/", created_utc=timezone.now(), num_comments=3)
    Recommendation.objects.create(post=p, parsed_artist="Mazzy Star", parsed_title="Fade Into You", method="artist_title", confidence=0.9, included=True, order=0)
    Recommendation.objects.create(post=p, parsed_artist="", parsed_title="in all seriousness", method="short_comment", confidence=0.5, included=False, order=1)
    return p


def test_section_and_detail_render(client, post):
    r = client.get(reverse("vibes:section", args=["music"]))
    assert r.status_code == 200 and b"songs that feel like this" in r.content
    r = client.get(post.get_absolute_url())
    assert r.status_code == 200 and b"Mazzy Star - Fade Into You" in r.content
    assert b"1 excluded" in r.content


def test_recommendations_sorted_by_mention_count_descending(client, db):
    src = Source.objects.create(subreddit="SongsThatFeelLikeThis", kind=Source.Kind.MUSIC)
    p = Post.objects.create(source=src, reddit_id="mc1", title="t", permalink="/z/", created_utc=timezone.now())
    # Confidence deliberately doesn't track mention_count, so a naive confidence-first
    # sort would put "One Mention" before "Two Mentions".
    Recommendation.objects.create(post=p, parsed_title="One Mention", method="short_comment", confidence=0.95, mention_count=1, included=True, order=0)
    Recommendation.objects.create(post=p, parsed_title="Five Mentions", method="short_comment", confidence=0.6, mention_count=5, included=True, order=1)
    Recommendation.objects.create(post=p, parsed_title="Two Mentions", method="short_comment", confidence=0.7, mention_count=2, included=True, order=2)

    r = client.get(p.get_absolute_url())
    content = r.content.decode()
    positions = {t: content.index(t) for t in ("Five Mentions", "Two Mentions", "One Mention")}
    assert positions["Five Mentions"] < positions["Two Mentions"] < positions["One Mention"]


def test_tile_grid_gallery_cycling_markup(client, post):
    from vibes.models import PostImage

    r = client.get(reverse("vibes:section", args=["music"]))
    assert b"data-images=" not in r.content and b"tile-nav" not in r.content  # single/no image: nothing to cycle

    PostImage.objects.create(post=post, order=0, source_url="https://example.test/a.jpg")
    PostImage.objects.create(post=post, order=1, source_url="https://example.test/b.jpg")
    r = client.get(reverse("vibes:section", args=["music"]))
    assert b'data-images="https://example.test/a.jpg|https://example.test/b.jpg"' in r.content
    assert b"tile-nav prev" in r.content and b"tile-nav next" in r.content
    assert b'class="gal">1 / 2</span>' in r.content


def test_unknown_section_404(client, db):
    assert client.get("/nope/").status_code == 404


def test_youtube_links_search_the_song_or_the_trailer(client, post, db):
    r = client.get(post.get_absolute_url())  # music fixture: Mazzy Star - Fade Into You
    assert b"\xe2\x96\xb6 Play" in r.content and b"\xe2\x96\xb6 Trailer" not in r.content
    assert youtube_query(r.content) == "Mazzy Star Fade Into You"
    assert b"Spotify" in r.content
    assert spotify_query(r.content) == "Mazzy Star Fade Into You"

    src = Source.objects.create(subreddit="MoviesThatFeelLike", kind=Source.Kind.MOVIES)
    movie_post = Post.objects.create(source=src, reddit_id="mv1", title="movies that feel like this", permalink="/r/y/", created_utc=timezone.now())
    Recommendation.objects.create(post=movie_post, parsed_title="Drive", parsed_year=2011, method="title_year", confidence=0.9, included=True, order=0)
    r = client.get(movie_post.get_absolute_url())
    assert b"\xe2\x96\xb6 Trailer" in r.content and b"\xe2\x96\xb6 Play" not in r.content
    assert youtube_query(r.content) == "Drive (2011) trailer"
    assert b"Spotify" not in r.content  # movies have no Spotify link -- there's no trailer catalogue there


def test_api_hot_only_lists_posts_with_a_cached_image(client, post):
    """The Glance widget needs a real, reliably-loadable image -- Post.primary_image
    falls back to the raw Reddit CDN URL when nothing's cached, which is fine for the
    app's own grid but not something an external viewer's browser is guaranteed to
    load, so the API is stricter than the grid here."""
    from vibes.models import PostImage

    assert client.get(reverse("vibes:api_hot", args=["nonsense"])).status_code == 404
    assert client.get(reverse("vibes:api_hot", args=["movies"])).json() == []  # this fixture's post is music
    assert client.get(reverse("vibes:api_hot", args=["music"])).json() == []  # no cached image yet

    PostImage.objects.create(post=post, order=0, source_url="https://example.test/x.jpg")  # uncached
    assert client.get(reverse("vibes:api_hot", args=["music"])).json() == []

    cached = PostImage.objects.create(post=post, order=1, source_url="https://example.test/y.jpg")
    cached.file.save("y.jpg", ContentFile(b"fake"), save=True)
    try:
        data = client.get(reverse("vibes:api_hot", args=["music"]) + "?limit=5").json()
        assert len(data) == 1
        item = data[0]
        assert item["id"] == post.pk and item["rec_count"] == 1  # one included rec in the fixture
        assert item["image"].endswith(cached.file.url)
        assert item["url"].endswith(post.get_absolute_url())
    finally:
        with contextlib.suppress(OSError):
            cached.file.delete(save=False)


def test_api_stats(client, post):
    data = client.get(reverse("vibes:api_stats")).json()
    assert data == {"movies": 0, "music": 1, "recommendations": 1, "last_synced": None}


def test_api_slskd_status_reports_not_configured(client, db):
    data = client.get(reverse("integrations:api_slskd")).json()
    assert data["connected"] is False and data["active"] == [] and "error" in data


def test_api_slskd_status_summarizes_a_configured_client(client, db, monkeypatch):
    from integrations import api as api_module

    class FakeClient:
        api = "/api/v0"

        def get(self, path):
            if path.endswith("/application"):
                return {"version": {"current": "0.26.0"}, "server": {"isConnected": True}}
            return [{"username": "flaggy12", "directories": [{"directory": r"Music\Specials", "files": [
                {"state": "InProgress", "size": 100, "bytesTransferred": 40, "averageSpeed": 512},
            ]}]}]

    monkeypatch.setattr(api_module, "client_for", lambda service: FakeClient())
    data = client.get(reverse("integrations:api_slskd")).json()
    assert data["connected"] is True and data["version"] == "0.26.0"
    assert data["counts"]["downloading"] == 1
    assert data["active"] == [{"label": "flaggy12 · Specials", "percent": 40, "speed": "512 B/s"}]


def test_htmx_toggle_and_edit(client, post):
    rec = post.recommendations.get(parsed_title="Fade Into You")
    r = client.post(reverse("vibes:rec_toggle", args=[rec.pk]), HTTP_HX_REQUEST="true")
    assert r.status_code == 200
    rec.refresh_from_db()
    assert rec.included is False and rec.edited is True
    r = client.post(reverse("vibes:rec_edit", args=[rec.pk]), {"artist": "Mazzy Star", "title": "Halah"})
    rec.refresh_from_db()
    assert r.status_code == 200 and rec.parsed_title == "Halah" and rec.included is True


def test_add_manual_and_bulk_reset(client, post):
    client.post(reverse("vibes:rec_add", args=[post.pk]), {"artist": "Beach House", "title": "Space Song"})
    assert post.recommendations.filter(parsed_title="Space Song", method="manual").exists()
    client.post(reverse("vibes:rec_bulk", args=[post.pk]), {"action": "exclude_all"})
    assert not post.recommendations.filter(included=True).exists()
    client.post(reverse("vibes:rec_bulk", args=[post.pk]), {"action": "reset"})
    assert post.recommendations.get(parsed_title="Fade Into You").included is True
    assert post.recommendations.get(parsed_title="in all seriousness").included is False
    assert post.recommendations.get(parsed_title="Space Song").included is False  # bulk exclude was a human act on the manual row


def test_exports(client, post):
    csv = client.get(reverse("exports:download", args=[post.pk, "csv"])).content.decode()
    assert csv.splitlines()[0].startswith("artist,title") and "Mazzy Star,Fade Into You" in csv
    assert "in all seriousness" not in csv
    m3u = client.get(reverse("exports:download", args=[post.pk, "m3u"]) + "?resolve=0").content.decode()
    assert m3u.startswith("#EXTM3U") and "#EXTINF:-1,Mazzy Star - Fade Into You" in m3u
    txt = client.get(reverse("exports:download", args=[post.pk, "txt"]))
    assert txt["Content-Disposition"].endswith('.txt"')
    assert client.get(reverse("exports:download", args=[post.pk, "xml"])).status_code == 404


def test_media_files_served_even_with_debug_false(client, post, settings):
    """Regression: django.conf.urls.static.static() is a no-op when DEBUG=False, which
    would 404 every cached post image in production. See config/urls.py.

    MEDIA_ROOT isn't overridden here because the media URL pattern's document_root is
    resolved once when the urlconf loads, before any per-test settings override could
    take effect -- so this writes into (and cleans up from) the real configured root.
    """
    settings.DEBUG = False
    from vibes.models import PostImage

    image = PostImage.objects.create(post=post, order=0, source_url="https://example.test/x.jpg")
    image.thumb.save("regression_test.jpg", ContentFile(b"fake-jpeg-bytes"), save=True)
    try:
        r = client.get(image.thumb.url)
        assert r.status_code == 200 and b"".join(r.streaming_content) == b"fake-jpeg-bytes"
    finally:
        # Windows can briefly hold the file handle open past the response finishing
        # (AV/indexer); cleanup is tidiness, not the point of the test.
        with contextlib.suppress(OSError):
            image.thumb.delete(save=False)


def test_rec_row_add_dropdown_matches_configured_services(client, post):
    """Explicit rows here rather than relying on whatever .env this machine happens to
    have -- the dev box's real .env auto-configures some services, which would make this
    test's outcome depend on where it runs."""
    from integrations.models import ServiceConfig

    ServiceConfig.objects.create(service="lidarr", enabled=False)
    r = client.get(post.get_absolute_url())
    assert b"Grab via slskd" not in r.content and b"Add to Lidarr" not in r.content
    # The request above auto-creates (unconfigured) rows for every other service via
    # ServiceConfig.all_services() -- update rather than create for the next step.
    ServiceConfig.objects.update_or_create(service="slskd", defaults={"enabled": True, "url": "http://slskd.test", "api_key": "k"})
    r = client.get(post.get_absolute_url())
    assert b"Grab via slskd" in r.content
    assert b"Add to Radarr" not in r.content  # movies-only button, this is a music post


def test_push_rec_updates_only_that_recommendations_chip(client, post, monkeypatch):
    from integrations import push as push_module
    from integrations.models import ServiceConfig

    ServiceConfig.objects.create(service="slskd", enabled=True, url="http://slskd.test", api_key="k")
    rec = post.recommendations.get(parsed_title="Fade Into You")
    other = post.recommendations.get(parsed_title="in all seriousness")

    class FakeSlskd:
        config = ServiceConfig.get("slskd")

        def push(self, r, options=None):
            return "queued", f"queued {r.parsed_title}"

    monkeypatch.setattr(push_module, "client_for", lambda service: FakeSlskd())
    r = client.post(reverse("integrations:push_rec", args=[rec.pk, "slskd"]), HTTP_HX_REQUEST="true")
    assert r.status_code == 200 and b"slskd: queued" in r.content
    # Regression: push_rec re-renders _rec_row.html directly (not via _rec_context), which
    # must still pass `post` through -- the row's YouTube link depends on post.source.is_music.
    assert b"\xe2\x96\xb6 Play" in r.content
    rec.refresh_from_db()
    other.refresh_from_db()
    assert rec.integration_state["slskd"]["status"] == "queued"
    assert other.integration_state == {}  # the other row on the same post is untouched
    # slskd has no per-item page to link to -- its chip must stay a plain, non-clickable span.
    assert b'<span class="chip ok" title="queued Fade Into You">slskd: queued</span>' in r.content


def test_radarr_exists_chip_links_to_the_movie_in_radarr(client, db, monkeypatch):
    from integrations import push as push_module
    from integrations.models import ServiceConfig

    src = Source.objects.create(subreddit="MoviesThatFeelLike", kind=Source.Kind.MOVIES)
    movie_post = Post.objects.create(source=src, reddit_id="mv2", title="t", permalink="/r/y2/", created_utc=timezone.now())
    rec = Recommendation.objects.create(post=movie_post, parsed_title="Drive", parsed_year=2011, method="title_year", confidence=0.9, included=True, order=0)
    ServiceConfig.objects.create(service="radarr", enabled=True, url="http://radarr.test", api_key="k")

    class FakeRadarr:
        config = ServiceConfig.get("radarr")

        def push(self, r):
            return "exists", f"already in Radarr: {r.parsed_title} ({r.parsed_year})", "http://radarr.test/movie/drive-2011"

    monkeypatch.setattr(push_module, "client_for", lambda service: FakeRadarr())
    r = client.post(reverse("integrations:push_rec", args=[rec.pk, "radarr"]), HTTP_HX_REQUEST="true")
    assert r.status_code == 200
    assert b'<a class="chip ok" href="http://radarr.test/movie/drive-2011" target="_blank" rel="noopener"' in r.content
    assert b"radarr: exists" in r.content
    rec.refresh_from_db()
    assert rec.integration_state["radarr"]["url"] == "http://radarr.test/movie/drive-2011"


def test_movie_row_offers_trailer_embed_only_when_tmdb_configured(client, db):
    from integrations.models import ServiceConfig

    src = Source.objects.create(subreddit="MoviesThatFeelLike", kind=Source.Kind.MOVIES)
    movie_post = Post.objects.create(source=src, reddit_id="mv3", title="t", permalink="/r/y3/", created_utc=timezone.now())
    Recommendation.objects.create(post=movie_post, parsed_title="Drive", parsed_year=2011, method="title_year", confidence=0.9, included=True, order=0)

    r = client.get(movie_post.get_absolute_url())
    assert b"watch trailer here" not in r.content  # not configured yet -- no toggle offered

    # update_or_create, not create: the page load above already auto-created (unconfigured)
    # rows for every service via ServiceConfig.all_services().
    ServiceConfig.objects.update_or_create(service="tmdb", defaults={"enabled": True, "url": "https://api.themoviedb.org", "api_key": "k"})
    r = client.get(movie_post.get_absolute_url())
    assert b"watch trailer here" in r.content


def test_rec_trailer_embeds_the_tmdb_match_or_falls_back_gracefully(client, db, monkeypatch):
    from integrations.clients import tmdb as tmdb_module
    from integrations.models import ServiceConfig

    src = Source.objects.create(subreddit="MoviesThatFeelLike", kind=Source.Kind.MOVIES)
    movie_post = Post.objects.create(source=src, reddit_id="mv4", title="t", permalink="/r/y4/", created_utc=timezone.now())
    rec = Recommendation.objects.create(post=movie_post, parsed_title="Drive", parsed_year=2011, method="title_year", confidence=0.9, included=True, order=0)

    # Not configured at all -- graceful fallback message, no crash.
    r = client.get(reverse("vibes:rec_trailer", args=[rec.pk]))
    assert b"No trailer found" in r.content

    # update_or_create: the request above already auto-created an (unconfigured) row.
    ServiceConfig.objects.update_or_create(service="tmdb", defaults={"enabled": True, "url": "https://api.themoviedb.org", "api_key": "k"})

    class FakeTmdb:
        def __init__(self, config):
            pass

        def best_trailer(self, title, year=None):
            return {"key": "abc123", "name": "Drive Official Trailer"}

    monkeypatch.setattr(tmdb_module, "TmdbClient", FakeTmdb)
    r = client.get(reverse("vibes:rec_trailer", args=[rec.pk]))
    assert b'src="https://www.youtube.com/embed/abc123"' in r.content
    # Some studios disable embedding for their trailer uploads (YouTube "error 153") --
    # a direct watch link must always be offered too, not just the iframe.
    assert b'href="https://www.youtube.com/watch?v=abc123"' in r.content


# -- recommendation detail pages ------------------------------------------------------


def _movie_rec(db, title="Big Fish", year=2003, reddit_id="rd1"):
    src = Source.objects.get_or_create(subreddit="MoviesThatFeelLike", defaults={"kind": Source.Kind.MOVIES})[0]
    p = Post.objects.create(source=src, reddit_id=reddit_id, title="Films that feel like a tall tale", permalink="/r/m/", created_utc=timezone.now())
    return Recommendation.objects.create(post=p, parsed_title=title, parsed_year=year, method="title_year", confidence=0.93, included=True, order=0, comment_author="Mysterio221B", raw_text="Big Fish (2003), obviously")


def _configure(service, **fields):
    from integrations.models import ServiceConfig

    defaults = {"enabled": True, "url": f"http://{service}.test", "api_key": "k"}
    defaults.update(fields)
    return ServiceConfig.objects.update_or_create(service=service, defaults=defaults)[0]


SHAPED_MOVIE = {
    "tmdb_id": 587, "title": "Big Fish", "original_title": "", "tagline": "An adventure as big as life itself.", "overview": "Edward Bloom...",
    "year": "2003", "release_date": "2003-12-10", "runtime": 125, "genres": ["Adventure"], "rating": 7.7, "votes": 7939, "certification": "PG (GB)",
    "status": "Released", "budget": 70000000, "revenue": 0, "companies": ["Columbia Pictures"], "countries": [], "languages": ["English"], "keywords": ["witch"],
    "poster": "https://image.tmdb.org/t/p/w500/p.jpg", "backdrop": "https://image.tmdb.org/t/p/w1280/b.jpg",
    "trailer": {"key": "tr1", "name": "Official Trailer"}, "other_videos": [{"key": "c1", "name": "A clip", "type": "Clip"}],
    "cast": [{"name": "Ewan McGregor", "character": "Ed Bloom", "photo": "https://image.tmdb.org/t/p/w185/e.jpg"}],
    "crew": [("Director", "Tim Burton")], "similar": [], "homepage": "", "imdb_url": "https://www.imdb.com/title/tt0319061/", "tmdb_url": "https://www.themoviedb.org/movie/587",
}


def test_rec_detail_movie_page_is_built_from_tmdb(client, db, monkeypatch):
    from integrations import enrich

    rec = _movie_rec(db)
    _configure("tmdb", url="https://api.themoviedb.org")
    _configure("radarr")
    _configure("jellyfin", enabled=False)  # the dev box's real .env may otherwise auto-seed it

    class FakeTmdb:
        def __init__(self, config):
            pass

        def movie_for(self, title, year=None):
            assert (title, year) == ("Big Fish", 2003)
            return SHAPED_MOVIE

    monkeypatch.setattr(enrich, "TmdbClient", FakeTmdb)
    r = client.get(reverse("vibes:rec_detail", args=[rec.pk]))
    html = r.content.decode()
    assert r.status_code == 200
    assert "<h1>Big Fish <small>(2003)</small></h1>" in html and "An adventure as big as life itself." in html
    assert "Tim Burton" in html and "Ewan McGregor" in html and "PG (GB)" in html
    assert 'src="https://www.youtube.com/embed/tr1"' in html and "watch?v=c1" in html
    assert "Mysterio221B" in html and "Big Fish (2003), obviously" in html  # the "from the post" panel
    # The Radarr card lazy-loads with the TMDB id so it can match exactly, not fuzzily.
    assert f'hx-get="/integrations/rec/{rec.pk}/card/radarr/?tmdb_id=587" hx-trigger="load"' in html
    assert "card/jellyfin/" not in html  # Jellyfin isn't configured in this test


def test_rec_detail_movie_page_degrades_when_tmdb_is_unconfigured_or_has_no_match(client, db, monkeypatch):
    from integrations import enrich

    rec = _movie_rec(db)
    r = client.get(reverse("vibes:rec_detail", args=[rec.pk]))
    assert r.status_code == 200 and b"Add a TheMovieDB read token" in r.content

    _configure("tmdb", url="https://api.themoviedb.org")

    class NoMatch:
        def __init__(self, config):
            pass

        def movie_for(self, title, year=None):
            return None

    monkeypatch.setattr(enrich, "TmdbClient", NoMatch)
    r = client.get(reverse("vibes:rec_detail", args=[rec.pk]))
    assert r.status_code == 200 and b"no confident match" in r.content


def test_rec_detail_music_page_is_built_from_musicbrainz_and_lastfm(client, post, monkeypatch):
    from integrations import enrich

    rec = post.recommendations.get(parsed_title="Fade Into You")
    _configure("lastfm", url="https://ws.audioscrobbler.com")
    _configure("lidarr")

    class FakeMB:
        def search_recording(self, artist, title):
            return {"mbid": "rec-1", "title": "Fade Into You", "artist": "Mazzy Star", "artist_mbid": "art-1", "length": "4:55",
                    "album": "So Tonight That I Might See", "album_year": "1993", "album_type": "Album", "release_group_mbid": "rg-1", "release_mbid": "rel-1",
                    "releases": [{"title": "So Tonight That I Might See", "year": "1993", "type": "Album", "country": "US", "url": "https://musicbrainz.org/release/rel-1"}],
                    "release_count": 1, "isrcs": [], "url": "https://musicbrainz.org/recording/rec-1"}

    class FakeLastfm:
        def __init__(self, config):
            pass

        def track_info(self, artist, track):
            return {"name": track, "artist": artist, "url": "https://www.last.fm/x", "listeners": 1234567, "playcount": 9999999, "length": "4:55",
                    "album": "So Tonight That I Might See", "album_url": "", "album_image": "", "tags": ["dream pop"], "wiki": "A hazy classic."}

    monkeypatch.setattr(enrich, "MusicBrainzClient", FakeMB)
    monkeypatch.setattr(enrich, "LastfmClient", FakeLastfm)
    r = client.get(reverse("vibes:rec_detail", args=[rec.pk]))
    html = r.content.decode()
    assert r.status_code == 200
    assert "<h1>Fade Into You <small>by Mazzy Star</small></h1>" in html
    assert "from <b>So Tonight That I Might See</b> (1993)" in html and "1,234,567 listeners" in html and "A hazy classic." in html
    assert 'src="https://coverartarchive.org/release-group/rg-1/front-500"' in html
    assert f'hx-get="/integrations/rec/{rec.pk}/artist/?mbid=art-1"' in html  # artist section lazy-loads with the MBID
    assert f'hx-get="/integrations/rec/{rec.pk}/card/lidarr/?mbid=art-1&album=So%20Tonight%20That%20I%20Might%20See"' in html
    assert "open.spotify.com/search/" in html and "youtube.com/results" in html


def test_rec_detail_music_page_corrects_a_reversed_rec_and_says_so(client, post, monkeypatch):
    from integrations import enrich
    from vibes.models import KnownArtist

    rec = post.recommendations.get(parsed_title="Fade Into You")
    rec.parsed_artist, rec.parsed_title = "Fade Into You", "Mazzy Star"     # the comment said "Fade Into You - Mazzy Star"
    rec.save()
    _configure("lastfm", enabled=False)
    _configure("lidarr", enabled=False)
    asked = []

    class FakeMB:
        def search_recording(self, artist, title):
            asked.append((artist, title))
            if artist == "Mazzy Star":
                return {"mbid": "rec-1", "title": "Fade Into You", "artist": "Mazzy Star", "artist_mbid": "art-1", "length": "4:55", "album": "",
                        "album_year": "", "album_type": "", "release_group_mbid": "", "release_mbid": "", "releases": [], "release_count": 0,
                        "isrcs": [], "url": "https://musicbrainz.org/recording/rec-1"}
            return None

    monkeypatch.setattr(enrich, "MusicBrainzClient", FakeMB)
    r = client.get(reverse("vibes:rec_detail", args=[rec.pk]))
    html = r.content.decode()
    assert r.status_code == 200 and asked == [("Fade Into You", "Mazzy Star"), ("Mazzy Star", "Fade Into You")]
    assert "<h1>Fade Into You <small>by Mazzy Star</small></h1>" in html
    assert "The comment had this the other way round" in html
    rec.refresh_from_db()
    assert (rec.parsed_artist, rec.parsed_title) == ("Mazzy Star", "Fade Into You") and rec.verified_at is not None
    assert "mazzy star" in KnownArtist.norms()
    # Second visit: already verified, MusicBrainz is asked once and nothing is rewritten.
    asked.clear()
    r = client.get(reverse("vibes:rec_detail", args=[rec.pk]))
    assert asked == [("Mazzy Star", "Fade Into You")] and "other way round" not in r.content.decode()


def test_rec_detail_music_page_survives_musicbrainz_being_down(client, post, monkeypatch):
    from integrations import enrich
    from integrations.clients.base import ServiceError

    rec = post.recommendations.get(parsed_title="Fade Into You")

    class DownMB:
        def search_recording(self, artist, title):
            raise ServiceError("MusicBrainz: rate limited (503)")

    monkeypatch.setattr(enrich, "MusicBrainzClient", DownMB)
    r = client.get(reverse("vibes:rec_detail", args=[rec.pk]))
    assert r.status_code == 200 and b"rate limited" in r.content and b"Mazzy Star" in r.content


def test_rec_card_radarr_reports_presence_and_performs_the_add_action(client, db, monkeypatch):
    from integrations import enrich

    rec = _movie_rec(db)
    _configure("radarr")
    pushed = []

    class FakeRadarr:
        def __init__(self, config):
            self.base_url = "http://radarr.test"

        def existing(self, tmdb_id):
            return {"id": 9, "title": "Big Fish", "year": 2003, "titleSlug": "587", "hasFile": True, "monitored": True, "sizeOnDisk": 1503238553,
                    "movieFile": {"quality": {"quality": {"name": "WEBDL-480p"}}}} if pushed else None

        def lookup(self, title, year=None):
            return None, 0.0

    monkeypatch.setattr(enrich, "RadarrClient", FakeRadarr)
    monkeypatch.setattr(enrich, "push_one", lambda rec, service, playlist_name=None: pushed.append(service))

    r = client.get(reverse("integrations:rec_card", args=[rec.pk, "radarr"]) + "?tmdb_id=587")
    assert b"Not in Radarr" in r.content and b'"action": "add"' in r.content and b"?tmdb_id=587" in r.content

    r = client.post(reverse("integrations:rec_card", args=[rec.pk, "radarr"]) + "?tmdb_id=587", {"action": "add"}, HTTP_HX_REQUEST="true")
    assert pushed == ["radarr"]
    assert b'href="http://radarr.test/movie/587"' in r.content and "Downloaded · WEBDL-480p · 1.4 GB".encode() in r.content
    assert b'"action": "add"' not in r.content  # nothing left to add

    assert client.get(reverse("integrations:rec_card", args=[rec.pk, "nope"])).status_code == 400


def test_rec_card_lidarr_matches_by_musicbrainz_id_and_links_the_album(client, post, monkeypatch):
    from integrations import enrich

    rec = post.recommendations.get(parsed_title="Fade Into You")
    _configure("lidarr")

    class FakeLidarr:
        api = "/api/v1"

        def __init__(self, config):
            self.base_url = "http://lidarr.test"

        def artists(self):
            return {"art-1": {"id": 5, "artistName": "Mazzy Star", "foreignArtistId": "art-1", "monitored": True, "statistics": {"trackFileCount": 21, "albumCount": 4, "sizeOnDisk": 0}}}

        def get(self, path, **params):
            assert path == "/api/v1/album" and params == {"artistId": 5}
            return [{"title": "So Tonight That I Might See", "foreignAlbumId": "alb-1", "releaseDate": "1993-10-05", "statistics": {"trackFileCount": 10, "trackCount": 10}},
                    {"title": "Among My Swan", "foreignAlbumId": "alb-2", "releaseDate": "1996-10-29", "statistics": {"trackFileCount": 0, "trackCount": 12}}]

    monkeypatch.setattr(enrich, "LidarrClient", FakeLidarr)
    r = client.get(reverse("integrations:rec_card", args=[rec.pk, "lidarr"]) + "?mbid=art-1&album=So%20Tonight%20That%20I%20Might%20See")
    html = r.content.decode()
    assert 'href="http://lidarr.test/artist/art-1"' in html and "21 tracks on disk · 4 albums · monitored" in html
    assert 'href="http://lidarr.test/album/alb-1"' in html and "So Tonight That I Might See (1993) · 10/10 tracks" in html
    assert '"action": "add"' not in html  # already have every track of that album


def test_rec_slskd_search_lists_candidates_and_download_queues_the_chosen_file(client, post, monkeypatch):
    from integrations import enrich

    rec = post.recommendations.get(parsed_title="Fade Into You")
    _configure("slskd")
    queued = []

    class FakeSlskd:
        def __init__(self, config):
            self.config = config

        def ranked(self, artist, title, options=None, limit=12, retries=0):
            assert retries == 1  # a person is waiting -- worth one retry when Soulseek drops the search
            return [{"username": "flac_hoarder", "file": {"filename": r"Mazzy Star\So Tonight\01 Fade Into You.flac", "size": 31504876, "bitDepth": 16, "sampleRate": 44100, "length": 295}, "score": 4.5, "free_slot": False, "queue": 12}]

        def enqueue(self, username, file):
            queued.append((username, file))

    monkeypatch.setattr(enrich, "SlskdClient", FakeSlskd)
    r = client.post(reverse("integrations:rec_slskd_search", args=[rec.pk]), HTTP_HX_REQUEST="true")
    html = r.content.decode()
    assert "01 Fade Into You.flac" in html and "FLAC · 16-bit/44.1kHz · 4:55 · 30.0 MB" in html and "queue of 12" in html
    assert 'name="filename" value="Mazzy Star\\So Tonight\\01 Fade Into You.flac"' in html  # backslashes survive as a form value

    r = client.post(reverse("integrations:rec_slskd_download", args=[rec.pk]), {"username": "flac_hoarder", "filename": r"Mazzy Star\So Tonight\01 Fade Into You.flac", "size": "31504876"}, HTTP_HX_REQUEST="true")
    assert queued == [("flac_hoarder", {"filename": r"Mazzy Star\So Tonight\01 Fade Into You.flac", "size": 31504876})]
    assert "✓ queued".encode() in r.content
    rec.refresh_from_db()
    assert rec.integration_state["slskd"]["status"] == "queued" and "picked by hand" in rec.integration_state["slskd"]["detail"]


def test_rec_row_title_links_to_its_detail_page(client, post):
    rec = post.recommendations.get(parsed_title="Fade Into You")
    r = client.get(post.get_absolute_url())
    assert f'<a href="/rec/{rec.pk}/"'.encode() in r.content


def test_settings_page_and_encrypted_save(client, db, settings):
    from integrations.models import ServiceConfig

    r = client.get(reverse("integrations:settings"))
    assert r.status_code == 200
    client.post(reverse("integrations:save_service", args=["radarr"]), {"enabled": "on", "url": "http://radarr.test/", "api_key": "sekrit"})
    cfg = ServiceConfig.get("radarr")
    assert cfg.url == "http://radarr.test" and cfg.api_key == "sekrit"
    from django.db import connection

    with connection.cursor() as c:
        c.execute("select api_key from integrations_serviceconfig where service='radarr'")
        stored = c.fetchone()[0]
    assert stored.startswith("enc:") and "sekrit" not in stored
    # Saving again with a blank key keeps the old one
    client.post(reverse("integrations:save_service", args=["radarr"]), {"enabled": "on", "url": "http://radarr.test/", "api_key": ""})
    assert ServiceConfig.get("radarr").api_key == "sekrit"
