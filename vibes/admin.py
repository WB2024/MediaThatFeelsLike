from django.contrib import admin
from django.utils.html import format_html

from .models import Post, PostImage, Recommendation, Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("subreddit", "kind", "listing", "time_filter", "fetch_limit", "enabled", "last_synced_at")
    list_editable = ("listing", "time_filter", "fetch_limit", "enabled")
    list_filter = ("kind", "enabled")


class PostImageInline(admin.TabularInline):
    model = PostImage
    extra = 0
    fields = ("order", "preview", "source_url", "width", "height", "cached_at", "cache_failed")
    readonly_fields = ("preview",)

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj.pk:
            return ""
        return format_html('<img src="{}" style="max-height:80px">', obj.display_url)


class RecommendationInline(admin.TabularInline):
    model = Recommendation
    extra = 0
    fields = ("included", "parsed_artist", "parsed_title", "parsed_year", "method", "confidence", "mention_count", "comment_score", "edited")
    ordering = ("order",)


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("reddit_id", "title", "source", "score", "num_comments", "created_utc", "comments_fetched_at", "hidden")
    list_filter = ("source", "hidden", "is_gallery")
    search_fields = ("title", "reddit_id", "author")
    readonly_fields = ("first_seen_at", "last_seen_at", "raw")
    date_hierarchy = "created_utc"
    inlines = [PostImageInline, RecommendationInline]


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ("display_label", "post", "method", "confidence", "mention_count", "comment_score", "included", "edited")
    list_filter = ("method", "included", "edited", "post__source")
    search_fields = ("parsed_title", "parsed_artist", "raw_text")
    raw_id_fields = ("post",)
