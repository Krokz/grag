"""M22 graph commands use the shared owner and init verifies the written entry."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grag import cli
from grag.admin import ServerInfo
from grag.api.main import create_app
from grag.client import GraphClient
from grag.config import GragConfig
from grag.core.errors import ConfigurationError
from grag.onboarding import verify_entry


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAG_EMBED_PROVIDER", "")
    monkeypatch.setenv("GRAG_AUTO_REFRESH_CODE", "0")
    monkeypatch.setattr("grag.admin.find_server", lambda db: None)
    return GragConfig(db_path=tmp_path / "cli.lbdb", buffer_pool_size=128 * 1024 * 1024)


def test_cli_memory_search_context_share_resolver(
    config, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    db = str(config.db_path)
    assert (
        cli.main(
            [
                "--db",
                db,
                "remember",
                "Cache results for ten minutes",
                "--id",
                "cache",
                "--json",
            ]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert saved["node_id"] == "Memory:cache"
    assert cli.main(["--db", db, "search", "Cache", "--json"]) == 0
    assert "Memory:cache" in json.loads(capsys.readouterr().out)["included_node_ids"]
    assert cli.main(["--db", db, "context", "Memory:cache", "--json"]) == 0
    assert "ten minutes" in json.loads(capsys.readouterr().out)["context"]
    assert (
        cli.main(
            [
                "--db",
                db,
                "remember",
                "Changed",
                "--id",
                "cache",
                "--expected-revision",
                "stale",
            ]
        )
        == 1
    )
    assert "revision" in capsys.readouterr().err.lower()


def test_existing_owner_handles_all_cli_graph_commands(
    config, tmp_path, monkeypatch, capsys
):
    app = create_app(config)
    with TestClient(app) as http:
        monkeypatch.setattr(
            "grag.admin.find_server", lambda db: ServerInfo(8471, "0.8.0", True, None)
        )

        class Borrowed:
            def request(self, *args, **kwargs):
                return http.request(*args, **kwargs)

            def close(self):
                pass

        monkeypatch.setattr("grag.client.httpx2.Client", lambda **kw: Borrowed())
        monkeypatch.setattr(
            "grag.service.GragService",
            lambda *a: pytest.fail("CLI attempted a second writer"),
        )
        source = tmp_path / "src"
        source.mkdir()
        (source / "main.py").write_text("def cached():\n    pass\n")
        doc = tmp_path / "guide.md"
        doc.write_text("# Guide\n\nCached context")
        prefix = ["--db", str(config.db_path)]
        for args in (
            ["remember", "Shared harness memory", "--id", "shared"],
            ["search", "Shared"],
            ["context", "Memory:shared"],
            ["ingest-code", str(source)],
            ["ingest", str(doc), "--sections"],
        ):
            assert cli.main(prefix + args) == 0, capsys.readouterr()
        assert app.state.service.engine.execute(
            "MATCH (m:Memory) RETURN m.id"
        ).rows == [["shared"]]


def test_owner_identity_change_never_falls_back_to_local(config, monkeypatch):
    monkeypatch.setattr(
        "grag.admin.find_server", lambda db: ServerInfo(8471, "0.8.0", True, None)
    )

    class Wrong:
        def request(self, *args, **kwargs):
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "json": lambda self: {"status": "ok", "server_id": "someone-else"},
                },
            )()

        def close(self):
            pass

    monkeypatch.setattr("grag.client.httpx2.Client", lambda **kw: Wrong())
    monkeypatch.setattr(
        "grag.service.GragService", lambda *a: pytest.fail("unsafe local fallback")
    )
    with (
        pytest.raises(ConfigurationError, match="identity changed"),
        GraphClient(config),
    ):
        pass


def test_unregistered_owner_error_is_actionable(config, monkeypatch):
    def busy(*args):
        raise RuntimeError("IO exception: Could not set lock on file")

    monkeypatch.setattr("grag.service.GragService", busy)
    with (
        pytest.raises(ConfigurationError, match="owned by another process") as caught,
        GraphClient(config),
    ):
        pass
    assert "direct stdio" in caught.value.hint


def test_init_verifies_written_registration_and_reports_failure(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr("grag.project._fastembed_available", lambda: False)
    seen = []

    def verify(paths, cwd):
        seen.append(json.loads(paths[0].read_text())["mcpServers"]["grag"])
        raise ConfigurationError("actual database cannot open")

    monkeypatch.setattr("grag.onboarding.verify_registrations", verify)
    assert cli.main(["init", "--client", "claude", "--no-skill", "--no-claude-md"]) == 1
    assert seen and "--db" in seen[0]["args"]
    assert "actual database cannot open" in capsys.readouterr().err
    assert (
        cli.main(
            [
                "init",
                "--client",
                "claude",
                "--no-skill",
                "--no-claude-md",
                "--no-verify",
            ]
        )
        == 0
    )
    assert "unverified" in capsys.readouterr().out
    assert len(seen) == 1


def test_actual_stdio_registration_initializes_and_writes_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAG_EMBED_PROVIDER", "")
    monkeypatch.setenv("GRAG_BUFFER_POOL_MB", "128")
    entry = {
        "command": sys.executable,
        "args": ["-m", "grag.cli", "--db", str(tmp_path / "mcp.lbdb"), "mcp"],
    }
    result = asyncio.run(verify_entry(entry, tmp_path))
    assert result["tools"] == 10 and result["write_read"] == "verified"


def test_old_owner_rejects_new_ingestion_without_local_fallback(config, monkeypatch):
    from grag.config import database_identity
    from grag.core.types import CodeIngestRequest

    monkeypatch.setattr(
        "grag.admin.find_server", lambda db: ServerInfo(8471, "0.8.0", True, None)
    )
    calls = []

    class OldOwner:
        def request(self, method, path, **kwargs):
            calls.append(path)
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "json": lambda self: {
                        "status": "ok",
                        "server_id": database_identity(config.db_path),
                    },
                },
            )()

        def close(self):
            pass

    monkeypatch.setattr("grag.client.httpx2.Client", lambda **kw: OldOwner())
    with (
        GraphClient(config) as client,
        pytest.raises(ConfigurationError, match="scope policy"),
    ):
        client.call(
            "ingest_code",
            CodeIngestRequest(paths=[], root="/unused", replace_scope=True),
        )
    assert all(path == "/api/health" for path in calls)


def test_explicit_database_overrides_remote_environment(config, monkeypatch, capsys):
    monkeypatch.setenv("GRAG_SERVER_URL", "https://unrelated.example.test")
    assert (
        cli.main(
            ["--db", str(config.db_path), "remember", "Local only", "--id", "local"]
        )
        == 0
    )
    assert str(config.db_path) in capsys.readouterr().out


def test_local_url_registration_preserves_owner_path_and_references_token(
    config, monkeypatch
):
    from grag.project import _mcp_entry

    monkeypatch.setenv("GRAG_API_TOKEN", "test-value")
    monkeypatch.setattr(
        "grag.admin.find_server",
        lambda db: ServerInfo(
            45678,
            "0.8.0",
            True,
            None,
            mcp_enabled=True,
            mcp_path="/agent/tools",
            host="::1",
        ),
    )
    entry = _mcp_entry(config.db_path, False, 45678, None, None)
    assert entry["url"] == "http://[::1]:45678/agent/tools/"
    assert entry["headers"] == {"Authorization": "Bearer ${GRAG_API_TOKEN}"}
    assert "test-value" not in json.dumps(entry)
