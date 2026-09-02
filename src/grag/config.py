"""Runtime configuration. Environment overrides use the GRAG_ prefix."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


def database_identity(path: Path) -> str:
    """Stable, non-reversible identity for a database path.

    Auto-serve proxies compare this value with ``/api/health`` before attaching,
    preventing one project's MCP client from silently reusing another project's
    server on the same port.
    """
    canonical = ":memory:" if str(path) == ":memory:" else str(path.resolve())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_port(path: Path) -> int:
    """Deterministic per-database port in [41000, 49151].

    ``grag init`` uses this as the default so two projects initialised on one
    machine get different ports instead of both claiming 8471 and colliding
    at auto-serve time. Derived from database_identity, so the same .lbdb
    path always yields the same port; the range stays below the common
    ephemeral-port floor (49152). A rare pair collision is caught at attach
    time by the identity check in grag.proxy and fixed with an explicit
    ``--port``.
    """
    return 41000 + int(database_identity(path)[:8], 16) % 8152


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


class EmbedderConfig(BaseModel):
    """Opt-in embedding provider. Without one, retrieval runs FTS-only."""

    provider: Literal["fastembed", "remote"]
    model: str = "BAAI/bge-small-en-v1.5"
    dim: int = 384
    base_url: str | None = None  # remote: OpenAI-compatible endpoint
    api_key_env: str | None = None  # remote: env var holding the API key


class GragConfig(BaseModel):
    db_path: Path = Path("knowledge.lbdb")
    # Multi-db root: when set, ServiceRegistry resolves short names to
    # db_dir / "<name>.lbdb" instead of serving only db_path.
    db_dir: Path | None = None
    buffer_pool_size: int = 256 * 1024 * 1024
    max_read_conns: int = 4
    default_query_limit: int = 100
    max_query_limit: int = 1000
    max_hops: int = 3
    default_token_budget: int = 2000
    statement_timeout_ms: int = 30_000
    # Per-label diversity cap: max seeds a single label may occupy in the fused
    # top_k before promoting other labels. Prevents one large table (e.g. code
    # Functions) from crowding out others. Set <= 0 to disable.
    search_label_cap: int = 2
    vector_codec: Literal["fp32", "int8", "binary", "polar"] = "fp32"
    embedder: EmbedderConfig | None = None
    # Mount the MCP streamable-http endpoint on the REST/UI server so one
    # process serves UI + REST + MCP against the same live .lbdb (single
    # writer satisfied; UI sees writes the moment they land). Off by default.
    mcp_path: str | None = None
    # Host the server binds to; drives the REST layer's Host-header allow-list
    # (DNS-rebinding guard) and the MCP endpoint's own allow-list. Defaults to
    # loopback.
    host: str = "127.0.0.1"
    # Bearer token required on /api/* (except /api/health) and the MCP mount
    # when set. Unset means the loopback trust model: anyone who can reach the
    # port directly is trusted, and browsers are kept out by the Host
    # allow-list + no cross-origin CORS. Set GRAG_API_TOKEN before binding a
    # non-loopback host.
    api_token: str | None = None
    # Extra CORS origins (e.g. a separately-hosted UI). Default is none: the
    # built-in UI is served same-origin, so browsers need no cross-origin
    # allowance at all.
    cors_origins: list[str] = Field(default_factory=list)
    # Max nodes embedded synchronously per search call. The remainder stays
    # pending (reported as SearchResponse.pending_embeddings) and drains over
    # later searches; ingest paths embed their own writes in full. Only used
    # when no background embedding worker is attached to the engine (a
    # serving process runs one; see grag.embedworker).
    max_embed_per_search: int = 256
    # Run a background embedding worker in serving processes so searches and
    # ingests never embed on the request thread. Off means the legacy inline
    # behaviour (embed-on-search, ingest embeds its own writes).
    embed_in_background: bool = True
    # Remote-server mode for the MCP proxy: when set, `grag mcp` bridges stdio
    # to an already-running grag server at this origin (e.g. a cloud host)
    # instead of auto-serving a local daemon. The proxy never opens a .lbdb.
    server_url: str | None = None
    # Database name sent as the x-grag-db header when the remote server runs
    # in multi-db (--db-dir) mode.
    server_db: str | None = None
    # Allow a plain-http (non-TLS) remote server_url on a non-loopback host.
    # The bearer token travels in clear text then — for trusted networks only.
    allow_insecure_http: bool = False

    @classmethod
    def from_env(cls) -> GragConfig:
        cfg = cls()
        if p := os.environ.get("GRAG_DB_PATH"):
            cfg.db_path = Path(p)
        if d := os.environ.get("GRAG_DB_DIR"):
            cfg.db_dir = Path(d)
        if mb := os.environ.get("GRAG_BUFFER_POOL_MB"):
            cfg.buffer_pool_size = int(mb) * 1024 * 1024
        if codec := os.environ.get("GRAG_VECTOR_CODEC"):
            cfg.vector_codec = codec  # type: ignore[assignment]
        if budget := os.environ.get("GRAG_TOKEN_BUDGET"):
            cfg.default_token_budget = int(budget)
        if cap := os.environ.get("GRAG_SEARCH_LABEL_CAP"):
            cfg.search_label_cap = int(cap)
        if token := os.environ.get("GRAG_API_TOKEN"):
            cfg.api_token = token
        if origins := os.environ.get("GRAG_CORS_ORIGINS"):
            cfg.cors_origins = [o.strip() for o in origins.split(",") if o.strip()]
        if cap := os.environ.get("GRAG_MAX_EMBED_PER_SEARCH"):
            cfg.max_embed_per_search = int(cap)
        if background := os.environ.get("GRAG_EMBED_BACKGROUND"):
            cfg.embed_in_background = _truthy(background)
        if url := os.environ.get("GRAG_SERVER_URL"):
            cfg.server_url = url
        if name := os.environ.get("GRAG_SERVER_DB"):
            cfg.server_db = name
        if insecure := os.environ.get("GRAG_ALLOW_INSECURE_HTTP"):
            cfg.allow_insecure_http = _truthy(insecure)
        if provider := os.environ.get("GRAG_EMBED_PROVIDER"):
            cfg.embedder = EmbedderConfig(
                provider=provider,  # type: ignore[arg-type]
                model=os.environ.get("GRAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
                dim=int(os.environ.get("GRAG_EMBED_DIM", "384")),
                base_url=os.environ.get("GRAG_EMBED_BASE_URL"),
                api_key_env=os.environ.get("GRAG_EMBED_API_KEY_ENV"),
            )
        return cfg
