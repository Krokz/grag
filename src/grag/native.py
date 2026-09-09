"""Load the Windows runtime shipped in grag's platform wheel.

Only grag-owned absolute paths are used. Never search another application's
OpenSSL installation or modify ladybug's package directory.
"""

from __future__ import annotations

import ctypes
import os
import threading
from importlib.metadata import version
from pathlib import Path
from typing import Any

from grag.core.errors import ConfigurationError

_LOCK = threading.Lock()
_HANDLES: list[Any] = []  # Keep DLLs alive for the lifetime of native connections.
_DIRECTORY = Path(__file__).parent / "_runtime"
_DLLS = ("libcrypto-3-x64.dll", "libssl-3-x64.dll")


def connection_backend(connection: Any) -> str:
    module = type(connection._connection).__module__
    return "capi" if module == "ladybug._lbug_capi" else "pybind" if module == "ladybug._lbug" else "unknown"


def configure_query_timeout(connection: Any, milliseconds: int) -> None:
    """Use the native deadline on each grag-owned connection, without Python timers.

    Ladybug 0.20.2/0.20.3's C-API wrapper interrupts after min(timeout, 10ms), and its
    outer Connection rejects two UNWIND ranges immediately when its Python flag
    is nonzero. The setter also configures the real native timeout. Clear ONLY
    those Python flags after the native setter succeeds; preserve native limits.
    No dependency files, global classes or unrelated connections are modified.
    """
    capi = connection_backend(connection) == "capi"
    if capi and version("ladybug") not in {"0.20.2", "0.20.3"}:
        raise ConfigurationError("Unverified Ladybug C-API timeout implementation",
                                 hint="Install grag's pinned Ladybug runtime before using the C-API backend.")
    try:
        connection.set_query_timeout(milliseconds)
        if capi:
            connection._query_timeout_ms = 0
            connection._connection._query_timeout_ms = 0
    except Exception as exc:
        raise ConfigurationError("Could not configure the native query timeout",
                                 hint="Restart with grag's supported Ladybug runtime; the requested execution bound was not installed.") from exc


def prepare_native_runtime() -> None:
    if os.name != "nt":
        return
    with _LOCK:
        if len(_HANDLES) == len(_DLLS):
            return
        try:
            for name in _DLLS[len(_HANDLES):]:
                path = (_DIRECTORY / name).resolve()
                if not path.is_file():
                    raise FileNotFoundError(f"Missing bundled runtime: {path}")
                # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32:
                # resolve dependencies beside this DLL or in the OS, never CWD/PATH.
                _HANDLES.append(ctypes.WinDLL(str(path), winmode=0x00000100 | 0x00000800))  # type: ignore[attr-defined]
        except OSError as exc:
            raise ConfigurationError(
                f"Windows native runtime could not load: {exc}",
                hint="Reinstall the gragdb Windows x64 wheel in this Python environment, "
                "then run grag doctor. For a source/editable install, first run "
                "python scripts/build_windows_runtime.py from an x64 Visual Studio "
                "Developer Command Prompt with Strawberry Perl installed.",
            ) from exc
