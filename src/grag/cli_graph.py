"""Small command adapters over the existing graph contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from grag.client import GraphClient
from grag.config import GragConfig
from grag.core.errors import ConfigurationError
from grag.core.types import (
    ContextRequest,
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    SearchRequest,
    UpsertNode,
    UpsertNodesRequest,
)


def graph_command(args: argparse.Namespace, cfg: GragConfig) -> int:
    if args.cmd == "ingest":
        from grag.ingest.loaders import ingest_paths

        print(ingest_paths(cfg, [Path(p) for p in args.paths], sections=args.sections))
        return 0
    if args.cmd == "ingest-code":
        from grag.ingest.code import ingest_code_paths

        if not args.paths and not (args.root and args.replace_scope):
            raise ConfigurationError(
                "Provide source paths, or --root <registered-root> --replace-scope to remove a scope."
            )
        print(
            ingest_code_paths(
                cfg,
                [Path(p) for p in args.paths],
                calls=not args.no_calls,
                max_file_kb=args.max_file_kb,
                root=args.root,
                replace_scope=args.replace_scope,
            )
        )
        return 0
    with GraphClient(cfg) as client:
        if args.cmd == "remember":
            spec = NodeTableSpec(
                name=args.label,
                primary_key="id",
                searchable=True,
                properties=[PropertySpec(name="text")],
            )
            schema = client.call("describe_schema")
            existing = next(
                (t for t in schema["node_tables"] if t["name"] == args.label), None
            )
            if existing and (
                not any(
                    p["name"] == "id" and p["is_primary_key"] and p["type"] == "STRING"
                    for p in existing["properties"]
                )
                or not any(
                    p["name"] == "text" and p["type"] == "STRING"
                    for p in existing["properties"]
                )
            ):
                raise ConfigurationError(
                    f"{args.label} is not a text memory table with an id key.",
                    hint="Choose another --label or use the existing schema through MCP.",
                )
            client.call("define_schema", DefineSchemaRequest(node_tables=[spec]))
            key = args.id or str(uuid4())
            result = client.call(
                "upsert_nodes",
                UpsertNodesRequest(
                    nodes=[
                        UpsertNode(
                            label=args.label,
                            key=key,
                            properties={"text": args.text},
                            source=args.source,
                            expected_revision=args.expected_revision,
                        )
                    ]
                ),
            )
            result["node_id"] = f"{args.label}:{key}"
            result["database"] = client.target
            print(
                json.dumps(result, ensure_ascii=False)
                if args.json
                else f"Saved {result['node_id']} in {client.target}."
            )
        else:
            options = {
                "hops": args.hops,
                "token_budget": args.tokens,
                "freshness": args.freshness,
            }
            result = (
                client.call(
                    "search_knowledge",
                    SearchRequest(query=args.query, labels=args.labels, **options),
                )
                if args.cmd == "search"
                else client.call(
                    "get_context", ContextRequest(node_ids=args.node_ids, **options)
                )
            )
            print(
                json.dumps(result, ensure_ascii=False)
                if args.json
                else result["context"]
                + "\n\n"
                + json.dumps(
                    {
                        k: result[k]
                        for k in ("freshness", "truncated", "included_node_ids")
                    }
                )
            )
    return 0
