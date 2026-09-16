# Development and evaluation

Start with [Architecture](architecture.md) for component responsibilities,
database ownership, request flows and persistence boundaries.

Release source archives include only root-scoped project files. License discovery
is limited to the project `LICENSE`; nested virtual environments and local audit
files must stay outside distributions. The publication gate checks archive scope.

**From source** (for development). Build the UI **first** — `pip install` needs the
built bundle at `src/grag/api/static` (the wheel's force-include; see `pyproject.toml`):

```bash
cd ui && npm ci && npm run build && cd ..   # builds the UI into src/grag/api/static/
pip install -e .            # core: engine, REST, MCP, FTS — no torch, no GPU stack
pip install -e ".[dev]"     # tests
pip install -e ".[code]"          # optional: tree-sitter parsers; see language coverage
pip install -e ".[embed-local]"   # optional: local embeddings (fastembed/ONNX, still no torch)
pip install -e ".[embed-remote]"  # optional: OpenAI-compatible remote embeddings
```

## Windows source builds

Before `pip install` on Windows, use an **x64 Visual Studio Developer Command
Prompt** with MSVC and Strawberry Perl on PATH:

```bat
python scripts/build_windows_runtime.py
python -m pip install -e ".[dev]"
```

The builder downloads official OpenSSL 3.5.8 source, verifies its pinned SHA-256,
and compiles two DLLs into the ignored `src/grag/_runtime/` directory. Windows
wheels include those DLLs, the OpenSSL license, and source/build provenance.
The build hook refuses an incomplete Windows runtime and excludes it from other
platforms' wheels and source archives. Source archives include the builder.
Updates must change the source pin/checksum together and pass the clean-wheel
gate; there is no end-user runtime downloader or fallback to another app's DLLs.

## Validate the distribution

```bash
python -m pip install build
python -m build
python scripts/smoke_wheel.py dist/<built-wheel>.whl
```

The smoke creates a disposable environment outside the checkout, installs the
wheel without developer extras, restricts PATH, and checks native writes/reopen,
the packaged UI, real stdio MCP, legacy console encoding, cold-cache diagnostics,
explicit extension preparation and subsequent offline FTS. The clean-wheel CI
gate runs on Linux/macOS/Windows, including Windows Python 3.10, 3.13 and 3.14.
Pass `--code` to also install the optional code extra and verify first-use grammar
preparation followed by offline parsing; CI does this on each platform.
The publish workflow waits for this gate and includes the verified Windows wheel
alongside the universal wheel and source archive.

## Use the checkout from your harness

To have the normal CLI and MCP launcher use this checkout, install it in editable
mode with pipx after building the UI:

```bash
pipx install --force --editable '.[code,embed-local]'
command -v grag
```

This replaces pipx's `gragdb` installation with a link to the checkout. New
processes load your Python edits directly; stop the server for the selected
database (`grag --db <file> stop`) and reconnect MCP after editing running code.
UI changes still need `npm run build` in `ui`. Keep the checkout at this path.
Check that `command -v grag` and the MCP registration select the intended launcher;
another installation earlier on `PATH` can still run different code. The database
selection is independent of the installation.

From a Python environment with the development dependencies, exercise the
installed command's MCP memory loop without a `PYTHONPATH` override:

```bash
GRAG_TEST_COMMAND="$(command -v grag)" python -m pytest -o addopts= -q tests/test_agent_workflow.py
```

The test launches that command outside the checkout, uses temporary source and
database files, and verifies ingestion, recall, corrections, fresh citations,
retry handling, and memory after restarting MCP. It uses full-text search; local
embeddings and language grammars may need an initial download before offline use.

## Qualify skill behavior in a real harness

SDK and packaging tests do not establish whether a model discovers a skill or
loads its references. When changing the bundle, also use fresh authenticated
Claude Code and Cursor sessions with a disposable repository and database.
Keep the harness's normal permissions and approve only the fixture's grag tools
and commands. Confirm the CLI inside the agent's shell and the written MCP
launcher both resolve to the candidate installation; a login shell can change
`PATH`. A successful login-status check alone does not prove model requests work.

Check these cases with actual tool activity and independently read back the graph:

| Case | Evidence to check |
|---|---|
| Connected MCP with CLI also permitted | Routine reads, writes and ingestion use MCP without the prompt naming a transport. Check actual calls, not just a connected badge. |
| MCP unavailable; explicit CLI request | Explain an availability fallback and keep the intended graph; honor a user's CLI choice even when MCP is connected. |
| Project skill, then personal skill in a new repo | `/grag` loads the operations reference and maps supported source once; a cited symbol exists and ignored source is absent. |
| Fresh session with existing code or only authored memory | Existing records survive; no automatic widening, duplicate memory or forced scan. |
| Explicit memory correction and review | Memory reference loads when needed; revision guards, source and agent attribution accompany the edit; earlier values remain queryable in history. |
| Explicit code scope change | Ingestion reference loads; registered paths match the request and authored memory survives. |
| Overlapping skill directories | Observe the selected reference path; keep installed copies synchronized instead of assuming project precedence. |

Record harness version, model, installation paths, prompts, tool calls and stored
outcomes. A passing session is a behavioral sample, not a guarantee that every
model follows every instruction. Keep essential storage/scope checks in grag's
runtime. Stop the fixture's server and restore any temporary personal skill when
finished. Never replace an existing personal bundle just to run this check.


## Checks and evaluation

```bash
python -m pytest tests/            # unit, recovery and agent-workflow checks
ruff check src tests && mypy src/grag   # CI gates on both
grag bench                        # codec recall/latency/RSS table
cd ui && npm run build            # rebuilds the UI into src/grag/api/static/
```

For evidence-level retrieval evaluation, run
`python tests/workflow_eval.py --scenarios --output /tmp/grag-workflows.json`.
The checked-in questions compare keyword search, optional real local embeddings,
graph expansion and explicit relationship queries. Reports measure required text,
nodes, edges and citations after packing, plus latency and token costs against file
reading. See the [workflow evaluation guide](https://github.com/Krokz/grag/blob/main/tests/fixtures/workflows/README.md)
for the real MCP drill, optional tokenizer calibration, and measurement limits.
Vector-neighbor recall from `grag bench` is a separate metric.

See **[CONTRIBUTING.md](https://github.com/Krokz/grag/blob/main/CONTRIBUTING.md)** for the branching model (Gitflow-lite:
`main` + `dev` + `feature`/`release`/`hotfix`), PR rules, and how releases are
cut and published to PyPI.

The embedded engine allows one writer process. Its buffer pool is only part of
total resident memory: Python, native query work and optional models also allocate.
Close services and engines you create. Compressed-codec encoding adds write-time
work; measure it on your data.

## Build this documentation

Use a separate environment; these tools are not grag runtime dependencies:

```bash
python -m venv /tmp/grag-docs
/tmp/grag-docs/bin/python -m pip install -r requirements-docs.txt
/tmp/grag-docs/bin/python -m mkdocs serve
```

Use the equivalent virtual-environment path on Windows. `mkdocs build --strict`
checks the navigation, internal links and anchors and writes `site/` (Git-ignored).
The documentation workflow validates changes on PRs and `dev`; it deploys `main`
through GitHub Pages. Repository Settings → Pages must use **GitHub Actions** as
the publishing source. There is no runtime or database dependency for the site.

When changing behavior, update its guide/reference alongside the code. Keep the
README to installation, the first memory loop and links. Update the version on
the overview page when the documented release changes. Review deployment changes
through the same dev/main flow; documentation publication does not require a
PyPI version bump or tag.

### Keep pages easy to scan

- Start a guide with its outcome and smallest useful example. Put language-specific
  behavior and detailed contracts in the reference pages, linked where needed.
- Use descriptive headings so the page outline and search results lead to an
  answer. Keep tables focused on one family of commands or settings.
- Use content tabs for equivalent alternatives, such as CLI/MCP or installers.
  Use collapsible details for optional explanations; keep prerequisites and
  recovery warnings visible.
- Preserve existing page paths and heading anchors when moving content. Leave
  a short explanation and link at an old target rather than duplicating the guide.
- Preview desktop, narrow screens and both color schemes. Check search, tabs,
  navigation and internal links after a strict build.

## Demo

```bash
# build the demo knowledgebase (fictional company handbook, entities + relations)
python examples/build_example.py

# serve REST + the graph UI at http://127.0.0.1:8471
# (note: start it from a normal terminal — servers launched inside an agent
# sandbox get torn down and can't be reached from your browser)
grag --db examples/knowledge.lbdb serve

# single-process mode: UI + REST + MCP on one live .lbdb (recommended for
# dogfooding — the UI sees MCP writes the moment they land)
grag --db examples/knowledge.lbdb serve --with-mcp
#   UI  → http://127.0.0.1:8471/
#   MCP → http://127.0.0.1:8471/mcp   (streamable-http; point MCP clients here)

# or answer 3 demo questions end-to-end in the terminal
python examples/demo_e2e.py
```

The UI opens on Graph. Memories provides searchable project memories, open tasks,
recent changes, evidence/history and guarded correction actions; Health shows
the selected owner's state. See the
[UI guide](guides/ui.md). The graph explorer supports click to inspect and
double-click to expand neighbors, with a Cypher console, schema sidebar and search.
Click a label in the legend to view
that label and its one-hop relationships. **Export SVG view** saves the currently
loaded, filtered view; **Export full SVG** downloads a consistent snapshot of every
user node and edge, then lays it out in the browser. The download includes only
keys, labels and endpoints; document bodies, vectors and other properties are not
needed for the drawing. It uses `/api/graph/export`, outside the ordinary 1 MiB
API reply limit. Capture uses temporary disk space and briefly holds off writes;
the lock is released before download. Available browser memory and layout time
still constrain very large drawings. Use `grag export` for a restorable data backup.

**One owning process per file.** Direct `grag mcp` opens the database and cannot
run alongside another owner. `mcp --auto-serve` is a thin proxy and can share the
existing owner with other clients. `serve --with-mcp` mounts MCP inside the
REST/UI process, so UI requests read the same committed graph that MCP updates.
Use `--mcp-path` to change the mount path (default `/mcp`).

Browser workflow regressions use temporary multi-database fixtures:

```bash
cd ui
npx playwright install chromium
npm run test:e2e
```

Build the UI and install the source Python package first. Set `GRAG_UI_TEST_PYTHON`
to an explicit Python interpreter if needed. `GRAG_UI_TEST_CHROME=1` uses installed
Chrome locally; CI installs Playwright Chromium. These tests never use project or
personal graph registrations. They cover evidence/history, correction conflicts,
ambiguous-write retries, retirement, custom keys and database switching.

## Performance measurements

Use the [product boundary](architecture.md#product-boundary) when proposing work:
prioritize the existing ingest, retrieve and remember workflow and its reliability.

`tests/test_perf.py` checks small-fixture cold start, warm search latency and RSS
with CI headroom. It is not a large-project performance guarantee. `grag bench`
reports synthetic recall@10, mean query/encoding time, peak-RSS growth and code
bytes per codec. The workflow evaluator separately reports end-to-end latency,
packed evidence and token cost. Keep optional models lazy and preserve query,
work and response limits when changing these paths.

### Judge retrieval before tuning it

`tests/retrieval_judging.py` evaluates source-navigation judgments against a pinned
commit using `git archive`. Questions, tests, audit notes and authored answer
memories remain outside the searchable corpus. A shallow clone must first fetch
the commit named in `tests/fixtures/retrieval_judgments.json`.

```bash
python tests/retrieval_judging.py --output retrieval.json --check-baseline
```

The runner builds separate disposable graphs for code-only and code-plus-docs
retrieval. Test-only logical source URIs, repository identity and timestamps make
IDs, searchable metadata and citation lengths independent of the extraction
directory. Citations are checked against the archived files. Reports record
source, judgment and searchable-graph hashes, candidate discovery, rank,
selection and whether a useful citation survives two budgets and hop counts.

Each reply is graded twice. The original mixed gold accepts a known code symbol
**or** a named documentation section; adding docs gives that criterion more
reachable targets, so its gain is not proof of better code navigation. The separate
`code_navigation` and `code_summary` metrics use only the existing code-symbol
targets, identical in both corpus scopes and recorded under `code_gold_sha256`.
They distinguish selected/expanded code before packing from cited code actually
delivered as a seed or neighbor. Document citations cannot satisfy code gold.
Seed labels and mention-link inventories help investigate displacement and missing
connections; a packed mention edge alone does not establish causal improvement.

Use `--hops 0 1 2` for an additional expansion diagnostic: a chunk can need two
hops through its section to reach a mentioned function. This produces a different
case matrix, so compare it separately from the default 0/1-hop CI baseline.

Natural questions, keyword/symbol controls and unsupported questions are scored
separately. CI compares each case with `tests/fixtures/retrieval_baseline.json`:
losing a known useful candidate, first result, selected rank or delivered citation
fails even if another case improves. Mixed and fixed-code metrics are checked
independently: a documentation win cannot hide a code-navigation loss.
Unsupported-query hit counts remain
diagnostics: matching text is not a claim that the question is answerable, and
empty retrieval is not an abstention requirement. Inspect saved replies before explicitly
refreshing a baseline with `--write-baseline`; changing gold or corpus hashes is
an incompatible comparison, not an automatic reset. CI retains the full report.
Passing this gate prevents measured regressions; known misses remain visible
and do not become successful answers merely because the baseline passes.

Ranking experiments live only in `tests/retrieval_variants.py`. Use
`--variant fields`, `fields_weighted`, `table_rrf` or `table_rrf_scaled` to compare
them without changing production. `--embeddings` evaluates the configured default
BGE-small model from its local cache, with downloads disabled and embedding
completion required; `--vector-only` separates vector recall from hybrid fusion.
The report records model asset hashes. Install the embedding extra and prepare
the cache separately before running that optional arm.

For the fixed `top_k=8` benchmark, `--embeddings --variant fusion_top8` and
`fusion_top16` restrict each modality to its top 8 or 16 unique finite-scored hits
before the existing RRF and label cap. Boundary ties use canonical ID order.
Compare delivered citations and individual losses, including vector-only hits
cut off before fusion; a narrower list can also remove label diversity. These are
evaluation-only variants, not production settings. In these reports `candidate_hit`
includes the lexical shortlist plus fused survivors, so a lost vector candidate
can reflect this cutoff rather than failure of the embedding search itself.

These are development judgments of known useful navigation targets, including
related query variants and cases written from existing source contracts before
examining experiment results. They are not exhaustive relevance labels, independent
human grading or a held-out accuracy benchmark. Inspect replies for qualifications
and alternative answers. The single corpus is documentation-rich; questions share
its development/failure-investigation context and vocabulary. The experiment is
not blinded or authored independently of that context, and says nothing measured
about repositories with sparse documentation. Neither successful navigation nor smaller replies establishes
correct agent answers or whole-session token savings. Those require matched
agent runs with independent task grading and complete usage accounting.
