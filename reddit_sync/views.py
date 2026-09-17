from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .models import SyncRun
from .sync import start_background_sync


def status_pill(request):
    """Tiny status indicator in the top bar, polled by htmx."""
    run = SyncRun.objects.first()
    return render(request, "reddit_sync/_pill.html", {"run": run, "cooldown": SyncRun.cooldown_until()})


def status_panel(request):
    """Settings-page panel: last run stats + log, polled while a run is in flight."""
    runs = list(SyncRun.objects.all()[:8])
    return render(request, "reddit_sync/_status.html", {"runs": runs, "run": runs[0] if runs else None, "cooldown": SyncRun.cooldown_until()})


@require_POST
def start(request):
    max_comments = request.POST.get("max_comments", "40")
    backfill = request.POST.get("backfill", "0")
    run = start_background_sync(
        max_comment_fetches=int(max_comments) if max_comments.isdigit() else 40,
        backfill_pages=int(backfill) if backfill.isdigit() else 0,
    )
    if run is None:
        until = SyncRun.cooldown_until()
        if until:
            messages.error(request, f"Reddit blocked the last run; cooling down until {until:%H:%M}.")
        else:
            messages.info(request, "A sync is already running.")
    else:
        messages.success(request, "Sync started in the background.")
    return redirect("integrations:settings")
