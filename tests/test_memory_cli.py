"""M20: CLI conveniences retain the existing memory, revision and owner contracts."""

from __future__ import annotations

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from grag import cli
from grag.admin import ServerInfo
from grag.api.main import create_app
from grag.client import GraphClient
from grag.config import GragConfig
from grag.core.types import DefineSchemaRequest, QueryRequest, UpsertNodesRequest
from grag.service import GragService


@pytest.fixture()
def graph(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAG_EMBED_PROVIDER", "")
    monkeypatch.setenv("GRAG_AUTO_REFRESH_CODE", "0")
    monkeypatch.setattr("grag.admin.find_server", lambda db: None)
    config = GragConfig(db_path=tmp_path / "memory.lbdb", buffer_pool_size=128 * 1024**2)

    def run(*args, success=True):
        status = cli.main(["--db", str(config.db_path), *args])
        output = capsys.readouterr()
        assert status == (0 if success else 1), output
        return json.loads(output.out) if success and "--json" in args else output

    return config, run


def test_save_inspect_revise_retire_and_history_loop(graph):
    config, run = graph
    saved = run("remember", "Retry twice", "--id", "retry", "--track-history", "--source", "design.md", "--json")
    assert saved["revision"] == saved["revisions"]["Memory:retry"]
    inspected = run("inspect", "Memory:retry", "--json")
    assert inspected["revision"] == saved["revision"]
    assert inspected["node"]["text"] == "Retry twice" and inspected["node"]["_source"] == "design.md"
    assert inspected["database"] == str(config.db_path) and inspected["evidence_policy"] == "all"
    assert "_ID" not in inspected["node"]
    args = ("remember", "Retry three times", "--id", "retry", "--reason", "Revised acceptance", "--source", "review.md",
            "--expected-revision", inspected["revision"], "--operation-id", "revise-1", "--json")
    revised = run(*args)
    assert revised["revision"] != inspected["revision"]
    assert run(*args)["replayed"]
    error = run("remember", "stale update", "--id", "retry", "--expected-revision", saved["revision"], success=False)
    assert "conflict" in error.err.lower()
    retired = run("retire", "Memory:retry", "--expected-revision", revised["revision"], "--reason", "No longer applies", "--json")
    assert retired["revision"] != revised["revision"]
    current = run("context", "Memory:retry", "--json")
    assert not current["included_node_ids"] and current["excluded_evidence"] == 1
    assert not run("search", "Retry", "--json")["included_node_ids"]
    all_evidence = run("search", "Retry", "--evidence", "all", "--json")
    assert "Memory:retry" in all_evidence["included_node_ids"] and 'retracted' in all_evidence["context"]
    latest = run("inspect", "Memory:retry", "--json")
    assert latest["node"]["text"] == "Retry three times" and latest["node"]["_source"] == "review.md"
    assert latest["node"]["_evidence_state"] == "retracted"
    history = run("context", "Memory:retry", "--history", "--json")["history"]
    assert [e["sequence"] for e in history["entries"]] == [3, 2, 1]
    assert history["entries"][0]["reason"] == "No longer applies"
    assert history["entries"][1]["source"] == "review.md"
    assert history["entries"][0]["actor"] is None  # CLI does not invent an authenticated user
    previous = run("context", "Memory:retry", "--revision", "1", "--json")
    assert "Retry twice" in previous["context"]
    assert run("context", "Memory:retry", "--history", "--history-before", "2", "--json")["history"]["entries"][0]["sequence"] == 1
    # The same retry receipt does not resurrect a later-retired memory.
    assert run(*args)["replayed"]
    assert run("inspect", "Memory:retry", "--json")["revision"] == retired["revision"]


def test_existing_unguarded_remember_remains_compatible_and_history_adopts_baseline(graph):
    _, run = graph
    run("remember", "old", "--id", "legacy")
    run("remember", "latest", "--id", "legacy")
    before = run("inspect", "Memory:legacy", "--json")
    assert before["node"]["text"] == "latest"
    failed = run("remember", "accidental overwrite", "--id", "legacy", "--track-history", success=False)
    assert "conflict" in failed.err.lower()
    run("remember", "corrected", "--id", "legacy", "--track-history", "--expected-revision", before["revision"])
    history = run("context", "Memory:legacy", "--history", "--json")["history"]
    assert [e["sequence"] for e in history["entries"]] == [1, 0]
    assert history["entries"][1]["baseline"]
    assert "latest" in run("context", "Memory:legacy", "--revision", "0", "--json")["context"]


@pytest.mark.parametrize("key", ["with:colon", 'with"quote', "with'quote", "with\\slash", "line\nfeed",
                                 'x" RETURN n UNION MATCH (n) RETURN n //', "שלום 🌍"])
def test_inspect_literal_is_safe_and_exact(graph, key):
    _, run = graph
    run("remember", "selected", "--id", key)
    run("remember", "unrelated", "--id", "other")
    result = run("inspect", f"Memory:{key}", "--json")
    assert result["node_id"] == f"Memory:{key}" and result["node"]["text"] == "selected"


def test_inspect_and_retire_support_custom_primary_keys_and_preserve_links(graph):
    config, run = graph
    with closing(GragService(config)) as service:
        service.define_schema(DefineSchemaRequest.model_validate({
            "node_tables": [
                {"name": "Decision", "primary_key": "name", "properties": [{"name": "summary"}]},
                {"name": "Ticket", "primary_key": "number", "properties": [{"name": "number", "type": "INT64"}]},
            ], "rel_tables": [{"name": "INFORMS", "from_label": "Decision", "to_label": "Ticket"}],
        }))
        service.upsert_nodes(UpsertNodesRequest.model_validate({"nodes": [
            {"label": "Decision", "key": "retry", "properties": {"summary": "Retry three times"}, "source": "design.md"},
            {"label": "Ticket", "key": 42},
        ], "edges": [{"type": "INFORMS", "from_label": "Decision", "from_key": "retry", "to_label": "Ticket", "to_key": 42}]}))
    ticket = run("inspect", "Ticket:0042", "--json")
    assert ticket["node_id"] == "Ticket:42"
    run("retire", "Ticket:42", "--expected-revision", ticket["revision"])
    decision = run("inspect", "Decision:retry", "--json")
    run("retire", "Decision:retry", "--expected-revision", decision["revision"])
    with closing(GragService(config)) as service:
        assert service.cypher_query(QueryRequest(cypher="MATCH (d:Decision)-[:INFORMS]->(t:Ticket) RETURN d.summary,t.number")).rows == [["Retry three times", 42]]


@pytest.mark.parametrize("args", [
    ("remember", "test", "--operation-id", "retry-without-id"),
    ("retire", "Memory:missing", "--expected-revision", "absent"),
    ("inspect", "not-canonical"),
    ("inspect", "Memory:"),
    ("inspect", 'Memory) RETURN n //:x'),
    ("inspect", "Missing:x"),
])
def test_invalid_or_unknown_targets_fail_without_creating_memory(graph, args):
    config, run = graph
    run(*args, success=False)
    with closing(GragService(config)) as service:
        assert not service.describe_schema().node_tables


def test_retire_cannot_create_missing_node_or_overwrite_a_newer_edit(graph):
    _, run = graph
    run("remember", "exists", "--id", "other")
    error = run("retire", "Memory:missing", "--expected-revision", "0" * 64, success=False)
    assert "conflict" in error.err.lower()
    before = run("inspect", "Memory:other", "--json")
    run("remember", "newer", "--id", "other")
    run("retire", "Memory:other", "--expected-revision", before["revision"], success=False)
    assert run("context", "Memory:other", "--json")["included_node_ids"] == ["Memory:other"]


def test_retirement_clears_supersession_without_altering_replacement(graph):
    config, run = graph
    run("remember", "previous rule", "--id", "previous")
    run("remember", "current rule", "--id", "current")
    old = run("inspect", "Memory:previous", "--json")
    replacement = run("inspect", "Memory:current", "--json")
    with closing(GragService(config)) as service:
        service.upsert_nodes(UpsertNodesRequest.model_validate({"nodes": [{
            "label": "Memory", "key": "previous", "expected_revision": old["revision"],
            "evidence": {"state": "superseded", "superseded_by": "Memory:current"},
        }]}))
    old = run("inspect", "Memory:previous", "--json")
    run("retire", "Memory:previous", "--expected-revision", old["revision"])
    retired = run("inspect", "Memory:previous", "--json")
    assert retired["node"]["_evidence_state"] == "retracted" and not retired["node"].get("_superseded_by")
    assert run("inspect", "Memory:current", "--json")["revision"] == replacement["revision"]


def test_history_human_output_includes_entries_and_continuation(graph):
    _, run = graph
    saved = run("remember", "tracked", "--id", "one", "--track-history", "--reason", "initial", "--json")
    run("remember", "two", "--id", "one", "--reason", "correction", "--expected-revision", saved["revision"])
    output = run("context", "Memory:one", "--history")
    assert '"history"' in output.out and "correction" in output.out and "next_before" in output.out
    run("context", "Memory:one", "--history-before", "1", success=False)
    run("context", "Memory:one", "Memory:other", "--history", success=False)


def test_new_memory_commands_use_existing_owner(graph, monkeypatch):
    config, run = graph
    with TestClient(create_app(config)) as http:
        monkeypatch.setattr("grag.admin.find_server", lambda db: ServerInfo(8471, "0.9.0", True, None))

        class Borrowed:
            def request(self, *args, **kwargs):
                return http.request(*args, **kwargs)

            def close(self):
                pass

        monkeypatch.setattr("grag.client.httpx2.Client", lambda **kw: Borrowed())
        monkeypatch.setattr("grag.service.GragService", lambda *a: pytest.fail("CLI attempted a second writer"))
        run("remember", "shared", "--id", "shared", "--track-history")
        before = run("inspect", "Memory:shared", "--json")
        # Direct owner setup marks a reviewed memory with an expiry. A CLI
        # patch through HTTP must preserve those omitted fields.
        service = http.app.state.service
        service.upsert_nodes(UpsertNodesRequest.model_validate({"nodes": [{
            "label": "Memory", "key": "shared", "expected_revision": before["revision"],
            "evidence": {"review": "accepted", "expires_at": "2099-01-01T00:00:00+00:00"},
        }]}))
        before = run("inspect", "Memory:shared", "--json")
        retired = run("retire", "Memory:shared", "--expected-revision", before["revision"], "--operation-id", "retire-shared", "--json")
        replay = run("retire", "Memory:shared", "--expected-revision", before["revision"], "--operation-id", "retire-shared")
        assert "Replayed the earlier operation" in replay.out
        assert run("inspect", "Memory:shared", "--json")["revision"] == retired["revision"]
        node = run("inspect", "Memory:shared", "--json")["node"]
        assert node["_review_state"] == "accepted" and node["_expires_at"].startswith("2099-01-01")
        assert len(run("context", "Memory:shared", "--history", "--json")["history"]["entries"]) == 3
        # The generic typed client must still transmit explicit nulls, while
        # leaving omitted evidence fields alone. Empty patches enable history.
        with GraphClient(config) as client:
            result = client.call("upsert_nodes", UpsertNodesRequest.model_validate({"nodes": [{
                "label": "Memory", "key": "shared", "expected_revision": retired["revision"],
                "evidence": {"expires_at": None},
            }]}))
            client.call("upsert_nodes", UpsertNodesRequest.model_validate({"nodes": [{
                "label": "Memory", "key": "shared", "expected_revision": result["revisions"]["Memory:shared"],
                "evidence": {},
            }]}))
        node = run("inspect", "Memory:shared", "--json")["node"]
        assert "_expires_at" not in node and node["_review_state"] == "accepted"
        assert node["_evidence_state"] == "retracted"
