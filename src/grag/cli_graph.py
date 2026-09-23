"""Small command adapters over the existing graph contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from grag.client import GraphClient
from grag.config import GragConfig
from grag.core.errors import ConfigurationError, NotFoundError
from grag.core.ident import validate_identifier
from grag.core.types import (
    ContextRequest,
    DefineSchemaRequest,
    EvidenceUpdate,
    NodeTableSpec,
    PropertySpec,
    QueryRequest,
    SearchRequest,
    UpsertNode,
    UpsertNodesRequest,
    make_node_id,
    split_node_id,
)


def _node_schema(client: GraphClient, node_id: str) -> tuple[str, str, str]:
    label, key = split_node_id(node_id)
    if not label or ":" not in node_id or not key:
        raise ConfigurationError("Use a complete Label:key node ID from a saved result or search.")
    validate_identifier(label)
    schema = client.call("describe_schema")
    table = next((table for table in schema["node_tables"] if table["name"] == label), None)
    if table is None:
        raise NotFoundError(f"Unknown node label: {label}")
    primary = next((prop["name"] for prop in table["properties"] if prop["is_primary_key"]), None)
    if primary is None:
        raise ConfigurationError(f"{label} has no declared primary key.")
    return label, key, validate_identifier(primary)


def _inspect(client: GraphClient, args: argparse.Namespace) -> dict:
    label, key, primary = _node_schema(client, args.node_id)
    # JSON quoting produces a Cypher string literal, including quotes, slashes
    # and controls. Only validated schema identifiers enter the query as syntax.
    literal = json.dumps(key, ensure_ascii=False)
    result = client.call("cypher_query", QueryRequest(
        cypher=f"MATCH (n:{label}) WHERE n.{primary}={literal} RETURN n", limit=1,
        freshness=args.freshness,
    ))
    if not result["rows"]:
        raise NotFoundError(f"Node not found: {args.node_id}")
    if result["truncated"]:
        raise ConfigurationError("Node inspection was incomplete; no revision was selected.")
    node = result["rows"][0][0]
    return {"node_id": make_node_id(label, node[primary]), "database": client.target,
            "revision": node["_revision"], "freshness": result["freshness"],
            "evidence_policy": "all",
            "node": {key: value for key, value in node.items()
                     if key not in {"_ID", "_LABEL"} and value is not None}}


def _print_mutation(result: dict, client: GraphClient, args: argparse.Namespace, node_id: str) -> None:
    revisions = result.get("revisions", {})
    if revisions:
        node_id, revision = next(iter(revisions.items()))  # these CLI writes target one node
        result["revision"] = revision
    result.update(node_id=node_id, database=client.target)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return
    if result.get("replayed"):
        print(f"Replayed the earlier operation for {node_id}; inspect for current state.")
    elif args.cmd == "retire":
        print(f"Retracted {node_id} in {client.target}; content, relationships and history retained.")
    else:
        print(f"Saved {node_id} in {client.target}.")
    if result.get("revision"):
        print(f"Revision: {result['revision']}")
    for warning in result.get("warnings", []):
        print(f"Warning: {warning}")


def preset_lines(report: dict, target: str) -> list[str]:
    """Human summary of one preset adoption."""
    before = report.get("previous_version")
    lines = [f"Memory preset v{report['version']} in {target}"
             + (f" (was v{before})." if before not in (None, report["version"]) else ".")]
    if report.get("created"):
        lines.append(f"Created: {', '.join(report['created'])}")
    if report.get("added"):
        lines.append(f"Added properties: {', '.join(report['added'])}")
    if not report.get("created") and not report.get("added"):
        lines.append("No changes needed.")
    lines.extend(f"Conflict (unchanged): {conflict}" for conflict in report.get("conflicts", []))
    return lines


def graph_command(args: argparse.Namespace, cfg: GragConfig) -> int:
    if args.cmd == "ingest":
        from grag.ingest.loaders import ingest_paths

        print(ingest_paths(cfg, [Path(p) for p in args.paths], sections=args.sections, json_mode=args.json_mode))
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
    if args.cmd == "remember" and args.operation_id and not args.id:
        raise ConfigurationError("remember --operation-id requires --id so retries address the same memory.")
    if args.cmd == "retire" and args.expected_revision == "absent":
        raise ConfigurationError("retire requires an existing revision token; use grag inspect Label:key first.")
    with GraphClient(cfg) as client:
        if args.cmd == "inspect":
            print(json.dumps(_inspect(client, args), ensure_ascii=False, indent=None if args.json else 2))
        elif args.cmd == "memory":
            result = client.call("define_schema", DefineSchemaRequest(preset="memory", allow_similar=args.allow_similar))
            if args.json:
                print(json.dumps(result["preset"], ensure_ascii=False))
            else:
                print("\n".join(preset_lines(result["preset"], client.target)))
        elif args.cmd == "retire":
            label, key, _ = _node_schema(client, args.node_id)
            result = client.call("upsert_nodes", UpsertNodesRequest(nodes=[UpsertNode(
                label=label, key=key, source=args.source, expected_revision=args.expected_revision,
                evidence=EvidenceUpdate(state="retracted", superseded_by=None, reason=args.reason),
            )], operation_id=args.operation_id))
            _print_mutation(result, client, args, args.node_id)
        elif args.cmd == "remember":
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
            key = args.id or str(uuid4())
            track = args.track_history or args.reason is not None
            request = UpsertNodesRequest(nodes=[UpsertNode(
                label=args.label, key=key, properties={"text": args.text}, source=args.source,
                expected_revision=args.expected_revision or ("absent" if track else None),
                evidence=EvidenceUpdate(reason=args.reason) if track else None,
            )], operation_id=args.operation_id)
            client.call("define_schema", DefineSchemaRequest(node_tables=[spec]))
            result = client.call(
                "upsert_nodes", request,
            )
            _print_mutation(result, client, args, make_node_id(args.label, key))
        else:
            options = {
                "hops": args.hops,
                "token_budget": args.tokens,
                "freshness": args.freshness,
                "evidence": args.evidence,
            }
            result = (
                client.call(
                    "search_knowledge",
                    SearchRequest(query=args.query, labels=args.labels, **options),
                )
                if args.cmd == "search"
                else client.call(
                    "get_context", ContextRequest(node_ids=args.node_ids, history=args.history,
                                                  history_before=args.history_before, revision=args.revision, **options)
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
                        for k in ("freshness", "truncated", "included_node_ids", "omitted_nodes",
                                  "omitted_edges", "omitted_properties", "expansion_limited",
                                  "evidence_policy", "excluded_evidence", "history")
                        if k in result and result[k] is not None
                    }
                )
            )
    return 0
