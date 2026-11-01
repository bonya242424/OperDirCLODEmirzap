#!/usr/bin/env bash
# Резервная копия базы и файлов. Запускать по cron раз в сутки, копии хранить вне этого сервера.
# Пример: 15 3 * * * /opt/operdir/scripts/backup.sh /var/backups/operdir
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${1:?Укажите папку для копий}"
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$DEST"
set -a; . ./.env; set +a
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$DEST/db-$STAMP.sql.gz"
docker compose exec -T backend tar -C /data -czf - files > "$DEST/files-$STAMP.tar.gz"
# Хранить 14 последних копий каждого вида
ls -1t "$DEST"/db-*.sql.gz | tail -n +15 | xargs -r rm --
ls -1t "$DEST"/files-*.tar.gz | tail -n +15 | xargs -r rm --
echo "Копия сохранена в $DEST ($STAMP)"
