"""
A small read-only JSON API, built specifically to give this app a real presence on the
Glance dashboard (a `custom-api` widget) rather than just a plain up/down status tile.
Not meant as a general integration surface -- there's no versioning or auth beyond what
the rest of the app already has (LAN-only, no login), and the shape is whatever's
convenient for a Go text/template to render, not a stable public contract.
"""

from django.http import JsonResponse

from .models import Post, Recommendation, Source
from .views import _hot_key, _kind_or_404

HOT_SCAN_LIMIT = 200  # matches the tile grid's own hot-sort scan window, scaled down


def hot(request, kind):
    """The N hottest posts with a locally cached image, for an image-strip widget.
    (Post.primary_image falls back to the raw Reddit CDN URL when nothing's cached
    yet, which is fine for the app's own grid but not guaranteed to load for an
    external viewer -- so this checks for an actually-cached file explicitly.)"""
    kind = _kind_or_404(kind)
    try:
        limit = min(int(request.GET.get("limit", 10)), 25)
    except ValueError:
        limit = 10

    posts = (
        Post.objects.visible().for_kind(kind).with_counts()
        .prefetch_related("images")
        .order_by("-created_utc")[:HOT_SCAN_LIMIT]
    )
    ranked = sorted(posts, key=_hot_key, reverse=True)

    data = []
    for post in ranked:
        image = next((i for i in post.images.all() if i.file), None)
        if not image:
            continue
        data.append(
            {
                "id": post.pk,
                "title": post.title,
                "url": request.build_absolute_uri(post.get_absolute_url()),
                "image": request.build_absolute_uri(image.display_url),
                "rec_count": post.rec_count or 0,
                "score": post.score,
                "num_comments": post.num_comments,
                "created_utc": post.created_utc.isoformat(),
            }
        )
        if len(data) >= limit:
            break
    return JsonResponse(data, safe=False)


def stats(request):
    """Aggregate counts for a small stats widget."""
    counts = {kind: Post.objects.visible().for_kind(kind).count() for kind in Source.Kind.values}
    last_synced = Source.objects.filter(last_synced_at__isnull=False).order_by("-last_synced_at").first()
    return JsonResponse(
        {
            "movies": counts.get(Source.Kind.MOVIES, 0),
            "music": counts.get(Source.Kind.MUSIC, 0),
            "recommendations": Recommendation.objects.filter(included=True).count(),
            "last_synced": last_synced.last_synced_at.isoformat() if last_synced else None,
        }
    )
