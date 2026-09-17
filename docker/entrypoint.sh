#!/bin/sh
# Runs before the web server: schema, default sources, static files. Idempotent.
set -e
cd /app
python manage.py migrate --noinput
python manage.py bootstrap
python manage.py collectstatic --noinput --clear > /dev/null
exec "$@"
