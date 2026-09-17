# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
- Test suite (46 tests): parser, sync pipeline, views/exports, integration clients.
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
