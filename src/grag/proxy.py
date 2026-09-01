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

import sys
from pathlib import Path
from urllib.parse import urlsplit

from grag.config import database_identity

_MCP_DEFAULT_TIMEOUT = 30.0
_MCP_SSE_READ_TIMEOUT = 300.0


async def _probe_server(
    url: str,
) -> tuple[bool, str | None, bool | None, str | None]:
    """Return server identity and MCP metadata from a health endpoint."""
    import httpx2

    try:
        # This is always a local control-plane probe. Ignore HTTP_PROXY so a
        # machine-wide proxy cannot intercept it or make localhost readiness
        # depend on NO_PROXY being configured correctly.
        async with httpx2.AsyncClient(trust_env=False) as c:
            r = await c.get(url, timeout=2.0)
            if r.status_code >= 500:
                return False, None, None, None
            try:
                body = r.json()
            except (TypeError, ValueError):
                return True, None, None, None
            identity = body.get("database_id") if isinstance(body, dict) else None
            mcp_enabled = body.get("mcp_enabled") if isinstance(body, dict) else None
            mcp_path = body.get("mcp_path") if isinstance(body, dict) else None
            return (
                True,
                identity if isinstance(identity, str) else None,
                mcp_enabled if isinstance(mcp_enabled, bool) else None,
                mcp_path if isinstance(mcp_path, str) else None,
            )
    except Exception:  # noqa: BLE001 — any network/OS error means not ready
        return False, None, None, None


def _wrong_database(port: int) -> SystemExit:
    return SystemExit(
        f"grag proxy: port {port} is already serving a different or unidentified "
        "database. Choose another --port, stop the existing server, or use "
        "grag --db-dir for a shared multi-database server."
    )


def _mcp_disabled(port: int) -> SystemExit:
    return SystemExit(
        f"grag proxy: port {port} is serving the requested database, but its MCP "
        "endpoint is disabled. Restart it with 'grag restart --with-mcp', or stop "
        "it and let auto-serve start an MCP-enabled server."
    )


def _invalid_mcp_path(port: int) -> SystemExit:
    return SystemExit(
        f"grag proxy: port {port} reported an invalid MCP path in /api/health. "
        "Refusing to connect; restart the server with a valid --mcp-path."
    )


def _validated_mcp_path(
    *, mcp_enabled: bool | None, mcp_path: str | None, port: int
) -> str:
    """Return a canonical mounted path without allowing an origin override."""
    if mcp_enabled is False:
        raise _mcp_disabled(port)
    if mcp_path is None:
        # Health responses before mcp_path was added only identified the
        # database. Preserve compatibility with their default MCP mount.
        if mcp_enabled is None:
            return "/mcp"
        raise _invalid_mcp_path(port)

    try:
        parsed = urlsplit(mcp_path)
    except ValueError:
        raise _invalid_mcp_path(port) from None
    if (
        not mcp_path.startswith("/")
        or mcp_path.startswith("//")
        or mcp_path == "/"
        or mcp_path == "/api"
        or mcp_path.startswith("/api/")
        or mcp_path == "/assets"
        or mcp_path.startswith("/assets/")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in mcp_path
        or any(
            char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in mcp_path
        )
        or any(0xD800 <= ord(char) <= 0xDFFF for char in mcp_path)
    ):
        raise _invalid_mcp_path(port)
    return mcp_path.rstrip("/")


def _matching_live_registration(db_path: Path, port: int) -> bool:
    """Whether another process is already starting this exact proxy target."""
    from grag.admin import _pid_alive, read_pidfile

    registration = read_pidfile(db_path)
    if registration is None:
        return False
    expected_db = ":memory:" if str(db_path) == ":memory:" else str(db_path.resolve())
    pid = registration.get("pid")
    registered_port = registration.get("port")
    return (
        registration.get("db") == expected_db
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(registered_port, int)
        and not isinstance(registered_port, bool)
        and registered_port == port
        and _pid_alive(pid)
    )


def _handle_probe(
    *,
    reachable: bool,
    actual: str | None,
    mcp_enabled: bool | None,
    mcp_path: str | None,
    expected: str,
    port: int,
) -> str | None:
    """Validate one probe; return its mounted MCP path when ready."""
    if not reachable:
        return None
    if actual != expected:
        raise _wrong_database(port)
    return _validated_mcp_path(mcp_enabled=mcp_enabled, mcp_path=mcp_path, port=port)


async def _ensure_server(db_path: Path, port: int, url: str) -> str:
    """Ensure an MCP-enabled server is ready and return its mounted path."""
    expected = database_identity(db_path)
    reachable, actual, mcp_enabled, mcp_path = await _probe_server(url)
    ready_path = _handle_probe(
        reachable=reachable,
        actual=actual,
        mcp_enabled=mcp_enabled,
        mcp_path=mcp_path,
        expected=expected,
        port=port,
    )
    if ready_path is not None:
        return ready_path

    from grag.admin import DaemonLifecycleError, _spawn_server_process, log_path

    # Shared with 'grag start': detached daemon, logs to ~/.grag/logs/ so
    # embedder failures and startup errors stay debuggable.
    try:
        _spawn_server_process(db_path, port, with_mcp=True)
    except DaemonLifecycleError:
        # Another proxy/start command can claim the registration after our
        # initial health probe but before this spawn. If it is the same live
        # target, attach to its startup wait instead of failing the MCP client.
        if not _matching_live_registration(db_path, port):
            raise
        print(
            f"grag proxy: another process is starting the matching server on port "
            f"{port}; waiting for it to become ready",
            file=sys.stderr,
        )
    else:
        print(
            f"grag proxy: starting 'grag serve --with-mcp' on port {port} "
            f"(log: {log_path(db_path)})",
            file=sys.stderr,
        )

    import asyncio

    for _ in range(40):
        await asyncio.sleep(0.5)
        reachable, actual, mcp_enabled, mcp_path = await _probe_server(url)
        ready_path = _handle_probe(
            reachable=reachable,
            actual=actual,
            mcp_enabled=mcp_enabled,
            mcp_path=mcp_path,
            expected=expected,
            port=port,
        )
        if ready_path is not None:
            return ready_path

    sys.exit(f"grag proxy: server at {url} did not become ready (waited 20 s)")


async def run_proxy(db_path: Path, port: int, *, api_token: str | None = None) -> None:
    """Bridge stdio ↔ streamable-http, auto-starting grag serve if needed."""
    import anyio
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server

    # Ping the REST health endpoint — GET /mcp/ opens an SSE stream and hangs.
    health_url = f"http://127.0.0.1:{port}/api/health"
    mcp_path = await _ensure_server(db_path, port, health_url)
    # Keep the origin fixed rather than resolving server-provided metadata as
    # a URL. _validated_mcp_path rejects authority/query/fragment overrides.
    mcp_url = f"http://127.0.0.1:{port}{mcp_path}/"

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

    headers = {"Authorization": f"Bearer {api_token}"} if api_token else None
    async with (
        httpx2.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx2.Timeout(_MCP_DEFAULT_TIMEOUT, read=_MCP_SSE_READ_TIMEOUT),
            # The bearer token belongs only on this loopback MCP endpoint.
            trust_env=False,
        ) as http_client,
        stdio_server() as (stdio_read, stdio_write),
        streamable_http_client(mcp_url, http_client=http_client) as (
            http_read,
            http_write,
        ),
    ):
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_forward, stdio_read, http_write, tg.cancel_scope)
                tg.start_soon(_forward, http_read, stdio_write, tg.cancel_scope)
        finally:
            # Close write ends so internal cleanup tasks (post_writer,
            # stdout_writer) see EndOfStream and exit cleanly.
            await http_write.aclose()
            await stdio_write.aclose()
