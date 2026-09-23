# Retrieve useful context

Choose a read based on what you already know, then inspect its sources and limits.
{ .grag-lead }

| What you need | Start with |
|---|---|
| A code definition or implementation | Source search and a bounded file read |
| Saved decisions, history or concepts | `search_knowledge`, narrowed to known labels |
| Callers, relationships, graph counts or status | A projected `cypher_query` after checking unfamiliar schema |
| Neighbors or evidence for known IDs | `get_context` |
| Earlier memory revisions | [History and evidence](history.md) |

There is no required query sequence. Fetch more only when the evidence you have
is insufficient.

`search_knowledge` combines lexical and vector retrieval when an embedder is
configured. See [Embeddings and semantic search](embeddings.md) for FastEmbed
setup, the retrieval pipeline, indexing progress and model configuration.

Use the graph when saved knowledge or relationships help with the task. Ordinary
code navigation can start with the harness's source search; having grag connected
does not require a graph lookup before each source read. Graph code search remains
available, but a natural-language match is a lead to verify, not evidence that the
matched function implements the requested behavior.

## Make the first lookup useful

Start a saved-knowledge topic search directly; `describe_schema` is needed for unfamiliar Cypher
or writes, not as a search preflight. Keep the query focused on the component or
concept you need. A whole implementation request can match incidental words
across unrelated repositories. For an initial lead, try `top_k=4, hops=0`, then
expand only when the question needs relationships or more evidence. These are
suggestions, not changed defaults or a completeness guarantee.

When the schema is known, narrow `labels` to the relevant kind of evidence—for
example, an existing service or decision label for architecture context. Do not
invent labels or treat historical memory as verified implementation. For an exact
name or a known repository path within a graph query, use projected Cypher. A repository name in search
text is **not** a scope filter. With the standard code schema and a source root
already established, a lookup can be:

```cypher
MATCH (f:Function)
WHERE f._source STARTS WITH '/workspace/example/src/'
  AND f.name CONTAINS 'auth'
RETURN f.name, f._source, f.line_start, f.line_end
ORDER BY f._source, f.line_start
LIMIT 10
```

Use the actual root and observed schema. The path restricts code citations; it
does not establish ownership of authored memories. A limited query can omit
matches even when its response is not marked truncated.

Stop recall when the relevant memory supplies the requested claim, scope, source
and qualifications. Read again to resolve specific missing evidence, a conflict,
current-code verification or an edit guard. For implementation questions, follow
relevant citations with bounded source reads. If hits are off-topic, change scope
or inspect source; increasing seeds and hops is not a relevance fix.
See [Capture and reuse conclusions](memory.md#save-a-conclusion-the-next-session-can-use)
and [Correct a stale finding](memory.md#correct-a-stale-finding).

=== "CLI"

    ```bash
    grag search "retry policy" --tokens 800 --hops 0
    grag context Memory:retry-policy --tokens 1600
    ```

=== "MCP"

    ```python
    search_knowledge(query="retry policy", labels=["Memory"], hops=0, token_budget=800)
    get_context(node_ids=["Memory:retry-policy"], hops=1, token_budget=1600)
    ```

    Use the labels and IDs returned by your own graph.

## Check completeness and budgets

Both retrieval calls return `truncated`, `omitted_nodes`, `omitted_edges`,
`omitted_properties`, and `expansion_limited`. MCP returns this metadata in a JSON
footer after `---`, including for `get_context`. A truncated answer is partial evidence. Omission
counts describe packing; `expansion_limited` separately reports a neighborhood
that exceeded 512 paths per seed. These fields do not claim exhaustive search
recall beyond the requested seeds, hops, and candidate limits.

When a query contains a code identifier (a word with an underscore, or CamelCase
with at least two humps), `search_knowledge` moves candidates whose `name` equals
that identifier to the front of the fused candidate list before diversity and
packing, keeping their fused order among themselves. Only the first 64 fused
candidates are inspected; capitalized ordinary words never count; and promotion
does not guarantee delivery within the budget. Several nodes with the same name
are all promoted in fused order.

`search_knowledge` also reports `label_hits`: distinct eligible candidate nodes per
label before fusion, for at most eight labels. Requested labels are listed with
their zeros first; an unrestricted search lists labels that had hits; and
`label_hits_omitted` counts labels left out. An omitted label is unknown, not zero.
A requested label that has no table in this graph is instead listed in
`unknown_labels`: that means the scope itself is wrong (a typo, or a different
database than intended), not that the label matched nothing — check the name or
the database rather than repeating the search. Like `label_hits`, the list is
trimmed from the end when a reply would otherwise exceed its budget;
`unknown_labels_omitted` counts any dropped names, so the wrong-scope signal
survives as a count even when no label name fits.
Counts are bounded by the per-label candidate shortlist, and they are the first
thing dropped (each counted as omitted) when a reply would otherwise exceed its
budget. An explicit zero for a memory label such as `Decision` means no eligible
candidate of that label matched this bounded search. If `excluded_evidence` is
above zero, inspect `evidence="all"` before saving a new record. See
[current and retained evidence](#current-and-retained-evidence) for the count's
limits: zero does not prove the label is empty or that another record is absent.
Counts never certify a claim.

Development: the MCP `search_knowledge` footer omits the duplicate
`included_node_ids` list. Context lines retain canonical node IDs, and `seeds`
retains canonical IDs, scores and match types for follow-up `get_context` calls.
MCP `get_context` still reports `included_node_ids` for requested-ID eligibility
checks, history and paging. REST, CLI JSON and Python responses retain the list
on both calls. Consumers of MCP search metadata should use the seed IDs for
follow-ups or the REST/Python response for a structured list of all included nodes.
REST and Python budgeting bounds the larger of the full JSON response and text.
MCP budgeting measures the text actually sent to the agent; see
[what the budget counts](#what-the-budget-counts).

Property values have no fixed character cutoff. When everything fits, long
strings and relationship properties are returned in full. Under a tight budget,
packing selects seed identities and connecting edges first, then adds complete
property values, with citations before other node properties. Every packed node
also carries its `_revision` guard token as a citation-class property, so an
ordinary read is sufficient for a guarded correction; the token is the last
thing budget pressure strips. Omission counts make missing records and
properties explicit. Returned edges always have both endpoints in the returned
graph. Vector payloads and null properties are omitted by design and do not
count as lost evidence.

Development: under budget pressure, additional selected code results can keep
their source, line range and lifecycle qualifiers while omitting their docstrings.
The leading result still gets its short preview. Omitted docstrings remain stored;
read the cited source or request `get_context(text_property="docstring")` for detail.
Expanded neighbors and authored memory prose keep their existing packing policy.

### What the budget counts

`token_budget` covers the **complete compact response on the serving
transport**. REST and Python calls bound the larger of the JSON payload
(including `seeds`, `subgraph`, context, and metadata) and the MCP text
(including its footer). MCP calls bound only the text the caller actually
receives (context plus footer); because the JSON serialization is typically
larger, an MCP reply at the same budget usually packs more evidence.
`response_token_estimate` reports the measured size on that transport;
`token_estimate` measures only `context`. Both use `ceil(UTF-8 bytes / 4)`,
a deterministic estimate rather than a model-specific tokenizer count. HTTP/MCP
protocol envelopes, client-added formatting, and tool errors are outside this
budget. The default remains 2000; supported budgets are **256–32,768**.
The compact application payload is bounded to `4 * token_budget` UTF-8 bytes;
this is **not a model-token guarantee**. Multilingual text, identifiers and JSON
can cost more tokens than this estimate; count with your harness's tokenizer
when enforcing a model context limit. Compared with v0.6.0, the same budget may
return fewer records: `seeds` and `subgraph` now contain only packed records and
properties, rather than an unbounded second copy of the retrieved graph.

## Page a long property

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

### Current and retained evidence

Search and context default to `evidence="current"`: explicitly superseded,
retracted, expired, disputed, and retained obsolete document nodes are excluded
before ranking and expansion. Use `evidence="all"` to inspect them. Legacy
`status` values `superseded`, `retracted`, and `expired` are recognized; task
`open`/`done` and other business statuses are unchanged. Unreviewed or legacy
evidence stays eligible; this is a selection policy, not a truth guarantee.
Cypher remains unfiltered. The footer names `evidence_policy`;
`excluded_evidence` counts distinct exclusions encountered during retrieval and
expansion, including hidden matches found by a bounded, unfiltered FTS recount.
The recount covers lexical matches, not vector-only hidden matches or every
record beyond the lexical shortlist. Zero therefore does not prove absence.
If the seeds look wrong or empty and `excluded_evidence` is above zero, the
record that mattered may be hidden: retry with `evidence='all'`.

### Whole-entity Cypher replies

Whole-entity Cypher replies (`RETURN n`, paths, lists and maps of entities)
omit derived vector properties. MCP also omits null columns on whole entities;
Python/REST retain those null columns. Entity IDs, relationships, provenance and
revision guards remain available. Explicit projections (`RETURN n.embedding`)
still return the requested values, including nulls in projected columns, maps
and lists. Query replies are row-limited; they do not
use search/context token budgets.


## Start small for narrow questions

For a known symbol, reuse the schema and project only its name, path, signature
and line range in Cypher. For text search, try a smaller budget (for example 800),
`hops=0`, and the relevant labels before expanding. The budget is a ceiling, not a
target, but current packing does not guarantee a short answer for a simple query
or a calibrated “no evidence” response. Inspect relevance and omissions.

There is no mandatory search → context → query sequence. Start with one projected
query for a known fact, search for discovery, and fetch more context only when the
answer needs it. Cache the schema and use its revision check when it may have
changed. Scalar, projected and empty queries skip catalog reads needed only for
graph serialization. These shortcuts retain freshness and completeness metadata.

The installed skill keeps everyday guidance in `SKILL.md`; ingestion, correction
history and operational procedures live in references loaded when needed. Harnesses
choose how much tool and skill text enters a model context. Count tool descriptions,
input schemas, server instructions, activated skill/references and returned evidence
separately when measuring a workflow; response `token_budget` covers only the
retrieval payload, not the whole session.

BM25 needs no model and does not import NumPy, the polar codec or model runtimes on
its normal first-search path. Optional embeddings add model preparation/download,
cached model load, document embedding backlog and query inference costs. Measure
those separately from database startup and warm searches; a cached-model run does
not measure first-time download cost. See [optional embeddings](embeddings.md).

See [code coverage](code.md), [embedding tradeoffs](embeddings.md), and
[resource limits](../reference/limits.md).


## How search ranks and expands


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

For [task resumption](resume.md), use explicit status, scope and priority queries.
Search relevance is not task priority; `evidence="current"` does not imply unfinished
work or unanswered questions.
