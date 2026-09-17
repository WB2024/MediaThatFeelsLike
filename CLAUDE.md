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
- **Unauthenticated fetch from old.reddit.com is the primary Reddit access path — not
  PRAW/OAuth.** This reverses the original plan: Reddit closed self-service developer
  app registration in November 2025 ("Responsible Builder Policy"), and new personal-use
  app registrations are now rejected in practice (confirmed directly against this
  project's own registration attempt). Keep `REDDIT_CLIENT_ID`/`SECRET` wired up as an
  optional preferred path in case an app registration is ever approved, but never make
  the sync command depend on it. See `docs/ARCHITECTURE.md` → "Reddit access" for the
  full reasoning, the evidence it's currently working, and the request-pacing rules
  (realistic browser User-Agent, one request per post via `/comments/<id>/.json`,
  aggressive caching, multi-second gaps, back off rather than retry on failure). Do not
  relitigate this without re-reading that section first.
- **Cron-driven sync command**, not Celery/Redis. Matches every other automation already
  running in this homelab (see the sibling `lidarr-drip-search.py` / queue-janitor style
  scripts on the services LXC) — a single-user tool doesn't need a task queue.
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

Repo scaffolding only (README, LICENSE, .gitignore, .gitattributes, .editorconfig,
requirements files, `.env.example`, this file, and `docs/ARCHITECTURE.md`). The actual
Django project (`manage.py`, settings, the four apps described in the architecture doc)
has not been created yet — that's the next piece of work.
