#!/usr/bin/env bash
# Online verified JSONL backup, including graph, history and retry receipts.
# Writes pause during snapshot capture; download uses the completed spool.
# Embeddings/indexes are derived data and rebuild after restore.
# The server stays running; this script keeps dated snapshots.
#
#   GRAG_SERVER_URL=https://grag.example.com GRAG_API_TOKEN=... deploy/backup.sh /backups
#   # cron: 17 3 * * * /opt/grag/deploy/backup.sh /backups >> /var/log/grag-backup.log 2>&1
set -euo pipefail

dest="${1:-./backups}"
keep="${GRAG_BACKUP_KEEP:-14}"
: "${GRAG_SERVER_URL:?set GRAG_SERVER_URL (e.g. https://grag.example.com)}"
: "${GRAG_API_TOKEN:?set GRAG_API_TOKEN}"

mkdir -p "$dest"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$dest/grag-$stamp.jsonl"

# The updated CLI checks the completion checksum before publishing the file.
grag export --url "$GRAG_SERVER_URL" -o "$out"
gzip -f "$out"
echo "backup: $out.gz ($(du -h "$out.gz" | cut -f1))"

# Restore into a new database file (existing destinations are refused):
#   gunzip -c grag-<stamp>.jsonl.gz | grag --db restored.lbdb import /dev/stdin
ls -1t "$dest"/grag-*.jsonl.gz 2>/dev/null | tail -n +$((keep + 1)) | xargs -r rm -f
