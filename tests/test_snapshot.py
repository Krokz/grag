"""M12: completed snapshots, isolated publication and durable agent continuity."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import ConflictError, GragError
from grag.core.mutate import define_schema, upsert_nodes
from grag.core.revisions import content_revision
from grag.core.types import (
    ContextRequest,
    DefineSchemaRequest,
    EvidenceUpdate,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    SearchRequest,
    UpsertEdge,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.retrieval.context import get_context
from grag.retrieval.search import search_knowledge
from grag.transfer import Contents, _dump, export_lines, import_from, read_archive
from grag.transfer_io import atomic_output, restore_file


def _seed(engine):
    define_schema(
        engine,
        engine.config,
        DefineSchemaRequest(
            node_tables=[
                NodeTableSpec(
                    name="Note", searchable=True, properties=[PropertySpec(name="body")]
                )
            ],
            rel_tables=[RelTableSpec(name="LINKS", from_label="Note", to_label="Note")],
        ),
    )
    req = UpsertNodesRequest(
        operation_id="first-write",
        nodes=[
            UpsertNode(
                label="Note",
                key="n",
                properties={"body": "cache policy fifteen seconds 🌍"},
                source="review.md",
                evidence=EvidenceUpdate(actor="Claude Code", review="accepted"),
            )
        ],
    )
    upsert_nodes(engine, engine.config, req)
    return req


def _node(engine):
    return engine.execute("MATCH (n:Note {id:'n'}) RETURN n").rows[0][0]


def _signed(records):
    """Deliberately construct a checksummed but semantically invalid archive."""
    contents = Contents(records[1])
    for r in records[2:]:
        if r["type"] in {"node", "edge"}:
            contents.add(r)
    lines = [_dump(r) for r in records]
    return [
        *lines,
        _dump(
            {
                "type": "complete",
                "lines": len(lines),
                "sha256": hashlib.sha256(
                    ("\n".join(lines) + "\n").encode()
                ).hexdigest(),
                "tables": contents.manifest(),
            }
        ),
    ]


def _backup(engine, tmp_path):
    backup = tmp_path / "snapshot.jsonl"
    backup.write_text("\n".join(export_lines(engine)) + "\n", encoding="utf-8")
    return backup


def test_history_receipts_guards_and_search_survive_verified_reopen(engine, tmp_path):
    first = _seed(engine)
    initial = content_revision(_node(engine))
    edit = UpsertNodesRequest(
        operation_id="second-write",
        nodes=[
            UpsertNode(
                label="Note",
                key="n",
                properties={"body": "cache policy five seconds"},
                expected_revision=initial,
                evidence=EvidenceUpdate(
                    actor="Cursor", reason="measured", review="accepted"
                ),
            )
        ],
    )
    upsert_nodes(engine, engine.config, edit)
    revision = content_revision(_node(engine))
    backup = _backup(engine, tmp_path)
    cfg = GragConfig(db_path=tmp_path / "restored.lbdb", buffer_pool_size=128 * 1024**2)
    report = restore_file(cfg, str(backup))
    assert report["verified"] and report["reopen_verified"]
    assert (
        report["nodes"] == 1
        and report["history"] == 2
        and report["retry_receipts"] == 2
    )
    with Engine(cfg) as target:
        assert content_revision(_node(target)) == revision
        assert upsert_nodes(target, cfg, first).replayed
        assert upsert_nodes(target, cfg, edit).replayed
        assert _node(target)["body"] == "cache policy five seconds"
        with pytest.raises(ConflictError):
            upsert_nodes(
                target, cfg, edit.model_copy(update={"operation_id": "different"})
            )
        with pytest.raises(ConflictError):
            upsert_nodes(target, cfg, first.model_copy(update={"nodes": edit.nodes}))
        history = get_context(
            target,
            cfg,
            ContextRequest(node_ids=["Note:n"], history=True, token_budget=3000),
        )
        assert [(h.sequence, h.actor) for h in history.history.entries] == [
            (2, "Cursor"),
            (1, "Claude Code"),
        ]
        old = get_context(
            target,
            cfg,
            ContextRequest(node_ids=["Note:n"], revision=1, token_budget=3000),
        )
        assert "fifteen seconds" in old.context and "🌍" in old.context
        result = search_knowledge(
            target, cfg, SearchRequest(query="cache policy", token_budget=3000)
        )
        assert result.included_node_ids == ["Note:n"]
        upsert_nodes(
            target,
            cfg,
            UpsertNodesRequest(
                nodes=[
                    UpsertNode(
                        label="Note",
                        key="n",
                        expected_revision=revision,
                        evidence=EvidenceUpdate(state="retracted"),
                    )
                ]
            ),
        )
        assert not search_knowledge(
            target, cfg, SearchRequest(query="cache policy")
        ).included_node_ids
        assert (
            get_context(
                target,
                cfg,
                ContextRequest(node_ids=["Note:n"], history=True, evidence="all"),
            )
            .history.entries[0]
            .sequence
            == 3
        )


def test_replaying_archive_is_noop_after_later_edit(engine):
    _seed(engine)
    lines = list(export_lines(engine))
    with Engine(GragConfig(db_path=":memory:")) as target:
        import_from(target, target.config, lines)
        upsert_nodes(
            target,
            target.config,
            UpsertNodesRequest(
                nodes=[UpsertNode(label="Note", key="n", properties={"body": "later"})]
            ),
        )
        report = import_from(target, target.config, lines)
        assert report["replayed"] and _node(target)["body"] == "later"
        with pytest.raises(GragError, match="not empty"):
            import_from(target, target.config, list(export_lines(engine)))


def test_relationship_revision_and_receipt_survive_restore(engine):
    _seed(engine)
    request = UpsertNodesRequest(nodes=[], operation_id="relationship", edges=[UpsertEdge(
        type="LINKS", from_label="Note", from_key="n", to_label="Note", to_key="n",
        source="review.md")])
    first = upsert_nodes(engine, engine.config, request)
    old = first.revisions["LINKS:Note:n->Note:n"]
    assert old.startswith("r2:")
    with Engine(GragConfig(db_path=":memory:")) as target:
        import_from(target, target.config, export_lines(engine))
        assert upsert_nodes(target, target.config, request).replayed
        row = target.execute("MATCH ()-[r:LINKS]->() RETURN r").rows[0][0]
        assert content_revision(row) == old
        edit = UpsertNodesRequest(nodes=[], edges=[request.edges[0].model_copy(update={"expected_revision": old})])
        upsert_nodes(target, target.config, edit)


@pytest.mark.parametrize(
    "fault",
    [
        "truncated",
        "tampered",
        "extra",
        "duplicate_json",
        "missing_schema",
        "wrong_type",
    ],
)
def test_invalid_archive_never_creates_destination(engine, tmp_path, fault):
    _seed(engine)
    lines = list(export_lines(engine))
    if fault == "truncated":
        lines.pop()
    elif fault == "tampered":
        lines = [line.replace("fifteen", "sixty") for line in lines]
    elif fault == "extra":
        lines.append(lines[0])
    elif fault == "duplicate_json":
        lines[0] = lines[0].replace("{", '{"type":"grag_export",', 1)
    elif fault == "missing_schema":
        del lines[1]
    elif fault == "wrong_type":
        lines[0] = '{"type": []}'
    source = tmp_path / "invalid.jsonl"
    source.write_text("\n".join(lines), encoding="utf-8")
    target = tmp_path / "new-folder" / "target.lbdb"
    with pytest.raises(GragError):
        restore_file(GragConfig(db_path=target), str(source))
    assert not target.parent.exists()


@pytest.mark.parametrize(
    "fault",
    ["duplicate_pk", "missing_endpoint", "schema_type", "registry", "exception"],
)
def test_restore_failure_rolls_back_schema_data_and_internal_state(
    engine, monkeypatch, fault
):
    import grag.transfer as transfer

    _seed(engine)
    records = [json.loads(line) for line in export_lines(engine)][:-1]
    if fault == "duplicate_pk":
        records.append(next(r for r in records if r.get("label") == "Note"))
    elif fault == "missing_endpoint":
        records.append(
            {
                "type": "edge",
                "rel": "LINKS",
                "from": "n",
                "to": "absent",
                "properties": {},
                "source": None,
            }
        )
    elif fault == "schema_type":
        records[1]["node_tables"][0]["properties"][0]["type"] = []
    elif fault == "registry":
        next(r for r in records if r.get("label") == "_grag_tables")["properties"][
            "pk"
        ] = "wrong"
    else:
        real = transfer._restore_record

        def fail(*args):
            real(*args)
            raise RuntimeError("interrupted restore")

        monkeypatch.setattr(transfer, "_restore_record", fail)
    with Engine(GragConfig(db_path=":memory:")) as target:
        with pytest.raises((GragError, RuntimeError)):
            import_from(target, target.config, _signed(records))
        assert [
            row[1] for row in target.execute("CALL SHOW_TABLES() RETURN *").rows
        ] == ["_grag_meta"]
        assert (
            target.execute(
                "MATCH (m:_grag_meta {key:'restored_archive_sha256'}) RETURN m"
            ).rows
            == []
        )


def test_external_typed_tables_nulls_aliases_and_parallel_edges_roundtrip(engine):
    engine.execute_write(
        "CREATE NODE TABLE External(id INT64 PRIMARY KEY, __key STRING, day DATE, moment TIMESTAMP, score DOUBLE)"
    )
    engine.execute_write(
        "CREATE REL TABLE NEXT(FROM External TO External, __from STRING)"
    )
    engine.execute_write(
        "CREATE (:External {id: 42, __key: 'original', day: DATE('2026-09-07'), moment: TIMESTAMP('2026-09-07 12:34:56.123456'), score: 1.125})"
    )
    for _ in range(2):
        engine.execute_write(
            "MATCH (a:External) CREATE (a)-[:NEXT {__from:'same'}]->(a)"
        )
    lines = list(export_lines(engine))
    with Engine(GragConfig(db_path=":memory:")) as target:
        report = import_from(target, target.config, lines)
        assert report["nodes"] == 1 and report["edges"] == 2
        assert (
            target.execute(
                "MATCH (n:External) RETURN n.id, n.__key, n.day, n.moment, n.score"
            ).rows
            == engine.execute(
                "MATCH (n:External) RETURN n.id, n.__key, n.day, n.moment, n.score"
            ).rows
        )


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE NODE TABLE Unsupported(id INT64 PRIMARY KEY, values STRING[])",
        "CREATE NODE TABLE Unsupported(id INT64 PRIMARY KEY, value STRING DEFAULT 'custom')",
        "CREATE NODE TABLE _unknown(id STRING PRIMARY KEY)",
    ],
)
def test_unsupported_storage_is_explicit_not_silently_dropped(engine, ddl):
    engine.execute_write(ddl)
    with pytest.raises(GragError):
        next(export_lines(engine))


def test_snapshot_serializes_data_and_schema_but_releases_writer_before_delivery(
    engine, monkeypatch
):
    import grag.transfer as transfer

    _seed(engine)
    entered, release, writer_started, writer_done = (
        threading.Event() for _ in range(4)
    )
    real = transfer._records

    def slow(*args):
        entered.set()
        assert release.wait(5)
        yield from real(*args)

    monkeypatch.setattr(transfer, "_records", slow)

    def capture():
        # Capture completes on the creating thread; the rest is just a spool.
        iterator = export_lines(engine)
        first = next(iterator)
        return first, iterator

    def mutate():
        writer_started.set()
        with engine.write_transaction():
            engine.execute_write("MATCH (n:Note) SET n.body='after'")
            engine.execute_write("CREATE NODE TABLE Later(id STRING PRIMARY KEY)")
        writer_done.set()

    with ThreadPoolExecutor(2) as pool:
        export = pool.submit(capture)
        assert entered.wait(5)
        writer = pool.submit(mutate)
        assert writer_started.wait(5)
        assert not writer_done.wait(0.05)
        release.set()
        first, iterator = export.result(timeout=5)
        writer.result(timeout=5)
        lines = [first, *iterator]
    with read_archive(lines) as archive:
        assert "Later" not in archive.tables
        assert next(r for r in archive.records() if r.get("label") == "Note")[
            "properties"
        ]["body"].startswith("cache policy")
    assert _node(engine)["body"] == "after"


@pytest.mark.parametrize("fault", ["download", "truncated", "disk"])
def test_failed_export_preserves_previous_backup(engine, tmp_path, fault):
    out = tmp_path / "backup.jsonl"
    out.write_text("previous backup")
    with (
        pytest.raises((GragError, OSError)),
        atomic_output(str(out), database=engine.config.db_path) as stream,
    ):
        if fault == "truncated":
            stream.write(
                '{"type":"grag_export","format_version":2,"history":"preserved","retry_receipts":"preserved"}\n'
            )
        else:
            stream.write("partial download")
            raise OSError(fault)
    assert out.read_text() == "previous backup"
    assert not list(tmp_path.glob(".*.partial-*"))


def test_output_cannot_replace_database_sidecar_or_hardlink(tmp_path):
    db = tmp_path / "source.lbdb"
    db.write_bytes(b"original")
    alias = tmp_path / "alias"
    os.link(db, alias)
    for path in (db, alias, tmp_path / "source.lbdb.wal"):
        with pytest.raises(GragError), atomic_output(str(path), database=db):
            pytest.fail("unsafe output admitted")
    assert db.read_bytes() == b"original"


def test_stdout_keeps_utf8_lf_checksum_bytes_on_windows_text_stream(engine, monkeypatch):
    _seed(engine)
    data = "\n".join(export_lines(engine)) + "\n"
    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", output)
        with atomic_output(None, database=engine.config.db_path) as stream:
            stream.write(data)
    assert raw.getvalue() == data.encode("utf-8")
    with read_archive(raw.getvalue().decode("utf-8").splitlines(keepends=True)) as archive:
        assert archive.report()["verified"]


@pytest.mark.parametrize("fault", ["checkpoint", "reopen", "race", "sidecar"])
def test_restore_never_publishes_before_verification_or_clobbers_target(
    engine, tmp_path, monkeypatch, fault
):
    import grag.transfer_io as transfer_io

    _seed(engine)
    source = _backup(engine, tmp_path)
    target = tmp_path / "target.lbdb"
    cfg = GragConfig(db_path=target, buffer_pool_size=128 * 1024**2)
    if fault == "checkpoint":
        real = Engine.execute_write

        def checkpoint(self, query, *args, **kwargs):
            if query == "CHECKPOINT":
                raise GragError("checkpoint failure")
            return real(self, query, *args, **kwargs)

        monkeypatch.setattr(Engine, "execute_write", checkpoint)
    elif fault == "sidecar":
        target.with_suffix(".lbdb.wal").write_text("original")
    else:
        real_verify = transfer_io.verify_contents

        def verify(*args):
            real_verify(*args)
            if fault == "race":
                target.write_text("racing owner")
            else:
                raise GragError("reopen verification failure")

        monkeypatch.setattr(transfer_io, "verify_contents", verify)
    with pytest.raises(GragError):
        restore_file(cfg, str(source))
    if fault == "race":
        assert target.read_text() == "racing owner"
    else:
        assert not target.exists()
    assert source.read_text(encoding="utf-8").endswith("\n")
    assert not list(tmp_path.glob(".grag-restore-*"))


@pytest.mark.parametrize("phase", ["export", "restore"])
def test_process_death_leaves_identifiable_staging_only(engine, tmp_path, phase):
    _seed(engine)
    source = _backup(engine, tmp_path)
    target = tmp_path / ("new.lbdb" if phase == "restore" else "backup.jsonl")
    if phase == "export":
        target.write_text("previous backup")
    script = """
import os, sys
from pathlib import Path
from grag.config import GragConfig
from grag.transfer_io import atomic_output, restore_file
import grag.transfer as transfer
phase, source, target = sys.argv[1:]
if phase == 'export':
    with atomic_output(target, database=Path(source)) as stream:
        stream.write('incomplete')
        stream.flush()
        os._exit(73)
else:
    real = transfer._restore_record
    def crash(*args):
        real(*args)
        os._exit(73)
    transfer._restore_record = crash
    restore_file(GragConfig(db_path=target, buffer_pool_size=128*1024**2), source)
"""
    result = subprocess.run(  # noqa: S603 — fixed subprocess over disposable fixtures
        [sys.executable, "-c", script, phase, str(source), str(target)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 73, result.stderr
    if phase == "export":
        assert target.read_text() == "previous backup"
        assert list(tmp_path.glob(".backup.jsonl.partial-*"))
    else:
        assert not target.exists()
        assert list(tmp_path.glob(".grag-restore-*"))


def test_legacy_requires_explicit_acceptance_and_reports_lost_continuity(
    engine, tmp_path
):
    legacy = [
        json.dumps(r)
        for r in [
            {"type": "grag_export", "format_version": 1},
            {
                "type": "schema",
                "node_tables": [
                    {
                        "name": "Note",
                        "primary_key": "id",
                        "searchable": True,
                        "properties": [{"name": "body", "type": "STRING"}],
                    }
                ],
                "rel_tables": [],
            },
            {
                "type": "node",
                "label": "Note",
                "key": "n",
                "properties": {"body": "legacy text"},
            },
        ]
    ]
    with pytest.raises(GragError, match="allow-legacy"):
        import_from(engine, engine.config, legacy)
    report = import_from(engine, engine.config, legacy, allow_legacy=True)
    assert not report["verified"] and report["warnings"]
    assert report["history"] == report["retry_receipts"] == 0
    assert _node(engine)["body"] == "legacy text"
