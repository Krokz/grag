"""Ingestion tests: chunking behavior, idempotent upserts, metadata
roundtrip, FTS findability, ingest_paths file formats, and the examples/
scripts end to end."""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

from grag.config import GragConfig
from grag.core.types import IngestDocument, IngestRequest, SearchRequest
from grag.ingest.loaders import (
    _chunk_text,
    _source_identity,
    ingest_documents,
    ingest_paths,
)
from grag.service import GragService

ROOT = Path(__file__).resolve().parents[1]

# --- chunking ---------------------------------------------------------------------


def test_chunking_respects_size_and_overlap():
    text = " ".join(f"Sentence number {i} ends here." for i in range(12))
    chunks = _chunk_text(text, size=80, overlap=20)

    assert len(chunks) > 3
    assert all(len(c) <= 80 for c in chunks)
    # every sentence survives in at least one chunk
    for i in range(12):
        assert any(f"Sentence number {i} ends here." in c for c in chunks)
    # overlap: each chunk opens with a word carried over from its predecessor
    for prev, nxt in itertools.pairwise(chunks):
        assert nxt.split()[0] in prev.split()
    # no mid-word splits: every original word appears intact
    words = set(text.split())
    seen = set(" ".join(chunks).split())
    assert words <= seen


def test_chunking_prefers_paragraph_boundaries():
    p1 = "Alpha paragraph. Short."
    p2 = "Beta paragraph. Also short."

    # both paragraphs fit: one chunk, paragraph break preserved
    assert _chunk_text(f"{p1}\n\n{p2}", size=200, overlap=20) == [f"{p1}\n\n{p2}"]

    # tight budget: paragraphs land in separate chunks, neither is split
    chunks = _chunk_text(f"{p1}\n\n{p2}", size=30, overlap=20)
    assert chunks[0] == p1
    assert chunks[-1].endswith(p2)
    assert all(len(c) <= 30 for c in chunks)


def test_chunking_falls_back_to_sentences_for_long_paragraph():
    para = "First sentence is here. Second sentence follows it. Third one trails."
    chunks = _chunk_text(para, size=40, overlap=10)
    assert chunks == [
        "First sentence is here.",
        "is here. Second sentence follows it.",
        "it. Third one trails.",
    ]


def test_chunking_never_splits_mid_word_when_avoidable():
    chunks = _chunk_text("alpha beta gamma delta epsilon", size=12, overlap=0)
    assert chunks == ["alpha beta", "gamma delta", "epsilon"]


def test_chunking_hard_splits_an_overlong_word():
    word = "x" * 120
    chunks = _chunk_text(word, size=50, overlap=10)
    assert all(len(c) <= 50 for c in chunks)
    assert chunks[0] == "x" * 50 and chunks[1] == "x" * 50
    assert chunks[-1].endswith("x" * 20)


def test_chunking_skips_empty_chunks():
    text = "Real content here.\n\n\n\n   \n\nMore content there."
    chunks = _chunk_text(text, size=200, overlap=10)
    assert chunks == ["Real content here.\n\nMore content there."]


# --- ingest_documents -------------------------------------------------------------


def _count_nodes(engine, label="Chunk") -> int:
    return int(engine.execute(f"MATCH (n:{label}) RETURN count(*)").rows[0][0])


def _doc_id(
    source: str | None, text: str, metadata: dict[str, object] | None = None
) -> str:
    return f"{_source_identity(source, text, metadata or {})}#0000"


def test_ingest_chunk_false_one_node_per_document(engine):
    req = IngestRequest(
        documents=[
            IngestDocument(text="hello world", source="dir/my notes.md"),
            IngestDocument(text="second doc", source=None),
        ],
        chunk=False,
    )
    resp = ingest_documents(engine, engine.config, req)

    assert resp.label == "Chunk"
    assert resp.nodes_created == 2
    rows = engine.execute(
        "MATCH (c:Chunk) RETURN c.id, c.text, c._source ORDER BY c.id"
    ).rows
    # IDs carry a readable basename plus a source digest; provenance keeps raw input.
    assert rows == [
        [_doc_id(None, "second doc"), "second doc", None],
        [_doc_id("dir/my notes.md", "hello world"), "hello world", "dir/my notes.md"],
    ]


def test_ingest_chunked_ids_and_counts(engine):
    text = "\n\n".join(f"Paragraph {i}. " + "padding " * 20 for i in range(4))
    req = IngestRequest(
        documents=[IngestDocument(text=text, source="big.md")],
        chunk=True,
        chunk_size=200,
        chunk_overlap=30,
    )
    resp = ingest_documents(engine, engine.config, req)

    assert resp.nodes_created > 1  # ~700 chars of text, 200-char chunks
    rows = engine.execute("MATCH (c:Chunk) RETURN c.id ORDER BY c.id").rows
    assert [r[0] for r in rows] == [
        f"{_source_identity('big.md', text, {})}#{i:04d}"
        for i in range(resp.nodes_created)
    ]


def test_ingest_is_idempotent(engine):
    req = IngestRequest(
        documents=[
            IngestDocument(text="alpha\n\nbeta\n\ngamma", source="idem.md"),
            IngestDocument(text="solo", source="other.md", metadata={"v": 1}),
        ],
        chunk_size=10,
        chunk_overlap=3,
    )
    first = ingest_documents(engine, engine.config, req)
    second = ingest_documents(engine, engine.config, req)

    assert second.nodes_created == first.nodes_created
    assert _count_nodes(engine) == first.nodes_created


def test_ingest_same_basename_sources_do_not_collide(engine):
    req = IngestRequest(
        documents=[
            IngestDocument(text="alpha", source="one/notes.md"),
            IngestDocument(text="beta", source="two/notes.md"),
        ],
        chunk=False,
    )
    ingest_documents(engine, engine.config, req)

    rows = engine.execute(
        "MATCH (c:Chunk) RETURN c.id, c.text, c._source ORDER BY c._source"
    ).rows
    assert rows == [
        [_doc_id("one/notes.md", "alpha"), "alpha", "one/notes.md"],
        [_doc_id("two/notes.md", "beta"), "beta", "two/notes.md"],
    ]
    assert rows[0][0] != rows[1][0]


def test_reingest_prunes_chunks_no_longer_in_source(engine):
    source = "shrinking.md"
    first = ingest_documents(
        engine,
        engine.config,
        IngestRequest(
            documents=[IngestDocument(text="alpha beta gamma delta", source=source)],
            chunk_size=8,
            chunk_overlap=0,
        ),
    )
    assert first.nodes_created > 1

    second = ingest_documents(
        engine,
        engine.config,
        IngestRequest(
            documents=[IngestDocument(text="short", source=source)],
            chunk=False,
        ),
    )
    assert second.nodes_created == 1
    assert second.nodes_pruned == first.nodes_created - 1
    assert _count_nodes(engine) == 1
    assert engine.execute("MATCH (c:Chunk) RETURN c.text").rows == [["short"]]


def test_reingest_prunes_across_equivalent_source_path_spellings(engine):
    relative = "docs/alias.md"
    absolute = str(Path(relative).resolve())
    first = ingest_documents(
        engine,
        engine.config,
        IngestRequest(
            documents=[IngestDocument(text="alpha beta gamma", source=relative)],
            chunk_size=6,
            chunk_overlap=0,
        ),
    )
    assert first.nodes_created > 1

    second = ingest_documents(
        engine,
        engine.config,
        IngestRequest(
            documents=[IngestDocument(text="short", source=absolute)],
            chunk=False,
        ),
    )
    assert second.nodes_pruned == first.nodes_created - 1
    assert _count_nodes(engine) == 1


def test_ingest_meta_json_roundtrip(engine):
    meta = {"author": "Ada", "tags": ["x", "y"], "n": 3}
    req = IngestRequest(
        documents=[
            IngestDocument(text="with meta", source="m.md", metadata=meta),
            IngestDocument(text="without meta", source="plain.md"),
        ],
        chunk=False,
    )
    ingest_documents(engine, engine.config, req)

    rows = {
        r[0]: r[1] for r in engine.execute("MATCH (c:Chunk) RETURN c.id, c.meta").rows
    }
    assert json.loads(rows[_doc_id("m.md", "with meta", meta)]) == meta
    assert rows[_doc_id("plain.md", "without meta")] is None


def test_ingest_custom_label(engine):
    req = IngestRequest(
        documents=[IngestDocument(text="labeled text", source="l.md")],
        label="Note",
        chunk=False,
    )
    resp = ingest_documents(engine, engine.config, req)
    assert resp.label == "Note"
    assert _count_nodes(engine, "Note") == 1


# --- retrieval over ingested content -------------------------------------------------


@pytest.fixture()
def service(tmp_path):
    svc = GragService(GragConfig(db_path=tmp_path / "svc.lbdb"))
    yield svc
    svc.close()


def test_ingested_content_findable_via_fts(service):
    service.ingest(
        IngestRequest(
            documents=[
                IngestDocument(
                    text="The cranberry indexer reconciles zeppelin metrics nightly.",
                    source="notes.md",
                ),
                IngestDocument(
                    text="Unrelated prose about ordinary kettle maintenance.",
                    source="other.md",
                ),
            ],
            chunk=False,
        )
    )
    resp = service.search_knowledge(SearchRequest(query="cranberry zeppelin", hops=0))

    assert resp.seeds
    expected = _doc_id(
        "notes.md", "The cranberry indexer reconciles zeppelin metrics nightly."
    )
    assert resp.seeds[0].node.id == f"Chunk:{expected}"
    assert resp.seeds[0].match == "fts"
    assert "cranberry" in resp.context


# --- ingest_paths --------------------------------------------------------------------


def test_ingest_paths_all_formats(tmp_path):
    (tmp_path / "a.md").write_text(
        "# Alpha\n\nMarkdown body about kestrels.", encoding="utf-8"
    )
    (tmp_path / "b.txt").write_text("Plain text about falcons.", encoding="utf-8")
    (tmp_path / "c.json").write_text(
        json.dumps(
            [
                {
                    "text": "json list doc about merlins",
                    "source": "c.json",
                    "metadata": {"k": 1},
                },
                {"text": "second json doc about harriers", "source": "c2.json"},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "d.json").write_text(
        json.dumps(
            {
                "documents": [
                    {"text": "wrapped json doc about ospreys", "source": "d.json"}
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "e.jsonl").write_text(
        '{"text": "jsonl one about hawks", "source": "e1.jsonl"}\n'
        "\n"
        '{"text": "jsonl two about eagles", "source": "e.jsonl"}\n',
        encoding="utf-8",
    )
    (tmp_path / "f.exe").write_bytes(b"MZ")

    cfg = GragConfig(db_path=tmp_path / "paths.lbdb")
    files = [
        tmp_path / n for n in ("a.md", "b.txt", "c.json", "d.json", "e.jsonl", "f.exe")
    ]
    summary = ingest_paths(cfg, files)

    # 7 documents from 5 files (md/txt one each, json list two, wrapped one, jsonl two)
    assert "7 document(s) from 5 file(s)" in summary
    assert "f.exe" in summary  # unsupported-extension warning collected

    from grag.core.engine import Engine

    eng = Engine(cfg)
    try:
        assert _count_nodes(eng) == 7  # each doc fits in one default-size chunk
        key = _doc_id("c.json", "json list doc about merlins", {"k": 1})
        row = eng.execute(
            "MATCH (c:Chunk) WHERE c.id = $key RETURN c.meta, c._source",
            {"key": key},
        ).rows[0]
        assert json.loads(row[0]) == {"k": 1}
        assert row[1] == "c.json"
    finally:
        eng.close()


def test_ingest_paths_missing_file_warns(tmp_path):
    cfg = GragConfig(db_path=tmp_path / "missing.lbdb")
    summary = ingest_paths(cfg, [tmp_path / "nope.md"])
    assert "nope.md" in summary
    assert "0 document(s)" in summary


# --- examples end to end ---------------------------------------------------------------


def test_example_scripts_run_end_to_end():
    build = subprocess.run(
        [sys.executable, "examples/build_example.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert build.returncode == 0, (
        f"build_example failed:\n{build.stdout}\n{build.stderr}"
    )
    assert "MENTIONS edges" in build.stdout

    demo = subprocess.run(
        [sys.executable, "examples/demo_e2e.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert demo.returncode == 0, f"demo_e2e failed:\n{demo.stdout}\n{demo.stderr}"
    assert "Who owns the ingestion gateway?" in demo.stdout
    assert "seeds:" in demo.stdout
