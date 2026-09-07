# Agent workflow evaluation

This is a development evaluation, not another grag setup step. It uses a small,
fictional project with hand-authored questions and required evidence. All databases,
sources, client registrations and relocation backups are disposable. No answer model
or remote embedding provider is called.

From an editable checkout with development dependencies:

```sh
python -m pytest tests/test_workflow_quality.py tests/test_shared_agent_workflow.py
python tests/workflow_eval.py --output /tmp/grag-workflows.json --scenarios
```

The ordinary test suite runs the keyword/graph contracts and both plain-folder and
Git lifecycle drills, including on the existing Windows/Linux/macOS CI matrix.
Set `GRAG_WORKFLOW_REPORT` to retain the test's quality report, and
`GRAG_SHARED_WORKFLOW_REPORT` for the MCP report. Cross-platform CI results still
need to be observed; a local run does not certify the other platforms.

To evaluate semantic relevance, install the existing `embed-local` extra and cache
its default `BAAI/bge-small-en-v1.5` model first, then run:

```sh
HF_HUB_OFFLINE=1 python tests/workflow_eval.py \
  --embeddings --scenarios --source-files 250 --output /tmp/grag-hybrid.json
GRAG_TEST_COMMAND=/absolute/path/to/grag GRAG_WORKFLOW_EMBEDDINGS=1 \
  python -m pytest tests/test_shared_agent_workflow.py
```

The installed-command option launches outside the repository and does not inject
`PYTHONPATH`. Without it, the MCP test launches this checkout with the current Python.
The shared test uses two proxies, one real localhost server, document ingestion,
overlapping memory edits and searches, revision conflicts, independent disconnect,
background embedding completion, stop/restart and durable receipt replay. It does
not automate the actual Claude Code or Cursor UI.

For optional model-token calibration, install `tiktoken` in the development
environment and add `--encodings cl100k_base o200k_base`. The first use may download
public tokenizer vocabularies; later runs can use the cache. These are named
tokenizers, not a claim about every model or harness's token accounting. The runner
counts plain tool-response text; chat wrappers, prompts, schemas and reasoning are
excluded. Calibration includes prose, source code, JSON, Spanish, Hebrew, Chinese,
and emoji. No tokenizer dependency is added to grag.

## What is measured

- **FTS / FTS plus graph** use the production keyword ranking and packing.
- **Vector / hybrid / hybrid plus graph** run only with actual local embeddings.
  They share the same completed embeddings, three-seed limit, and 1,000/3,000
  estimated-token budgets. The vector baseline packs its seeds without expansion.
- **Explicit Cypher** handles four structural questions. Its path is hand-selected;
  this is not an evaluation of autonomous query planning. A packed graph projection
  permits evidence comparison, while `raw_cypher_tokens` separately records the
  actual MCP Cypher response cost. The raw tool has no retrieval-budget contract.
- **Seed hit rate and reciprocal rank** assess starting nodes. **Node/edge/text
  recall** assess all required evidence actually delivered after packing. IDs must
  match exactly; edges must have the expected type and direction. A node ID alone
  cannot satisfy the required prose. Metrics with no applicable gold items are null.
- **Citation checks** validate source availability and function location at retrieval
  time. Missing line/source fields are distinguished from incorrect citations.
  This is not proof that every claim made by an LLM is entailed by its sources.
- **Complete evidence** requires all gold nodes, edges, phrases and citations, with
  no forbidden obsolete text. An unanswerable question is measured separately for
  the context it nevertheless receives. Search results do not themselves constitute
  a hallucinated answer or promise automatic abstention.
- **Cost baselines** compare full response text against reading all fixture files
  and against an oracle that knows the relevant files. Both can batch reads in one
  call. Saved-token fractions are reported only for complete evidence; negative
  savings remain visible. One retrieval call plus separately reported schema/setup
  work is not evidence of fewer calls in an autonomous agent session.
- **Latency** includes the public read's required source verification, retrieval and
  packing. The first query after reopening is distinguished from later samples;
  neither flushes OS caches. Model initialization/embedding belongs to setup cost.
- **Stateful drills** measure corrections, refused stale edits, exact text paging,
  source bytes hashed, freshness deadlines, shared checks, concurrent reads/writes,
  restart, task resumption and a moved checkout with an external database and an
  obsolete client command. Timing numbers are observations, not CI performance SLOs.

`questions.json` and `memories.json` are the gold fixture; add independent questions
and evidence rather than deriving expected answers from retrieval output. The
report includes a fixture hash and runtime/provider identity. Reports retain failed
quality cases: long-note packing, superseded evidence and vague task search are
deliberate challenges. Passing the evaluator's contract tests is not perfect recall.

These ten questions are a regression and diagnostic set. They do not replace
user-judged questions on larger real projects. `grag bench` remains the separate
synthetic vector-neighbor/codec benchmark; its recall is not answer accuracy.

M27 adds explicit exact excerpts to oversized search evidence. The evaluator
checks their text, character coordinates and SHA-256 against the independently
materialized authored values before crediting an answer; it still treats the
full property as omitted. Question/gold fixtures stay unchanged. Repository
provenance is checked as a directory, while function citations require a file
and a matching source line. This corrects a diagnostic false positive in the
original reports without changing which answers pass.
