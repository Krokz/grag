"""grag CLI: serve / mcp / ingest / ingest-code / status / doctor / export / bench."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from grag.config import GragConfig, derive_port


def _port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _mounted_mcp_path(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    invalid_character = any(
        char.isspace()
        or ord(char) < 0x20
        or ord(char) == 0x7F
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    )
    if (
        parsed is None
        or not value.startswith("/")
        or value.startswith("//")
        or value == "/"
        or value == "/api"
        or value.startswith("/api/")
        or value == "/assets"
        or value.startswith("/assets/")
        or parsed.scheme
        or parsed.netloc
        or "?" in value
        or "#" in value
        or "\\" in value
        or invalid_character
    ):
        raise argparse.ArgumentTypeError(
            "mounted MCP path must be a clean URL path under '/' and cannot overlap "
            "'/', '/api', or '/assets'"
        )
    return value


def _config(args: argparse.Namespace) -> GragConfig:
    cfg = GragConfig.from_env()
    if getattr(args, "db", None):
        cfg.db_path = Path(args.db)
        # An explicit CLI selector wins over the opposite environment selector.
        cfg.db_dir = None
    if getattr(args, "db_dir", None):
        # Leave cfg.db_path at its default: its name picks the preferred
        # default db inside db_dir.
        cfg.db_path = GragConfig().db_path
        cfg.db_dir = Path(args.db_dir)
    return cfg


def main(argv: list[str] | None = None) -> int:
    import grag

    parser = argparse.ArgumentParser(
        prog="grag", description="LLM-first graph knowledgebase"
    )
    parser.add_argument(
        "--version", action="version", version=f"grag {grag.__version__}"
    )
    db_sel = parser.add_mutually_exclusive_group()
    db_sel.add_argument(
        "--db", default=None, help="path to the .lbdb database file (env: GRAG_DB_PATH)"
    )
    db_sel.add_argument(
        "--db-dir",
        default=None,
        help="directory of .lbdb files for multi-db serving (env: GRAG_DB_DIR)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the REST API + UI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_port_number, default=8471)
    serve.add_argument(
        "--with-mcp",
        action="store_true",
        help="also mount the MCP streamable-http endpoint on this server, so one "
        "process serves UI + REST + MCP against the same live .lbdb (single "
        "writer satisfied; the UI sees MCP writes the moment they land)",
    )
    serve.add_argument(
        "--mcp-path",
        type=_mounted_mcp_path,
        default="/mcp",
        help="HTTP path for the MCP endpoint when --with-mcp is set",
    )

    mcp = sub.add_parser("mcp", help="run the MCP server (stdio or streamable-http)")
    mcp.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http"],
        help="MCP transport: stdio (default, one process per client) or "
        "streamable-http (one shared server many clients connect to)",
    )
    mcp.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host (non-loopback streamable-http requires GRAG_API_TOKEN)",
    )
    mcp.add_argument(
        "--port",
        type=_port_number,
        default=8471,
        help="port for streamable-http transport or the --auto-serve target port (default 8471)",
    )
    mcp.add_argument(
        "--path", default="/mcp", help="HTTP endpoint path (streamable-http)"
    )
    mcp.add_argument(
        "--auto-serve",
        action="store_true",
        help=(
            "proxy stdio to 'grag serve --with-mcp' at --port, starting it as a "
            "background daemon if not already running; the proxy holds no write lock "
            "so the browser UI and LLM tools work simultaneously"
        ),
    )
    mcp.add_argument(
        "--server-url",
        default=None,
        help=(
            "proxy stdio to an already-running remote grag server at this origin "
            "(e.g. https://grag.example.com); never starts a local daemon, "
            "reconnects when the server restarts (env: GRAG_SERVER_URL; send "
            "GRAG_API_TOKEN as the bearer token)"
        ),
    )
    mcp.add_argument(
        "--server-db",
        default=None,
        help="database name (x-grag-db header) when the remote server runs "
        "--db-dir multi-db mode (env: GRAG_SERVER_DB)",
    )
    mcp.add_argument(
        "--insecure-http",
        action="store_true",
        help="allow a plain-http --server-url to a non-loopback host "
        "(env: GRAG_ALLOW_INSECURE_HTTP=1)",
    )

    ingest = sub.add_parser("ingest", help="ingest files into the graph")
    ingest.add_argument("paths", nargs="+")

    ingest_code = sub.add_parser(
        "ingest-code", help="ingest code structure (Repo/Module/Class/Function)"
    )
    ingest_code.add_argument("paths", nargs="+")
    ingest_code.add_argument("--no-calls", action="store_true", help="skip CALLS edges")
    ingest_code.add_argument(
        "--max-file-kb", type=int, default=1024, help="skip files larger than this (KB)"
    )

    sub.add_parser("status", help="show whether a server is running for this database")

    start = sub.add_parser(
        "start",
        help="start the server as a background daemon (frees the terminal; "
        "logs to ~/.grag/logs/)",
    )
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument(
        "--port",
        type=_port_number,
        default=None,
        help="port to bind (default: per-database derived port)",
    )
    start.add_argument(
        "--no-mcp",
        action="store_true",
        help="serve UI + REST only, without the MCP endpoint",
    )
    start.add_argument(
        "--mcp-path",
        type=_mounted_mcp_path,
        default="/mcp",
        help="mounted MCP path (default: /mcp)",
    )

    restart = sub.add_parser(
        "restart",
        help="stop the background server for this database (if any) and start a "
        "fresh one — picks up code changes",
    )
    restart.add_argument(
        "--host", default=None, help="bind host (default: preserve current setting)"
    )
    restart.add_argument(
        "--port",
        type=_port_number,
        default=None,
        help="port (default: preserve current setting)",
    )
    restart_mcp = restart.add_mutually_exclusive_group()
    restart_mcp.add_argument(
        "--with-mcp", dest="with_mcp", action="store_true", help="enable MCP"
    )
    restart_mcp.add_argument(
        "--no-mcp", dest="with_mcp", action="store_false", help="disable MCP"
    )
    restart.set_defaults(with_mcp=None)
    restart.add_argument(
        "--mcp-path",
        type=_mounted_mcp_path,
        default=None,
        help="MCP path (default: preserve current setting)",
    )
    restart.add_argument(
        "--force",
        action="store_true",
        help="allow the one-time restart of a legacy/unverified registration",
    )

    stop = sub.add_parser("stop", help="stop the background server for this database")
    stop.add_argument(
        "-a",
        "-all",
        "--all",
        action="store_true",
        help="stop every running grag server on the system, not just this database's",
    )
    stop.add_argument(
        "--force",
        action="store_true",
        help="also signal live legacy/unverified pidfile entries (unsafe; use only "
        "when you have independently confirmed the recorded PID)",
    )
    sub.add_parser(
        "doctor",
        help="diagnose the install: extras, embedder, server, code-index staleness",
    )

    export = sub.add_parser(
        "export", help="dump the database as portable JSONL (schema + nodes + edges)"
    )
    export.add_argument(
        "--out", "-o", default=None, help="output file (default: stdout)"
    )

    import_ = sub.add_parser(
        "import", help="replay a 'grag export' JSONL file into this database"
    )
    import_.add_argument("file", help="JSONL file produced by 'grag export'")

    bench = sub.add_parser("bench", help="codec benchmark (recall / latency / RSS)")
    bench.add_argument("--codec", default=None)

    reindex = sub.add_parser(
        "reindex",
        help="drop and rebuild vector indexes from scratch (use after a crash or WAL recovery)",
    )
    reindex.add_argument(
        "--batch-size", type=int, default=128, help="embedding batch size (default 128)"
    )

    init = sub.add_parser(
        "init",
        help="register grag with your LLM client (MCP config), update CLAUDE.md, "
        "and install the grag SKILL.md for skill-capable harnesses",
    )
    init.add_argument(
        "--client",
        default="auto",
        choices=["auto", "claude", "cursor", "windsurf", "zed"],
        help="LLM client to configure (default: auto-detect)",
    )
    init.add_argument(
        "--port",
        type=_port_number,
        default=None,
        help="port for the grag serve --with-mcp server (default: a per-project "
        "port derived from the database path, so projects don't collide)",
    )
    init.add_argument(
        "--ingest",
        action="store_true",
        help="also run ingest-code on the current directory right away",
    )
    init.add_argument(
        "--remove",
        action="store_true",
        help="undo init: remove the grag MCP entry, the CLAUDE.md block, and the "
        "grag SKILL.md (unless locally modified)",
    )
    init.add_argument(
        "--url",
        action="store_true",
        help=(
            "write URL transport instead of stdio+auto-serve — requires 'grag serve "
            "--with-mcp' to be running before the LLM client connects; the client "
            "will not auto-start grag."
        ),
    )
    init.add_argument(
        "--server-url",
        default=None,
        help=(
            "register a remote grag server (https://host[:port]) instead of a "
            "local database: MCP config points 'grag mcp --server-url' at it and "
            "CLAUDE.md documents the shared graph; GRAG_API_TOKEN is read from "
            "the environment at connect time"
        ),
    )
    init.add_argument(
        "--server-db",
        default=None,
        help="with --server-url: database name on a multi-db (--db-dir) server",
    )
    init.add_argument(
        "--no-mcp",
        action="store_true",
        help="skip MCP config — only update CLAUDE.md",
    )
    init.add_argument(
        "--no-claude-md",
        action="store_true",
        help="skip CLAUDE.md — only write MCP config",
    )
    init.add_argument(
        "--no-skill",
        action="store_true",
        help="skip SKILL.md — only write MCP config and CLAUDE.md",
    )
    init.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be written without writing anything",
    )

    args = parser.parse_args(argv)
    cfg = _config(args)

    if args.cmd == "serve":
        import os
        import secrets

        import uvicorn

        from grag.admin import (
            DaemonLifecycleError,
            _prepare_server_target,
            remove_pidfile,
            server_target,
            write_pidfile,
        )
        from grag.api.main import create_app
        from grag.core.errors import GragError

        if getattr(args, "with_mcp", False):
            cfg.mcp_path = args.mcp_path
        # The bind host drives the REST Host-header allow-list and the MCP
        # endpoint's DNS-rebinding allow-list, not just uvicorn's socket.
        cfg.host = args.host
        shutdown_token = secrets.token_urlsafe(32)
        # Register in ~/.grag/run/ so 'grag status' / 'grag stop' can find us.
        target = server_target(cfg)
        try:
            # A foreground serve should recover from a confirmed-dead daemon
            # registration just like `start` and MCP auto-serve do.
            _prepare_server_target(target)
        except DaemonLifecycleError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        claimed = write_pidfile(
            target,
            args.port,
            is_db_dir=cfg.db_dir is not None,
            with_mcp=cfg.mcp_path is not None,
            mcp_path=cfg.mcp_path,
            host=args.host,
            shutdown_token=shutdown_token,
        )
        if not claimed:
            print(
                "Could not safely register this server target; another server or "
                "stale pidfile may already own it. Check 'grag status' before "
                "starting another writer.",
                file=sys.stderr,
            )
            return 1
        try:
            app = create_app(cfg)
            server = uvicorn.Server(
                uvicorn.Config(app, host=args.host, port=args.port, workers=1)
            )
            app.state.shutdown_token = shutdown_token
            app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
            server.run()
        except GragError as exc:
            print(exc.message, file=sys.stderr)
            if exc.hint:
                print(exc.hint, file=sys.stderr)
            return 1
        finally:
            remove_pidfile(
                target, owner_pid=os.getpid(), shutdown_token=shutdown_token
            )
    elif args.cmd == "mcp":
        server_url = args.server_url or cfg.server_url
        if server_url:
            import asyncio

            from grag.proxy import run_remote_proxy

            asyncio.run(
                run_remote_proxy(
                    server_url,
                    api_token=cfg.api_token,
                    db_name=args.server_db or cfg.server_db,
                    allow_insecure=args.insecure_http or cfg.allow_insecure_http,
                )
            )
        elif getattr(args, "auto_serve", False):
            if cfg.db_dir is not None:
                parser.error(
                    "--db-dir cannot be used with 'mcp --auto-serve'; auto-serve "
                    "currently supports one --db file only. Start 'grag --db-dir "
                    "DIR serve --with-mcp' and configure the MCP client with that "
                    "server URL instead."
                )
            import asyncio

            from grag.proxy import run_proxy

            asyncio.run(run_proxy(cfg.db_path, args.port, api_token=cfg.api_token))
        else:
            from grag.mcp_server.server import run

            run(
                cfg,
                transport=args.transport,
                host=args.host,
                port=args.port,
                path=args.path,
            )
    elif args.cmd == "ingest":
        from grag.ingest.loaders import ingest_paths

        summary = ingest_paths(cfg, [Path(p) for p in args.paths])
        print(summary)
    elif args.cmd == "ingest-code":
        from grag.ingest.code import ingest_code_paths

        summary = ingest_code_paths(
            cfg,
            [Path(p) for p in args.paths],
            calls=not args.no_calls,
            max_file_kb=args.max_file_kb,
        )
        print(summary)
    elif args.cmd == "status":
        from grag.admin import status_lines

        print("\n".join(status_lines(cfg)))
    elif args.cmd == "start":
        from grag.admin import DaemonLifecycleError, start_daemon

        try:
            print(
                start_daemon(
                    cfg,
                    host=args.host,
                    port=args.port,
                    with_mcp=not args.no_mcp,
                    mcp_path=args.mcp_path,
                )
            )
        except DaemonLifecycleError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.cmd == "restart":
        from grag.admin import DaemonLifecycleError, restart_daemon

        try:
            print(
                restart_daemon(
                    cfg,
                    host=args.host,
                    port=args.port,
                    with_mcp=args.with_mcp,
                    mcp_path=args.mcp_path,
                    force=args.force,
                )
            )
        except DaemonLifecycleError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.cmd == "stop":
        from grag.admin import server_target, stop_all_result, stop_server_result

        outcome = (
            stop_all_result(force=args.force)
            if args.all
            else stop_server_result(server_target(cfg), force=args.force)
        )
        print(outcome.message)
        if not outcome.stopped:
            return 1
    elif args.cmd == "doctor":
        from grag.admin import doctor_lines

        print("\n".join(doctor_lines(cfg)))
    elif args.cmd == "export":
        from grag.admin import find_server
        from grag.core.engine import Engine
        from grag.transfer import export_to

        if find_server(cfg.db_path) is not None:
            print(
                "A server is running on this database (single-writer lock).\n"
                "Stop it first: grag --db "
                f"{cfg.db_path} stop",
                file=sys.stderr,
            )
            return 1
        with Engine(cfg) as engine:
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    n = export_to(engine, fh)
                print(f"Exported {n} line(s) to {args.out}", file=sys.stderr)
            else:
                export_to(engine, sys.stdout)
    elif args.cmd == "import":
        from grag.admin import find_server
        from grag.core.engine import Engine
        from grag.transfer import import_from

        if find_server(cfg.db_path) is not None:
            print(
                "A server is running on this database (single-writer lock).\n"
                "Stop it first: grag --db "
                f"{cfg.db_path} stop",
                file=sys.stderr,
            )
            return 1
        with Engine(cfg) as engine, open(args.file, encoding="utf-8") as fh:
            report = import_from(engine, cfg, fh)
            engine.execute_write("CHECKPOINT")
        print(f"Imported {report['nodes']} node(s), {report['edges']} edge(s).")
        for w in report["warnings"]:
            print(f"  warning: {w}")
    elif args.cmd == "bench":
        from grag.retrieval.bench import run_bench

        print(run_bench(cfg, codec=args.codec))
    elif args.cmd == "reindex":
        from grag.core.engine import Engine
        from grag.retrieval.vectors import node_tables, reindex_embeddings

        engine = Engine(cfg)
        try:
            tables = node_tables(engine)
            total = 0
            for table in tables:
                print(f"Reindexing {table} ...", flush=True)
                n = reindex_embeddings(engine, cfg, table, batch_size=args.batch_size)
                print(f"  {table}: {n} node(s) re-embedded")
                total += n
            engine.execute_write("CHECKPOINT")
            print(f"\nDone. {total} node(s) re-embedded across {len(tables)} table(s).")
        finally:
            engine.close()
    elif args.cmd == "init":
        from grag.project import (
            DeleteOp,
            SkipOp,
            WriteOp,
            apply_ops,
            detect_clients,
            plan_claude_md_op,
            plan_mcp_ops,
            plan_remove_ops,
            plan_skill_ops,
        )

        project_root = Path.cwd()
        # Explicit --db wins; otherwise default to ~/.grag/<project-name>.lbdb
        if getattr(args, "db", None):
            db_path = Path(args.db).resolve()
        else:
            db_path = Path.home() / ".grag" / f"{project_root.name}.lbdb"
        # Per-project default port: two initialised projects must not both
        # claim one port and collide at auto-serve time.
        port = args.port if args.port is not None else derive_port(db_path)

        clients = (
            detect_clients(project_root) if args.client == "auto" else [args.client]
        )

        if args.remove:
            remove_ops = plan_remove_ops(clients, project_root)
            if not remove_ops:
                print("Nothing to remove — no grag entries found.")
                return 0
            if args.dry_run:
                print("Would write (dry run):")
                for op in remove_ops:
                    if isinstance(op, SkipOp):
                        print(f"  skip:   {op.path}  ({op.reason})")
                    elif isinstance(op, DeleteOp):
                        print(f"  delete: {op.path}")
                    else:
                        print(f"  update: {op.path}")
            else:
                print("Removing grag configuration:")
                apply_ops(remove_ops)
            return 0

        server_url = args.server_url
        if server_url:
            from grag.proxy import validate_server_url

            try:
                server_url = validate_server_url(server_url, allow_insecure=True)
            except SystemExit as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if args.ingest:
                print(
                    "--ingest is ignored with --server-url: ingest on the server "
                    "host (or POST /api/jobs/ingest/code) instead.",
                    file=sys.stderr,
                )
                args.ingest = False

        ops: list[WriteOp | SkipOp | DeleteOp] = []
        if not args.no_mcp:
            ops.extend(
                plan_mcp_ops(
                    clients,
                    project_root,
                    db_path,
                    stdio=not args.url,
                    port=port,
                    server_url=server_url,
                    server_db=args.server_db,
                )
            )
        if not args.no_claude_md:
            ops.append(
                plan_claude_md_op(
                    project_root,
                    db_path,
                    port=port,
                    server_url=server_url,
                    server_db=args.server_db,
                )
            )
        if not args.no_skill:
            ops.extend(plan_skill_ops(clients, project_root))

        if not ops:
            print("Nothing to do (--no-mcp, --no-claude-md and --no-skill all given).")
            return 0

        if args.dry_run:
            print("Would write (dry run):")
            for op in ops:
                if isinstance(op, SkipOp):
                    print(f"  skip:   {op.path}  ({op.reason})")
                elif isinstance(op, DeleteOp):
                    print(f"  delete: {op.path}")
                else:
                    verb = "create" if op.created else "update"
                    print(f"  {verb}: {op.path}")
            return 0

        print("Writing:")
        apply_ops(ops)

        if args.ingest:
            from grag.ingest.code import ingest_code_paths

            cfg.db_path = db_path
            print(f"\nIngesting code from {project_root} ...")
            summary = ingest_code_paths(cfg, [project_root])
            print(summary)

        if server_url:
            print(
                "\nDone. Next steps:\n"
                "  1. Export GRAG_API_TOKEN in the environment your MCP client "
                "runs in (the config references it, never stores it).\n"
                "  2. Restart your MCP client (Claude Code / Cursor / ...) so it "
                "picks up the config.\n"
                f"  3. Browse the shared graph: {server_url}/"
            )
            return 0
        print(
            "\nDone. Next steps:\n"
            "  1. Restart your MCP client (Claude Code / Cursor / ...) so it "
            "picks up the config;\n"
            "     grag then starts automatically when the agent first uses it.\n"
            + (
                ""
                if args.ingest
                else "  2. Index this repo (ask your agent to run ingest_code, "
                f"or run:\n       grag --db {db_path} ingest-code {project_root})\n"
            )
            + f"  {'2' if args.ingest else '3'}. Browse the graph once the server "
            f"is up: http://127.0.0.1:{port}/\n"
            f"     (check with: grag --db {db_path} status)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
