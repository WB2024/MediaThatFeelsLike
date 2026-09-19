"""
slskd (https://github.com/slskd/slskd) -- a Soulseek daemon with a REST API.

Flow per recommendation: POST a search, poll until it completes (or the configured
timeout elapses), rank the file results, then POST the best one to the download queue
for its owning peer. Verified directly against a running slskd 0.26 instance:

    POST /api/v0/searches                          {"id": <uuid>, "searchText": ...}
    GET  /api/v0/searches/{id}                      -> {..., "isComplete": bool, "state": ...}
    GET  /api/v0/searches/{id}/responses            -> [{username, hasFreeUploadSlot,
                                                          queueLength, files: [...]}]
    POST /api/v0/transfers/downloads/{username}     [{"filename": ..., "size": ...}]
                                                     -> {"enqueued": [...], "failed": [...]}

Each file in a search response carries `extension`, `size`, `length` (seconds) and
either `bitRate` (+`isVariableBitRate`) for lossy formats or `bitDepth`+`sampleRate` for
lossless ones -- `extension` is sometimes blank, so the filename's own suffix is the
reliable source of truth.
"""

import time
import uuid

from .base import BaseClient, ServiceError, normalise, similarity

AUDIO_EXTENSIONS = {"mp3", "flac", "m4a", "ogg", "opus", "wav", "aac", "wma", "ape"}
FORMAT_RANK = {"flac": 3, "ape": 3, "wav": 3, "m4a": 2, "aac": 2, "ogg": 2, "opus": 2, "mp3": 1, "wma": 0}
MIN_TITLE_SIMILARITY = 0.5


def _extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _basename(filename):
    return filename.replace("\\", "/").rsplit("/", 1)[-1]


def describe_file(file):
    """Human-readable facts about a search-result file, for the detail page's picker."""
    filename = file.get("filename", "")
    ext = (file.get("extension") or _extension(filename)).upper()
    if file.get("bitRate"):
        quality = f"{file['bitRate']}kbps{' VBR' if file.get('isVariableBitRate') else ''}"
    elif file.get("bitDepth") or file.get("sampleRate"):
        rate = f"/{(file.get('sampleRate') or 0) / 1000:g}kHz" if file.get("sampleRate") else ""
        quality = f"{file.get('bitDepth') or '?'}-bit{rate}"
    else:
        quality = ""
    length = file.get("length") or 0
    return {
        "name": _basename(filename),
        "folder": filename.replace("\\", "/").rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in filename.replace("\\", "/") else "",
        "ext": ext,
        "quality": quality,
        "size_mb": round((file.get("size") or 0) / 1048576, 1),
        "length": f"{length // 60}:{length % 60:02d}" if length else "",
    }


class SlskdClient(BaseClient):
    api = "/api/v0"

    def __init__(self, config):
        super().__init__(config)
        self.session.headers["X-Api-Key"] = config.api_key or ""

    def test(self):
        info = self.get(f"{self.api}/application")
        server = info.get("server", {})
        if not server.get("isConnected"):
            return "slskd reachable, but not connected to the Soulseek network"
        version = (info.get("version") or {}).get("current", "?")
        return f"slskd {version}, connected to Soulseek as of {server.get('state', 'unknown state')}"

    def refresh_choices(self):
        return None

    # -- searching ----------------------------------------------------------------------

    def search(self, query, max_wait=15, poll_interval=1.5):
        """Start a search and poll until it completes or `max_wait` elapses. A popular
        track often hits slskd's own response-limit within a few seconds; an obscure one
        can run for its full internal timeout (~25-30s observed), well past a reasonable
        UI wait. Critically, GET .../responses returns [] -- not partial data -- until
        the search reports isComplete, so hitting our own deadline first isn't enough:
        PUT .../{id} cancels it early, which *does* mark it complete with whatever
        results have arrived so far. Returns the list of per-peer response dicts."""
        search_id = str(uuid.uuid4())
        self.post(f"{self.api}/searches", json={"id": search_id, "searchText": query})
        deadline = time.monotonic() + max_wait
        completed = False
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            status = self.get(f"{self.api}/searches/{search_id}")
            if status.get("isComplete"):
                completed = True
                break
        if not completed:
            self._request("PUT", f"{self.api}/searches/{search_id}")
        try:
            return self.get(f"{self.api}/searches/{search_id}/responses") or []
        finally:
            self._request("DELETE", f"{self.api}/searches/{search_id}")

    # -- ranking --------------------------------------------------------------------------

    def _score_file(self, filename, file, query):
        ext = (file.get("extension") or "").lower() or _extension(filename)
        if ext not in AUDIO_EXTENSIONS:
            return None
        name_sim = similarity(_basename(filename).rsplit(".", 1)[0], query)
        if name_sim < MIN_TITLE_SIMILARITY:
            return None
        length = file.get("length") or 0
        if length and not (30 <= length <= 20 * 60):
            return None  # a sample/snippet or something absurdly long, not the track itself
        quality = FORMAT_RANK.get(ext, 0) + min(file.get("bitRate", 0) or 0, 1000) / 1000
        return name_sim * 3 + quality

    def ranked(self, artist, title, options=None, limit=12, retries=0):
        """Search and return the plausible audio files, best first, each as
        {username, file, score, free_slot, queue}. The detail page shows these so a
        person can pick; `find_best` just takes the top one.

        `retries`: the Soulseek server silently drops a search fired too soon after
        another (observed live: back-to-back searches for hugely popular tracks came
        back empty roughly every other time, regardless of query). The interactive
        picker retries once after a pause; the batch push path doesn't, to keep its
        per-item time budget honest."""
        options = options or {}
        query = f"{artist} {title}".strip()
        responses = self.search(query, max_wait=options.get("max_wait", 15))
        for _ in range(retries):
            if responses:
                break
            time.sleep(4)
            responses = self.search(query, max_wait=options.get("max_wait", 15))

        rows = []
        for resp in responses:
            # Availability matters more than a marginal quality gain: a peer with no
            # free slot and a long queue may take hours to start, or never finish if
            # they go offline first, so weight this more heavily than format/bitrate.
            slot_bonus = 1.5 if resp.get("hasFreeUploadSlot") else 0.0
            queue_penalty = min(resp.get("queueLength", 0) or 0, 200) / 40
            for file in resp.get("files", []):
                base = self._score_file(file.get("filename", ""), file, query)
                if base is None:
                    continue
                rows.append({
                    "username": resp.get("username"), "file": file, "score": base + slot_bonus - queue_penalty,
                    "free_slot": bool(resp.get("hasFreeUploadSlot")), "queue": resp.get("queueLength", 0) or 0,
                })
        rows.sort(key=lambda r: -r["score"])
        return rows[:limit]

    def find_best(self, artist, title, options=None):
        """Search and return (username, file, score) for the best matching audio file, or
        (None, None, 0) if nothing good enough turned up."""
        rows = self.ranked(artist, title, options, limit=1)
        if not rows:
            return None, None, 0
        return rows[0]["username"], rows[0]["file"], rows[0]["score"]

    # -- downloading ------------------------------------------------------------------------

    def enqueue(self, username, file):
        payload = [{"filename": file["filename"], "size": file["size"]}]
        result = self.post(f"{self.api}/transfers/downloads/{username}", json=payload)
        if result and result.get("failed"):
            raise ServiceError(f"slskd: {username} refused the file ({result['failed'][0].get('exception', 'unknown error')})")
        return result

    def push(self, rec, options=None):
        """One recommendation -> (status, detail)."""
        options = options or {}
        artist, title = rec.parsed_artist, rec.parsed_title
        if not title and not artist:
            return "skipped", "nothing to search for"
        username, file, score = self.find_best(artist, title, options)
        if not username:
            return "not_found", f"no matching audio file found on Soulseek for '{normalise(f'{artist} {title}')}'"
        label = _basename(file["filename"])
        ext = (file.get("extension") or _extension(file["filename"])).upper()
        quality = f"{file['bitRate']}kbps" if file.get("bitRate") else (f"{file.get('bitDepth', '?')}-bit" if file.get("bitDepth") else "")
        summary = f"{label} ({ext} {quality})".strip()
        if not options.get("auto_download", True):
            return "found", f"found from {username}, not downloaded (auto-download is off): {summary}"
        try:
            self.enqueue(username, file)
        except ServiceError as exc:
            return "error", str(exc)
        return "queued", f"queued from {username}: {summary}"
