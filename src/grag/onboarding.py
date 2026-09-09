"""Verify the written client registration through a real MCP graph round trip."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

from grag.config_document import read_server
from grag.core.errors import ConfigurationError


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match[1]
        if name not in os.environ:
            raise ConfigurationError(
                f"Client registration requires {name} in its environment."
            )
        return os.environ[name]

    return re.sub(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


async def verify_entry(entry: dict, cwd: Path) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import get_default_environment, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    async with AsyncExitStack() as stack:
        if "url" in entry:
            from urllib.parse import urlsplit

            import httpx2

            from grag.proxy import _is_loopback_host

            headers = {
                key: _expand(value) for key, value in entry.get("headers", {}).items()
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
                **{key: _expand(value) for key, value in entry.get("env", {}).items()},
            }
            log = stack.enter_context(
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

        await call("describe_schema", {})
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
                        "source": "grag init connection verification",
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
