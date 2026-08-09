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
    ingest_code.add_argument(
        "--no-calls", action="store_true", help="skip CALLS edges"
    )
    ingest_code.add_argument(
        "--max-file-kb", type=int, default=1024, help="skip files larger than this (KB)"
    )

    bench = sub.add_parser("bench", help="codec benchmark (recall / latency / RSS)")
    bench.add_argument("--codec", default=None)

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

        run(cfg, transport=args.transport, host=args.host, port=args.port, path=args.path)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
