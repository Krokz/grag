"""Codec benchmark: recall@10 / latency / RSS for fp32, int8, binary, polar.

Synthetic deterministic corpus (no model downloads): n_docs documents spread
over ~8 latent topics, embedded with a local sha256-token-hash embedder in the
spirit of tests' FakeEmbedder — texts sharing tokens get correlated vectors,
so topical nearest neighbors exist to be found. Ground truth is exact fp32
cosine over all doc vectors in numpy; each codec runs against a fresh tmp db
through the real pipeline (define_schema -> upsert_nodes -> embed_pending_nodes
-> vector_candidates), so numbers include LadybugDB I/O, approximate candidate
scoring, and the exact rescore.

RSS delta is the growth of ru_maxrss (peak RSS) across the codec phase —
getrusage exposes only the high-water mark, so it overstates steady usage.
"""

from __future__ import annotations

import hashlib
import resource
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from grag.config import EmbedderConfig, GragConfig
from grag.core import mutate
from grag.core.engine import Engine
from grag.core.types import (
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.retrieval import vectors

ALL_CODECS = ("fp32", "int8", "binary", "polar")
N_QUERIES = 50
_UPSERT_BATCH = 250

_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("databases", ("graph", "cypher", "index", "query", "storage", "transaction",
                   "schema", "join", "table", "node", "edge", "traversal")),
    ("ml", ("embedding", "vector", "model", "training", "inference", "tensor",
            "gradient", "transformer", "attention", "tokenizer", "latency", "benchmark")),
    ("web", ("http", "server", "request", "response", "route", "handler",
             "middleware", "cookie", "session", "cache", "proxy", "tls")),
    ("devops", ("deploy", "container", "kubernetes", "pipeline", "build", "artifact",
                "rollback", "cluster", "monitoring", "alert", "uptime", "replica")),
    ("security", ("token", "encryption", "certificate", "vulnerability", "audit",
                  "permission", "firewall", "secret", "hash", "signature", "threat", "policy")),
    ("data", ("pipeline", "etl", "warehouse", "parquet", "stream", "batch",
              "schema", "ingest", "partition", "catalog", "lineage", "quality")),
    ("lang", ("compiler", "parser", "syntax", "runtime", "garbage", "collector",
              "bytecode", "typing", "macro", "closure", "iterator", "coroutine")),
    ("os", ("kernel", "scheduler", "memory", "page", "syscall", "thread",
            "mutex", "filesystem", "socket", "driver", "interrupt", "virtual")),
)
_FILLER = ("the", "of", "and", "in", "to", "for", "with", "on", "a", "an",
           "system", "using", "based", "modern", "efficient", "scalable",
           "distributed", "practical", "robust", "simple")


class HashEmbedder:
    """Deterministic local embedder: each token signs a few dims via sha256."""

    def __init__(self, dim: int):
        self.dim = dim
        self.model_id = f"bench-hash-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for tok in text.lower().split():
                d = hashlib.sha256(tok.encode()).digest()
                for j in range(3):
                    idx = d[j] % self.dim
                    sign = 1.0 if d[16 + j] % 2 == 0 else -1.0
                    v[idx] += sign
            out.append([float(x) for x in v])
        return out


def _build_corpus(n_docs: int, seed: int) -> list[tuple[int, str, str]]:
    """[(key, title, text)]; doc i belongs to topic i % len(_TOPICS)."""
    rng = np.random.default_rng(seed)
    docs = []
    for i in range(n_docs):
        name, vocab = _TOPICS[i % len(_TOPICS)]
        k = int(rng.integers(6, 11))
        words = [vocab[j] for j in rng.integers(0, len(vocab), size=k)]
        words += [_FILLER[j] for j in rng.integers(0, len(_FILLER), size=3)]
        rng.shuffle(words)
        docs.append((i, f"{name} note {i}", " ".join(words)))
    return docs


def _build_queries(n_queries: int, seed: int) -> list[str]:
    rng = np.random.default_rng(seed + 1)
    queries = []
    for qi in range(n_queries):
        _, vocab = _TOPICS[qi % len(_TOPICS)]
        words = [vocab[j] for j in rng.choice(len(vocab), size=4, replace=False)]
        words.append(_FILLER[rng.integers(0, len(_FILLER))])
        queries.append(" ".join(words))
    return queries


def _rss_mb() -> float:
    # ru_maxrss is KiB on Linux; it is a high-water mark (never decreases).
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _doc_vectors(embedder: HashEmbedder, docs: list[tuple[int, str, str]], dim: int) -> np.ndarray:
    # embed_pending_nodes concatenates STRING props in column order: title, text
    texts = [f"{title}\n{text}" for _, title, text in docs]
    return np.asarray(embedder.embed(texts), dtype=np.float32)


def _bench_codec(
    codec: str,
    base_config: GragConfig,
    embedder: HashEmbedder,
    docs: list[tuple[int, str, str]],
    doc_vecs: np.ndarray,
    queries: list[str],
    truth: list[set[str]],
    dim: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"grag-bench-{codec}-") as tmp:
        cfg = base_config.model_copy(deep=True)
        cfg.db_path = Path(tmp) / "bench.lbdb"
        cfg.vector_codec = codec  # type: ignore[assignment]
        cfg.embedder = EmbedderConfig(provider="fastembed", model=embedder.model_id, dim=dim)

        eng = Engine(cfg)
        orig_get_embedder = vectors.get_embedder
        vectors.get_embedder = lambda config: embedder  # inject local embedder
        try:
            mutate.define_schema(
                eng,
                cfg,
                DefineSchemaRequest(
                    node_tables=[
                        NodeTableSpec(
                            name="Doc",
                            primary_key="id",
                            properties=[
                                PropertySpec(name="id", type="INT64"),
                                PropertySpec(name="title", type="STRING"),
                                PropertySpec(name="text", type="STRING"),
                            ],
                        )
                    ]
                ),
            )
            for start in range(0, len(docs), _UPSERT_BATCH):
                batch = docs[start : start + _UPSERT_BATCH]
                mutate.upsert_nodes(
                    eng,
                    cfg,
                    UpsertNodesRequest(
                        nodes=[
                            UpsertNode(label="Doc", key=key, properties={"title": t, "text": x})
                            for key, t, x in batch
                        ]
                    ),
                )

            rss0 = _rss_mb()
            n_embedded = vectors.embed_pending_nodes(eng, cfg, "Doc")

            # encode latency + blob size, pure codec path (split + encode)
            enc_ns = 0
            code_bytes = 0
            for v in doc_vecs:
                t0 = time.perf_counter_ns()
                _, u = vectors.split_magnitude(v)
                blob = vectors.encode_direction(u, codec)
                enc_ns += time.perf_counter_ns() - t0
                code_bytes += len(blob)

            recalls, query_ms = [], []
            for qi, qtext in enumerate(queries):
                t0 = time.perf_counter()
                hits = vectors.vector_candidates(eng, cfg, qtext, None, 10)
                query_ms.append((time.perf_counter() - t0) * 1e3)
                got = {h.node.id for h in hits}
                recalls.append(len(got & truth[qi]) / 10.0)
            rss1 = _rss_mb()
        finally:
            vectors.get_embedder = orig_get_embedder
            eng.close()

    return {
        "recall_at_10": float(np.mean(recalls)),
        "mean_query_ms": float(np.mean(query_ms)),
        "mean_encode_us": float(enc_ns / len(doc_vecs) / 1e3),
        "rss_delta_mb": float(rss1 - rss0),
        "code_bytes": code_bytes // len(doc_vecs),
        "nodes_embedded": int(n_embedded),
    }


def run_bench(
    config: GragConfig,
    codec: str | None = None,
    n_docs: int = 1500,
    dim: int = 64,
    seed: int = 7,
) -> dict:
    """Benchmark vector codecs on a synthetic topical corpus. Returns the raw
    results dict and prints a formatted table (so `grag bench` is readable)."""
    codecs = [codec] if codec else list(ALL_CODECS)
    embedder = HashEmbedder(dim)
    docs = _build_corpus(n_docs, seed)
    queries = _build_queries(N_QUERIES, seed)

    doc_vecs = _doc_vectors(embedder, docs, dim)
    query_vecs = np.asarray(embedder.embed(queries), dtype=np.float32)

    # exact fp32 cosine ground truth over all vectors
    dv = doc_vecs / np.linalg.norm(doc_vecs, axis=1, keepdims=True)
    qv = query_vecs / np.linalg.norm(query_vecs, axis=1, keepdims=True)
    sims = qv @ dv.T
    truth = [
        {f"Doc:{docs[i][0]}" for i in np.argsort(-sims[qi])[:10]}
        for qi in range(len(queries))
    ]

    results: dict[str, Any] = {}
    for c in codecs:
        results[c] = _bench_codec(c, config, embedder, docs, doc_vecs, queries, truth, dim)

    out = {
        "params": {
            "n_docs": n_docs,
            "dim": dim,
            "seed": seed,
            "n_queries": len(queries),
            "codecs": codecs,
        },
        "results": results,
    }
    print(format_bench(out))
    return out


def format_bench(results: dict) -> str:
    """Render run_bench's dict as an aligned table."""
    p = results["params"]
    lines = [
        f"grag codec bench — n_docs={p['n_docs']} dim={p['dim']} "
        f"seed={p['seed']} queries={p['n_queries']}",
        f"{'codec':<8} {'recall@10':>10} {'query_ms':>10} {'encode_us':>10} "
        f"{'rss_delta_mb':>13} {'code_bytes':>11}",
    ]
    for c in p["codecs"]:
        r = results["results"][c]
        lines.append(
            f"{c:<8} {r['recall_at_10']:>10.4f} {r['mean_query_ms']:>10.2f} "
            f"{r['mean_encode_us']:>10.2f} {r['rss_delta_mb']:>13.1f} {r['code_bytes']:>11}"
        )
    return "\n".join(lines)
