"""
Cron entry point. Typical crontab on the services LXC:

    */30 * * * *  cd /opt/mediathatfeelslike && docker compose exec -T app python manage.py sync_reddit >> /var/log/mtfl-sync.log 2>&1
"""

from django.core.management.base import BaseCommand, CommandError

from reddit_sync.models import SyncRun
from reddit_sync.sync import Syncer
from vibes.models import Source


class Command(BaseCommand):
    help = "Fetch new posts from the configured subreddits and parse their comments into recommendations."

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", help="Subreddit name (repeatable). Default: all enabled sources.")
        parser.add_argument("--kind", choices=["movies", "music"], help="Only sources of this kind.")
        parser.add_argument("--limit", type=int, help="Override each source's fetch_limit.")
        parser.add_argument("--max-comments", type=int, default=40, help="Cap on comment-thread fetches this run (default 40).")
        parser.add_argument("--refresh", action="store_true", help="Re-fetch comments for every post in the listing, not just new/grown ones.")
        parser.add_argument("--no-images", action="store_true", help="Skip downloading/caching images.")
        parser.add_argument("--force", action="store_true", help="Ignore an apparently running sync.")
        parser.add_argument(
            "--backfill", type=int, default=0, metavar="N",
            help="Page N pages further back into each source's history (archive backend only). "
            "Comment fetches for what this pulls in still go through --max-comments, so a big "
            "backfill just queues up over several runs rather than all at once.",
        )

    def handle(self, *args, **opts):
        running = SyncRun.running()
        if running and not opts["force"]:
            raise CommandError(f"A sync is already running (started {running.started_at:%H:%M:%S}). Use --force to override.")

        until = SyncRun.cooldown_until()
        if until and not opts["force"]:
            raise CommandError(f"Reddit blocked the last run; cooling down until {until:%H:%M:%S}. Use --force to override.")

        sources = Source.objects.filter(enabled=True)
        if opts["source"]:
            sources = Source.objects.filter(subreddit__in=opts["source"])
        if opts["kind"]:
            sources = sources.filter(kind=opts["kind"])
        sources = list(sources)
        if not sources:
            raise CommandError("No matching sources. Run `manage.py bootstrap` or add one in admin.")

        run = SyncRun.objects.create(trigger=SyncRun.Trigger.CLI)
        syncer = Syncer(
            run, max_comment_fetches=opts["max_comments"], refresh=opts["refresh"],
            cache_images=not opts["no_images"], backfill_pages=opts["backfill"],
        )
        if opts["limit"]:
            for s in sources:
                s.fetch_limit = opts["limit"]
        syncer.run_sources(sources)
        self.stdout.write(run.log)
        if run.status == SyncRun.Status.FAILED:
            raise CommandError("Sync failed -- see log above.")
