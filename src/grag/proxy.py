"""Stdio-to-HTTP MCP proxy.

Bridges a stdio MCP client (e.g. Claude Code) to a running
``grag serve --with-mcp`` HTTP server, auto-starting that server if
it is not yet reachable.  The proxy process itself never opens the
.lbdb file, so the single-writer constraint is satisfied: only
``grag serve`` holds the write lock.

Usage (written by ``grag init``)::

    grag --db /path/to/db.lbdb mcp --auto-serve --port 8471
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


async def _ping(url: str) -> bool:
    import httpx2

    try:
        async with httpx2.AsyncClient() as c:
            r = await c.get(url, timeout=2.0)
            return r.status_code < 500
    except Exception:  # noqa: BLE001 — any network/OS error means not ready
        return False


async def _ensure_server(db_path: Path, port: int, url: str) -> None:
    """Start ``grag serve --with-mcp`` as a detached daemon if not running."""
    if await _ping(url):
        return

    grag = shutil.which("grag") or sys.argv[0]
    subprocess.Popen(  # noqa: S603 — grag is resolved from PATH, not user input
        [grag, "--db", str(db_path.resolve()), "serve", "--with-mcp", f"--port={port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    import asyncio

    for _ in range(40):
        await asyncio.sleep(0.5)
        if await _ping(url):
            return

    sys.exit(f"grag proxy: server at {url} did not become ready (waited 20 s)")


async def run_proxy(db_path: Path, port: int) -> None:
    """Bridge stdio ↔ streamable-http, auto-starting grag serve if needed."""
    import anyio
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server

    mcp_url = f"http://127.0.0.1:{port}/mcp/"
    # Ping the REST health endpoint — GET /mcp/ opens an SSE stream and hangs.
    health_url = f"http://127.0.0.1:{port}/api/health"
    await _ensure_server(db_path, port, health_url)

    async def _forward(src, dst, scope: anyio.CancelScope) -> None:
        try:
            async for msg in src:
                if isinstance(msg, Exception):
                    break
                await dst.send(msg)
        except (anyio.ClosedResourceError, anyio.EndOfStream):
            pass
        finally:
            scope.cancel()

    async with stdio_server() as (stdio_read, stdio_write):  # noqa: SIM117
        async with streamable_http_client(mcp_url) as (http_read, http_write):
            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_forward, stdio_read, http_write, tg.cancel_scope)
                    tg.start_soon(_forward, http_read, stdio_write, tg.cancel_scope)
            finally:
                # Close write ends so internal cleanup tasks (post_writer,
                # stdout_writer) see EndOfStream and exit cleanly.
                await http_write.aclose()
                await stdio_write.aclose()
