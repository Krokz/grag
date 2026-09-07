"""Development-only, evidence-level evaluation; see fixtures/workflows/README.md.

Run: python tests/workflow_eval.py --output /tmp/grag-workflows.json
No generated answers, remote model calls, or production database access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import shutil
import statistics
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

import grag
from grag.config import EmbedderConfig, GragConfig
from grag.core.serialize import estimate_tokens
from grag.core.types import (
    CodeIngestRequest,
    DefineSchemaRequest,
    QueryRequest,
    ReadPolicy,
    SearchRequest,
    Subgraph,
    UpsertNodesRequest,
)
from grag.retrieval.packing import (
    mcp_retrieval_text,
    pack_context_response,
    pack_search_response,
)
from grag.retrieval.search import search_knowledge
from grag.retrieval.vectors import (
    embed_pending_nodes,
    searchable_node_tables,
    vector_candidates,
)
from grag.service import GragService

FIXTURES = Path(__file__).parent / "fixtures" / "workflows"
BUFFER = 128 * 1024**2


def fixture_hash():
    digest = hashlib.sha256()
    for path in sorted(FIXTURES.rglob("*")):
        if path.is_file() and path.suffix in (".json", ".py"):
            digest.update(path.relative_to(FIXTURES).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def materialize(root, distractors=24):
    shutil.copytree(FIXTURES / "project", root)
    notes = root / "notes"
    notes.mkdir()
    data = json.loads((FIXTURES / "memories.json").read_text(encoding="utf-8"))
    for i in range(distractors):
        data["nodes"].append({
            "label": "Note", "key": f"export-{i}",
            "properties": {"title": f"Report export job {i}", "body":
                "Report exports use local temporary files. The progress indicator displays rows written. "
                "Export jobs do not change authorization policy or session lifetime."},
        })
    for node in data["nodes"]:
        props = node["properties"]
        props["body"] = props["body"] * props.pop("repeat_body", 1) + props.pop("tail", "")
        source = notes / f"{node['key']}.md"
        source.write_text(f"# {props['title']}\n\n{props['body']}\n", encoding="utf-8")
        node["source"] = str(source)
    return data


def populate(service, root, data):
    service.ingest_code(CodeIngestRequest(paths=[str(root)], calls=True))
    service.describe_schema()
    # Bound this lookup to the independently authored fixture files. Stress
    # scenarios add hundreds of other functions beyond the default query limit.
    functions = service.cypher_query(QueryRequest(cypher=
        "MATCH (f:Function) WHERE f.path IN ['auth.py', 'cache.py', 'export.py'] RETURN f.name, f.id"))
    assert not functions.truncated and len(functions.rows) == 7
    refs = {f"Function:{name}": f"Function:{key}" for name, key in functions.rows}
    labels = {"Decision": ["status"], "Note": [], "Task": ["status", "priority"]}
    schema = {
        "node_tables": [{"name": label, "primary_key": "id", "properties": [
            {"name": prop, "type": "INT64" if prop == "priority" else "STRING"}
            for prop in ["title", "body", *extra]
        ]} for label, extra in labels.items()],
        "rel_tables": [
            {"name": "EXPLAINS", "from_label": "Decision", "to_label": "Function"},
            {"name": "IMPLEMENTS_DECISION", "from_label": "Task", "to_label": "Decision"},
            {"name": "REPLACES", "from_label": "Decision", "to_label": "Decision"},
        ],
    }
    service.define_schema(DefineSchemaRequest.model_validate(schema))
    refs.update({f"{n['label']}:{n['key']}": f"{n['label']}:{n['key']}" for n in data["nodes"]})
    edges = []
    for edge in data["edges"]:
        a, b = refs[edge["from"]].split(":", 1), refs[edge["to"]].split(":", 1)
        edges.append({"type": edge["type"], "from_label": a[0], "from_key": a[1],
                      "to_label": b[0], "to_key": b[1],
                      "source": str(root / "notes" / f"{a[1]}.md")})
    result = service.upsert_nodes(UpsertNodesRequest.model_validate({"nodes": data["nodes"], "edges": edges}))
    assert not result.warnings, result
    return refs


def token_counts(text, encodings):
    return {"utf8_estimate": estimate_tokens(text), **{
        name: len(encoding.encode(text, disallowed_special=()))
        for name, encoding in encodings.items()
    }}


def percentile95(samples):
    """Empirical nearest-rank percentile; small samples retain their slow tail."""
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def score_evidence(case, response, refs, *, text_values=None):
    """Score what was actually packed, never the unpacked candidate graph."""
    nodes = response.subgraph.node_map()
    required = {refs[key] for key in case["nodes"]}
    expected_edges = {(refs[a], kind, refs[b]) for a, kind, b in case["edges"]}
    edges = {(e.source, e.type, e.target) for e in response.subgraph.edges}
    text = response.context.casefold()
    wanted_text = case["text"]
    missing_text = [phrase for phrase in wanted_text if phrase.casefold() not in text]
    forbidden = [phrase for phrase in case.get("forbidden_text", []) if phrase.casefold() in text]
    invalid_excerpts = []
    for excerpt in response.text_excerpts:
        value = (text_values or {}).get((excerpt.node_id, excerpt.property))
        if (not isinstance(value, str) or not 0 <= excerpt.offset < excerpt.end <= len(value)
                or excerpt.total_chars != len(value)
                or excerpt.text != value[excerpt.offset:excerpt.end]
                or excerpt.sha256 != hashlib.sha256(value.encode("utf-8")).hexdigest()
                or excerpt.node_id not in nodes
                or excerpt.property in nodes[excerpt.node_id].properties):
            invalid_excerpts.append(f"{excerpt.node_id}.{excerpt.property}")
    citations = {}
    incomplete_citations = []
    invalid_citations = []
    for key, node in nodes.items():
        source = node.properties.get("_source")
        valid = isinstance(source, str) and (
            Path(source).is_dir() if node.label == "Repo" else Path(source).is_file()
        )
        if not valid:
            (invalid_citations if source else incomplete_citations).append(key)
        if valid and node.label == "Function":
            lines = Path(source).read_text(encoding="utf-8").splitlines()
            start = node.properties.get("line_start", 0)
            end = node.properties.get("line_end", start)
            name = node.properties.get("name", node.id.rsplit("#", 1)[-1])
            valid = 1 <= start <= end <= len(lines) and f"def {name}(" in "\n".join(lines[start - 1:end])
            if not valid:
                (incomplete_citations if not start else invalid_citations).append(key)
        citations[key] = bool(valid)
    seed_ids = [seed.node.id for seed in getattr(response, "seeds", [])]
    seed_gold = {refs[key] for key in case["seeds"]}
    ranks = [i for i, key in enumerate(seed_ids, 1) if key in seed_gold]
    answerable = case.get("answerable", True)
    node_recall = len(required & nodes.keys()) / len(required) if required else None
    edge_recall = len(expected_edges & edges) / len(expected_edges) if expected_edges else None
    citation_recall = sum(citations.get(key, False) for key in required) / len(required) if required else None
    complete = (required <= nodes.keys() and expected_edges <= edges and not missing_text
                and not forbidden and not invalid_excerpts and all(citations.get(key, False) for key in required))
    if not answerable:
        complete = not nodes
    return {
        "seed_hit": bool(ranks) if seed_gold else None,
        "reciprocal_rank": 1 / min(ranks) if ranks else (0.0 if seed_gold else None),
        "node_recall": node_recall, "edge_recall": edge_recall,
        "text_recall": 1 - len(missing_text) / len(wanted_text) if wanted_text else None,
        "required_citation_recall": citation_recall,
        "invalid_citations": invalid_citations, "incomplete_citations": incomplete_citations,
        "missing_nodes": sorted(required - nodes.keys()),
        "missing_edges": sorted(expected_edges - edges),
        "missing_text": missing_text, "forbidden_text_present": forbidden,
        "complete_evidence": complete,
        "invalid_excerpts": invalid_excerpts,
        "unsupported_context_nodes": len(nodes) if not answerable else None,
    }


def retrieve(service, mode, case, budget, top_k=3):
    policy = ReadPolicy(freshness="require", freshness_timeout_ms=60000)
    if mode == "cypher":
        result = service.cypher_query(QueryRequest(cypher=case["cypher"], **policy.model_dump()))
        # A projected graph view for comparing packing. Raw Cypher MCP output
        # is separately measured below; this packing is not a Cypher API claim.
        return pack_context_response(result.subgraph, budget, [], freshness=result.freshness), result
    freshness = service.read_freshness(policy)
    if mode == "vector":
        import datetime as dt

        seeds = vector_candidates(service.engine, service.config, case["question"], None, top_k,
                                  evidence_now=dt.datetime.now(dt.timezone.utc))
        return pack_search_response(Subgraph(nodes=[s.node for s in seeds]), seeds, budget,
                                    freshness=freshness, query=case["question"], evidence_policy="current"), None
    config = service.config.model_copy(update={"embedder": None}) if mode.startswith("fts") else service.config
    response = search_knowledge(service.engine, config, SearchRequest(
        query=case["question"], top_k=top_k, hops=1 if mode.endswith("graph") else 0,
        token_budget=budget,
    ), freshness=freshness)
    if mode.startswith("hybrid"):
        assert response.vector_status is None and not response.pending_embeddings, response
    return response, None


def evaluate(directory, *, embeddings=False, budgets=(1000, 3000), repeats=2,
             distractors=24, encodings=None):
    encodings = encodings or {}
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    root = directory / "project"
    data = materialize(root, distractors)
    config = GragConfig(db_path=directory / "workflow.lbdb", buffer_pool_size=BUFFER,
                        embedder=EmbedderConfig(provider="fastembed") if embeddings else None)
    started = time.perf_counter()
    service = GragService(config)
    try:
        refs = populate(service, root, data)
        embedded = 0
        if embeddings:
            embedded = sum(embed_pending_nodes(service.engine, config, table, max_nodes=None)
                           for table in searchable_node_tables(service.engine, config))
    finally:
        service.close()
    setup_ms = (time.perf_counter() - started) * 1000
    cases = json.loads((FIXTURES / "questions.json").read_text(encoding="utf-8"))
    corpus_files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".md"))
    corpus_text = "\n".join(f"{p.relative_to(root)}\n{p.read_text(encoding='utf-8')}" for p in corpus_files)
    corpus_counts = token_counts(corpus_text, encodings)
    modes = ["fts", "fts_graph", *(["vector", "hybrid", "hybrid_graph"] if embeddings else []), "cypher"]
    results = []
    timings = {}
    for mode in modes:
        start_open = time.perf_counter()
        service = GragService(config)
        service.enable_auto_refresh()
        timings[mode] = {"open_ms": (time.perf_counter() - start_open) * 1000}
        first = True
        try:
            for case in cases:
                if mode == "cypher" and "cypher" not in case:
                    continue
                for budget in budgets:
                    samples = []
                    for _ in range(repeats):
                        started = time.perf_counter()
                        response, raw = retrieve(service, mode, case, budget)
                        samples.append((time.perf_counter() - started) * 1000)
                        if first:
                            timings[mode]["first_query_after_reopen_ms"] = samples[-1]
                            first = False
                    assert response.response_token_estimate <= budget
                    payload = mcp_retrieval_text(response)
                    baseline = "\n".join(f"{name}\n{(root / name).read_text(encoding='utf-8')}" for name in case["baseline_files"])
                    text_values = {(f"{n['label']}:{n['key']}", prop): value
                                   for n in data["nodes"] for prop, value in n["properties"].items()
                                   if isinstance(value, str)}
                    score = score_evidence(case, response, refs, text_values=text_values)
                    if raw is not None:
                        score["seed_hit"] = score["reciprocal_rank"] = None
                    counts = token_counts(payload, encodings)
                    oracle_counts = token_counts(baseline, encodings)
                    result = {
                        "case": case["id"], "question": case["question"], "mode": mode, "budget": budget,
                        "answerable": case.get("answerable", True),
                        **score, "latency_ms": samples, "response_tokens": counts,
                        "reported_response_estimate": response.response_token_estimate,
                        "tokenizer_over_budget": {name: count > budget for name, count in counts.items() if name != "utf8_estimate"},
                        "oracle_source_tokens": oracle_counts,
                        "oracle_files_read": len(case["baseline_files"]),
                        "retrieval_calls": 1, "schema_calls_excluded": 1,
                        "token_savings_when_complete": {name: 1 - count / max(1, oracle_counts[name])
                                                       for name, count in counts.items()}
                        if score["complete_evidence"] and raw is None else None,
                        "token_savings_vs_full_corpus_when_complete": {name: 1 - count / max(1, corpus_counts[name])
                                                                      for name, count in counts.items()}
                        if score["complete_evidence"] and raw is None else None,
                        "truncated": response.truncated, "omitted_properties": response.omitted_properties,
                        "expansion_limited": response.expansion_limited,
                        "freshness": response.freshness.model_dump(),
                        "context": response.context, "seed_ids": [s.node.id for s in getattr(response, "seeds", [])],
                        "text_excerpts": [e.model_dump() for e in response.text_excerpts],
                    }
                    if raw is not None:
                        # Same shape as the MCP cypher_query tool, not the full
                        # Python model (which also contains a graph projection).
                        raw_text = json.dumps(raw.model_dump(mode="json", exclude={"subgraph"}),
                                              separators=(",", ":"), ensure_ascii=False)
                        result["raw_cypher_tokens"] = token_counts(raw_text, encodings)
                        result["comparison_view_only"] = True
                    results.append(result)
        finally:
            service.close()
    summary = []
    for mode in modes:
        for budget in budgets:
            rows = [row for row in results if row["mode"] == mode and row["budget"] == budget]
            summary.append({"mode": mode, "budget": budget, "cases": len(rows),
                            "complete_evidence_rate": statistics.mean(r["complete_evidence"] for r in rows),
                            "answerable_cases": sum(r["answerable"] for r in rows),
                            "complete_answerable_evidence_rate": statistics.mean(r["complete_evidence"] for r in rows if r["answerable"]),
                            "unanswerable_context_nodes": sum(r["unsupported_context_nodes"] or 0 for r in rows),
                            **{metric: statistics.mean(values) if (values := [r[metric] for r in rows if r[metric] is not None]) else None
                               for metric in ("seed_hit", "reciprocal_rank", "node_recall", "edge_recall", "text_recall")},
                            "tokenizer_budget_exceedances": {name: sum(r["tokenizer_over_budget"][name] for r in rows) for name in encodings},
                            "median_query_ms": statistics.median(t for r in rows for t in r["latency_ms"]),
                            "p95_query_ms": percentile95([t for r in rows for t in r["latency_ms"]])})
    return {
        "schema_version": 1, "fixture_sha256": fixture_hash(), "grag_version": grag.__version__,
        "runtime_path": grag.__file__, "ladybug_version": version("ladybug"),
        "python": platform.python_version(), "platform": platform.platform(),
        "tokenizers": {"package_version": version("tiktoken"), "encodings": list(encodings)} if encodings else None,
        "embedding": config.embedder.model_dump() if embeddings else None,
        "embedded_nodes": embedded, "setup_ms": setup_ms, "distractors": distractors,
        "full_corpus_baseline": {"files": len(corpus_files), "response_tokens": corpus_counts, "bulk_read_calls": 1},
        "timings": timings, "summary": summary, "results": results,
        "tokenizer_calibration": [{**sample, "counts": token_counts(sample["text"], encodings)}
                                  for sample in json.loads((FIXTURES / "tokenizer_samples.json").read_text(encoding="utf-8"))],
        "limits": ["Hand-authored fictional fixture, not a representative customer corpus or generated-answer evaluation.",
                   "No fake embedding scores: semantic modes run only with the actual configured local model.",
                   "First query means a reopened engine; model and OS caches may already be warm.",
                   "Tokens count tool response text; host prompts, schemas and model reasoning are excluded.",
                   "File baseline has oracle knowledge of relevant paths and can batch reads in one tool call.",
                   "The full-corpus baseline reads every fixture source. Both file baselines assume one bulk-read call; this does not demonstrate fewer agent calls.",
                   "No token-saving claim is made for incomplete evidence; negative savings are retained.",
                   "Cypher uses hand-selected routing, not a tested autonomous query planner; raw tool cost is separate."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embeddings", action="store_true", help="Use installed fastembed and its real local model")
    parser.add_argument("--encodings", nargs="*", default=[], help="Optional tiktoken names; no runtime dependency")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--distractors", type=int, default=24)
    parser.add_argument("--scenarios", action="store_true", help="Also run correction, paging, freshness, concurrency and moved-folder drills")
    parser.add_argument("--source-files", type=int, default=40, help="Additional files in each freshness workload")
    args = parser.parse_args()
    if args.repeats < 1 or args.distractors < 0 or args.source_files < 0:
        parser.error("repeats must be positive; distractors/source-files must be nonnegative")
    encodings = {}
    if args.encodings:
        import tiktoken
        encodings = {name: tiktoken.get_encoding(name) for name in args.encodings}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="grag-workflow-") as directory:
        report = evaluate(Path(directory), embeddings=args.embeddings, encodings=encodings,
                          repeats=args.repeats, distractors=args.distractors)
        # Preserve completed quality measurements if a later stateful drill fails.
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.scenarios:
            from workflow_scenarios import run_scenarios
            report["scenarios"] = [run_scenarios(Path(directory) / label, source_files=args.source_files,
                                                git=use_git, encodings=encodings)
                                   for label, use_git in (("plain", False), ("git", True))]
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
