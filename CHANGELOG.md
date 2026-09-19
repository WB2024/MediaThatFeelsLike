# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Artist/title orientation fix** for music recommendations -- "All I wanna do - Sheryl
  crow" was being stored (and shown, pushed and searched) as artist "All I wanna do".
  Three layers: (1) the parser now consults a `KnownArtist` table (new model; seeded
  from Lidarr's library, grown by every MusicBrainz confirmation, editable in the admin)
  to decide which half of an "X - Y" is the artist, applies one orientation per comment
  (a single recognisable artist flips its sibling lines too) and no longer drops lines
  whose title starts like a sentence ("This kiss - Faith Hill") when the comment is
  demonstrably "Title - Artist"; explicit markers -- quotes/emphasis around the title,
  "Title by Artist" -- count as orientation evidence too, so re-parsing also repairs
  rows an older parser got backwards from `"Aja" - Steely Dan`; (2) the dedupe key is
  orientation-insensitive, so both
  spellings merge into one recommendation, a corrected row survives re-parsing, and an
  unverified row adopts the parser's orientation on re-parse once it has evidence;
  (3) MusicBrainz is the referee: `enrich.resolve_recording()` looks a pair up as stored
  then reversed, and a match on the reversed pair corrects the row in place (never a
  human-edited one), stamps the new `Recommendation.verified_at`, and teaches the
  artist. Runs on the detail page (with a note when it did) and in the new
  `verify_recommendations` command, which the sync sidecar runs after every sync
  (`SYNC_VERIFY_LIMIT`, default 100, paced at 2 s/request since MusicBrainz's 1 req/s
  limit is per IP and shared with the web container; a 503 gets one polite retry).
  Migration `vibes 0002`. 15 new tests.

### Fixed

- Music detail page raised a template error when Last.fm was disabled and MusicBrainz
  returned no track length (failed lookup inside a `|default:` argument); the length is
  now resolved in Python like the other hints.

- **Recommendation detail pages** (`/rec/<pk>/`, linked from every row's title). Movies:
  everything TheMovieDB returns in one `append_to_response` call -- backdrop, poster,
  tagline, synopsis, rating/votes, runtime, age rating (GB preferred), genres, embedded
  trailer + every other listed video, cast with photos, headline crew, budget/box office,
  companies, countries, languages, keywords, "more like this". Music: MusicBrainz pins
  the recording (canonical-version scoring, see ARCHITECTURE) and yields the artist MBID
  + Cover Art Archive keys; Last.fm adds listeners/plays, tags, track blurb, artist bio,
  similar artists, top tracks; MusicBrainz's artist lookup adds type/country/years and
  curated external links (website, Wikipedia, Discogs, Bandcamp, Spotify, Apple Music,
  Twitter/Instagram…). Down the side: live "in your library" cards for Radarr / Lidarr /
  Jellyfin / Navidrome (present? downloaded? quality/size? direct link into that app;
  add / add-to-playlist actions if not), a Soulseek picker that lists every plausible copy
  with format, bitrate, size, peer, free-slot/queue so you choose rather than trust the
  automatic best guess, and the YouTube/Spotify/IMDb/TMDB/Last.fm/MusicBrainz links. All
  per-service panels are htmx-lazy so the page renders with the hero and fills in. New
  clients: `musicbrainz.py` (keyless, throttled, proper User-Agent), `lastfm.py` (new
  `lastfm` service on the Settings page, `LASTFM_API_KEY` to seed). 17 new tests.
- **TheMovieDB trailer embed**: a movie recommendation gets a "▶ watch trailer here"
  toggle (lazy-loaded via htmx, once, on first expand) that embeds the official trailer
  right on the page instead of only linking out. `TmdbClient.best_trailer()` searches
  TMDB for the title, ranks that movie's listed videos (YouTube + official + Trailer >
  Teaser > anything, by size), and the view degrades gracefully at every step -- TMDB
  not configured, no match, no YouTube video listed -- to a plain "no trailer found"
  message rather than an error, since the row's existing YouTube-search button is
  already a working fallback. New `tmdb` service on the Settings page (`TMDB_API_KEY`
  env var to seed it -- TMDB's v4 "API Read Access Token", sent as a Bearer header).
  Discovered live: a real, fairly common fraction of official studio trailer uploads
  have embedding disabled by the channel owner (YouTube's own "error 153", not
  something this app can work around) -- so the embed is always paired with a direct
  "open on YouTube" link to that exact video underneath, which works regardless.
- **Radarr chip links to the movie**: the "radarr: added"/"radarr: exists" chip on a
  recommendation row is now a link straight to that film's own page in Radarr (new tab).
  `RadarrClient.push()` returns the movie's `titleSlug` alongside the existing
  status/detail whenever it has one; `integrations.push._push_arr` and `_record` accept
  it as an optional third element so Lidarr/slskd's still-2-tuple `push()` need no
  changes, and it's threaded through to `integration_state[service]["url"]`, which
  `_rec_row.html` renders as an `<a>` instead of a plain `<span>` only when present.
  Verified against a real Radarr instance (chip correctly opened the exact film's page).

### Changed

- Recommendations on a vibe page now sort by mention count (most-mentioned first), then
  confidence, then the parser's own order -- previously it was parser-order first, so a
  title several separate commenters agreed on could still land below a single high-
  confidence one-off. One-line change in `_rec_context`'s `order_by`, shared by the
  initial page load and every htmx partial re-render.

### Added

- **YouTube trailer/song links**: every recommendation row gets a hover-revealed
  **▶ Trailer** (movies) or **▶ Play** (music) button that opens a YouTube search for it
  in a new tab -- `{title} ({year}) trailer` for movies, `{artist} {title}` for music,
  reusing the `display_label`/`search_term` already computed for exports and the slskd
  push. Deliberately just a search link, not a call to the YouTube Data API: a
  well-formed query already reliably surfaces the real trailer/song as the first or
  second result, and skipping the API sidesteps needing a Google API key and its quota.
  Music rows also get a **Spotify** button next to it, same query, straight to
  `open.spotify.com/search/<query>` -- same reasoning (no Spotify API/app credentials,
  its own search is good enough), and useful for anyone whose library lives there rather
  than on Navidrome/Jellyfin.
- **slskd Glance widget, upgraded**: `GET /integrations/api/slskd/` aggregates slskd's
  own transfer list in Python (peer + directory groups, not raw per-file JSON) so the
  Media page's slskd widget can show *what's actually downloading right now* -- a
  progress percent and speed per album/grab -- instead of just the four running
  DOWNLOADING/QUEUED/DONE/FAILED counts it had before. Counts are unchanged (still
  per-file, computed the same way); the new `active` list is additive. 6 new tests.
- **Gallery cycling on the tile grid**: posts with more than one image get prev/next
  arrows right on the tile (hover to reveal), so you can flick through a gallery's
  photos before deciding whether to open the post at all -- previously that was only
  possible on the detail page. A single lightweight `static/js/app.js` (the app's first
  standalone script) swaps the tile's `<img>` and a repurposed "N / total" counter;
  clicking the tile itself still opens the detail page as before. Image URLs for a
  gallery are embedded on the tile as a pipe-delimited `data-images` attribute rather
  than one `<img>` per photo, so a big gallery's other images still aren't fetched until
  you actually cycle to them.
- **Configurable media cache location**: `MEDIA_DATA_DIR` (Django) / `MEDIA_HOST_PATH`
  (`compose.yaml`) let cached images live on different storage than the sqlite DB --
  e.g. a large, slower NFS/network drive -- instead of always being a subfolder of
  `DATA_DIR`. Fixed `Syncer._disk_has_room()` to check whichever filesystem
  `MEDIA_ROOT` actually resolves to rather than always `DATA_DIR`, which the disk-floor
  feature below would otherwise have gotten wrong the moment the two diverged.
- **Disk safety floor for image caching**: `MIN_FREE_DISK_GB` (default 2.0) -- these
  subreddits are high-volume enough that a deep `--backfill` can create far more
  pending images than fit on a shared homelab disk. `cache_images()` now checks free
  space before starting and again every 40 successful caches within a run, skipping the
  rest of that run's caching (with a warning in the sync log) once the floor is hit.
  Everything else (listings, comments, recommendations) keeps working regardless, and
  the grid still shows something for every post via `PostImage.display_url`'s existing
  fallback to the original Reddit CDN URL.
- **Glance dashboard widget**: a small JSON API (`vibes/api.py` -- `/api/hot/<kind>/`,
  `/api/stats/`) backs a proper image-strip widget on the homelab dashboard (hottest
  posts per section, plus a stats tile) instead of a bare status tile.
- **Per-recommendation actions**: an "⊕" dropdown on every recommendation row adds just
  that one item to Radarr/Lidarr/slskd/Jellyfin/Navidrome, independent of the row's
  include/exclude checkbox and the bulk buttons. Radarr/Lidarr/slskd reuse the exact
  per-item logic the bulk buttons already use; Jellyfin/Navidrome add to an *existing*
  playlist of the given name (creating it only if needed) instead of always starting a
  fresh one, so picking tracks one at a time builds a single running collection.
  `find_playlist`/`add_to_playlist` verified live against both services. None of this
  uses Radarr/Lidarr's "Import List" feature -- see docs/ARCHITECTURE.md → "Integrations"
  for why that's the wrong shape here. 12 new tests.
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
