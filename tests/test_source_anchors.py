"""Cited-source anchors on tracked memory: stored at write time, reported on reads."""
from __future__ import annotations

import json

from grag.core.mutate import define_schema, upsert_nodes
from grag.core.source_anchors import anchor_sources, changed_sources, cited_paths
from grag.core.types import (
    ContextRequest,
    DefineSchemaRequest,
    EvidenceUpdate,
    NodeTableSpec,
    PropertySpec,
    SearchRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.retrieval.context import get_context
from grag.retrieval.packing import mcp_retrieval_text
from grag.retrieval.search import search_knowledge


def test_cited_paths_keeps_files_and_drops_locations_dates_and_urls():
    source = "Discussion (user, 2026-09-17); svc/src/usage.py:80-85, /abs/x.py#L3-L9, https://x.io/a.py; README.md."
    assert cited_paths(source) == ["svc/src/usage.py", "/abs/x.py", "README.md"]


def test_anchor_and_change_detection(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    cited = root / "src" / "usage.py"
    cited.write_text("TOTAL = 1\n")
    encoded = anchor_sources("decision thread; src/usage.py:1, src/absent.py", [str(root)])
    anchors = json.loads(encoded)
    assert [a[0] for a in anchors] == ["src/usage.py"]  # unresolved citations are not anchored
    assert changed_sources(encoded) == []
    cited.write_text("TOTAL = 2\n")
    assert changed_sources(encoded) == ["src/usage.py"]
    cited.unlink()
    assert changed_sources(encoded) == ["src/usage.py (missing)"]
    assert anchor_sources("discussion only, no files", [str(root)]) is None


def _setup(engine):
    define_schema(engine, engine.config, DefineSchemaRequest(
        node_tables=[NodeTableSpec(name="Decision", properties=[PropertySpec(name="body")])]))


def _save(engine, key, body, source, **kwargs):
    return upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[UpsertNode(
        label="Decision", key=key, properties={"body": body}, source=source, **kwargs)]))


def _search(engine):
    return search_knowledge(engine, engine.config, SearchRequest(query="cache buckets", labels=["Decision"], hops=0))


def test_tracked_record_reports_changed_cited_file_on_reads(engine, tmp_path):
    _setup(engine)
    cited = tmp_path / "usage.py"
    cited.write_text("logical = uncached + cache_read + cache_write\n")
    _save(engine, "usage", "Input counts all three cache buckets.", f"discussion 2026-09-17; {cited}:1",
          expected_revision="absent", evidence=EvidenceUpdate())
    stored = engine.execute("MATCH (n:Decision {id:'usage'}) RETURN n._source_files").rows[0][0]
    assert json.loads(stored)[0][1] == str(cited.resolve())

    fresh = _search(engine)
    props = fresh.seeds[0].node.properties
    assert "_source_changed" not in props and "_source_files" not in props
    assert "_source_files" not in mcp_retrieval_text(fresh)

    cited.write_text("logical = uncached\n")
    stale = _search(engine)
    assert stale.seeds[0].node.properties["_source_changed"] == [str(cited)]
    assert "_source_changed" in mcp_retrieval_text(stale)
    ctx = get_context(engine, engine.config, ContextRequest(node_ids=["Decision:usage"], hops=0))
    assert ctx.subgraph.nodes[0].properties["_source_changed"] == [str(cited)]


def test_anchor_does_not_change_the_guard_and_a_correction_reanchors(engine, tmp_path):
    _setup(engine)
    cited = tmp_path / "usage.py"
    cited.write_text("v1\n")
    _save(engine, "usage", "Input counts all three cache buckets.", str(cited),
          expected_revision="absent", evidence=EvidenceUpdate())
    cited.write_text("v2\n")
    token = _search(engine).seeds[0].node.properties["_revision"]
    # The read token still guards the write after the cited file changed.
    result = _save(engine, "usage", "Input now counts uncached tokens only; cache buckets stay separate.", str(cited),
                   expected_revision=token, evidence=EvidenceUpdate(reason="usage.py changed"))
    assert result.history == {"Decision:usage": "recorded"}
    assert "_source_changed" not in _search(engine).seeds[0].node.properties


def test_untracked_writes_are_not_anchored(engine, tmp_path):
    _setup(engine)
    cited = tmp_path / "usage.py"
    cited.write_text("v1\n")
    _save(engine, "usage", "Input counts all three cache buckets.", str(cited))
    columns = {row[1] for row in engine.execute("CALL TABLE_INFO('Decision') RETURN *").rows}
    if "_source_files" in columns:
        assert engine.execute("MATCH (n:Decision {id:'usage'}) RETURN n._source_files").rows[0][0] is None
    assert "_source_changed" not in _search(engine).seeds[0].node.properties


def test_review_only_updates_and_identical_resaves_keep_pending_changes(engine, tmp_path):
    """Regression: only a claim or source change re-anchors; other writes must not
    clear a pending change report without the claim being rechecked."""
    _setup(engine)
    cited = tmp_path / "usage.py"
    cited.write_text("v1\n")
    _save(engine, "usage", "Input counts all three cache buckets.", str(cited),
          expected_revision="absent", evidence=EvidenceUpdate())
    cited.write_text("v2\n")
    token = _search(engine).seeds[0].node.properties["_revision"]
    upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[UpsertNode(
        label="Decision", key="usage", properties={}, expected_revision=token, evidence=EvidenceUpdate(review="accepted"))]))
    assert _search(engine).seeds[0].node.properties["_source_changed"] == [str(cited)]
    token = _search(engine).seeds[0].node.properties["_revision"]
    _save(engine, "usage", "Input counts all three cache buckets.", str(cited), expected_revision=token)
    assert _search(engine).seeds[0].node.properties["_source_changed"] == [str(cited)]


def test_adopting_an_existing_record_without_a_claim_change_sets_no_anchor(engine, tmp_path):
    """An untracked record's check time is unknown, so adoption alone does not anchor."""
    _setup(engine)
    cited = tmp_path / "usage.py"
    cited.write_text("v1\n")
    _save(engine, "usage", "Input counts all three cache buckets.", str(cited))
    row = engine.execute("MATCH (n:Decision {id:'usage'}) RETURN n").rows[0][0]
    from grag.core.revisions import content_revision
    _save(engine, "usage", "Input counts all three cache buckets.", str(cited),
          expected_revision=content_revision(row), evidence=EvidenceUpdate())
    assert engine.execute("MATCH (n:Decision {id:'usage'}) RETURN n._source_files").rows[0][0] is None
