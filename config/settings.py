"""
Django settings for MediaThatFeelsLike.

Everything environment-specific comes from `.env` (via django-environ) so the same code
runs unchanged on the Windows dev box and inside the Docker container on the services
LXC. See `.env.example` for every variable read here.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CREDENTIAL_ENCRYPTION_KEY=(str, ""),
    REDDIT_FETCH_USER_AGENT=(
        str,
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    ),
    REDDIT_CLIENT_ID=(str, ""),
    REDDIT_CLIENT_SECRET=(str, ""),
    REDDIT_USER_AGENT=(str, "MediaThatFeelsLike/0.1"),
    REDDIT_BACKEND=(str, "auto"),
    ARCHIVE_USER_AGENT=(str, "MediaThatFeelsLike/0.1 (self-hosted; +https://github.com/WB2024/MediaThatFeelsLike)"),
    RADARR_URL=(str, ""),
    RADARR_API_KEY=(str, ""),
    LIDARR_URL=(str, ""),
    LIDARR_API_KEY=(str, ""),
    JELLYFIN_URL=(str, ""),
    JELLYFIN_API_KEY=(str, ""),
    NAVIDROME_URL=(str, ""),
    NAVIDROME_USERNAME=(str, ""),
    NAVIDROME_PASSWORD=(str, ""),
    SLSKD_URL=(str, ""),
    SLSKD_API_KEY=(str, ""),
    TMDB_API_KEY=(str, ""),
    DATA_DIR=(str, ""),
    MEDIA_DATA_DIR=(str, ""),
    MIN_FREE_DISK_GB=(float, 2.0),
)
environ.Env.read_env(BASE_DIR / ".env")

# Where the sqlite DB and cached images live. In Docker this is the mounted volume; on
# the dev box it defaults to a `data/` folder next to manage.py (gitignored).
DATA_DIR = Path(env("DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")
# Behind Nginx Proxy Manager the Host header is the proxied hostname; CSRF needs to
# trust it explicitly on Django >= 4.
CSRF_TRUSTED_ORIGINS = [
    f"{scheme}://{host}"
    for host in ALLOWED_HOSTS
    if host not in ("*", "")
    for scheme in ("http", "https")
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "vibes",
    "reddit_sync",
    "integrations",
    "exports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "vibes.context_processors.sections",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": env.db_url("DATABASE_URL", default=f"sqlite:///{DATA_DIR / 'db.sqlite3'}"),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Cached copies of Reddit post images (so the tile grid doesn't have to rely on
# hotlinking Reddit's CDN, which is slow, rate-limited and occasionally blocks
# referers -- it's still used as a fallback when nothing's cached yet, see
# PostImage.display_url). This is comfortably the largest and fastest-growing thing
# this app stores, so it can live on different storage than the sqlite DB -- e.g. a
# large, slower NFS/network-attached drive shared with the rest of the media library --
# by setting MEDIA_DATA_DIR to an absolute path. Left unset, it's a subfolder of
# DATA_DIR as before.
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env("MEDIA_DATA_DIR")) if env("MEDIA_DATA_DIR") else DATA_DIR / "media"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- App-specific settings -------------------------------------------------------------

# Fernet key for encrypting service credentials at rest. Generated with
# `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
CREDENTIAL_ENCRYPTION_KEY = env("CREDENTIAL_ENCRYPTION_KEY")

# Reddit access -- see docs/ARCHITECTURE.md → "Reddit access".
#   auto    -> PRAW if OAuth creds are set, otherwise the Arctic Shift archive
#   archive -> Arctic Shift archive API (default in practice)
#   direct  -> unauthenticated JSON from www.reddit.com (blocked per-IP for long stretches)
#   praw    -> official OAuth API
REDDIT_BACKEND = env("REDDIT_BACKEND")
ARCHIVE_USER_AGENT = env("ARCHIVE_USER_AGENT")
REDDIT_FETCH_USER_AGENT = env("REDDIT_FETCH_USER_AGENT")
REDDIT_CLIENT_ID = env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = env("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = env("REDDIT_USER_AGENT")
# Pacing for the direct (unauthenticated reddit.com) path only: seconds between requests.
# Reddit rate-limits logged-out access per IP and a burst (~40 requests in a couple of
# minutes) earns a block that lingers for a while; ~6 requests/minute has been fine.
REDDIT_MIN_REQUEST_INTERVAL = 8.0
REDDIT_REQUEST_JITTER = 4.0
REDDIT_REQUEST_TIMEOUT = 20
# After a run gets blocked, refuse to start another for this long (doubles per
# consecutive blocked run, capped at REDDIT_BLOCK_COOLDOWN_MAX).
REDDIT_BLOCK_COOLDOWN = 30 * 60
REDDIT_BLOCK_COOLDOWN_MAX = 6 * 3600

# Image caching stops (with a warning in the sync log) once free disk space under
# DATA_DIR drops below this. These subreddits are high-volume enough that a deep
# backfill can genuinely fill a shared homelab disk if left unchecked -- see
# reddit_sync/sync.py's Syncer._disk_has_room.
MIN_FREE_DISK_GB = env("MIN_FREE_DISK_GB")

# First-run defaults for the Settings page; the DB copy is the source of truth after that.
SERVICE_ENV_DEFAULTS = {
    "radarr": {"url": env("RADARR_URL"), "api_key": env("RADARR_API_KEY")},
    "lidarr": {"url": env("LIDARR_URL"), "api_key": env("LIDARR_API_KEY")},
    "jellyfin": {"url": env("JELLYFIN_URL"), "api_key": env("JELLYFIN_API_KEY")},
    "navidrome": {
        "url": env("NAVIDROME_URL"),
        "username": env("NAVIDROME_USERNAME"),
        "password": env("NAVIDROME_PASSWORD"),
    },
    "slskd": {"url": env("SLSKD_URL"), "api_key": env("SLSKD_API_KEY")},
    # Fixed API host -- there's nothing to configure there, just the read access token.
    "tmdb": {"url": "https://api.themoviedb.org", "api_key": env("TMDB_API_KEY")},
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"level": "WARNING"},
        "reddit_sync": {"level": "INFO"},
        "integrations": {"level": "INFO"},
    },
}
