"""Sync pipeline against a fake Reddit client -- no network."""

import pytest

from reddit_sync.models import SyncRun
from reddit_sync.sync import Syncer
from vibes.models import Post, Recommendation, Source


class FakeClient:
    requests_made = 0

    def __init__(self, posts, comments_by_id):
        self.posts = posts
        self.comments_by_id = comments_by_id

    def listing(self, subreddit, sort="hot", limit=50, time_filter=None):
        self.requests_made += 1
        return self.posts

    def post_with_comments(self, subreddit, post_id, limit=500):
        self.requests_made += 1
        return {}, self.comments_by_id.get(post_id, [])


def gallery_post(pid, title, num_comments=5):
    return {
        "id": pid, "title": title, "author": "someone", "permalink": f"/r/MoviesThatFeelLike/comments/{pid}/x/",
        "url": f"https://www.reddit.com/gallery/{pid}", "score": 42, "num_comments": num_comments,
        "created_utc": 1789600000, "is_gallery": True,
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
