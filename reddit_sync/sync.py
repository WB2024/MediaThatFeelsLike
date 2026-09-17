"""
The sync itself: listing → posts → comments → recommendations → cached images.

Request budget per run (unauthenticated path): 1 per source listing + 1 per post whose
comments are (re)fetched, capped by `max_comment_fetches`. With ~4-6 s pacing, a run
that fetches comments for 40 posts takes 3-4 minutes -- fine for cron, and it's why the
cap exists rather than trying to catch everything up at once.
"""

import logging
import threading
import traceback

from django import db
from django.utils import timezone

from vibes.models import Post, PostImage, Recommendation, Source

from . import parser
from .client import RedditBlocked, RedditError, RedditNotFound, get_client, utc_datetime
from .images import ImageCacher, extract_images
from .models import SyncRun

log = logging.getLogger(__name__)

# Keys of the listing JSON worth keeping on Post.raw (enough to re-extract images later).
RAW_KEEP = (
    "id", "title", "url", "url_overridden_by_dest", "post_hint", "is_gallery", "media_metadata",
    "gallery_data", "thumbnail", "link_flair_text", "author", "permalink", "created_utc",
    "over_18", "is_video", "domain", "is_self",
)


def trim_raw(post):
    raw = {k: post[k] for k in RAW_KEEP if k in post}
    preview = (post.get("preview") or {}).get("images") or []
    if preview:
        raw["preview"] = {"images": [{"source": preview[0].get("source", {})}]}
    return raw


class Syncer:
    def __init__(self, run, client=None, cacher=None, max_comment_fetches=40, refresh=False, cache_images=True, backfill_pages=0):
        self.run = run
        self.client = client or get_client()
        self.cacher = cacher or (ImageCacher() if cache_images else None)
        self.max_comment_fetches = max_comment_fetches
        self.refresh = refresh
        self.backfill_pages = backfill_pages
        self.comment_fetches = 0
        self.blocked = False

    # -- logging ----------------------------------------------------------------------

    def log(self, msg, level=logging.INFO):
        log.log(level, msg)
        self.run.append_log(f"{timezone.now():%H:%M:%S} {msg}")

    def _save_counts(self):
        self.run.requests_made = self.client.requests_made
        self.run.save(update_fields=["posts_seen", "posts_new", "comments_fetched", "images_cached", "recommendations_created", "requests_made", "errors"])

    # -- top level --------------------------------------------------------------------

    def run_sources(self, sources):
        self.run.sources = ", ".join(f"r/{s.subreddit}" for s in sources)
        self.run.save(update_fields=["sources"])
        per_source = max(1, -(-self.max_comment_fetches // max(1, len(sources))))  # ceil
        try:
            for source in sources:
                if self.blocked:
                    break
                self.source_cap = min(self.max_comment_fetches, self.comment_fetches + per_source)
                self.sync_source(source)
            if self.cacher and not self.blocked:
                self.cache_images()
        except Exception:
            self.run.errors += 1
            self.log("Unhandled error:\n" + traceback.format_exc(), logging.ERROR)
            self.run.status = SyncRun.Status.FAILED
        else:
            self.run.status = SyncRun.Status.PARTIAL if (self.run.errors or self.blocked) else SyncRun.Status.OK
        finally:
            self.run.blocked = self.blocked
            self.run.finished_at = timezone.now()
            self._save_counts()
            self.run.save()
            self.log(
                f"Done: {self.run.status}. posts seen {self.run.posts_seen}, new {self.run.posts_new}, "
                f"comments fetched {self.run.comments_fetched}, recs {self.run.recommendations_created}, "
                f"images {self.run.images_cached}, requests {self.run.requests_made}, errors {self.run.errors}"
            )
        return self.run

    # -- per source -------------------------------------------------------------------

    def sync_source(self, source, limit=None):
        self.log(f"r/{source.subreddit}: fetching {source.listing} listing (limit {limit or source.fetch_limit})")
        try:
            posts = self.client.listing(
                source.subreddit, sort=source.listing, limit=limit or source.fetch_limit,
                time_filter=source.time_filter,
            )
        except RedditBlocked as exc:
            self.blocked = True
            self.run.errors += 1
            self.log(f"r/{source.subreddit}: blocked ({exc}); stopping this run", logging.WARNING)
            return
        except RedditError as exc:
            self.run.errors += 1
            self.log(f"r/{source.subreddit}: listing failed ({exc})", logging.WARNING)
            return

        new_posts, existing_posts = self._ingest(source, posts)
        self.log(f"r/{source.subreddit}: {len(posts)} posts in listing, {len(new_posts)} new")

        if self.backfill_pages and not self.blocked:
            new_posts += self._backfill(source)

        # Comments: new posts first, then anything whose thread has grown.
        queue = new_posts + [p for p in existing_posts if self.refresh or p.needs_comment_fetch]
        for post in queue:
            if self.comment_fetches >= getattr(self, "source_cap", self.max_comment_fetches):
                self.log(f"r/{source.subreddit}: comment fetch cap reached; {len(queue) - queue.index(post)} posts wait for the next run")
                break
            if not self.sync_comments(post):
                break

        source.last_synced_at = timezone.now()
        source.save(update_fields=["last_synced_at"])

    def _ingest(self, source, posts):
        """Upsert a page of listing results. Returns (new_posts, existing_posts)."""
        new_posts, existing_posts = [], []
        for data in posts:
            if data.get("stickied") or data.get("is_self") and not data.get("selftext") and not extract_images(data):
                continue
            post, created = self.upsert_post(source, data)
            (new_posts if created else existing_posts).append(post)
        self.run.posts_seen += len(new_posts) + len(existing_posts)
        self.run.posts_new += len(new_posts)
        self._save_counts()
        return new_posts, existing_posts

    def _backfill(self, source):
        """Page further back into the subreddit's history using the oldest post we
        already have as the cursor. Only the archive backend supports this -- Reddit's
        own listings aren't a simple timestamp-ordered cursor. Comment threads for
        whatever this pulls in still go through the normal per-run fetch cap, so a big
        backfill just queues up over several runs rather than fetching everything at once.
        """
        if not getattr(self.client, "supports_backfill", False):
            self.log(f"r/{source.subreddit}: backfill skipped -- {type(self.client).__name__} can't page by time")
            return []

        all_new = []
        for page in range(self.backfill_pages):
            oldest = source.posts.order_by("created_utc").values_list("created_utc", flat=True).first()
            if oldest is None:
                break
            try:
                older = self.client.listing(
                    source.subreddit, sort=source.listing, limit=source.fetch_limit,
                    time_filter=source.time_filter, before=int(oldest.timestamp()),
                )
            except RedditBlocked as exc:
                self.blocked = True
                self.run.errors += 1
                self.log(f"r/{source.subreddit}: backfill blocked ({exc}); stopping", logging.WARNING)
                break
            except RedditError as exc:
                self.run.errors += 1
                self.log(f"r/{source.subreddit}: backfill page {page + 1} failed ({exc})", logging.WARNING)
                break
            if not older:
                self.log(f"r/{source.subreddit}: backfill reached the start of the archive")
                break
            new_posts, _ = self._ingest(source, older)
            self.log(f"r/{source.subreddit}: backfill page {page + 1}/{self.backfill_pages}: {len(older)} posts, {len(new_posts)} new")
            all_new += new_posts
            if not new_posts:
                # Nothing new on this page -- we've already caught up to here before.
                break
        return all_new

    def upsert_post(self, source, data):
        defaults = {
            "source": source,
            "title": (data.get("title") or "")[:500],
            "author": data.get("author") or "",
            "permalink": data.get("permalink") or "",
            "url": (data.get("url_overridden_by_dest") or data.get("url") or "")[:1000],
            "selftext": data.get("selftext") or "",
            "flair": (data.get("link_flair_text") or "")[:100],
            "score": data.get("score") or 0,
            "upvote_ratio": data.get("upvote_ratio"),
            "num_comments": data.get("num_comments") or 0,
            "created_utc": utc_datetime(data.get("created_utc")),
            "is_gallery": bool(data.get("is_gallery")),
            "is_video": bool(data.get("is_video")),
            "nsfw": bool(data.get("over_18")),
            "raw": trim_raw(data),
            "last_seen_at": timezone.now(),
        }
        post, created = Post.objects.update_or_create(reddit_id=data["id"], defaults=defaults)
        if created or not post.images.exists():
            self.ensure_images(post, data)
        return post, created

    def ensure_images(self, post, data):
        for order, img in enumerate(extract_images(data)):
            PostImage.objects.update_or_create(
                post=post, order=order,
                defaults={
                    "source_url": img["url"][:1000], "width": img.get("width"), "height": img.get("height"),
                    "caption": (img.get("caption") or "")[:500],
                },
            )

    # -- comments → recommendations ---------------------------------------------------

    def sync_comments(self, post):
        """Fetch one post's comment thread and rebuild its recommendations. Returns False
        when the run should stop (blocked)."""
        try:
            data, comments = self.client.post_with_comments(post.source.subreddit, post.reddit_id)
        except RedditBlocked as exc:
            self.blocked = True
            self.run.errors += 1
            self.log(f"{post.reddit_id}: blocked ({exc}); stopping this run", logging.WARNING)
            return False
        except RedditNotFound:
            self.log(f"{post.reddit_id}: gone (404); hiding")
            post.hidden = True
            post.save(update_fields=["hidden"])
            return True
        except RedditError as exc:
            self.run.errors += 1
            self.log(f"{post.reddit_id}: comment fetch failed ({exc})", logging.WARNING)
            return True
        finally:
            self.comment_fetches += 1

        self.run.comments_fetched += 1
        if data:
            post.score = data.get("score") or post.score
            post.num_comments = data.get("num_comments") or post.num_comments
            post.upvote_ratio = data.get("upvote_ratio", post.upvote_ratio)
        post.comments = comments
        post.num_comments = max(post.num_comments, len(comments))
        post.comments_fetched_at = timezone.now()
        post.comments_fetched_count = post.num_comments
        created = self.rebuild_recommendations(post)
        post.save()
        self.run.recommendations_created += created
        self._save_counts()
        self.log(f"{post.reddit_id}: {len(comments)} comments -> {created} new recommendations ({post.title[:50]!r})")
        return True

    def rebuild_recommendations(self, post):
        """Run the parser over post.comments and reconcile with existing rows: rows a
        human has edited or pushed to a service are preserved; the rest are replaced."""
        kind = post.source.kind
        candidates = []
        for comment in post.comments:
            if comment.get("author") == "AutoModerator" or comment.get("distinguished") or comment.get("stickied"):
                continue
            for cand in parser.parse_comment(comment.get("body", ""), kind, comment.get("score", 0), comment.get("depth", 0)):
                cand.comment = comment
                candidates.append(cand)
        candidates = parser.dedupe(candidates)

        existing = {parser.normalise_key(r.parsed_artist, r.parsed_title): r for r in post.recommendations.all()}
        keep_ids, created = set(), 0
        for order, cand in enumerate(candidates):
            row = existing.get(cand.key)
            comment = cand.comment
            fields = {
                "comment_id": comment.get("id", ""),
                "comment_author": comment.get("author", "")[:80],
                "comment_score": comment.get("score") or 0,
                "comment_depth": comment.get("depth") or 0,
                "comment_permalink": (comment.get("permalink") or "")[:500],
                "raw_text": comment.get("body", ""),
                "snippet": cand.snippet[:300],
                "method": cand.method,
                "confidence": cand.confidence,
                "mention_count": cand.mention_count,
                "order": order,
            }
            if row is None:
                row = Recommendation(
                    post=post, parsed_title=cand.title[:300], parsed_artist=cand.artist[:200],
                    parsed_year=cand.year, parsed_url=cand.url[:1000],
                    included=cand.confidence >= parser.INCLUDE_THRESHOLD, **fields,
                )
                row.save()
                created += 1
            else:
                if not row.edited:
                    row.parsed_year = row.parsed_year or cand.year
                    row.parsed_url = row.parsed_url or cand.url[:1000]
                    row.included = cand.confidence >= parser.INCLUDE_THRESHOLD
                for k, v in fields.items():
                    setattr(row, k, v)
                row.save()
            keep_ids.add(row.pk)

        for row in post.recommendations.exclude(pk__in=keep_ids):
            if row.edited or row.integration_state or row.method == Recommendation.Method.MANUAL:
                continue
            row.delete()
        post.parsed_at = timezone.now()
        return created

    # -- images -----------------------------------------------------------------------

    def cache_images(self, limit=200):
        pending = PostImage.objects.filter(file="", cache_failed=False, post__hidden=False).select_related("post")[:limit]
        count = 0
        for image in pending:
            if self.cacher.cache(image):
                count += 1
                self.run.images_cached += 1
                if count % 10 == 0:
                    self._save_counts()
        if count:
            self.log(f"cached {count} images")
        self._save_counts()
        self.hide_imageless()

    def hide_imageless(self):
        """A post whose every image failed to download was almost certainly deleted on
        Reddit (the archive keeps the record). An image-first grid can't show it."""
        gone = 0
        for post in Post.objects.filter(hidden=False, images__isnull=False).distinct().prefetch_related("images"):
            images = list(post.images.all())
            if images and all(i.cache_failed for i in images):
                post.hidden = True
                post.save(update_fields=["hidden"])
                gone += 1
        if gone:
            self.log(f"hid {gone} posts whose images are gone from Reddit (deleted posts)")


def start_background_sync(trigger=SyncRun.Trigger.UI, sources=None, **kwargs):
    """Kick off a sync in a daemon thread (for the Settings page button). Returns the
    SyncRun, or None if one is already running."""
    if SyncRun.running() or SyncRun.cooldown_until():
        return None
    run = SyncRun.objects.create(trigger=trigger)
    source_ids = [s.pk for s in (sources or Source.objects.filter(enabled=True))]

    def target():
        try:
            Syncer(run, **kwargs).run_sources(list(Source.objects.filter(pk__in=source_ids)))
        finally:
            db.close_old_connections()

    threading.Thread(target=target, name=f"sync-{run.pk}", daemon=True).start()
    return run
