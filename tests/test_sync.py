"""Sync pipeline against a fake Reddit client -- no network."""

import pytest

from reddit_sync.models import SyncRun
from reddit_sync.sync import Syncer
from vibes.models import Post, Recommendation, Source


class FakeClient:
    requests_made = 0
    supports_backfill = False

    def __init__(self, posts, comments_by_id, pages=None):
        self.posts = posts
        self.comments_by_id = comments_by_id
        self.pages = pages or []  # extra pages served in order when `before` is passed

    def listing(self, subreddit, sort="hot", limit=50, time_filter=None, before=None):
        self.requests_made += 1
        if before is None:
            return self.posts
        return self.pages.pop(0) if self.pages else []

    def post_with_comments(self, subreddit, post_id, limit=500):
        self.requests_made += 1
        return {}, self.comments_by_id.get(post_id, [])


def gallery_post(pid, title, num_comments=5, created_utc=1789600000):
    return {
        "id": pid, "title": title, "author": "someone", "permalink": f"/r/MoviesThatFeelLike/comments/{pid}/x/",
        "url": f"https://www.reddit.com/gallery/{pid}", "score": 42, "num_comments": num_comments,
        "created_utc": created_utc, "is_gallery": True,
        "media_metadata": {"abc": {"status": "valid", "s": {"u": "https://preview.redd.it/abc.jpg?width=100&amp;s=1", "x": 100, "y": 80}}},
        "gallery_data": {"items": [{"media_id": "abc"}]},
    }


def comment(cid, body, score=1, depth=0, author="u1"):
    return {"id": cid, "author": author, "body": body, "score": score, "depth": depth, "permalink": f"/c/{cid}/"}


@pytest.fixture
def source(db):
    return Source.objects.create(subreddit="MoviesThatFeelLike", kind=Source.Kind.MOVIES)


def run_sync(source, client, **kwargs):
    run = SyncRun.objects.create()
    Syncer(run, client=client, cache_images=False, **kwargs).run_sources([source])
    return run


def test_sync_creates_posts_images_and_recommendations(source):
    client = FakeClient(
        [gallery_post("p1", "Movies that feel like this")],
        {"p1": [comment("c1", "Drive (2011)", score=10), comment("c2", "Definitely Nightcrawler"), comment("c3", "Drive", author="u2"),
                comment("c4", "[removed]"), comment("c5", "what movie is this from?"),
                comment("c6", "Bot notice", author="AutoModerator")]},
    )
    run = run_sync(source, client)
    assert run.status == SyncRun.Status.OK
    assert run.posts_new == 1 and run.comments_fetched == 1 and run.requests_made == 2
    post = Post.objects.get(reddit_id="p1")
    assert post.images.count() == 1
    assert post.images.first().source_url == "https://preview.redd.it/abc.jpg?width=100&s=1"  # &amp; unescaped
    labels = {r.display_label: r for r in post.recommendations.all()}
    assert "Drive (2011)" in labels and labels["Drive (2011)"].mention_count == 2
    assert "Nightcrawler" in labels
    assert not any("what movie" in k for k in labels)
    assert post.comments_fetched_at is not None and post.num_comments == 6  # listing said 5, we saw 6


def test_resync_preserves_human_edits_and_replaces_the_rest(source):
    client = FakeClient([gallery_post("p1", "t")], {"p1": [comment("c1", "Drive (2011)"), comment("c2", "Heat")]})
    run_sync(source, client)
    post = Post.objects.get(reddit_id="p1")
    heat = post.recommendations.get(parsed_title="Heat")
    heat.included = False
    heat.edited = True
    heat.save()
    manual = Recommendation.objects.create(post=post, parsed_title="Collateral", method="manual", confidence=1, edited=True)

    # Comments changed: "Heat" gone, new one appears. Force a refetch.
    client.comments_by_id["p1"] = [comment("c1", "Drive (2011)"), comment("c9", "Thief (1981)")]
    run_sync(source, client, refresh=True)
    post.refresh_from_db()
    titles = set(post.recommendations.values_list("parsed_title", flat=True))
    assert titles == {"Drive", "Heat", "Collateral", "Thief"}
    heat.refresh_from_db()
    assert heat.included is False  # human decision survived
    assert Recommendation.objects.filter(pk=manual.pk).exists()


def test_reparse_adopts_the_orientation_once_the_artist_is_known(db):
    from vibes.models import KnownArtist

    music = Source.objects.create(subreddit="SongsThatFeelLikeThis", kind=Source.Kind.MUSIC)
    client = FakeClient([gallery_post("m1", "t")], {"m1": [comment("c1", "All I wanna do - Sheryl crow\nBreathe - Michelle branch")]})
    run_sync(music, client)
    post = Post.objects.get(reddit_id="m1")
    rec = post.recommendations.get(parsed_title="Sheryl crow")   # nothing to go on yet: stored as written
    pushed = post.recommendations.get(parsed_title="Michelle branch")
    pushed.integration_state = {"lidarr": {"status": "added"}}
    pushed.save()

    KnownArtist.learn(["Sheryl Crow"], KnownArtist.Source.LIDARR)
    run_sync(music, client, refresh=True)
    rec.refresh_from_db()
    pushed.refresh_from_db()
    assert (rec.parsed_artist, rec.parsed_title) == ("Sheryl crow", "All I wanna do")   # same row, corrected in place
    assert post.recommendations.count() == 2                                              # not duplicated
    assert (pushed.parsed_artist, pushed.parsed_title) == ("Breathe", "Michelle branch")  # already sent to a service: left alone


def test_comment_cap_is_split_across_sources(db):
    a = Source.objects.create(subreddit="A", kind="movies")
    b = Source.objects.create(subreddit="B", kind="music")
    posts = [gallery_post(f"p{i}", f"post {i}") for i in range(6)]
    client = FakeClient(posts, {p["id"]: [comment("c", "Drive (2011)")] for p in posts})
    run = SyncRun.objects.create()
    Syncer(run, client=client, cache_images=False, max_comment_fetches=4).run_sources([a, b])
    assert run.comments_fetched == 4
    assert "cap reached" in run.log


def test_blocked_run_sets_cooldown(source, settings):
    from reddit_sync.client import RedditBlocked

    class Blocked(FakeClient):
        def listing(self, *a, **k):
            self.requests_made += 1
            raise RedditBlocked("429")

    run = run_sync(source, Blocked([], {}))
    assert run.blocked and run.status == SyncRun.Status.PARTIAL
    assert SyncRun.cooldown_until() is not None


def test_backfill_pages_further_back_and_stops_at_the_archive_start(source):
    client = FakeClient(
        [gallery_post("new1", "recent", created_utc=3000)],
        {},
        pages=[
            [gallery_post("old1", "older", created_utc=2000), gallery_post("old2", "older still", created_utc=1000)],
            [],  # archive has nothing before that -- backfill should stop here
        ],
    )
    client.supports_backfill = True
    run = run_sync(source, client, backfill_pages=5, max_comment_fetches=0)
    assert run.posts_new == 3  # 1 from the normal listing + 2 backfilled
    assert set(Post.objects.values_list("reddit_id", flat=True)) == {"new1", "old1", "old2"}
    assert "backfill reached the start of the archive" in run.log
    # Stopped early (2 pages used) rather than exhausting all 5 requested.
    assert client.requests_made == 1 + 2  # 1 normal listing + 2 backfill pages


def test_backfill_stops_when_a_page_has_nothing_new(source):
    dup = gallery_post("new1", "recent", created_utc=3000)
    client = FakeClient([dup], {}, pages=[[dup]])  # "older" page is actually the same post
    client.supports_backfill = True
    run = run_sync(source, client, backfill_pages=5, max_comment_fetches=0)
    assert run.posts_new == 1
    assert client.requests_made == 1 + 1  # didn't keep paging once a page had 0 new posts


def test_backfill_skipped_for_a_backend_that_doesnt_support_it(source):
    client = FakeClient([gallery_post("new1", "recent")], {})  # supports_backfill=False by default
    run = run_sync(source, client, backfill_pages=3, max_comment_fetches=0)
    assert run.posts_new == 1
    assert "backfill skipped" in run.log
    assert client.requests_made == 1  # never attempted a backfill request


class FakeCacher:
    """Marks every pending image as cached without touching the network or disk."""

    def cache(self, image):
        image.file.name = f"posts/{image.pk}.jpg"
        image.save(update_fields=["file"])
        return True


def _disk(free_gb):
    from types import SimpleNamespace

    return lambda path: SimpleNamespace(free=free_gb * 1024**3)


def test_disk_check_looks_at_media_root_not_data_dir(source, settings, monkeypatch):
    """Regression: images can be pointed at separate storage from the sqlite DB via
    MEDIA_DATA_DIR (e.g. an NFS drive with plenty of room, while the LXC's own disk is
    tight). The disk floor has to check wherever images actually get written, not
    wherever DATA_DIR happens to be, or a deployment that split the two would get
    warnings based on the wrong filesystem's free space."""
    settings.DATA_DIR = "/data-is-tight"
    settings.MEDIA_ROOT = "/media-has-room"
    seen_paths = []

    def disk_usage(path):
        from types import SimpleNamespace

        seen_paths.append(str(path))
        return SimpleNamespace(free=20 * 1024**3)

    monkeypatch.setattr("reddit_sync.sync.shutil.disk_usage", disk_usage)
    run = SyncRun.objects.create()
    Syncer(run, client=FakeClient([], {}), cacher=FakeCacher(), cache_images=True).cache_images()
    assert seen_paths == ["/media-has-room"]


def test_image_caching_stops_below_the_disk_floor(source, settings, monkeypatch):
    from django.utils import timezone

    from vibes.models import PostImage

    post = Post.objects.create(source=source, reddit_id="p1", title="t", permalink="/x/", created_utc=timezone.now())
    image = PostImage.objects.create(post=post, order=0, source_url="https://example.test/x.jpg")

    settings.MIN_FREE_DISK_GB = 5.0
    monkeypatch.setattr("reddit_sync.sync.shutil.disk_usage", _disk(1))  # below the 5GB floor

    run = SyncRun.objects.create()
    syncer = Syncer(run, client=FakeClient([], {}), cacher=FakeCacher(), cache_images=True)
    syncer.cache_images()
    image.refresh_from_db()
    assert image.file == ""  # nothing cached
    assert "skipping image caching" in run.log and "1.0GB free" in run.log

    monkeypatch.setattr("reddit_sync.sync.shutil.disk_usage", _disk(20))  # comfortably above it
    syncer.cache_images()
    image.refresh_from_db()
    assert image.file != ""  # now it proceeds normally


def test_disk_floor_is_rechecked_partway_through_a_large_backlog(source, settings, monkeypatch):
    """A single run can be asked to cache up to `limit` images; if the backlog is big
    enough that caching them all would blow through the floor, a run must notice partway
    through rather than only checking once at the start."""
    from django.utils import timezone

    from vibes.models import PostImage

    post = Post.objects.create(source=source, reddit_id="p1", title="t", permalink="/x/", created_utc=timezone.now())
    images = [PostImage.objects.create(post=post, order=i, source_url=f"https://example.test/{i}.jpg") for i in range(45)]

    settings.MIN_FREE_DISK_GB = 5.0
    calls = {"n": 0}

    def disk_usage(path):
        from types import SimpleNamespace

        calls["n"] += 1
        # Plenty of room at first; drops below the floor once the run checks again
        # partway through (cache_images re-checks every 40 successfully cached images).
        return SimpleNamespace(free=(20 if calls["n"] <= 1 else 1) * 1024**3)

    monkeypatch.setattr("reddit_sync.sync.shutil.disk_usage", disk_usage)
    run = SyncRun.objects.create()
    syncer = Syncer(run, client=FakeClient([], {}), cacher=FakeCacher(), cache_images=True)
    syncer.cache_images(limit=45)

    cached = sum(1 for i in images if PostImage.objects.get(pk=i.pk).file)
    assert 0 < cached < 45  # stopped partway through, not all 45
    assert "skipping image caching" in run.log
