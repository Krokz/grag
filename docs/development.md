# Development and evaluation

Start with [Architecture](architecture.md) for component responsibilities,
database ownership, request flows and persistence boundaries.

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

The UI has a graph explorer (click to inspect, double-click to expand neighbors),
Cypher console, schema sidebar and search. Click a label in the legend to view
that label and its one-hop relationships. **Export SVG view** saves the currently
loaded, filtered view; **Export full SVG** requests the full graph and lays it out
in the browser, subject to server response limits and available browser resources.

**One owning process per file.** Direct `grag mcp` opens the database and cannot
run alongside another owner. `mcp --auto-serve` is a thin proxy and can share the
existing owner with other clients. `serve --with-mcp` mounts MCP inside the
REST/UI process, so UI requests read the same committed graph that MCP updates.
Use `--mcp-path` to change the mount path (default `/mcp`).

## Performance measurements

`tests/test_perf.py` checks small-fixture cold start, warm search latency and RSS
with CI headroom. It is not a large-project performance guarantee. `grag bench`
reports synthetic recall@10, mean query/encoding time, peak-RSS growth and code
bytes per codec. The workflow evaluator separately reports end-to-end latency,
packed evidence and token cost. Keep optional models lazy and preserve query,
work and response limits when changing these paths.
