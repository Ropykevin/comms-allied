#!/usr/bin/env sh
# Nightly PostgreSQL backup for the docker-compose stack.
#   ./scripts/backup.sh                  # writes backups/allied-YYYYmmdd-HHMMSS.dump
#   crontab: 30 2 * * * cd /srv/allied_chat && ./scripts/backup.sh >> backups/backup.log 2>&1
# Restore:
#   docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < backups/<file>.dump
set -eu

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

file="$BACKUP_DIR/allied-$(date +%Y%m%d-%H%M%S).dump"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$file"

if [ ! -s "$file" ]; then
  echo "Backup failed: $file is empty" >&2
  rm -f "$file"
  exit 1
fi

find "$BACKUP_DIR" -name 'allied-*.dump' -mtime "+$RETENTION_DAYS" -delete
echo "$(date -Iseconds) wrote $file ($(du -h "$file" | cut -f1))"
