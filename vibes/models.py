"""
Core domain model: a Source (subreddit) yields Posts (the "vibe" images), each of which
carries Recommendations parsed from its comments. See docs/ARCHITECTURE.md → "Data model".
"""

from django.db import models
from django.urls import reverse
from django.utils import timezone


class Source(models.Model):
    """A subreddit we sync from, and which section of the app its posts belong to."""

    class Kind(models.TextChoices):
        MOVIES = "movies", "Movies"
        MUSIC = "music", "Music"

    class Listing(models.TextChoices):
        HOT = "hot", "Hot"
        NEW = "new", "New"
        TOP = "top", "Top"
        RISING = "rising", "Rising"

    class TimeFilter(models.TextChoices):
        DAY = "day", "Past day"
        WEEK = "week", "Past week"
        MONTH = "month", "Past month"
        YEAR = "year", "Past year"
        ALL = "all", "All time"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    subreddit = models.CharField(max_length=60, unique=True, help_text="Without the r/ prefix")
    listing = models.CharField(max_length=10, choices=Listing.choices, default=Listing.HOT)
    time_filter = models.CharField(
        max_length=10, choices=TimeFilter.choices, default=TimeFilter.MONTH,
        help_text="Only used when listing is 'top'",
    )
    fetch_limit = models.PositiveSmallIntegerField(
        default=50, help_text="How many posts to pull from the listing each sync (max 100)",
    )
    enabled = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "subreddit"]

    def __str__(self):
        return f"r/{self.subreddit} ({self.get_kind_display()})"

    @property
    def is_music(self):
        return self.kind == self.Kind.MUSIC


class PostQuerySet(models.QuerySet):
    def visible(self):
        return self.filter(hidden=False)

    def for_kind(self, kind):
        return self.filter(source__kind=kind)

    def with_counts(self):
        return self.annotate(
            rec_count=models.Count(
                "recommendations", filter=models.Q(recommendations__included=True), distinct=True,
            ),
        )


class Post(models.Model):
    """One Reddit submission. `raw` keeps the trimmed listing JSON so the parser can be
    re-run offline when its heuristics improve."""

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="posts")
    reddit_id = models.CharField(max_length=20, unique=True, db_index=True)
    title = models.CharField(max_length=500)
    author = models.CharField(max_length=80, blank=True)
    permalink = models.CharField(max_length=500, help_text="Path on reddit.com")
    url = models.URLField(max_length=1000, blank=True, help_text="Link target of the post")
    selftext = models.TextField(blank=True)
    flair = models.CharField(max_length=100, blank=True)
    score = models.IntegerField(default=0)
    upvote_ratio = models.FloatField(null=True, blank=True)
    num_comments = models.IntegerField(default=0)
    created_utc = models.DateTimeField(db_index=True)
    is_gallery = models.BooleanField(default=False)
    is_video = models.BooleanField(default=False)
    nsfw = models.BooleanField(default=False)
    raw = models.JSONField(default=dict, blank=True)
    # Flattened comment dicts from the last fetch (id/author/body/score/depth/permalink),
    # kept so recommendations can be re-parsed without another request.
    comments = models.JSONField(default=list, blank=True)

    comments_fetched_at = models.DateTimeField(null=True, blank=True)
    comments_fetched_count = models.IntegerField(
        default=0, help_text="num_comments at the time comments were last fetched",
    )
    parsed_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    hidden = models.BooleanField(default=False, help_text="Hide from the tile grid")
    starred = models.BooleanField(default=False)

    objects = PostQuerySet.as_manager()

    class Meta:
        ordering = ["-created_utc"]
        indexes = [models.Index(fields=["source", "-created_utc"]), models.Index(fields=["-score"])]

    def __str__(self):
        return f"[{self.reddit_id}] {self.title[:60]}"

    def get_absolute_url(self):
        return reverse("vibes:post_detail", args=[self.pk])

    @property
    def reddit_url(self):
        return f"https://www.reddit.com{self.permalink}"

    @property
    def primary_image(self):
        images = list(self.images.all())
        return images[0] if images else None

    @property
    def needs_comment_fetch(self):
        """True when we have never fetched comments, or the comment count moved on enough
        since last time to be worth another request."""
        if self.comments_fetched_at is None:
            return True
        grown = self.num_comments - self.comments_fetched_count
        age = timezone.now() - self.comments_fetched_at
        return grown >= 3 and age.total_seconds() > 6 * 3600


class PostImage(models.Model):
    """An image attached to a post (galleries have several). `file` is our cached copy
    so the tile grid never hotlinks Reddit's CDN; `thumb` is a downsized version for
    the grid."""

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="images")
    order = models.PositiveSmallIntegerField(default=0)
    source_url = models.URLField(max_length=1000)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    caption = models.CharField(max_length=500, blank=True)
    file = models.ImageField(upload_to="posts/", blank=True)
    thumb = models.ImageField(upload_to="thumbs/", blank=True)
    cached_at = models.DateTimeField(null=True, blank=True)
    cache_failed = models.BooleanField(default=False)

    class Meta:
        ordering = ["post", "order"]
        unique_together = [("post", "order")]

    def __str__(self):
        return f"{self.post.reddit_id}#{self.order}"

    @property
    def display_url(self):
        """Best URL to show in the grid: cached thumb → cached full → original."""
        if self.thumb:
            return self.thumb.url
        if self.file:
            return self.file.url
        return self.source_url

    @property
    def full_url(self):
        return self.file.url if self.file else self.source_url


class Recommendation(models.Model):
    """One candidate title extracted from a comment. The parser is a heuristic
    candidate-generator; `included` plus the editable parsed_* fields are what the
    exports and integrations actually act on."""

    class Method(models.TextChoices):
        LINK = "link", "Link in comment"
        ARTIST_TITLE = "artist_title", "Artist - Title pattern"
        TITLE_BY_ARTIST = "title_by_artist", "Title by Artist pattern"
        TITLE_YEAR = "title_year", "Title (Year) pattern"
        QUOTED = "quoted", "Quoted / italic title"
        LIST_ITEM = "list_item", "List item"
        SHORT_COMMENT = "short_comment", "Whole short comment"
        TITLECASE = "titlecase", "Title-case run in sentence"
        MANUAL = "manual", "Added manually"

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="recommendations")
    comment_id = models.CharField(max_length=20, blank=True)
    comment_author = models.CharField(max_length=80, blank=True)
    comment_score = models.IntegerField(default=0)
    comment_depth = models.PositiveSmallIntegerField(default=0)
    comment_permalink = models.CharField(max_length=500, blank=True)
    raw_text = models.TextField(blank=True, help_text="Comment body the candidate came from")
    snippet = models.CharField(max_length=300, blank=True, help_text="The exact fragment matched")

    parsed_title = models.CharField(max_length=300, blank=True, help_text="Blank = artist-only recommendation")
    parsed_artist = models.CharField(max_length=200, blank=True)
    parsed_year = models.PositiveSmallIntegerField(null=True, blank=True)
    parsed_url = models.URLField(max_length=1000, blank=True)

    method = models.CharField(max_length=20, choices=Method.choices)
    confidence = models.FloatField(default=0.5)
    mention_count = models.PositiveSmallIntegerField(default=1)
    included = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    edited = models.BooleanField(
        default=False, help_text="Set once a human touches it; re-parsing then leaves it alone",
    )
    # Per-service outcome of the last push, e.g. {"radarr": {"status": "added", "at": "...", "detail": "..."}}
    integration_state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["post", "order", "-confidence"]

    def __str__(self):
        return self.display_label

    @property
    def display_label(self):
        if self.parsed_artist and not self.parsed_title:
            return f"{self.parsed_artist} (artist)"
        if self.parsed_artist:
            return f"{self.parsed_artist} - {self.parsed_title}"
        if self.parsed_year:
            return f"{self.parsed_title} ({self.parsed_year})"
        return self.parsed_title

    @property
    def search_term(self):
        return f"{self.parsed_artist} {self.parsed_title}".strip()

    @property
    def confidence_band(self):
        if self.confidence >= 0.8:
            return "high"
        if self.confidence >= 0.6:
            return "mid"
        return "low"

    @property
    def comment_url(self):
        return f"https://www.reddit.com{self.comment_permalink}" if self.comment_permalink else ""
