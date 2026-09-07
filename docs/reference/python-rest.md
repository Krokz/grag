# Python and REST

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

Everything is also mirrored over REST: `POST /api/{query,search,context,ingest,ingest/code}`, `GET /api/{schema,graph/sample,health}`, `POST /api/{schema/define,nodes/upsert,edges/upsert}`.

## Storage conventions

- One `.lbdb` file per database. Properties starting with `_` are grag-internal.
- Provenance: `_source`, `_created_at` on every table created via `define_schema`.
- Vector columns (`embedding`, `_emb_r`, `_emb_code`, `_emb_model`, `_emb_fingerprint`) are added lazily by the retrieval layer. Fingerprints identify the configuration used; vector writes also check that the input properties still match.
- `_grag_tables` registry powers introspection and canonical `Label:key` node ids.
