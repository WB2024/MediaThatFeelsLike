from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Max, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from reddit_sync.models import SyncRun
from reddit_sync.sync import Syncer

from .models import Post, Recommendation, Source

PAGE_SIZE = 40
SORTS = {
    "hot": "Hot",
    "new": "New",
    "top": "Top",
    "most": "Most recs",
    "starred": "Starred",
}


def _kind_or_404(kind):
    if kind not in Source.Kind.values:
        raise Http404
    return kind


def home(request):
    return redirect("vibes:section", kind=Source.Kind.MOVIES)


def section(request, kind):
    """Tile grid for one section. htmx requests get just the next page of tiles."""
    kind = _kind_or_404(kind)
    sort = request.GET.get("sort", "hot")
    if sort not in SORTS:
        sort = "hot"
    q = request.GET.get("q", "").strip()

    posts = Post.objects.visible().for_kind(kind).with_counts().prefetch_related("images")
    if q:
        posts = posts.filter(Q(title__icontains=q) | Q(recommendations__parsed_title__icontains=q) | Q(recommendations__parsed_artist__icontains=q)).distinct()
    if sort == "new":
        posts = posts.order_by("-created_utc")
    elif sort == "top":
        posts = posts.order_by("-score", "-created_utc")
    elif sort == "most":
        posts = posts.order_by("-rec_count", "-score")
    elif sort == "starred":
        posts = posts.filter(starred=True).order_by("-created_utc")
    else:
        # "hot": recency-weighted -- newest first, but a well-upvoted post from a few
        # days ago outranks a fresh one nobody has answered yet.
        posts = posts.order_by("-created_utc")
        posts = sorted(posts[:400], key=_hot_key, reverse=True)

    paginator = Paginator(posts, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page", 1))
    context = {
        "kind": kind,
        "kind_label": dict(Source.Kind.choices)[kind],
        "page": page,
        "sort": sort,
        "sorts": SORTS,
        "q": q,
        "total": paginator.count,
        "sources": Source.objects.filter(kind=kind),
    }
    template = "vibes/_tiles.html" if request.headers.get("HX-Request") else "vibes/section.html"
    return render(request, template, context)


def _hot_key(post):
    age_hours = max(1.0, (timezone.now() - post.created_utc).total_seconds() / 3600)
    signal = max(post.score, 0) + 2 * (post.rec_count or 0)
    return (signal + 1) / (age_hours + 12) ** 1.2


def post_detail(request, pk):
    post = get_object_or_404(Post.objects.select_related("source").prefetch_related("images"), pk=pk)
    context = {
        "post": post,
        "kind": post.source.kind,
        "images": list(post.images.all()),
        "cooldown": SyncRun.cooldown_until(),
        **_rec_context(post),
    }
    return render(request, "vibes/post_detail.html", context)


def _rec_context(post):
    """Shared by post_detail and every htmx rec-list partial -- the per-row "add to..."
    dropdown needs `services`/`can_*` just as much as the bulk buttons at the top do, so
    this always computes them rather than only the page-load view bothering to."""
    from integrations.models import service_flags  # local import: avoid app-load cycles

    recs = list(post.recommendations.order_by("-mention_count", "-confidence", "order"))
    return {
        "post": post,
        "recs": recs,
        "included": [r for r in recs if r.included],
        "excluded": [r for r in recs if not r.included],
        "included_count": sum(1 for r in recs if r.included),
        **service_flags(post.source.kind),
    }


def _rec_list_response(request, post):
    return render(request, "vibes/_rec_list.html", {**_rec_context(post), "oob_count": True})


# -- recommendation curation (all htmx) --------------------------------------------------


@require_POST
def rec_toggle(request, pk):
    rec = get_object_or_404(Recommendation.objects.select_related("post"), pk=pk)
    rec.included = not rec.included
    rec.edited = True
    rec.save(update_fields=["included", "edited", "updated_at"])
    return _rec_list_response(request, rec.post)


def rec_edit(request, pk):
    rec = get_object_or_404(Recommendation.objects.select_related("post__source"), pk=pk)
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        artist = request.POST.get("artist", "").strip()
        year = request.POST.get("year", "").strip()
        if not title and not artist:
            return render(request, "vibes/_rec_edit.html", {"rec": rec, "error": "Need at least a title or an artist."})
        rec.parsed_title = title[:300]
        rec.parsed_artist = artist[:200]
        rec.parsed_year = int(year) if year.isdigit() else None
        rec.included = True
        rec.edited = True
        rec.save()
        return _rec_list_response(request, rec.post)
    return render(request, "vibes/_rec_edit.html", {"rec": rec})


@require_POST
def rec_delete(request, pk):
    rec = get_object_or_404(Recommendation.objects.select_related("post"), pk=pk)
    post = rec.post
    rec.delete()
    return _rec_list_response(request, post)


@require_POST
def rec_add(request, post_pk):
    post = get_object_or_404(Post.objects.select_related("source"), pk=post_pk)
    title = request.POST.get("title", "").strip()
    artist = request.POST.get("artist", "").strip()
    year = request.POST.get("year", "").strip()
    if title or artist:
        next_order = (post.recommendations.aggregate(m=Max("order"))["m"] or 0) + 1
        Recommendation.objects.create(
            post=post, parsed_title=title[:300], parsed_artist=artist[:200],
            parsed_year=int(year) if year.isdigit() else None,
            method=Recommendation.Method.MANUAL, confidence=1.0, included=True, edited=True, order=next_order,
        )
    return _rec_list_response(request, post)


@require_POST
def rec_bulk(request, post_pk):
    post = get_object_or_404(Post, pk=post_pk)
    action = request.POST.get("action")
    recs = post.recommendations.all()
    if action == "include_all":
        recs.update(included=True, edited=True)
    elif action == "exclude_all":
        recs.update(included=False, edited=True)
    elif action == "reset":
        # Back to the parser's own judgement (keeps manual additions).
        from reddit_sync.parser import INCLUDE_THRESHOLD

        recs.exclude(method=Recommendation.Method.MANUAL).update(edited=False)
        recs.filter(edited=False, confidence__gte=INCLUDE_THRESHOLD).update(included=True)
        recs.filter(edited=False, confidence__lt=INCLUDE_THRESHOLD).update(included=False)
    return _rec_list_response(request, post)


@require_POST
def post_reparse(request, post_pk):
    """Re-run the parser over the stored comments (no network)."""
    post = get_object_or_404(Post.objects.select_related("source"), pk=post_pk)
    run = SyncRun.objects.create(trigger=SyncRun.Trigger.UI, sources=f"reparse {post.reddit_id}")
    syncer = Syncer(run, client=_NoClient(), cache_images=False)
    created = syncer.rebuild_recommendations(post)
    post.save(update_fields=["parsed_at"])
    run.status, run.finished_at, run.recommendations_created = SyncRun.Status.OK, timezone.now(), created
    run.save()
    return _rec_list_response(request, post)


@require_POST
def post_refresh(request, post_pk):
    """Fetch this post's comments again right now (one request)."""
    post = get_object_or_404(Post.objects.select_related("source"), pk=post_pk)
    if SyncRun.running():
        messages.error(request, "A sync is already running -- try again when it finishes.")
        return _rec_list_response(request, post)
    run = SyncRun.objects.create(trigger=SyncRun.Trigger.UI, sources=f"refresh {post.reddit_id}")
    syncer = Syncer(run, cache_images=False, max_comment_fetches=1)
    syncer.sync_comments(post)
    run.status = SyncRun.Status.PARTIAL if run.errors else SyncRun.Status.OK
    run.blocked, run.finished_at = syncer.blocked, timezone.now()
    run.save()
    if run.errors:
        messages.error(request, "Refresh failed -- see the sync log on the Settings page.")
    return _rec_list_response(request, post)


@require_POST
def post_flag(request, post_pk, flag):
    post = get_object_or_404(Post, pk=post_pk)
    if flag not in ("hidden", "starred"):
        raise Http404
    setattr(post, flag, not getattr(post, flag))
    post.save(update_fields=[flag])
    if flag == "hidden" and post.hidden:
        messages.info(request, "Post hidden from the grid. Un-hide it from Django admin if needed.")
        return redirect("vibes:section", kind=post.source.kind)
    return HttpResponse(
        f'<button class="btn small {"primary" if post.starred else ""}" hx-post="{request.path}" hx-swap="outerHTML">'
        f'{"★ Starred" if post.starred else "☆ Star"}</button>'
    )


class _NoClient:
    requests_made = 0
