"""First-session failures: real native probes, cold assets, damaged installs and encodings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from grag import admin, native, readiness
from grag.config import EmbedderConfig, GragConfig
from grag.core.engine import Engine
from grag.core.errors import ConfigurationError, CypherError, GragError


def run_cli(tmp_path, *args, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRAG_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONIOENCODING="cp1252")
    env.update(extra_env or {})
    return subprocess.run(  # noqa: S603 — actual installed CLI with fixture arguments
        [sys.executable, "-m", "grag.cli", *args], cwd=tmp_path, env=env,
        capture_output=True, timeout=60, check=False,
    )


def test_real_isolated_native_query():
    result = readiness._probe("engine", GragConfig(), prepare=False, timeout=20)
    assert result["status"] == "ready", result
    assert "Cypher query passed" in result["detail"]


def test_doctor_survives_unimportable_native_package(tmp_path):
    (tmp_path / "ladybug.py").write_text("raise ImportError('fixture native dependency missing')")
    result = run_cli(tmp_path, "doctor", "--json", extra_env={"PYTHONPATH": str(tmp_path)})
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["ready"] is False
    assert report["checks"][0]["status"] == "unavailable"
    assert "fixture native dependency missing" in report["checks"][0]["detail"]


@pytest.mark.parametrize("behavior", ["crash", "timeout"])
def test_probe_process_failures_are_reports(monkeypatch, behavior):
    original = subprocess.run
    def run_child(command, **kwargs):
        source = "import os; os._exit(91)" if behavior == "crash" else "import time; time.sleep(60)"
        return original([sys.executable, "-c", source], **kwargs)
    monkeypatch.setattr(readiness.subprocess, "run", run_child)
    result = readiness._probe("engine", GragConfig(), prepare=False, timeout=0.5)
    assert result["status"] == "unavailable"
    assert ("code 91" if behavior == "crash" else "timed out") in result["detail"]


def test_doctor_leaves_corrupt_project_and_sidecars_untouched(tmp_path, monkeypatch):
    db = tmp_path / "broken.lbdb"
    before = {}
    for suffix in ("", ".wal", ".shadow", ".ses"):
        path = Path(str(db) + suffix)
        path.write_bytes(b"corrupt fixture " + suffix.encode())
        before[path] = path.read_bytes()
    monkeypatch.setattr(admin, "find_server", lambda *a, **kw: None)
    monkeypatch.setattr(Engine, "__init__", lambda *a, **kw: pytest.fail("doctor opened project DB"))
    checks = [{"status": "ready", "label": "fixture", "detail": "probe"}]
    admin.doctor_lines(GragConfig(db_path=db), checks=checks)
    assert {p: p.read_bytes() for p in before} == before


def test_cold_cache_does_not_install_extensions(tmp_path, monkeypatch):
    calls = []
    def execute(self, cypher, *args):
        calls.append(cypher)
        if cypher.startswith("LOAD EXTENSION"):
            raise CypherError("extension absent")
        return SimpleNamespace(rows=[[42]])
    monkeypatch.setattr(Engine, "execute", execute)
    monkeypatch.setattr(Engine, "execute_write", execute)
    result = readiness._worker({"kind": "fts", "prepare": False})
    assert result["status"] == "unavailable"
    assert "doctor --prepare" in result["detail"]
    assert not any(q.startswith("INSTALL ") for q in calls)


def test_extension_install_failure_retains_first_cause(monkeypatch):
    calls = []
    def execute(self, query):
        calls.append(query)
        raise CypherError("cache permission denied" if query.startswith("INSTALL") else "not cached")
    monkeypatch.setattr(Engine, "execute_write", execute)
    with pytest.raises(GragError, match="cache permission denied") as error:
        Engine.load_extension(Engine.__new__(Engine), "FTS")
    assert calls == ["LOAD EXTENSION FTS", "INSTALL FTS"]
    assert "~/.lbdb/extension" in error.value.hint


def test_cached_extension_never_installs(monkeypatch):
    calls = []
    monkeypatch.setattr(Engine, "execute_write", lambda self, query: calls.append(query))
    Engine.load_extension(Engine.__new__(Engine), "FTS")
    assert calls == ["LOAD EXTENSION FTS"]


def test_offline_grammar_uses_load_only_registry(monkeypatch):
    if not readiness._installed("tree_sitter"):
        pytest.skip("optional code extra")
    calls = []
    class Registry:
        def get_language(self, name):
            calls.append(name)
            raise RuntimeError("damaged cached grammar")
    fake = SimpleNamespace(cache_dir=lambda: "/cache-fixture", LanguageRegistry=SimpleNamespace(new=Registry),
                           get_parser=lambda *a: pytest.fail("auto-download API called"))
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", fake)
    monkeypatch.setenv("TREE_SITTER_LANGUAGE_PACK_LIBS_DIR", "before")
    result = readiness._worker({"kind": "grammar:.rs", "prepare": False})
    assert result["status"] == "unavailable"
    assert "damaged cached grammar" in result["detail"]
    assert calls == ["rust"]
    assert os.environ["TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"] == "before"


def test_preparation_downloads_one_grammar_batch_then_probes_offline(monkeypatch):
    batches = []
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", SimpleNamespace(download=batches.append))
    result = readiness._worker({"kind": "grammar_assets", "prepare": True})
    assert result["status"] == "ready"
    assert batches == [["bash", "c", "cpp", "java", "kotlin", "lua", "php", "ruby", "rust", "scala", "sql", "swift"]]
    calls = []
    def probe(kind, config, **kwargs):
        calls.append((kind, kwargs["prepare"]))
        return {"status": "ready", "detail": "fixture"}
    monkeypatch.setattr(readiness, "_probe", probe)
    monkeypatch.setattr(readiness, "_installed", lambda name: True)
    readiness.check_install(GragConfig(), prepare=True)
    assert calls.count(("grammar_assets", True)) == 1
    assert all(not prepare for kind, prepare in calls if kind.startswith("grammar:"))


@pytest.mark.parametrize("offline", [False, True])
def test_model_loading_reports_cache_and_download_failure(monkeypatch, caplog, offline):
    from grag.retrieval.vectors import FastembedEmbedder

    calls = []
    def model(**kwargs):
        calls.append(kwargs.get("local_files_only", False))
        raise RuntimeError("incomplete cache" if kwargs.get("local_files_only") else "permission denied")
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=model))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    with pytest.raises(ConfigurationError) as error:
        FastembedEmbedder(EmbedderConfig(provider="fastembed"), local_files_only=offline)
    assert calls == ([True] if offline else [True, False])
    assert "FASTEMBED_CACHE_PATH" in error.value.hint
    assert ("not usable offline" if offline else "permission denied") in error.value.message
    assert ("Preparing local embedding model" in caplog.text) is not offline


def test_model_readiness_requires_valid_inference(monkeypatch):
    from grag.retrieval import vectors

    class Model:
        def __init__(self, config, **kwargs):
            assert kwargs["local_files_only"] is True
        def embed(self, texts):
            return [[float("nan"), 0.0]]
    monkeypatch.setattr(vectors, "FastembedEmbedder", Model)
    result = readiness._worker({"kind": "model", "prepare": False,
                                "embedder": {"provider": "fastembed", "dim": 2}})
    assert result["status"] == "unavailable"
    assert "model output" in result["detail"]


def test_remote_doctor_does_not_contact_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(readiness, "_installed", lambda name: False)
    def probe(kind, *args, **kwargs):
        calls.append(kind)
        return {"status": "ready", "detail": "fixture"}
    monkeypatch.setattr(readiness, "_probe", probe)
    checks = readiness.check_install(GragConfig(embedder=EmbedderConfig(provider="remote", base_url="https://example.invalid")), prepare=True)
    assert "model" not in calls
    assert next(c for c in checks if c["key"] == "model")["status"] == "unverified"


def test_legacy_encoding_init_and_errors(tmp_path):
    project = tmp_path / "project-中文-🌍"
    project.mkdir()
    before = list(project.iterdir())
    result = run_cli(project, "init", "--client", "claude", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert b"UnicodeEncodeError" not in result.stdout + result.stderr
    assert list(project.iterdir()) == before
    (project / ".mcp.json").write_text("invalid")
    result = run_cli(project, "init", "--client", "claude")
    assert result.returncode == 1
    assert b"UnicodeEncodeError" not in result.stderr


def test_export_uses_utf8_even_with_legacy_output_encoding(tmp_path):
    cfg = GragConfig(db_path=tmp_path / "source.lbdb")
    with Engine(cfg) as engine:
        engine.execute_write("CREATE NODE TABLE Memory(id STRING PRIMARY KEY, body STRING, _source STRING)")
        engine.execute_write("CREATE (:Memory {id:'one',body:$body})", {"body": "中文 🌍"})
    result = run_cli(tmp_path, "--db", str(cfg.db_path), "export")
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
    assert any("中文 🌍" in str(row) for row in rows)


def test_windows_loader_uses_only_owned_dlls_and_keeps_handles(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(native, "_DIRECTORY", tmp_path)
    monkeypatch.setattr(native, "_HANDLES", [])
    calls = []
    for name in native._DLLS:
        (tmp_path / name).write_bytes(b"fixture")
    def load(path, **kwargs):
        calls.append((path, kwargs))
        return object()
    monkeypatch.setattr(native.ctypes, "WinDLL", load, raising=False)
    native.prepare_native_runtime()
    native.prepare_native_runtime()
    assert [Path(path) for path, _ in calls] == [tmp_path / name for name in native._DLLS]
    assert all(options == {"winmode": 0x900} for _, options in calls)
    assert len(native._HANDLES) == 2


def test_windows_missing_owned_runtime_has_reinstall_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(native, "_DIRECTORY", tmp_path)
    monkeypatch.setattr(native, "_HANDLES", [])
    with pytest.raises(ConfigurationError, match="Missing bundled runtime") as error:
        native.prepare_native_runtime()
    assert "Windows x64 wheel" in error.value.hint


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_doctor_rejects_invalid_timeout(tmp_path, value):
    result = run_cli(tmp_path, "doctor", "--timeout", value)
    assert result.returncode == 2
    assert b"finite positive" in result.stderr
