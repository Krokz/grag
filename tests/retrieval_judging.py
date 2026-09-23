"""Source-grounded navigation judgments, separate from agent answer/cost evaluation.

Build the pinned source corpus from git and evaluate it:
    python tests/retrieval_judging.py --output report.json --check-baseline

Only those sources are ingested. Gold, tests, audit notes and agent memories stay
outside the graph. The temporary database uses the installed runtime. This is a
development set with related query variants, not a held-out quality estimate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import subprocess
import tarfile
import tempfile
import time
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from grag.config import EmbedderConfig, GragConfig
from grag.core.types import (
    CodeIngestRequest,
    IngestDocument,
    IngestRequest,
    NodeRecord,
    QueryRequest,
    SearchRequest,
)
from grag.retrieval import search
from grag.retrieval.packing import mcp_retrieval_text
from grag.retrieval.vectors import (
    embed_pending_nodes,
    get_embedder,
    pk_map_with_fallback,
    searchable_node_tables,
    string_props,
)
from grag.service import GragService
from retrieval_variants import VARIANTS, ranking_variant

JUDGMENTS = Path(__file__).parent / "fixtures" / "retrieval_judgments.json"
BASELINE = Path(__file__).parent / "fixtures" / "retrieval_baseline.json"
SOURCE_PREFIX = "fixture://grag/"


def archive_corpus(repository: Path, directory: Path, commit: str) -> Path:
    """Read a pinned commit, never the dirty worktree or downloaded code."""
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Corpus commit must be a full SHA-1")
    raw = subprocess.check_output(["git", "archive", "--format=tar", commit, "src/grag", "docs"], cwd=repository)  # noqa: S603, S607 — fixed git argv, validated full commit hash
    root = directory / "corpus"
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            name = Path(member.name)
            if not member.isfile() or name.suffix not in {".py", ".md"}:
                continue
            if name.is_absolute() or ".." in name.parts or name.parts[0] not in {"src", "docs"}:
                raise ValueError(f"Invalid archived source: {name}")
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
    return root


def normalize_sources(service, corpus):
    """Only the disposable fixture: stable citations keep packing host-independent.

    Native function IDs use the fixed fixture root; docs are ingested with
    logical source URIs. Resolve these citations against the archived files when
    grading. No original database or production ingestion policy is modified.
    """
    for table, pk in pk_map_with_fallback(service.engine).items():
        columns = string_props(service.engine, table)
        path_column = ", n.path" if "path" in columns else ""
        rows = service.engine.execute(f"MATCH (n:{table}) RETURN n.{pk}, n._source{path_column}").rows
        values = []
        for key, source, *paths in rows:
            if not source:
                continue
            logical_source = SOURCE_PREFIX + Path(source).relative_to(corpus).as_posix() if source.startswith(str(corpus)) else source
            path = paths[0] if paths else None
            if path and path.startswith(str(corpus)):
                path = SOURCE_PREFIX + Path(path).relative_to(corpus).as_posix()
            values.append({"key": key, "source": logical_source, "path": path})
        if values:
            set_path = ", n.path=row.path" if path_column else ""
            service.engine.execute_write(
                f"UNWIND $rows AS row MATCH (n:{table} {{{pk}:row.key}}) "
                f"SET n._source=row.source, n._created_at=timestamp('2026-09-16T00:00:00'){set_path}",
                {"rows": values})
        if "ingested_at" in columns:
            service.engine.execute_write(f"MATCH (n:{table}) SET n.ingested_at='2026-09-16T00:00:00+00:00'")


def matches(node: NodeRecord, target: dict, corpus: Path) -> bool:
    """Match an explicit source and symbol/section, never an ID substring."""
    props = node.properties
    if props.get("_source") not in {str(corpus / target["source"]), SOURCE_PREFIX + target["source"]}:
        return False
    if "symbols" in target:
        return node.label == "Function" and node.id.rsplit("#", 1)[-1] in target["symbols"]
    if node.label == "Section":
        return props.get("title") in target["sections"]
    if node.label == "Chunk":
        return json.loads(props.get("meta") or "{}").get("section") in target["sections"]
    return False


def citation_visible(node: NodeRecord, corpus: Path) -> bool:
    """Judge packed citations; full candidate properties cannot fill omissions."""
    source = node.properties.get("_source")
    if not isinstance(source, str):
        return False
    path = corpus / source.removeprefix(SOURCE_PREFIX) if source.startswith(SOURCE_PREFIX) else Path(source)
    if not path.is_relative_to(corpus) or not path.is_file():
        return False
    if node.label == "Function":
        lines = path.read_text(encoding="utf-8").splitlines()
        line = node.properties.get("line_start")
        name = node.id.rsplit("#", 1)[-1].rsplit(".", 1)[-1]
        return isinstance(line, int) and 1 <= line <= len(lines) and f"def {name}(" in lines[line - 1]
    return node.label in {"Chunk", "Section"}


def first_rank(ids, relevant):
    return next((i for i, key in enumerate(ids, 1) if key in relevant), None)


def code_targets(targets):
    """Freeze the existing symbol gold across scopes; never add doc alternatives."""
    return [target for target in targets if "symbols" in target]


def score(response, candidates, ranked, selected, targets, corpus):
    relevant = {s.node.id for s in candidates if any(matches(s.node, t, corpus) for t in targets)}
    seed_ids = [s.node.id for s in response.seeds]
    # Expansion may find a useful node that was not in the FTS shortlist. Its
    # full identity is judged independently, but citation must actually ship.
    packed = response.subgraph.node_map()
    relevant.update(key for key, node in packed.items() if any(matches(node, t, corpus) for t in targets))
    useful_cited = sorted(key for key, node in packed.items() if key in relevant and citation_visible(node, corpus))
    return {
        "candidate_hit": bool(relevant & {s.node.id for s in candidates}),
        "fused_first_useful": first_rank([s.node.id for s in ranked], relevant),
        "selected_first_useful": first_rank([s.node.id for s in selected], relevant),
        "packed_seed_first_useful": first_rank(seed_ids, relevant),
        "top1_useful": bool(seed_ids and seed_ids[0] in relevant),
        "useful_cited_ids": useful_cited,
        "useful_cited_seed_ids": [key for key in useful_cited if key in seed_ids],
        "useful_cited_expansion_ids": [key for key in useful_cited if key not in seed_ids],
        "packed_navigation_hit": bool(useful_cited),
        "returned_seeds": seed_ids,
    }


def code_navigation(response, candidates, ranked, selected, expanded, targets, corpus):
    targets = code_targets(targets)
    scored = score(response, candidates, ranked, selected, targets, corpus)
    selected_ids = {s.node.id for s in selected}
    scored["prepack_relevant_ids"] = sorted(
        n.id for n in expanded.nodes if any(matches(n, t, corpus) for t in targets)
    )
    scored["prepack_expansion_ids"] = [key for key in scored["prepack_relevant_ids"] if key not in selected_ids]
    scored["packed_mention_edges"] = [
        e.model_dump() for e in response.subgraph.edges
        if e.type == "MENTIONS_FUNCTION" and e.target in scored["useful_cited_ids"]
    ]
    return scored


def validate_targets(corpus, topics):
    """Fail on stale gold anchors instead of silently grading a missing target."""
    import ast

    for topic in topics.values():
        for target in topic["targets"]:
            text = (corpus / target["source"]).read_text(encoding="utf-8")
            if "symbols" in target:
                names = {n.name for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                assert all(s.rsplit(".", 1)[-1] in names for s in target["symbols"]), target
            else:
                headings = {re.sub(r"`([^`]*)`", r"\1", h) for h in re.findall(r"^#+ (.+)$", text, re.MULTILINE)}
                assert set(target["sections"]) <= headings, target


def evaluate(corpus: Path, directory: Path, *, budgets=(2000, 4000), hops=(0, 1), variant="baseline", embeddings=False, vector_only=False, surface="rest"):
    corpus = corpus.resolve()
    manifest = json.loads(JUDGMENTS.read_text(encoding="utf-8"))
    validate_targets(corpus, manifest["topics"])
    fixed_code_gold = {name: code_targets(topic["targets"]) for name, topic in manifest["topics"].items()}
    assert all(fixed_code_gold[q["topic"]] for q in manifest["queries"] if q["category"] != "unsupported")
    files = sorted([*(corpus / "src/grag").rglob("*.py"), *(corpus / "docs").rglob("*.md")])
    hashes = {p.relative_to(corpus).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    report = {
        "scope": manifest["scope"], "corpus": str(corpus), "files": hashes,
        "corpus_commit": manifest["corpus_commit"], "source_policy": manifest["source_policy"],
        "variant": variant, "embeddings": embeddings,
        "vector_only": vector_only,
        # Diagnostic only: the committed baseline gate always packs for "rest".
        "surface": surface,
        "corpus_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "judgments_sha256": hashlib.sha256(JUDGMENTS.read_bytes()).hexdigest(),
        "code_gold_sha256": hashlib.sha256(json.dumps(fixed_code_gold, sort_keys=True).encode()).hexdigest(),
        "code_gold": fixed_code_gold,
        "runtime": {"python": platform.python_version(), "ladybug": importlib.metadata.version("ladybug")},
        "limits": "Navigation relevance only; no answer grading, agent/model generation or session-token savings. One documentation-rich repository, unblinded questions shaped by its own development/failure investigations and documentation vocabulary. Positive targets are development judgments, not exhaustive relevance labels. No independent-author, sparse-doc or cross-project conclusion.",
        "results": [], "ingestion": {}, "exact_routes": [],
    }
    service = None
    try:
        identity = "grag-" + hashlib.sha256((SOURCE_PREFIX + "src/grag").encode()).hexdigest()
        for scope in ("code", "code_and_docs"):
            # Build each arm before its first index/query; adding docs to an
            # already queried database is a different stateful workload.
            service = GragService(GragConfig(db_path=directory / f"{scope}.lbdb", buffer_pool_size=128 * 1024**2))
            with patch("grag.ingest.code._repo_id", return_value=identity):
                report["ingestion"]["code"] = service.ingest_code(CodeIngestRequest(paths=[str(corpus / "src/grag")])).model_dump()
            if scope == "code_and_docs":
                report["ingestion"]["docs"] = service.ingest(IngestRequest(sections=True, documents=[
                    IngestDocument(source=SOURCE_PREFIX + p.relative_to(corpus).as_posix(), text=p.read_text(encoding="utf-8")) for p in files if p.suffix == ".md"
                ])).model_dump()
            normalize_sources(service, corpus)
            if scope == "code_and_docs":
                links = service.engine.execute(
                    "MATCH (s:Section)-[:MENTIONS_FUNCTION]->(f:Function) "
                    "RETURN s.id,s._source,s.title,f.id,f._source ORDER BY s.id,f.id"
                ).rows
                report["gold_mention_links"] = {
                    name: [{"section": "Section:" + sid, "source": source, "title": title, "function": "Function:" + fid}
                           for sid, source, title, fid, fsource in links
                           if any(matches(NodeRecord(id="Function:" + fid, label="Function", properties={"_source": fsource}), t, corpus) for t in targets)]
                    for name, targets in fixed_code_gold.items()
                }
            texts = {}
            for table, pk in pk_map_with_fallback(service.engine).items():
                cols = string_props(service.engine, table)
                if cols:
                    rows = service.engine.execute(f"MATCH (n:{table}) RETURN " + ",".join(f"n.{col}" for col in cols) + f" ORDER BY n.{pk}").rows
                    texts[table] = {"columns": cols, "sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()}
            report.setdefault("graph_text_hashes", {})[scope] = texts
            if embeddings:
                os.environ["HF_HUB_OFFLINE"] = "1"
                service.config = service.config.model_copy(update={"embedder": EmbedderConfig(provider="fastembed")})
                report["embedder"] = service.config.embedder.model_dump()
                embedder = get_embedder(service.config)
                asset_root = Path(embedder._model.model._model_dir)
                report["model_snapshot"] = asset_root.name
                report["model_assets"] = {}
                for asset in sorted(asset_root.rglob("*")):
                    if asset.is_file():
                        digest = hashlib.sha256()
                        with asset.open("rb") as stream:
                            while block := stream.read(1024 * 1024):
                                digest.update(block)
                        report["model_assets"][asset.relative_to(asset_root).as_posix()] = digest.hexdigest()
                report["runtime"].update({name: importlib.metadata.version(name) for name in ("fastembed", "onnxruntime", "numpy")})
                started = time.perf_counter()
                embedded = sum(embed_pending_nodes(service.engine, service.config, table, max_nodes=None)
                               for table in searchable_node_tables(service.engine, service.config))
                report["ingestion"][f"{scope}_embedding"] = {"nodes": embedded, "seconds": time.perf_counter() - started}
            service.describe_schema()
            for case in manifest["queries"]:
                targets = manifest["topics"][case["topic"]]["targets"]
                if symbol := case.get("exact_symbol"):
                    result = service.cypher_query(QueryRequest(cypher=
                        f"MATCH (f:Function) WHERE f.name={json.dumps(symbol)} RETURN f.id,f.path,f.line_start"))
                    assert result.rows and not result.truncated, (case["id"], result)
                    for key, path, line in result.rows:
                        assert key.rsplit("#", 1)[-1] == symbol
                        assert f"def {symbol}(" in (corpus / "src/grag" / path).read_text(encoding="utf-8").splitlines()[line - 1]
                    report["exact_routes"].append({"scope": scope, "case": case["id"], "rows": result.rows})
                for hop in hops:
                    for budget in budgets:
                        with (
                            ranking_variant(variant),
                            patch.object(search, "rank_lexical", wraps=search.rank_lexical) as ranking,
                            patch.object(search, "_diversify", wraps=search._diversify) as diversity,
                            patch.object(search, "pack_search_response", wraps=search.pack_search_response) as packing,
                            patch.object(search, "_fts_seeds", return_value=[]) if vector_only else nullcontext(),
                        ):
                            response = service.search_knowledge(SearchRequest(query=case["query"], top_k=8, hops=hop, token_budget=budget), surface=surface)
                        if embeddings:
                            assert response.vector_status is None and response.pending_embeddings == 0, response
                        candidates = ranking.call_args.args[1]
                        # With embeddings this is the fused, not lexical, rank.
                        ranked = diversity.call_args.args[0]
                        selected = search._diversify(ranked, 8, service.config.search_label_cap)
                        row = {"case": case["id"], "topic": case["topic"], "query": case["query"], "scope": scope,
                               "category": case["category"], "batch": case["batch"],
                               "budget": budget, "hops": hop, "answerable": bool(targets),
                               **score(response, [*candidates, *ranked], ranked, selected, targets, corpus),
                               "code_navigation": code_navigation(response, [*candidates, *ranked], ranked, selected,
                                                                  packing.call_args.args[0], targets, corpus),
                               "selected_seed_ids": [s.node.id for s in selected],
                               "selected_labels": dict(Counter(s.node.label for s in selected)),
                               "lexical_candidate_hit": any(matches(s.node, t, corpus) for s in candidates for t in targets),
                               "lexical_pool_sha256": hashlib.sha256(json.dumps(sorted(s.node.id for s in candidates)).encode()).hexdigest(),
                               "candidates": len(candidates), "truncated": response.truncated,
                               "response_token_estimate": response.response_token_estimate,
                               "response": mcp_retrieval_text(response)}
                        report["results"].append(row)
            service.close()
    finally:
        if service is not None:
            service.close()
    assert hashes == {p.relative_to(corpus).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}, "Corpus changed during evaluation"
    report["summary"] = []
    report["code_summary"] = []
    for scope in ("code", "code_and_docs"):
        for hop in hops:
            for budget in budgets:
                rows = [r for r in report["results"] if r["scope"] == scope and r["hops"] == hop and r["budget"] == budget]
                for category in ("natural", "control", "unsupported"):
                    group = [r for r in rows if r["category"] == category]
                    report["summary"].append({"scope": scope, "budget": budget, "hops": hop, "category": category, "queries": len(group),
                        **{key: sum(bool(r[key]) for r in group) for key in ("candidate_hit", "top1_useful", "packed_navigation_hit")},
                        "selected_hit_at_8": sum(r["selected_first_useful"] is not None for r in group),
                        "unanswerable_with_hits": sum(bool(r["returned_seeds"]) for r in group if not r["answerable"])})
                    report["code_summary"].append({"scope": scope, "budget": budget, "hops": hop, "category": category, "queries": len(group),
                        **{key: sum(bool(r["code_navigation"][key]) for r in group) for key in (
                            "candidate_hit", "top1_useful", "packed_navigation_hit", "useful_cited_seed_ids", "useful_cited_expansion_ids",
                            "prepack_relevant_ids", "prepack_expansion_ids", "packed_mention_edges")},
                        "selected_hit_at_8": sum(r["code_navigation"]["selected_first_useful"] is not None for r in group)})
    return report


def compact_baseline(report):
    keys = ("scope", "case", "category", "batch", "hops", "budget", "candidate_hit", "top1_useful", "packed_navigation_hit", "selected_first_useful")
    return {**{k: report[k] for k in ("corpus_commit", "corpus_sha256", "judgments_sha256", "source_policy", "variant", "embeddings", "runtime", "summary")},
            **({"code_gold_sha256": report["code_gold_sha256"], "code_summary": report["code_summary"]} if "code_gold_sha256" in report else {}),
            "results": [{**{k: r[k] for k in keys}, "has_hits": bool(r["returned_seeds"]),
                         **({"code_navigation": {k: r["code_navigation"][k] for k in keys[6:]}} if "code_navigation" in r else {})}
                        for r in report["results"]]}


def regressions(report, baseline):
    for key in ("corpus_commit", "corpus_sha256", "judgments_sha256", "source_policy"):
        if report[key] != baseline[key]:
            raise ValueError(f"Incomparable baseline: {key}")
    if "code_gold_sha256" in baseline and report.get("code_gold_sha256") != baseline["code_gold_sha256"]:
        raise ValueError("Incomparable baseline: code_gold_sha256")
    def key(row):
        return row["scope"], row["case"], row["hops"], row["budget"]
    current = {key(r): r for r in compact_baseline(report)["results"]}
    failures = []
    if current.keys() != {key(r) for r in baseline["results"]}:
        raise ValueError("Incomparable baseline: missing or extra cases")
    for before in baseline["results"]:
        after = current[key(before)]
        # Unsupported-query hit counts are diagnostics, not an abstention gate:
        # lexical overlap or vector similarity alone does not assert answerability.
        for gold in ("mixed", "code_navigation") if "code_navigation" in before else ("mixed",):
            previous = before if gold == "mixed" else before[gold]
            current_scores = after if gold == "mixed" else after.get(gold)
            if current_scores is None:
                raise ValueError("Incomparable baseline: missing code_navigation metrics")
            for metric in ("candidate_hit", "top1_useful", "packed_navigation_hit"):
                if previous[metric] and not current_scores[metric]:
                    failures.append({"case": key(before), "gold": gold, "metric": metric, "before": previous[metric], "after": current_scores[metric]})
            old_rank, new_rank = previous["selected_first_useful"], current_scores["selected_first_useful"]
            if old_rank is not None and (new_rank is None or new_rank > old_rank):
                failures.append({"case": key(before), "gold": gold, "metric": "selected_first_useful", "before": old_rank, "after": new_rank})
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, help="Optional existing snapshot; default builds the pinned git archive")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", choices=VARIANTS, default="baseline")
    parser.add_argument("--hops", type=int, nargs="+", choices=(0, 1, 2), default=[0, 1], help="Evaluation hop counts; 2 diagnoses chunk-to-section-to-code expansion")
    parser.add_argument("--embeddings", action="store_true", help="Use cached BGE-small offline; no downloads")
    parser.add_argument("--vector-only", action="store_true", help="With --embeddings, omit FTS to diagnose vector recall")
    parser.add_argument("--surface", choices=("rest", "mcp"), default="rest",
                        help="Transport the response is packed for; the baseline gate uses the default rest contract")
    parser.add_argument("--write-baseline", type=Path, help="Explicitly write a reviewed per-case baseline")
    parser.add_argument("--check-baseline", action="store_true")
    args = parser.parse_args()
    if args.vector_only and not args.embeddings:
        parser.error("--vector-only requires --embeddings")
    if args.write_baseline and args.check_baseline:
        parser.error("Baseline refresh and regression checking must be separate runs")
    with tempfile.TemporaryDirectory(prefix="grag-judging-") as work:
        corpus = args.corpus or archive_corpus(Path(__file__).resolve().parents[1], Path(work), json.loads(JUDGMENTS.read_text())["corpus_commit"])
        result = evaluate(corpus, Path(work), variant=args.variant, embeddings=args.embeddings, vector_only=args.vector_only, hops=tuple(args.hops), surface=args.surface)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"mixed_gold": result["summary"], "code_gold": result["code_summary"]}, indent=2))
    if args.write_baseline:
        args.write_baseline.write_text(json.dumps(compact_baseline(result), indent=2) + "\n", encoding="utf-8")
    if args.check_baseline:
        failures = regressions(result, json.loads(BASELINE.read_text(encoding="utf-8")))
        if failures:
            raise SystemExit(json.dumps(failures, indent=2))
