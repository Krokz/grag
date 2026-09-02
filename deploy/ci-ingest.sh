#!/usr/bin/env bash
# CI hook: after a push, refresh the code graph on the grag server.
#
# The server can only parse files on ITS filesystem, so the pattern is:
#   1. the server host keeps a checkout of each repo (e.g. /repos/algo4,
#      mounted read-only into the container — see docker-compose.yml);
#   2. CI (or a post-receive hook) updates that checkout, then calls this;
#   3. this queues a background ingest job and polls until it finishes.
#
# Re-ingest is incremental: every file is parsed for cross-file resolution,
# but only files whose content changed are rewritten, so a one-file commit
# costs seconds, not a full rewrite under the write lock. Searches keep
# working while it runs.
#
#   GRAG_SERVER_URL=https://grag.example.com GRAG_API_TOKEN=... \
#     deploy/ci-ingest.sh /repos/algo4 [/repos/other ...]
set -euo pipefail

: "${GRAG_SERVER_URL:?set GRAG_SERVER_URL}"
: "${GRAG_API_TOKEN:?set GRAG_API_TOKEN}"
[ "$#" -ge 1 ] || { echo "usage: $0 <server-side repo path> [...]" >&2; exit 2; }

auth=(-H "Authorization: Bearer $GRAG_API_TOKEN" -H "content-type: application/json")
[ -n "${GRAG_SERVER_DB:-}" ] && auth+=(-H "x-grag-db: $GRAG_SERVER_DB")

paths="$(printf '%s\n' "$@" | python3 -c 'import json,sys; print(json.dumps([l.rstrip("\n") for l in sys.stdin if l.strip()]))')"
job="$(curl -fsS "${auth[@]}" -X POST "$GRAG_SERVER_URL/api/jobs/ingest/code" \
        -d "{\"paths\": $paths, \"calls\": true}")"
id="$(printf '%s' "$job" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "queued ingest job $id"

for _ in $(seq 1 "${GRAG_INGEST_WAIT:-600}"); do
  sleep 1
  body="$(curl -fsS "${auth[@]}" "$GRAG_SERVER_URL/api/jobs/$id")"
  status="$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
  case "$status" in
    done)   printf '%s\n' "$body" | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(f"ingest done: {r[\"files_parsed\"]} files parsed, {r[\"files_unchanged\"]} unchanged, {r[\"nodes_pruned\"]} nodes pruned, warnings={len(r[\"warnings\"])}")'; exit 0 ;;
    failed) printf '%s\n' "$body" | python3 -c 'import json,sys; print("ingest FAILED:", json.load(sys.stdin)["error"])' >&2; exit 1 ;;
  esac
done
echo "timed out waiting for job $id (it keeps running on the server)" >&2
exit 1
