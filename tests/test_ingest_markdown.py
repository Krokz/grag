"""Section-aware Markdown ingest: heading tree -> Document/Section/Chunk graph,
code-symbol links, authoritative re-sync, CLI and MCP entry points."""

from __future__ import annotations

import json

from grag import cli
from grag.config import GragConfig
from grag.core.types import (
    CodeIngestRequest,
    IngestDocument,
    IngestRequest,
    SearchRequest,
)
from grag.ingest.code import ingest_code
from grag.ingest.loaders import _source_identity, ingest_documents
from grag.ingest.markdown import heading_path, parse_sections
from grag.service import GragService

BIBLE = """# Algo Bible

Intro paragraph before any section.

## 1. Overview

The system routes orders. See `OrderRouter` for the entry point and
`route_order()` for the algorithm.

### 1.1 Retry policy

Retries use `backoff.compute_delay`. Not `unknown_symbol`.

```python
# not a heading
def x():
    pass
```

## 2. Storage

Persistence lives in `algo.storage`.

Setext Heading
--------------

Body under a setext level-2 heading.
"""


def _rows(engine, cypher, **params):
    return engine.execute(cypher, params or None).rows


# --- parser ------------------------------------------------------------------------------


def test_parse_sections_builds_heading_tree():
    title, sections = parse_sections(BIBLE)
    assert title == "Algo Bible"
    titles = [(s.level, s.title) for s in sections]
    assert titles == [
        (1, "Algo Bible"),
        (2, "1. Overview"),
        (3, "1.1 Retry policy"),
        (2, "2. Storage"),
        (2, "Setext Heading"),
    ]
    # the fenced "# not a heading" stayed inside 1.1's body
    assert "# not a heading" in sections[2].body
    assert sections[2].parent == 1 and sections[1].parent == 0
    assert heading_path(sections, 2) == "Algo Bible > 1. Overview > 1.1 Retry policy"
    assert sections[0].body == "Intro paragraph before any section."


def test_parse_sections_preamble_without_h1():
    title, sections = parse_sections("free text\n\n## Only H2\n\nbody")
    assert title is None
    assert [(s.level, s.title) for s in sections] == [(0, "Preamble"), (2, "Only H2")]
    assert sections[1].parent is None  # the preamble never parents


def test_parse_sections_ignores_rule_after_blank_line():
    _, sections = parse_sections("# T\n\npara\n\n---\n\nmore")
    assert [s.title for s in sections] == ["T"]
    assert "---" in sections[0].body


# --- graph shape -------------------------------------------------------------------------


def test_sections_ingest_builds_document_graph(engine):
    resp = ingest_documents(
        engine,
        engine.config,
        IngestRequest(
            documents=[IngestDocument(text=BIBLE, source="docs/bible.md")],
            sections=True,
            chunk_size=200,
            chunk_overlap=20,
        ),
    )
    assert resp.documents == 1 and resp.sections == 5
    assert resp.nodes_created >= 5  # at least one chunk per non-empty section
    assert resp.code_links == 0  # no code graph yet

    ident = _source_identity("docs/bible.md", BIBLE, {})
    assert _rows(engine, "MATCH (d:Document) RETURN d.id, d.title, d.sections") == [
        [ident, "Algo Bible", 5]
    ]
    sections = _rows(
        engine, "MATCH (s:Section) RETURN s.id, s.heading_path, s.level ORDER BY s.position"
    )
    assert sections[0][0] == f"{ident}#algo-bible"
    assert sections[2] == [
        f"{ident}#algo-bible/1-overview/1-1-retry-policy",
        "Algo Bible > 1. Overview > 1.1 Retry policy",
        3,
    ]
    assert _rows(engine, "MATCH ()-[r:HAS_SECTION]->() RETURN count(r)") == [[5]]
    assert _rows(engine, "MATCH ()-[r:SUBSECTION_OF]->() RETURN count(r)") == [[4]]
    assert _rows(engine, "MATCH ()-[r:NEXT_SECTION]->() RETURN count(r)") == [[4]]
    # every chunk hangs off exactly one section, and carries its citation
    chunk_rows = _rows(
        engine,
        "MATCH (c:Chunk)-[:IN_SECTION]->(s:Section) RETURN c.id, s.id, c.meta",
    )
    assert len(chunk_rows) == resp.nodes_created
    for cid, sid, meta in chunk_rows:
        assert cid.startswith(sid + "@")
        assert json.loads(meta)["heading_path"].startswith("Algo Bible")
    # the code-hook tables exist only once a code graph exists
    tables = {r[1] for r in _rows(engine, "CALL SHOW_TABLES() RETURN *")}
    assert "IMPLEMENTS" not in tables


def test_sections_ingest_links_backtick_mentions_to_code(engine, tmp_path):
    pkg = tmp_path / "algo"
    pkg.mkdir()
    (pkg / "router.py").write_text(
        "class OrderRouter:\n    def route(self):\n        return 1\n\n\n"
        "def route_order():\n    return 2\n",
        encoding="utf-8",
    )
    (pkg / "backoff.py").write_text("def compute_delay():\n    return 3\n", encoding="utf-8")
    (pkg / "storage.py").write_text("X = 1\n", encoding="utf-8")
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(pkg)]))

    resp = ingest_documents(
        engine,
        engine.config,
        IngestRequest(
            documents=[IngestDocument(text=BIBLE, source="docs/bible.md")],
            sections=True,
        ),
    )
    assert resp.code_links == 4
    links = {
        (r[0], r[1], r[2])
        for r in _rows(
            engine,
            "MATCH (s:Section)-[r:MENTIONS_FUNCTION|MENTIONS_CLASS|MENTIONS_MODULE]->(n) "
            "RETURN s.title, label(r), n.name",
        )
    }
    assert links == {
        ("1. Overview", "MENTIONS_CLASS", "OrderRouter"),
        ("1. Overview", "MENTIONS_FUNCTION", "route_order"),
        ("1.1 Retry policy", "MENTIONS_FUNCTION", "compute_delay"),
        ("2. Storage", "MENTIONS_MODULE", "storage"),
    }
    tables = {r[1] for r in _rows(engine, "CALL SHOW_TABLES() RETURN *")}
    assert {"IMPLEMENTS", "IMPLEMENTS_CLASS"} <= tables


def test_sections_reingest_is_authoritative_and_replaces_flat_chunks(engine):
    src = "docs/bible.md"
    flat = ingest_documents(
        engine,
        engine.config,
        IngestRequest(documents=[IngestDocument(text=BIBLE, source=src)], chunk_size=150),
    )
    assert flat.nodes_created > 1
    first = ingest_documents(
        engine,
        engine.config,
        IngestRequest(documents=[IngestDocument(text=BIBLE, source=src)], sections=True),
    )
    assert first.nodes_pruned == flat.nodes_created  # flat chunks replaced
    assert _rows(engine, "MATCH (c:Chunk) RETURN count(c)") == [[first.nodes_created]]

    shorter = "# Algo Bible\n\n## 1. Overview\n\nOnly this remains.\n"
    second = ingest_documents(
        engine,
        engine.config,
        IngestRequest(documents=[IngestDocument(text=shorter, source=src)], sections=True),
    )
    assert second.sections == 2
    assert _rows(engine, "MATCH (s:Section) RETURN count(s)") == [[2]]
    assert _rows(engine, "MATCH (c:Chunk) RETURN count(c)") == [[1]]
    assert _rows(engine, "MATCH ()-[r:SUBSECTION_OF]->() RETURN count(r)") == [[1]]
    # idempotent
    third = ingest_documents(
        engine,
        engine.config,
        IngestRequest(documents=[IngestDocument(text=shorter, source=src)], sections=True),
    )
    assert third.nodes_pruned == 0


def test_search_over_sections_reaches_the_citing_section(tmp_path):
    svc = GragService(GragConfig(db_path=tmp_path / "sec.lbdb"))
    try:
        svc.ingest(
            IngestRequest(
                documents=[IngestDocument(text=BIBLE, source="docs/bible.md")],
                sections=True,
            )
        )
        resp = svc.search_knowledge(SearchRequest(query="retries backoff", hops=1))
        ids = {n.id for n in resp.subgraph.nodes}
        assert any(i.startswith("Chunk:") for i in ids)
        assert any("1-1-retry-policy" in i and i.startswith("Section:") for i in ids)
        assert "1.1 Retry policy" in resp.context
    finally:
        svc.close()


# --- entry points ------------------------------------------------------------------------


def test_cli_ingest_sections_walks_directories(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "bible.md").write_text(BIBLE, encoding="utf-8")
    (docs / "notes.txt").write_text("plain notes", encoding="utf-8")
    db = tmp_path / "cli.lbdb"
    assert cli.main(["--db", str(db), "ingest", "--sections", str(docs)]) == 0
    out = capsys.readouterr().out
    assert "2 document(s) from 2 file(s)" in out
    assert "2 document node(s)" in out and "section(s)" in out


def test_mcp_ingest_docs_tool(tmp_path):
    from grag.mcp_server import server as mcp_server

    (tmp_path / "bible.md").write_text(BIBLE, encoding="utf-8")
    svc = GragService(GragConfig(db_path=tmp_path / "mcp.lbdb"))
    try:
        payload = json.loads(mcp_server.ingest_docs(svc, [str(tmp_path / "bible.md")]))
        assert payload["documents"] == 1 and payload["sections"] == 5
        assert payload["files_read"] == 1 and payload["warnings"] == []
        queued = json.loads(
            mcp_server.ingest_docs(svc, [str(tmp_path / "bible.md")], background=True)
        )
        assert queued["status"] == "queued" and queued["kind"] == "ingest"
    finally:
        svc.close()
