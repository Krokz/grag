"""Bounded, non-mutating installation discovery; explicit saved-launcher probes.

No native imports, graph opens, client launches, repair writes or PID cleanup in
collect(). A healthy temporary install probe is never called client readiness.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from grag.config import GragConfig, database_identity
from grag.config_document import read_object

_MAX_FILE = 2 * 1024 * 1024
_SECRET = re.compile(r"token|secret|password|credential|authorization|api.?key", re.IGNORECASE)


def _file(path: Path) -> tuple[str, dict]:
    """Inspect links without rewriting them; never block on a FIFO/device."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        raise ValueError("not a regular file or a link to one")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        target = os.fstat(stream.fileno())
        if not stat.S_ISREG(target.st_mode):
            raise ValueError("not a regular file")
        data = stream.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE:
        raise ValueError("exceeds the 2 MiB diagnostic read limit")
    return data.decode("utf-8-sig"), {
        "path": str(path), "resolved_path": str(path.resolve()),
        "symlink": stat.S_ISLNK(info.st_mode), "hardlinks": target.st_nlink,
    }


def _url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            return "<unsupported URL>"
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        return urlunsplit((parsed.scheme, host + (f":{parsed.port}" if parsed.port else ""), parsed.path, "", ""))
    except ValueError:
        return "<invalid URL>"


def redact(value: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "<redacted>")
    value = re.sub(r"https?://[^\s\"'<>]+", lambda m: _url(m[0]), value)
    value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", value)
    return value


def _public(value, secrets: list[str]):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [_public(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _public(v, secrets) for k, v in value.items()}
    return value


def runtime_identity() -> dict:
    """Identity without importing optional/native modules."""
    import grag

    packages: dict = {}
    for name in ("gragdb", "ladybug", "mcp", "fastembed", "onnxruntime"):
        try:
            dist = importlib.metadata.distribution(name)
            packages[name] = {"version": dist.version, "location": str(dist.locate_file(""))}
        except importlib.metadata.PackageNotFoundError:
            packages[name] = {"version": None}
    modules = {}
    for name in ("ladybug", "onnxruntime"):
        try:
            spec = importlib.util.find_spec(name)
            modules[name] = spec.origin if spec else None
        except (ImportError, ValueError):
            modules[name] = None
    launchers = []
    for folder in os.get_exec_path():
        executable = shutil.which("grag", path=folder)
        if executable and executable not in launchers:
            launchers.append(executable)
    return {"grag_version": grag.__version__, "python": sys.executable,
            "python_version": sys.version.split()[0], "grag_module": grag.__file__,
            "path_launchers": launchers, "packages": packages, "modules": modules}


def _config_locations(root: Path) -> list[tuple[str, str, Path, str]]:
    from grag.project import _claude_config_dir

    home = Path.home()
    claude = _claude_config_dir()
    user = claude / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    return [
        ("claude", "project", root / ".mcp.json", "mcpServers"),
        ("claude", "user", user, "mcpServers"),
        ("claude", "local", user, "projects"),
        ("cursor", "project", root / ".cursor/mcp.json", "mcpServers"),
        ("cursor", "user", home / ".cursor/mcp.json", "mcpServers"),
        ("windsurf", "user", home / ".codeium/windsurf/mcp_config.json", "mcpServers"),
        ("zed", "user", home / ".config/zed/settings.json", "context_servers"),
    ]


def _normalize(entry: dict) -> dict:
    if isinstance(entry.get("command"), dict):
        command = entry["command"]
        return {"command": command.get("path"), "args": command.get("args", []), "env": command.get("env", {})}
    return entry


def _expand(value: str) -> str:
    def replace(match: re.Match) -> str:
        name, default = match[1], match[2]
        if name not in os.environ and default is None:
            raise ValueError(f"missing environment variable {name} in this diagnostic process")
        return os.environ.get(name, default or "")

    return re.sub(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", replace, value)


def _launcher(entry: dict, root: Path) -> tuple[dict, dict]:
    entry = _normalize(entry)
    if entry.get("cwd") is not None:
        raise ValueError("custom launcher cwd is not supported by this verifier; inspect it in the harness")
    env = entry.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ValueError("env must contain string values")
    expanded_env = {k: _expand(v) for k, v in env.items()}
    if "url" in entry:
        url = _expand(entry["url"])
        return {"url": _url(url), "environment_keys": sorted(env), "database": None, "transport": entry.get("type", "unspecified")}, {**entry, "url": url}
    command, args = entry.get("command"), entry.get("args", [])
    if not isinstance(command, str) or not command or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError("command must be a string and args a string array")
    command, args = _expand(command), [_expand(a) for a in args]
    resolved = shutil.which(command, path=expanded_env.get("PATH", os.environ.get("PATH")))
    if any(sep in command for sep in ("/", "\\")):
        resolved = shutil.which(str(root / command))
    # Expose only selectors, never arbitrary launcher arguments or env values.
    selectors = {}
    for index, arg in enumerate(args):
        key, equal, value = arg.partition("=")
        if key in {"--db", "--db-dir", "--server-url", "--server-db", "--port"}:
            if key in selectors:
                raise ValueError(f"duplicate selector {key}")
            value = value if equal else args[index + 1] if index + 1 < len(args) else ""
            if not value or value.startswith("--"):
                raise ValueError(f"missing value for {key}")
            selectors[key] = value
    effective = {**os.environ, **expanded_env}
    db = selectors.get("--db", effective.get("GRAG_DB_PATH"))
    remote = selectors.get("--server-url") or (None if "--db" in selectors else effective.get("GRAG_SERVER_URL"))
    directory = selectors.get("--db-dir") or (None if "--db" in selectors else effective.get("GRAG_DB_DIR"))
    db = str((root / db).resolve()) if db and not remote and not directory else None
    public = {"command": command, "resolved_command": resolved,
              "argument_count": len(args), "selectors": selectors,
              "environment_keys": sorted(env), "database": db,
              "shared_owner": "--auto-serve" in args or bool(remote),
              "transport": "stdio", "database_directory": directory}
    return public, {**entry, "command": resolved or command, "args": args, "env": expanded_env}


def _skills(root: Path) -> list[dict]:
    from grag.project import _claude_config_dir

    home = Path.home()
    canonical = Path(__file__).parent / "assets/skill"

    def digest(directory: Path) -> str:
        result = hashlib.sha256()
        for relative in [Path("SKILL.md"), *sorted(p.relative_to(canonical) for p in (canonical / "references").glob("*.md"))]:
            text, _ = _file(directory / relative)
            result.update(str(relative).encode() + b"\0" + text.encode())
        return result.hexdigest()

    current = digest(canonical)
    locations = [root / folder / "skills/grag" for folder in (".claude", ".cursor", ".agents", ".windsurf")]
    locations += [base / "skills/grag" for base in (_claude_config_dir(), home / ".cursor", home / ".agents", home / ".codeium/windsurf")]
    results = []
    for location in dict.fromkeys(locations):
        if not (location / "SKILL.md").exists() and not (location / "SKILL.md").is_symlink():
            continue
        try:
            _, info = _file(location / "SKILL.md")
            results.append({**info, "bundle": "matches this runtime" if digest(location) == current else "different from this runtime"})
        except (OSError, ValueError) as exc:
            results.append({"path": str(location), "error": str(exc)})
    return results


def _owner(config: GragConfig) -> dict:
    from grag import admin

    target = admin.server_target(config)
    result: dict = {"status_lines": admin.status_lines(config), "log_path": str(admin.log_path(target)),
                    "registration_path": str(admin.pidfile_path(target)), "status": "not reachable"}
    info = admin.find_server(target)
    if info is None:
        return result
    result.update({"status": "reachable", "pid": info.pid, "port": info.port, "host": info.host,
                   "version": info.version, "mcp_enabled": info.mcp_enabled, "mcp_path": info.mcp_path})
    health = admin.probe_health(info.port, info.host)
    if health:
        result["engine"] = health.get("engine")
    # Older owners ignore unknown query parameters: never send check=false to
    # one that could silently run a freshness check instead.
    if not health or health.get("capabilities", {}).get("passive_diagnostics") != 1:
        result["index"] = "unverified; owner lacks passive diagnostics"
        return result
    origin = admin._http_origin(info.host, info.port)
    if not origin:
        return result
    try:
        headers = {"Authorization": f"Bearer {config.api_token}"} if config.api_token else {}
        request = urllib.request.Request(origin + "/api/index/status?check=false", headers=headers)  # noqa: S310 — validated discovered HTTP owner
        with admin._DIRECT_HTTP.open(request, timeout=2) as response:
            data = response.read(_MAX_FILE + 1)
        if len(data) > _MAX_FILE:
            raise ValueError("passive owner response exceeds limit")
        observed = json.loads(data)
        if observed.get("observed_only") is not True or observed.get("database_id") != database_identity(config.db_path):
            raise ValueError("passive owner response did not identify the selected database")
        result["observed"] = observed
    except (OSError, ValueError, urllib.error.URLError) as exc:
        result["index"] = f"unverified: {exc}"
    return result


def collect(args: argparse.Namespace) -> tuple[dict, GragConfig | None, dict]:
    from grag import admin
    from grag.cli import _config
    from grag.project_files import ProjectConfigError
    from grag.project_identity import (
        MANIFEST,
        explicit_database,
        legacy_database,
        project_root,
    )

    root = project_root()
    secrets = [v for k, v in os.environ.items() if _SECRET.search(k)]
    report: dict = {"runtime": runtime_identity(), "project": {"root": str(root)},
                    "configurations": [], "registrations": [], "skills": _skills(root),
                    "client_readiness": "unverified", "indexed_roots": "unverified; use --verify-client",
                    "setup_provenance": "unknown; no installation actor is recorded by the manifest"}
    manifest = root / MANIFEST
    try:
        text, info = _file(manifest)
        raw = read_object(text)
        report["project"]["manifest"] = {**info, **{k: raw.get(k) for k in ("version", "root", "db_path", "checkout_id", "port")}}
    except FileNotFoundError:
        report["project"]["manifest"] = None
    except (OSError, ValueError) as exc:
        report["project"]["manifest"] = {"path": str(manifest), "error": str(exc)}
    cfg = None
    try:
        if not explicit_database(args):
            # Apply diagnostic read bounds before the normal resolver reads its
            # authoritative files. Broken mappings must not become fallbacks.
            candidates = (manifest,) if manifest.exists() or manifest.is_symlink() else (root / ".mcp.json", root / ".cursor/mcp.json")
            for path in candidates:
                if path.exists() or path.is_symlink():
                    _file(path)
        cfg = _config(args)
    except (ProjectConfigError, OSError, ValueError) as exc:
        report["project"]["error"] = str(exc)
        report["project"]["repair"] = "Review the reported mapping/configuration. For a move, preview grag relocate OLD_ROOT NEW_ROOT --dry-run; for new setup preview grag init --dry-run. No files changed."
    if cfg is not None:
        target = admin.server_target(cfg)
        origin = ("CLI" if getattr(args, "db", None) or getattr(args, "db_dir", None)
                  else "environment" if any(os.environ.get(k) for k in ("GRAG_DB_PATH", "GRAG_DB_DIR", "GRAG_SERVER_URL"))
                  else "project manifest" if report["project"]["manifest"]
                  else "legacy project registration" if legacy_database(root) else "project-root knowledge.lbdb fallback")
        report["project"].update({"selection_source": origin, "database": str(target.resolve()),
                                  "exists": target.exists(), "server_url": cfg.server_url,
                                  "server_db": cfg.server_db})
        # HTTP health is passive. Do not query Repo or trigger read freshness here.
        report["server"] = {"status": "unverified remote"} if cfg.server_url else _owner(cfg)
        observed = report["server"].get("observed", {})
        if observed.get("index"):
            report["indexed_roots"] = {"source": "owner's cached observations; not a new freshness check",
                                       "roots": observed["index"].get("roots", [])}
        report["project"]["sidecars"] = [str(p) for suffix in (".wal", ".shadow", ".ses") if (p := Path(str(target) + suffix)).exists()]
    entries = {}
    for client, scope, path, section in _config_locations(root):
        config = {"client": client, "scope": scope, "path": str(path)}
        try:
            text, info = _file(path)
            config.update(info)
            obj = read_object(text, jsonc=client == "zed")
            servers = obj.get(section, {})
            if section == "projects":
                servers = servers.get(str(root), {}).get("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("server section must be an object")
            config["status"] = "read"
            discovered = 0
            for name, original in servers.items():
                entry = _normalize(original) if isinstance(original, dict) else {}
                command = entry.get("command", "")
                if not (name.lower().startswith("grag") or (isinstance(command, str) and Path(command).stem == "grag")
                        or (isinstance(entry.get("args"), list) and "grag.cli" in entry["args"])):
                    continue
                if discovered == 128:
                    config["truncated"] = "more than 128 grag registrations"
                    break
                discovered += 1
                key = f"{client}:{scope}:{name}"
                record = {"id": key, "client": client, "scope": scope, "name": name, "path": str(path), "issues": []}
                for kind in ("env", "headers"):
                    mapping = entry.get(kind, {})
                    if isinstance(mapping, dict):
                        for k, v in mapping.items():
                            if isinstance(v, str) and v and (kind == "headers" or _SECRET.search(k)):
                                secrets.append(v)
                                with contextlib.suppress(ValueError):
                                    secrets.append(_expand(v))
                try:
                    public, expanded = _launcher(entry, root)
                    record.update(public)
                    entries[key] = expanded
                    if entry.get("disabled") is True or entry.get("enabled") is False:
                        record["issues"].append("disabled_in_configuration: a successful explicit probe would not enable this server in the harness")
                    if client == "claude" and "url" in entry and "type" not in entry:
                        record["issues"].append("missing_transport_type: Claude URL registrations require type=http (or the applicable transport)")
                    if "command" in public and not public["resolved_command"]:
                        record["issues"].append("launcher_missing: restore/update the saved executable; preview grag init --dry-run")
                    db = public["database"]
                    if db and not Path(db).is_file():
                        record["issues"].append("database_missing: locate the original before starting; do not create an empty replacement")
                    if db and cfg is not None and not cfg.server_url and Path(db) != cfg.db_path.resolve():
                        record["issues"].append("database_mismatch: this registration selects a different database than this CLI")
                    if db and not public.get("shared_owner"):
                        record["issues"].append("direct_owner: multiple harnesses need the shared --auto-serve registration; preview init --dry-run")
                    if info["symlink"] or info["hardlinks"] > 1:
                        record["issues"].append("linked_configuration: review its resolved target manually; init deliberately preserves links")
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    record["issues"].append(str(exc))
                report["registrations"].append(record)
        except FileNotFoundError:
            config["status"] = "missing"
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            config.update({"status": "error", "error": str(exc)})
        report["configurations"].append(config)
    priority = {"user": 0, "project": 1, "local": 2}
    for record in report["registrations"]:
        same = [r for r in report["registrations"] if r["client"] == record["client"] and r["name"] == record["name"]]
        winner = max(same, key=lambda r: priority[r["scope"]])
        record["scope_precedence"] = "highest discovered" if winner is record else f"shadowed by {winner['id']}"
    report["discovery_limits"] = "Known Claude, Cursor, Windsurf and Zed files only. Client enablement, approvals, GUI environment and other profiles are unverified. Codex MCP is managed separately. Skill matches compare bundled files, not harness selection."
    # Private launch configurations/secrets never enter JSON or human output.
    return _public(report, secrets), cfg, {"entries": entries, "secrets": secrets,
                                         "project": report["project"], "registrations": report["registrations"]}


def _errors(exc: BaseException) -> str:
    children = getattr(exc, "exceptions", None)
    return "; ".join(_errors(c) for c in children) if children else str(exc) or type(exc).__name__


def _log_stamp(path: Path) -> tuple | None:
    try:
        info = path.stat()
        return info.st_ino, info.st_size, info.st_mtime_ns
    except OSError:
        return None


def _log_tail(path: Path, before: tuple | None = None) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return ""
            start = before[1] if before and before[0] == info.st_ino and before[1] <= info.st_size else 0
            stream.seek(max(start, info.st_size - 8192))
            return stream.read(8192).decode("utf-8", errors="replace")
    except OSError:
        return ""


def verify(report: dict, private: dict, selector: str, timeout: float) -> dict:
    from grag import admin
    from grag.onboarding import verify_entry

    matches = [r for r in private["registrations"] if r["id"] == selector]
    if not matches:
        return {"status": "failed", "detail": "Choose an exact registration id from status/doctor, e.g. claude:project:grag."}
    record = matches[0]
    entry = private["entries"].get(selector)
    # Refuse guessed/fresh/wrong targets; a successful empty DB is not recovery.
    expected = private["project"].get("database")
    db = record.get("database")
    if not entry or not db or db != expected or not Path(db).is_file() or not record.get("resolved_command"):
        return {"status": "failed", "registration": selector,
                "detail": "Verification requires an existing, explicit local database matching this CLI and an available launcher. Select it with --db. URL, implicit and directory targets remain unverified."}
    result: dict
    owner_log = admin.log_path(Path(db))
    before_log = _log_stamp(owner_log)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log:
        try:
            checked = asyncio.run(asyncio.wait_for(verify_entry(entry, Path(private["project"]["root"]), read_only=True, errlog=log, expanded=True), timeout=timeout))
            result = {"status": "verified", "registration": selector, "configured_database": db,
                      "harness_session": "unverified; this is a saved-launcher probe using this process environment",
                      "identity_basis": "saved explicit launcher selector; MCP does not attest its filesystem path", **checked}
            result["indexed_roots"] = [{"path": p, "exists": Path(p).exists()} for p in checked["indexed_roots"] if isinstance(p, str)]
        except Exception as exc:  # noqa: BLE001 — MCP/native child failures must become diagnostics
            log.flush()
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 8192))
            tail = log.read()
            detail = _errors(exc) + (f"; launcher stderr: {tail}" if tail else "")
            if record.get("shared_owner") and _log_stamp(owner_log) != before_log:
                detail += f"; owner log changed during probe ({owner_log}): {_log_tail(owner_log, before_log)}"
            lower = detail.lower()
            if ("corrupt" in lower and "wal" in lower) or "database replay failed" in lower:
                summary = "The selected database failed WAL replay."
                hint = "Stop this database's users, preserve all sidecars, then run grag --db FILE recover. Recovery uses a separate copy; do not delete WAL/shadow files or reindex a database that cannot open."
            elif "lock" in lower:
                summary = "The selected database is locked by another owner."
                hint = "Use the existing shared owner; preview init --dry-run for an --auto-serve registration."
            elif "buffer" in lower or "allocat" in lower or "out of memory" in lower:
                summary = "The native runtime reported a memory/resource failure."
                hint = "Check native buffer/memory limits and workload size. This is not evidence of invalid Cypher or WAL corruption."
            else:
                summary = _errors(exc)[:512]
                hint = "Check the saved launcher and its environment, the owner log and database. A passed install probe does not establish client readiness."
            result = {"status": "failed", "registration": selector, "summary": summary, "detail": detail, "hint": hint}
    return _public(result, private["secrets"])


def lines(report: dict) -> list[str]:
    from grag.admin import _safe_display_text

    runtime, project = report["runtime"], report["project"]
    output = [f"grag {runtime['grag_version']} | Python {runtime['python']}", f"source: {runtime['grag_module']}",
              f"checkout: {project['root']}"]
    if "error" in project:
        output += [f"configuration error: {project['error']}", project["repair"]]
    else:
        output += [f"selection: {project['selection_source']} -> {project['database']}"]
        output += report.get("server", {}).get("status_lines", ["server: remote; not probed"])
    for check in report.get("checks", []):
        output.append(f"[{check['status']}] {check['label']}: {check['detail']}")
        if "native" in check:
            output.append("  native identity: " + json.dumps(check["native"], ensure_ascii=True))
    for config in report["configurations"]:
        if config["status"] == "error":
            output.append(f"configuration error: {config['path']}: {config['error']}")
    output.append(f"client readiness: {report['client_readiness']}")
    for record in report["registrations"]:
        output += [f"  {record['id']} ({record['scope_precedence']}) | {record['path']}",
                   f"    launcher: {record.get('resolved_command') or record.get('url') or 'unavailable'}; database: {record.get('database') or 'unverified'}"]
        output += [f"    {issue}" for issue in record["issues"]]
    if not report["registrations"]:
        output.append("  No grag registrations found in the known client files.")
    for skill in report["skills"]:
        output.append(f"skill: {skill['path']} ({skill.get('bundle', skill.get('error'))})")
    output.append("indexed roots: " + json.dumps(report["indexed_roots"], ensure_ascii=True))
    if report.get("server", {}).get("observed"):
        owner_runtime = report["server"]["observed"]["runtime"]
        output.append(f"owner source: {owner_runtime['grag_module']} | Python {owner_runtime['python']}")
    if "verification" in report:
        verification = report["verification"]
        if verification["status"] == "failed":
            output.append("verification failed: " + verification.get("summary", verification.get("detail", "unknown")))
            output.append(verification.get("hint", "Use --json for the diagnostic details."))
        else:
            output.append("verification: " + json.dumps(verification, ensure_ascii=True))
    output.append("Use doctor --verify-client CLIENT:SCOPE:NAME for a saved launcher + graph read; this can start an owner and refresh indexes.")
    output.append(report["discovery_limits"])
    return [_safe_display_text(line) for line in output]


def run(args: argparse.Namespace) -> int:
    from grag.readiness import check_install, install_ready

    report, config, private = collect(args)
    exit_ok = "error" not in report["project"]
    if args.cmd == "doctor":
        try:
            checks = check_install(config or GragConfig.from_env(), prepare=args.prepare, timeout=args.timeout)
        except ValueError:
            checks = [{"required": True, "status": "unavailable", "key": "configuration",
                       "label": "environment configuration", "detail": "Fix the reported environment configuration before probing."}]
        report.update({"ready": install_ready(checks), "ready_scope": "installation only",
                       "checks": _public(checks, private["secrets"])})
        exit_ok = exit_ok and report["ready"]
        if args.verify_client:
            report["verification"] = verify(report, private, args.verify_client, args.timeout or 45)
            report["client_readiness"] = report["verification"]["status"]
            exit_ok = exit_ok and report["client_readiness"] == "verified"
    print(json.dumps(report, ensure_ascii=True) if args.json else "\n".join(lines(report)))
    return 0 if exit_ok else 1
