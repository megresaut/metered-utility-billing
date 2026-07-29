#!/usr/bin/env bash
# Nightly Postgres backup. Wire into cron:
#   0 3 * * *  /path/to/metered/deploy/ovh/backup.sh >> /var/log/metered-backup.log 2>&1
# Keeps the last 14 daily dumps. (Issue #9: also copy these OFF the box — e.g.
# `rclone copy` to object storage — and test a restore before relying on it.)
set -euo pipefail
cd "$(dirname "$0")"

OUT_DIR="./backups"
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="$OUT_DIR/metered-$STAMP.sql.gz"

docker compose --env-file .env.production exec -T db \
  pg_dump -U metered -d metered --no-owner | gzip > "$FILE"

echo "[backup] wrote $FILE ($(du -h "$FILE" | cut -f1))"

# Retain last 14.
ls -1t "$OUT_DIR"/metered-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
