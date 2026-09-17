"""Re-run the comment parser over stored comments (no network). Use after improving
the heuristics in reddit_sync/parser.py."""

from django.core.management.base import BaseCommand

from reddit_sync.models import SyncRun
from reddit_sync.sync import Syncer
from vibes.models import Post


class Command(BaseCommand):
    help = "Re-parse recommendations from the comments already stored on each post."

    def add_arguments(self, parser):
        parser.add_argument("--post", help="Reddit id of a single post.")
        parser.add_argument("--kind", choices=["movies", "music"])

    def handle(self, *args, **opts):
        posts = Post.objects.exclude(comments=[]).select_related("source")
        if opts["post"]:
            posts = posts.filter(reddit_id=opts["post"])
        if opts["kind"]:
            posts = posts.filter(source__kind=opts["kind"])
        run = SyncRun.objects.create(trigger=SyncRun.Trigger.CLI, sources="reparse")
        syncer = Syncer(run, client=_NoClient(), cache_images=False)
        total = 0
        for post in posts:
            created = syncer.rebuild_recommendations(post)
            post.num_comments = max(post.num_comments, len(post.comments))
            post.save(update_fields=["parsed_at", "num_comments"])
            total += created
            self.stdout.write(f"{post.reddit_id}: {post.recommendations.count()} recs ({created} new)")
        run.status = SyncRun.Status.OK
        run.recommendations_created = total
        run.save()
        self.stdout.write(self.style.SUCCESS(f"Reparsed {posts.count()} posts, {total} new recommendations"))


class _NoClient:
    requests_made = 0
