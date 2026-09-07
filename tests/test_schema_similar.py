"""define_schema refuses near-duplicate labels so agent-built graphs converge."""

from __future__ import annotations

import pytest

from grag.core.errors import SchemaError
from grag.core.mutate import _canonical_name, define_schema, similar_table
from grag.core.types import DefineSchemaRequest, NodeTableSpec, RelTableSpec


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Decision", "Decisions"),
        ("Decision", "decision"),
        ("TodoItem", "todo_item"),
        ("Todo-Item", "TodoItems"),
        ("Category", "Categories"),
        ("Process", "Processes"),
        ("MENTIONS", "mentions"),
    ],
)
def test_canonical_name_merges_case_plural_punctuation(a, b):
    assert _canonical_name(a) == _canonical_name(b)


@pytest.mark.parametrize(("a", "b"), [("Person", "Persona"), ("Class", "Classifier"), ("Task", "Ask")])
def test_canonical_name_keeps_distinct_concepts_apart(a, b):
    assert _canonical_name(a) != _canonical_name(b)


def test_similar_table_ignores_exact_and_internal():
    assert similar_table("Decision", ["Decision"]) is None
    assert similar_table("Decisions", ["Decision", "_grag_tables"]) == "Decision"
    assert similar_table("_grag_table", ["_grag_tables"]) is None


def test_define_schema_refuses_near_duplicate_with_hint(engine):
    define_schema(
        engine, engine.config, DefineSchemaRequest(node_tables=[NodeTableSpec(name="Decision")])
    )
    with pytest.raises(SchemaError) as exc:
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(node_tables=[NodeTableSpec(name="Decisions")]),
        )
    assert "Decision" in exc.value.message and "allow_similar" in (exc.value.hint or "")
    tables = {r[1] for r in engine.execute("CALL SHOW_TABLES() RETURN *").rows}
    assert "Decisions" not in tables


def test_define_schema_allow_similar_creates_anyway(engine):
    define_schema(
        engine, engine.config, DefineSchemaRequest(node_tables=[NodeTableSpec(name="Decision")])
    )
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(node_tables=[NodeTableSpec(name="Decisions")], allow_similar=True),
    )
    tables = {r[1] for r in engine.execute("CALL SHOW_TABLES() RETURN *").rows}
    assert {"Decision", "Decisions"} <= tables


def test_define_schema_near_duplicate_rel(engine):
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(
            node_tables=[NodeTableSpec(name="A"), NodeTableSpec(name="B")],
            rel_tables=[RelTableSpec(name="MENTIONS", from_label="A", to_label="B")],
        ),
    )
    with pytest.raises(SchemaError, match="MENTIONS"):
        define_schema(
            engine,
            engine.config,
            DefineSchemaRequest(rel_tables=[RelTableSpec(name="mentions", from_label="A", to_label="B")]),
        )


def test_mcp_tool_surfaces_hint(tmp_path):
    from grag.config import GragConfig
    from grag.mcp_server import server as mcp
    from grag.service import GragService

    svc = GragService(GragConfig(db_path=tmp_path / "sim.lbdb"))
    try:
        mcp.define_schema(svc, [{"name": "Insight"}], [])
        out = mcp.define_schema(svc, [{"name": "insights"}], [])
        assert out.startswith("ERROR:") and "allow_similar" in out
        assert not mcp.define_schema(svc, [{"name": "insights"}], [], allow_similar=True).startswith("ERROR")
    finally:
        svc.close()
