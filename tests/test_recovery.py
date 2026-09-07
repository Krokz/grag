"""Recovery drills use disposable databases and real native WAL replay."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from grag import recovery
from grag.cli import main
from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import ConfigurationError

_POOL = 128 * 1024**2


@pytest.fixture()
def cfg(tmp_path):
    config = GragConfig(db_path=tmp_path / "memories.lbdb", buffer_pool_size=_POOL)
    with Engine(config) as engine:
        engine.execute_write("CREATE NODE TABLE Memory(id STRING PRIMARY KEY, text STRING)")
        engine.execute_write("CREATE REL TABLE SUPPORTS(FROM Memory TO Memory)")
        engine.execute_write("CREATE (:Memory {id: 'base', text: 'checkpointed decision'})")
        engine.execute_write("CREATE (:Memory {id: 'other', text: 'supporting evidence'})")
        engine.execute_write(
            "MATCH (a:Memory {id: 'base'}), (b:Memory {id: 'other'}) CREATE (a)-[:SUPPORTS]->(b)"
        )
    return config


def _child(script, *args):
    return subprocess.run(  # noqa: S603 — fixed fixture scripts and argv paths
        [sys.executable, "-c", script, *map(str, args)],
        capture_output=True, text=True, timeout=25, check=False,
    )


def _leave_wal(cfg, *, uncommitted=False):
    script = """
import os, sys
from grag.config import GragConfig
from grag.core.engine import Engine
e = Engine(GragConfig(db_path=sys.argv[1], buffer_pool_size=128*1024**2))
e.execute_write("CREATE (:Memory {id: 'late', text: 'late committed decision'})")
e.execute_write("MATCH (a:Memory {id: 'late'}), (b:Memory {id: 'base'}) CREATE (a)-[:SUPPORTS]->(b)")
if sys.argv[2] == 'yes':
    e.execute_write('BEGIN TRANSACTION')
    e.execute_write("CREATE (:Memory {id: 'unfinished', text: 'must not survive'})")
os._exit(73)
"""
    result = _child(script, cfg.db_path, "yes" if uncommitted else "no")
    assert result.returncode == 73, result.stderr
    assert Path(f"{cfg.db_path}.wal").stat().st_size > 0


def _corrupt_wal(cfg):
    _leave_wal(cfg)
    with Path(f"{cfg.db_path}.wal").open("ab") as stream:
        stream.write(b"\xff" * 16)  # invalid record after committed transactions


def _files(cfg):
    return {
        suffix: Path(f"{cfg.db_path}{suffix}").read_bytes()
        for suffix in recovery._SUFFIXES if Path(f"{cfg.db_path}{suffix}").exists()
    }


def _check_snapshot(bundle, expected):
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["files"].keys() == expected.keys()
    for suffix, data in expected.items():
        assert (bundle / "original" / f"database.lbdb{suffix}").read_bytes() == data
        assert manifest["files"][suffix] == {
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        }
    return manifest


def _contents(path):
    with Engine(GragConfig(db_path=path, buffer_pool_size=_POOL)) as engine:
        return engine.execute("MATCH (n:Memory) RETURN n.id, n.text ORDER BY n.id").rows


@pytest.mark.parametrize("uncommitted", [False, True])
def test_strict_recovery_replays_committed_wal_and_rolls_back_unfinished_work(cfg, tmp_path, uncommitted):
    _leave_wal(cfg, uncommitted=uncommitted)
    before = _files(cfg)
    bundle = tmp_path / "recovery bundle"
    report = recovery.recover_database(cfg, out_dir=bundle)
    assert report["status"] == "verified"
    assert report["data_loss_possible"] is False
    assert report["tables"]["Memory"]["rows"] == 3
    assert report["tables"]["SUPPORTS"]["rows"] == 2
    assert _files(cfg) == before
    assert _check_snapshot(bundle, before) == report
    assert _contents(report["recovered_db"]) == [
        ["base", "checkpointed decision"], ["late", "late committed decision"],
        ["other", "supporting evidence"],
    ]


def test_strict_failure_preserves_every_original_byte(cfg, tmp_path):
    _corrupt_wal(cfg)
    before = _files(cfg)
    bundle = tmp_path / "failed"
    with pytest.raises(recovery.RecoveryError, match="allow-data-loss"):
        recovery.recover_database(cfg, out_dir=bundle)
    assert _files(cfg) == before
    manifest = _check_snapshot(bundle, before)
    assert manifest["status"] == "failed"
    assert "recovered_db" not in manifest
    assert not (bundle / "partial").exists()


@pytest.mark.parametrize("damage", ["after_commits", "before_commits"])
def test_explicit_partial_replay_uses_fresh_copy_and_records_uncertain_loss(cfg, tmp_path, damage):
    _corrupt_wal(cfg)
    if damage == "before_commits":
        wal = Path(f"{cfg.db_path}.wal")
        wal.write_bytes(b"\xff" + wal.read_bytes()[1:])
    before = _files(cfg)
    bundle = tmp_path / "partial"
    report = recovery.recover_database(cfg, out_dir=bundle, allow_data_loss=True)
    assert report["status"] == "verified"
    assert report["data_loss_possible"] is True
    assert report["strict_error"]
    assert Path(report["recovered_db"]).parent.name == "partial"
    assert (bundle / "strict" / "recovered.lbdb").exists()  # retained failed attempt
    assert _files(cfg) == before
    _check_snapshot(bundle, before)
    expected = ["base", "late", "other"] if damage == "after_commits" else ["base", "other"]
    assert [key for key, _ in _contents(report["recovered_db"])] == expected


def test_snapshot_can_be_restored_independently(cfg, tmp_path):
    _leave_wal(cfg)
    report = recovery.recover_database(cfg, out_dir=tmp_path / "first")
    # Exercise raw snapshot restoration, independently of the returned DB.
    restored = recovery._restore_snapshot(Path(report["bundle"]), report["files"], "restore-drill")
    assert [key for key, _ in _contents(restored)] == ["base", "late", "other"]


def test_live_native_writer_is_refused_without_releasing_its_lock(cfg, tmp_path):
    with Engine(cfg) as engine:
        with pytest.raises(recovery.RecoveryError, match="Stop every server"):
            recovery.recover_database(cfg, out_dir=tmp_path / "busy")
        # The snapshot child must not release the calling process's native
        # lock. A separate native open is still excluded after the refusal.
        probe = _child("""
import sys
from grag.config import GragConfig
from grag.core.engine import Engine
Engine(GragConfig(db_path=sys.argv[1], buffer_pool_size=128*1024**2))
""", cfg.db_path)
        assert probe.returncode != 0 and "lock" in probe.stderr.lower()
        engine.execute_write("CREATE (:Memory {id: 'still-writable', text: 'writer intact'})")
    assert len(_contents(cfg.db_path)) == 3


def test_snapshot_lock_excludes_new_native_writer(cfg, tmp_path, monkeypatch):
    original = recovery._copy_fd
    probes = []

    def probe_during_copy(fd, target):
        if not probes:
            probes.append(_child("""
import sys
from grag.config import GragConfig
from grag.core.engine import Engine
Engine(GragConfig(db_path=sys.argv[1], buffer_pool_size=128*1024**2))
""", cfg.db_path))
        return original(fd, target)

    monkeypatch.setattr(recovery, "_copy_fd", probe_during_copy)
    target = tmp_path / "snapshot"
    target.mkdir()
    # No Engine is open in this process; direct use is safe for this drill.
    recovery._snapshot(cfg.db_path, target)
    assert probes[0].returncode != 0 and "lock" in probes[0].stderr.lower()


@pytest.mark.parametrize("auto_recover", [False, True])
def test_normal_open_never_prompts_or_selects_lossy_replay(cfg, monkeypatch, auto_recover):
    import grag.core.engine as module

    calls = []

    def failed_open(*args, **kw):
        calls.append(kw)
        raise RuntimeError("Corrupted wal file. Read out invalid WAL record type.")

    def unexpected_input(*args, **kw):
        pytest.fail("normal database open must not prompt for destructive recovery")

    monkeypatch.setattr(module.lb, "Database", failed_open)
    monkeypatch.setattr("builtins.input", unexpected_input)
    cfg.wal_auto_recover = auto_recover
    with pytest.raises(ConfigurationError, match="recover"):
        Engine(cfg)
    assert len(calls) == 1
    assert calls[0].get("throw_on_wal_replay_failure", True) is True


@pytest.mark.parametrize("error", [
    "Could not set lock on /wal/replay.lbdb", "Permission denied: /wal/replay.lbdb",
    "Checksum failed on /walter/database.lbdb", "Cannot open unrelated file",
])
def test_unrelated_errors_do_not_enable_partial_replay(error):
    assert not recovery.is_replay_error(error)


def test_shadow_and_checkpoint_files_are_preserved_even_when_unusable(cfg, tmp_path):
    for suffix in (".shadow", ".wal.checkpoint", ".checkpoint"):
        Path(f"{cfg.db_path}{suffix}").write_bytes(b"preserve for diagnosis")
    before = _files(cfg)
    bundle = tmp_path / "sidecars"
    # Invalid shadow data may fail open or be ignored by this engine version;
    # preservation must happen before either outcome.
    with suppress(recovery.RecoveryError):
        recovery.recover_database(cfg, out_dir=bundle)
    _check_snapshot(bundle, before)
    assert _files(cfg) == before


def test_changed_source_during_snapshot_is_refused(cfg, tmp_path, monkeypatch):
    original = recovery._copy_fd

    def external_write(fd, target):
        result = original(fd, target)
        Path(f"{cfg.db_path}.wal").write_bytes(b"noncooperating external write")
        return result

    monkeypatch.setattr(recovery, "_copy_fd", external_write)
    target = tmp_path / "snapshot"
    target.mkdir()
    with pytest.raises(recovery.RecoveryError, match="changed during snapshot"):
        recovery._snapshot(cfg.db_path, target)


@pytest.mark.parametrize("failure", ["snapshot", "corrupt_backup", "replay_crash", "timeout"])
def test_failed_stages_never_publish_a_verified_copy(cfg, tmp_path, monkeypatch, failure):
    before = _files(cfg)
    real = recovery._worker
    bundle = tmp_path / "failure"
    calls = []

    def fail(args, **kw):
        calls.append(args[0])
        if failure == "snapshot":
            return {"ok": False, "error": "No space left on device"}
        if args[0] == "replay":
            if failure == "timeout":
                raise recovery.RecoveryError("Recovery worker timed out")
            return {"ok": False, "error": "Recovery worker exited with code -11"}
        report = real(args, **kw)
        if failure == "corrupt_backup":
            (bundle / "original" / "database.lbdb").write_bytes(b"damaged snapshot")
        return report

    monkeypatch.setattr(recovery, "_worker", fail)
    with pytest.raises(recovery.RecoveryError):
        recovery.recover_database(cfg, out_dir=bundle, allow_data_loss=True)
    assert _files(cfg) == before
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert "recovered_db" not in manifest
    assert not (bundle / "partial").exists()  # native crash is not permission to skip WAL
    if failure in {"snapshot", "corrupt_backup"}:
        assert calls == ["snapshot"]


def test_actual_worker_timeout_is_bounded():
    with pytest.raises(recovery.RecoveryError, match="timed out"):
        recovery._worker(["snapshot", "unused", "unused"], timeout=0.0001)


def test_disk_failure_during_copy_leaves_source_intact(cfg, tmp_path, monkeypatch):
    before = _files(cfg)
    target = tmp_path / "snapshot"
    target.mkdir()

    def full(fd):
        raise OSError("No space left on device")

    monkeypatch.setattr(recovery.os, "fsync", full)
    with pytest.raises(OSError, match="No space"):
        recovery._snapshot(cfg.db_path, target)
    assert _files(cfg) == before


def test_existing_output_is_never_reused(cfg, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep"
    marker.write_text("unrelated")
    with pytest.raises(FileExistsError):
        recovery.recover_database(cfg, out_dir=output)
    assert list(output.iterdir()) == [marker]
    assert marker.read_text() == "unrelated"


def test_missing_input_is_not_created(tmp_path):
    missing = tmp_path / "missing.lbdb"
    with pytest.raises(recovery.RecoveryError, match="does not exist"):
        recovery.recover_database(GragConfig(db_path=missing))
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_recovery_bundle_files_are_private(cfg, tmp_path):
    _leave_wal(cfg)
    report = recovery.recover_database(cfg, out_dir=tmp_path / "private")
    bundle = Path(report["bundle"])
    for path in [bundle, *bundle.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs special permissions on Windows")
def test_symlink_sidecar_is_refused(cfg, tmp_path):
    unrelated = tmp_path / "private-file"
    unrelated.write_text("unrelated data")
    Path(f"{cfg.db_path}.wal").symlink_to(unrelated)
    with pytest.raises(recovery.RecoveryError, match="not links"):
        recovery.recover_database(cfg, out_dir=tmp_path / "links")
    assert unrelated.read_text() == "unrelated data"


def test_cli_requires_explicit_database(capsys):
    assert main(["recover"]) == 1
    assert "explicit --db" in capsys.readouterr().err


def test_cli_reports_verified_output_and_preserves_original(cfg, tmp_path, capsys):
    before = _files(cfg)
    output = tmp_path / "cli"
    assert main(["--db", str(cfg.db_path), "recover", "--out-dir", str(output)]) == 0
    text = capsys.readouterr().out
    assert "Verified recovered copy" in text
    assert "Original database remains" in text
    assert _files(cfg) == before


def test_cli_failed_recovery_reports_loss_option_without_claiming_success(cfg, tmp_path, capsys):
    _corrupt_wal(cfg)
    assert main(["--db", str(cfg.db_path), "recover", "--out-dir", str(tmp_path / "cli")]) == 1
    result = capsys.readouterr()
    assert "--allow-data-loss" in result.err
    assert "Verified recovered copy" not in result.out


def test_recovered_indexed_graph_can_search_and_accept_new_vectors(cfg, tmp_path):
    with Engine(cfg) as engine:
        engine.load_extension("FTS")
        engine.load_extension("VECTOR")
        engine.execute_write("ALTER TABLE Memory ADD embedding FLOAT[2]")
        engine.execute_write("MATCH (n:Memory) SET n.embedding = [1.0, 0.0]")
        engine.execute_write("CALL CREATE_FTS_INDEX('Memory', 'grag_fts__Memory', ['text'])")
        engine.execute_write(
            "CALL CREATE_VECTOR_INDEX('Memory', 'grag_vec__Memory', 'embedding', metric := 'cosine')"
        )
    _corrupt_wal(cfg)
    report = recovery.recover_database(cfg, out_dir=tmp_path / "indexed", allow_data_loss=True)
    # Contain a possible native index-maintenance crash in the test as well.
    result = _child("""
import sys
from grag.config import GragConfig
from grag.core.engine import Engine
with Engine(GragConfig(db_path=sys.argv[1], buffer_pool_size=128*1024**2)) as e:
    rows = e.execute("CALL QUERY_FTS_INDEX('Memory', 'grag_fts__Memory', 'decision', TOP := 10) RETURN node").rows
    assert {row[0]['id'] for row in rows} == {'base', 'late'}
    e.execute_write("MATCH (n:Memory) SET n.embedding = [1.0, 0.0]")
    e.execute_write("CALL CREATE_VECTOR_INDEX('Memory', 'grag_vec__Memory', 'embedding', metric := 'cosine')")
    e.execute_write("MATCH (n:Memory {id: 'base'}) SET n.embedding = [0.0, 1.0]")
    rows = e.execute("CALL QUERY_VECTOR_INDEX('Memory', 'grag_vec__Memory', [0.0, 1.0], 1) RETURN node").rows
    assert rows[0][0]['id'] == 'base'
""", report["recovered_db"])
    assert result.returncode == 0, result.stderr
