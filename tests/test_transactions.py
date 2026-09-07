"""Single-connection transaction visibility and failure recovery."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from grag.core.errors import CypherError


def _ids(engine):
    return engine.execute("MATCH (n:Doc) RETURN n.id ORDER BY n.id").rows


def test_transaction_reads_own_writes_and_publishes_on_commit(engine):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    engine.execute_write("CREATE (:Doc {id: 'before'})")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine.write_transaction():
            engine.execute_write("CREATE (:Doc {id: 'inside'})")
            assert _ids(engine) == [["before"], ["inside"]]
            assert pool.submit(_ids, engine).result(timeout=5) == [["before"]]
        assert pool.submit(_ids, engine).result(timeout=5) == [["before"], ["inside"]]


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_transaction_rolls_back_python_failures_and_remains_usable(engine, error):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    with pytest.raises(error), engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id: 'discard'})")
        raise error("interrupted")
    assert _ids(engine) == []
    with engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id: 'retry'})")
    assert _ids(engine) == [["retry"]]


def test_caught_native_error_cannot_turn_later_writes_into_autocommits(engine):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    engine.execute_write("CREATE (:Doc {id: 'before'})")
    with (
        pytest.raises(CypherError, match="transaction has failed"),
        engine.write_transaction(),
    ):
        engine.execute_write("CREATE (:Doc {id: 'discard'})")
        with pytest.raises(CypherError):
            engine.execute_write("CREATE (:Doc {id: 'before'})")
        with pytest.raises(CypherError, match="transaction has failed"):
            engine.execute_write("CREATE (:Doc {id: 'must-not-escape'})")
    assert _ids(engine) == [["before"]]
    engine.execute_write("CREATE (:Doc {id: 'after'})")
    assert _ids(engine) == [["after"], ["before"]]


def test_nested_transaction_is_rejected_without_committing_the_outer(engine):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY)")
    with pytest.raises(CypherError, match="Nested"), engine.write_transaction():
        engine.execute_write("CREATE (:Doc {id: 'discard'})")
        with engine.write_transaction():
            pass
    assert _ids(engine) == []


def test_fts_index_tracks_rollback_and_retry(engine):
    engine.execute_write("CREATE NODE TABLE Doc(id STRING PRIMARY KEY, text STRING)")
    engine.execute_write("CREATE (:Doc {id: 'one', text: 'orchid'})")
    engine.load_extension("FTS")
    engine.execute_write("CALL CREATE_FTS_INDEX('Doc', 'audit_fts', ['text'])")
    with pytest.raises(RuntimeError), engine.write_transaction():
        engine.execute_write("MATCH (n:Doc) SET n.text = 'tulip'")
        raise RuntimeError("interrupted")
    query = "CALL QUERY_FTS_INDEX('Doc', 'audit_fts', $q, TOP := 5) RETURN node.id"
    assert engine.execute(query, {"q": "orchid"}).rows == [["one"]]
    assert engine.execute(query, {"q": "tulip"}).rows == []
    with engine.write_transaction():
        engine.execute_write("MATCH (n:Doc) SET n.text = 'tulip'")
    assert engine.execute(query, {"q": "tulip"}).rows == [["one"]]


def test_vector_index_survives_rolled_back_invalidation_and_pruning(engine):
    """Code ingestion clears changed vectors and detaches removed symbols."""
    engine.load_extension("VECTOR")
    engine.execute_write("CREATE NODE TABLE Emb(id STRING PRIMARY KEY, embedding FLOAT[3])")
    vectors = ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])
    for i, vector in enumerate(vectors):
        engine.execute_write(
            "CREATE (:Emb {id: $id, embedding: $v})", {"id": str(i), "v": vector}
        )
    engine.execute_write(
        "CALL CREATE_VECTOR_INDEX('Emb', 'emb_vec', 'embedding', metric := 'cosine')"
    )
    with pytest.raises(RuntimeError), engine.write_transaction():
        engine.execute_write("MATCH (n:Emb {id: '0'}) SET n.embedding = NULL")
        engine.execute_write("MATCH (n:Emb {id: '1'}) DETACH DELETE n")
        raise RuntimeError("interrupted")
    query = "CALL QUERY_VECTOR_INDEX('Emb', 'emb_vec', $q, 1) RETURN node.id"
    for i, vector in enumerate(vectors):
        assert engine.execute(query, {"q": vector}).rows == [[str(i)]]
