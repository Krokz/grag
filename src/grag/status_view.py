"""Readable `grag status`: grouped sections, color on terminals, and problems with fixes.

The JSON report (`grag status --json`) stays the machine interface; this is only its
human rendering. Color is used only on a terminal and honours NO_COLOR/FORCE_COLOR.
`--verbose` adds every server, full paths, runtime details and the raw index state.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, TextIO

_CODES = {"ok": "32", "warn": "33", "bad": "31", "dim": "2", "bold": "1", "accent": "36"}
_UUID = re.compile(r"-([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LABEL = 10


def use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


class _Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, kind: str, text: str) -> str:
        return f"\033[{_CODES[kind]}m{text}\033[0m" if self.enabled else text


def short(path: Any, *, verbose: bool = False) -> str:
    """Home as ~, and UUID-bearing database names cut to their first block."""
    text = str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + os.sep):
        text = "~" + text[len(home):]
    return text if verbose else _UUID.sub(r"-\1…", text)


def _version_key(value: str) -> tuple:
    return tuple(int(part) if part.isdigit() else -1 for part in re.split(r"[.+-]", value)) if value != "?" else (-1,)


def _size(n: int | None) -> str:
    if n is None:
        return ""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def render(report: dict, *, color: bool = False, verbose: bool = False) -> list[str]:
    st = _Style(color)
    out: list[str] = []
    problems = 0

    def row(label: str, text: str) -> None:
        out.append(st("bold", label.ljust(LABEL)) + text)

    def more(text: str) -> None:
        out.append(" " * LABEL + text)

    def warn(text: str, fix: str | None = None) -> None:
        nonlocal problems
        problems += 1
        more(st("warn", "⚠ " + text))
        if fix:
            more("  " + st("dim", "fix: ") + st("accent", fix))

    runtime, project = report["runtime"], report["project"]
    server = report.get("server", {})
    version = runtime.get("grag_version", "?")
    out.append(f"{st('bold', 'grag ' + version)}  {st('dim', 'checkout')} {short(project.get('root', '?'), verbose=verbose)}")
    if verbose:
        out.append(st("dim", f"  python {runtime.get('python')}  ·  source {runtime.get('grag_module')}"))
    out.append("")

    # --- database and server -----------------------------------------------------
    if "error" in project:
        problems += 1
        row("Database", st("bad", "✗ configuration error"))
        more(str(project["error"]))
        if project.get("repair"):
            more(st("accent", str(project["repair"])))
    else:
        details = ", ".join(x for x in (_size(server.get("database_bytes")),
                                        f"id {server['database_id8']}" if server.get("database_id8") else "") if x)
        row("Database", short(project["database"], verbose=verbose) + (st("dim", f"  ({details})") if details else ""))
        more(st("dim", f"selected by {project.get('selection_source', '?')}"))
        if not project.get("exists", True):
            more(st("dim", "not created yet; the first write creates it"))
    if project.get("server_url"):
        row("Server", f"remote {project['server_url']}" + st("dim", "  (not probed)"))
    elif server.get("status") == "reachable":
        origin = f"http://{server.get('host')}:{server.get('port')}/"
        mcp = f"mcp {server.get('mcp_path') or '/mcp'}" if server.get("mcp_enabled") else "mcp off"
        row("Server", st("ok", "● running ") + origin + st("dim", f"  pid {server.get('pid')} · grag {server.get('version') or '?'} · {mcp}"))
        if server.get("version") and server["version"] != version:
            warn(f"the server runs grag {server['version']}; this CLI is {version}", "grag restart")
    elif "error" not in project:
        row("Server", st("dim", "○ not running"))
        more(st("dim", "starts on demand from a registered client, or: ") + st("accent", "grag start"))
    if verbose and server.get("log_path"):
        more(st("dim", f"log {short(server['log_path'], verbose=True)}"))
    embedder = server.get("embedder")
    row("Embedder", f"{embedder['provider']} ({embedder['model']})" if embedder
        else st("dim", "off: BM25 only (set GRAG_EMBED_PROVIDER for local embeddings)"))

    indexed = report.get("indexed_roots")
    roots = indexed.get("roots") or [] if isinstance(indexed, dict) else []
    if isinstance(indexed, str) and indexed and "error" not in project:
        row("Index", st("dim", indexed))
    if roots:
        bad_roots = [r for r in roots if r.get("error")]
        if bad_roots:
            row("Index", st("warn", f"⚠ {len(bad_roots)} of {len(roots)} code root(s) need attention"))
            problems += 1
            for r in bad_roots:
                message = str(r["error"]).split(". ")[0].rstrip(".")
                more(st("warn", f"{short(r.get('path'), verbose=verbose)}: {message}"))
            more("  " + st("dim", "fix: ") + st("accent", "run ingest_code (or grag ingest-code <path>) once for each root"))
        else:
            row("Index", st("ok", f"✓ {len(roots)} code root(s) indexed"))
    out.append("")

    # --- clients and skills ------------------------------------------------------
    registrations = report.get("registrations", [])
    if not registrations:
        row("Clients", st("dim", "no grag registrations found in the known client files"))
    for i, record in enumerate(registrations):
        issues = record.get("issues") or []
        precedence = record.get("scope_precedence", "")
        shadowed = "shadowed" in precedence
        mark = st("dim", "·") if shadowed else st("warn", "⚠") if issues else st("ok", "✓")
        name = f"{record.get('client')} ({record.get('scope')})"
        text = f"{mark} {name.ljust(18)} {short(record.get('path', ''), verbose=verbose)}"
        if shadowed:
            text += st("dim", f"  {precedence}")
        (row if i == 0 else lambda _label, t: more(t))("Clients", text)
        for issue in issues:
            code, _, message = str(issue).partition(": ")
            if shadowed:  # another registration takes precedence here, so this one is informational
                more("  " + st("dim", f"{message or code} (not used here)"))
                continue
            problems += 1
            more("  " + st("warn", message or code) + (st("dim", f"  [{code}]") if message else ""))
        if verbose:
            more("  " + st("dim", f"launcher {record.get('resolved_command') or record.get('url') or 'unavailable'}; "
                                  f"database {short(record.get('database') or 'unverified', verbose=True)}"))
    if registrations and report.get("client_readiness") == "unverified":
        more(st("dim", "connections not verified; check one with: ") + st("accent", "grag doctor --verify-client CLIENT:SCOPE:grag"))

    skills = report.get("skills", [])
    groups: dict[str, list[dict]] = {"match": [], "differ": [], "other": []}
    for skill in skills:
        state = str(skill.get("bundle", skill.get("error", "")))
        groups["match" if "matches" in state else "differ" if "different" in state else "other"].append(skill)
    if skills:
        lines_before = len(out)
        if groups["match"]:
            row("Skills", st("ok", f"✓ {len(groups['match'])} match this runtime"))
            if verbose:
                for s in groups["match"]:
                    more("  " + st("dim", short(s["path"], verbose=True)))
        if groups["differ"]:
            text = st("warn", f"⚠ {len(groups['differ'])} differ from this runtime")
            (row if len(out) == lines_before else lambda _label, t: more(t))("Skills", text)
            problems += 1
            for s in groups["differ"]:
                more("  " + st("warn", short(s["path"], verbose=verbose)))
            project_level = any(not str(s["path"]).startswith(str(Path.home() / ".")) for s in groups["differ"])
            more("  " + st("dim", "fix: ") + st("accent", "grag init" if project_level else "grag init --global-skill"))
        for s in groups["other"]:
            text = st("warn", f"⚠ {short(s['path'], verbose=verbose)}: {s.get('bundle', s.get('error', 'unreadable'))}")
            (row if len(out) == lines_before else lambda _label, t: more(t))("Skills", text)
            problems += 1
    out.append("")

    # --- other servers -------------------------------------------------------------
    others = [s for s in server.get("servers", []) if not s.get("current")]
    if others:
        versions = Counter(s.get("version") or "?" for s in others)
        tally = ", ".join(f"{v} \u00d7{n}" for v, n in sorted(versions.items(), key=lambda kv: _version_key(kv[0])))
        older = sum(1 for s in others if s.get("version") and _version_key(s["version"]) < _version_key(version))
        row("Others", f"{len(others)} other grag server(s) running" + st("dim", f"  (grag {tally})"))
        if older:
            more(st("dim", f"{older} run an older grag; restart each from its checkout to update"))
        if verbose:
            for s in server.get("servers", []):
                marker = st("accent", "*") if s.get("current") else " "
                where = f"http://{s.get('host')}:{s.get('port')}/" if s.get("port") else "port ?"
                more(f"{marker} pid {s.get('pid')}  {where}  " + st("dim", f"grag {s.get('version') or '?'} since {s.get('started_at') or '?'}"))
                more("    " + st("dim", short(s.get("db") or "?", verbose=True)))
        else:
            more(st("dim", "list them: ") + st("accent", "grag status --verbose"))
        out.append("")

    if verbose:
        out.append(st("dim", "indexed roots: " + json.dumps(report.get("indexed_roots"), ensure_ascii=True)))
        if server.get("observed"):
            owner = server["observed"]["runtime"]
            out.append(st("dim", f"owner source: {owner['grag_module']} | Python {owner['python']}"))
        out.append(st("dim", str(report.get("discovery_limits", ""))))
        out.append("")

    out.append(st("warn", f"{problems} issue(s) above.") if problems else st("ok", "No issues found."))
    if not verbose:
        out.append(st("dim", "More detail: grag status --verbose  ·  machine-readable: grag status --json"))
    return out
