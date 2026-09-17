from django.contrib import admin

from .models import SyncRun


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "finished_at", "status", "trigger", "sources", "posts_seen", "posts_new", "comments_fetched", "recommendations_created", "requests_made", "errors")
    list_filter = ("status", "trigger")
    readonly_fields = [f.name for f in SyncRun._meta.fields]
