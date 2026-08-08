"""Performance smoke tests — guardrails for grag's lightness budget.

The hard product budgets (cold start < 2s, search p95 < 100ms @ 100k chunks,
RSS < 500MB) are measured at scale by `grag bench`. These tests assert scaled-
down versions that run in seconds and catch regressions in CI-sized envs.
Headroom is deliberate: shared/sandboxed CPUs are noisy.
"""

from __future__ import annotations

import resource
import statistics
import time

from grag.config import GragConfig
from grag.core.types import IngestDocument, IngestRequest, SearchRequest
from grag.service import GragService


def _service(tmp_path) -> GragService:
    return GragService(
        GragConfig(db_path=tmp_path / "perf.lbdb", buffer_pool_size=128 * 1024 * 1024)
    )


def test_cold_start_under_budget(tmp_path):
    start = time.perf_counter()
    svc = _service(tmp_path)
    svc.cypher_query  # attribute exists
    from grag.core.types import QueryRequest

    svc.cypher_query(QueryRequest(cypher="RETURN 1"))
    elapsed = time.perf_counter() - start
    svc.close()
    assert elapsed < 2.0, f"cold start {elapsed:.2f}s exceeds 2s budget"


def test_search_latency_and_rss(tmp_path):
    svc = _service(tmp_path)
    docs = [
        IngestDocument(
            text=f"note {i}: " + ("graph databases store relationships "
                                  "and enable grounded retrieval "
                                  "with provenance " * 10),
            source=f"note-{i}.md",
        )
        for i in range(500)
    ]
    svc.ingest(IngestRequest(documents=docs, chunk=True))

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # warm up: first search builds the FTS index — that's a write-path cost,
    # not query latency
    svc.search_knowledge(SearchRequest(query="graph", top_k=8, hops=1))

    latencies = []
    for i in range(20):
        start = time.perf_counter()
        res = svc.search_knowledge(
            SearchRequest(query=f"relationships grounded retrieval {i % 3}", top_k=8, hops=1)
        )
        latencies.append(time.perf_counter() - start)
        assert res.seeds, "expected FTS seeds"

    p95 = statistics.quantiles(latencies, n=20)[-1]
    rss_mb = (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before
    ) / 1024
    svc.close()

    assert p95 < 0.25, f"search p95 {p95*1000:.0f}ms exceeds 250ms guardrail"
    assert rss_mb < 400, f"RSS delta {rss_mb:.0f}MB exceeds 400MB guardrail"
