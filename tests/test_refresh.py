"""Auto-refresh: the serving process re-ingests a checkout when git state moves."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from grag.config import GragConfig
from grag.core.types import CodeIngestRequest, SearchRequest
from grag.refresh import CodeIndexRefresher, fingerprint
from grag.service import GragService


def _git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed git argv over a tmp path
        ["git", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "core.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture()
def service(tmp_path: Path) -> GragService:
    svc = GragService(GragConfig(db_path=tmp_path / "refresh.lbdb", auto_refresh_interval_s=1.0))
    svc.enable_auto_refresh()
    yield svc
    svc.close()


def _functions(svc: GragService) -> set[str]:
    return {r[0] for r in svc.engine.execute("MATCH (f:Function) RETURN f.name").rows}


def _wait_fresh(svc: GragService, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        svc.refresher.interval = 0.0  # type: ignore[union-attr]
        if svc.refresher.maybe_refresh() is None:  # type: ignore[union-attr]
            return
        time.sleep(0.05)
    raise AssertionError("refresh never completed")


def test_fingerprint_tracks_head_and_dirty_files(repo: Path):
    clean = fingerprint(repo)
    assert clean is not None and clean.head
    (repo / "core.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    dirty = fingerprint(repo)
    assert dirty.head == clean.head and dirty.tree != clean.tree
    _git(repo, "commit", "-qam", "edit")
    committed = fingerprint(repo)
    assert committed.head != clean.head
    assert fingerprint(repo.parent) is None  # not a checkout


def test_search_triggers_reingest_after_commit(service: GragService, repo: Path):
    service.ingest_code(CodeIngestRequest(paths=[str(repo)]))
    assert _functions(service) == {"alpha"}
    assert service.search_knowledge(SearchRequest(query="alpha", hops=0)).index_status is None

    (repo / "core.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
    )
    _git(repo, "commit", "-qam", "add beta")
    service.refresher.interval = 0.0  # type: ignore[union-attr]
    resp = service.search_knowledge(SearchRequest(query="beta", hops=0))
    assert resp.index_status == "refreshing"
    _wait_fresh(service)
    assert _functions(service) == {"alpha", "beta"}
    assert service.refresher.refreshes == 1  # type: ignore[union-attr]


def test_uncommitted_edits_also_refresh(service: GragService, repo: Path):
    service.ingest_code(CodeIngestRequest(paths=[str(repo)]))
    service.refresher.interval = 0.0  # type: ignore[union-attr]
    assert service.refresher.maybe_refresh() is None  # type: ignore[union-attr]
    time.sleep(0.02)
    (repo / "extra.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")
    assert service.refresher.maybe_refresh() == "refreshing"  # type: ignore[union-attr]
    _wait_fresh(service)
    assert "gamma" in _functions(service)


def test_check_is_throttled_and_idempotent(service: GragService, repo: Path, monkeypatch):
    service.ingest_code(CodeIngestRequest(paths=[str(repo)]))
    calls = []
    import grag.refresh as refresh_module

    real = refresh_module.fingerprint
    monkeypatch.setattr(refresh_module, "fingerprint", lambda root: calls.append(root) or real(root))
    service.refresher.interval = 60.0  # type: ignore[union-attr]
    for _ in range(5):
        service.search_knowledge(SearchRequest(query="alpha", hops=0))
    assert len(calls) == 1  # one fingerprint per interval, not per search


def test_stale_recorded_commit_refreshes_on_first_check(tmp_path: Path, repo: Path):
    """A graph indexed by an earlier process (recorded git_commit behind HEAD)
    is refreshed the first time the serving process looks at it."""
    cfg = GragConfig(db_path=tmp_path / "old.lbdb", auto_refresh_interval_s=1.0)
    first = GragService(cfg)
    first.ingest_code(CodeIngestRequest(paths=[str(repo)]))
    first.close()
    (repo / "core.py").write_text("def alpha():\n    return 1\n\n\ndef delta():\n    return 4\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "delta")

    svc = GragService(cfg)
    try:
        svc.enable_auto_refresh()
        svc.refresher.interval = 0.0  # type: ignore[union-attr]
        assert svc.refresher.maybe_refresh() == "refreshing"  # type: ignore[union-attr]
        _wait_fresh(svc)
        assert "delta" in _functions(svc)
    finally:
        svc.close()


def test_non_git_checkout_is_left_alone(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    svc = GragService(GragConfig(db_path=tmp_path / "plain.lbdb"))
    try:
        svc.ingest_code(CodeIngestRequest(paths=[str(plain)]))
        r = CodeIndexRefresher(svc, interval=1.0)
        r.interval = 0.0
        assert r.maybe_refresh() is None
        assert r.status()["tracked"] == 0
    finally:
        svc.close()
