"""Document and file ingestion: chunking, batched node upserts, embeddings.

`ingest_documents` is the frozen library entry point (reached via
grag.service.GragService.ingest); `ingest_paths` backs the `grag ingest` CLI
command. Chunk nodes carry the raw `text` plus a JSON-encoded `meta` property
and `_source` provenance. Node keys are deterministic (`<source-slug>#NNNN`),
so re-ingesting the same document MERGEs over its own chunks — ingestion is
idempotent by construction.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pydantic import ValidationError

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import ConfigurationError
from grag.core.mutate import define_schema, upsert_nodes
from grag.core.types import (
    DefineSchemaRequest,
    IngestDocument,
    IngestRequest,
    IngestResponse,
    NodeTableSpec,
    PropertySpec,
    UpsertNode,
    UpsertNodesRequest,
)

log = logging.getLogger(__name__)

_CHUNK_TABLE_PROPS = [PropertySpec(name="text"), PropertySpec(name="meta")]

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")

_SUPPORTED_SUFFIXES = {".md", ".txt", ".json", ".jsonl"}


# --- public: ingest_documents ---------------------------------------------------


def ingest_documents(
    engine: Engine, config: GragConfig, req: IngestRequest
) -> IngestResponse:
    """Chunk documents and upsert them as nodes under `req.label`.

    The chunk table (id PK, text/meta, searchable) is defined idempotently on
    every call; all chunks go in via one batched upsert_nodes call.
    """
    define_schema(
        engine,
        config,
        DefineSchemaRequest(
            node_tables=[
                NodeTableSpec(
                    name=req.label,
                    primary_key="id",
                    properties=list(_CHUNK_TABLE_PROPS),
                    searchable=True,
                )
            ]
        ),
    )

    nodes: list[UpsertNode] = []
    for doc in req.documents:
        slug = _source_slug(doc.source)
        if req.chunk:
            chunks = _chunk_text(doc.text, req.chunk_size, req.chunk_overlap)
        else:
            chunks = [doc.text]
        for i, text in enumerate(chunks):
            props: dict[str, str] = {"text": text}
            if doc.metadata:
                props["meta"] = json.dumps(doc.metadata, sort_keys=True)
            nodes.append(
                UpsertNode(
                    label=req.label,
                    key=f"{slug}#{i:04d}",
                    properties=props,
                    source=doc.source,
                )
            )

    if nodes:
        upsert_nodes(engine, config, UpsertNodesRequest(nodes=nodes))
    _embed_pending(engine, config, req.label)
    return IngestResponse(label=req.label, nodes_created=len(nodes))


def _embed_pending(engine: Engine, config: GragConfig, label: str) -> None:
    """Embed freshly written nodes when an embedder is configured. FTS-only
    deployments (no optional embedding deps installed) are a supported mode,
    so ImportError/ConfigurationError degrade to a warning."""
    if config.embedder is None:
        return
    try:
        from grag.retrieval.vectors import embed_pending_nodes

        embed_pending_nodes(engine, config, label)
    except (ImportError, ConfigurationError) as exc:
        log.warning(
            "Embedding skipped for label '%s'; continuing FTS-only: %s", label, exc
        )


# --- public: ingest_paths (CLI) ---------------------------------------------------


def ingest_paths(config: GragConfig, paths: list[Path]) -> str:
    """Load .md/.txt/.json/.jsonl files and ingest them as chunked documents.

    Returns a human-readable summary for the CLI. Unreadable files and
    unsupported extensions are collected as warnings instead of failing the
    batch.
    """
    from grag.service import GragService

    documents: list[IngestDocument] = []
    warnings: list[str] = []
    files_read = 0
    for path in paths:
        suffix = path.suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            warnings.append(
                f"skipped {path}: unsupported extension '{suffix or '(none)'}' "
                f"(supported: {', '.join(sorted(_SUPPORTED_SUFFIXES))})"
            )
            continue
        try:
            loaded = _load_file(path, suffix)
        except FileNotFoundError:
            warnings.append(f"skipped {path}: file not found")
            continue
        except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
            warnings.append(f"skipped {path}: could not load ({exc})")
            continue
        documents.extend(loaded)
        files_read += 1

    service = GragService(config)
    try:
        resp = service.ingest(IngestRequest(documents=documents))
    finally:
        service.close()

    lines = [
        (
            f"Ingested {len(documents)} document(s) from {files_read} file(s): "
            f"{resp.nodes_created} node(s) written to label '{resp.label}' in {config.db_path}."
        )
    ]
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in warnings)
    return "\n".join(lines)


def _load_file(path: Path, suffix: str) -> list[IngestDocument]:
    if suffix in (".md", ".txt"):
        return [IngestDocument(text=path.read_text(encoding="utf-8"), source=str(path))]
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "documents" in data:
            data = data["documents"]
        if not isinstance(data, list):
            raise ValueError(
                ".json ingestion expects a list of {text, source?, metadata?} "
                "or an object with a 'documents' list"
            )
        return [IngestDocument.model_validate(item) for item in data]
    docs: list[IngestDocument] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped:
            docs.append(IngestDocument.model_validate(json.loads(stripped)))
    return docs


# --- chunking ---------------------------------------------------------------------


def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split `text` into chunks of at most `size` chars with `overlap` carry-over.

    Paragraphs (blank-line separated) are kept whole while they fit; oversized
    paragraphs fall back to sentence boundaries; oversized sentences are cut at
    the last space before the limit — mid-word only when a single word forces
    it. Overlap seeds each new chunk with a word-aligned tail of the previous
    one. Empty chunks are skipped.
    """
    text = text.strip()
    if not text:
        return []
    size = max(1, int(size))
    overlap = max(0, min(int(overlap), size - 1))

    chunks: list[str] = []
    for raw_para in _PARAGRAPH_RE.split(text):
        para = raw_para.strip()
        if not para:
            continue
        if len(para) <= size:
            _append_piece(chunks, para, "\n\n", size, overlap)
            continue
        for piece in _sentence_pieces(para, size):
            _append_piece(chunks, piece, " ", size, overlap)
    return [c for c in chunks if c.strip()]


def _append_piece(
    chunks: list[str], piece: str, sep: str, size: int, overlap: int
) -> None:
    if not chunks:
        chunks.append(piece)
        return
    current = chunks[-1]
    if len(current) + len(sep) + len(piece) <= size:
        chunks[-1] = current + sep + piece
        return
    tail = _fit_tail(_overlap_tail(current, overlap), size - len(piece) - 1)
    chunks.append(f"{tail} {piece}" if tail else piece)


def _overlap_tail(chunk: str, overlap: int) -> str:
    """Last <= overlap chars of `chunk`, advanced to a word boundary."""
    if overlap <= 0:
        return ""
    if len(chunk) <= overlap:
        return chunk
    tail = chunk[-overlap:]
    space = tail.find(" ")
    if space >= 0:
        tail = tail[space + 1 :]
    return tail.strip()


def _fit_tail(tail: str, budget: int) -> str:
    """Drop leading words until `tail` fits the remaining chunk budget."""
    while len(tail) > max(0, budget):
        space = tail.find(" ")
        if space < 0:
            return ""
        tail = tail[space + 1 :]
    return tail


def _sentence_pieces(paragraph: str, size: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for raw_sent in _SENTENCE_RE.split(paragraph):
        sent = raw_sent.strip()
        if not sent:
            continue
        if len(sent) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_hard_split(sent, size))
        elif not current:
            current = sent
        elif len(current) + 1 + len(sent) <= size:
            current = f"{current} {sent}"
        else:
            pieces.append(current)
            current = sent
    if current:
        pieces.append(current)
    return pieces


def _hard_split(text: str, size: int) -> list[str]:
    """Cut an overlong sentence at the last space before `size`; cut mid-word
    only when no space exists in the window."""
    out: list[str] = []
    rest = text
    while len(rest) > size:
        cut = rest.rfind(" ", 0, size + 1)
        if cut <= 0:
            cut = size
        out.append(rest[:cut])
        rest = rest[cut:].lstrip(" ")
    if rest:
        out.append(rest)
    return out


def _source_slug(source: str | None) -> str:
    """basename of `source` with spaces/odd chars collapsed to '-'."""
    if not source:
        return "doc"
    slug = _SLUG_RE.sub("-", Path(str(source)).name).strip("-")
    return slug or "doc"
