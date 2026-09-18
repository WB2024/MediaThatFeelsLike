"""
A small read-only JSON API for a Glance `custom-api` widget showing slskd activity --
what's downloading right now, plus today's running done/failed counts. See vibes/api.py
for the same rationale (a real Glance presence, not a general integration surface).
"""

from django.http import JsonResponse

from .clients.base import ServiceError
from .models import ServiceConfig
from .push import client_for

ACTIVE_LIMIT = 8


def _basename(path):
    return (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _human_rate(bytes_per_sec):
    if not bytes_per_sec:
        return ""
    value = float(bytes_per_sec)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.0f} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"
        value /= 1024


def _classify(state):
    """-> "active" (still transferring or waiting on a peer slot), "done", "failed", or
    "other" (a transient state like Requested/Initializing -- too brief to bucket)."""
    if state == "InProgress" or state.startswith("Queued"):
        return "active"
    if state == "Completed, Succeeded":
        return "done"
    if state.startswith("Completed"):
        return "failed"
    return "other"


def summarize(transfers):
    """transfers: slskd's GET /api/v0/transfers/downloads payload (a list of per-peer
    blocks). Counts are per-file (what slskd's own UI would show); "active" groups by
    (peer, directory) instead, so one 150-track album grab is a single row, not 150."""
    counts = {"downloading": 0, "queued": 0, "completed": 0, "failed": 0}
    groups = {}  # (username, directory) -> {size, transferred, speed, active}
    for peer in transfers or []:
        username = peer.get("username", "")
        for d in peer.get("directories") or []:
            directory = d.get("directory", "")
            g = groups.setdefault((username, directory), {"size": 0, "transferred": 0, "speed": 0, "active": False})
            for f in d.get("files") or []:
                state = f.get("state", "")
                kind = _classify(state)
                if kind == "active":
                    counts["downloading" if state == "InProgress" else "queued"] += 1
                    g["active"] = True
                    g["speed"] += f.get("averageSpeed") or 0
                elif kind == "done":
                    counts["completed"] += 1
                elif kind == "failed":
                    counts["failed"] += 1
                g["size"] += f.get("size") or 0
                g["transferred"] += f.get("bytesTransferred") or 0

    active = []
    for (username, directory), g in groups.items():
        if not g["active"] or g["size"] <= 0:
            continue
        active.append(
            {
                "label": f"{username} · {_basename(directory)}",
                "percent": min(100, round(g["transferred"] * 100 / g["size"])),
                "speed": _human_rate(g["speed"]),
            }
        )
    active.sort(key=lambda a: -a["percent"])
    return {"counts": counts, "active": active[:ACTIVE_LIMIT]}


def slskd_status(request):
    try:
        client = client_for(ServiceConfig.Service.SLSKD)
        app_info = client.get(f"{client.api}/application")
        transfers = client.get(f"{client.api}/transfers/downloads")
    except ServiceError as exc:
        return JsonResponse({"connected": False, "error": str(exc), "counts": {}, "active": []})

    server = (app_info or {}).get("server") or {}
    data = summarize(transfers)
    data.update(
        {
            "connected": bool(server.get("isConnected")),
            "version": ((app_info or {}).get("version") or {}).get("current", ""),
        }
    )
    return JsonResponse(data)
