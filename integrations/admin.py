from django.contrib import admin

from .models import ServiceConfig


@admin.register(ServiceConfig)
class ServiceConfigAdmin(admin.ModelAdmin):
    list_display = ("service", "enabled", "url", "username", "last_test_ok", "last_test_at", "updated_at")
    # Secrets are edited on the app's own Settings page; keep them out of admin forms so
    # they never render decrypted in a browser by accident.
    exclude = ("api_key", "password")
