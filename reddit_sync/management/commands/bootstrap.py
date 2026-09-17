"""First-run setup: create the two default sources and seed service configs from .env.
Idempotent -- safe to run on every container start."""

from django.core.management.base import BaseCommand

from integrations.models import ServiceConfig
from vibes.models import Source

DEFAULT_SOURCES = [
    {"subreddit": "MoviesThatFeelLike", "kind": Source.Kind.MOVIES},
    {"subreddit": "SongsThatFeelLikeThis", "kind": Source.Kind.MUSIC},
]


class Command(BaseCommand):
    help = "Create default sources and seed service settings from the environment (idempotent)."

    def handle(self, *args, **opts):
        for spec in DEFAULT_SOURCES:
            src, created = Source.objects.get_or_create(subreddit=spec["subreddit"], defaults={"kind": spec["kind"]})
            self.stdout.write(f"source r/{src.subreddit}: {'created' if created else 'exists'}")
        for cfg in ServiceConfig.all_services():
            self.stdout.write(f"service {cfg.service}: {'configured' if cfg.is_configured else 'not configured'}")
