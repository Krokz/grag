"""Tests for grag.registry — per-process ServiceRegistry for multi-db support."""

from __future__ import annotations

import pytest

from grag.config import GragConfig
from grag.core.errors import ConfigurationError, NotFoundError
from grag.core.types import DefineSchemaRequest, NodeTableSpec
from grag.registry import ServiceRegistry
from grag.service import GragService

TEST_BUFFER_POOL = 128 * 1024 * 1024


def _doc_spec() -> NodeTableSpec:
    return NodeTableSpec(name="Doc", primary_key="id")


def _make_db(db_dir, name: str) -> None:
    svc = GragService(
        GragConfig(db_path=db_dir / f"{name}.lbdb", buffer_pool_size=TEST_BUFFER_POOL)
    )
    try:
        svc.define_schema(DefineSchemaRequest(node_tables=[_doc_spec()]))
    finally:
        svc.close()


@pytest.fixture()
def registry(tmp_path):
    db_dir = tmp_path / "dbs"
    db_dir.mkdir()
    _make_db(db_dir, "project-a")
    _make_db(db_dir, "project-b")
    reg = ServiceRegistry(GragConfig(db_dir=db_dir, buffer_pool_size=TEST_BUFFER_POOL))
    yield reg
    reg.close()


def test_get_same_name_returns_cached_service(registry):
    svc = registry.get("project-a")
    assert registry.get("project-a") is svc


def test_get_different_names_bind_different_files(registry):
    a = registry.get("project-a")
    b = registry.get("project-b")
    assert a is not b
    assert a.config.db_path.name == "project-a.lbdb"
    assert b.config.db_path.name == "project-b.lbdb"


def test_get_rejects_path_escape(registry):
    with pytest.raises(ConfigurationError):
        registry.get("../etc")
    with pytest.raises(ConfigurationError):
        registry.get("/abs/path")


def test_get_missing_db_lists_available_in_hint(registry):
    with pytest.raises(NotFoundError) as excinfo:
        registry.get("nope")
    assert "project-a" in excinfo.value.hint
    assert "project-b" in excinfo.value.hint


def test_list_dbs_returns_stems(registry):
    assert registry.list_dbs() == ["project-a", "project-b"]


def test_close_is_idempotent_and_closes_all(registry):
    a = registry.get("project-a")
    b = registry.get("project-b")
    registry.close()
    registry.close()
    # Cache was cleared: a fresh get() rebuilds instead of returning closed instances.
    assert registry.get("project-a") is not a
    assert registry.get("project-b") is not b


def test_single_db_mode_serves_db_path(tmp_path):
    db_path = tmp_path / "single.lbdb"
    reg = ServiceRegistry(
        GragConfig(db_path=db_path, buffer_pool_size=TEST_BUFFER_POOL)
    )
    try:
        svc = reg.get()
        assert svc is reg.get()
        assert svc.config.db_path == db_path.resolve()
        assert reg.list_dbs() == []
        with pytest.raises(ConfigurationError):
            reg.get("project-a")
    finally:
        reg.close()


def test_multi_db_default_resolution(tmp_path):
    db_dir = tmp_path / "dbs"
    db_dir.mkdir()
    _make_db(db_dir, "only")
    reg = ServiceRegistry(GragConfig(db_dir=db_dir, buffer_pool_size=TEST_BUFFER_POOL))
    try:
        # Exactly one .lbdb present: get() with no db uses it.
        assert reg.get().config.db_path.name == "only.lbdb"
    finally:
        reg.close()

    _make_db(db_dir, "other")
    reg = ServiceRegistry(
        GragConfig(
            db_path="other.lbdb", db_dir=db_dir, buffer_pool_size=TEST_BUFFER_POOL
        )
    )
    try:
        # Two files: default prefers the one named by config.db_path.name.
        assert reg.get().config.db_path.name == "other.lbdb"
    finally:
        reg.close()

    reg = ServiceRegistry(GragConfig(db_dir=db_dir, buffer_pool_size=TEST_BUFFER_POOL))
    try:
        # Two files, no preferred name: ambiguous default.
        with pytest.raises(ConfigurationError):
            reg.get()
    finally:
        reg.close()
