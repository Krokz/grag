"""Readable `grag status`: sections, color only when wanted, problems with fixes."""
from __future__ import annotations

import io
import re
from pathlib import Path

from grag import status_view

HOME = str(Path.home())
DB = f"{HOME}/.grag/platform-bc2146f4-7f41-437b-9505-00e3302162e9.lbdb"


def report(**overrides):
    base = {
        "runtime": {"grag_version": "0.13.0", "python": "/py", "grag_module": "/src/grag/__init__.py"},
        "project": {"root": f"{HOME}/work/platform", "selection_source": "project manifest", "database": DB, "exists": True},
        "server": {"status": "reachable", "host": "127.0.0.1", "port": 43036, "pid": 7, "version": "0.10.1",
                   "mcp_enabled": True, "mcp_path": "/mcp", "database_bytes": 31_246_336, "database_id8": "b1d5e564",
                   "embedder": {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"},
                   "servers": [{"pid": 7, "port": 43036, "host": "127.0.0.1", "db": DB, "version": "0.10.1", "current": True},
                               {"pid": 8, "port": 41007, "host": "127.0.0.1", "db": f"{HOME}/.grag/lumio.lbdb", "version": "0.9.0"},
                               {"pid": 9, "port": 41656, "host": "127.0.0.1", "db": f"{HOME}/.grag/adam.lbdb", "version": "0.13.0"}]},
        "registrations": [
            {"id": "claude:project:grag", "client": "claude", "scope": "project", "path": f"{HOME}/work/platform/.mcp.json",
             "scope_precedence": "highest discovered", "issues": []},
            {"id": "cursor:user:grag", "client": "cursor", "scope": "user", "path": f"{HOME}/.cursor/mcp.json",
             "scope_precedence": "shadowed by cursor:project:grag",
             "issues": ["database_mismatch: this registration selects a different database than this CLI"]},
            {"id": "cursor:project:grag", "client": "cursor", "scope": "project", "path": f"{HOME}/work/platform/.cursor/mcp.json",
             "scope_precedence": "highest discovered", "issues": ["database_mismatch: this registration selects a different database than this CLI"]},
        ],
        "client_readiness": "unverified",
        "skills": [{"path": f"{HOME}/work/platform/.claude/skills/grag/SKILL.md", "bundle": "different from this runtime"},
                   {"path": f"{HOME}/.claude/skills/grag/SKILL.md", "bundle": "matches this runtime"}],
        "indexed_roots": {"roots": [{"path": f"{HOME}/work/platform", "error": "Legacy index has no saved scope/options. Run ingest_code once."}]},
        "discovery_limits": "limits text",
    }
    base.update(overrides)
    return base


def test_problems_are_grouped_with_fixes_and_noise_is_hidden():
    text = "\n".join(status_view.render(report()))
    assert "\033[" not in text  # no color unless asked
    assert "~/.grag/platform-bc2146f4….lbdb  (29.8 MB, id b1d5e564)" in text
    assert "the server runs grag 0.10.1; this CLI is 0.13.0" in text and "fix: grag restart" in text
    assert "1 of 1 code root(s) need attention" in text and "Legacy index has no saved scope/options" in text
    assert "(not used here)" in text  # the shadowed registration's mismatch is informational
    assert text.count("[database_mismatch]") == 1  # the active one is a problem
    assert "⚠ 1 differ from this runtime" in text and "fix: grag init" in text
    assert "2 other grag server(s) running  (grag 0.9.0 \u00d71, 0.13.0 \u00d71)" in text and "1 run an older grag" in text
    assert "4 issue(s) above." in text
    assert "lumio.lbdb" not in text and "indexed roots:" not in text and "limits text" not in text


def test_verbose_lists_servers_full_paths_and_raw_state():
    text = "\n".join(status_view.render(report(), verbose=True))
    assert DB.replace(HOME, "~") in text and "lumio.lbdb" in text and "pid 8" in text
    assert "indexed roots:" in text and "limits text" in text and "source /src/grag/__init__.py" in text


def test_healthy_setup_reports_no_issues():
    healthy = report(server={**report()["server"], "version": "0.13.0", "servers": []}, indexed_roots={"roots": []},
                     skills=[{"path": f"{HOME}/.claude/skills/grag/SKILL.md", "bundle": "matches this runtime"}],
                     registrations=[report()["registrations"][0]])
    assert status_view.render(healthy)[-2] == "No issues found."


def test_configuration_errors_are_shown_first():
    broken = report(project={"root": f"{HOME}/w", "error": "GRAG_STATEMENT_TIMEOUT_MS must be positive", "repair": "Unset it."},
                    server={}, indexed_roots="unavailable until the configuration is fixed")
    text = "\n".join(status_view.render(broken))
    assert "configuration error" in text and "GRAG_STATEMENT_TIMEOUT_MS must be positive" in text


def test_color_follows_the_terminal_and_no_color(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert status_view.use_color(Tty()) and not status_view.use_color(io.StringIO())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not status_view.use_color(Tty())
    colored = "\n".join(status_view.render(report(), color=True))
    assert re.search(r"\033\[33m⚠ the server runs grag 0.10.1", colored)


def test_sections_are_separated_by_blank_lines():
    lines = status_view.render(report())
    labelled = [i for i, line in enumerate(lines) if line[:1].isalpha() and not line.startswith(("grag ", "More detail"))
                and line.split()[0] in {"Database", "Server", "Embedder", "Index", "Clients", "Skills", "Others"}]
    assert len(labelled) == 7 and all(lines[i - 1] == "" for i in labelled)
