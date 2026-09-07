# Retrieve useful context

1. Text properties get a native BM25 FTS index per searchable table. Search takes
   up to `max(32, 4 * top_k)` lexical candidates per label. When hits span labels,
   it reranks them with shared BM25 statistics over that shortlist (Unicode word
   tokens and English Snowball stemming). Single-label hits keep native BM25.
   Raw BM25 scores from separate indexes are never directly compared.
2. With an embedder configured, the default `fp32` codec scans eligible vectors with exact cosine scoring. Optional compressed codecs generate an approximate shortlist, then rescore it with full-precision vectors. Rescoring cannot recover a relevant vector missed by that shortlist.
3. Lexical and vector candidates are fused using reciprocal ranks. Equal scores
   receive equal ranks; node IDs break final ties consistently. Label filters are
   sets, so their order and duplicates cannot bias ranking or inflate counts.
   Vector shortlists are retained per label until the diversity pass, which
   promotes other labels after the configured cap and backfills unused slots.
4. Selected seeds expand k hops through the graph and become cited context.

Cross-label lexical scores use **candidate-pool statistics**, not full-corpus
statistics. The native lexical/vector shortlists still bound recall; changing
`top_k` can change those shortlists and their rankings. RRF scores express rank
agreement, not confidence probabilities. Diversity can promote a lower-scoring
label ahead of a deferred hit; set `GRAG_SEARCH_LABEL_CAP=0` for pure fused order.

**Context completeness and budgets.** Both retrieval calls return `truncated`,
`omitted_nodes`, `omitted_edges`, `omitted_properties`, `included_node_ids`, and
`expansion_limited`. MCP returns this metadata in a JSON footer after `---`,
including for `get_context`. A truncated answer is partial evidence. Omission
counts describe packing; `expansion_limited` separately reports a neighborhood
that exceeded 512 paths per seed. These fields do not claim exhaustive search
recall beyond the requested seeds, hops, and candidate limits.

Property values have no fixed character cutoff. When everything fits, long
strings and relationship properties are returned in full. Under a tight budget,
packing selects seed identities and connecting edges first, then adds complete
property values, with citations before other node properties. Omission counts
make missing records and properties explicit. Returned edges always have both
endpoints in the returned graph. Vector payloads and null properties are omitted
by design and do not count as lost evidence.

`token_budget` now covers the **complete compact response**: the larger of the
REST/Python JSON payload (including `seeds`, `subgraph`, context, and metadata)
and the MCP text (including its footer). `response_token_estimate` reports that
size; `token_estimate` measures only `context`. Both use `ceil(UTF-8 bytes / 4)`,
a deterministic estimate rather than a model-specific tokenizer count. HTTP/MCP
protocol envelopes, client-added formatting, and tool errors are outside this
budget. The default remains 2000; supported budgets are **256–32,768**.
The compact application payload is bounded to `4 * token_budget` UTF-8 bytes;
this is **not a model-token guarantee**. Multilingual text, identifiers and JSON
can cost more tokens than this estimate; count with your harness's tokenizer
when enforcing a model context limit. Compared with v0.6.0, the same budget may
return fewer records: `seeds` and `subgraph` now contain only packed records and
properties, rather than an unbounded second copy of the retrieved graph.

For a long memory, call `get_context` with one node id and `text_property`, such
as `"body"` or `"text"` (check `describe_schema`). This mode skips expansion and
returns an exact slice of that STRING plus its citation. In Python/REST the
slice is in `subgraph.nodes[0].properties[text_property]`; in MCP it is a
JSON-escaped property value in the context. The `text_page` metadata gives
character offsets, total length, and a SHA-256 digest:

```python
from grag.core.types import ContextRequest

offset, digest, parts = 0, None, []
while True:
    result = svc.get_context(ContextRequest(
        node_ids=["Doc:42"], text_property="body", token_budget=2000,
        text_offset=offset, text_sha256=digest,
    ))
    parts.append(result.subgraph.nodes[0].properties["body"])
    page = result.text_page
    if page.next_offset is None:
        break
    offset, digest = page.next_offset, page.sha256
full_body = "".join(parts)
```

Pass `next_offset` and `sha256` on each continuation. If the text changed,
discard the accumulated slices and restart from offset 0 without a digest;
pages are not a persistent database snapshot. Every page that returns a proper
subset of the value has `truncated=true`, including a final page starting after
offset 0; `next_offset=null` is the end-of-text signal. An id or citation too
large to leave room for a character produces an actionable budget error.


## Evidence selection and query responses

Search packing prioritizes cited evidence and connections between seeds before
bookkeeping fields and unrelated neighbors. Oversized prose can appear as a
marked, exact excerpt, selected by lexical sentence matches. The full property
remains omitted and `truncated` stays true. `text_excerpts` carries character
`offset`/`end`, `total_chars`, `node_id`, `property` and `sha256`; Python/REST also
include the exact `text`. Use those coordinates and hash with the existing
`get_context` text pager to read more, or start at 0 for the complete value.
Excerpts may omit qualifications outside the selected window and may miss
semantic-only matches. This adds no configuration or model dependency.

Search and context default to `evidence="current"`: explicitly superseded,
retracted, expired, disputed, and retained obsolete document nodes are excluded
before ranking and expansion. Use `evidence="all"` to inspect them. Legacy
`status` values `superseded`, `retracted`, and `expired` are recognized; task
`open`/`done` and other business statuses are unchanged. Unreviewed or legacy
evidence stays eligible; this is a selection policy, not a truth guarantee.
Cypher remains unfiltered. The footer names `evidence_policy`;
`excluded_evidence` counts encountered post-shortlist/path exclusions only,
not every row filtered within the database.

Whole-entity Cypher replies (`RETURN n`, paths, lists and maps of entities)
omit derived vector properties. Entity IDs, relationships, provenance and
revision guards remain available. Explicit projections (`RETURN n.embedding`)
still return the requested values. Query replies are row-limited; they do not
use search/context token budgets.


## Start small for narrow questions

For a known symbol, describe the schema and project only its name, path, signature
and line range in Cypher. For text search, try a smaller budget (for example 800),
`hops=0`, and the relevant labels before expanding. The budget is a ceiling, not a
target, but current packing does not guarantee a short answer for a simple query
or a calibrated “no evidence” response. Inspect relevance and omissions.

See [code coverage](code.md), [embedding tradeoffs](embeddings.md), and
[resource limits](../reference/limits.md).
