"""Compare embedding models on YOUR graph: recall@k, MRR and latency per model.

Usage:
    python examples/embedding_eval.py --export graph.jsonl --questions q.jsonl \
        --models fastembed:BAAI/bge-small-en-v1.5:384 \
                 fastembed:nomic-ai/nomic-embed-text-v1.5:768 \
                 fastembed:jinaai/jina-embeddings-v2-base-code:768 \
                 remote:text-embedding-3-large:3072

    # no hand-written questions yet? derive a proxy set from Section nodes
    # (query = section title, expected = that section's chunks):
    python examples/embedding_eval.py --export graph.jsonl --auto 40 --models ...

`--export` is a `grag export` JSONL (or `grag export --url` from the live
server). Each model gets a fresh temporary database: the export is imported,
every searchable table is embedded (timed), then each question is answered
three ways — vector-only, FTS-only and grag's hybrid search — so you can see
what the model adds over BM25 on your own data rather than on a leaderboard.

Questions file: one JSON object per line, {"query": "...", "expect": [...]}
where `expect` holds canonical node ids ("Chunk:doc-…#3-2-retry@0000") or
substrings of them ("#3-2-retry"); a question is a hit when any expected item
matches any returned seed id.

Remote models need GRAG_EMBED_BASE_URL and GRAG_EMBED_API_KEY_ENV in the
environment (same as serving). Switching the server afterwards is
GRAG_EMBED_MODEL + GRAG_EMBED_DIM and a `grag reindex`.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

from grag.config import EmbedderConfig, GragConfig
from grag.core.engine import Engine
from grag.core.types import SearchRequest
from grag.retrieval.search import _fts_seeds, search_knowledge
from grag.retrieval.vectors import (
    embed_pending_nodes,
    pk_map_with_fallback,
    searchable_node_tables,
    vector_candidates,
)
from grag.transfer import import_from


def _parse_model(spec: str) -> EmbedderConfig:
    try:
        provider, model, dim = spec.rsplit(":", 2)
        if provider not in ("fastembed", "remote"):
            raise ValueError(provider)
        return EmbedderConfig(
            provider=provider,  # type: ignore[arg-type]
            model=model,
            dim=int(dim),
            base_url=os.environ.get("GRAG_EMBED_BASE_URL"),
            api_key_env=os.environ.get("GRAG_EMBED_API_KEY_ENV"),
        )
    except Exception:  # noqa: BLE001
        sys.exit(f"bad --models entry {spec!r}: expected provider:model:dim")


def _load_questions(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            expect = q["expect"] if isinstance(q["expect"], list) else [q["expect"]]
            out.append({"query": q["query"], "expect": [str(e) for e in expect]})
    return out


def _auto_questions(engine: Engine, n: int) -> list[dict]:
    """Proxy questions from a sections ingest: title -> that section's chunks."""
    rows = engine.execute(
        "MATCH (c:Chunk)-[:IN_SECTION]->(s:Section) WHERE s.level >= 2 "
        "RETURN s.id, s.title, collect(c.id)"
    ).rows
    if not rows:
        sys.exit("--auto needs a `grag ingest --sections` graph (Section/IN_SECTION)")
    rows.sort(key=lambda r: r[0])
    step = max(1, len(rows) // n)
    return [
        {"query": str(title), "expect": [f"Chunk:{cid}" for cid in chunk_ids]}
        for _sid, title, chunk_ids in rows[::step][:n]
    ]


def _hit_rank(seed_ids: list[str], expect: list[str]) -> int | None:
    for rank, sid in enumerate(seed_ids, 1):
        if any(e == sid or e in sid for e in expect):
            return rank
    return None


def _score(ranks: list[int | None], k: int) -> tuple[float, float]:
    recall = sum(1 for r in ranks if r is not None and r <= k) / max(1, len(ranks))
    mrr = sum(1.0 / r for r in ranks if r is not None) / max(1, len(ranks))
    return recall, mrr


def evaluate(export: Path, questions: list[dict], cfg: EmbedderConfig, k: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        config = GragConfig(db_path=Path(tmp) / "eval.lbdb", embedder=cfg)
        engine = Engine(config)
        try:
            with open(export, encoding="utf-8") as fh:
                import_from(engine, config, fh)
            t0 = time.perf_counter()
            embedded = sum(
                embed_pending_nodes(engine, config, t)
                for t in searchable_node_tables(engine, config)
            )
            embed_s = time.perf_counter() - t0
            pk = pk_map_with_fallback(engine)
            tables = searchable_node_tables(engine, config)
            ranks = {"vector": [], "fts": [], "hybrid": []}
            latency = []
            for q in questions:
                t1 = time.perf_counter()
                vec = vector_candidates(engine, config, q["query"], None, k)
                latency.append(time.perf_counter() - t1)
                ranks["vector"].append(_hit_rank([s.node.id for s in vec], q["expect"]))
                fts = []
                for t in tables:
                    fts.extend(_fts_seeds(engine, t, q["query"], k, pk))
                fts.sort(key=lambda s: s.score, reverse=True)
                ranks["fts"].append(_hit_rank([s.node.id for s in fts[:k]], q["expect"]))
                hyb = search_knowledge(
                    engine, config, SearchRequest(query=q["query"], top_k=k, hops=0)
                )
                ranks["hybrid"].append(_hit_rank([s.node.id for s in hyb.seeds], q["expect"]))
        finally:
            engine.close()
    out = {"model": cfg.model, "embedded": embedded, "embed_s": embed_s,
           "query_ms": 1000 * statistics.median(latency) if latency else 0.0}
    for mode, rs in ranks.items():
        out[f"{mode}_recall"], out[f"{mode}_mrr"] = _score(rs, k)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", required=True, type=Path, help="grag export JSONL of the graph")
    ap.add_argument("--questions", type=Path, help="JSONL of {query, expect}")
    ap.add_argument("--auto", type=int, default=0, help="derive N proxy questions from Section nodes")
    ap.add_argument("--models", nargs="+", required=True, help="provider:model:dim ...")
    ap.add_argument("--top-k", type=int, default=8)
    args = ap.parse_args()
    if not args.questions and not args.auto:
        ap.error("give --questions FILE or --auto N")

    questions = _load_questions(args.questions) if args.questions else []
    if args.auto:
        with tempfile.TemporaryDirectory() as tmp:
            config = GragConfig(db_path=Path(tmp) / "q.lbdb")
            engine = Engine(config)
            try:
                with open(args.export, encoding="utf-8") as fh:
                    import_from(engine, config, fh)
                questions += _auto_questions(engine, args.auto)
            finally:
                engine.close()
    print(f"{len(questions)} question(s), top_k={args.top_k}\n")
    header = f"{'model':44} {'embed s':>8} {'q ms':>6} | {'vec R@k':>7} {'MRR':>5} | {'fts R@k':>7} {'MRR':>5} | {'hyb R@k':>7} {'MRR':>5}"
    print(header)
    print("-" * len(header))
    for spec in args.models:
        r = evaluate(args.export, questions, _parse_model(spec), args.top_k)
        print(
            f"{r['model'][:44]:44} {r['embed_s']:8.1f} {r['query_ms']:6.0f} | "
            f"{r['vector_recall']:7.2f} {r['vector_mrr']:5.2f} | "
            f"{r['fts_recall']:7.2f} {r['fts_mrr']:5.2f} | "
            f"{r['hybrid_recall']:7.2f} {r['hybrid_mrr']:5.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
