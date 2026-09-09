"""Real native deadlines and completion safety; also run under forced C-API in CI."""
from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import (
    ConfigurationError,
    QueryInterruptedError,
    ResourceLimitError,
    TransactionOutcomeUnknown,
)
from grag.core.limits import WORK_LIMITS, work_budget
from grag.core.mutate import define_schema, upsert_nodes
from grag.core.types import DefineSchemaRequest, UpsertNodesRequest
from grag.native import configure_query_timeout


@pytest.mark.parametrize("value", ["-1", "1.5", "true", "", "2147483648"])
def test_invalid_timeout_environment_rejected(monkeypatch, value):
    monkeypatch.setenv("GRAG_STATEMENT_TIMEOUT_MS", value)
    with pytest.raises(ValueError, match="GRAG_STATEMENT_TIMEOUT_MS"):
        GragConfig.from_env()


@pytest.mark.parametrize("value", [-1, True, 1.5, "30", 2147483648])
def test_invalid_python_timeout_rejected(value):
    with pytest.raises(ValidationError):
        GragConfig(statement_timeout_ms=value)


@pytest.mark.parametrize("value", [0, 50, 30_000, 2147483647])
def test_timeout_environment_and_python_configuration(monkeypatch, value):
    monkeypatch.setenv("GRAG_STATEMENT_TIMEOUT_MS", str(value))
    assert GragConfig.from_env().statement_timeout_ms == value
    assert GragConfig(statement_timeout_ms=value).statement_timeout_ms == value


def test_invalid_timeout_cli_does_not_create_database(tmp_path, monkeypatch, capsys):
    from grag.cli import main

    monkeypatch.setenv("GRAG_STATEMENT_TIMEOUT_MS", "-30")
    assert main(["--db", str(tmp_path / "absent.lbdb"), "status"]) == 1
    assert "GRAG_STATEMENT_TIMEOUT_MS" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("ladybug_version", ["0.20.2", "0.20.3"])
def test_capi_workaround_preserves_native_limit_and_is_connection_local(monkeypatch, ladybug_version):
    from grag import native

    class Inner:
        _query_timeout_ms = 0

    Inner.__module__ = "ladybug._lbug_capi"

    class Outer:
        def __init__(self):
            self._connection = Inner()
            self._query_timeout_ms = 0
            self.native_ms = None

        def set_query_timeout(self, value):
            self.native_ms = self._query_timeout_ms = self._connection._query_timeout_ms = value

    a, b = Outer(), Outer()
    b.set_query_timeout(123)
    monkeypatch.setattr(native, "version", lambda package: ladybug_version)
    configure_query_timeout(a, 30_000)
    assert a.native_ms == 30_000
    assert a._query_timeout_ms == a._connection._query_timeout_ms == 0
    assert b.native_ms == b._query_timeout_ms == b._connection._query_timeout_ms == 123
    configure_query_timeout(a, 0)
    assert a.native_ms == 0
    monkeypatch.setattr(native, "version", lambda package: "0.21.0")
    with pytest.raises(ConfigurationError, match="Unverified"):
        configure_query_timeout(a, 1)


def test_actual_backend_and_nontrivial_read(engine):
    runtime = engine.runtime_info()
    expected = os.environ.get("LBUG_PYTHON_BACKEND", "auto")
    if expected in {"pybind", "capi"}:
        assert runtime["backend"] == expected  # Never silently test the other backend.
    assert runtime["backend"] in {"pybind", "capi"}
    assert runtime["timeout_mode"] == "native"
    # The original outer C-API wrapper rejects this tiny valid query outright.
    assert engine.execute("UNWIND range(1,2) AS x UNWIND range(1,3) AS y RETURN sum(x*y)").rows == [[18]]
    # Long enough to exercise the original 10ms watchdog on normal CI machines.
    assert len(engine.execute("UNWIND range(1,20000) AS x UNWIND range(1,100) AS y RETURN sum(sin(x)+cos(y))").rows) == 1


EXPENSIVE = "UNWIND range(1,1000000) AS x UNWIND range(1,100) AS y RETURN sum(sin(x)+cos(y))"


def test_native_deadline_interrupts_and_reader_recovers(engine, monkeypatch):
    monkeypatch.setattr(engine, "_statement_timeout_ms", 50)
    # Cooperative native checks are not an exact wall-clock deadline.
    with pytest.raises(QueryInterruptedError):
        engine.execute(EXPENSIVE)
    assert engine.execute("RETURN 42").rows == [[42]]
    monkeypatch.setattr(engine, "_statement_timeout_ms", 0)
    assert engine.execute("UNWIND range(1,20000) AS x UNWIND range(1,100) AS y RETURN sum(x*y)").rows
    assert engine.runtime_info()["statement_timeout_ms"] == 0


def test_native_deadline_rolls_back_transaction_and_allows_retry(engine, monkeypatch):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    with pytest.raises(QueryInterruptedError), engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id:'discard'})")
        monkeypatch.setattr(engine, "_statement_timeout_ms", 50)
        engine.execute(EXPENSIVE)
    monkeypatch.setattr(engine, "_statement_timeout_ms", 30_000)
    assert engine.execute("MATCH (n:Doc) RETURN n.id").rows == []
    with engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id:'retry'})")
    assert engine.execute("MATCH (n:Doc) RETURN n.id").rows == [["retry"]]
    assert engine.runtime_info()["writer_state"] == "ready"


def test_large_commit_checkpoint_and_strict_reopen(engine, monkeypatch):
    conn = engine._write_conn
    real = conn.execute
    seen = []

    def observe(query, parameters=None):
        if query in {"COMMIT", "ROLLBACK", "CHECKPOINT"}:
            seen.append((query, engine._configured_timeouts[conn]))
        return real(query, parameters)

    monkeypatch.setattr(conn, "execute", observe)
    engine.execute_write("CREATE NODE TABLE Doc(id INT64 PRIMARY KEY, body STRING)")
    with engine.write_transaction():
        engine.execute_write("UNWIND range(1,6000) AS id CREATE (:Doc {id:id, body:$body})", {"body": "x" * 2048})
    assert engine._configured_timeouts[conn] == 0
    engine.execute_write("RETURN 1")
    assert engine._configured_timeouts[conn] == 30_000
    engine.close()
    assert seen == [("COMMIT", 0), ("CHECKPOINT", 0)]
    with Engine(engine.config) as reopened:
        assert reopened.execute("MATCH (n:Doc) RETURN count(n), min(size(n.body))").rows == [[6000, 2048]]


def test_repeated_checkpoints_reuse_native_capacity(tmp_path):
    """0.20.2 exhausted a 64 MiB virtual capacity after only seven checkpoints."""
    import ladybug as lb

    from grag.native import prepare_native_runtime

    prepare_native_runtime()
    path = tmp_path / "checkpoints.lbdb"
    db = lb.Database(str(path), buffer_pool_size=16 * 1024**2, max_db_size=64 * 1024**2)
    conn = lb.Connection(db)
    try:
        conn.execute("CREATE NODE TABLE CheckpointDoc(id INT64 PRIMARY KEY)").close()
        for index in range(50):
            conn.execute(f"CREATE (:CheckpointDoc {{id:{index}}})").close()
            conn.execute("CHECKPOINT").close()
    finally:
        conn.close()
        db.close()
    db = lb.Database(str(path), buffer_pool_size=16 * 1024**2, read_only=True,
                     throw_on_wal_replay_failure=True)
    conn = lb.Connection(db)
    try:
        result = conn.execute("MATCH (n:CheckpointDoc) RETURN count(n),min(n.id),max(n.id)")
        try:
            assert result.get_all() == [[50, 0, 49]]
        finally:
            result.close()
    finally:
        conn.close()
        db.close()


def test_nontrivial_code_documents_and_fts_survive_reopen(tmp_path):
    from grag.core.types import CodeIngestRequest, SearchRequest
    from grag.ingest.loaders import load_request
    from grag.service import GragService

    code, docs = tmp_path / "code", tmp_path / "docs"
    code.mkdir()
    docs.mkdir()
    for i in range(12):
        (code / f"module_{i}.py").write_text("\n".join(
            f'def policy_{i}_{j}():\n    """Return a timeout policy."""\n    return {j}\n' for j in range(8)), encoding="utf-8")
        (docs / f"guide_{i}.md").write_text(f"# Guide {i}\n\n" + "\n\n".join(
            f"## Policy {j}\n\n" + "quartzrule preserves committed graph memories. " * 40 for j in range(3)), encoding="utf-8")
    config = GragConfig(db_path=tmp_path / "ingest.lbdb", buffer_pool_size=128 * 1024**2)
    service = GragService(config)
    try:
        service.ingest_code(CodeIngestRequest(paths=[str(code)]))
        request, warnings, files = load_request([docs], sections=True)
        assert files == 12 and not warnings
        service.ingest(request)
        assert service.engine.execute("MATCH (f:Function) RETURN count(f)").rows == [[96]]
        assert service.engine.execute("MATCH (s:Section) RETURN count(s)").rows == [[48]]
    finally:
        service.close()
    service = GragService(config)
    try:
        assert service.engine.execute("MATCH (m:Module) RETURN count(m)").rows == [[12]]
        assert service.engine.execute("MATCH (s:Section) RETURN count(s)").rows == [[48]]
        result = service.search_knowledge(SearchRequest(query="quartzrule", labels=["Section"], hops=0))
        assert result.included_node_ids
    finally:
        service.close()


def test_rollback_survives_exhausted_work_budget(engine, monkeypatch):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    monkeypatch.setitem(WORK_LIMITS, "statements", 2)
    with pytest.raises(ResourceLimitError), work_budget(), engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id:'discard'})")
        engine.execute_write("RETURN 1")
    assert engine.execute("MATCH (n:Doc) RETURN n.id").rows == []
    assert engine.runtime_info()["writer_state"] == "ready"


def test_failed_timeout_setter_never_executes_statement(engine, monkeypatch):
    from grag.core import engine as module

    real = module.configure_query_timeout

    def fail(conn, milliseconds):
        if milliseconds == 0:
            raise ConfigurationError("injected timeout configuration failure")
        real(conn, milliseconds)

    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    # A setter failure BEFORE COMMIT executes is not reported as a committed write.
    with monkeypatch.context() as patch:
        patch.setattr(module, "configure_query_timeout", fail)
        with pytest.raises(TransactionOutcomeUnknown), engine.write_transaction():
            engine.execute_write("CREATE (:Doc {id:'discard'})")
    # Both COMMIT and ROLLBACK could not be configured: no further writes.
    with pytest.raises(TransactionOutcomeUnknown):
        engine.execute_write("CREATE (:Doc {id:'later'})")
    engine.close()
    with Engine(engine.config) as reopened:
        assert reopened.execute("MATCH (n:Doc) RETURN n.id").rows == []


def test_failed_reader_configuration_does_not_leak_pool_capacity(engine, monkeypatch):
    from grag.core import engine as module

    def fail(*args):
        raise ConfigurationError("injected setter failure")

    with monkeypatch.context() as patch:
        patch.setattr(module, "configure_query_timeout", fail)
        with pytest.raises(ConfigurationError):
            engine.execute("RETURN 42")
    assert engine._readers_created == 0
    assert engine.execute("RETURN 42").rows == [[42]]


def test_configuration_failure_before_commit_can_roll_back(engine, monkeypatch):
    real = engine._write_conn.set_query_timeout
    failed = False

    def fail_once(milliseconds):
        nonlocal failed
        if milliseconds == 0 and not failed:
            failed = True
            raise RuntimeError("injected setter failure")
        real(milliseconds)

    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    monkeypatch.setattr(engine._write_conn, "set_query_timeout", fail_once)
    with pytest.raises(ConfigurationError), engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id:'discard'})")
    assert engine.execute("MATCH (n:Doc) RETURN n.id").rows == []
    assert engine.runtime_info()["writer_state"] == "ready"


def test_restore_timeout_failure_is_deferred_until_next_statement(engine, monkeypatch):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    real = engine._write_conn.set_query_timeout

    def fail_restore(milliseconds):
        if milliseconds:
            raise RuntimeError("injected setter failure")
        real(milliseconds)

    with engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id:'saved'})")
        monkeypatch.setattr(engine._write_conn, "set_query_timeout", fail_restore)
    # COMMIT returns success; no timeout restoration can mask the committed write.
    with pytest.raises(ConfigurationError):
        engine.execute_write("CREATE (:Doc {id:'later'})")
    assert engine.execute("MATCH (n:Doc) RETURN n.id").rows == [["saved"]]


@pytest.mark.parametrize("error", [QueryInterruptedError, TransactionOutcomeUnknown])
def test_protocol_errors_are_actionable_without_syntax_hint(tmp_path, monkeypatch, error):
    from fastapi.testclient import TestClient

    from grag.api.main import create_app
    from grag.mcp_server import server as mcp

    with TestClient(create_app(GragConfig(db_path=tmp_path / "api.lbdb", buffer_pool_size=128 * 1024**2))) as client:
        service = client.app.state.service

        def fail(*args, **kwargs):
            raise error()

        monkeypatch.setattr(service, "cypher_query", fail)
        rest = client.post("/api/query", json={"cypher": "RETURN 42"})
        assert rest.status_code == 503
        result = mcp._mcp_result(mcp.cypher_query)(service, "RETURN 42")
        assert result.is_error
        direct = result.structured_content
        assert direct == rest.json()
        assert direct["code"] == error.code
        assert "syntax" not in direct["hint"]
        assert "operation_id" in direct["hint"]
        service.engine._writer_recovery_required = True
        health = client.get("/api/health").json()
        assert health["status"] == "reopen_required"
        assert health["engine"]["writer_state"] == "reopen_required"


def test_ambiguous_commit_reopens_and_replays_original_receipt(engine, monkeypatch):
    define_schema(engine, engine.config, DefineSchemaRequest.model_validate({
        "node_tables": [{"name": "Memory", "properties": [{"name": "body"}]}], "rel_tables": [],
    }))
    request = UpsertNodesRequest.model_validate({
        "nodes": [{"label": "Memory", "key": "one", "properties": {"body": "saved"},
                   "evidence": {"actor": "M33"}}], "operation_id": "uncertain-commit",
    })
    real = engine._write_conn.execute

    def commit_then_fail(query, parameters=None):
        result = real(query, parameters)
        if query == "COMMIT":
            result.close()
            raise RuntimeError("Interrupted.")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(engine._write_conn, "execute", commit_then_fail)
        with pytest.raises(TransactionOutcomeUnknown) as failure:
            upsert_nodes(engine, engine.config, request)
        assert "same operation_id" in failure.value.hint
        with pytest.raises(TransactionOutcomeUnknown):
            engine.execute_write("CREATE (:Memory {id:'duplicate'})")
        assert engine.runtime_info()["writer_state"] == "reopen_required"
    engine.close()
    with Engine(engine.config) as reopened:
        result = upsert_nodes(reopened, reopened.config, request)
        assert result.replayed and result.nodes == 1
        assert reopened.execute("MATCH (n:Memory) RETURN n.id,n.body,n._evidence_seq").rows == [["one", "saved", 1]]


@pytest.mark.parametrize("command", ["BEGIN TRANSACTION", "COMMIT", "ROLLBACK", "CHECKPOINT"])
def test_transaction_control_is_rejected_on_agent_read_path(command):
    from grag.core.errors import ReadOnlyViolation
    from grag.service import _assert_read_only

    with pytest.raises(ReadOnlyViolation):
        _assert_read_only(command)


def test_reader_commit_error_does_not_poison_the_writer(engine):
    from grag.core.errors import CypherError

    with pytest.raises(CypherError):
        engine.execute("COMMIT")
    assert engine.runtime_info()["writer_state"] == "ready"
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")


def test_failed_rollback_refuses_later_writes(engine, monkeypatch):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    real = engine._write_conn.execute

    def fail(query, parameters=None):
        if query == "ROLLBACK":
            raise RuntimeError("injected rollback failure")
        return real(query, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(engine._write_conn, "execute", fail)
        with pytest.raises(TransactionOutcomeUnknown), engine.write_transaction():
            engine.execute_write("CREATE (:Doc {id:'discard'})")
            raise ValueError("injected validation failure")
        with pytest.raises(TransactionOutcomeUnknown):
            engine.execute_write("CREATE (:Doc {id:'escape'})")
    engine.close()
    with Engine(engine.config) as reopened:
        assert reopened.execute("MATCH (n:Doc) RETURN n.id").rows == []
