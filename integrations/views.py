from django import forms
from django.contrib import messages
from django.forms import modelformset_factory
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from reddit_sync.models import SyncRun
from vibes.models import Post, Recommendation, Source

from .clients.base import ServiceError
from .models import ServiceConfig, service_flags
from .push import CLIENTS, push_one, push_post

SourceFormSet = modelformset_factory(
    Source, fields=("subreddit", "kind", "listing", "time_filter", "fetch_limit", "enabled"), extra=1, can_delete=True,
)


class ServiceForm(forms.Form):
    enabled = forms.BooleanField(required=False)
    url = forms.URLField(required=False, assume_scheme="http")
    api_key = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False))
    username = forms.CharField(required=False)
    password = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False))
    # service-specific knobs live in options{}
    root_folder = forms.CharField(required=False)
    quality_profile_id = forms.CharField(required=False)
    metadata_profile_id = forms.CharField(required=False)
    minimum_availability = forms.CharField(required=False)
    artist_monitor = forms.CharField(required=False)
    user_id = forms.CharField(required=False)
    search_on_add = forms.BooleanField(required=False)
    max_wait = forms.IntegerField(required=False, min_value=5, max_value=30)
    auto_download = forms.BooleanField(required=False)

    OPTION_FIELDS = ("root_folder", "quality_profile_id", "metadata_profile_id", "minimum_availability", "artist_monitor", "user_id", "max_wait")

    def apply(self, config):
        d = self.cleaned_data
        config.enabled = d["enabled"]
        config.url = d["url"].rstrip("/")
        if d["api_key"]:
            config.api_key = d["api_key"]
        config.username = d["username"]
        if d["password"]:
            config.password = d["password"]
        opts = dict(config.options or {})
        for key in self.OPTION_FIELDS:
            if d.get(key):
                opts[key] = d[key]
        opts["search_on_add"] = d["search_on_add"]
        opts["auto_download"] = d["auto_download"]
        config.options = opts
        config.save()


def settings_page(request):
    context = {
        "settings_page": True,
        "services": ServiceConfig.all_services(),
        "formset": SourceFormSet(queryset=Source.objects.all()),
        "runs": list(SyncRun.objects.all()[:8]),
        "cooldown": SyncRun.cooldown_until(),
    }
    context["run"] = context["runs"][0] if context["runs"] else None
    return render(request, "integrations/settings.html", context)


@require_POST
def save_service(request, service):
    config = ServiceConfig.get(service)
    form = ServiceForm(request.POST)
    if form.is_valid():
        form.apply(config)
        messages.success(request, f"{config.get_service_display()} settings saved.")
        if "test" in request.POST:
            _run_test(config)
            messages.info(request, f"{config.get_service_display()}: {config.last_test_message}")
    else:
        messages.error(request, f"{config.get_service_display()}: {form.errors.as_text()}")
    return redirect("integrations:settings")


def _run_test(config):
    try:
        client = CLIENTS[config.service](config)
        message = client.test()
        client.refresh_choices()
        config.last_test_ok, config.last_test_message = True, message
    except ServiceError as exc:
        config.last_test_ok, config.last_test_message = False, str(exc)
    except Exception as exc:  # a client bug shouldn't 500 the settings page
        config.last_test_ok, config.last_test_message = False, f"{exc.__class__.__name__}: {exc}"
    config.last_test_at = timezone.now()
    config.save(update_fields=["last_test_ok", "last_test_message", "last_test_at", "options"])
    return config


@require_POST
def test_service(request, service):
    """htmx: test the saved settings and return the refreshed service card."""
    config = _run_test(ServiceConfig.get(service))
    return render(request, "integrations/_service_card.html", {"svc": config, "settings_page": True})


@require_POST
def save_sources(request):
    formset = SourceFormSet(request.POST, queryset=Source.objects.all())
    if formset.is_valid():
        formset.save()
        messages.success(request, "Sources saved.")
    else:
        messages.error(request, "Sources not saved: " + "; ".join(str(e) for e in formset.errors if e))
    return redirect("integrations:settings")


@require_POST
def push(request, post_pk, service):
    """htmx: push the post's included recommendations to a service; render results."""
    post = get_object_or_404(Post.objects.select_related("source"), pk=post_pk)
    if service not in ServiceConfig.Service.values:
        return render(request, "integrations/_push_results.html", {"error": "Unknown service"})
    name = request.headers.get("HX-Prompt") or request.POST.get("name") or post.title
    outcome = push_post(post, service, playlist_name=name)
    outcome["service"] = ServiceConfig.get(service)
    # Re-render the rec list too so the per-item chips update.
    from vibes.views import _rec_context

    outcome.update(_rec_context(post))
    return render(request, "integrations/_push_results.html", outcome)


@require_POST
def push_rec(request, rec_pk, service):
    """htmx: push ONE recommendation to a service, regardless of its included state --
    a per-row click is its own instruction. Re-renders just that row so its chips
    (the only feedback for this action) reflect the outcome."""
    rec = get_object_or_404(Recommendation.objects.select_related("post__source"), pk=rec_pk)
    if service not in ServiceConfig.Service.values:
        return HttpResponseBadRequest("unknown service")
    name = request.headers.get("HX-Prompt") or None
    push_one(rec, service, playlist_name=name)
    rec.refresh_from_db()
    return render(request, "vibes/_rec_row.html", {"rec": rec, "post": rec.post, **service_flags(rec.post.source.kind)})
