#!/usr/bin/env bash
# Online backup of a RUNNING grag server as portable JSONL (schema + nodes +
# edges + provenance; embeddings are derived data and rebuild on import).
#
# The .lbdb file is single-writer, so `grag export` on the file itself needs
# the server stopped. This script streams GET /api/export instead — no
# downtime — and keeps a rolling window of dated files.
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

# `grag export --url` needs only the stdlib; curl works too:
#   curl -fsS -H "Authorization: Bearer $GRAG_API_TOKEN" "$GRAG_SERVER_URL/api/export" -o "$out"
grag export --url "$GRAG_SERVER_URL" -o "$out"
gzip -f "$out"
echo "backup: $out.gz ($(du -h "$out.gz" | cut -f1))"

# Restore into a fresh database (server stopped, or a new file):
#   gunzip -c grag-<stamp>.jsonl.gz | grag --db restored.lbdb import /dev/stdin
ls -1t "$dest"/grag-*.jsonl.gz 2>/dev/null | tail -n +$((keep + 1)) | xargs -r rm -f
