"""Unit tests for the read-only guard and LIMIT pushdown in grag.service."""

from __future__ import annotations

import pytest

from grag.core.errors import ReadOnlyViolation
from grag.service import _assert_read_only, _with_limit


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (p:Person) RETURN p",
        # write keywords inside string literals / comments are not writes
        "MATCH (p:Person) WHERE p.name = 'Set Theory' RETURN p",
        "MATCH (p:Person) WHERE p.name = 'DROP TABLE' RETURN p",
        "MATCH (p:Person) RETURN p.name // a MERGE sounds nice",
        "/* CREATE nothing */ MATCH (p:Person) RETURN p",
        # known read procedures are allowed
        "CALL TABLE_INFO('Person') RETURN *",
        "CALL SHOW_TABLES() RETURN *",
        "CALL QUERY_FTS_INDEX('Doc', 'idx', $q, TOP := 5) RETURN node, score",
        "CALL QUERY_VECTOR_INDEX('Doc', 'idx', $q, 5) RETURN node, distance",
    ],
)
def test_reads_allowed(cypher):
    _assert_read_only(cypher)


@pytest.mark.parametrize(
    "cypher",
    [
        "CREATE (p:Person {name: 'x'})",
        "MATCH (p:Person) SET p.name = 'x'",
        "MATCH (p:Person) DELETE p",
        "MERGE (p:Person {name: 'x'})",
        "DROP TABLE Person",
        "LOAD EXTENSION FTS",
        # side-effecting procedures: underscores make \bCREATE\b miss these
        "CALL CREATE_VECTOR_INDEX('Doc', 'idx', 'embedding', metric := 'cosine')",
        "CALL DROP_FTS_INDEX('Doc', 'idx')",
    ],
)
def test_writes_rejected(cypher):
    with pytest.raises(ReadOnlyViolation):
        _assert_read_only(cypher)


def test_with_limit_appends():
    assert _with_limit("MATCH (n) RETURN n", 101) == "MATCH (n) RETURN n\nLIMIT 101"


def test_with_limit_strips_trailing_semicolon():
    assert _with_limit("MATCH (n) RETURN n;  ", 101) == "MATCH (n) RETURN n\nLIMIT 101"


def test_with_limit_respects_existing_limit():
    q = "MATCH (n) RETURN n LIMIT 5"
    assert _with_limit(q, 101) == q


def test_with_limit_ignores_limit_inside_string_literal():
    assert _with_limit("RETURN 'no limit here'", 101).endswith("\nLIMIT 101")


def test_with_limit_leaves_union_alone():
    q = "MATCH (a) RETURN a UNION MATCH (b) RETURN b"
    assert _with_limit(q, 101) == q
