"""Measured stateful workflows, using only a disposable graph and checkout."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from grag.config import GragConfig
from grag.core.errors import ConflictError, FreshnessError
from grag.core.types import (
    ContextRequest,
    QueryRequest,
    ReadPolicy,
    SearchRequest,
    UpsertNodesRequest,
)
from grag.project_files import apply_ops
from grag.project_identity import identity_ops, new_identity
from grag.relocation import relocate_checkout
from grag.service import GragService
from workflow_eval import BUFFER, materialize, populate, token_counts


def timed(function, *args, **kwargs):
    started = time.perf_counter()
    value = function(*args, **kwargs)
    return value, (time.perf_counter() - started) * 1000


def query(service, text, timeout=60000):
    return service.cypher_query(QueryRequest(cypher=text, freshness="require", freshness_timeout_ms=timeout))


def fresh(service):
    return service.read_freshness(ReadPolicy(freshness="require", freshness_timeout_ms=60000))


def upsert(service, key, body, source, *, revision=None, operation=None):
    return service.upsert_nodes(UpsertNodesRequest.model_validate({
        "operation_id": operation, "nodes": [{"label": "Decision", "key": key,
            "properties": {"body": body}, "source": str(source), "expected_revision": revision}],
    }))


def run_scenarios(directory, *, source_files=40, git=False, encodings=None):
    import grag.refresh as refresh

    encodings = encodings or {}
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    root = directory / "before"
    data = materialize(root)
    for i in range(source_files):
        (root / f"generated_{i}.py").write_text(
            f"def unused_{i}():\n    return {i}\n" + "# workload padding for source verification\n" * 100,
            encoding="utf-8",
        )
    if git:
        command = shutil.which("git")
        assert command, "Git fixture requires git on PATH"
        for args in (["init", "-q"], ["add", "."],
                     ["-c", "user.name=grag evaluation", "-c", "user.email=eval@example.invalid",
                      "-c", "commit.gpgsign=false", "commit", "-qm", "disposable fixture"]):
            subprocess.run([command, "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603
    config = GragConfig(db_path=directory / "scenarios.lbdb", embedder=None, buffer_pool_size=BUFFER)
    service = GragService(config)
    report = {"git": git, "source_files": source_files + 3, "source_bytes": sum(p.stat().st_size for p in root.glob("*.py"))}
    try:
        refs = populate(service, root, data)
        coverage = service.cypher_query(QueryRequest(cypher="MATCH (f:Function) RETURN count(f)")).rows[0][0]
        assert coverage == source_files + 7
        report["indexed_functions"] = coverage
        service.enable_auto_refresh()
        scans = []
        original_scan = refresh.scan_sources

        def scan(path, request):
            result, elapsed = timed(original_scan, path, request)
            scans.append({"ms": elapsed, "files_hashed": len(result.files),
                          "bytes_hashed": sum(Path(p).stat().st_size for p in result.files)})
            return result

        with patch.object(refresh, "scan_sources", scan):
            _, first_ms = timed(fresh, service)
            warm = [timed(fresh, service)[1] for _ in range(3)]
        assert len(scans) == 4 and all(s["bytes_hashed"] == report["source_bytes"] for s in scans)
        report["source_verification"] = {"first_require_ms": first_ms, "warm_require_ms": warm, "scans": scans}

        source = root / "auth.py"
        function_query = "MATCH (f:Function {name:'refresh_session'}) RETURN f.id, f.line_start, f._source"
        before = query(service, function_query).rows[0]
        source.write_text("# source moved down between agent sessions\n\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        held_scans = []

        def held(path, request):
            held_scans.append(str(path))
            entered.set()
            assert release.wait(10), "Verification test did not release its worker"
            return original_scan(path, request)

        with patch.object(refresh, "scan_sources", held):
            try:
                with ThreadPoolExecutor(max_workers=4) as readers:
                    pending = [readers.submit(service.read_freshness, ReadPolicy(freshness="wait", freshness_timeout_ms=80)) for _ in range(4)]
                    assert entered.wait(5)
                    reports = [future.result(timeout=5) for future in pending]
                assert len(held_scans) == 1 and all(r.timed_out for r in reports)
                shared_scans = len(held_scans)
                stale = service.cypher_query(QueryRequest(cypher=function_query))
                assert stale.rows[0] == before and stale.freshness.status == "checking"
                started = time.perf_counter()
                try:
                    query(service, function_query, timeout=25)
                    raise AssertionError("require returned unverified code")
                except FreshnessError as error:
                    assert error.freshness["timed_out"]
                require_ms = (time.perf_counter() - started) * 1000
                assert require_ms < 2000, "Freshness deadline did not bound the wait"
            finally:
                release.set()
            after = query(service, function_query).rows[0]
        assert after == [before[0], before[1] + 2, before[2]]
        report["freshness_deadline"] = {"require_timeout_ms": 25, "observed_ms": require_ms,
                                        "waiting_readers": 4, "coalesced_scans": shared_scans,
                                        "total_scans_including_refresh_verification": len(held_scans),
                                        "stale_read_marked": True, "refreshed_citation": after}

        # Actual correction, conflict and restart; never treat lexical ranking as task priority.
        decision = query(service, "MATCH (d:Decision {id:'authorization-cache'}) RETURN d").rows[0][0]
        corrected = "Correction: cached access expires after 5 seconds so revoked permissions stop sooner."
        note = root / "notes" / "authorization-cache.md"
        note.write_text(corrected, encoding="utf-8")
        saved = upsert(service, "authorization-cache", corrected, note, revision=decision["_revision"], operation="workflow-correction")
        assert not saved.warnings
        try:
            upsert(service, "authorization-cache", "old policy", note, revision=decision["_revision"])
            raise AssertionError("Stale correction was accepted")
        except ConflictError:
            pass
        response = service.search_knowledge(SearchRequest(query="cached access expires 5 seconds", labels=["Decision"], top_k=1, hops=0, token_budget=2000))
        assert corrected in response.context and "15 seconds" not in response.context
        report["correction"] = {"readback": corrected, "conflict_refused": True, "operation_id": saved.operation_id}

        # Fetch all pages in order, using only the returned cursor and hash.
        full = query(service, "MATCH (n:Note {id:'rollout'}) RETURN n.body").rows[0][0]
        initial = service.get_context(ContextRequest(node_ids=["Note:rollout"], hops=0, token_budget=1000))
        assert initial.truncated and "service accounts must reauthenticate immediately" not in initial.context
        pages, offset, digest, texts = [], 0, None, []
        while True:
            response, elapsed = timed(service.get_context, ContextRequest(
                node_ids=["Note:rollout"], hops=0, token_budget=1000, text_property="body",
                text_offset=offset, text_sha256=digest,
            ))
            from grag.retrieval.packing import mcp_retrieval_text
            page = response.text_page
            assert page and page.offset == offset
            texts.append(response.subgraph.nodes[0].properties["body"])
            pages.append({"ms": elapsed, "tokens": token_counts(mcp_retrieval_text(response), encodings),
                          "offset": offset, "next_offset": page.next_offset})
            if page.next_offset is None:
                break
            assert page.next_offset > offset
            offset, digest = page.next_offset, page.sha256
        assert "".join(texts) == full
        report["long_text"] = {"initial_evidence_missing_but_reported": True, "complete_after_paging": True,
                               "retrieval_calls_including_initial": 1 + len(pages), "pages": pages,
                               "raw_body_tokens": token_counts(full, encodings)}

        # Two simultaneous writers/readers share one service. MCP transport is
        # measured separately by test_shared_agent_workflow.py.
        barrier = threading.Barrier(4)

        def client(number):
            samples = []
            barrier.wait(timeout=10)
            for i in range(12):
                if number < 2:
                    result, elapsed = timed(upsert, service, f"client-{number}", f"Shared memory revision {i}", note,
                                            operation=f"client-{number}-revision-{i}")
                    assert not result.warnings
                else:
                    result, elapsed = timed(service.search_knowledge, SearchRequest(query="shared memory", labels=["Decision"], top_k=2))
                    assert result.vector_status != "error"
                samples.append(elapsed)
            return samples

        with ThreadPoolExecutor(max_workers=4) as pool:
            work = [pool.submit(client, i) for i in range(4)]
            report["concurrency"] = {"clients": 4, "writes": 24, "reads": 24,
                                     "latencies_ms": [future.result(timeout=60) for future in work]}
        for number in range(2):
            assert query(service, f"MATCH (d:Decision {{id:'client-{number}'}}) RETURN d.body").rows == [["Shared memory revision 11"]]
    finally:
        service.close()

    service = GragService(config)
    try:
        service.enable_auto_refresh()
        assert query(service, "MATCH (d:Decision {id:'authorization-cache'}) RETURN d.body").rows == [[corrected]]
        replay = upsert(service, "authorization-cache", corrected, note, revision=decision["_revision"], operation="workflow-correction")
        assert replay.replayed
        next_task = query(service, "MATCH (t:Task) WHERE t.status='open' RETURN t.id ORDER BY t.priority LIMIT 1")
        assert next_task.rows == [["first"]]
        report["restart"] = {"correction_retained": True, "receipt_replayed": True, "next_task": "first"}
    finally:
        service.close()

    # The saved database lives outside the moved checkout, like the CTO report.
    # Client files and backups are isolated; no user's harness configuration is read/written.
    identity = new_identity(root, config.db_path, 43211)
    with patch.object(Path, "home", return_value=directory / "isolated-home"):
        apply_ops(list(identity_ops(root, identity)))
    entry = {"command": str(root / ".venv" / "bin" / "grag"), "args": ["--db", str(config.db_path), "mcp", "--port", "43211"]}
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"grag": entry, "unrelated": {"command": "keep"}}}))
    moved = directory / "after"
    root.rename(moved)
    with patch.object(Path, "home", return_value=directory / "isolated-home"):
        relocate_checkout(config, root, moved)
    client_config = json.loads((moved / ".mcp.json").read_text())["mcpServers"]
    assert client_config["unrelated"] == {"command": "keep"}
    assert str(root) not in json.dumps(client_config)
    service = GragService(config)
    try:
        service.enable_auto_refresh()
        citation = query(service, function_query).rows[0]
        assert citation == [before[0], before[1] + 2, str(moved / "auth.py")]
        linked = query(service, "MATCH (d:Decision {id:'authorization-cache'})-[:EXPLAINS]->(f:Function) RETURN d.body, d._source, f.id")
        assert linked.rows == [[corrected, str(moved / "notes" / "authorization-cache.md"), refs["Function:read_cached_policy"].split(":", 1)[1]]]
        report["relocation"] = {"same_database": str(config.db_path), "citation": citation,
                                "authored_link_preserved": True, "client_path_repaired": True}
    finally:
        service.close()
    return report
