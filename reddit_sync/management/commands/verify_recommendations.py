"""Settle "which half is the artist" for music recommendations using MusicBrainz, and
keep the parser's KnownArtist table fed. Runs from the sync sidecar after every sync
(see docker/sync-loop.sh) and by hand:

    manage.py verify_recommendations --limit 500     # work through the backlog
    manage.py verify_recommendations --seed-only     # just pull artist names from Lidarr

MusicBrainz allows ~1 request/s per IP, shared with the web container, so this paces
itself at 2 s and a pass over N rows takes up to 4N seconds -- the default --limit keeps
one pass well inside a sync interval."""

from django.core.management.base import BaseCommand

from integrations.clients.base import ServiceError
from integrations.clients.lidarr import LidarrClient
from integrations.clients.musicbrainz import MusicBrainzClient
from integrations.enrich import resolve_recording
from integrations.models import ServiceConfig
from vibes.models import KnownArtist, Recommendation


class Command(BaseCommand):
    help = "Confirm music recommendations against MusicBrainz, swapping artist/title where the comment had them reversed."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100, help="Rows to verify this run (default 100).")
        parser.add_argument("--seed-only", action="store_true", help="Only refresh KnownArtist from Lidarr; verify nothing.")
        parser.add_argument("--recheck", action="store_true", help="Include rows already verified (newest first).")

    def handle(self, *args, **opts):
        self.seed_from_lidarr()
        if opts["seed_only"]:
            return
        rows = (
            Recommendation.objects.filter(post__source__kind="music")
            .exclude(parsed_title="").exclude(parsed_artist="")
            .select_related("post")
            .order_by("-post__created_utc", "order")
        )
        if not opts["recheck"]:
            rows = rows.filter(verified_at__isnull=True)
        rows = list(rows[: opts["limit"]])
        if not rows:
            self.stdout.write("Nothing to verify.")
            return
        mb = MusicBrainzClient(min_interval=2.0)   # leave room for the web container's own lookups
        found = swapped = missing = 0
        for rec in rows:
            before = rec.display_label
            try:
                recording, did_swap = resolve_recording(rec, mb)
            except ServiceError as exc:
                # Network / rate-limit trouble: stop here, leave the rest unverified for next time.
                self.stderr.write(f"MusicBrainz unavailable ({exc}); verified {found + missing} of {len(rows)} before stopping")
                break
            if recording is None:
                missing += 1
                continue
            found += 1
            if did_swap:
                swapped += 1
                self.stdout.write(f"  swapped: {before}  ->  {rec.display_label}")
        self.stdout.write(self.style.SUCCESS(
            f"Verified {found + missing} recommendations: {found} matched ({swapped} swapped), {missing} unknown to MusicBrainz; "
            f"{Recommendation.objects.filter(post__source__kind='music', verified_at__isnull=True).exclude(parsed_title='').exclude(parsed_artist='').count()} still waiting"
        ))

    def seed_from_lidarr(self):
        config = ServiceConfig.get(ServiceConfig.Service.LIDARR)
        if not config.is_configured:
            return
        try:
            names = [a.get("artistName") for a in LidarrClient(config).artists().values()]
        except ServiceError as exc:
            self.stderr.write(f"Lidarr unavailable ({exc}); KnownArtist not refreshed")
            return
        new = KnownArtist.learn(names, KnownArtist.Source.LIDARR)
        self.stdout.write(f"KnownArtist: {len(names)} Lidarr artists, {new} new (table now {KnownArtist.objects.count()})")
