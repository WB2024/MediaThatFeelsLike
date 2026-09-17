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

Flow: tile grid of post images → click a tile → **vibe detail page** (image at top, then
a cleaned list of recommendations pulled from that post's comments) → export the list or
push it into Radarr/Lidarr/Jellyfin/Navidrome.

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
3. For each queued post, fetch the comment tree (one request), store the flattened
   comments on the post (`Post.comments`, so the parser can be re-run offline), and
   rebuild its `Recommendation` rows -- preserving rows a human has edited or pushed to a
   service, and manual additions.
4. Download any uncached images to `MEDIA_ROOT` and write a 640px JPEG thumbnail for
   the grid (the tile grid never hotlinks Reddit's CDN). Posts whose images have all
   vanished are hidden.

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

All four are opt-in per vibe detail page (nothing is pushed anywhere without an explicit
action), and all read their connection details from `ServiceConfig`.

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

Every integration action returns a per-item result (added / already existed / not found /
error) rather than a single pass/fail for the whole batch — with a curated list of maybe
10-30 items, "3 of 12 didn't match, here's which ones" is far more useful than a silent
partial success.

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
(sqlite database + cached images). There is no login -- the app is LAN-only, like Glance
and Homepage in this homelab; service credentials are encrypted at rest, never rendered
back into the browser, and the Django admin (which does have a login) hides them too.

`DJANGO_ALLOWED_HOSTS` must list the LAN IP / hostname the app is reached on.

**Deployed** at `/opt/mediathatfeelslike` on the services LXC (`192.168.1.110:8095`),
pulling `wb20244/mediathatfeelslike` from Docker Hub rather than building on that LXC's
disk (it runs tight on space -- check `df -h /` before building there again). Not yet
behind Nginx Proxy Manager. Listed on the Glance dashboard's Media page as its own
`monitor` tile (**not** the "More" tile next to it -- that one is reserved for
NSFW-adjacent tools under a deliberately unobvious name).

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
