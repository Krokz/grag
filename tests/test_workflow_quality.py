"""M24: evidence contracts use real FTS/code graphs; model quality is opt-in."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from grag.core.types import EdgeRecord, NodeRecord, Subgraph
from grag.retrieval.packing import pack_context_response
from workflow_eval import evaluate, percentile95, score_evidence
from workflow_scenarios import run_scenarios


def test_workflow_corpus(tmp_path):
    report = evaluate(tmp_path, repeats=1)
    rows = report["results"]
    assert len(rows) == 48  # ten searches per mode/budget, four structural questions
    for row in rows:
        assert row["freshness"]["status"] == "fresh"
        assert row["reported_response_estimate"] <= row["budget"]
        assert not row["invalid_citations"]
        assert not row["invalid_excerpts"]
        if not row["complete_evidence"]:
            assert row["token_savings_when_complete"] is None
    routed = [r for r in rows if r["mode"] == "cypher" and r["budget"] == 3000]
    assert len(routed) == 4 and all(r["complete_evidence"] for r in routed)
    graph = {r["case"]: r for r in rows if r["mode"] == "fts_graph" and r["budget"] == 3000}
    assert graph["shared-harnesses"]["complete_evidence"]
    assert graph["cache-rationale"]["complete_evidence"]
    assert graph["direct-callers"]["complete_evidence"]
    assert graph["superseded-evidence"]["complete_evidence"]
    assert not graph["superseded-evidence"]["forbidden_text_present"]
    assert sum(r["complete_evidence"] for r in graph.values()) == 9
    # M27 returns the decisive exact excerpt, while still declaring that the
    # original body is partial. The evaluator verifies its offsets and hash.
    long = next(r for r in rows if r["case"] == "long-exception" and r["mode"] == "fts" and r["budget"] == 1000)
    assert long["truncated"] and long["complete_evidence"] and long["text_excerpts"]
    target = Path(os.environ.get("GRAG_WORKFLOW_REPORT", str(tmp_path / "report.json")))
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.mark.parametrize("git", [False, True], ids=["plain-folder", "git-checkout"])
def test_stateful_workflows(tmp_path, git):
    report = run_scenarios(tmp_path, git=git, source_files=8)
    assert report["restart"]["receipt_replayed"]
    assert report["relocation"]["authored_link_preserved"]
    assert report["long_text"]["complete_after_paging"]


def test_evidence_scoring_requires_all_nodes_directed_edges_text_and_citations(tmp_path):
    source = tmp_path / "facts.md"
    source.write_text("First fact. Second fact.")
    a = NodeRecord(id="Note:a", label="Note", properties={"body": "First fact.", "_source": str(source)})
    b = NodeRecord(id="Note:b", label="Note", properties={"body": "Second fact.", "_source": str(source)})
    refs = {"Note:a": "Note:a", "Note:b": "Note:b"}
    case = {"nodes": list(refs), "seeds": ["Note:a"], "edges": [["Note:a", "SUPPORTS", "Note:b"]],
            "text": ["First fact.", "Second fact."], "forbidden_text": ["obsolete"]}
    partial = pack_context_response(Subgraph(nodes=[a]), 3000, [])
    score = score_evidence(case, partial, refs)
    assert score["node_recall"] == 0.5 and score["text_recall"] == 0.5
    assert not score["complete_evidence"]
    wrong = EdgeRecord(id="wrong", type="SUPPORTS", source="Note:b", target="Note:a")
    response = pack_context_response(Subgraph(nodes=[a, b], edges=[wrong]), 3000, [])
    assert score_evidence(case, response, refs)["edge_recall"] == 0
    correct = wrong.model_copy(update={"source": "Note:a", "target": "Note:b"})
    response = pack_context_response(Subgraph(nodes=[a, b], edges=[correct]), 3000, [])
    assert score_evidence(case, response, refs)["complete_evidence"]
    source.unlink()
    assert score_evidence(case, response, refs)["required_citation_recall"] == 0


def test_metadata_and_substring_ids_are_not_evidence(tmp_path):
    node = NodeRecord(id="Note:answer-with-required-phrase", label="Note", properties={"body": "unrelated"})
    response = pack_context_response(Subgraph(nodes=[node]), 3000, [])
    case = {"nodes": ["Note:answer"], "seeds": ["Note:answer"], "edges": [], "text": ["decisive fact"]}
    score = score_evidence(case, response, {"Note:answer": "Note:answer"})
    assert score["node_recall"] == 0 and score["text_recall"] == 0
    assert not score["complete_evidence"]


def test_percentile_includes_slow_tail_with_few_samples():
    assert percentile95([5]) == 5
    assert percentile95([100, 1]) == 100
    assert percentile95(list(range(1, 101))) == 95


def test_repository_directory_citations_are_valid_but_function_citations_need_files(tmp_path):
    repo=NodeRecord(id='Repo:project',label='Repo',properties={'_source':str(tmp_path)})
    response=pack_context_response(Subgraph(nodes=[repo]),3000,[])
    case={'nodes':[repo.id],'seeds':[],'edges':[],'text':[]}
    assert score_evidence(case,response,{repo.id:repo.id})['complete_evidence']
    function=repo.model_copy(update={'id':'Function:fake','label':'Function'})
    response=pack_context_response(Subgraph(nodes=[function]),3000,[])
    case['nodes']=[function.id]
    assert not score_evidence(case,response,{function.id:function.id})['complete_evidence']
