# Python and REST

`GragService` owns its database directly. Use it when no other process owns the
file; use the running server's REST API for shared access. The CLI graph commands
perform this routing automatically. See [architecture](../architecture.md).

```python
from grag import GragConfig
from grag.service import GragService
from grag.core.types import SearchRequest

svc = GragService(GragConfig(db_path="knowledge.lbdb"))
try:
    res = svc.search_knowledge(SearchRequest(query="who owns the ingestion gateway?", hops=1))
    print(res.context)
finally:
    svc.close()
```

## REST endpoints

Request and response models live in
[`grag.core.types`](https://github.com/Krokz/grag/blob/main/src/grag/core/types.py).
The built-in OpenAPI schema lists the running server's endpoints and models.

| Method and path | Purpose |
|---|---|
| `GET /api/schema` | Schema, optional detail/revision reuse and freshness policy. |
| `POST /api/schema/define` | Define tables and properties. |
| `POST /api/nodes/upsert`, `POST /api/edges/upsert` | Atomic writes, optional revision guards, history and retry receipts. |
| `POST /api/query` | Bounded read-only Cypher. |
| `POST /api/search`, `POST /api/context` | Ranked retrieval or context around known IDs. |
| `POST /api/ingest`, `POST /api/ingest/code` | Synchronous document or code ingestion. |
| `POST /api/jobs/ingest`, `POST /api/jobs/ingest/code` | Queue ingestion; return HTTP 202 and a job record. |
| `GET /api/jobs`, `GET /api/jobs/{job_id}` | List or inspect process-local job records. |
| `GET /api/index/status` | Code freshness, registered roots and verification diagnostics. |
| `GET /api/graph/sample`, `GET /api/graph/full` | Graph views within server response/work limits. |
| `GET /api/export` | Stream a completed consistent format-2 snapshot; not ordinary response JSON. |
| `GET /api/dbs` | List databases and the selected default. |
| `GET /api/health` | Server/default database identity, runtime, workers and shutdown status. |

Database-scoped routes accept `?db=<name>` or `x-grag-db` in multi-db mode.
Supply the configured bearer token; health is the public exception.
[Server routing](../operations/server.md), [freshness](../guides/freshness.md),
[limits](limits.md) and [backup/restore](../operations/recovery.md) define the
corresponding contracts. Restore is an offline CLI operation into a new file.

## Storage conventions

- One `.lbdb` per database, with possible WAL/shadow sidecars needed for recovery.
  Use logical snapshots for live backups. Properties starting with `_` are grag-internal.
- Provenance: `_source`, `_created_at` on every table created via `define_schema`.
- Vector columns (`embedding`, `_emb_r`, `_emb_code`, `_emb_model`, `_emb_fingerprint`) are added lazily by the retrieval layer. Fingerprints identify the configuration used; vector writes also check that the input properties still match.
- `_grag_tables` registry powers introspection and canonical `Label:key` node ids.
