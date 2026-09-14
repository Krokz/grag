"""Verify the written client registration through a real MCP graph round trip."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from uuid import uuid4

from grag.config import GragConfig
from grag.config_document import read_server
from grag.core.errors import ConfigurationError

if TYPE_CHECKING:
    from grag.client import GraphClient

SETUP_SOURCE = "grag init connection verification"


def initial_mapping_needed(config: GragConfig) -> bool:
    """Check before init can write its own verification marker."""
    from grag.client import GraphClient

    with GraphClient(config) as client:
        return _initial_mapping_needed(client)


def _initial_mapping_needed(client: GraphClient) -> bool:
    """Inspect the selected owner before first-use indexing; fail closed on unknown counts."""
    from grag.core.types import QueryRequest

    schema = client.call("describe_schema")
    if schema.get("detail") != "full" or schema.get("unchanged") or not all(
        isinstance(schema.get(key), list) for key in ("node_tables", "rel_tables")
    ):
        raise ConfigurationError("Cannot establish whether the graph is empty from this schema response.")
    for table in [*schema["node_tables"], *schema["rel_tables"]]:
        count = table.get("row_count")
        if type(count) is not int or count < 0:
            raise ConfigurationError("Cannot establish whether the graph is empty: a row count is unknown.")
    if any(t["row_count"] for t in schema["rel_tables"]):
        return False
    occupied = [t for t in schema["node_tables"] if t["row_count"]]
    if not occupied:
        return True
    if len(occupied) != 1 or occupied[0]["name"] != "GragSetup" or occupied[0]["row_count"] != 1:
        return False
    props = {p["name"]: p["type"] for p in occupied[0]["properties"]}
    if not all(props.get(p) == "STRING" for p in ("id", "nonce", "_source")):
        return False
    result = client.call("cypher_query", QueryRequest(
        cypher="MATCH (n:GragSetup) RETURN n.id,n.nonce,n._source",
    ))
    rows = result["rows"]
    return (
        len(rows) == 1 and rows[0][0] == "connection"
        and isinstance(rows[0][1], str) and bool(rows[0][1])
        and rows[0][2] == SETUP_SOURCE
    )


def ingest_if_empty(config: GragConfig, root: Path) -> dict:
    """First-use convenience; recheck after MCP verification before indexing."""
    from grag.client import GraphClient
    from grag.core.types import CodeIngestRequest

    with GraphClient(config) as client:
        if not _initial_mapping_needed(client):
            return {"status": "skipped", "reason": "existing_graph_content", "database": client.target}
        result = client.call("ingest_code", CodeIngestRequest(paths=[str(root)], root=str(root)))
        return {"status": "ingested" if result.get("modules") else "no_supported_code",
                "database": client.target, "result": result}


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match[1]
        if name not in os.environ:
            raise ConfigurationError(
                f"Client registration requires {name} in its environment."
            )
        return os.environ[name]

    return re.sub(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


async def verify_entry(entry: dict, cwd: Path, *, read_only: bool = False, errlog: TextIO | None = None, expanded: bool = False) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import get_default_environment, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    def expand(value: str) -> str:
        return value if expanded else _expand(value)

    async with AsyncExitStack() as stack:
        if "url" in entry:
            from urllib.parse import urlsplit

            import httpx2

            from grag.proxy import _is_loopback_host

            headers = {
                key: expand(value) for key, value in entry.get("headers", {}).items()
            }
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=headers,
                    timeout=30,
                    trust_env=not _is_loopback_host(
                        urlsplit(entry["url"]).hostname or ""
                    ),
                )
            )
            read, write = await stack.enter_async_context(
                streamable_http_client(entry["url"], http_client=http)
            )
            runtime = entry["url"]
        else:
            command = entry["command"]
            if isinstance(command, dict):  # Zed's command object
                entry = {
                    "command": command["path"],
                    "args": command.get("args", []),
                    "env": command.get("env", {}),
                }
                command = entry["command"]
            env = {
                **get_default_environment(),
                **os.environ,
                **{key: expand(value) for key, value in entry.get("env", {}).items()},
            }
            log = errlog if errlog is not None else stack.enter_context(
                tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115 — owned by the exit stack
            )
            read, write = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=command,
                        args=entry.get("args", []),
                        env=env,
                        cwd=str(cwd),
                    ),
                    errlog=log,
                )
            )
            import shutil

            runtime = shutil.which(command, path=env.get("PATH")) or command
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=30)
        )
        initialized = await session.initialize()
        available = {tool.name for tool in (await session.list_tools()).tools}
        required = {
            "describe_schema",
            "define_schema",
            "upsert_nodes",
            "cypher_query",
            "search_knowledge",
            "get_context",
        }
        if not required.issubset(available):
            raise ConfigurationError(
                f"Configured client is missing grag tools: {sorted(required - available)}"
            )

        async def call(tool: str, args: dict) -> str:
            result = await session.call_tool(tool, args)
            text = "\n".join(c.text for c in result.content if c.type == "text")
            if result.is_error:
                raise ConfigurationError(f"Configured MCP {tool} failed: {text}")
            return text

        schema_text = await call("describe_schema", {})
        if read_only:
            # These are read tools, but an owning server can refresh its index on
            # reads. Doctor therefore invokes this only on explicit opt-in.
            def payload(text: str) -> dict:
                return json.loads(text.rsplit("\n---\n", 1)[-1])

            schema = payload(schema_text)
            result = payload(await call("cypher_query", {"cypher": "MATCH (n) RETURN label(n) LIMIT 1"}))
            if not isinstance(result.get("rows"), list) or not isinstance(schema.get("schema_revision"), str):
                raise ConfigurationError("Configured MCP returned an incomplete graph read.")
            roots = []
            setup = "unknown"
            # MCP's schema is rendered text with a metadata footer. Inspect only
            # the two known table signatures; do not infer a JSON schema document.
            tables = dict(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\(([^)]+)\)", schema_text, re.MULTILINE))
            if re.search(r"(?:^|, )path:STRING(?:,|$)", tables.get("Repo", "")):
                roots = payload(await call("cypher_query", {
                    "cypher": "MATCH (r:Repo) RETURN r.path LIMIT 100",
                }))["rows"]
            if re.search(r"(?:^|, )_source:STRING(?:,|$)", tables.get("GragSetup", "")):
                rows = payload(await call("cypher_query", {
                    "cypher": "MATCH (n:GragSetup) RETURN n._source LIMIT 1",
                }))["rows"]
                if rows and rows[0][0] == SETUP_SOURCE:
                    setup = SETUP_SOURCE
            return {"runtime": runtime, "server": initialized.server_info.name,
                    "reported_server_version": initialized.server_info.version,
                    "protocol": initialized.protocol_version, "tools": len(available),
                    "graph_read": "verified", "indexed_roots": [r[0] for r in roots],
                    "roots_limit": 100, "setup_provenance": setup}
        await call(
            "define_schema",
            {
                "rel_tables": [],
                "node_tables": [
                    {
                        "name": "GragSetup",
                        "primary_key": "id",
                        "searchable": False,
                        "properties": [{"name": "nonce"}],
                    }
                ],
            },
        )
        nonce = uuid4().hex
        await call(
            "upsert_nodes",
            {
                "nodes": [
                    {
                        "label": "GragSetup",
                        "key": "connection",
                        "properties": {"nonce": nonce},
                        "source": SETUP_SOURCE,
                    }
                ]
            },
        )
        result = json.loads(
            await call(
                "cypher_query",
                {"cypher": "MATCH (n:GragSetup {id:'connection'}) RETURN n.nonce"},
            )
        )
        if result["rows"] != [[nonce]]:
            raise ConfigurationError(
                "Configured MCP could not read back the value it wrote."
            )
        return {
            "runtime": runtime,
            "server": initialized.server_info.name,
            "protocol": initialized.protocol_version,
            "tools": len(available),
            "write_read": "verified",
        }


def verify_registrations(paths: list[Path], cwd: Path) -> list[dict]:
    def errors(exc: BaseException) -> str:
        children = getattr(exc, "exceptions", None)
        return (
            "; ".join(errors(child) for child in children)
            if children
            else str(exc) or type(exc).__name__
        )

    reports = []
    checked: dict[str, dict] = {}
    for path in dict.fromkeys(paths):
        text = path.read_text(encoding="utf-8")
        entry = (
            read_server(text, "context_servers", jsonc=True)
            if path.name == "settings.json"
            else read_server(text, "mcpServers")
        )
        if entry is None:
            raise ConfigurationError(
                f"No grag entry in {path}; client setup is incomplete."
            )
        signature = json.dumps(entry, sort_keys=True)
        try:
            if signature not in checked:
                checked[signature] = asyncio.run(
                    asyncio.wait_for(verify_entry(entry, cwd), timeout=45)
                )
        except Exception as exc:
            raise ConfigurationError(
                f"Client verification failed for {path}: {errors(exc)}",
                hint="The configuration was saved. Check its command, environment and selected database with 'grag status' and 'grag doctor'. Inspect the daemon log if startup failed; a WAL replay error needs 'grag --db <file> recover', not reindexing. Then rerun init.",
            ) from exc
        reports.append({"registration": str(path), **checked[signature]})
    return reports
