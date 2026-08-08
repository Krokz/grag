"""run_bench smoke test: small corpus, one codec, checks the result contract."""

from __future__ import annotations

from grag.config import GragConfig
from grag.retrieval.bench import format_bench, run_bench


def test_run_bench_int8_small(tmp_path, capsys):
    cfg = GragConfig(db_path=tmp_path / "bench.lbdb")  # run_bench uses its own tmp dbs
    out = run_bench(cfg, codec="int8", n_docs=200, dim=32)

    assert out["params"]["n_docs"] == 200
    assert out["params"]["dim"] == 32
    assert out["params"]["codecs"] == ["int8"]
    r = out["results"]["int8"]
    assert 0.0 <= r["recall_at_10"] <= 1.0
    assert r["mean_query_ms"] > 0.0
    assert r["mean_encode_us"] > 0.0
    assert r["code_bytes"] == 4 + 32  # int8 layout: scale header + dim bytes
    assert r["nodes_embedded"] == 200

    printed = capsys.readouterr().out
    assert "int8" in printed and "recall@10" in printed


def test_format_bench_table():
    fake = {
        "params": {"n_docs": 10, "dim": 8, "seed": 7, "n_queries": 3, "codecs": ["fp32", "polar"]},
        "results": {
            "fp32": {"recall_at_10": 1.0, "mean_query_ms": 0.5, "mean_encode_us": 0.2,
                     "rss_delta_mb": 1.0, "code_bytes": 32},
            "polar": {"recall_at_10": 0.7, "mean_query_ms": 0.6, "mean_encode_us": 3.0,
                      "rss_delta_mb": 2.0, "code_bytes": 8},
        },
    }
    table = format_bench(fake)
    assert "fp32" in table and "polar" in table
    assert "n_docs=10" in table
    assert len(table.strip().splitlines()) == 4  # header lines + one per codec
