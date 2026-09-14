"""M32: identifiers fail before DDL; schema publication is all-or-nothing."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest

from grag.core.errors import CypherError, SchemaError
from grag.core.ident import RESERVED_KEYWORDS, validate_identifier
from grag.core.mutate import define_schema, upsert_nodes
from grag.core.schema import build_schema_document
from grag.core.types import DefineSchemaRequest, UpsertNodesRequest


def request(nodes=None, rels=None, **kwargs):
    return DefineSchemaRequest.model_validate({"node_tables": nodes or [], "rel_tables": rels or [], **kwargs})


def schema(engine):
    return build_schema_document(engine, engine.config).model_dump()


@pytest.mark.parametrize("name", ["optional", "Optional", "OpTiOnAl", "WHERE", "default", "index", "COMMIT_SKIP_CHECKPOINT"])
def test_reserved_names_have_specific_correction(name):
    with pytest.raises(SchemaError) as error:
        validate_identifier(name, "property of 'Note'")
    assert name in error.value.message and "property of 'Note'" in error.value.message
    assert f"{name}_value" in error.value.hint
    assert validate_identifier(name + "_value") == name + "_value"


@pytest.mark.parametrize("name", ["`optional`", "has space", "has-dash", "1name", "x); DROP TABLE Note", "", "\nNote"])
def test_quoting_and_unsafe_spelling_are_not_a_schema_escape_hatch(name):
    with pytest.raises(SchemaError, match="Invalid"):
        validate_identifier(name)


def test_reserved_list_matches_actual_native_parser(engine):
    # Execute the generated DDL shapes against the pinned binary, rather than
    # testing the Python lookup against a duplicate copy of itself. This also
    # forces keyword policy review when the dependency changes its grammar.
    for name in sorted(RESERVED_KEYWORDS):
        with pytest.raises(CypherError, match="Parser exception"):
            engine.execute_write(f"CREATE NODE TABLE KeywordProbe(id STRING PRIMARY KEY, {name.lower()} STRING)")


@pytest.mark.parametrize("role", ["node", "property", "primary_key", "rel", "rel_property", "from_label", "to_label"])
def test_entire_request_validates_identifiers_before_any_schema_write(engine, monkeypatch, role):
    before = schema(engine)
    nodes = [{"name": "Earlier"}, {"name": "Later"}]
    rels = [{"name": "LINKS", "from_label": "Earlier", "to_label": "Later"}]
    if role == "node":
        nodes[1]["name"] = "optional"
    elif role == "property":
        nodes[1]["properties"] = [{"name": "optional"}]
    elif role == "primary_key":
        nodes[1]["primary_key"] = "optional"
    elif role == "rel":
        rels[0]["name"] = "optional"
    elif role == "rel_property":
        rels[0]["properties"] = [{"name": "optional"}]
    else:
        rels[0][role] = "optional"
    statements = []
    original = engine.execute_write

    def record(cypher, params=None):
        statements.append(cypher)
        return original(cypher, params)

    monkeypatch.setattr(engine, "execute_write", record)
    with pytest.raises(SchemaError, match="optional"):
        define_schema(engine, engine.config, request(nodes, rels))
    assert not any(s.startswith(("CREATE", "MERGE", "ALTER")) for s in statements)
    assert schema(engine) == before


@pytest.mark.parametrize("failure", ["endpoint", "similar", "exists", "native", "registry"])
def test_late_schema_failure_rolls_back_tables_and_registry(engine, monkeypatch, failure):
    define_schema(engine, engine.config, request([{"name": "Existing", "searchable": False}]))
    before = schema(engine)
    nodes = [{"name": "Earlier"}, {"name": "Later"}]
    rels = [{"name": "LINKS", "from_label": "Earlier", "to_label": "Later"}]
    options = {}
    if failure == "endpoint":
        rels[0]["to_label"] = "Missing"
    elif failure == "similar":
        nodes[1]["name"] = "earlier"
    elif failure == "exists":
        nodes[1]["name"] = "Existing"
        options["if_not_exists"] = False
    else:
        original = engine.execute_write

        def fail(cypher, params=None):
            if failure == "native" and cypher.startswith("CREATE REL TABLE"):
                # A real native statement error aborts the current transaction.
                return original("CREATE NODE TABLE invalid syntax")
            if failure == "registry" and cypher.startswith("MERGE") and params["name"] == "Later":
                raise SchemaError("fixture registry failure")
            return original(cypher, params)

        monkeypatch.setattr(engine, "execute_write", fail)
    with pytest.raises((SchemaError, CypherError)):
        define_schema(engine, engine.config, request(nodes, rels, **options))
    assert schema(engine) == before
    registry = engine.execute("MATCH (m:_grag_tables) RETURN m.name,m.searchable").rows
    assert registry == [["Existing", False]]


def test_existing_registry_changes_roll_back_with_later_failure(engine, monkeypatch):
    define_schema(engine, engine.config, request([{"name": "Existing", "searchable": False}]))
    before = schema(engine)
    original = engine.execute_write

    def fail(cypher, params=None):
        if cypher.startswith("MERGE") and params["name"] == "Later":
            raise SchemaError("fixture after existing metadata changed")
        return original(cypher, params)

    monkeypatch.setattr(engine, "execute_write", fail)
    with pytest.raises(SchemaError):
        define_schema(engine, engine.config, request([{"name": "Existing", "searchable": True}, {"name": "Later"}]))
    assert schema(engine) == before


def test_caught_schema_failure_poisons_enclosing_transaction(engine):
    before = schema(engine)
    with pytest.raises(CypherError, match="transaction has failed"), engine.write_transaction():
        define_schema(engine, engine.config, request([{"name": "Earlier"}]))
        with pytest.raises(SchemaError):
            define_schema(engine, engine.config, request([{"name": "Later", "properties": [{"name": "optional"}]}]))
    assert schema(engine) == before


def test_uncertain_schema_commit_requires_reopen_and_keeps_whole_batch(engine, monkeypatch):
    from grag.core.engine import Engine
    from grag.core.errors import TransactionOutcomeUnknown

    req = request([{"name": "Earlier"}, {"name": "Later"}], [{"name": "LINKS", "from_label": "Earlier", "to_label": "Later"}])
    execute = engine._write_conn.execute

    def commit_then_fail(query, params=None):
        result = execute(query, params)
        if query == "COMMIT":
            result.close()
            raise RuntimeError("Interrupted.")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(engine._write_conn, "execute", commit_then_fail)
        with pytest.raises(TransactionOutcomeUnknown):
            define_schema(engine, engine.config, req)
        assert engine.runtime_info()["writer_state"] == "reopen_required"
    engine.close()
    with Engine(engine.config) as reopened:
        doc = schema(reopened)
        assert {t["name"] for t in doc["node_tables"]} == {"Earlier", "Later"}
        assert [t["name"] for t in doc["rel_tables"]] == ["LINKS"]
        assert len(reopened.execute("MATCH (m:_grag_tables) RETURN m.name").rows) == 3
        assert define_schema(reopened, reopened.config, req).schema_revision == doc["schema_revision"]


def test_schema_reader_cannot_observe_half_a_batch(engine, monkeypatch):
    created, release, reading = Event(), Event(), Event()
    original = engine.execute_write

    def pause(cypher, params=None):
        result = original(cypher, params)
        if cypher.startswith("CREATE NODE TABLE Earlier"):
            created.set()
            assert release.wait(5)
        return result

    def read():
        reading.set()
        return schema(engine)

    monkeypatch.setattr(engine, "execute_write", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(define_schema, engine, engine.config, request([{"name": "Earlier"}, {"name": "Later"}]))
        try:
            assert created.wait(5)
            reader = pool.submit(read)
            assert reading.wait(5)
            assert not reader.done()
        finally:
            release.set()
        writer.result(5)
        assert {t["name"] for t in reader.result(5)["node_tables"]} == {"Earlier", "Later"}


def test_nonreserved_keywords_and_typed_primary_keys_still_roundtrip(engine):
    # Ladybug explicitly allows these words as symbolic names. Do not reject
    # all SQL/Cypher tokens or use the upstream docs' outdated reserved list.
    properties = [{"name": n} for n in ("return", "from", "to", "is", "limit", "type", "count")]
    req = request([
        {"name": "Match", "primary_key": "key", "properties": [{"name": "key", "type": "INT64"}, *properties]},
        {"name": "Target", "primary_key": "date", "properties": [{"name": "date", "type": "DATE"}]},
    ], [{"name": "Return", "from_label": "Match", "to_label": "Target", "properties": [{"name": "type"}]}])
    define_schema(engine, engine.config, req)
    define_schema(engine, engine.config, req)  # Existing tables remain idempotent.
    result = upsert_nodes(engine, engine.config, UpsertNodesRequest.model_validate({
        "nodes": [
            {"label": "Match", "key": 42, "properties": {p["name"]: "kept" for p in properties}},
            {"label": "Target", "key": "2026-09-14"},
        ], "edges": [{"type": "Return", "from_label": "Match", "from_key": 42, "to_label": "Target", "to_key": "2026-09-14", "properties": {"type": "kept"}}],
    }))
    assert result.nodes == 2 and result.edges == 1
    assert engine.execute("MATCH (n:Match)-[r:Return]->(t:Target) RETURN n.key,n.return,n.limit,r.type,CAST(t.date AS STRING)").rows == [[42, "kept", "kept", "kept", "2026-09-14"]]


def test_mcp_error_is_schema_error_and_corrected_retry_succeeds(tmp_path):
    from grag.config import GragConfig
    from grag.mcp_server import server as mcp
    from grag.service import GragService

    with closing(GragService(GragConfig(db_path=tmp_path / "mcp.lbdb", buffer_pool_size=128*1024**2))) as service:
        response = mcp.define_schema(service, [{"name": "Earlier"}, {"name": "Note", "properties": [{"name": "optional"}]}], [])
        error = json.loads(response.rsplit("\n---\n", 1)[-1])
        assert error["code"] == "schema_error"
        assert "optional" in error["error"] and "optional_value" in error["hint"]
        assert service.describe_schema().node_tables == []
        corrected = mcp.define_schema(service, [{"name": "Note", "properties": [{"name": "optional_value"}]}], [])
        assert not corrected.startswith("ERROR"), corrected


def test_rest_schema_error_leaves_no_partial_publication(tmp_path):
    from fastapi.testclient import TestClient

    from grag.api.main import create_app
    from grag.config import GragConfig

    with TestClient(create_app(GragConfig(db_path=tmp_path / "rest.lbdb", buffer_pool_size=128*1024**2))) as http:
        response = http.post("/api/schema/define", json={"node_tables": [{"name": "Earlier"}, {"name": "Later", "primary_key": "optional"}]})
        assert response.status_code == 400
        assert response.json()["code"] == "schema_error"
        assert "optional" in response.json()["error"]
        assert http.get("/api/schema").json()["node_tables"] == []
