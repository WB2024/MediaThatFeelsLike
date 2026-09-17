# MediaThatFeelsLike

A self-hosted web app that turns two "vibe recommendation" subreddits —
[r/MoviesThatFeelLike](https://reddit.com/r/MoviesThatFeelLike) and
[r/SongsThatFeelLikeThis](https://reddit.com/r/SongsThatFeelLikeThis) — into a browsable,
exportable discovery tool.

## The idea

Both subreddits work the same way: someone posts an image (or just a feeling) and asks
"what feels like this?", and the comments fill in with recommendations. That's a great
source of curated, vibe-matched discovery — but it's stuck in Reddit's UI, mixed in with
noise, and impossible to act on directly.

MediaThatFeelsLike pulls posts from both subs and presents them as two sections:

- **Movies** — sourced from r/MoviesThatFeelLike
- **Music** — sourced from r/SongsThatFeelLikeThis

Each section shows a tile grid of post images (à la [Scrolller](https://scrolller.com)-style
mood boards). Clicking a tile opens a **vibe detail page**: the original image at the top,
followed by a cleaned-up list of recommendations parsed out of the post's comments.

From there you can:

- **Export** the list as CSV, TXT, or M3U/M3U8
- **Send to Radarr** (movies) or **Lidarr** (music) to add and search for them
- **Build a playlist** directly in **Jellyfin** or **Navidrome**

Radarr/Lidarr API keys and Jellyfin/Navidrome URLs+credentials are configured once in the
app's settings page.

## Status

🚧 **Early scaffolding.** This repo currently holds project setup only — no application
code yet. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the planned design
(data model, Reddit ingestion approach, recommendation parsing strategy, and how each
integration works) before the Django project itself lands.

## Planned stack

- **Django** — chosen over Flask for the built-in admin (handy for inspecting cached
  Reddit data and debugging the recommendation parser), migrations, and ORM, given the
  amount of structured/relational data this app carries.
- **Unauthenticated fetching from old.reddit.com**, paced carefully — not Reddit's
  official OAuth API. Reddit closed self-service developer app registration in late 2025
  ("Responsible Builder Policy"); new personal-use apps are rejected in practice. See
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full reasoning, the current
  evidence it works, and the request-pacing rules that keep it working.
- A periodic sync management command (cron-driven, matching the rest of this homelab's
  automation) rather than a Celery/Redis stack — this is a single-user tool, not a
  service that needs a task queue.

## Getting set up (once code lands)

1. Copy `.env.example` to `.env` and fill in a Django `SECRET_KEY` and a Fernet
   `CREDENTIAL_ENCRYPTION_KEY` (used to encrypt saved service credentials at rest — never
   store Radarr/Lidarr/Jellyfin/Navidrome secrets in plain text). The Reddit fields can
   be left as their defaults for now — see the comments in `.env.example`.
2. `python -m venv .venv && .venv\Scripts\activate` (or `source .venv/bin/activate` on
   Linux), then `pip install -r requirements.txt`.
3. Django project setup (`manage.py`, migrations, first run) is the next piece of work —
   not yet in this repo.

Optional: a Reddit developer app registration (free, at
<https://www.reddit.com/prefs/apps>, type **script**) is worth submitting anyway in case
it's ever approved — the sync command will use it automatically if the credentials are
present — but nothing in this app depends on it being approved.

## License

[MIT](LICENSE) — see the license file for the full text. Change this before publishing
if you'd rather use something else; it's just a sane permissive default for a personal
project.
