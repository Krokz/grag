"""Embeddings, direction codecs, and lazy vector storage for grag retrieval.

Codec ladder (``config.vector_codec``):

    fp32   — exact vectors only; candidates come from the native HNSW index
             (QUERY_VECTOR_INDEX) with an exact numpy scan as fallback.
    int8   — direction quantized to int8 with a float32 scale header;
             candidates scored by int dot against the query, top 4*top_k
             rescored exactly against the stored fp32 embedding.
    binary — direction quantized to sign bits; candidates scored by hamming
             distance (converted to a cosine estimate), then rescored exactly.
    polar  — direction coded as quantized hyperspherical angles
             (grag.retrieval.polar); candidates decoded + dotted against the
             query, then rescored exactly. GRAG_POLAR_BITS_PER_DIM tunes the
             bit budget (default 1.0, comparable to binary).

Verified LadybugDB 0.19.1 behaviors relied on here:

    * UINT8[] columns bind python list[int] params (bytes are rejected as
      BLOB); reads come back as list[int]. BLOB columns bind/return bytes.
    * QUERY_VECTOR_INDEX with metric 'cosine' returns distance = 1 - cosine
      similarity in [0, 2]; identical direction ~0, orthogonal 1, opposite 2.
    * HNSW and FTS indexes pick up rows written after index creation.
    * numpy scalar/array types do not bind as params — convert via
      float()/int()/[float(x) ...] before binding.
    * Index existence must NOT be probed with a try-query: a failed
      QUERY_*_INDEX bind permanently poisons that connection's view of the
      index (even if the same connection creates it afterwards). Create-first
      with a tolerated "already exists" error is the safe existence check;
      the write connection's catalog is authoritative, so a final retry goes
      through execute_write.
"""

from __future__ import annotations

import os
import threading
import weakref
from typing import Any, Protocol

import numpy as np

from grag.config import EmbedderConfig, GragConfig
from grag.core.engine import Engine, node_record_from_value
from grag.core.errors import ConfigurationError, GragError, SchemaError
from grag.core.ident import validate_identifier
from grag.core.types import (
    EMB_CODE_PROP,
    EMB_MAGNITUDE_PROP,
    EMB_MODEL_PROP,
    EMBEDDING_PROP,
    META_TABLE,
    RESERVED_PREFIX,
    VECTOR_PROPS,
    ScoredNode,
)
from grag.retrieval import polar

# ---------------------------------------------------------------------------
# embedders
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    """Text embedder. Implementations must be deterministic per process."""

    dim: int
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastembedEmbedder:
    """Local embeddings via fastembed (optional dependency grag[embed-local])."""

    def __init__(self, cfg: EmbedderConfig):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ConfigurationError(
                "fastembed is not installed.",
                hint="Install the local embedding extra: pip install gragdb[embed-local]",
            ) from exc
        # threads: ONNX Runtime intra-op parallelism. onnxruntime 1.28.x
        # crashed (SIGSEGV) on macOS arm64 with one thread per core, even on
        # the first inference; 1.29+ is clean at 4-8 threads, so the default
        # is a modest min(4, cores) — set GRAG_EMBED_THREADS=1 to go back to
        # the conservative setting. enable_cpu_mem_arena=False: the arena
        # allocator was another source of instability on macOS.
        threads = cfg.threads if cfg.threads and cfg.threads > 0 else min(4, os.cpu_count() or 1)
        self._model = TextEmbedding(
            model_name=cfg.model,
            threads=threads,
            enable_cpu_mem_arena=False,
        )
        self.dim = cfg.dim
        self.model_id = cfg.model
        # Serialize Python-level calls: one inference at a time per process
        # (the worker's batches and a search's query embedding interleave).
        self._lock = threading.Lock()

    # ONNX pads every batch to its longest member, so one 500-token docstring
    # among 60 one-liners makes the whole batch cost 500 tokens each. Sorting
    # by length and keeping batches small cut padding waste: on real code
    # graphs (median 90 chars, p90 350, max 1000) this ran 3.5x faster than
    # fastembed's default batch of 256 in input order.
    _BATCH = 16

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        with self._lock:
            sorted_vecs = list(
                self._model.embed([texts[i] for i in order], batch_size=self._BATCH)
            )
        out: list[list[float]] = [[] for _ in texts]
        for position, vec in zip(order, sorted_vecs, strict=True):
            out[position] = [float(x) for x in vec]
        return out


class RemoteEmbedder:
    """OpenAI-compatible /embeddings endpoint (optional dep grag[embed-remote])."""

    def __init__(self, cfg: EmbedderConfig):
        try:
            import httpx
        except ImportError as exc:
            raise ConfigurationError(
                "httpx is not installed.",
                hint="Install the remote embedding extra: pip install gragdb[embed-remote]",
            ) from exc
        if not cfg.base_url:
            raise ConfigurationError(
                "embedder.base_url is required for provider='remote'.",
                hint="Set GRAG_EMBED_BASE_URL to an OpenAI-compatible endpoint root.",
            )
        self._httpx = httpx
        self._base_url = cfg.base_url.rstrip("/")
        self._model = cfg.model
        self._api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        self.dim = cfg.dim
        self.model_id = cfg.model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        resp = self._httpx.post(
            f"{self._base_url}/embeddings",
            json={"model": self._model, "input": texts},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda d: d.get("index", 0))
        return [[float(x) for x in d["embedding"]] for d in data]


_EMBEDDER_CACHE: dict[tuple, Embedder] = {}

# Retrieval prefixes by model family. Most modern embedders are trained
# asymmetrically: a short query and a long passage get different instructions,
# and skipping them costs measurable recall. Matched case-insensitively on a
# substring of the model id; first hit wins.
_BGE_QUERY = "Represent this sentence for searching relevant passages: "
_MODEL_PREFIXES: tuple[tuple[str, str, str], ...] = (
    ("bge-m3", "", ""),  # bge-m3 is symmetric
    ("bge-", _BGE_QUERY, ""),
    ("arctic-embed", _BGE_QUERY, ""),
    ("nomic-embed", "search_query: ", "search_document: "),
    ("e5-", "query: ", "passage: "),
    ("mxbai-embed", _BGE_QUERY, ""),
)


def model_prefixes(model: str) -> tuple[str, str]:
    """(query prefix, document prefix) a model family expects; ("", "") if none."""
    lowered = model.lower()
    for needle, query, document in _MODEL_PREFIXES:
        if needle in lowered:
            return query, document
    return "", ""


def resolve_prefixes(cfg: EmbedderConfig) -> tuple[str, str]:
    """Configured prefixes with model-family defaults for the unset ones."""
    auto_query, auto_doc = model_prefixes(cfg.model)
    query = cfg.query_prefix if cfg.query_prefix is not None else auto_query
    document = cfg.document_prefix if cfg.document_prefix is not None else auto_doc
    return query, document


def embed_text_props(
    cfg: EmbedderConfig, table: str, props: dict[str, str]
) -> list[str]:
    """STRING properties of `table` that form its embedding text.

    An explicit ``text_props[table]`` wins (unknown names are ignored so a
    schema change cannot break embedding); otherwise every non-reserved
    STRING prop minus ``exclude_props``. Falls back to all STRING props when
    exclusion would leave nothing, so a table made only of "excluded" names
    still embeds.
    """
    strings = [
        p
        for p, t in props.items()
        if t.upper() == "STRING" and not p.startswith(RESERVED_PREFIX) and p not in VECTOR_PROPS
    ]
    explicit = cfg.text_props.get(table)
    if explicit:
        chosen = [p for p in explicit if p in strings]
        if chosen:
            return chosen
    excluded = set(cfg.exclude_props)
    kept = [p for p in strings if p not in excluded]
    return kept or strings


def get_embedder(config: GragConfig) -> Embedder | None:
    """Process-cached embedder for config.embedder; None when unconfigured."""
    cfg = config.embedder
    if cfg is None:
        return None
    key = (cfg.provider, cfg.model, cfg.dim, cfg.base_url, cfg.api_key_env)
    if key not in _EMBEDDER_CACHE:
        if cfg.provider == "fastembed":
            _EMBEDDER_CACHE[key] = FastembedEmbedder(cfg)
        elif cfg.provider == "remote":
            _EMBEDDER_CACHE[key] = RemoteEmbedder(cfg)
        else:
            raise ConfigurationError(
                f"Unknown embedder provider {cfg.provider!r}.",
                hint="Supported providers: 'fastembed', 'remote'.",
            )
    return _EMBEDDER_CACHE[key]


# ---------------------------------------------------------------------------
# polar split + direction codecs
# ---------------------------------------------------------------------------

_CODECS = ("fp32", "int8", "binary", "polar")
_POPCOUNT = np.array([i.bit_count() for i in range(256)], dtype=np.uint8)


def _check_codec(codec: str) -> None:
    if codec not in _CODECS:
        raise ConfigurationError(
            f"Unknown vector codec {codec!r}.",
            hint=f"Supported codecs: {', '.join(_CODECS)}.",
        )


def _polar_bits_per_dim() -> float:
    """Angle-quantization budget for the polar codec (env-tunable)."""
    raw = os.environ.get("GRAG_POLAR_BITS_PER_DIM")
    if not raw:
        return polar.DEFAULT_BITS_PER_DIM
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"GRAG_POLAR_BITS_PER_DIM={raw!r} is not a number.",
            hint="Set GRAG_POLAR_BITS_PER_DIM to a float in (0, 8], e.g. 1.0.",
        ) from exc


def split_magnitude(v: np.ndarray) -> tuple[float, np.ndarray]:
    """Polar split: r = L2 norm, u = unit direction (zeros when r == 0)."""
    v = np.asarray(v, dtype=np.float32).ravel()
    r = float(np.linalg.norm(v))
    if r == 0.0:
        return 0.0, np.zeros_like(v)
    return r, v / r


def encode_direction(u: np.ndarray, codec: str) -> bytes:
    """Encode a unit direction vector.

    Blob layouts:
        fp32:  dim float32, raw.
        int8:  4-byte float32 scale (max abs of u) + dim int8 (round(u/scale*127)).
        binary: ceil(dim/8) bytes of sign bits (u >= 0 -> 1), np.packbits order.
        polar: 6-byte header (magic/version/dim/bits_per_dim) + packed angle
               codes; see grag.retrieval.polar.
    """
    _check_codec(codec)
    u = np.asarray(u, dtype=np.float32).ravel()
    if codec == "fp32":
        return u.tobytes()
    if codec == "int8":
        scale = float(np.max(np.abs(u))) if u.size else 0.0
        if scale == 0.0:
            q = np.zeros(u.shape, dtype=np.int8)
        else:
            q = np.round(u * (127.0 / scale)).clip(-127, 127).astype(np.int8)
        return np.float32(scale).tobytes() + q.tobytes()
    if codec == "polar":
        # polar codes a *direction*: refuse clearly non-unit input instead of
        # quantizing garbage (the zero vector from split_magnitude is allowed
        # through; its stored magnitude _emb_r = 0 annihilates it anyway).
        norm = float(np.linalg.norm(u))
        if norm > 1e-6 and abs(norm - 1.0) > 1e-2:
            raise ConfigurationError(
                f"The 'polar' codec encodes unit directions; got ||u||={norm:.6f}.",
                hint="Encode split_magnitude(v)[1] (the unit direction), not the raw vector.",
            )
        return polar.encode(polar.cartesian_to_angles(u), u.size, _polar_bits_per_dim())
    return np.packbits((u >= 0).astype(np.uint8)).tobytes()


def decode_direction(blob: bytes, codec: str, dim: int) -> np.ndarray:
    """Inverse of encode_direction (int8/binary are lossy approximations)."""
    _check_codec(codec)
    blob = bytes(blob)
    if codec == "fp32":
        arr = np.frombuffer(blob, dtype=np.float32)
        if arr.size != dim:
            raise ValueError(f"fp32 code has {arr.size} floats, expected dim={dim}.")
        return arr.copy()
    if codec == "int8":
        if len(blob) != 4 + dim:
            raise ValueError(f"int8 code has {len(blob)} bytes, expected {4 + dim}.")
        scale = float(np.frombuffer(blob[:4], dtype=np.float32)[0])
        q = np.frombuffer(blob[4:], dtype=np.int8)
        return q.astype(np.float32) * (scale / 127.0)
    if codec == "polar":
        return polar.reconstruct(blob, dim, _polar_bits_per_dim())
    nbytes = (dim + 7) // 8
    if len(blob) != nbytes:
        raise ValueError(f"binary code has {len(blob)} bytes, expected {nbytes}.")
    bits = np.unpackbits(np.frombuffer(blob, dtype=np.uint8))[:dim]
    v = bits.astype(np.float32) * 2.0 - 1.0
    n = float(np.linalg.norm(v))
    return v / n if n else v


def candidate_scores(codes: list[bytes], codec: str, u_query: np.ndarray) -> np.ndarray:
    """Approximate cosine scores of encoded directions against a unit query,
    without fully decoding: int8 uses an int dot rescaled by the blob header,
    binary uses hamming distance mapped through cos(pi * hamming_ratio),
    polar decodes each code and dots the reconstructed unit vector with the
    query (polar decode is a table gather + cumulative product, so full
    decoding is cheaper than a codebook-space score would be clever)."""
    _check_codec(codec)
    if not codes:
        return np.zeros(0, dtype=np.float32)
    uq = np.asarray(u_query, dtype=np.float32).ravel()
    dim = uq.size
    if codec == "fp32":
        M = np.stack([decode_direction(c, "fp32", dim) for c in codes])
        return (M @ uq).astype(np.float32)
    if codec == "int8":
        scales = np.array(
            [float(np.frombuffer(bytes(c[:4]), dtype=np.float32)[0]) for c in codes],
            dtype=np.float32,
        )
        Q = np.stack([np.frombuffer(bytes(c[4:]), dtype=np.int8) for c in codes])
        return (Q.astype(np.float32) @ uq) * (scales / 127.0)
    if codec == "polar":
        M = polar.reconstruct_many(
            [bytes(c) for c in codes], dim, _polar_bits_per_dim()
        )
        return (M @ uq).astype(np.float32)
    nbytes = (dim + 7) // 8
    C = np.stack([np.frombuffer(bytes(c), dtype=np.uint8) for c in codes])
    if C.shape[1] != nbytes:
        raise ValueError(f"binary codes have {C.shape[1]} bytes, expected {nbytes}.")
    qbits = np.packbits((uq >= 0).astype(np.uint8))
    mismatches = _POPCOUNT[np.bitwise_xor(C, qbits)].sum(axis=1)
    return np.cos(np.pi * (mismatches.astype(np.float32) / dim)).astype(np.float32)


# ---------------------------------------------------------------------------
# schema introspection (Agent A preferred, meta table / SHOW_TABLES fallback)
# ---------------------------------------------------------------------------


def _ident(name: str) -> str:
    return validate_identifier(name)


def _table_info(engine: Engine, table: str) -> list[list[Any]]:
    # TABLE_INFO does not bind params — identifier is validated instead.
    return engine.execute(f"CALL TABLE_INFO('{_ident(table)}') RETURN *").rows


def table_properties(engine: Engine, table: str) -> dict[str, str]:
    """Property name -> LadybugDB type string (e.g. 'STRING', 'FLOAT[384]')."""
    return {row[1]: row[2] for row in _table_info(engine, table)}


def _table_pk(engine: Engine, table: str) -> str | None:
    for row in _table_info(engine, table):
        if row[4]:
            return row[1]
    return None


def _show_tables(engine: Engine) -> list[tuple[str, str]]:
    """[(name, 'NODE'|'REL')] from SHOW_TABLES."""
    res = engine.execute("CALL SHOW_TABLES() RETURN *")
    return [(row[1], str(row[2]).upper()) for row in res.rows]


def node_tables(engine: Engine) -> list[str]:
    """All node table names, excluding grag-internal ("_"-prefixed) tables."""
    return [
        n
        for n, kind in _show_tables(engine)
        if kind == "NODE" and not n.startswith(RESERVED_PREFIX)
    ]


def pk_map_with_fallback(engine: Engine) -> dict[str, str]:
    """label -> primary key property. Prefers grag.core.schema.pk_map, then the
    META_TABLE registry, then per-table TABLE_INFO introspection."""
    try:
        from grag.core.schema import pk_map
    except ImportError:
        pass
    else:
        return pk_map(engine)
    out: dict[str, str] = {}
    try:
        res = engine.execute(f"MATCH (t:{META_TABLE}) RETURN t.name, t.kind, t.pk")
        out = {row[0]: row[2] for row in res.rows if row[1] == "node" and row[2]}
    except GragError:
        pass
    for name, kind in _show_tables(engine):
        if kind == "NODE" and name not in out:
            pk = _table_pk(engine, name)
            if pk:
                out[name] = pk
    return out


def searchable_node_tables(engine: Engine, config: GragConfig) -> list[str]:
    """Node tables flagged searchable. Prefers Agent A's schema document, then
    the META_TABLE registry; falls back to every node table."""
    try:
        from grag.core.schema import build_schema_document
    except ImportError:
        pass
    else:
        doc = build_schema_document(engine, config)
        names = [t.name for t in doc.node_tables if t.searchable]
        if names:
            return names
    try:
        res = engine.execute(
            f"MATCH (t:{META_TABLE}) RETURN t.name, t.kind, t.searchable"
        )
        if res.rows:
            return [row[0] for row in res.rows if row[1] == "node" and row[2]]
    except GragError:
        pass
    return node_tables(engine)


def candidate_tables(
    engine: Engine, config: GragConfig, labels: list[str] | None
) -> list[str]:
    """Tables to draw retrieval candidates from: an explicit label filter
    (unknown labels skipped) or all searchable tables."""
    if labels:
        existing = set(node_tables(engine))
        return [lbl for lbl in labels if lbl in existing]
    return searchable_node_tables(engine, config)


def string_props(engine: Engine, table: str) -> list[str]:
    """STRING properties usable as text (excludes reserved/vector columns)."""
    return [
        name
        for name, typ in table_properties(engine, table).items()
        if typ.upper() == "STRING"
        and not name.startswith(RESERVED_PREFIX)
        and name not in VECTOR_PROPS
    ]


# ---------------------------------------------------------------------------
# extension + index bookkeeping
# ---------------------------------------------------------------------------

_LOADED_EXTENSIONS: weakref.WeakKeyDictionary[Engine, set[str]] = (
    weakref.WeakKeyDictionary()
)


def _ensure_extension(engine: Engine, name: str) -> None:
    """LOAD an extension once per engine (LOAD is needed per process)."""
    loaded = _LOADED_EXTENSIONS.setdefault(engine, set())
    if name not in loaded:
        engine.load_extension(name)
        loaded.add(name)


def vector_index_name(table: str) -> str:
    return f"grag_vec__{_ident(table)}"


_VEC_INDEXES: weakref.WeakKeyDictionary[Engine, set[str]] = weakref.WeakKeyDictionary()


def _ensure_vector_index(engine: Engine, table: str, index: str) -> None:
    """Create the HNSW index once per engine. Create-first (duplicate error
    tolerated) because a failed QUERY_VECTOR_INDEX probe poisons the
    connection's catalog for that index name."""
    ensured = _VEC_INDEXES.setdefault(engine, set())
    if index in ensured:
        return
    try:
        engine.execute_write(
            f"CALL CREATE_VECTOR_INDEX('{_ident(table)}', '{index}', "
            f"'{EMBEDDING_PROP}', metric := 'cosine')"
        )
    except GragError as exc:
        if "already exists" not in str(exc):
            raise
    ensured.add(index)


# Which physical column type holds direction codes per (db, table) — UINT8[]
# preferred, BLOB fallback. Recorded for observability; writes/reads also
# re-check TABLE_INFO so this never gates correctness.
CODE_COLUMN_KINDS: dict[tuple[str, str], str] = {}


# ---------------------------------------------------------------------------
# vector storage + embedding writes
# ---------------------------------------------------------------------------


def ensure_vector_storage(engine: Engine, config: GragConfig, table: str) -> None:
    """Lazily ALTER TABLE ADD the four vector columns when embeddings are
    configured. Idempotent; no-op when config.embedder is None."""
    cfg = config.embedder
    if cfg is None:
        return
    _ident(table)
    existing = table_properties(engine, table)
    want_embedding = f"FLOAT[{int(cfg.dim)}]"
    if EMBEDDING_PROP in existing:
        if existing[EMBEDDING_PROP].upper() != want_embedding:
            raise ConfigurationError(
                f"Table '{table}' already has {EMBEDDING_PROP} {existing[EMBEDDING_PROP]} "
                f"but config.embedder.dim={cfg.dim}.",
                hint="Align GRAG_EMBED_DIM with the stored vectors, or drop the column and re-embed.",
            )
    else:
        engine.execute_write(
            f"ALTER TABLE {_ident(table)} ADD {EMBEDDING_PROP} {want_embedding}"
        )
    if EMB_MAGNITUDE_PROP not in existing:
        engine.execute_write(
            f"ALTER TABLE {_ident(table)} ADD {EMB_MAGNITUDE_PROP} DOUBLE"
        )
    if EMB_CODE_PROP not in existing:
        try:
            engine.execute_write(
                f"ALTER TABLE {_ident(table)} ADD {EMB_CODE_PROP} UINT8[]"
            )
            kind = "UINT8[]"
        except GragError:
            engine.execute_write(
                f"ALTER TABLE {_ident(table)} ADD {EMB_CODE_PROP} BLOB"
            )
            kind = "BLOB"
        CODE_COLUMN_KINDS[(str(config.db_path), table)] = kind
    if EMB_MODEL_PROP not in existing:
        engine.execute_write(f"ALTER TABLE {_ident(table)} ADD {EMB_MODEL_PROP} STRING")


def embed_pending_nodes(
    engine: Engine,
    config: GragConfig,
    table: str,
    batch_size: int = 128,
    max_nodes: int | None = None,
) -> int:
    """Embed nodes in `table` whose embedding is NULL.

    Embed text is the concatenation of the node's STRING properties
    (excluding reserved/vector columns). Each node gets the full fp32 vector,
    its polar magnitude, the encoded direction (config.vector_codec) and the
    embedder model id. Returns the number of nodes embedded.

    max_nodes caps the total embedded in this call (the search path uses it
    to bound request latency); None drains the whole backlog (ingest paths).
    """
    if max_nodes is not None and max_nodes <= 0:
        return 0
    cfg = config.embedder
    if cfg is None:
        return 0
    embedder = get_embedder(config)
    if embedder is None:
        return 0
    _check_codec(config.vector_codec)
    _ident(table)
    ensure_vector_storage(engine, config, table)
    props = table_properties(engine, table)
    pk = pk_map_with_fallback(engine).get(table)
    if not pk:
        raise SchemaError(
            f"Cannot embed table '{table}': primary key unknown.",
            hint="Define the table via define_schema so grag can key embedding writes.",
        )
    text_props = embed_text_props(cfg, table, props)
    _, document_prefix = resolve_prefixes(cfg)
    code_kind = props.get(EMB_CODE_PROP, "UINT8[]").upper()
    projection = f"n.{_ident(pk)}"
    if text_props:
        projection += ", " + ", ".join(f"n.{_ident(p)}" for p in text_props)
    batch_size = max(1, int(batch_size))
    total = 0
    while True:
        limit = batch_size
        if max_nodes is not None:
            limit = min(limit, max_nodes - total)
        res = engine.execute(
            f"MATCH (n:{_ident(table)}) WHERE n.{EMBEDDING_PROP} IS NULL "
            f"RETURN {projection} LIMIT {limit}"
        )
        if not res.rows:
            return total
        keys = [row[0] for row in res.rows]
        texts = []
        for row, key in zip(res.rows, keys, strict=True):
            parts = [str(v) for v in row[1:] if v is not None]
            texts.append(document_prefix + ("\n".join(parts) if parts else str(key)))
        vectors = embedder.embed(texts)
        if len(vectors) != len(keys):
            raise ConfigurationError(
                f"Embedder returned {len(vectors)} vectors for {len(keys)} texts.",
                hint="Embedder.embed must return one vector per input text.",
            )
        for key, vec in zip(keys, vectors, strict=True):
            v = np.asarray(vec, dtype=np.float32).ravel()
            if v.size != cfg.dim:
                raise ConfigurationError(
                    f"Embedder produced {v.size}-dim vectors but config.embedder.dim={cfg.dim}.",
                    hint="Set GRAG_EMBED_DIM to the model's output dimension.",
                )
            r, u = split_magnitude(v)
            blob = encode_direction(u, config.vector_codec)
            code_param: Any = (
                [int(b) for b in blob] if code_kind.startswith("UINT8") else bytes(blob)
            )
            engine.execute_write(
                f"MATCH (n:{_ident(table)} {{{_ident(pk)}: $key}}) SET "
                f"n.{EMBEDDING_PROP} = $emb, n.{EMB_MAGNITUDE_PROP} = $r, "
                f"n.{EMB_CODE_PROP} = $code, n.{EMB_MODEL_PROP} = $model",
                {
                    "key": key,
                    "emb": [float(x) for x in v],
                    "r": float(r),
                    "code": code_param,
                    "model": embedder.model_id,
                },
            )
        total += len(keys)
        if max_nodes is not None and total >= max_nodes:
            return total


def pending_embedding_count(engine: Engine, config: GragConfig, table: str) -> int:
    """Nodes in `table` still awaiting an embedding. 0 when no embedder is
    configured or the table has no vector columns yet."""
    if config.embedder is None:
        return 0
    if EMBEDDING_PROP not in table_properties(engine, table):
        return 0
    res = engine.execute(
        f"MATCH (n:{_ident(table)}) WHERE n.{EMBEDDING_PROP} IS NULL RETURN count(n)"
    )
    return int(res.rows[0][0]) if res.rows else 0


def reindex_embeddings(
    engine: Engine,
    config: GragConfig,
    table: str,
    batch_size: int = 128,
) -> int:
    """Drop the HNSW vector index, clear all embeddings, and re-embed all nodes.

    Safe to call after WAL recovery when the HNSW index may reference nodes
    whose embedding writes were rolled back. Dropping the index before any write
    to the embedding column prevents LadybugDB's HNSW auto-maintenance from
    dereferencing stale NULL embedding handles — the mechanism that causes
    SIGSEGV in simsimd_cos_f32_neon.

    Sequence: drop index → clear embeddings → embed all nodes (no HNSW fires
    during writes) → recreate index from clean data.

    Returns the number of nodes re-embedded; 0 if no embedder is configured.
    """
    if config.embedder is None:
        return 0
    _ident(table)
    _ensure_extension(engine, "VECTOR")
    index = vector_index_name(table)
    try:
        engine.execute_write(f"CALL DROP_VECTOR_INDEX('{_ident(table)}', '{index}')")
    except GragError as exc:
        # Tolerate "doesn't have an index with name" (index never existed or
        # was already dropped) — the goal is to ensure it's gone before writes.
        if (
            "doesn't have" not in str(exc).lower()
            and "not exist" not in str(exc).lower()
        ):
            raise
    # Invalidate the process-local set so _ensure_vector_index recreates the index.
    ensured = _VEC_INDEXES.get(engine)
    if ensured is not None:
        ensured.discard(index)
    ensure_vector_storage(engine, config, table)
    props = table_properties(engine, table)
    to_clear = [
        p
        for p in (EMBEDDING_PROP, EMB_MAGNITUDE_PROP, EMB_CODE_PROP, EMB_MODEL_PROP)
        if p in props
    ]
    if to_clear:
        sets = ", ".join(f"n.{_ident(p)} = NULL" for p in to_clear)
        engine.execute_write(f"MATCH (n:{_ident(table)}) SET {sets}")
    n = embed_pending_nodes(
        engine, config, table, batch_size=batch_size, max_nodes=None
    )
    if config.vector_codec == "fp32" and n > 0:
        _ensure_vector_index(engine, table, index)
    return n


# ---------------------------------------------------------------------------
# candidate retrieval
# ---------------------------------------------------------------------------


def vector_candidates(
    engine: Engine,
    config: GragConfig,
    query_text: str,
    labels: list[str] | None,
    top_k: int,
) -> list[ScoredNode]:
    """Cosine-similarity candidates for query_text. Returns [] when no
    embedder is configured (FTS-only mode). Lazily provisions vector columns
    and embeds pending nodes on candidate tables — capped at
    config.max_embed_per_search per call so a first query after a large
    ingest doesn't embed the whole table on the request thread (the backlog
    drains over subsequent searches; see SearchResponse.pending_embeddings)."""
    cfg = config.embedder
    if cfg is None:
        return []
    embedder = get_embedder(config)
    if embedder is None:
        return []
    top_k = max(1, int(top_k))
    tables = candidate_tables(engine, config, labels)
    if not tables:
        return []
    from grag.embedworker import attached_worker

    worker = attached_worker(engine)
    for table in tables:
        ensure_vector_storage(engine, config, table)
        if worker is None:
            embed_pending_nodes(
                engine, config, table, max_nodes=config.max_embed_per_search
            )
    if worker is not None:
        # Never embed on the request thread when a worker exists; just make
        # sure it is awake so the reported backlog shrinks.
        worker.wake()
    query_prefix, _ = resolve_prefixes(cfg)
    q = np.asarray(
        embedder.embed([query_prefix + query_text])[0], dtype=np.float32
    ).ravel()
    if q.size != cfg.dim:
        raise ConfigurationError(
            f"Embedder produced a {q.size}-dim query vector but config.embedder.dim={cfg.dim}.",
            hint="Set GRAG_EMBED_DIM to the model's output dimension.",
        )
    r_q, u_q = split_magnitude(q)
    if r_q == 0.0:
        return []
    pk = pk_map_with_fallback(engine)
    results: list[ScoredNode] = []
    for table in tables:
        if config.vector_codec == "fp32":
            results.extend(_fp32_candidates(engine, table, q, top_k, pk))
        else:
            _check_codec(config.vector_codec)
            results.extend(
                _codec_candidates(engine, table, config.vector_codec, u_q, q, top_k, pk)
            )
    results.sort(key=lambda s: s.score, reverse=True)
    return results[:top_k]


def _cosine_scores(E: np.ndarray, q: np.ndarray) -> np.ndarray:
    qn = float(np.linalg.norm(q))
    if qn == 0.0 or E.shape[0] == 0:
        return np.zeros(E.shape[0], dtype=np.float32)
    denom = np.linalg.norm(E, axis=1) * qn
    denom = np.where(denom == 0.0, 1.0, denom)
    return ((E @ q) / denom).astype(np.float32)


def _query_vector_index(
    engine: Engine, table: str, index: str, q: np.ndarray, k: int, write: bool = False
):
    cypher = (
        f"CALL QUERY_VECTOR_INDEX('{_ident(table)}', '{index}', $q, $k) "
        "RETURN node, distance"
    )
    params = {"q": [float(x) for x in q], "k": int(k)}
    if write:
        return engine.execute_write(cypher, params)
    return engine.execute(cypher, params)


def _fp32_candidates(
    engine: Engine, table: str, q: np.ndarray, top_k: int, pk: dict[str, str]
) -> list[ScoredNode]:
    try:
        _ensure_extension(engine, "VECTOR")
        index = vector_index_name(table)
        _ensure_vector_index(engine, table, index)
        try:
            res = _query_vector_index(engine, table, index, q, top_k)
        except GragError:
            # reader may hold a stale catalog; the write connection is authoritative
            res = _query_vector_index(engine, table, index, q, top_k, write=True)
    except GragError:
        return _exact_scan(engine, table, q, top_k, pk)
    out = []
    for node_val, dist in res.rows:
        score = max(-1.0, min(1.0, 1.0 - float(dist)))
        out.append(
            ScoredNode(
                node=node_record_from_value(node_val, pk), score=score, match="vector"
            )
        )
    return out


def _fetch_nodes_by_keys(
    engine: Engine, table: str, pk_prop: str, keys: list[Any]
) -> list[dict]:
    """Full node rows for a primary-key shortlist (the exact-rescore pass)."""
    if not keys:
        return []
    res = engine.execute(
        f"MATCH (n:{_ident(table)}) WHERE n.{_ident(pk_prop)} IN $keys RETURN n",
        {"keys": list(keys)},
    )
    return [row[0] for row in res.rows]


def _exact_scan(
    engine: Engine, table: str, q: np.ndarray, top_k: int, pk: dict[str, str]
) -> list[ScoredNode]:
    """HNSW-unavailable fallback: exact cosine over the table, two-phase.

    Phase 1 ships only (pk, fp32 vector) per row — full node records are
    fetched for the top_k winners only."""
    pk_prop = pk.get(table)
    if not pk_prop:
        # Unreachable via embed_pending_nodes (it requires a pk to key
        # embedding writes); guards against externally-written columns.
        return []
    res = engine.execute(
        f"MATCH (n:{_ident(table)}) WHERE n.{EMBEDDING_PROP} IS NOT NULL "
        f"RETURN n.{_ident(pk_prop)}, n.{EMBEDDING_PROP}"
    )
    keys, vecs = [], []
    for key, emb in res.rows:
        if emb is None:
            continue
        keys.append(key)
        vecs.append(np.asarray(emb, dtype=np.float32))
    if not vecs:
        return []
    scores = _cosine_scores(np.stack(vecs), q)
    order = np.argsort(-scores)[:top_k]
    by_key = {
        nv.get(pk_prop): nv
        for nv in _fetch_nodes_by_keys(engine, table, pk_prop, [keys[i] for i in order])
    }
    out = []
    for i in order:
        nv = by_key.get(keys[i])
        if nv is not None:
            out.append(
                ScoredNode(
                    node=node_record_from_value(nv, pk),
                    score=float(scores[i]),
                    match="vector",
                )
            )
    return out


def _codec_candidates(
    engine: Engine,
    table: str,
    codec: str,
    u_q: np.ndarray,
    q: np.ndarray,
    top_k: int,
    pk: dict[str, str],
) -> list[ScoredNode]:
    """Approximate scoring over stored direction codes, then exact rescore of
    the top 4*top_k against the fp32 embeddings.

    The approximate pass is inherently O(table) — codec candidate quality is
    the property grag bench measures, so no ANN index is involved — but it
    ships only (pk, code) per row; full nodes + fp32 vectors are fetched for
    the shortlist only."""
    pk_prop = pk.get(table)
    if not pk_prop:
        return []  # see _exact_scan
    res = engine.execute(
        f"MATCH (n:{_ident(table)}) WHERE n.{EMB_CODE_PROP} IS NOT NULL "
        f"RETURN n.{_ident(pk_prop)}, n.{EMB_CODE_PROP}"
    )
    keys, codes = [], []
    for key, raw_code in res.rows:
        if raw_code is None:
            continue
        keys.append(key)
        codes.append(bytes(raw_code))
    if not codes:
        return []
    approx = candidate_scores(codes, codec, u_q)
    pre = np.argsort(-approx)[: 4 * top_k]
    nodes, vecs = [], []
    for nv in _fetch_nodes_by_keys(engine, table, pk_prop, [keys[i] for i in pre]):
        emb = nv.get(EMBEDDING_PROP)
        if emb is None:
            continue
        nodes.append(nv)
        vecs.append(np.asarray(emb, dtype=np.float32))
    if not vecs:
        return []
    exact = _cosine_scores(np.stack(vecs), q)
    order = np.argsort(-exact)[:top_k]
    return [
        ScoredNode(
            node=node_record_from_value(nodes[i], pk),
            score=float(exact[i]),
            match="vector",
        )
        for i in order
    ]
