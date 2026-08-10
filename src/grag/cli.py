"""grag CLI: serve / mcp / ingest / ingest-code / bench."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from grag.config import GragConfig


def _config(args: argparse.Namespace) -> GragConfig:
    cfg = GragConfig.from_env()
    if getattr(args, "db", None):
        cfg.db_path = Path(args.db)
    if getattr(args, "db_dir", None):
        # Leave cfg.db_path at its default: its name picks the preferred
        # default db inside db_dir.
        cfg.db_dir = Path(args.db_dir)
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grag", description="LLM-first graph knowledgebase"
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
    serve.add_argument("--port", type=int, default=8471)
    serve.add_argument(
        "--with-mcp",
        action="store_true",
        help="also mount the MCP streamable-http endpoint on this server, so one "
        "process serves UI + REST + MCP against the same live .lbdb (single "
        "writer satisfied; the UI sees MCP writes the moment they land)",
    )
    serve.add_argument(
        "--mcp-path",
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
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8472)
    mcp.add_argument(
        "--path", default="/mcp", help="HTTP endpoint path (streamable-http)"
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
        help="register grag with your LLM client (MCP config) and update CLAUDE.md",
    )
    init.add_argument(
        "--client",
        default="auto",
        choices=["auto", "claude", "cursor", "windsurf", "zed"],
        help="LLM client to configure (default: auto-detect)",
    )
    init.add_argument(
        "--port",
        type=int,
        default=8471,
        help="port for the grag serve --with-mcp server (default 8471, used in the MCP URL)",
    )
    init.add_argument(
        "--stdio",
        action="store_true",
        help=(
            "write stdio transport instead of URL — the client starts 'grag mcp' "
            "automatically but holds the write lock, preventing 'grag serve' on the "
            "same file. Prefer URL (default) when you also want the browser UI."
        ),
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
        "--dry-run",
        action="store_true",
        help="show what would be written without writing anything",
    )

    args = parser.parse_args(argv)
    cfg = _config(args)

    if args.cmd == "serve":
        import uvicorn

        from grag.api.main import create_app

        if getattr(args, "with_mcp", False):
            cfg.mcp_path = args.mcp_path
        # The bind host drives the REST Host-header allow-list and the MCP
        # endpoint's DNS-rebinding allow-list, not just uvicorn's socket.
        cfg.host = args.host
        uvicorn.run(create_app(cfg), host=args.host, port=args.port, workers=1)
    elif args.cmd == "mcp":
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
            SkipOp,
            WriteOp,
            apply_ops,
            detect_clients,
            plan_claude_md_op,
            plan_mcp_ops,
        )

        project_root = Path.cwd()
        # Explicit --db wins; otherwise default to ~/.grag/<project-name>.lbdb
        if getattr(args, "db", None):
            db_path = Path(args.db).resolve()
        else:
            db_path = Path.home() / ".grag" / f"{project_root.name}.lbdb"

        clients = (
            detect_clients(project_root) if args.client == "auto" else [args.client]
        )

        ops: list[WriteOp | SkipOp] = []
        if not args.no_mcp:
            ops.extend(
                plan_mcp_ops(
                    clients,
                    project_root,
                    db_path,
                    stdio=args.stdio,
                    port=args.port,
                )
            )
        if not args.no_claude_md:
            ops.append(plan_claude_md_op(project_root, db_path, port=args.port))

        if not ops:
            print("Nothing to do (both --no-mcp and --no-claude-md were given).")
            return 0

        if args.dry_run:
            print("Would write (dry run):")
            for op in ops:
                if isinstance(op, SkipOp):
                    print(f"  skip:   {op.path}  ({op.reason})")
                else:
                    verb = "create" if op.created else "update"
                    print(f"  {verb}: {op.path}")
        else:
            print("Writing:")
            apply_ops(ops)
    return 0


if __name__ == "__main__":
    sys.exit(main())
