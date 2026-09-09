"""CLI access to the selected graph, through its existing owner when available."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any

from typing_extensions import Self

if TYPE_CHECKING:
    from grag.service import GragService

import httpx2
from pydantic import BaseModel

from grag.config import GragConfig
from grag.core.errors import ConfigurationError, GragError
from grag.core.limits import validate_request

_ROUTES = {
    "describe_schema": ("GET", "/api/schema"),
    "define_schema": ("POST", "/api/schema/define"),
    "upsert_nodes": ("POST", "/api/nodes/upsert"),
    "search_knowledge": ("POST", "/api/search"),
    "get_context": ("POST", "/api/context"),
    "cypher_query": ("POST", "/api/query"),
    "ingest": ("POST", "/api/ingest"),
    "ingest_code": ("POST", "/api/ingest/code"),
}


class GraphClient:
    def __init__(self, config: GragConfig):
        self.config = config
        self.service: GragService | None = None
        self.http: httpx2.Client | None = None
        self.origin: str | None = None
        self.headers: dict[str, str] = {}
        self.expected: str | None = None
        self.capabilities: dict = {}
        self.target = str(config.db_path.expanduser().resolve())

    def __enter__(self) -> Self:
        from grag.admin import _http_origin, find_server
        from grag.config import database_identity
        from grag.proxy import validate_server_url

        if self.config.db_dir:
            raise ConfigurationError(
                "Select one graph with --db <file>; CLI graph commands do not choose an implicit --db-dir default."
            )
        if self.config.server_url:
            try:
                self.origin = validate_server_url(
                    self.config.server_url,
                    allow_insecure=self.config.allow_insecure_http,
                )
            except SystemExit as exc:
                raise ConfigurationError(str(exc)) from exc
            self.target = self.origin
            if self.config.server_db:
                self.headers["x-grag-db"] = self.config.server_db
                self.target += f" ({self.config.server_db})"
        else:
            db = self.config.db_path.expanduser().resolve()
            owner = find_server(db)
            owner_target = db
            if owner is None:
                owner_target = db.parent
                owner = find_server(owner_target)
                if owner:
                    self.headers["x-grag-db"] = db.stem
            if owner:
                self.origin = _http_origin(owner.host, owner.port)
                self.expected = database_identity(owner_target)
        if self.origin:
            if self.config.api_token:
                self.headers["Authorization"] = f"Bearer {self.config.api_token}"
            self.http = httpx2.Client(
                base_url=self.origin,
                headers=self.headers,
                timeout=120,
                trust_env=bool(self.config.server_url),
                follow_redirects=False,
            )
            try:
                self._verify_owner()
            except BaseException:
                self.http.close()
                raise
        else:
            from grag.service import GragService

            try:
                self.service = GragService(self.config)
            except RuntimeError as exc:
                if any(
                    word in str(exc).lower()
                    for word in ("lock", "another process", "error 33")
                ):
                    raise ConfigurationError(
                        f"Database is owned by another process: {self.target}",
                        hint="Use its shared 'grag serve --with-mcp' server. If it is a direct stdio MCP session, close that session and reconnect using 'grag init'; do not delete database sidecar files.",
                    ) from exc
                raise
        return self

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if self.http is None:
            raise RuntimeError("GraphClient must be opened before use")
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx2.HTTPError as exc:
            raise GragError(
                "Could not complete the request to the selected server.",
                hint="Check 'grag status' and the server log. A write may have committed; inspect the graph before retrying.",
            ) from exc
        if response.status_code >= 300:
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            raise GragError(
                str(body.get("error", f"Server returned HTTP {response.status_code}")),
                hint=body.get("hint")
                or "Check GRAG_API_TOKEN, the selected database, and the server log.",
            )
        return response.json()

    def _verify_owner(self) -> None:
        health = self._request("GET", "/api/health")
        actual = health.get("server_id") or health.get("database_id")
        if (
            health.get("status") != "ok"
            or not actual
            or (self.expected and actual != self.expected)
        ):
            raise ConfigurationError(
                "The selected server is unhealthy or its database identity changed; no graph request was sent."
            )
        self.expected = actual
        self.capabilities = health.get("capabilities", {})

    def call(self, operation: str, req: BaseModel | None = None) -> dict:
        if req is not None:
            validate_request(req)
        if self.service is not None:
            fn = getattr(self.service, operation)
            return (fn(req) if req is not None else fn()).model_dump(mode="json")
        self._verify_owner()
        if (
            operation in ("ingest", "ingest_code")
            and self.capabilities.get("ingestion_scope", 0) < 2
        ):
            raise ConfigurationError(
                "The running owner does not support the current ingestion scope policy.",
                hint="Restart that server with the updated grag installation, then retry. No ingestion request was sent.",
            )
        method, path = _ROUTES[operation]
        return self._request(
            method,
            path,
            **({"json": req.model_dump(mode="json")} if req is not None else {}),
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.http:
            self.http.close()
        if self.service:
            self.service.close()
