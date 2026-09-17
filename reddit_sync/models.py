from django.conf import settings
from django.db import models
from django.utils import timezone


class SyncRun(models.Model):
    """Audit trail for each sync -- what was fetched, how many requests it cost, and
    anything that went wrong. Also doubles as the lock that stops two syncs overlapping."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        OK = "ok", "OK"
        PARTIAL = "partial", "Finished with errors"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        CLI = "cli", "Command line / cron"
        UI = "ui", "Settings page"

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.CLI)
    sources = models.CharField(max_length=200, blank=True)
    posts_seen = models.IntegerField(default=0)
    posts_new = models.IntegerField(default=0)
    comments_fetched = models.IntegerField(default=0)
    images_cached = models.IntegerField(default=0)
    recommendations_created = models.IntegerField(default=0)
    requests_made = models.IntegerField(default=0)
    errors = models.IntegerField(default=0)
    blocked = models.BooleanField(default=False, help_text="Reddit refused us (rate limit / interstitial) during this run")
    log = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Sync {self.started_at:%Y-%m-%d %H:%M} ({self.status})"

    @property
    def is_running(self):
        return self.status == self.Status.RUNNING

    @property
    def duration(self):
        end = self.finished_at or timezone.now()
        return end - self.started_at

    def append_log(self, line):
        self.log = (self.log + "\n" if self.log else "") + line
        self.save(update_fields=["log"])

    @classmethod
    def running(cls):
        """The current in-flight run, if any. Runs older than 2h are treated as stale
        (crashed process) so a dead run can't block syncing forever."""
        cutoff = timezone.now() - timezone.timedelta(hours=2)
        for run in cls.objects.filter(status=cls.Status.RUNNING, started_at__lt=cutoff):
            run.status = cls.Status.FAILED
            run.finished_at = timezone.now()
            run.log = (run.log + "\n" if run.log else "") + "Marked failed: exceeded 2h without finishing."
            run.save()
        return cls.objects.filter(status=cls.Status.RUNNING).first()

    @classmethod
    def cooldown_until(cls):
        """When the next sync is allowed to start, or None if not in a cooldown. Each
        consecutive blocked run doubles the wait so cron can't keep poking a blocked IP."""
        recent = list(cls.objects.exclude(status=cls.Status.RUNNING).order_by("-started_at")[:6])
        streak = 0
        for run in recent:
            if run.blocked:
                streak += 1
            else:
                break
        if not streak:
            return None
        wait = min(settings.REDDIT_BLOCK_COOLDOWN * (2 ** (streak - 1)), settings.REDDIT_BLOCK_COOLDOWN_MAX)
        until = (recent[0].finished_at or recent[0].started_at) + timezone.timedelta(seconds=wait)
        return until if until > timezone.now() else None
