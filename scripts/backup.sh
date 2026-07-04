#!/usr/bin/env bash
# Nightly Postgres backup (E4). Runs as a Railway/Render cron.
#
# Required env vars:
#   DATABASE_URL          — Postgres connection string
#   BACKUP_BUCKET         — S3-compatible bucket name (skip upload if unset)
#   BACKUP_PATH           — prefix inside bucket (default: trustkaro)
#   AWS_ACCESS_KEY_ID     — S3 credentials
#   AWS_SECRET_ACCESS_KEY — S3 credentials
#   AWS_ENDPOINT_URL      — set if using R2/MinIO (omit for AWS S3)
#
# Restore a backup:
#   gunzip -c backup-<stamp>.sql.gz | psql "$STAGING_DATABASE_URL"
set -euo pipefail

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST="backup-${STAMP}.sql.gz"

# plain format (not custom) so gunzip | psql restore works without pg_restore
pg_dump --format=plain --no-owner "$DATABASE_URL" | gzip > "/tmp/${DEST}"

if [ -n "${BACKUP_BUCKET:-}" ]; then
  aws s3 cp "/tmp/${DEST}" "s3://${BACKUP_BUCKET}/${BACKUP_PATH:-trustkaro}/${DEST}" \
    ${AWS_ENDPOINT_URL:+--endpoint-url "$AWS_ENDPOINT_URL"}
  # delete backups older than 14; keep the 14 most recent
  aws s3 ls "s3://${BACKUP_BUCKET}/${BACKUP_PATH:-trustkaro}/" \
    ${AWS_ENDPOINT_URL:+--endpoint-url "$AWS_ENDPOINT_URL"} | \
    awk '{print $4}' | sort | head -n -14 | while read -r old; do
      [ -n "$old" ] && aws s3 rm "s3://${BACKUP_BUCKET}/${BACKUP_PATH:-trustkaro}/$old" \
        ${AWS_ENDPOINT_URL:+--endpoint-url "$AWS_ENDPOINT_URL"}
    done
fi

rm -f "/tmp/${DEST}"
echo "backup complete: ${DEST}"
