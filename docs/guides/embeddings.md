# Optional embeddings

Start with BM25. Add embeddings when semantic questions justify the model download,
indexing time and memory on your workload. Exact cosine similarity measures vector
similarity; it does not establish that a retrieved fact answers the question.

Install the optional extra using the same package manager/environment as grag:

```bash
pip install 'gragdb[embed-local]'
```

When `embed-local` is installed, a later `grag init` writes
`GRAG_EMBED_PROVIDER=fastembed` into its MCP configuration. Preview that change
with `grag init --dry-run`. An already running server keeps its previous settings
until restarted.

On macOS/Linux:

```bash
GRAG_EMBED_PROVIDER=fastembed grag serve --with-mcp
```

On PowerShell:

```powershell
$env:GRAG_EMBED_PROVIDER = "fastembed"
grag serve --with-mcp
```

The default model is `BAAI/bge-small-en-v1.5`. First use downloads model assets;
size and startup time depend on the model/cache. It runs locally through ONNX
Runtime, without PyTorch. grag tries a cached load first;
when missing assets require preparation it logs that step and reports download
or cache-permission errors. With `HF_HUB_OFFLINE=1`, a failed cached load never
falls back to a download. Use `grag doctor --prepare` with the same embedding
settings, then `grag doctor`, to verify actual offline inference. A present cache
directory is not sufficient. See [installation](../installation.md).

An initialized serving process (`serve`, `mcp`) runs a
background embedding worker. Health reports counters under `embedding`; searches
report `pending_embeddings` while it drains. `GRAG_EMBED_BACKGROUND=0` enables
inline work. **One-shot CLI ingests still embed synchronously** and can take much
longer with embeddings enabled.

Retrieval latency and resident memory depend on your graph and model. Benchmark
useful answers against BM25 and file search before enabling embeddings everywhere.

## Embedding inputs and invalidation

A node's embedding text is its STRING properties minus
side-cars that only dilute the vector (`meta`, `path`, `heading_path`, `language`, git
fields — `GRAG_EMBED_EXCLUDE_PROPS` overrides the list; `EmbedderConfig.text_props`
pins an explicit list per label). Queries and documents get the retrieval prefixes the
model family expects (bge/arctic/mxbai: query instruction; nomic: `search_query:` /
`search_document:`; e5: `query:` / `passage:`), overridable with
`GRAG_EMBED_QUERY_PREFIX` / `GRAG_EMBED_DOC_PREFIX`. Changing the model, the prefixes
or the effective text policy makes old vectors pending; they rebuild automatically
on the worker or subsequent searches. Codec and remote endpoint changes also trigger
rebuilding. Legacy vectors without a configuration fingerprint rebuild once.
Use `grag reindex` to rebuild immediately, or when a model changes behind the same
model name and endpoint. Changing vector dimensions still requires an explicit
storage migration; grag reports the mismatch without dropping data.
Concurrent text edits discard in-flight embeddings and leave the new text pending.
To pick a model on
your own data rather than a leaderboard, `examples/embedding_eval.py` scores candidates
(vector-only, BM25-only, hybrid recall@k and MRR, embed time, query latency) against a
`grag export` and a question set — `--auto N` derives proxy questions from a
`--sections` ingest.

```bash
pip install -e ".[embed-local]"
GRAG_EMBED_PROVIDER=fastembed grag --db knowledge.lbdb serve
# optional: GRAG_EMBED_MODEL=BAAI/bge-base-en-v1.5 GRAG_EMBED_DIM=768
```

## Vector codecs and crash safety

Codec ladder (`grag bench` reproduces these numbers on a synthetic 1500-doc corpus):

| codec | bytes/vec (dim 64) | recall@10 | note |
|---|---|---|---|
| `fp32` | 256 | 1.000 | baseline; full-precision cosine scoring |
| `int8` | 68 | 0.998 | 4x smaller, near-zero loss |
| `binary` | 8 | 0.476 | 32x, hamming scan + rescore |
| `polar` | 14 | 0.766 | experimental PolarQuant-style angular codes (sine-power-law bit allocation, training-free) |

Select with `GRAG_VECTOR_CODEC` / `GragConfig.vector_codec`. `fp32` is the default. Compressed codecs are opt-in; evaluate their recall on your data.

Two honest costs of the codec path: candidate generation for non-fp32 codecs is an O(rows) approximate scan (only pk + code bytes cross the wire; fp32 nodes are fetched for the 4·top_k rescore shortlist only) — that's the property `grag bench` measures, so no ANN index is involved. Without a background worker, searches embed lazily: at most `GRAG_MAX_EMBED_PER_SEARCH` (default 256) nodes per search call, with the remainder reported as `pending_embeddings` on the search response so agents know vector recall is still improving.

Full-precision (`fp32`) retrieval uses an exact cosine scan, also O(rows × dimensions),
with full records fetched only for the shortlist. Native HNSW acceleration remains
disabled after the LadybugDB 0.20.2 indexed-embedding crash. The 0.20.3 engine
includes upstream index fixes; grag retains exact cosine and its existing safety
policy while broader acceleration validation remains separate. On writable open,
grag removes its legacy `grag_vec__*`
indexes, checkpoints and reopens before serving; graph data and stored vectors
are preserved. This also applies when embeddings are disabled. Read-only
inspection leaves indexes intact; externally managed HNSW indexes require their
owner to remove them before grag accepts writes. An already unreadable WAL still
requires the separate `grag recover` workflow. Search may be slower on large
graphs, but semantic search, codecs, and the embedding model settings remain available.
