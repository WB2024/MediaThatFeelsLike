# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **slskd integration**: a "Grab via slskd" button on music vibe pages searches
  Soulseek, ranks results (title match, format/bitrate, weighted toward free upload
  slots and short queues over marginal quality), and queues the best file for
  download -- or just reports it if "auto-download" is off. Verified against a live
  slskd instance including a full search → download → cleanup round trip. A push
  time-budgets ~100s and processes as many recommendations as fit (6 at the default
  15s search timeout, fewer if raised) and skips ones already queued from an earlier
  push.
- Fixed immediately after first deploying the above: slskd's `GET .../responses`
  returns nothing -- not partial results -- until a search reports itself complete,
  and an unpopular query can run for slskd's own ~25-30s internal timeout, well past
  our UI wait. Every recommendation came back "not found" against genuinely findable
  tracks until this was caught. `PUT .../searches/{id}` now cancels a search early to
  unlock whatever arrived so far; covered by a regression test.
- Backfill: `sync_reddit --backfill N` (and a field on the Settings page's Sync
  form) pages N pages further back into each source's history using the archive
  backend's `before` cursor. Previously the sync only ever pulled the single most
  recent page per source, so "Load more" ran out once that page was exhausted.

### Fixed

- Media files (cached post images) 404'd in production: `django.conf.urls.static.static()`
  is a no-op unless `DEBUG=True`. `config/urls.py` now serves `MEDIA_URL` unconditionally.
  Found immediately after the first production deploy.

### Added

- The Django application itself: `config/` project, `vibes` (models, tile grids, detail
  page with htmx curation), `reddit_sync` (three interchangeable Reddit backends, image
  caching with thumbnails, heuristic comment parser, `sync_reddit` /
  `reparse_recommendations` / `bootstrap` commands, `SyncRun` audit log with block
  cooldown), `integrations` (Fernet-encrypted `ServiceConfig`, Radarr / Lidarr /
  Jellyfin / Navidrome clients, Settings page with connection tests and choice loading,
  push orchestration with per-item results), `exports` (CSV / TXT / M3U / M3U8, with
  library paths resolved through Navidrome or Jellyfin when configured).
- Dark, image-first UI: masonry tile grid with hot/new/top/most-recs/starred ordering
  and search, gallery strip on the detail page, inline recommendation editing, star/hide.
- Docker packaging: `Dockerfile`, `compose.yaml` (app + sync sidecar), entrypoint.
- Test suite (47 tests): parser, sync pipeline, views/exports, integration clients.
- Deployed to the services LXC (`192.168.1.110:8095`) and pushed to Docker Hub
  (`wb20244/mediathatfeelslike`). README screenshots taken from the live deployment.
- A `MediaThatFeelsLike` tile on the Glance dashboard's Media page.
- Initial repo scaffolding: README, MIT license, `.gitignore`, `.gitattributes`
  (enforcing LF line endings), `.editorconfig`, `requirements.txt` /
  `requirements-dev.txt`, `.env.example`, `docs/ARCHITECTURE.md`, `CLAUDE.md`.

### Changed

- Reddit access, second revision: the Arctic Shift archive API is now the default
  backend. Unauthenticated reddit.com JSON was built first and found to be blocked
  per-IP for hours once the rate limit trips (measured); it stays available as
  `REDDIT_BACKEND=direct` with an automatic cooldown. PRAW remains the optional
  `REDDIT_BACKEND=praw`. See `docs/ARCHITECTURE.md` → "Reddit access".
- Reddit access, first revision: plan reversed from PRAW/OAuth to unauthenticated
  fetching after confirming Reddit's November 2025 "Responsible Builder Policy" blocks
  new developer app registrations in practice.
