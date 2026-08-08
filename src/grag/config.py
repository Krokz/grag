"""Runtime configuration. Environment overrides use the GRAG_ prefix."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


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
    vector_codec: Literal["fp32", "int8", "binary", "polar"] = "fp32"
    embedder: EmbedderConfig | None = None

    @classmethod
    def from_env(cls) -> "GragConfig":
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
        if provider := os.environ.get("GRAG_EMBED_PROVIDER"):
            cfg.embedder = EmbedderConfig(
                provider=provider,  # type: ignore[arg-type]
                model=os.environ.get("GRAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
                dim=int(os.environ.get("GRAG_EMBED_DIM", "384")),
                base_url=os.environ.get("GRAG_EMBED_BASE_URL"),
                api_key_env=os.environ.get("GRAG_EMBED_API_KEY_ENV"),
            )
        return cfg
