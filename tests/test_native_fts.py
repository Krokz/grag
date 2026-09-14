"""Persisted FTS mutation coverage and incident-specific diagnostics."""
from __future__ import annotations

import pytest

from grag import Engine
from grag.core.errors import IndexConsistencyError


def test_fts_mutations_rollback_delete_and_reopen(engine):
    engine.load_extension("FTS")
    engine.execute_write("CREATE NODE TABLE Decision(name STRING PRIMARY KEY, rationale STRING)")
    engine.execute_write("CREATE (:Decision {name:'release', rationale:'awaiting qualification'})")
    engine.execute_write("CALL CREATE_FTS_INDEX('Decision','grag_fts__Decision',['name','rationale'])")
    engine.close()
    for cycle in range(4):
        # Exercise repeated database lifetimes on the same thread too.
        with Engine(engine.config) as reopened:
            if cycle:
                assert reopened.execute("CALL QUERY_FTS_INDEX('Decision','grag_fts__Decision','qualified') RETURN node.name").rows == [['release']]
            with pytest.raises(ValueError, match="abort"), reopened.write_transaction():
                reopened.execute_write("MATCH (n:Decision) SET n.rationale='discarded change'")
                raise ValueError("abort")
            with reopened.write_transaction():
                reopened.execute_write("MATCH (n:Decision) SET n.rationale=$text",
                                       {"text": f"qualified revision {cycle}"})
            assert reopened.execute("CALL QUERY_FTS_INDEX('Decision','grag_fts__Decision','discarded') RETURN node.name").rows == []
    with Engine(engine.config) as reopened:
        assert reopened.execute("CALL QUERY_FTS_INDEX('Decision','grag_fts__Decision','qualified') RETURN node.name").rows == [['release']]
        reopened.execute_write("MATCH (n:Decision) DETACH DELETE n")
        assert reopened.execute("CALL QUERY_FTS_INDEX('Decision','grag_fts__Decision','qualified') RETURN node.name").rows == []


def test_fts_consistency_error_rolls_back_with_repair_guidance(engine, monkeypatch):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    original = engine._write_conn.execute

    def fail(query, parameters=None):
        if query == "RETURN 'trigger'":
            raise RuntimeError("Runtime exception: FTS index 'grag_fts__Decision' is inconsistent: term 'await' is missing during delete. Drop and recreate the FTS index.")
        return original(query, parameters)

    monkeypatch.setattr(engine._write_conn, 'execute', fail)
    with pytest.raises(IndexConsistencyError) as error, engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id:'discarded'})")
        engine.execute_write("RETURN 'trigger'")
    assert error.value.code == 'index_inconsistent'
    assert 'verified recovery copy' in error.value.hint
    assert 'syntax' not in error.value.hint
    assert engine.execute("MATCH (n:Doc) RETURN n.id").rows == []
