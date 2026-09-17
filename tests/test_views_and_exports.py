import pytest
from django.urls import reverse
from django.utils import timezone

from vibes.models import Post, Recommendation, Source


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


def test_unknown_section_404(client, db):
    assert client.get("/nope/").status_code == 404


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
