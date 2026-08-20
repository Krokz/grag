"""Round-trip tests for grag export / import (grag.transfer)."""

from __future__ import annotations

import json

import pytest

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import GragError
from grag.core.mutate import define_schema, upsert_edges, upsert_nodes
from grag.core.types import (
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.transfer import export_lines, import_from


@pytest.fixture()
def config():
    return GragConfig(db_path=":memory:")


@pytest.fixture()
def populated(config):
    engine = Engine(config)
    define_schema(
        engine,
        config,
        DefineSchemaRequest(
            node_tables=[
                NodeTableSpec(
                    name="Person",
                    primary_key="id",
                    searchable=True,
                    properties=[
                        PropertySpec(name="name"),
                        PropertySpec(name="age", type="INT64"),
                    ],
                ),
                NodeTableSpec(
                    name="Team",
                    primary_key="id",
                    properties=[PropertySpec(name="name")],
                ),
            ],
            rel_tables=[
                RelTableSpec(
                    name="MEMBER_OF",
                    from_label="Person",
                    to_label="Team",
                    properties=[PropertySpec(name="role")],
                )
            ],
        ),
    )
    upsert_nodes(
        engine,
        config,
        UpsertNodesRequest(
            nodes=[
                UpsertNode(
                    label="Person",
                    key="p1",
                    properties={"name": "Ada", "age": 36},
                    source="handbook.md",
                ),
                UpsertNode(label="Person", key="p2", properties={"name": "Lin"}),
                UpsertNode(label="Team", key="t1", properties={"name": "Platform"}),
            ]
        ),
    )
    upsert_edges(
        engine,
        config,
        UpsertEdgesRequest(
            edges=[
                UpsertEdge(
                    type="MEMBER_OF",
                    from_label="Person",
                    from_key="p1",
                    to_label="Team",
                    to_key="t1",
                    properties={"role": "lead"},
                    source="handbook.md",
                )
            ]
        ),
    )
    yield engine
    engine.close()


def test_export_stream_shape(populated):
    lines = list(export_lines(populated))
    records = [json.loads(line) for line in lines]
    assert records[0]["type"] == "grag_export"
    assert records[1]["type"] == "schema"
    kinds = {r["type"] for r in records}
    assert {"node", "edge"} <= kinds
    node_labels = {r["label"] for r in records if r["type"] == "node"}
    assert node_labels == {"Person", "Team"}
    ada = next(
        r for r in records if r["type"] == "node" and r.get("key") == "p1"
    )
    assert ada["properties"]["name"] == "Ada"
    assert ada["properties"]["age"] == 36
    assert ada["source"] == "handbook.md"
    edge = next(r for r in records if r["type"] == "edge")
    assert edge["rel"] == "MEMBER_OF"
    assert edge["from"] == "p1" and edge["to"] == "t1"
    assert edge["properties"]["role"] == "lead"


def test_roundtrip_into_fresh_database(populated, config):
    lines = list(export_lines(populated))

    target_cfg = GragConfig(db_path=":memory:")
    target = Engine(target_cfg)
    try:
        report = import_from(target, target_cfg, lines)
        assert report["nodes"] == 3
        assert report["edges"] == 1

        res = target.execute("MATCH (p:Person) RETURN p.id, p.name, p.age ORDER BY p.id")
        assert res.rows == [["p1", "Ada", 36], ["p2", "Lin", None]]
        res = target.execute(
            "MATCH (p:Person)-[r:MEMBER_OF]->(t:Team) RETURN p.id, r.role, t.id"
        )
        assert res.rows == [["p1", "lead", "t1"]]
        # provenance survives
        res = target.execute("MATCH (p:Person {id: 'p1'}) RETURN p._source")
        assert res.rows == [["handbook.md"]]
    finally:
        target.close()


def test_import_is_idempotent(populated, config):
    lines = list(export_lines(populated))
    target_cfg = GragConfig(db_path=":memory:")
    target = Engine(target_cfg)
    try:
        import_from(target, target_cfg, lines)
        import_from(target, target_cfg, lines)  # replay: merge, not duplicate
        assert target.execute("MATCH (p:Person) RETURN count(p)").rows == [[2]]
        assert (
            target.execute("MATCH ()-[r:MEMBER_OF]->() RETURN count(r)").rows == [[1]]
        )
    finally:
        target.close()


def test_import_rejects_non_export_file(config):
    target = Engine(config)
    try:
        with pytest.raises(GragError, match="header"):
            import_from(target, config, ['{"type": "schema"}'])
        with pytest.raises(GragError, match="header"):
            import_from(target, config, [])
    finally:
        target.close()


def test_import_rejects_newer_format(config):
    target = Engine(config)
    try:
        with pytest.raises(GragError, match="newer"):
            import_from(
                target,
                config,
                ['{"type": "grag_export", "format_version": 999}'],
            )
    finally:
        target.close()


def test_export_excludes_internal_tables(populated):
    records = [json.loads(line) for line in export_lines(populated)]
    schema = next(r for r in records if r["type"] == "schema")
    names = {t["name"] for t in schema["node_tables"]}
    assert not any(n.startswith("_") for n in names)
    assert all(not r["label"].startswith("_") for r in records if r["type"] == "node")
