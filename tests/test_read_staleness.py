"""Read-side staleness guards for LadybugDB's implicit prepared-statement cache.

Upstream LadybugDB/ladybug#877: on 0.20.0/0.20.1, re-executing the same
parameterized query string on one connection can reuse stale first-execution
state instead of re-scanning with the new parameters. Affected shapes include
traversals/joins, ORDER BY, LIMIT/top-k (which covers QUERY_FTS_INDEX and
QUERY_VECTOR_INDEX), OPTIONAL MATCH and UNION. Single-table scans are clean.

grag's write path evicts the cached plan before every write (see
Engine.execute_write); reads run on pooled connections and keep their cache.
These tests replay grag's exact parameterized read shapes — FTS seed queries,
k-hop expansion, vector-index queries — three times with different parameters
over the pooled reader and assert each execution sees its own parameters.
Sequential execute() calls reuse the same pooled connection, which is the
upstream trigger condition.

If any of these fail, the read side needs the same eviction the write path
got (or a ladybug upgrade past the fix, upstream commit 96a4b080).
"""

from __future__ import annotations

import pytest

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.types import IngestDocument, IngestRequest, SearchRequest
from grag.service import GragService

# (id, title, text) — each doc owns one distinctive keyword.
_DOCS = [
    ("doc-0", "graph databases", "graph databases store relationships between entities"),
    ("doc-1", "vector search", "vector embeddings enable semantic retrieval"),
    ("doc-2", "context packing", "token budgets matter for llm prompts"),
]


@pytest.fixture()
def docs(engine: Engine) -> Engine:
    engine.execute_write(
        "CREATE NODE TABLE Doc(id STRING PRIMARY KEY, title STRING, text STRING)"
    )
    engine.execute_write("CREATE REL TABLE RELATED(FROM Doc TO Doc, since INT64)")
    for doc_id, title, text in _DOCS:
        engine.execute_write(
            "CREATE (d:Doc {id: $id, title: $t, text: $x})",
            {"id": doc_id, "t": title, "x": text},
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


def test_fts_topk_repeated_queries_not_stale(docs: Engine):
    """grag's seed shape: QUERY_FTS_INDEX with $q, re-executed per search."""
    docs.load_extension("FTS")
    docs.execute_write("CALL CREATE_FTS_INDEX('Doc', 'doc_fts', ['title', 'text'])")
    cypher = (
        "CALL QUERY_FTS_INDEX('Doc', 'doc_fts', $q, TOP := 1) RETURN node.id, score"
    )
    for query, expected in [("graph", "doc-0"), ("vector", "doc-1"), ("token", "doc-2")]:
        res = docs.execute(cypher, {"q": query})
        assert res.rows, f"FTS query {query!r} returned no rows"
        assert res.rows[0][0] == expected, (
            f"FTS query {query!r} returned {res.rows[0][0]} — stale cached plan?"
        )


def test_traversal_order_by_not_stale(docs: Engine):
    """#877's canonical shape: parameterized join + ORDER BY."""
    cypher = "MATCH (a:Doc {id: $v})-[:RELATED]->(b:Doc) RETURN b.id ORDER BY b.id"
    for start, expected in [("doc-0", ["doc-1"]), ("doc-1", ["doc-2"]), ("doc-2", [])]:
        rows = docs.execute(cypher, {"v": start}).rows
        assert [r[0] for r in rows] == expected, (
            f"traversal from {start} returned {rows} — stale cached plan?"
        )


def test_khop_expansion_not_stale(docs: Engine):
    """grag's hops shape: undirected var-length traversal keyed by $key."""
    cypher = (
        "MATCH p = (a:Doc {id: $key})-[:RELATED*1..2]-(b:Doc) "
        "RETURN DISTINCT b.id ORDER BY b.id"
    )
    cases = [
        ("doc-0", ["doc-0", "doc-1", "doc-2"]),  # reaches all via doc-1
        ("doc-2", ["doc-0", "doc-1", "doc-2"]),
        ("doc-1", ["doc-0", "doc-1", "doc-2"]),
    ]
    for start, expected in cases:
        rows = docs.execute(cypher, {"key": start}).rows
        assert [r[0] for r in rows] == expected, (
            f"k-hop from {start} returned {rows} — stale cached plan?"
        )


def test_limit_repeated_not_stale(docs: Engine):
    """#877: with LIMIT the first call is right and later calls return nothing."""
    cypher = "MATCH (a:Doc {id: $v})-[:RELATED]->(b:Doc) RETURN b.id LIMIT 1"
    for start, expected in [("doc-0", "doc-1"), ("doc-1", "doc-2")]:
        rows = docs.execute(cypher, {"v": start}).rows
        assert rows and rows[0][0] == expected, (
            f"LIMIT query from {start} returned {rows} — stale cached plan?"
        )
    # doc-2 has no outgoing edges: must be empty, not a replay of doc-1's row
    assert docs.execute(cypher, {"v": "doc-2"}).rows == []


def test_vector_index_repeated_queries_not_stale(engine: Engine):
    """grag's semantic shape: QUERY_VECTOR_INDEX with $q/$k per search."""
    engine.load_extension("VECTOR")
    engine.execute_write(
        "CREATE NODE TABLE Emb(id STRING PRIMARY KEY, embedding FLOAT[3])"
    )
    for key, vec in [("e0", [1.0, 0.0, 0.0]), ("e1", [0.0, 1.0, 0.0]), ("e2", [0.0, 0.0, 1.0])]:
        engine.execute_write(
            "CREATE (n:Emb {id: $id, embedding: $v})", {"id": key, "v": vec}
        )
    engine.execute_write(
        "CALL CREATE_VECTOR_INDEX('Emb', 'emb_vec', 'embedding', metric := 'cosine')"
    )
    cypher = "CALL QUERY_VECTOR_INDEX('Emb', 'emb_vec', $q, $k) RETURN node.id, distance"
    for query, expected in [([1.0, 0.0, 0.0], "e0"), ([0.0, 1.0, 0.0], "e1"), ([0.0, 0.0, 1.0], "e2")]:
        res = engine.execute(cypher, {"q": query, "k": 1})
        assert res.rows, f"vector query {query} returned no rows"
        assert res.rows[0][0] == expected, (
            f"vector query {query} returned {res.rows[0][0]} — stale cached plan?"
        )


def test_search_knowledge_repeated_queries_not_stale(tmp_path):
    """End-to-end: consecutive search_knowledge calls must not replay seeds."""
    svc = GragService(
        GragConfig(db_path=tmp_path / "svc.lbdb", buffer_pool_size=128 * 1024 * 1024)
    )
    try:
        svc.ingest(
            IngestRequest(
                documents=[IngestDocument(text=f"{t}. {x}") for _, t, x in _DOCS],
                chunk=True,
            )
        )
        seen: dict[str, str] = {}
        for query, keyword in [("graph", "graph"), ("vector", "vector"), ("token", "token")]:
            res = svc.search_knowledge(SearchRequest(query=query, top_k=3, hops=1))
            assert res.seeds, f"search {query!r} returned no seeds"
            top_text = " ".join(
                str(v) for v in res.seeds[0].node.properties.values()
            )
            seen[query] = top_text
            assert keyword in top_text, (
                f"top seed for {query!r} is {top_text!r} — stale cached plan?"
            )
        # paranoia: the three top seeds must not all be the same node
        assert len(set(seen.values())) > 1
    finally:
        svc.close()
