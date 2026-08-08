"""Tests for grag.core.serialize: token-budgeted subgraph packing."""

from __future__ import annotations

import datetime

import pytest

from grag.core.engine import Engine, extract_subgraph
from grag.core.serialize import estimate_tokens, pack_context
from grag.core.types import EdgeRecord, NodeRecord, Subgraph


def _node(nid: str, label: str = "Doc", **props) -> NodeRecord:
    return NodeRecord(id=nid, label=label, properties=props)


@pytest.fixture()
def graph(engine: Engine) -> Engine:
    engine.execute_write(
        "CREATE NODE TABLE Doc(id STRING PRIMARY KEY, title STRING, text STRING)"
    )
    engine.execute_write("CREATE REL TABLE RELATED(FROM Doc TO Doc, since INT64)")
    engine.execute_write(
        "CREATE (d:Doc {id: 'doc-0', title: 'graph databases', text: 'about graphs'})"
    )
    engine.execute_write(
        "CREATE (d:Doc {id: 'doc-1', title: 'vector search', text: 'about vectors'})"
    )
    return engine


@pytest.fixture()
def docs_graph(graph: Engine) -> Subgraph:
    graph.execute_write(
        "MATCH (a:Doc {id: 'doc-0'}), (b:Doc {id: 'doc-1'}) "
        "CREATE (a)-[:RELATED {since: 2024}]->(b)"
    )
    res = graph.execute("MATCH (a:Doc)-[r:RELATED]->(b:Doc) RETURN a, r, b")
    return extract_subgraph(res, pk_by_label={"Doc": "id"})


# -- estimate_tokens --------------------------------------------------------------


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2


# -- line formats ------------------------------------------------------------------


def test_node_line_format():
    sub = Subgraph(
        nodes=[
            _node(
                "Doc:doc-0",
                title="graph databases",
                text="graph databases store relationships",
                _source="file.md",
            )
        ]
    )
    packed = pack_context(sub, token_budget=10_000)
    assert packed.text == (
        'Doc:doc-0 {title: "graph databases", '
        'text: "graph databases store relationships"} [source: file.md]'
    )
    assert packed.included_node_ids == ["Doc:doc-0"]
    assert packed.truncated is False
    assert packed.token_estimate == len(packed.text) // 4


def test_source_suffix_omitted_when_absent():
    sub = Subgraph(nodes=[_node("Doc:a", title="t")])
    packed = pack_context(sub, token_budget=10_000)
    assert packed.text == 'Doc:a {title: "t"}'
    assert "[source:" not in packed.text


def test_node_without_props():
    sub = Subgraph(nodes=[_node("Doc:a")])
    packed = pack_context(sub, token_budget=10_000)
    assert packed.text == "Doc:a"


def test_edge_line_format(docs_graph: Subgraph):
    packed = pack_context(docs_graph, token_budget=10_000)
    edge_line = [l for l in packed.text.splitlines() if "-[" in l][0]
    assert edge_line == "Doc:doc-0 -[RELATED {since: 2024}]-> Doc:doc-1"


def test_edge_props_omitted_when_empty():
    sub = Subgraph(
        nodes=[_node("Doc:a"), _node("Doc:b")],
        edges=[EdgeRecord(id="R:Doc:a->Doc:b", type="R", source="Doc:a", target="Doc:b")],
    )
    packed = pack_context(sub, token_budget=10_000)
    assert "Doc:a -[R]-> Doc:b" in packed.text


def test_vector_props_skipped():
    sub = Subgraph(
        nodes=[
            _node(
                "Doc:a",
                title="t",
                embedding=[0.1] * 384,
                _emb_r=1.0,
                _emb_code=[1, 2, 3],
                _emb_model="bge-small",
            )
        ]
    )
    packed = pack_context(sub, token_budget=10_000)
    assert packed.text == 'Doc:a {title: "t"}'
    assert "emb" not in packed.text


def test_long_strings_truncated():
    sub = Subgraph(nodes=[_node("Doc:a", text="x" * 500)])
    packed = pack_context(sub, token_budget=100_000)
    line = packed.text
    assert "x" * 197 + "..." in line
    assert "x" * 200 not in line


def test_datetime_rendered_isoformat():
    sub = Subgraph(
        nodes=[_node("Doc:a", _created_at=datetime.datetime(2026, 8, 7, 12, 30, 5))]
    )
    packed = pack_context(sub, token_budget=10_000)
    assert '"2026-08-07T12:30:05"' in packed.text


# -- ordering ----------------------------------------------------------------------


def test_seed_ids_come_first_in_given_order():
    sub = Subgraph(
        nodes=[_node("Doc:a", title="a"), _node("Doc:b", title="b"), _node("Doc:c", title="c")]
    )
    packed = pack_context(sub, token_budget=10_000, seed_ids=["Doc:c", "Doc:a"])
    lines = packed.text.splitlines()
    assert lines[0].startswith("Doc:c")
    assert lines[1].startswith("Doc:a")
    assert lines[2].startswith("Doc:b")
    assert packed.included_node_ids == ["Doc:c", "Doc:a", "Doc:b"]


def test_unknown_seed_ids_skipped():
    sub = Subgraph(nodes=[_node("Doc:a", title="a")])
    packed = pack_context(sub, token_budget=10_000, seed_ids=["Doc:missing", "Doc:a"])
    assert packed.included_node_ids == ["Doc:a"]


def test_edges_after_nodes(docs_graph: Subgraph):
    packed = pack_context(docs_graph, token_budget=10_000)
    lines = packed.text.splitlines()
    assert lines[0].startswith("Doc:doc-0 {")
    assert lines[1].startswith("Doc:doc-1 {")
    assert "-[RELATED" in lines[2]


# -- budget / truncation -------------------------------------------------------------


def test_budget_stops_packing():
    nodes = [_node(f"Doc:n{i}", text="y" * 100) for i in range(20)]
    packed = pack_context(Subgraph(nodes=nodes), token_budget=200)
    assert packed.truncated is True
    assert packed.token_estimate <= 200
    assert len(packed.included_node_ids) < 20
    assert packed.included_node_ids  # something still fits


def test_everything_fits_not_truncated(docs_graph: Subgraph):
    packed = pack_context(docs_graph, token_budget=10_000)
    assert packed.truncated is False
    assert len(packed.included_node_ids) == 2


def test_zero_budget():
    sub = Subgraph(nodes=[_node("Doc:a", title="a")])
    packed = pack_context(sub, token_budget=0)
    assert packed.text == ""
    assert packed.token_estimate == 0
    assert packed.included_node_ids == []
    assert packed.truncated is True


def test_negative_budget():
    sub = Subgraph(nodes=[_node("Doc:a", title="a")])
    packed = pack_context(sub, token_budget=-5)
    assert packed.text == ""
    assert packed.truncated is True


def test_zero_budget_empty_subgraph_not_truncated():
    packed = pack_context(Subgraph(), token_budget=0)
    assert packed.text == ""
    assert packed.truncated is False


def test_edge_truncation_after_all_nodes_fit():
    line_node = 'Doc:a {title: "a"}'
    line_edge = "Doc:a -[R]-> Doc:b"
    # budget fits both node lines but not the edge line
    two_nodes = f"{line_node}\n{line_node.replace('a', 'b')}"
    budget = estimate_tokens(two_nodes) + estimate_tokens(line_edge) - 1
    sub = Subgraph(
        nodes=[_node("Doc:a", title="a"), _node("Doc:b", title="b")],
        edges=[EdgeRecord(id="R:Doc:a->Doc:b", type="R", source="Doc:a", target="Doc:b")],
    )
    packed = pack_context(sub, token_budget=budget)
    assert packed.included_node_ids == ["Doc:a", "Doc:b"]
    assert "-[R]" not in packed.text
    assert packed.truncated is True


# -- engine roundtrip -----------------------------------------------------------------


def test_engine_roundtrip_with_citation(graph: Engine):
    graph.execute_write("ALTER TABLE Doc ADD _source STRING")
    graph.execute_write("MATCH (d:Doc {id: 'doc-0'}) SET d._source = 'notes.md'")
    res = graph.execute("MATCH (d:Doc) RETURN d")
    sub = extract_subgraph(res, pk_by_label={"Doc": "id"})
    packed = pack_context(sub, token_budget=10_000, seed_ids=["Doc:doc-0"])
    lines = packed.text.splitlines()
    assert lines[0].startswith('Doc:doc-0 {id: "doc-0", title: "graph databases"')
    assert lines[0].endswith("[source: notes.md]")
    assert lines[1].startswith("Doc:doc-1")
    assert "[source:" not in lines[1]
