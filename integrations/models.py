from django.conf import settings
from django.db import models

from .fields import EncryptedTextField


class ServiceConfig(models.Model):
    """Connection details for one external service. One row per service; the Settings
    page edits these, and the env vars only seed them on first run."""

    class Service(models.TextChoices):
        RADARR = "radarr", "Radarr"
        LIDARR = "lidarr", "Lidarr"
        JELLYFIN = "jellyfin", "Jellyfin"
        NAVIDROME = "navidrome", "Navidrome"

    service = models.CharField(max_length=20, choices=Service.choices, unique=True)
    enabled = models.BooleanField(default=True)
    url = models.URLField(blank=True, help_text="Base URL, e.g. http://192.168.1.110:7878")
    api_key = EncryptedTextField(blank=True)
    username = models.CharField(max_length=100, blank=True)
    password = EncryptedTextField(blank=True)
    # Service-specific knobs chosen on the Settings page (root folder, quality profile,
    # Jellyfin user id, ...). Kept schemaless so adding a knob doesn't need a migration.
    options = models.JSONField(default=dict, blank=True)

    last_test_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True, blank=True)
    last_test_message = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["service"]

    def __str__(self):
        return self.get_service_display()

    @property
    def is_configured(self):
        if not (self.enabled and self.url):
            return False
        if self.service == self.Service.NAVIDROME:
            return bool(self.username and self.password)
        return bool(self.api_key)

    @property
    def uses_api_key(self):
        return self.service != self.Service.NAVIDROME

    @property
    def kind(self):
        """Which section this service acts on."""
        return "movies" if self.service in (self.Service.RADARR,) else (
            "music" if self.service in (self.Service.LIDARR, self.Service.NAVIDROME) else "both"
        )

    @classmethod
    def get(cls, service):
        """Fetch the row for a service, creating it (seeded from env) if missing."""
        obj, created = cls.objects.get_or_create(service=service)
        if created:
            defaults = settings.SERVICE_ENV_DEFAULTS.get(service, {})
            changed = False
            for field in ("url", "api_key", "username", "password"):
                if defaults.get(field) and not getattr(obj, field):
                    setattr(obj, field, defaults[field])
                    changed = True
            if changed:
                obj.save()
        return obj

    @classmethod
    def all_services(cls):
        return [cls.get(key) for key, _ in cls.Service.choices]

    @classmethod
    def configured(cls):
        """{service: config} for every service that's usable right now."""
        return {c.service: c for c in cls.all_services() if c.is_configured}
