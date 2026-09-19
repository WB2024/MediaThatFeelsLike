"""Push a post's included recommendations into one service, recording per-item results."""

from django.utils import timezone

from .clients.base import ServiceError
from .clients.jellyfin import JellyfinClient
from .clients.lidarr import LidarrClient
from .clients.navidrome import NavidromeClient
from .clients.radarr import RadarrClient
from .clients.slskd import SlskdClient
from .clients.tmdb import TmdbClient
from .models import ServiceConfig

CLIENTS = {
    ServiceConfig.Service.RADARR: RadarrClient,
    ServiceConfig.Service.LIDARR: LidarrClient,
    ServiceConfig.Service.JELLYFIN: JellyfinClient,
    ServiceConfig.Service.NAVIDROME: NavidromeClient,
    ServiceConfig.Service.SLSKD: SlskdClient,
    ServiceConfig.Service.TMDB: TmdbClient,
}
OK_STATUSES = {"added", "exists", "playlisted", "artist_only", "queued"}
DEFAULT_PLAYLIST_NAME = "MediaThatFeelsLike Picks"
# A slskd search+download can take up to ~max_wait seconds each; this is a synchronous
# htmx request behind gunicorn's --timeout (120s), so a single push budgets this many
# seconds total and processes only as many recommendations as fit -- the rest stay for a
# follow-up click (already-queued ones are skipped automatically, so repeated clicks work
# through a long list safely). Scaling by the configured search timeout means raising it
# for hard-to-find tracks on the Settings page can't push a batch past the worker timeout.
SLSKD_MAX_PER_PUSH = 6
SLSKD_TIME_BUDGET = 100


def _slskd_batch_size(max_wait):
    return max(1, min(SLSKD_MAX_PER_PUSH, SLSKD_TIME_BUDGET // max(max_wait, 1)))


def client_for(service):
    config = ServiceConfig.get(service)
    if not config.is_configured:
        raise ServiceError(f"{config.get_service_display()} is not configured")
    return CLIENTS[service](config)


def _record(rec, service, status, detail, url=None):
    state = dict(rec.integration_state or {})
    entry = {"status": status, "detail": detail[:300], "at": timezone.now().isoformat(timespec="seconds")}
    if url:
        entry["url"] = url
    state[service] = entry
    rec.integration_state = state
    rec.save(update_fields=["integration_state", "updated_at"])
    return {"rec": rec, "status": status, "detail": detail, "url": url}


def push_post(post, service, playlist_name=None):
    """Returns {"results": [...], "summary": str, "error": str|None}."""
    recs = list(post.recommendations.filter(included=True).order_by("order"))
    if not recs:
        return {"results": [], "summary": "Nothing included -- tick some recommendations first.", "error": None}
    try:
        client = client_for(service)
    except ServiceError as exc:
        return {"results": [], "summary": "", "error": str(exc)}

    if service in (ServiceConfig.Service.RADARR, ServiceConfig.Service.LIDARR):
        return _push_arr(client, service, recs)
    if service == ServiceConfig.Service.SLSKD:
        return _push_slskd(client, recs)
    return _push_playlist(client, service, post, recs, playlist_name)


def push_one(rec, service, playlist_name=None):
    """Push a single recommendation, independent of its included/excluded state -- a
    direct per-row click is its own instruction, not gated by the bulk-export checkbox.
    Same result shape as push_post: {"results": [...], "summary": str, "error": str|None}."""
    try:
        client = client_for(service)
    except ServiceError as exc:
        # Record it as a chip on the row too, so the only feedback mechanism for a
        # per-row action (its own updated chips) still surfaces the problem.
        result = _record(rec, service, "error", str(exc))
        return {"results": [result], "summary": str(exc), "error": None}

    if service in (ServiceConfig.Service.RADARR, ServiceConfig.Service.LIDARR):
        return _push_arr(client, service, [rec])
    if service == ServiceConfig.Service.SLSKD:
        return _push_slskd(client, [rec])
    return _push_one_playlist(client, service, rec, playlist_name)


def _push_one_playlist(client, service, rec, playlist_name):
    """Jellyfin/Navidrome, one item: add to the existing playlist of this name if there
    is one, otherwise create it -- unlike the bulk button, which always makes a fresh
    playlist. That's the point of the per-item action: pick tracks/movies one at a time
    into a running collection instead of starting a new single-item playlist every time."""
    kind = rec.post.source.kind
    try:
        item, status, detail = client.resolve(rec, kind)
    except ServiceError as exc:
        item, status, detail = None, "error", str(exc)
    if item is None:
        result = _record(rec, service, status, detail)
        return {"results": [result], "summary": detail, "error": None}

    name = (playlist_name or DEFAULT_PLAYLIST_NAME).strip()[:100] or DEFAULT_PLAYLIST_NAME
    try:
        if service == ServiceConfig.Service.JELLYFIN:
            playlist_id, created = client.add_or_create_playlist(name, [item["Id"]], "Video" if kind == "movies" else "Audio")
        else:
            playlist_id, created = client.add_or_create_playlist(name, [item["id"]])
    except ServiceError as exc:
        result = _record(rec, service, "error", str(exc))
        return {"results": [result], "summary": str(exc), "error": None}

    verb = "Created" if created else "Added to"
    full_detail = f"{verb} playlist “{name}” ({playlist_id}): {detail}"
    result = _record(rec, service, "playlisted", full_detail)
    return {"results": [result], "summary": full_detail, "error": None}


def _push_slskd(client, recs):
    already = [r for r in recs if (r.integration_state or {}).get("slskd", {}).get("status") == "queued"]
    pending = [r for r in recs if r not in already]
    batch_size = _slskd_batch_size(client.config.options.get("max_wait", 15))
    batch, rest = pending[:batch_size], pending[batch_size:]

    results = []
    for rec in batch:
        try:
            status, detail = client.push(rec, options=client.config.options)
        except ServiceError as exc:
            status, detail = "error", str(exc)
        results.append(_record(rec, ServiceConfig.Service.SLSKD, status, detail))

    ok = sum(1 for r in results if r["status"] in OK_STATUSES)
    summary = f"{ok} of {len(batch)} queued on slskd" if batch else "Nothing to do -- every included recommendation is already queued."
    if already:
        summary += f" ({len(already)} already queued from an earlier push)"
    if rest:
        summary += f". {len(rest)} more waiting -- press the button again to continue (each search takes a few seconds)."
    return {"results": results, "summary": summary, "error": None}


def _push_arr(client, service, recs):
    results = []
    for rec in recs:
        url = None
        try:
            status, detail, *rest = client.push(rec)
            url = rest[0] if rest else None
        except ServiceError as exc:
            status, detail = "error", str(exc)
        results.append(_record(rec, service, status, detail, url))
    ok = sum(1 for r in results if r["status"] in OK_STATUSES)
    return {"results": results, "summary": f"{ok} of {len(results)} handled by {client.config.get_service_display()}", "error": None}


def _push_playlist(client, service, post, recs, playlist_name):
    kind = post.source.kind
    results, matched = [], []
    for rec in recs:
        try:
            item, status, detail = client.resolve(rec, kind)
        except ServiceError as exc:
            item, status, detail = None, "error", str(exc)
        if item is not None:
            matched.append((rec, item))
        results.append({"rec": rec, "status": status, "detail": detail})

    if not matched:
        for r in results:
            _record(r["rec"], service, r["status"], r["detail"])
        return {"results": results, "summary": "No included recommendation was found in the library, so no playlist was created.", "error": None}

    name = (playlist_name or post.title)[:100]
    try:
        if service == ServiceConfig.Service.JELLYFIN:
            ids = [item["Id"] for _, item in matched]
            playlist_id = client.create_playlist(name, ids, "Video" if kind == "movies" else "Audio")
        else:
            playlist_id = client.create_playlist(name, [item["id"] for _, item in matched])
    except ServiceError as exc:
        return {"results": results, "summary": "", "error": f"Playlist creation failed: {exc}"}

    matched_ids = {id(rec) for rec, _ in matched}
    for r in results:
        if id(r["rec"]) in matched_ids:
            r["status"] = "playlisted"
        _record(r["rec"], service, r["status"], r["detail"])
    return {
        "results": results,
        "summary": f"Created playlist “{name}” in {client.config.get_service_display()} with {len(matched)} of {len(recs)} items (id {playlist_id}).",
        "error": None,
    }
