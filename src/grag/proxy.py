"""Stdio-to-HTTP MCP proxy.

Bridges a stdio MCP client (e.g. Claude Code) to a running
``grag serve --with-mcp`` HTTP server, auto-starting that server if
it is not yet reachable — and re-starting it if it dies mid-session.
On reconnect the proxy replays the client's captured ``initialize``
handshake into the fresh upstream session, so a server crash surfaces
to the MCP client as at most one failed tool call; the next retry goes
through.  The proxy process itself never opens the .lbdb file, so the
single-writer constraint is satisfied: only ``grag serve`` holds the
write lock.

Two modes share the relay/heal machinery:

* **local** (``run_proxy``): the upstream is ``http://127.0.0.1:<port>``;
  a missing server is spawned as a detached daemon and a crashed one is
  respawned.
* **remote** (``run_remote_proxy``): the upstream is an operator-run grag
  server at ``GRAG_SERVER_URL`` (a cloud host). The proxy never spawns
  anything; when the upstream drops it waits for the host's supervisor
  (systemd / container restart policy) to bring it back, then reconnects
  and replays the handshake exactly like the local heal path. The
  server's ``database_id`` is pinned on first contact so a reconnect can
  never silently land on a different database.

Usage (written by ``grag init``)::

    grag --db /path/to/db.lbdb mcp --auto-serve --port 8471
    grag mcp --server-url https://grag.example.com
"""

from __future__ import annotations

import asyncio
import ipaddress
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from grag.config import database_identity

_MCP_DEFAULT_TIMEOUT = 30.0
_MCP_SSE_READ_TIMEOUT = 300.0
# Remote health probes cross a real network; give them more room than the
# 2 s loopback budget.
_REMOTE_PROBE_TIMEOUT = 5.0
# How long a remote heal waits for the host supervisor to restart the server
# before the proxy gives up on this attempt (the supervisor loop retries up
# to _MAX_SESSION_RESTARTS times, so the overall patience is several minutes).
_REMOTE_READY_WAIT_SECONDS = 60.0


async def _probe_server(
    url: str,
    *,
    trust_env: bool = False,
    timeout: float = 2.0,
) -> tuple[bool, str | None, bool | None, str | None]:
    """Return server identity and MCP metadata from a health endpoint."""
    import httpx2

    try:
        # Local probes ignore HTTP_PROXY so a machine-wide proxy cannot
        # intercept them or make localhost readiness depend on NO_PROXY being
        # configured correctly. Remote probes honour the environment (corporate
        # proxies, custom CA bundles) like any other outbound HTTPS call.
        async with httpx2.AsyncClient(trust_env=trust_env) as c:
            r = await c.get(url, timeout=timeout)
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


def _mcp_disabled(target: int | str) -> SystemExit:
    where = f"port {target}" if isinstance(target, int) else target
    return SystemExit(
        f"grag proxy: {where} is serving the requested database, but its MCP "
        "endpoint is disabled. Restart it with 'grag restart --with-mcp', or stop "
        "it and let auto-serve start an MCP-enabled server."
    )


def _invalid_mcp_path(target: int | str) -> SystemExit:
    where = f"port {target}" if isinstance(target, int) else target
    return SystemExit(
        f"grag proxy: {where} reported an invalid MCP path in /api/health. "
        "Refusing to connect; restart the server with a valid --mcp-path."
    )


def _validated_mcp_path(
    *, mcp_enabled: bool | None, mcp_path: str | None, port: int | str
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


# --- remote mode --------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    candidate = host.strip().strip("[]")
    if candidate.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def validate_server_url(url: str, *, allow_insecure: bool = False) -> str:
    """Normalise a remote grag origin (``scheme://host[:port]``).

    Rejects anything but http(s), a path component (the MCP path is discovered
    from ``/api/health``, never configured on the client), and plain http to a
    non-loopback host unless ``allow_insecure`` — the bearer token would travel
    in clear text otherwise. Raises SystemExit with an operator-facing message.
    """
    raw = url.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SystemExit(
            f"grag proxy: invalid --server-url {url!r}; expected "
            "https://host[:port] (or http://127.0.0.1:port for a local server)."
        )
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise SystemExit(
            f"grag proxy: --server-url {url!r} must be an origin without a path, "
            "query or fragment; the MCP mount path is discovered from /api/health."
        )
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        if not allow_insecure:
            raise SystemExit(
                f"grag proxy: refusing plain http to non-loopback host "
                f"{parsed.hostname!r}: the bearer token would be sent in clear "
                "text. Use https://, or set GRAG_ALLOW_INSECURE_HTTP=1 on a "
                "trusted network."
            )
        print(
            "grag proxy: WARNING — connecting over plain http; the bearer token "
            "is not encrypted in transit",
            file=sys.stderr,
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _remote_changed_database(origin: str) -> SystemExit:
    return SystemExit(
        f"grag proxy: {origin} now serves a different database than it did when "
        "this session started. Refusing to reconnect; restart the MCP client to "
        "attach to the new database deliberately."
    )


async def _ensure_remote_server(
    origin: str, pinned: dict, *, wait_seconds: float = _REMOTE_READY_WAIT_SECONDS
) -> str:
    """Wait for the remote server to answer /api/health; return its MCP path.

    Nothing is spawned: the host's supervisor owns the server's lifecycle. The
    first successful probe pins the server's ``database_id``; later probes
    (heals) must report the same id or the proxy exits rather than silently
    bridging the client onto a different database.
    """
    health_url = f"{origin}/api/health"
    deadline = time.monotonic() + wait_seconds
    first = True
    while True:
        reachable, actual, mcp_enabled, mcp_path = await _probe_server(
            health_url, trust_env=True, timeout=_REMOTE_PROBE_TIMEOUT
        )
        if reachable:
            if "database_id" not in pinned:
                pinned["database_id"] = actual
            elif pinned["database_id"] != actual:
                raise _remote_changed_database(origin)
            return _validated_mcp_path(
                mcp_enabled=mcp_enabled, mcp_path=mcp_path, port=origin
            )
        if first:
            print(
                f"grag proxy: waiting for {origin} to become ready",
                file=sys.stderr,
            )
            first = False
        if time.monotonic() >= deadline:
            raise ConnectionError(
                f"grag proxy: server at {origin} did not become ready "
                f"(waited {int(wait_seconds)} s)"
            )
        await asyncio.sleep(1.0)


# --- relay ------------------------------------------------------------------------

# How often the proxy re-establishes a dead upstream session before giving up.
# A session that survives this long resets the counter, so a server that
# crashes once in a while never accumulates towards the cap.
_MAX_SESSION_RESTARTS = 5
_HEALTHY_SESSION_SECONDS = 60.0


def _mark(ended: dict, side: str) -> None:
    """Record which side of the bridge ended first (the other is cancelled)."""
    if ended["side"] is None:
        ended["side"] = side


async def _forward(src, dst, scope, ended: dict, src_side: str, dst_side: str, snoop=None) -> None:
    try:
        async for msg in src:
            if isinstance(msg, Exception):
                _mark(ended, src_side)
                break
            if snoop is not None:
                snoop(msg)
            try:
                await dst.send(msg)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                _mark(ended, dst_side)
                break
        # A closed stream ends the async-for silently (StopAsyncIteration),
        # not via EndOfStream — loop exhaustion means the source side ended.
        _mark(ended, src_side)
    except (anyio.ClosedResourceError, anyio.EndOfStream):
        _mark(ended, src_side)
    except Exception:  # noqa: BLE001 — a broken stream ends the session either way
        _mark(ended, src_side)
    finally:
        scope.cancel()


def _capture_initialize(msg, handshake: dict) -> None:
    """Remember the client's initialize params so a reconnect can replay them."""
    message = getattr(msg, "message", None)
    method = getattr(message, "method", None)
    if method == "initialize" and handshake.get("initialize") is None:
        handshake["initialize"] = getattr(message, "params", None)
    elif method == "notifications/initialized":
        handshake["initialized"] = True


async def _replay_initialize(http_read, http_write, handshake: dict) -> None:
    """Re-establish the MCP session on a fresh upstream after a server restart.

    The stdio client initialized once at startup and does not know the upstream
    died, so the proxy replays its captured initialize (under a proxy-internal
    id, swallowing the response) plus the initialized notification before
    resuming normal forwarding.
    """
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCNotification, JSONRPCRequest

    init_id = "grag-proxy-reinit"
    await http_write.send(
        SessionMessage(
            message=JSONRPCRequest(
                jsonrpc="2.0",
                id=init_id,
                method="initialize",
                params=handshake["initialize"],
            )
        )
    )
    answered = False
    with anyio.move_on_after(_MCP_DEFAULT_TIMEOUT):
        async for msg in http_read:
            if getattr(getattr(msg, "message", None), "id", None) == init_id:
                answered = True
                break
    if not answered:
        raise TimeoutError("upstream did not answer the replayed initialize")
    await http_write.send(
        SessionMessage(
            message=JSONRPCNotification(
                jsonrpc="2.0", method="notifications/initialized"
            )
        )
    )


async def _relay_session(
    mcp_url: str, http_client, stdio_read, stdio_write, handshake: dict
) -> str:
    """Bridge one upstream MCP session; return which side ended it.

    "stdio" means the MCP client went away (normal shutdown); "upstream" means
    the HTTP server connection failed (the supervisor should heal it).
    """
    from mcp.client.streamable_http import streamable_http_client

    ended = {"side": None}
    async with streamable_http_client(mcp_url, http_client=http_client) as (
        http_read,
        http_write,
    ):
        if handshake.get("initialize") is not None:
            await _replay_initialize(http_read, http_write, handshake)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    _forward,
                    stdio_read,
                    http_write,
                    tg.cancel_scope,
                    ended,
                    "stdio",
                    "upstream",
                    lambda msg: _capture_initialize(msg, handshake),
                )
                tg.start_soon(
                    _forward,
                    http_read,
                    stdio_write,
                    tg.cancel_scope,
                    ended,
                    "upstream",
                    "stdio",
                )
        finally:
            # Close the write end so internal cleanup tasks (post_writer)
            # see EndOfStream and exit cleanly.
            await http_write.aclose()
    return ended["side"] or "upstream"


def _deliberately_stopped(db_path: Path) -> bool:
    """Whether the server went away via `grag stop` rather than a crash.

    A graceful stop removes the pidfile (serve's finally block); a crash
    (SIGKILL, segfault) leaves it behind with a dead pid. The heal loop must
    only resurrect crashes — never override a deliberate stop.
    """
    from grag.admin import pidfile_path

    return not pidfile_path(db_path).exists()


async def _supervise(
    *,
    origin: str,
    ensure: Callable[[], Awaitable[str]],
    stopped: Callable[[], bool],
    http_client,
    stdio_read,
    stdio_write,
) -> None:
    """Relay stdio ↔ upstream until the client leaves, healing upstream losses.

    ``ensure`` readies the upstream and returns its mounted MCP path;
    ``stopped`` reports whether the upstream went away deliberately (never
    resurrected). Shared by the local and remote proxies.
    """
    handshake: dict = {}
    restarts = 0
    last_error: BaseException | None = None
    while True:
        mcp_path = await ensure()
        # Keep the origin fixed rather than resolving server-provided
        # metadata as a URL; _validated_mcp_path rejects overrides.
        mcp_url = f"{origin}{mcp_path}/"
        started = time.monotonic()
        try:
            side = await _relay_session(
                mcp_url, http_client, stdio_read, stdio_write, handshake
            )
            last_error = None
        except Exception as exc:  # noqa: BLE001 — upstream connect/relay failed
            side = "upstream"
            last_error = exc
        if side == "stdio":
            return  # MCP client disconnected — normal shutdown
        if stopped():
            # Return (not sys.exit) so the stdio context exits cleanly —
            # a SystemExit through the anyio task group surfaces to the
            # MCP client as an alarming BaseExceptionGroup traceback.
            print(
                "grag proxy: the server was stopped deliberately "
                "(grag stop); respecting that and exiting. Run "
                "'grag start' or reconnect the MCP server to resume.",
                file=sys.stderr,
            )
            return
        if time.monotonic() - started > _HEALTHY_SESSION_SECONDS:
            restarts = 0
        restarts += 1
        if restarts > _MAX_SESSION_RESTARTS:
            if last_error is not None:
                raise last_error
            print(
                f"grag proxy: upstream server kept failing after "
                f"{_MAX_SESSION_RESTARTS} restarts; giving up",
                file=sys.stderr,
            )
            return
        print(
            f"grag proxy: upstream server lost — reconnecting "
            f"({restarts}/{_MAX_SESSION_RESTARTS})",
            file=sys.stderr,
        )
        await asyncio.sleep(min(0.5 * restarts, 5.0))


async def run_proxy(db_path: Path, port: int, *, api_token: str | None = None) -> None:
    """Bridge stdio ↔ streamable-http, healing the upstream server on crashes."""
    import httpx2
    from mcp.server.stdio import stdio_server

    origin = f"http://127.0.0.1:{port}"
    # Ping the REST health endpoint — GET /mcp/ opens an SSE stream and hangs.
    health_url = f"{origin}/api/health"
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
    ):
        try:
            await _supervise(
                origin=origin,
                ensure=lambda: _ensure_server(db_path, port, health_url),
                stopped=lambda: _deliberately_stopped(db_path),
                http_client=http_client,
                stdio_read=stdio_read,
                stdio_write=stdio_write,
            )
        finally:
            # Close the write end so the stdout_writer task sees EndOfStream.
            close = getattr(stdio_write, "aclose", None)
            if close is not None:
                await close()


async def run_remote_proxy(
    server_url: str,
    *,
    api_token: str | None = None,
    db_name: str | None = None,
    allow_insecure: bool = False,
) -> None:
    """Bridge stdio ↔ a remote grag server; reconnect (never spawn) on loss."""
    import httpx2
    from mcp.server.stdio import stdio_server

    origin = validate_server_url(server_url, allow_insecure=allow_insecure)
    headers: dict[str, str] = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    if db_name:
        headers["x-grag-db"] = db_name
    pinned: dict = {}
    async with (
        httpx2.AsyncClient(
            headers=headers or None,
            # No redirects: a redirect could carry the bearer token to another
            # origin. The operator configures the final origin directly.
            follow_redirects=False,
            timeout=httpx2.Timeout(_MCP_DEFAULT_TIMEOUT, read=_MCP_SSE_READ_TIMEOUT),
            trust_env=True,
        ) as http_client,
        stdio_server() as (stdio_read, stdio_write),
    ):
        try:
            await _supervise(
                origin=origin,
                ensure=lambda: _ensure_remote_server(origin, pinned),
                stopped=lambda: False,
                http_client=http_client,
                stdio_read=stdio_read,
                stdio_write=stdio_write,
            )
        finally:
            close = getattr(stdio_write, "aclose", None)
            if close is not None:
                await close()
