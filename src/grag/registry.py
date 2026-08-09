"""Per-process cache of GragService instances, keyed by resolved DB path.

Single-db mode (config.db_dir is None) serves one service for config.db_path.
Multi-db mode (config.db_dir set) resolves a short name like "project-a" to
db_dir / "project-a.lbdb". LadybugDB holds a per-process single-writer lock
per .lbdb file, so exactly one service per path is ever built and reused.
"""

from __future__ import annotations

import threading
from pathlib import Path

from grag.config import GragConfig
from grag.core.errors import ConfigurationError, NotFoundError
from grag.service import GragService


class ServiceRegistry:
    def __init__(self, config: GragConfig):
        self.config = config
        self._services: dict[str, GragService] = {}
        self._lock = threading.Lock()

    def get(self, db: str | None = None) -> GragService:
        if self.config.db_dir is None:
            if db and db.strip():
                raise ConfigurationError(
                    f"db='{db}' was passed, but this grag instance runs in "
                    "single-db mode (no db_dir configured).",
                    hint="Call get() with no db argument, or set GRAG_DB_DIR "
                    "to enable multi-db mode.",
                )
            return self._open_or_reuse(self.config.db_path)
        return self._open_or_reuse(self._resolve(db))

    def list_dbs(self) -> list[str]:
        if self.config.db_dir is None:
            return []
        root = self.config.db_dir.resolve()
        if not root.is_dir():
            return []
        return sorted(f.stem for f in root.glob("*.lbdb"))

    def close(self) -> None:
        with self._lock:
            services = list(self._services.values())
            self._services.clear()
        for svc in services:
            svc.close()

    # -- internals --------------------------------------------------------------

    def _open_or_reuse(self, path: Path) -> GragService:
        # ":memory:" must survive verbatim; resolving it would turn the
        # in-memory database into a cwd-relative file path.
        resolved = path if str(path) == ":memory:" else path.resolve()
        key = str(resolved)
        with self._lock:
            svc = self._services.get(key)
            if svc is None:
                svc = GragService(self.config.model_copy(update={"db_path": resolved}))
                self._services[key] = svc
        return svc

    def _resolve(self, db: str | None) -> Path:
        if self.config.db_dir is None:
            # _resolve is only called in multi-db mode; guard for direct use.
            raise ConfigurationError("db_dir is not configured.")
        root = self.config.db_dir.resolve()
        if db and db.strip():
            name = db.strip()
            p = Path(name)
            if p.is_absolute() or ".." in p.parts:
                raise ConfigurationError(
                    f"Invalid database name '{db}'.",
                    hint="Pass a plain database name within db_dir "
                    "(e.g. 'project-a'); absolute paths and '..' are not allowed.",
                )
            resolved = (root / f"{name}.lbdb").resolve()
            if not resolved.is_relative_to(root):
                raise ConfigurationError(
                    f"Invalid database name '{db}'.",
                    hint="Pass a plain database name within db_dir "
                    "(e.g. 'project-a'); absolute paths and '..' are not allowed.",
                )
            if resolved.exists():
                return resolved
            lowered = name.lower()
            for f in sorted(root.glob("*.lbdb")):
                if f.stem.lower() == lowered:
                    return f.resolve()
            available = ", ".join(self.list_dbs()) or "(none)"
            raise NotFoundError(
                f"No database named '{name}' in {root}.",
                hint=f"Available databases: {available}.",
            )
        preferred = root / self.config.db_path.name
        if preferred.is_file():
            return preferred.resolve()
        dbs = sorted(root.glob("*.lbdb"))
        if len(dbs) == 1:
            return dbs[0].resolve()
        available = ", ".join(f.stem for f in dbs) or "(none)"
        raise ConfigurationError(
            "No default database could be determined in multi-db mode.",
            hint=f"Pass a db name explicitly. Available databases: {available}.",
        )
