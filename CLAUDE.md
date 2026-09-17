# Project notes for Claude Code

Read `docs/ARCHITECTURE.md` first — it has the full design reasoning. This file is just
the fast-recall summary plus conventions, for picking the project back up quickly.

## What this is

A Django app with two mood-board sections (Movies from r/MoviesThatFeelLike, Music from
r/SongsThatFeelLikeThis): tile grid of post images → click through to a detail page with
parsed recommendations from the comments → export as CSV/TXT/M3U or push into
Radarr/Lidarr (add + search) or Jellyfin/Navidrome (create playlist).

## Locked-in decisions (don't relitigate without a reason)

- **Django**, not Flask — the admin panel and ORM/migrations earn their keep given the
  relational data and four integrations.
- **The Arctic Shift archive API is the default Reddit backend** (`REDDIT_BACKEND=auto`
  → archive). Not PRAW (registration closed since Nov 2025) and not unauthenticated
  reddit.com JSON (works until the IP trips Reddit's logged-out rate limit, then every
  JSON endpoint is blocked for hours -- measured on 2026-09-17). Both remain selectable
  backends behind the same `listing` / `post_with_comments` interface in
  `reddit_sync/client.py`. `docs/ARCHITECTURE.md` → "Reddit access" has the full
  evidence trail; read it before proposing a change here.
- **A periodic management command** (`sync_reddit`), run by the compose `sync` sidecar
  loop (or cron on a bare host) -- not Celery/Redis. Matches every other automation in
  this homelab; a single-user tool doesn't need a task queue. The comment-thread cap per
  run (`--max-comments`) is what keeps a cold start from becoming a burst.
- **Recommendation parsing is a heuristic candidate-generator, not a guarantee.** The
  curation step on the vibe detail page (tick/edit/discard) is load-bearing UX, not a
  stopgap — don't try to make the parser perfect instead of making curation good.
- **Service credentials are encrypted at rest** (Fernet, key from
  `CREDENTIAL_ENCRYPTION_KEY`), never plain text in the DB, even though this only runs on
  the private LAN.
- **Deployment target**: Docker on the services LXC (`192.168.1.110`), behind the
  existing Nginx Proxy Manager, gunicorn, sqlite. Don't invent a different deployment
  shape without discussing it — this needs to slot into an existing homelab, not become
  a one-off.

## Environment

- Developed on Windows (`C:\Users\Will\Github\MediaThatFeelsLike`), deployed to a Debian
  LXC. **Every text file must use LF line endings** — `.gitattributes` enforces this, but
  if you're ever writing a file outside git's normal path (e.g. generating it on the
  Linux box directly), double-check with `sed -i 's/\r$//'` before running it. A CRLF
  shebang has repeatedly broken scripts elsewhere in this homelab with a cryptic
  `/usr/bin/env: 'python3\r': No such file or directory` — don't reintroduce that class
  of bug here.
- `.env` (from `.env.example`) holds real secrets and is gitignored — never commit it,
  never paste its contents into a commit message or PR description.

## Current state

Working application: sync (archive backend, tested live), tile grids, detail page with
htmx curation, CSV/TXT/M3U exports, Radarr/Lidarr adds and Jellyfin/Navidrome playlists
(all four tested against the real services on the LAN), Settings page, Docker/compose
packaging. `pytest` covers the parser, the sync pipeline (fake client), the views/exports
and the integration clients (stubbed HTTP). Not yet deployed to the LXC.

## Conventions

- Windows dev box: use `.venv\Scripts\python.exe`, set `PYTHONIOENCODING=utf-8` before
  running management commands that print (the sync log has arrows), and keep Bash
  heredocs under ~8 KB (longer commands get truncated by the shell on Windows -- write a
  file instead).
- `ruff check .` must be clean (config in `pyproject.toml`); `pytest -q` must pass.
- Don't run pushes (Radarr/Lidarr adds, playlist creation) against the real services in
  tests -- stub `_request`. When verifying by hand, add with search disabled and delete
  afterwards; the Lidarr artist-monitoring quirk is documented in
  `integrations/clients/lidarr.py`.
