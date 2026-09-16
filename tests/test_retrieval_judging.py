"""Guard the evaluator against scoring absent evidence as a successful read."""

import io
import tarfile

import pytest

from grag.core.types import NodeRecord, ScoredNode, SearchResponse, Subgraph
from retrieval_judging import (
    SOURCE_PREFIX,
    archive_corpus,
    code_navigation,
    code_targets,
    compact_baseline,
    matches,
    regressions,
    score,
)
from retrieval_variants import bounded_fusion, native_table_ranks


def test_fixed_code_gold_rejects_document_citations_and_tracks_packing(tmp_path):
    code = tmp_path / "identity.py"
    code.write_text("def repo_id(root):\n    pass\n", encoding="utf-8")
    doc = tmp_path / "identity.md"
    doc.write_text("# Identity\nUse repo_id\n", encoding="utf-8")
    targets = [{"source": "identity.py", "symbols": ["repo_id"]}, {"source": "identity.md", "sections": ["Identity"]}]
    assert code_targets(targets) == targets[:1]
    function = NodeRecord(id="Function:repo:identity.py#repo_id", label="Function", properties={"_source": str(code), "line_start": 1})
    section = NodeRecord(id="Section:identity", label="Section", properties={"_source": str(doc), "title": "Identity"})
    seed = ScoredNode(node=section, score=1, match="fts")
    response = SearchResponse(seeds=[seed], subgraph=Subgraph(nodes=[section]))
    expanded = Subgraph(nodes=[section, function])
    assert score(response, [seed], [seed], [seed], targets, tmp_path)["packed_navigation_hit"]
    result = code_navigation(response, [seed], [seed], [seed], expanded, targets, tmp_path)
    assert not result["packed_navigation_hit"]
    assert result["prepack_expansion_ids"] == [function.id]
    response.subgraph.nodes.append(function)
    result = code_navigation(response, [seed], [seed], [seed], expanded, targets, tmp_path)
    assert result["useful_cited_expansion_ids"] == [function.id]
    assert result["useful_cited_seed_ids"] == []
    response.seeds.append(ScoredNode(node=function, score=0.5, match="fts"))
    result = code_navigation(response, response.seeds, response.seeds, response.seeds, expanded, targets, tmp_path)
    assert result["useful_cited_seed_ids"] == [function.id]
    assert not result["useful_cited_expansion_ids"]


def test_bounded_fusion_keeps_unique_finite_hits_and_stable_boundary_ties():
    from grag.retrieval.search import _rrf_fuse

    def hit(key, score):
        return ScoredNode(node=NodeRecord(id=f"Function:{key}", label="Function"), score=score, match="fts")

    lists = {"fts": [hit("z", 100), hit("a", 50), hit("b", 50), hit("z", 25), hit("bad", float("nan"))],
             "vector": [hit("b", 0.8), hit("c", 0.7)]}
    fused = bounded_fusion(lists, 2, _rrf_fuse)
    assert {h.node.id for h in fused} == {"Function:z", "Function:a", "Function:b", "Function:c"}
    assert bounded_fusion({m: list(reversed(h)) for m, h in lists.items()}, 2, _rrf_fuse) == fused
    # b is vector-only after the deterministic FTS cutoff, not a dual hit.
    b = next(h for h in fused if h.node.id == "Function:b")
    assert b.score == 1 / 61 and b.match == "vector"


def test_candidate_or_id_alone_does_not_count_as_delivered_evidence(tmp_path):
    path = tmp_path / "identity.py"
    path.write_text("def repo_id(root):\n    pass\n", encoding="utf-8")
    node = NodeRecord(id="Function:repo:identity.py#repo_id", label="Function",
                      properties={"_source": str(path), "line_start": 1})
    hit = ScoredNode(node=node, score=1, match="fts")
    targets = [{"source": "identity.py", "symbols": ["repo_id"]}]
    response = SearchResponse()
    result = score(response, [hit], [hit], [hit], targets, tmp_path)
    assert result["candidate_hit"] and result["selected_first_useful"] == 1
    assert not result["packed_navigation_hit"] and not result["top1_useful"]
    for props in ({}, {"_source": str(path)}, {"_source": str(path), "line_start": 2}):
        packed = node.model_copy(update={"properties": props})
        response = SearchResponse(seeds=[hit], subgraph=Subgraph(nodes=[packed]))
        assert not score(response, [hit], [hit], [hit], targets, tmp_path)["packed_navigation_hit"]
    response = SearchResponse(seeds=[hit], subgraph=Subgraph(nodes=[node]))
    assert score(response, [hit], [hit], [hit], targets, tmp_path)["packed_navigation_hit"]


def test_expansion_can_supply_a_cited_target_but_wrong_source_cannot(tmp_path):
    path = tmp_path / "identity.py"
    path.write_text("def repo_id(root):\n    pass\n", encoding="utf-8")
    node = NodeRecord(id="Function:repo:identity.py#repo_id", label="Function",
                      properties={"_source": str(path), "line_start": 1})
    target = {"source": "identity.py", "symbols": ["repo_id"]}
    response = SearchResponse(subgraph=Subgraph(nodes=[node]))
    result = score(response, [], [], [], [target], tmp_path)
    assert result["packed_navigation_hit"] and not result["candidate_hit"]
    assert not matches(node.model_copy(update={"properties": {"_source": str(tmp_path / "other.py")}}), target, tmp_path)
    assert not matches(node.model_copy(update={"id": node.id + "_unrelated"}), target, tmp_path)


def test_document_judgments_require_the_right_section_and_source(tmp_path):
    target = {"source": "recovery.md", "sections": ["Recover a database"]}
    props = {"_source": str(tmp_path / "recovery.md"), "meta": '{"section":"Recover a database"}'}
    node = NodeRecord(id="Chunk:recovery@0", label="Chunk", properties=props)
    assert matches(node, target, tmp_path)
    assert not matches(node.model_copy(update={"properties": {**props, "meta": '{"section":"Backups"}'}}), target, tmp_path)
    assert not matches(node.model_copy(update={"label": "Document"}), target, tmp_path)


def test_logical_citations_resolve_to_archived_source(tmp_path):
    (tmp_path / "identity.py").write_text("def repo_id(root):\n    pass\n", encoding="utf-8")
    node = NodeRecord(id="Function:repo:identity.py#repo_id", label="Function",
                      properties={"_source": SOURCE_PREFIX + "identity.py", "line_start": 1})
    target = {"source": "identity.py", "symbols": ["repo_id"]}
    response = SearchResponse(subgraph=Subgraph(nodes=[node]))
    assert score(response, [], [], [], [target], tmp_path)["packed_navigation_hit"]


def test_archive_reads_pin_and_filters_files_without_extracting_links(tmp_path, monkeypatch):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name in ("src/grag/example.py", "docs/example.md", "src/grag/api/static/bundle.js"):
            info = tarfile.TarInfo(name)
            info.size = 5
            archive.addfile(info, io.BytesIO(b"hello"))
        link = tarfile.TarInfo("docs/link.md")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    def command(argv, cwd):
        assert argv == ["git", "archive", "--format=tar", "a" * 40, "src/grag", "docs"]
        assert cwd == tmp_path
        return stream.getvalue()
    monkeypatch.setattr("retrieval_judging.subprocess.check_output", command)
    with pytest.raises(ValueError, match="full SHA"):
        archive_corpus(tmp_path, tmp_path, "HEAD")
    root = archive_corpus(tmp_path, tmp_path, "a" * 40)
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == ["docs/example.md", "src/grag/example.py"]


def test_per_case_gate_rejects_a_regression_hidden_by_an_equal_total():
    report = {"corpus_commit": "pin", "corpus_sha256": "corpus", "judgments_sha256": "gold", "source_policy": "policy",
              "variant": "baseline", "embeddings": False, "runtime": {}, "summary": [], "results": []}
    for name, hit in (("a", True), ("b", False)):
        report["results"].append({"scope": "code", "case": name, "category": "natural", "batch": "test", "hops": 1, "budget": 2000,
                                  "candidate_hit": True, "top1_useful": hit, "packed_navigation_hit": hit,
                                  "selected_first_useful": 1 if hit else None, "returned_seeds": [name]})
    baseline = compact_baseline(report)
    assert not regressions(report, baseline)
    for row in report["results"]:
        row["packed_navigation_hit"] = not row["packed_navigation_hit"]
    failures = regressions(report, baseline)
    assert len(failures) == 1 and failures[0]["case"][1] == "a"
    report["corpus_sha256"] = "different"
    with pytest.raises(ValueError, match="corpus_sha256"):
        regressions(report, baseline)


def test_unsupported_hits_are_diagnostic_not_an_abstention_requirement():
    report = {"corpus_commit": "pin", "corpus_sha256": "corpus", "judgments_sha256": "gold", "source_policy": "policy",
              "variant": "baseline", "embeddings": False, "runtime": {}, "summary": [], "results": [{
                  "scope": "code", "case": "unsupported", "category": "unsupported", "batch": "test", "hops": 1, "budget": 2000,
                  "candidate_hit": False, "top1_useful": False, "packed_navigation_hit": False,
                  "selected_first_useful": None, "returned_seeds": [],
              }]}
    baseline = compact_baseline(report)
    report["results"][0]["returned_seeds"] = ["lexical-overlap-without-an-answer"]
    assert compact_baseline(report)["results"][0]["has_hits"]
    assert not regressions(report, baseline)


def test_document_success_cannot_mask_a_code_navigation_regression():
    metrics = {"candidate_hit": True, "top1_useful": True, "packed_navigation_hit": True, "selected_first_useful": 1}
    report = {"corpus_commit": "pin", "corpus_sha256": "corpus", "judgments_sha256": "gold", "source_policy": "policy",
              "code_gold_sha256": "fixed-code", "code_summary": [],
              "variant": "baseline", "embeddings": False, "runtime": {}, "summary": [], "results": [{
                  "scope": "code_and_docs", "case": "same-question", "category": "natural", "batch": "test", "hops": 1, "budget": 2000,
                  **metrics, "returned_seeds": ["function"], "code_navigation": dict(metrics),
              }]}
    baseline = compact_baseline(report)
    report["results"][0]["code_navigation"]["packed_navigation_hit"] = False
    failures = regressions(report, baseline)
    assert len(failures) == 1
    assert failures[0]["gold"] == "code_navigation"
    assert failures[0]["metric"] == "packed_navigation_hit"
    report["code_gold_sha256"] = "changed-targets"
    with pytest.raises(ValueError, match="code_gold_sha256"):
        regressions(report, baseline)
    report["code_gold_sha256"] = "fixed-code"
    del report["results"][0]["code_navigation"]
    with pytest.raises(ValueError, match="missing code_navigation"):
        regressions(report, baseline)


def test_table_rrf_ignores_input_order_but_cannot_calibrate_singletons():
    def hit(label, key, score):
        return ScoredNode(node=NodeRecord(id=f"{label}:{key}", label=label), score=score, match="fts")
    candidates = [hit("Code", "a", 100), hit("Code", "b", 100), hit("Code", "c", 50), hit("Note", "weak", 0.01)]
    for scaled in (False, True):
        forward = native_table_ranks(None, candidates, "", {}, scaled=scaled)
        assert forward == native_table_ranks(None, list(reversed(candidates)), "", {}, scaled=scaled)
        scores = {h.node.id: h.score for h in forward}
        assert scores["Code:a"] == scores["Code:b"] == scores["Note:weak"]
        assert scores["Code:c"] == (0.5 if scaled else 1) / 63
