import re

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("integrations/", include("integrations.urls")),
    path("exports/", include("exports.urls")),
    path("sync/", include("reddit_sync.urls")),
    path("", include("vibes.urls")),
]

# Cached post images live under MEDIA_ROOT; serve them from Django directly. This is a
# single-user LAN tool behind gunicorn, so no separate static server is warranted.
# NOTE: django.conf.urls.static.static() is a no-op unless DEBUG=True, which would make
# every image 404 in production -- serve unconditionally instead.
urlpatterns += [
    re_path(rf"^{re.escape(settings.MEDIA_URL.lstrip('/'))}(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
