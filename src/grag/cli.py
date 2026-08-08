"""grag CLI: serve / mcp / ingest / bench."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from grag.config import GragConfig


def _config(args: argparse.Namespace) -> GragConfig:
    cfg = GragConfig.from_env()
    if getattr(args, "db", None):
        cfg.db_path = Path(args.db)
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grag", description="LLM-first graph knowledgebase"
    )
    parser.add_argument(
        "--db", default=None, help="path to the .lbdb database file (env: GRAG_DB_PATH)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the REST API + UI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8471)

    sub.add_parser("mcp", help="run the MCP stdio server")

    ingest = sub.add_parser("ingest", help="ingest files into the graph")
    ingest.add_argument("paths", nargs="+")

    bench = sub.add_parser("bench", help="codec benchmark (recall / latency / RSS)")
    bench.add_argument("--codec", default=None)

    args = parser.parse_args(argv)
    cfg = _config(args)

    if args.cmd == "serve":
        import uvicorn

        from grag.api.main import create_app

        uvicorn.run(create_app(cfg), host=args.host, port=args.port, workers=1)
    elif args.cmd == "mcp":
        from grag.mcp_server.server import run

        run(cfg)
    elif args.cmd == "ingest":
        from grag.ingest.loaders import ingest_paths

        summary = ingest_paths(cfg, [Path(p) for p in args.paths])
        print(summary)
    elif args.cmd == "bench":
        from grag.retrieval.bench import run_bench

        print(run_bench(cfg, codec=args.codec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
