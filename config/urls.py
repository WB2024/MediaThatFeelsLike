from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("integrations/", include("integrations.urls")),
    path("exports/", include("exports.urls")),
    path("sync/", include("reddit_sync.urls")),
    path("", include("vibes.urls")),
]

# Cached post images live under MEDIA_ROOT; serve them from Django directly. This is a
# single-user LAN tool behind gunicorn, so no separate static server is warranted.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
