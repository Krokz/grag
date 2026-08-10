"""Tests for grag.core.schema: introspection over SHOW_TABLES / TABLE_INFO /
SHOW_CONNECTION plus the grag meta table (META_TABLE)."""

from __future__ import annotations

import pytest

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.schema import build_schema_document, pk_map, table_stats
from grag.core.types import META_TABLE


@pytest.fixture()
def config() -> GragConfig:
    return GragConfig()


@pytest.fixture()
def graph(engine: Engine) -> Engine:
    """Doc/Tag node tables + HAS_TAG rel table, created via raw cypher."""
    engine.execute_write(
        "CREATE NODE TABLE Doc(id STRING PRIMARY KEY, title STRING, "
        "text STRING, _source STRING)"
    )
    engine.execute_write("CREATE NODE TABLE Tag(name STRING PRIMARY KEY)")
    engine.execute_write("CREATE REL TABLE HAS_TAG(FROM Doc TO Tag, w DOUBLE)")
    for i in range(7):
        engine.execute_write(
            "CREATE (d:Doc {id: $id, title: $t, text: 'x', _source: 'f.md'})",
            {"id": f"doc-{i}", "t": f"title {i}"},
        )
    engine.execute_write("CREATE (t:Tag {name: 'graphs'})")
    engine.execute_write("CREATE (t:Tag {name: 'vectors'})")
    engine.execute_write(
        "MATCH (a:Doc {id: 'doc-0'}), (b:Tag {name: 'graphs'}) "
        "CREATE (a)-[:HAS_TAG {w: 0.9}]->(b)"
    )
    return engine


def _create_meta(engine: Engine) -> None:
    engine.execute_write(
        f"CREATE NODE TABLE {META_TABLE}(name STRING PRIMARY KEY, kind STRING, "
        "pk STRING, searchable BOOL, from_label STRING, to_label STRING)"
    )


def _meta_row(
    engine: Engine,
    name: str,
    kind: str,
    pk: str,
    searchable: bool,
    from_label: str = "",
    to_label: str = "",
) -> None:
    engine.execute_write(
        f"CREATE (m:{META_TABLE} {{name: $n, kind: $k, pk: $p, "
        "searchable: $s, from_label: $f, to_label: $t})",
        {
            "n": name,
            "k": kind,
            "p": pk,
            "s": searchable,
            "f": from_label,
            "t": to_label,
        },
    )


# -- empty database -------------------------------------------------------------


def test_empty_database(engine: Engine, config: GragConfig):
    doc = build_schema_document(engine, config)
    assert doc.node_tables == []
    assert doc.rel_tables == []
    assert doc.text == ""
    assert pk_map(engine) == {}
    stats = table_stats(engine)
    assert stats.node_count == 0 and stats.edge_count == 0 and stats.labels == {}


# -- pk_map ---------------------------------------------------------------------


def test_pk_map_from_table_info_fallback(graph: Engine):
    """No meta table: primary keys come from TABLE_INFO's primary-key flag."""
    assert pk_map(graph) == {"Doc": "id", "Tag": "name"}


def test_pk_map_meta_wins(graph: Engine):
    """Meta rows override TABLE_INFO; tables without meta rows still fall back."""
    _create_meta(graph)
    _meta_row(graph, "Doc", "node", "title", True)
    pks = pk_map(graph)
    assert pks["Doc"] == "title"  # meta wins over TABLE_INFO's 'id'
    assert pks["Tag"] == "name"  # TABLE_INFO fallback still covers the rest


def test_pk_map_missing_meta_tolerated(graph: Engine):
    assert pk_map(graph)["Doc"] == "id"


# -- build_schema_document -------------------------------------------------------


def test_schema_roundtrip(graph: Engine, config: GragConfig):
    doc = build_schema_document(graph, config)
    names = [t.name for t in doc.node_tables]
    assert names == ["Doc", "Tag"]
    assert [t.name for t in doc.rel_tables] == ["HAS_TAG"]

    doc_table = doc.node_tables[0]
    assert doc_table.row_count == 7
    assert {p.name for p in doc_table.properties} == {"id", "title", "text", "_source"}
    pk_props = [p for p in doc_table.properties if p.is_primary_key]
    assert len(pk_props) == 1 and pk_props[0].name == "id"
    assert doc_table.searchable is False  # no meta row
    assert doc_table.sample_keys == [f"doc-{i}" for i in range(5)]  # capped at 5

    tag_table = doc.node_tables[1]
    assert tag_table.row_count == 2
    assert set(tag_table.sample_keys) == {"graphs", "vectors"}

    rel = doc.rel_tables[0]
    assert rel.from_label == "Doc"
    assert rel.to_label == "Tag"
    assert rel.row_count == 1
    assert {p.name for p in rel.properties} == {"w"}


def test_schema_searchable_flag_from_meta(graph: Engine, config: GragConfig):
    _create_meta(graph)
    _meta_row(graph, "Doc", "node", "id", True)
    _meta_row(graph, "Tag", "node", "name", False)
    doc = build_schema_document(graph, config)
    searchable = {t.name: t.searchable for t in doc.node_tables}
    assert searchable == {"Doc": True, "Tag": False}


def test_schema_excludes_meta_table(graph: Engine, config: GragConfig):
    _create_meta(graph)
    _meta_row(graph, "Doc", "node", "id", True)
    doc = build_schema_document(graph, config)
    all_names = [t.name for t in doc.node_tables] + [t.name for t in doc.rel_tables]
    assert META_TABLE not in all_names
    stats = table_stats(graph)
    assert META_TABLE not in stats.labels


def test_schema_excludes_any_internal_table(graph: Engine, config: GragConfig):
    """Any "_"-prefixed table is grag-internal, not just the registry."""
    _create_meta(graph)
    graph.execute_write("CREATE NODE TABLE _grag_scratch(tmp STRING PRIMARY KEY)")
    graph.execute_write("CREATE (s:_grag_scratch {tmp: 'x'})")
    graph.execute_write(
        "CREATE REL TABLE _grag_trace(FROM Doc TO _grag_scratch, note STRING)"
    )

    doc = build_schema_document(graph, config)
    all_names = [t.name for t in doc.node_tables] + [t.name for t in doc.rel_tables]
    assert all(not n.startswith("_") for n in all_names)
    assert "_grag_scratch" not in doc.text and "_grag_trace" not in doc.text

    stats = table_stats(graph)
    assert all(not k.startswith("_") for k in stats.labels)
    assert stats.node_count == 9  # Doc + Tag only

    assert all(not k.startswith("_") for k in pk_map(graph))


def test_schema_text_rendering(graph: Engine, config: GragConfig):
    _create_meta(graph)
    _meta_row(graph, "Doc", "node", "id", True)
    doc = build_schema_document(graph, config)
    text = doc.text
    assert "Doc(id:STRING PK, title:STRING, text:STRING, _source:STRING)" in text
    assert "[7 rows, searchable]" in text
    assert 'samples: "doc-0"' in text
    assert "Tag(name:STRING PK) [2 rows]" in text  # no meta -> not searchable
    assert "HAS_TAG(Doc -> Tag, w:DOUBLE) [1 rows]" in text
    assert META_TABLE not in text


def test_schema_text_empty_db_is_empty(engine: Engine, config: GragConfig):
    assert build_schema_document(engine, config).text == ""


# -- table_stats -----------------------------------------------------------------


def test_table_stats(graph: Engine):
    stats = table_stats(graph)
    assert stats.node_count == 9
    assert stats.edge_count == 1
    assert stats.labels == {"Doc": 7, "Tag": 2, "HAS_TAG": 1}


def test_table_stats_empty_tables(engine: Engine):
    engine.execute_write("CREATE NODE TABLE Empty(id STRING PRIMARY KEY)")
    stats = table_stats(engine)
    assert stats.node_count == 0
    assert stats.labels == {"Empty": 0}
