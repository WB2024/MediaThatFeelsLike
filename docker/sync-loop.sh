#!/bin/sh
# Sidecar: run the sync every SYNC_INTERVAL seconds (default 30 min). Same idea as a
# cron entry, but self-contained in the compose stack so the host needs nothing.
set -u
INTERVAL="${SYNC_INTERVAL:-1800}"
MAX_COMMENTS="${SYNC_MAX_COMMENTS:-40}"
cd /app
# Give the app container a moment to run migrations on a fresh deploy.
sleep 20
while true; do
  echo "[sync-loop] $(date -u +%FT%TZ) starting sync (max ${MAX_COMMENTS} comment threads)"
  python manage.py sync_reddit --max-comments "${MAX_COMMENTS}" || echo "[sync-loop] sync exited with status $?"
  echo "[sync-loop] sleeping ${INTERVAL}s"
  sleep "${INTERVAL}"
done
