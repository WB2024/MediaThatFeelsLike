# Architecture

Written before any application code, so the plan is reviewable and changeable rather
than something that only exists as decisions buried in commits. Treat this as a working
design doc, not a spec set in stone — it'll be updated as the app gets built and reality
disagrees with the plan.

## Goals, restated

Two mood-board sections, each backed by one subreddit:

| Section | Source | 
|---|---|
| Movies | r/MoviesThatFeelLike |
| Music  | r/SongsThatFeelLikeThis |

Flow: tile grid of post images (galleries can be flicked through right on the tile, via
prev/next arrows, before committing to a click) → click a tile → **vibe detail page**
(image at top, then a cleaned list of recommendations pulled from that post's comments)
→ export the list or push it into Radarr/Lidarr/Jellyfin/Navidrome.

## Why Django over Flask

- Real relational data that will grow: posts, images (posts can be galleries), parsed
  recommendations, sync state, per-service credentials. Django's ORM + migrations handle
  schema evolution far more cleanly than hand-rolled SQLAlchemy wiring.
- The built-in admin is genuinely useful here, not just boilerplate: being able to open
  `/admin/` and look at exactly what got synced from a post, what the parser extracted,
  and edit/delete bad rows, is worth a lot during development of the parsing heuristics —
  it's a free debugging UI.
- Four external integrations (Radarr, Lidarr, Jellyfin, Navidrome) plus Reddit sync are
  cleanly separable into their own Django apps.

## Reddit access: the Arctic Shift archive, with direct fetch as a fallback

**Revised again 2026-09-17, after building the sync and testing it for real.** This
section has now been rewritten twice, each time on evidence, so the reasoning is kept in
full:

1. **Official OAuth API (PRAW)** -- the original plan. Reddit closed self-service
   developer app registration in November 2025 ("Responsible Builder Policy"); the
   `reddit.com/prefs/apps` form rejects new personal-use apps in practice (confirmed
   directly with this project's own registration attempt). Kept as an optional backend
   (`REDDIT_BACKEND=praw`) in case a registration is ever approved.

2. **Unauthenticated JSON from reddit.com** -- the second plan, and the first thing built.
   It works *until it doesn't*: Reddit rate-limits logged-out access per IP, and once an
   IP trips the limit (about 40 requests in a couple of minutes did it during
   verification) every JSON endpoint stays blocked for hours -- `www.reddit.com` answers
   403 with an HTML interstitial and `old.reddit.com` redirects to `/login?reason=lor2`
   ("logged-out rate limit"). Measured: still blocked three hours later at one probe
   request every five minutes. Browser-impersonated TLS fingerprints didn't help; RSS
   feeds were served but are rate-limited too, and for gallery posts (most of both subs)
   RSS only carries a 140px thumbnail. Glance survives on the same WAN IP because it
   caches for 30 minutes and only refreshes on page view -- a much smaller footprint than
   a comment-thread-per-post sync. Kept as `REDDIT_BACKEND=direct` with a per-IP cooldown
   (doubling from 30 min after each blocked run) so cron can never poke a blocked IP.

3. **Arctic Shift** (`https://arctic-shift.photon-reddit.com`) -- the default now. It is a
   public, volunteer-run archive of Reddit with a documented JSON API:
   `/api/posts/search?subreddit=&limit=100&sort=desc&sort_type=created_utc` (page back
   with `before=<unix ts>`), and `/api/comments/tree?link_id=<post id>&limit=1000`. Posts
   appear within about an hour of being made; a second retrieval pass roughly a day
   later refreshes the post's score/comment count and the comment tree with real
   scores. Verified on 2026-09-17 against r/MoviesThatFeelLike: a 591-comment thread came
   back as a 608-node tree with scores up to 259 and depths to 4; gallery
   `media_metadata` is present so every image can be fetched at full size from
   `i.redd.it` / `preview.redd.it` (the image CDN is not rate-limited the way the API
   is). Limits: 100 items per page, recency sort only (the app does its own "hot"
   ranking), no `score` sort. One request per listing page plus one per comment tree.

The earlier line here that said "no third-party middleman" was written with *paid
scraping services* in mind. Arctic Shift is a different thing: free, open, documented,
and the only path that gives full data without playing cat-and-mouse with Reddit's
anti-bot layer. The app identifies itself honestly to it (`ARCHIVE_USER_AGENT`), paces
at about one request per second, and caps how many comment threads a sync run fetches
(`--max-comments`, default 40, split evenly across sources). Deleted posts live on in
the archive with their images gone from the CDN; the sync hides any post whose images
all fail to download.

All three backends implement the same two calls (`listing`, `post_with_comments`) and
return the same dict shapes, so the sync code doesn't know which one it's talking to.
`REDDIT_BACKEND=auto` (the default) means PRAW if OAuth credentials exist, else the
archive.

## Sync design

`manage.py sync_reddit` -- a Django management command, run periodically. In the compose
stack it runs from a tiny sidecar container that loops `sync; sleep 1800`
(`docker/sync-loop.sh`); on a bare host a cron line does the same job. Not Celery/Redis:
every other automation in this homelab is a cron-driven Python script, and a single-user
tool pulling two subreddits every half hour does not need a task queue.

Per run, per enabled `Source`:

1. Fetch the listing (one request) and upsert `Post` rows (title, score, comment count,
   trimmed raw JSON, image URLs extracted from `media_metadata` / `url` / `preview`).
2. Queue comment fetches: new posts first, then any post whose comment count has grown
   by 3+ since its last fetch and that hasn't been fetched in the last 6 hours. The queue
   is capped per run (`--max-comments`, split evenly across sources) so a cold start
   spreads over several runs rather than one huge burst.
3. Optionally (`--backfill N`), page N pages further back using the oldest post
   already stored as the cursor (`ArchiveClient.listing(..., before=<ts>)`); stops
   early once a page has no new posts or the archive is exhausted. Only the archive
   backend supports this (`Client.supports_backfill`) -- Reddit's own listings
   aren't a simple timestamp cursor. Comment fetches for backfilled posts still go
   through the normal per-run cap, so a big backfill queues up over several runs.
4. For each queued post, fetch the comment tree (one request), store the flattened
   comments on the post (`Post.comments`, so the parser can be re-run offline), and
   rebuild its `Recommendation` rows -- preserving rows a human has edited or pushed to a
   service, and manual additions.
5. Download any uncached images to `MEDIA_ROOT` and write a 640px JPEG thumbnail for
   the grid, up to `limit` (200) per run and only while `Syncer._disk_has_room()` says
   there's headroom (see below). `PostImage.display_url` falls back to the original
   Reddit CDN URL when nothing's cached yet, which is what makes it safe to throttle
   caching under disk pressure without the grid going blank. Posts whose images have
   all vanished are hidden.

**Disk safety.** These subreddits are high-volume enough that a deep `--backfill` can
create far more pending images than fit on a shared homelab disk -- listings and
comments are KB-scale DB rows, but cached images are the one part of this pipeline with
real disk impact. `cache_images()` checks free space under `DATA_DIR` before starting
and again every 40 successful caches within a run (a single run can be asked to cache
up to `limit` images, so a big backlog needs a mid-run recheck, not just a check at the
top), and skips the rest of that run's caching (logging a warning) once free space drops
below `MIN_FREE_DISK_GB` (default 2.0). Everything else in the sync keeps working
regardless -- new posts, comments and recommendations still land, and the grid still
shows something for every post via the CDN fallback above; only local caching pauses.

Every run is recorded as a `SyncRun` (counts, request total, log, blocked flag). A
running `SyncRun` acts as the lock against overlapping runs; a run stuck for over two
hours is marked failed automatically. The Settings page has a "Sync now" button that
starts a run in a background thread and polls the log.

`manage.py reparse_recommendations` re-runs the parser over stored comments with no
network -- use it after improving the heuristics. `manage.py bootstrap` creates the two
default sources and seeds service settings from `.env`; it is idempotent and runs on
every container start.

## Data model (sketch)

- **`Source`** — `movies` | `music`, each pointing at one subreddit name. Kept as data
  rather than hardcoded so a subreddit swap doesn't need a code change.
- **`Post`** — Reddit post ID (unique), source FK, title, permalink, author, score,
  num_comments, created_utc, last_synced_at.
- **`PostImage`** — post FK, image URL, order (for galleries), is_primary (which one
  shows on the tile).
- **`Recommendation`** — post FK, raw comment text, comment score, comment permalink,
  **parsed_title** (best-guess extracted title), **parsed_year** (movies) /
  **parsed_artist** (music), a confidence/method field (which heuristic produced it, for
  debugging), and a user-facing `included` boolean (ticked/unticked in the curation UI —
  see below) that export/integration actions respect.
- **`ServiceConfig`** — one row per integration (Radarr/Lidarr/Jellyfin/Navidrome): base
  URL + credential fields. Credential fields are **encrypted at rest** with a Fernet key
  from `CREDENTIAL_ENCRYPTION_KEY` (see `.env.example`) via a custom encrypted model
  field — not stored in plain text in the database even though this only ever runs on
  the private LAN, because "it's only ever on the LAN" is exactly the assumption that
  stops holding the day a backup, a misconfigured share, or a future public-facing
  tunnel exposes the DB file.

## Recommendation parsing: heuristic, not magic

There is no reliable way to extract a clean "Artist – Title" or "Movie Title (Year)" from
freeform natural-language comments 100% of the time — people write "Anything by Boards of
Canada tbh" as often as a clean title. So the parser is designed as a **candidate
generator**, and the vibe detail page's curation step is where the real cleanup happens
(tick/untick/edit before anything is exported or pushed to a *arr app) — this is *by
design*, not a gap to close later.

Heuristics, roughly in order of confidence:
1. **Embedded links** — `open.spotify.com/track|album`, `music.youtube.com`,
   `youtube.com/watch`, `imdb.com/title`, `letterboxd.com/film` in a comment are the
   highest-confidence signal; where feasible, fetch the link's oEmbed/OpenGraph title
   rather than trusting the comment's own wording of it.
2. **"Artist - Title" / "Title (Year)" patterns** — regex over comment text for the
   conventional shorthand people already use in these subs.
3. **Quoted or Title-Case runs** — lower confidence, surfaced but visually flagged as
   "unconfirmed" in the UI.
4. Comments that don't match anything are simply not turned into recommendation
   candidates (they stay visible as raw comments on the detail page, just not promoted
   into the actionable list).

Every extracted candidate keeps a link back to the source comment (text + permalink +
score) so a human can sanity-check it in one click without leaving the page.

## Export formats

- **CSV / TXT** — straightforward serialization of the *currently included* (ticked)
  recommendations on a vibe detail page: title, parsed artist/year, source comment score,
  permalink.
- **M3U / M3U8** — standard extended-M3U (`#EXTM3U` + `#EXTINF:-1,Artist - Title` per
  entry). Where a track has been resolved to a real local file via the Navidrome/Lidarr
  library match (see below), that resolved path is used as the entry's URI; otherwise the
  entry is metadata-only (title as both the `#EXTINF` comment and the URI line) — honest
  about what could and couldn't be resolved locally, rather than pretending every
  recommendation exists in the library.

## Integrations

All five are opt-in per vibe detail page or per recommendation (nothing is pushed
anywhere without an explicit action), and all read their connection details from
`ServiceConfig`.

**Not import lists.** Radarr/Lidarr both support "Import Lists" -- a mechanism where
Radarr/Lidarr itself polls an external URL on a schedule and auto-imports whatever's on
it (a Trakt watchlist, an IMDb list). That's the wrong shape for this app: the whole
point is curating *specific* recommendations out of a specific vibe post, not
continuously following a blanket external list. Instead, every add is a direct,
explicit API call (`lookup` → `POST`) triggered by a button press, exactly like every
other integration here -- see "Per-recommendation actions" below for the individual
(as opposed to whole-post) version of that click.

- **Radarr / Lidarr** — for each included recommendation: `GET /api/v3/movie/lookup` (Radarr)
  or `GET /api/v1/artist/lookup` (Lidarr) by search term, then `POST` to add if not
  already present (using a configured default root folder + quality profile — surfaced as
  Settings-page choices, not hardcoded), monitored, with an immediate search triggered.
  Reuses the same "does this already exist" checks this homelab's other Lidarr/Radarr
  tooling already relies on, rather than blindly re-adding duplicates.
- **Jellyfin** — search the library (`/Items?searchTerm=`) for each included title, collect
  matched item IDs, `POST /Playlists` to create a new playlist from the matches. Titles
  that don't match anything in the library are reported back to the user, not silently
  dropped.
- **Navidrome** — same idea via the Subsonic API: `search3.view` per title, then
  `createPlaylist.view` with the resolved song IDs.
- **slskd** (music only) — for each included recommendation: `POST /api/v0/searches`,
  poll `GET /api/v0/searches/{id}` until complete or a configurable timeout (default 15s)
  elapses, rank the file results, then `POST /api/v0/transfers/downloads/{username}` for
  the winner (or just report it, if "auto-download" is turned off on the Settings page).
  **Non-obvious API behaviour, found by testing live rather than guessing**:
  `GET .../responses` returns `[]` -- not partial results -- until the search itself
  reports `isComplete`, and an unpopular query can run for slskd's own internal timeout
  (~25-30s observed) well past a sane UI wait. `PUT .../searches/{id}` cancels a search
  early *and* marks it complete, unlocking whatever arrived so far; without this, every
  search that doesn't finish inside our own timeout silently returns nothing, which is
  exactly what happened on first deploy against genuinely findable tracks (fixed and
  covered by a regression test). Ranking favours availability over a marginal quality
  gain -- a peer with no free upload slot and a long queue may take hours or never
  finish if they go offline, so `hasFreeUploadSlot` and `queueLength` are weighted more
  heavily than format/bitrate, after a similarity + extension + length filter rules out
  non-audio files and obvious mismatches. Soulseek is a live P2P network, so result
  counts genuinely vary run to run for the same query -- that variability is expected,
  not a bug. Because a search can take several seconds, a single push time-budgets
  ~100s and processes only as many recommendations as fit (`SLSKD_MAX_PER_PUSH`, 6 at
  the default 15s timeout, fewer if the timeout is raised) and skips ones already
  queued from an earlier push, so working through a long list is a few clicks rather
  than one request that risks the gunicorn worker timeout.

Every integration action returns a per-item result (added / already existed / not found /
error) rather than a single pass/fail for the whole batch — with a curated list of maybe
10-30 items, "3 of 12 didn't match, here's which ones" is far more useful than a silent
partial success.

### Per-recommendation actions

Every row also carries an "add just this one" dropdown (`integrations/push.py`'s
`push_one`), independent of the bulk buttons and of the row's own included/excluded
checkbox -- a direct click is its own instruction, not gated by whether the row happens
to be ticked for bulk export. Radarr/Lidarr/slskd reuse the exact same per-item client
methods the bulk path uses (`_push_arr` / `_push_slskd` called with a one-item list --
slskd's dynamic batch-size cap naturally floors at 1, so no special-casing was needed).

Jellyfin/Navidrome are different: the *bulk* button always creates a fresh playlist
(the whole point is "make a playlist from this vibe post"), but a *per-track* click
needs the opposite default -- add to a playlist you keep reusing, not spin up a new
single-song playlist every time. `add_or_create_playlist(name, ids)` looks up an
existing playlist by exact (case-insensitive) name via `find_playlist` and adds to it
(`POST /Playlists/{id}/Items` / Subsonic `updatePlaylist`) if found, otherwise creates
one -- both verified live. Leaving the name prompt blank falls back to
`DEFAULT_PLAYLIST_NAME` ("MediaThatFeelsLike Picks"), so accepting the default on every
click naturally builds one running collection.

The only feedback for a per-row action is that row's own updated chips (no separate
results banner) -- `push_rec` re-renders and returns just the `<li>` for that
recommendation, via `integrations.models.service_flags(kind)`, the same capability
computation the bulk buttons and every htmx rec-list partial share (added specifically
so the per-row dropdown keeps working after any toggle/edit/reparse/etc. swap, not just
on the initial page load).

## Glance dashboard widget

`vibes/api.py` is a small, deliberately unstable JSON API (no versioning, no auth beyond
"LAN-only, no login" like the rest of the app) whose only real consumer is a Glance
`custom-api` widget: `GET /api/hot/<movies|music>/?limit=N` (the N hottest posts with a
locally cached image, for an image-strip widget) and `GET /api/stats/` (post/rec counts
+ last sync, for a stats tile). Kept separate from `vibes/views.py` because it's a
different kind of surface (JSON contract for an external renderer, not HTML for this
app's own pages), even though it reuses the same `_hot_key` ranking as the tile grid so
the widget and the grid agree on what's "hot".

`hot()` deliberately doesn't use `Post.primary_image` (which falls back to the raw
Reddit CDN URL when nothing's cached locally yet) -- an image that's fine to eventually
appear in this app's own grid isn't guaranteed to load reliably for an external viewer
right now, so the API only returns posts with an actually-cached file.

**Deployed as**: on the Glance Media page (`/opt/glance/config/pages/media.yml` on the
services LXC), a stats tile (small column, replacing what used to be a bare
connectivity-check `monitor` tile) plus two full-width image-strip widgets --
"MediaThatFeelsLike · Movies" and "· Music" -- placed right after "Continue watching"
for visibility. Both strips reuse `.wb-card`/`.wb-strip`, this homelab's shared
custom-widget CSS also used by the Navidrome/Jellyfin strips on the same page.

`integrations/api.py` is the same idea for slskd: the Media page already had a hand-built
widget hitting slskd's own `/api/v0/transfers/downloads` directly from a Glance template
(file-level DOWNLOADING/QUEUED/DONE/FAILED counts via nested `range`+`add`), but Soulseek
groups activity by peer-then-directory-then-file, and template languages are a poor place
to do that grouping -- a 150-track album grab would need to become one progress row, not
150. `GET /integrations/api/slskd/` does the aggregation in Python instead: same
per-file counts (unchanged, so the existing numbers on the dashboard don't move), plus an
`active` list that collapses each (peer, directory) still in flight into one row with an
overall percent (bytes transferred / bytes total across every file in that grab, not just
the ones currently moving -- so a partially-succeeded, partially-queued album still shows
sensible progress) and a human-readable speed. Reuses `integrations.push.client_for` for
the same Fernet-stored URL/API key the Settings page already manages, rather than a
separate credential (the existing widget used its own `${HOMEPAGE_VAR_SLSKD_KEY}`).

## App layout

```
MediaThatFeelsLike/
├── config/            # settings (django-environ), urls, wsgi
├── vibes/             # Source, Post, PostImage, Recommendation; tile grid, detail page,
│                      # htmx curation endpoints (toggle / edit / add / delete / bulk /
│                      # re-parse / refresh)
├── reddit_sync/       # client.py (Archive / Fetch / Praw backends), images.py (URL
│                      # extraction + caching/thumbnails), parser.py (heuristics),
│                      # sync.py (the Syncer), SyncRun model, management commands
│                      # sync_reddit / reparse_recommendations / bootstrap
├── integrations/      # ServiceConfig (Fernet-encrypted fields), clients/ for Radarr,
│                      # Lidarr, Jellyfin, Navidrome, push.py orchestration, Settings page
├── exports/           # CSV / TXT / M3U / M3U8 downloads
├── templates/         # base layout; app templates live in each app
├── static/            # app.css (no build step) and a vendored htmx
├── docker/            # entrypoint.sh (migrate/bootstrap/collectstatic), sync-loop.sh
└── tests/             # pytest: parser, sync pipeline (fake client), views/exports,
                       # integration clients (stubbed HTTP)
```

## Deployment

`Dockerfile` + `compose.yaml`: an `app` service (gunicorn, 2 workers x 4 threads, port
8095 on the host) and a `sync` sidecar sharing the same image and the `./data` volume
(sqlite database + cached images by default). There is no login -- the app is LAN-only,
like Glance and Homepage in this homelab; service credentials are encrypted at rest,
never rendered back into the browser, and the Django admin (which does have a login)
hides them too.

`DJANGO_ALLOWED_HOSTS` must list the LAN IP / hostname the app is reached on.

**Splitting media cache storage from the DB.** Cached images are comfortably the
largest and fastest-growing thing this app stores (a deep `--backfill` on these
high-volume subs can queue tens of thousands of them), so they don't have to live next
to the sqlite DB. Set `MEDIA_DATA_DIR` (Django) / `MEDIA_HOST_PATH` (`compose.yaml`, the
host-side path bind-mounted to `/media-cache` in both containers) to point them at
different, larger, and likely slower storage instead -- a network/NFS-attached drive
shared with the rest of the media library, for instance. `Syncer._disk_has_room()`
checks whichever filesystem `MEDIA_ROOT` actually resolves to, so the disk-safety floor
(`MIN_FREE_DISK_GB`) stays correct either way. Moving an *existing* cache means copying
`./data/media`'s contents to the new location yourself before switching
`MEDIA_HOST_PATH` and recreating the containers -- nothing does that automatically, and
nothing breaks if you don't (uncached images just fall back to hotlinking Reddit's CDN
per `PostImage.display_url`, same as any other uncached image).

**Deployed** at `/opt/mediathatfeelslike` on the services LXC (`192.168.1.110:8095`),
pulling `wb20244/mediathatfeelslike` from Docker Hub rather than building on that LXC's
disk (it runs tight on space -- check `df -h /` before building there again). Not yet
behind Nginx Proxy Manager. On the Glance dashboard's Media page: a stats tile plus
"MediaThatFeelsLike · Movies"/"· Music" image-strip widgets (see "Glance dashboard
widget" above) -- not the "More" tile nearby, which is reserved for NSFW-adjacent tools
under a deliberately unobvious name.

Image build/push doesn't need Docker on the dev box: `git archive` the committed tree,
`pscp` it to the LXC (which already runs Docker for the other services and already had a
Docker Hub token for `wb20244` configured), `docker build`, `docker push`. Clean up the
temp build dir and local image tags afterward given the disk headroom.

## Known limitations / next steps

- The archive's first pass records a fresh post with score 1 and 0 comments; real
  numbers arrive with its second pass about a day later, so the "Hot" ordering is only
  meaningful for posts older than that.
- The Lidarr push adds the *album/single* that carries a recommended track (Lidarr has
  no per-track concept); when no album can be identified the artist is added
  unmonitored for hand-picking. "Anything by X" recommendations are handled the same way
  (configurable to monitor all/latest).
- Jellyfin here has no music library, so music playlists are Navidrome's job; the
  Jellyfin button is still shown for music posts and will simply report "not in the
  library".
- Playlist buttons use a browser `prompt()` for the name -- fine for a person, awkward
  for automation.
