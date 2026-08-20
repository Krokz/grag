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

from grag.config import database_identity


async def _probe_server(url: str) -> tuple[bool, str | None]:
    """Return ``(reachable, database_id)`` for a grag health endpoint."""
    import httpx2

    try:
        async with httpx2.AsyncClient() as c:
            r = await c.get(url, timeout=2.0)
            if r.status_code >= 500:
                return False, None
            try:
                body = r.json()
            except (TypeError, ValueError):
                return True, None
            identity = body.get("database_id") if isinstance(body, dict) else None
            return True, identity if isinstance(identity, str) else None
    except Exception:  # noqa: BLE001 — any network/OS error means not ready
        return False, None


def _wrong_database(port: int) -> SystemExit:
    return SystemExit(
        f"grag proxy: port {port} is already serving a different or unidentified "
        "database. Choose another --port, stop the existing server, or use "
        "grag --db-dir for a shared multi-database server."
    )


async def _ensure_server(db_path: Path, port: int, url: str) -> None:
    """Start ``grag serve --with-mcp`` as a detached daemon if not running."""
    expected = database_identity(db_path)
    reachable, actual = await _probe_server(url)
    if reachable:
        if actual == expected:
            return
        raise _wrong_database(port)

    from grag.admin import log_path, open_daemon_log

    grag = shutil.which("grag") or sys.argv[0]
    # Daemon output goes to ~/.grag/logs/ (not /dev/null): embedder failures
    # and startup errors in the auto-served process must stay debuggable.
    log_fd = open_daemon_log(db_path)
    try:
        subprocess.Popen(  # noqa: S603 — grag is resolved from PATH, not user input
            [
                grag,
                "--db",
                str(db_path.resolve()),
                "serve",
                "--with-mcp",
                f"--port={port}",
            ],
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )
    finally:
        if isinstance(log_fd, int) and log_fd >= 0:
            import os

            os.close(log_fd)
    print(
        f"grag proxy: starting 'grag serve --with-mcp' on port {port} "
        f"(log: {log_path(db_path)})",
        file=sys.stderr,
    )

    import asyncio

    for _ in range(40):
        await asyncio.sleep(0.5)
        reachable, actual = await _probe_server(url)
        if reachable:
            if actual == expected:
                return
            raise _wrong_database(port)

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
