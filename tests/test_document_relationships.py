"""M08: authoritative document edges without deleting authored knowledge."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grag.config import GragConfig
from grag.core.errors import GragError
from grag.core.mutate import define_schema, upsert_edges, upsert_nodes
from grag.core.types import (
    DOCUMENT_OWNER_PROP,
    CodeIngestRequest,
    DefineSchemaRequest,
    IngestDocument,
    IngestRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.ingest.code import ingest_code
from grag.ingest.loaders import _source_identity, ingest_documents
from grag.service import GragService


def _ingest(engine, text, source="docs/guide.md", *, sections=True, label="Chunk"):
    return ingest_documents(
        engine,
        engine.config,
        IngestRequest(
            documents=[IngestDocument(text=text, source=source)],
            sections=sections,
            label=label,
            chunk_size=30,
            chunk_overlap=0,
        ),
    )


def _rows(engine, cypher, **params):
    return engine.execute(cypher, params or None).rows


@pytest.fixture()
def code(engine, tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "module.py").write_text(
        "def alpha():\n    return 1\n\nclass Widget:\n    pass\n"
    )
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(root)]))
    return engine


def _edge(engine, table, *, source="agent", properties=None):
    row = _rows(
        engine, f"MATCH (a)-[r:{table}]->(b) RETURN label(a), a.id, label(b), b.id"
    )[0]
    return UpsertEdge(
        type=table,
        from_label=row[0],
        from_key=row[1],
        to_label=row[2],
        to_key=row[3],
        source=source,
        properties=properties or {},
    )


def _legacy_link(engine, source, table):
    """A pre-M08 graph: the edge table has never had an ownership column."""
    from grag.ingest.markdown import _DOC_NODE_TABLES

    owner = _source_identity(source, "", {})
    sid = owner + "#guide"
    from_label, to_label = (
        ("Document", "Section") if table == "HAS_SECTION" else ("Section", "Function")
    )
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(
            node_tables=_DOC_NODE_TABLES,
            rel_tables=[
                RelTableSpec(name=table, from_label=from_label, to_label=to_label)
            ],
        ),
    )
    upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(
            nodes=[
                UpsertNode(label="Document", key=owner, source=source),
                UpsertNode(label="Section", key=sid, source=source),
            ]
        ),
    )
    from_key, to_key = (
        (owner, sid)
        if table == "HAS_SECTION"
        else (sid, _rows(engine, "MATCH (f:Function) RETURN f.id")[0][0])
    )
    upsert_edges(
        engine,
        engine.config,
        UpsertEdgesRequest(
            edges=[
                UpsertEdge(
                    type=table,
                    from_label=from_label,
                    from_key=from_key,
                    to_label=to_label,
                    to_key=to_key,
                    source=source,
                )
            ]
        ),
    )


@pytest.mark.parametrize(
    "table,symbol",
    [
        ("MENTIONS_FUNCTION", "alpha"),
        ("MENTIONS_CLASS", "Widget"),
        ("MENTIONS_MODULE", "module"),
    ],
)
def test_removed_mentions_disappear_between_surviving_nodes(code, table, symbol):
    first = _ingest(code, f"# Guide\n\nUse `{symbol}`.")
    assert first.code_links == 1
    sid = _rows(code, "MATCH (s:Section) RETURN s.id")[0][0]
    assert _rows(code, f"MATCH ()-[r:{table}]->() RETURN r.{DOCUMENT_OWNER_PROP}") == [
        [_source_identity("docs/guide.md", "", {})]
    ]
    result = _ingest(code, "# Guide\n\nNo code references.")
    assert result.code_links == 0 and not result.warnings
    assert _rows(code, f"MATCH ()-[r:{table}]->() RETURN count(r)") == [[0]]
    assert _rows(code, "MATCH (s:Section) RETURN s.id") == [[sid]]
    assert _rows(code, "MATCH (f:Function) RETURN f.name") == [["alpha"]]


def test_reordering_surviving_sections_replaces_reading_order(engine):
    _ingest(engine, "# Guide\n\n## One\n\nFirst.\n\n## Two\n\nSecond.")
    before = {r[0] for r in _rows(engine, "MATCH (s:Section) RETURN s.id")}
    for _ in range(2):
        _ingest(engine, "# Guide\n\n## Two\n\nSecond.\n\n## One\n\nFirst.")
        assert _rows(
            engine,
            "MATCH (a)-[:NEXT_SECTION]->(b) RETURN a.title,b.title ORDER BY a.title",
        ) == [
            ["Guide", "Two"],
            ["Two", "One"],
        ]
        assert {r[0] for r in _rows(engine, "MATCH (s:Section) RETURN s.id")} == before


@pytest.mark.parametrize("label", ["Chunk", "Passage"])
def test_heading_move_and_empty_document_reconcile_structure(engine, label):
    _ingest(engine, "# Guide\n\n## Parent\n\n### Child\n\nBody.", label=label)
    _ingest(engine, "# Guide\n\n## Parent\n\n## Child\n\nBody.", label=label)
    assert _rows(
        engine,
        "MATCH (a)-[:SUBSECTION_OF]->(b) RETURN a.title,b.title ORDER BY a.title",
    ) == [
        ["Child", "Guide"],
        ["Parent", "Guide"],
    ]
    result = _ingest(engine, "", label=label)
    assert result.sections == 0 and result.nodes_pruned == 4 and not result.warnings
    assert _rows(engine, "MATCH (s:Section) RETURN count(s)") == [[0]]
    assert _rows(engine, f"MATCH (c:{label}) RETURN count(c)") == [[0]]
    assert _rows(
        engine,
        "MATCH ()-[r:HAS_SECTION|SUBSECTION_OF|NEXT_SECTION]->() RETURN count(r)",
    ) == [[0]]


@pytest.mark.parametrize("source", ["docs/guide.md", "agent-session", None])
def test_public_upsert_adopts_generated_edge_and_preserves_its_properties(code, source):
    _ingest(code, "# Guide\n\nUse `alpha`.")
    code.execute_write("ALTER TABLE MENTIONS_FUNCTION ADD reason STRING")
    edge = _edge(
        code,
        "MENTIONS_FUNCTION",
        source=source,
        properties={"reason": "manually verified"},
    )
    upsert_edges(code, code.config, UpsertEdgesRequest(edges=[edge]))
    original = _rows(
        code, "MATCH ()-[r:MENTIONS_FUNCTION]->() RETURN r._source,r.reason"
    )
    for text in ["# Guide\n\nUse `alpha`.", "# Guide\n\nNo mention."]:
        result = _ingest(code, text)
        assert not result.warnings
        assert (
            _rows(code, "MATCH ()-[r:MENTIONS_FUNCTION]->() RETURN r._source,r.reason")
            == original
        )
        assert _rows(
            code, f"MATCH ()-[r:MENTIONS_FUNCTION]->() RETURN r.{DOCUMENT_OWNER_PROP}"
        ) == [[""]]


def test_unknown_legacy_edges_are_preserved_without_claiming_them(code):
    _legacy_link(code, "docs/guide.md", "MENTIONS_FUNCTION")
    for text in ["# Guide\n\nUse `alpha`.", "# Guide\n\nNo mention."]:
        response = _ingest(code, text)
        assert any("unknown ownership" in warning for warning in response.warnings)
        assert _rows(
            code, f"MATCH ()-[r:MENTIONS_FUNCTION]->() RETURN r.{DOCUMENT_OWNER_PROP}"
        ) == [[None]]


def test_removed_section_with_authored_implementation_survives(code):
    _ingest(code, "# Guide\n\n## Removed\n\nOld body.")
    sid = _rows(code, "MATCH (s:Section {title:'Removed'}) RETURN s.id")[0][0]
    fid = _rows(code, "MATCH (f:Function) RETURN f.id")[0][0]
    upsert_edges(
        code,
        code.config,
        UpsertEdgesRequest(
            edges=[
                UpsertEdge(
                    type="IMPLEMENTS",
                    from_label="Function",
                    from_key=fid,
                    to_label="Section",
                    to_key=sid,
                    source="agent",
                )
            ]
        ),
    )
    result = _ingest(code, "# Guide\n\nNew body.")
    assert any("obsolete Section" in w for w in result.warnings)
    assert _rows(code, "MATCH (f)-[r:IMPLEMENTS]->(s) RETURN f.id,s.id,r._source") == [
        [fid, sid, "agent"]
    ]
    assert _rows(code, "MATCH ()-[:HAS_SECTION]->(s) RETURN s.title") == [["Guide"]]
    assert _rows(code, "MATCH (s:Section) RETURN count(s)") == [[2]]


@pytest.mark.parametrize("sections", [False, True])
def test_obsolete_chunks_with_authored_relationships_survive(engine, sections):
    _ingest(engine, "# Guide\n\n" + "a long sentence. " * 20, sections=sections)
    cid = _rows(engine, "MATCH (c:Chunk) RETURN c.id ORDER BY c.id DESC LIMIT 1")[0][0]
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(
            node_tables=[
                NodeTableSpec(name="Memory", properties=[PropertySpec(name="text")])
            ],
            rel_tables=[
                RelTableSpec(name="EVIDENCE", from_label="Memory", to_label="Chunk")
            ],
        ),
    )
    upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(nodes=[UpsertNode(label="Memory", key="m", source="agent")]),
    )
    upsert_edges(
        engine,
        engine.config,
        UpsertEdgesRequest(
            edges=[
                UpsertEdge(
                    type="EVIDENCE",
                    from_label="Memory",
                    from_key="m",
                    to_label="Chunk",
                    to_key=cid,
                    source="agent",
                )
            ]
        ),
    )
    response = _ingest(engine, "# Guide\n\nShort.", sections=sections)
    assert any("obsolete Chunk" in w for w in response.warnings)
    assert _rows(engine, "MATCH ()-[:EVIDENCE]->(c) RETURN c.id") == [[cid]]
    assert _rows(engine, "MATCH (c:Chunk) RETURN count(c)") == [[2]]


def test_flat_reingest_removes_only_owned_chunk_relationships(engine):
    _ingest(engine, "# Guide\n\nBody.")
    result = _ingest(engine, "Replacement flat text.", sections=False)
    assert result.nodes_pruned == 1 and not result.warnings
    assert _rows(engine, "MATCH ()-[r:IN_SECTION]->() RETURN count(r)") == [[0]]
    assert _rows(engine, "MATCH (c:Chunk) RETURN c.text") == [
        ["Replacement flat text."]
    ]


def test_sync_is_scoped_to_named_source_and_canonical_path(code, tmp_path):
    first, second = (
        str(tmp_path / "one" / "guide.md"),
        str(tmp_path / "two" / "guide.md"),
    )
    for source in [first, second]:
        _ingest(code, "# Guide\n\nUse `alpha`.", source)
    _ingest(
        code,
        "# Guide\n\nNo reference.",
        str(Path(first).parent / ".." / "one" / "guide.md"),
    )
    assert _rows(
        code, "MATCH (s)-[r:MENTIONS_FUNCTION]->() RETURN s._source,r._source"
    ) == [[second, second]]


def _snapshot(engine):
    return {
        "sections": _rows(
            engine, "MATCH (n:Section) RETURN n.id,n.title,n.preview ORDER BY n.id"
        ),
        "chunks": _rows(engine, "MATCH (n:Chunk) RETURN n.id,n.text ORDER BY n.id"),
        "links": _rows(
            engine,
            "MATCH (a)-[r:HAS_SECTION|SUBSECTION_OF|NEXT_SECTION|IN_SECTION|MENTIONS_FUNCTION]->(b) RETURN a.id,label(r),b.id,r._source ORDER BY a.id,label(r),b.id",
        ),
    }


@pytest.mark.parametrize("phase", ["nodes", "delete_edges", "insert_edges", "prune"])
def test_failed_reconciliation_rolls_back_every_document_change(
    code, monkeypatch, phase
):
    _ingest(code, "# Guide\n\n## Old\n\nUse `alpha`.")
    before = _snapshot(code)
    real = code.execute_write
    failed = False

    def execute(query, *args, **kwargs):
        nonlocal failed
        matches = {
            "nodes": query.startswith("MERGE (n:Section"),
            "delete_edges": "DELETE r" in query,
            "insert_edges": "MERGE (a)-[r:" in query,
            "prune": "DELETE n RETURN" in query,
        }
        result = real(query, *args, **kwargs)
        if not failed and matches[phase]:
            failed = True
            raise GragError("injected publication failure")
        return result

    monkeypatch.setattr(code, "execute_write", execute)
    with pytest.raises(GragError, match="injected"):
        _ingest(code, "# Guide\n\n## New\n\nNo mention.")
    assert failed and _snapshot(code) == before
    monkeypatch.setattr(code, "execute_write", real)
    _ingest(code, "# Guide\n\n## New\n\nNo mention.")
    assert _rows(code, "MATCH ()-[r:MENTIONS_FUNCTION]->() RETURN count(r)") == [[0]]


def test_public_upsert_waits_for_reconciliation_and_remains_authored(code, monkeypatch):
    _ingest(code, "# Guide\n\nUse `alpha`.")
    before = _snapshot(code)
    edge = _edge(code, "MENTIONS_FUNCTION")
    entered, release = threading.Event(), threading.Event()
    real = code.execute_write

    def execute(query, *args, **kwargs):
        result = real(query, *args, **kwargs)
        if "DELETE r" in query and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(code, "execute_write", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sync = pool.submit(_ingest, code, "# Guide\n\nNo reference.")
        try:
            assert entered.wait(2)
            assert _snapshot(code) == before  # readers see the previous committed graph
            authored = pool.submit(
                upsert_edges, code, code.config, UpsertEdgesRequest(edges=[edge])
            )
            assert not authored.done()
        finally:
            release.set()
        sync.result(timeout=3)
        authored.result(timeout=3)
    _ingest(code, "# Guide\n\nStill no reference.")
    assert _rows(code, "MATCH ()-[r:MENTIONS_FUNCTION]->() RETURN r._source") == [
        ["agent"]
    ]


def test_flat_replacement_failure_restores_section_chunk_links(code, monkeypatch):
    _ingest(code, "# Guide\n\nUse `alpha`.")
    before = _snapshot(code)
    real = code.execute_write

    def execute(query, *args, **kwargs):
        result = real(query, *args, **kwargs)
        if "DELETE n RETURN" in query:
            raise GragError("flat prune failed")
        return result

    monkeypatch.setattr(code, "execute_write", execute)
    with pytest.raises(GragError, match="flat prune"):
        _ingest(code, "Flat replacement.", sections=False)
    assert _snapshot(code) == before


def test_code_catalog_error_does_not_silently_remove_mentions(code, monkeypatch):
    _ingest(code, "# Guide\n\nUse `alpha`.")
    before = _snapshot(code)
    real = code.execute

    def execute(query, *args, **kwargs):
        if "SHOW_TABLES" in query:
            raise GragError("catalog read failed")
        return real(query, *args, **kwargs)

    monkeypatch.setattr(code, "execute", execute)
    with pytest.raises(GragError, match="catalog read"):
        _ingest(code, "# Guide\n\nNo mention.")
    assert _snapshot(code) == before


def test_failed_first_ingest_leaves_no_partial_document(engine, monkeypatch):
    real = engine.execute_write

    def execute(query, *args, **kwargs):
        result = real(query, *args, **kwargs)
        if "MERGE (a)-[r:" in query:
            raise GragError("first edge failed")
        return result

    monkeypatch.setattr(engine, "execute_write", execute)
    with pytest.raises(GragError, match="first edge"):
        _ingest(engine, "# Guide\n\nBody.")
    for label in ("Document", "Section", "Chunk"):
        assert _rows(engine, f"MATCH (n:{label}) RETURN count(n)") == [[0]]
    monkeypatch.setattr(engine, "execute_write", real)
    assert _ingest(engine, "# Guide\n\nBody.").nodes_created == 1


@pytest.mark.parametrize("sections", [False, True])
def test_embedding_runs_after_publication_lock_is_released(
    engine, monkeypatch, sections
):
    from grag.ingest import loaders, markdown

    calls = []

    def embed(eng, config, label):
        def acquire():
            with eng.serialized_writes():
                return True

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(acquire).result(timeout=2)
        calls.append(label)

    monkeypatch.setattr(markdown if sections else loaders, "_embed_pending", embed)
    _ingest(engine, "# Guide\n\nBody.", sections=sections)
    assert calls == (["Chunk", "Section", "Document"] if sections else ["Chunk"])


def test_competing_document_ingests_publish_complete_versions(code, monkeypatch):
    _ingest(code, "# Guide\n\nUse `alpha`.")
    entered, release = threading.Event(), threading.Event()
    real = code.execute_write

    def execute(query, *args, **kwargs):
        result = real(query, *args, **kwargs)
        if "DELETE r" in query and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(code, "execute_write", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_ingest, code, "# Guide\n\n## Earlier\n\nUse `alpha`.")
        try:
            assert entered.wait(2)
            second = pool.submit(_ingest, code, "# Guide\n\n## Latest\n\nNo mention.")
            assert not second.done()
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
    assert _rows(code, "MATCH (s:Section) RETURN s.title ORDER BY s.title") == [
        ["Guide"],
        ["Latest"],
    ]
    assert _rows(code, "MATCH ()-[r:MENTIONS_FUNCTION]->() RETURN count(r)") == [[0]]


def test_owned_edges_survive_restart_and_remain_removable(tmp_path):
    config = GragConfig(db_path=tmp_path / "restart.lbdb", embedder=None)
    first = GragService(config)
    try:
        _ingest(first.engine, "# Guide\n\n## One\n\n## Two")
    finally:
        first.close()
    second = GragService(config)
    try:
        _ingest(second.engine, "# Guide\n\n## Two\n\n## One")
        assert _rows(
            second.engine,
            "MATCH (a)-[:NEXT_SECTION]->(b) RETURN a.title,b.title ORDER BY a.title",
        ) == [["Guide", "Two"], ["Two", "One"]]
    finally:
        second.close()


def test_ingest_warnings_reach_rest_mcp_and_cli(tmp_path):
    from grag.api.main import create_app
    from grag.ingest.loaders import ingest_paths
    from grag.mcp_server import server as mcp

    path = tmp_path / "guide.md"
    path.write_text("# Guide\n\n## One")
    config = GragConfig(db_path=tmp_path / "transports.lbdb", embedder=None)
    with TestClient(create_app(config)) as client:
        svc = client.app.state.service
        _legacy_link(svc.engine, str(path), "HAS_SECTION")
        response = client.post(
            "/api/ingest",
            json={
                "documents": [{"text": path.read_text(), "source": str(path)}],
                "sections": True,
            },
        )
        assert response.status_code == 200
        assert any("unknown ownership" in w for w in response.json()["warnings"])
        output = json.loads(
            mcp.ingest_docs(
                svc, [str(path), str(tmp_path / "missing.md")], sections=True
            )
        )
        assert any("unknown ownership" in w for w in output["warnings"])
        assert any("file not found" in w for w in output["warnings"])
        queued = json.loads(mcp.ingest_docs(svc, [str(path)], background=True))
        svc.jobs._pool.submit(lambda: None).result(timeout=3)
        finished = json.loads(mcp.job_status(svc, queued["id"]))
        assert finished["status"] == "done"
        assert any("unknown ownership" in w for w in finished["result"]["warnings"])
    assert "unknown ownership" in ingest_paths(config, [path], sections=True)
