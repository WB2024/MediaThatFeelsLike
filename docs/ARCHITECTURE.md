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

## Reddit access: unauthenticated fetch, not the official API

**Revised 2026-09-17.** The original plan here was PRAW against Reddit's official OAuth
API. That's now blocked in practice: Reddit closed self-service developer app
registration in November 2025 under its "Responsible Builder Policy" — the
`reddit.com/prefs/apps` "create app" form now rejects essentially all new personal-use
app registrations (confirmed directly: hit this wall trying to register this project's
own app). This wasn't a mistake in the form submission; it's a Reddit-side policy change
that arrived after the original recommendation was made. Recorded here so nobody
re-discovers this the hard way a second time.

**What still works, verified directly against this exact network on 2026-09-17:**
Unauthenticated fetches from `old.reddit.com` (JSON endpoints) succeed when the request
carries a realistic desktop browser `User-Agent` and is paced sensibly — no burst
traffic. Evidence: Glance (this homelab's dashboard) live-fetches 25+ subreddits this way
on a 30-minute cache with zero issues. The one time this project's own scripted checks
got soft-blocked was after ~40 rapid-fire verification requests in a couple of minutes
from one IP — a burst pattern this app will never produce, since it only needs to check
two subreddits every so often.

Design consequences:

- **Realistic `User-Agent`** on every request (a real Chrome/Firefox desktop string, not
  a polite "AppName/1.0 by u/username" — that convention is for the OAuth API's own
  etiquette, not for unauthenticated fetches, and using it here would make requests
  *more* identifiable as a bot, not less).
- **One request per post for full detail** — `old.reddit.com/r/<sub>/comments/<id>/.json`
  returns the post *and* its full comment tree in a single call, avoiding a separate
  listing + comments round trip per post.
- **Cache aggressively, re-check rarely.** A post's comments/images don't need
  re-fetching once synced; only score/comment-count are worth periodically refreshing
  (and even that on a slow cadence, e.g. once a day), which keeps total request volume
  low regardless of how large the archive grows.
- **Self-throttle with multi-second gaps** between requests within a sync run, and back
  off (skip the rest of that run, try again next scheduled run) on any non-200 response
  rather than retrying immediately — never turn a transient hiccup into the burst pattern
  that causes blocks in the first place.
- **Keep the official-API door open, but don't depend on it.** `REDDIT_CLIENT_ID` /
  `REDDIT_CLIENT_SECRET` stay in `.env.example` as optional — if a Reddit app
  registration ever does get approved (worth submitting anyway since it costs nothing),
  the sync command should prefer it automatically. Nothing in the design should require
  it.
- **No third-party scraping-API middleman.** Paid scraping services exist for this
  exact problem, but they cost money on an ongoing basis, add a dependency on another
  company's ToS/business model, and route this app's subreddit reading through a third
  party — all three cut against the point of self-hosting. Only worth reconsidering if
  direct unauthenticated fetching stops working entirely.

## Sync design

A Django management command (`sync_reddit` or similar), run periodically by **cron** —
not Celery/Redis. This matches how every other automation in this homelab already works
(the Lidarr drip search, queue janitor, unmapped janitor, path fixer are all cron-driven
Python scripts with self-throttling state files), and a single-user tool pulling two
subreddits every N minutes does not need a task queue's operational overhead.

Per sync run, per subreddit:
1. Fetch the `hot` (and optionally `top` / `new` — configurable) listing page for new
   post IDs — one lightweight request.
2. For each post not already stored (dedupe on Reddit's post ID), fetch that post's own
   `/comments/<id>/.json` (post + full comment tree in one call). Extract image URL(s) —
   handling `is_gallery` posts (multiple images via `media_metadata`), single-image posts
   (`url_overridden_by_dest` / `preview.images[0].source.url`), and skip text-only posts
   with no image (nothing to tile).
3. Parse the fetched comments (sorted by score, capped at some count — e.g. top 50) for
   the recommendation parser.
4. Store post metadata (title, score, comment count, permalink, created time) +
   image(s) + raw comment text for parsing. Pace requests with a multi-second delay
   between posts; stop the run early on any non-200 response instead of retrying.

Re-sync of an existing post occasionally refreshes score/comment count (posts age and
gain recommendations over time) without re-parsing comments that haven't changed, to
keep total request volume low no matter how large the archive gets.

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

## App layout (planned Django apps)

```
mediathatfeelslike/
├── config/                # Django project settings/urls/wsgi
├── vibes/                 # Post, PostImage, Recommendation, Source models + the
│                          # tile-grid and detail-page views (the core UI)
├── reddit_sync/           # unauthenticated fetch client (+ optional PRAW/OAuth path
│                          # if an app registration ever gets approved), the sync
│                          # management command, comment parser
├── integrations/          # Radarr/Lidarr/Jellyfin/Navidrome clients + ServiceConfig
│                          # model + the settings page
└── exports/               # CSV/TXT/M3U serializers
```

## Deployment (once there's something to deploy)

Matches every other service in this homelab rather than inventing a new pattern:
Docker container on the services LXC (`192.168.1.110`), `gunicorn` behind the existing
Nginx Proxy Manager (`<app>.wbhomelab`), sqlite for the database (fine at this scale — a
single user, a few thousand posts), cron entry for the sync command, and a homepage
tile once it's live.

## Open questions for when the code lands

- Exact sync cadence and post-count-per-sync — needs to stay comfortably low-volume
  given unauthenticated access is being relied on (see above); not a hard problem, just
  needs a conservative number chosen deliberately rather than maximized.
- Whether `top` (of week/month) is worth pulling alongside `hot`, since these subs'
  best content may not stay on `hot` for long.
- Default Radarr/Lidarr root folder + quality profile — pull from existing instances'
  config at setup time rather than asking the user to retype what's already configured.
- Submit the Reddit developer app registration anyway (it's free) and just let the sync
  command use it automatically if/when it's ever approved — no downside to having it
  pending in the background.
