"""Tests for grag.core.mutate — schema definition + idempotent upserts."""

from __future__ import annotations

import pytest

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import NotFoundError, SchemaError
from grag.core.mutate import define_schema, upsert_edges, upsert_nodes
from grag.core.types import (
    META_TABLE,
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    SchemaDocument,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)


def _doc_spec() -> NodeTableSpec:
    return NodeTableSpec(
        name="Doc",
        primary_key="id",
        properties=[
            PropertySpec(name="title"),
            PropertySpec(name="year", type="INT64"),
            PropertySpec(name="score", type="DOUBLE"),
            PropertySpec(name="published", type="BOOL"),
        ],
    )


def _person_spec() -> NodeTableSpec:
    return NodeTableSpec(name="Person", primary_key="name")


def _knows_spec() -> RelTableSpec:
    return RelTableSpec(
        name="KNOWS",
        from_label="Person",
        to_label="Person",
        properties=[PropertySpec(name="since", type="INT64")],
    )


def _cols(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    return {
        r[1]: (r[2], r[4])
        for r in engine.execute(f"CALL TABLE_INFO('{table}') RETURN *").rows
    }


def _meta(engine: Engine) -> dict[str, list]:
    rows = engine.execute(
        f"MATCH (m:{META_TABLE}) "
        "RETURN m.name, m.kind, m.pk, m.searchable, m.from_label, m.to_label"
    ).rows
    return {r[0]: r[1:] for r in rows}


# --- define_schema -----------------------------------------------------------------


def test_define_schema_creates_tables_with_provenance_and_meta(engine: Engine):
    doc = define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(
            node_tables=[_doc_spec(), _person_spec()],
            rel_tables=[_knows_spec()],
        ),
    )
    assert isinstance(doc, SchemaDocument)
    assert {"Doc", "Person"} <= {t.name for t in doc.node_tables}
    assert "KNOWS" in {t.name for t in doc.rel_tables}

    cols = _cols(engine, "Doc")
    assert cols["id"] == ("STRING", True)  # pk auto-added, flagged primary key
    assert cols["year"] == ("INT64", False)
    assert cols["_source"] == ("STRING", False)
    assert cols["_created_at"] == ("TIMESTAMP", False)

    rel_cols = _cols(engine, "KNOWS")
    assert "since" in rel_cols
    assert "_source" in rel_cols

    meta = _meta(engine)
    assert meta["Doc"] == ["node", "id", True, "", ""]
    assert meta["Person"] == ["node", "name", True, "", ""]
    assert meta["KNOWS"] == ["rel", "", False, "Person", "Person"]


def test_define_schema_is_idempotent_by_default(engine: Engine):
    req = DefineSchemaRequest(node_tables=[_doc_spec()])
    define_schema(engine, engine.config, req)
    define_schema(engine, engine.config, req)  # second call must not raise
    assert len(_meta(engine)) == 1


def test_define_schema_if_not_exists_false_raises(engine: Engine):
    define_schema(engine, engine.config, DefineSchemaRequest(node_tables=[_doc_spec()]))
    with pytest.raises(SchemaError, match="already exists"):
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(node_tables=[_doc_spec()], if_not_exists=False),
        )


@pytest.mark.parametrize("name", ["2Doc", "has space", "semi;colon", ""])
def test_define_schema_invalid_table_names(engine: Engine, name: str):
    with pytest.raises(SchemaError) as exc:
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(node_tables=[NodeTableSpec(name=name)]),
        )
    assert exc.value.hint


def test_define_schema_invalid_property_and_pk(engine: Engine):
    with pytest.raises(SchemaError):
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(
                node_tables=[
                    NodeTableSpec(name="Doc", properties=[PropertySpec(name="bad-prop")])
                ]
            ),
        )
    with pytest.raises(SchemaError):
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(node_tables=[NodeTableSpec(name="Doc", primary_key="1id")]),
        )


@pytest.mark.parametrize("prop", ["_secret", "_source", "embedding", "_emb_code"])
def test_define_schema_reserved_node_props_rejected(engine: Engine, prop: str):
    with pytest.raises(SchemaError) as exc:
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(
                node_tables=[NodeTableSpec(name="Doc", properties=[PropertySpec(name=prop)])]
            ),
        )
    assert prop in str(exc.value)


def test_define_schema_reserved_rel_prop_rejected(engine: Engine):
    with pytest.raises(SchemaError):
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(
                node_tables=[_person_spec()],
                rel_tables=[
                    RelTableSpec(
                        name="KNOWS",
                        from_label="Person",
                        to_label="Person",
                        properties=[PropertySpec(name="_x")],
                    )
                ],
            ),
        )


def test_define_schema_rel_endpoint_must_exist(engine: Engine):
    with pytest.raises(SchemaError) as exc:
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(
                node_tables=[_person_spec()],
                rel_tables=[RelTableSpec(name="LIKES", from_label="Person", to_label="Ghost")],
            ),
        )
    assert "Ghost" in str(exc.value)
    assert "Person" in (exc.value.hint or "")  # hint lists existing node tables


def test_define_schema_rel_can_reference_tables_from_same_request(engine: Engine):
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(node_tables=[_person_spec()], rel_tables=[_knows_spec()]),
    )
    assert _meta(engine)["KNOWS"][0] == "rel"


# --- upsert_nodes -------------------------------------------------------------------


def test_upsert_nodes_create_then_update_is_idempotent(engine: Engine):
    define_schema(engine, engine.config, DefineSchemaRequest(node_tables=[_doc_spec()]))

    s1 = upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(
            nodes=[
                UpsertNode(
                    label="Doc",
                    key="d1",
                    properties={"title": "a", "year": 2020},
                    source="src1",
                )
            ]
        ),
    )
    assert s1.nodes == 1 and s1.warnings == []
    ts1 = engine.execute("MATCH (d:Doc {id: 'd1'}) RETURN d._created_at").rows[0][0]
    assert ts1 is not None

    s2 = upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(
            nodes=[UpsertNode(label="Doc", key="d1", properties={"title": "b"}, source="src2")]
        ),
    )
    assert s2.nodes == 1

    count = engine.execute("MATCH (d:Doc) RETURN count(*)").rows[0][0]
    assert count == 1  # MERGE matched, no duplicate node

    row = engine.execute(
        "MATCH (d:Doc {id: 'd1'}) RETURN d.title, d.year, d._source, d._created_at"
    ).rows[0]
    assert row[0] == "b"  # updated
    assert row[1] == 2020  # untouched props preserved
    assert row[2] == "src2"  # _source follows latest write
    assert row[3] == ts1  # _created_at stable across updates


def test_upsert_nodes_unknown_label(engine: Engine):
    with pytest.raises(SchemaError) as exc:
        upsert_nodes(
            engine,
            engine.config,
            UpsertNodesRequest(nodes=[UpsertNode(label="Ghost", key="x")]),
        )
    assert "define_schema" in (exc.value.hint or "")


def test_upsert_nodes_type_coercion_and_warnings(engine: Engine):
    define_schema(engine, engine.config, DefineSchemaRequest(node_tables=[_doc_spec()]))
    summary = upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(
            nodes=[
                UpsertNode(
                    label="Doc",
                    key="d1",
                    properties={
                        "title": "t",
                        "year": "2024",  # str -> INT64 coerced
                        "score": "1.5",  # str -> DOUBLE coerced
                        "published": 1,  # int into BOOL: rejected
                        "_evil": "x",  # reserved: rejected
                        "nope": 5,  # undeclared: rejected
                    },
                )
            ]
        ),
    )
    assert summary.nodes == 1
    assert len(summary.warnings) == 3
    text = "\n".join(summary.warnings)
    assert "published" in text and "_evil" in text and "nope" in text

    row = engine.execute(
        "MATCH (d:Doc {id: 'd1'}) RETURN d.year, d.score, d.published"
    ).rows[0]
    assert row[0] == 2024
    assert row[1] == 1.5
    assert row[2] is None  # rejected prop never written


def test_upsert_nodes_type_mismatch_warns_and_skips(engine: Engine):
    define_schema(engine, engine.config, DefineSchemaRequest(node_tables=[_doc_spec()]))
    summary = upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(
            nodes=[UpsertNode(label="Doc", key="d1", properties={"year": "abc"})]
        ),
    )
    assert summary.nodes == 1
    assert len(summary.warnings) == 1
    assert "year" in summary.warnings[0]
    row = engine.execute("MATCH (d:Doc {id: 'd1'}) RETURN d.year").rows[0]
    assert row[0] is None


def test_upsert_nodes_raw_table_without_meta(engine: Engine):
    """Tables created via raw cypher still upsert via the TABLE_INFO pk fallback;
    missing provenance columns are skipped silently."""
    engine.execute_write("CREATE NODE TABLE Raw(id STRING PRIMARY KEY, v STRING)")
    summary = upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(
            nodes=[UpsertNode(label="Raw", key="r1", properties={"v": "x"}, source="s")]
        ),
    )
    assert summary.nodes == 1 and summary.warnings == []
    assert engine.execute("MATCH (r:Raw {id: 'r1'}) RETURN r.v").rows == [["x"]]


# --- upsert_edges -------------------------------------------------------------------


def _people(engine: Engine) -> None:
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(node_tables=[_person_spec()], rel_tables=[_knows_spec()]),
    )
    upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(
            nodes=[UpsertNode(label="Person", key="a"), UpsertNode(label="Person", key="b")]
        ),
    )


def test_upsert_edges_happy_path_and_update(engine: Engine):
    _people(engine)
    s1 = upsert_edges(
        engine,
        engine.config,
        UpsertEdgesRequest(
            edges=[
                UpsertEdge(
                    type="KNOWS",
                    from_label="Person",
                    from_key="a",
                    to_label="Person",
                    to_key="b",
                    properties={"since": 2020},
                    source="test",
                )
            ]
        ),
    )
    assert s1.edges == 1 and s1.warnings == []
    rows = engine.execute(
        "MATCH (a:Person)-[r:KNOWS]->(b:Person) RETURN r.since, r._source"
    ).rows
    assert rows == [[2020, "test"]]

    s2 = upsert_edges(
        engine,
        engine.config,
        UpsertEdgesRequest(
            edges=[
                UpsertEdge(
                    type="KNOWS",
                    from_label="Person",
                    from_key="a",
                    to_label="Person",
                    to_key="b",
                    properties={"since": 2021},
                )
            ]
        ),
    )
    assert s2.edges == 1
    rows = engine.execute("MATCH ()-[r:KNOWS]->() RETURN r.since").rows
    assert rows == [[2021]]  # single rel, updated prop


def test_upsert_edges_missing_endpoint(engine: Engine):
    _people(engine)
    with pytest.raises(NotFoundError) as exc:
        upsert_edges(
            engine,
            engine.config,
            UpsertEdgesRequest(
                edges=[
                    UpsertEdge(
                        type="KNOWS",
                        from_label="Person",
                        from_key="a",
                        to_label="Person",
                        to_key="ghost",
                    )
                ]
            ),
        )
    assert "Person:ghost" in str(exc.value)
    assert "upsert_nodes" in (exc.value.hint or "")


def test_upsert_edges_wrong_label_pair(engine: Engine):
    _people(engine)
    define_schema(
        engine, engine.config, DefineSchemaRequest(node_tables=[_doc_spec()])
    )
    with pytest.raises(SchemaError) as exc:
        upsert_edges(
            engine,
            engine.config,
            UpsertEdgesRequest(
                edges=[
                    UpsertEdge(
                        type="KNOWS",
                        from_label="Doc",
                        from_key="d1",
                        to_label="Person",
                        to_key="a",
                    )
                ]
            ),
        )
    assert "Person" in str(exc.value)  # message names the defined endpoints


def test_upsert_edges_unknown_type(engine: Engine):
    _people(engine)
    with pytest.raises(SchemaError) as exc:
        upsert_edges(
            engine,
            engine.config,
            UpsertEdgesRequest(
                edges=[
                    UpsertEdge(
                        type="HATES",
                        from_label="Person",
                        from_key="a",
                        to_label="Person",
                        to_key="b",
                    )
                ]
            ),
        )
    assert "define_schema" in (exc.value.hint or "")


def test_upsert_edges_raw_rel_table_fallback(engine: Engine):
    """Rels created via raw cypher resolve endpoints via SHOW_CONNECTION."""
    engine.execute_write("CREATE NODE TABLE A(id STRING PRIMARY KEY)")
    engine.execute_write("CREATE NODE TABLE B(id STRING PRIMARY KEY)")
    engine.execute_write("CREATE REL TABLE LINKS(FROM A TO B, w DOUBLE)")
    upsert_nodes(
        engine,
        engine.config,
        UpsertNodesRequest(
            nodes=[UpsertNode(label="A", key="1"), UpsertNode(label="B", key="2")]
        ),
    )
    summary = upsert_edges(
        engine,
        engine.config,
        UpsertEdgesRequest(
            edges=[
                UpsertEdge(
                    type="LINKS",
                    from_label="A",
                    from_key="1",
                    to_label="B",
                    to_key="2",
                    properties={"w": 0.5},
                )
            ]
        ),
    )
    assert summary.edges == 1
    rows = engine.execute("MATCH ()-[r:LINKS]->() RETURN r.w").rows
    assert rows == [[0.5]]


# --- service wiring -----------------------------------------------------------------


def test_service_delegates_to_mutate(tmp_path):
    from grag.service import GragService

    svc = GragService(GragConfig(db_path=tmp_path / "svc.lbdb"))
    try:
        doc = svc.define_schema(DefineSchemaRequest(node_tables=[_doc_spec()]))
        assert isinstance(doc, SchemaDocument)
        summary = svc.upsert_nodes(
            UpsertNodesRequest(
                nodes=[UpsertNode(label="Doc", key="d1", properties={"title": "x"})]
            )
        )
        assert summary.nodes == 1
    finally:
        svc.close()


# --- extension loading on the write path ----------------------------------------------


def test_write_after_search_fresh_engine(tmp_path):
    """Regression: a table with an FTS/HNSW index rejects writes unless the
    extension is LOADed in-process. The search path loads extensions lazily, so
    same-session writes masked this; a fresh engine writing first always failed
    with "extension is not loaded". upsert_* must ensure extensions themselves.

    Simulates a fresh process by building an indexed table on one engine, then
    writing through a brand-new Engine on the same file (fresh extension state).
    """
    from grag.config import GragConfig
    from grag.retrieval.search import search_knowledge
    from grag.core.types import SearchRequest

    db = tmp_path / "ext.lbdb"
    cfg = GragConfig(db_path=db)

    # Engine 1: define a searchable table, insert a row, run a search so the
    # retrieval layer builds the FTS index.
    eng1 = Engine(cfg)
    try:
        define_schema(eng1, cfg, DefineSchemaRequest(node_tables=[_doc_spec()]))
        upsert_nodes(
            eng1, cfg,
            UpsertNodesRequest(
                nodes=[UpsertNode(label="Doc", key="d1", properties={"title": "graph"})]
            ),
        )
        search_knowledge(eng1, cfg, SearchRequest(query="graph", top_k=1, hops=0))
    finally:
        eng1.close()

    # Engine 2 (fresh process state): write without any prior search. Must not
    # raise "Trying to insert into an index ... but its extension is not loaded."
    eng2 = Engine(cfg)
    try:
        summary = upsert_nodes(
            eng2, cfg,
            UpsertNodesRequest(
                nodes=[UpsertNode(label="Doc", key="d2", properties={"title": "fresh write"})]
            ),
        )
        assert summary.nodes == 1
        rows = eng2.execute("MATCH (d:Doc) RETURN count(d)").rows
        assert rows == [[2]]
    finally:
        eng2.close()
