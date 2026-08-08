"""Engine smoke tests — also the verified-behavior record for LadybugDB's
python value formats that grag.core.serialize and friends rely on."""

from __future__ import annotations

import pytest

from grag.core.engine import (
    Engine,
    drop_internal_rows,
    extract_subgraph,
    is_node_value,
    is_rel_value,
)
from grag.core.types import make_node_id


@pytest.fixture()
def docs(engine: Engine) -> Engine:
    engine.execute_write(
        "CREATE NODE TABLE Doc(id STRING PRIMARY KEY, title STRING, text STRING)"
    )
    engine.execute_write("CREATE REL TABLE RELATED(FROM Doc TO Doc, since INT64)")
    for i, (title, text) in enumerate(
        [
            ("graph databases", "graph databases store relationships between entities"),
            ("vector search", "vector embeddings enable semantic retrieval"),
            ("context packing", "token budgets matter for llm prompts"),
        ]
    ):
        engine.execute_write(
            "CREATE (d:Doc {id: $id, title: $t, text: $x})",
            {"id": f"doc-{i}", "t": title, "x": text},
        )
    engine.execute_write(
        "MATCH (a:Doc {id: 'doc-0'}), (b:Doc {id: 'doc-1'}) "
        "CREATE (a)-[:RELATED {since: 2024}]->(b)"
    )
    engine.execute_write(
        "MATCH (a:Doc {id: 'doc-1'}), (b:Doc {id: 'doc-2'}) "
        "CREATE (a)-[:RELATED {since: 2025}]->(b)"
    )
    return engine


def test_ddl_and_query(docs: Engine):
    res = docs.execute("MATCH (d:Doc) RETURN d.id ORDER BY d.id")
    assert res.columns == ["d.id"]
    assert [r[0] for r in res.rows] == ["doc-0", "doc-1", "doc-2"]


def test_parameterized_write_and_read(docs: Engine):
    res = docs.execute("MATCH (d:Doc {id: $id}) RETURN d.title", {"id": "doc-1"})
    assert res.rows == [["vector search"]]


def test_node_and_rel_value_formats(docs: Engine):
    res = docs.execute("MATCH (a:Doc)-[r:RELATED]->(b:Doc) RETURN a, r, b LIMIT 1")
    a, r, _b = res.rows[0]
    assert is_node_value(a) and a["_LABEL"] == "Doc"
    assert "_ID" in a and "title" in a
    assert is_rel_value(r)
    assert r.get("since") == 2024


def test_extract_subgraph_canonical_ids(docs: Engine):
    res = docs.execute("MATCH (a:Doc)-[r:RELATED]->(b:Doc) RETURN a, r, b")
    sub = extract_subgraph(res, pk_by_label={"Doc": "id"})
    ids = {n.id for n in sub.nodes}
    assert make_node_id("Doc", "doc-0") in ids
    assert len(sub.edges) == 2
    edge = sub.edges[0]
    assert edge.source.startswith("Doc:") and edge.target.startswith("Doc:")
    assert edge.type


def test_path_value_extraction(docs: Engine):
    res = docs.execute(
        "MATCH p = (a:Doc {id: 'doc-0'})-[:RELATED*1..2]->(b:Doc) RETURN p"
    )
    sub = extract_subgraph(res, pk_by_label={"Doc": "id"})
    assert len(sub.nodes) == 3
    assert len(sub.edges) == 2


def test_fts_extension(docs: Engine):
    docs.load_extension("FTS")
    docs.execute_write("CALL CREATE_FTS_INDEX('Doc', 'doc_fts', ['title', 'text'])")
    res = docs.execute(
        "CALL QUERY_FTS_INDEX('Doc', 'doc_fts', $q, TOP := 2) RETURN node.id, score",
        {"q": "graph relationships"},
    )
    assert res.rows
    assert res.rows[0][0] == "doc-0"


def test_drop_internal_rows(docs: Engine):
    docs.execute_write("CREATE NODE TABLE _grag_meta(name STRING PRIMARY KEY)")
    docs.execute_write("CREATE (m:_grag_meta {name: 'Doc'})")
    docs.execute_write("CREATE REL TABLE _grag_link(FROM _grag_meta TO Doc)")
    docs.execute_write(
        "MATCH (m:_grag_meta {name: 'Doc'}), (d:Doc {id: 'doc-0'}) "
        "CREATE (m)-[:_grag_link]->(d)"
    )

    res = docs.execute("MATCH (n) RETURN n")
    assert {r[0]["_LABEL"] for r in res.rows} == {"Doc", "_grag_meta"}
    filtered = drop_internal_rows(res)
    assert {r[0]["_LABEL"] for r in filtered.rows} == {"Doc"}
    assert filtered.columns == res.columns

    # rows touching internal values through rels/paths are dropped too
    res = docs.execute("MATCH (m)-[r:_grag_link]->(d) RETURN m, r, d")
    assert res.rows and drop_internal_rows(res).rows == []
    res = docs.execute("MATCH p = (m:_grag_meta)-[:_grag_link]->(d:Doc) RETURN p")
    assert res.rows and drop_internal_rows(res).rows == []

    # ...while a row whose only graph value is a user node stays
    res = docs.execute("MATCH (m:_grag_meta)-[:_grag_link]->(d:Doc) RETURN d")
    assert len(drop_internal_rows(res).rows) == 1

    # scalar projections over an internal table are explicit introspection —
    # no graph values in the row, so the row stays
    res = docs.execute("MATCH (m:_grag_meta) RETURN m.name")
    assert drop_internal_rows(res).rows == [["Doc"]]
