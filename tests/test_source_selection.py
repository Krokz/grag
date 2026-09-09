"""M14 source scope, synchronization and retained authored evidence."""

from __future__ import annotations

import json

import pytest

from grag.code_state import index_records, saved_request, scan_sources
from grag.core.types import (
    CodeIngestRequest,
    IngestDocument,
    IngestRequest,
    UpsertEdgesRequest,
)
from grag.ingest.code import ingest_code
from grag.ingest.loaders import ingest_documents, load_request


def write(root, name, text="def thing():\n    pass\n"):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def rows(engine, cypher):
    return engine.execute(cypher).rows


def test_ignore_rules_nested_worktree_and_file_roots(engine, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    write(root, ".gitignore", "ignored/\n*.secret.py\n!keep.secret.py\n")
    write(root, ".gragignore", "private.py\n")
    for path in (
        "a/main.py",
        "b/util.py",
        "ignored/leak.py",
        "x.secret.py",
        "keep.secret.py",
        "private.py",
        ".claude/worktrees/agent/copied.py",
    ):
        write(root, path)
    write(root, ".claude/worktrees/agent/.git", "gitdir: /unused")
    req = CodeIngestRequest(paths=[str(root)])
    result = ingest_code(engine, engine.config, req)
    assert result.modules == 3
    assert {r[0] for r in rows(engine, "MATCH (m:Module) RETURN m.path")} == {
        "a/main.py",
        "b/util.py",
        "keep.secret.py",
    }
    assert any("nested repository/worktree" in w for w in result.warnings)
    result = ingest_code(
        engine,
        engine.config,
        CodeIngestRequest(
            paths=[str(root / "a/main.py"), str(root / "b/util.py")],
            root=str(root),
            replace_scope=True,
        ),
    )
    assert result.repos == 1 and result.modules == 2 and result.nodes_pruned == 2
    saved = saved_request(root, index_records(engine)[str(root)]["_index_options"])
    assert set(scan_sources(root, saved).files) == {
        str(root / "a/main.py"),
        str(root / "b/util.py"),
    }


def test_file_arguments_in_one_checkout_share_root(engine, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    first, second = write(root, "a/one.py"), write(root, "b/two.py")
    result = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(first), str(second)])
    )
    assert result.repos == 1 and result.modules == 2
    assert rows(engine, "MATCH (r:Repo) RETURN r.path") == [[str(root)]]


def test_new_exclusions_remove_generated_code_but_keep_authored_edge(engine, tmp_path):
    root = tmp_path / "repo"
    source = write(root, "module.py")
    req = CodeIngestRequest(paths=[str(root)])
    ingest_code(engine, engine.config, req)
    from grag.core.mutate import upsert_edges

    module, function = rows(
        engine, "MATCH (m:Module)-[:CONTAINS_MODULE_FUNCTION]->(f) RETURN m.id,f.id"
    )[0]
    upsert_edges(
        engine,
        engine.config,
        UpsertEdgesRequest(
            edges=[
                {
                    "type": "CONTAINS_MODULE_FUNCTION",
                    "from_label": "Module",
                    "from_key": module,
                    "to_label": "Function",
                    "to_key": function,
                    "source": "agent",
                }
            ]
        ),
    )
    write(root, ".gragignore", "module.py\n")
    result = ingest_code(engine, engine.config, req)
    assert any("Retained" in w for w in result.warnings)
    assert rows(
        engine,
        "MATCH (m:Module)-[r:CONTAINS_MODULE_FUNCTION]->(f) RETURN r._source,f._source_state",
    ) == [["agent", "obsolete"]]
    assert index_records(engine)[str(root)]["_index_error"] is None
    (root / ".gragignore").unlink()
    ingest_code(engine, engine.config, req)
    assert rows(engine, "MATCH (m:Module) RETURN m._source_state") == [["current"]]
    assert source.exists()


def test_empty_replacement_unregisters_scope(engine, tmp_path):
    root = tmp_path / "repo"
    write(root, "one.py")
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(root)]))
    result = ingest_code(
        engine,
        engine.config,
        CodeIngestRequest(paths=[], root=str(root), replace_scope=True),
    )
    assert result.nodes_pruned == 2 and not index_records(engine)
    assert rows(engine, "MATCH (m:Module) RETURN count(m)") == [[0]]


def test_symlinks_never_escape_scope(engine, tmp_path):
    root = tmp_path / "repo"
    write(root, "real.py")
    outside = write(tmp_path, "elsewhere/secret.py")
    try:
        (root / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(root)]))
    assert result.modules == 1


@pytest.mark.parametrize("sections", [True, False])
def test_document_batch_shrink_and_mode_label_change(engine, sections):
    first = IngestDocument(text="# First\n\nalpha", source="guide.md")
    second = IngestDocument(text="# Second\n\nbeta", source="guide.md")
    ingest_documents(
        engine,
        engine.config,
        IngestRequest(documents=[first, second], sections=sections),
    )
    result = ingest_documents(
        engine,
        engine.config,
        IngestRequest(documents=[first], sections=not sections, label="Passage"),
    )
    assert not result.warnings
    assert rows(engine, "MATCH (c:Chunk) RETURN count(c)") == [[0]]
    assert rows(engine, "MATCH (c:Passage) RETURN count(c)") == [[1]]
    assert rows(engine, "MATCH (d:Document) RETURN count(d)") == [
        [0 if sections else 1]
    ]


def test_directory_sync_deletions_json_shrink_and_failure(engine, tmp_path):
    root = tmp_path / "docs"
    a = write(root, "one.md", "# One\n\nfirst")
    batch = write(
        root,
        "batch.json",
        json.dumps(
            [
                {"text": "# A\n\nalpha", "source": "https://example.test/a"},
                {"text": "# B\n\nbeta", "source": "https://example.test/b"},
            ]
        ),
    )

    def sync():
        req, warnings, _ = load_request([root], sections=True)
        return ingest_documents(engine, engine.config, req), warnings

    sync()
    a.unlink()
    batch.write_text("invalid json")
    _, warnings = sync()
    assert any("incomplete" in w for w in warnings)
    assert rows(engine, "MATCH (d:Document) RETURN count(d)") == [[3]]
    batch.write_text('[{"text":"# A\\n\\nchanged", "source":"https://example.test/a"}]')
    sync()
    assert rows(engine, "MATCH (d:Document) RETURN d.path") == [
        ["https://example.test/a"]
    ]


def test_relocation_keeps_document_ids_on_reingest(engine, tmp_path):
    from grag.relocation import plan_graph

    old, new = tmp_path / "old", tmp_path / "new"
    file = write(old, "docs/guide.md", "# Guide\n\nbody")
    req, _, _ = load_request([file], sections=True)
    ingest_documents(engine, engine.config, req)
    before = rows(engine, "MATCH (d:Document) RETURN d.id")
    old.rename(new)
    # Exercise the same graph plan the offline relocation command publishes.
    plan = plan_graph(engine, old, new)
    with engine.write_transaction():
        for item in plan:
            engine.execute_write(item.query, item.params)
    req, _, _ = load_request([new / "docs/guide.md"], sections=True)
    ingest_documents(engine, engine.config, req)
    assert rows(engine, "MATCH (d:Document) RETURN d.id") == before


def test_file_reingest_keeps_previously_selected_subdirectory_root(engine, tmp_path):
    (tmp_path / ".git").mkdir()
    root = tmp_path / "src"
    source = write(root, "a/file.py")
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(root)]))
    before = rows(engine, "MATCH (m:Module) RETURN m.id")
    result = ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(source)]))
    assert result.repos == 1 and rows(engine, "MATCH (m:Module) RETURN m.id") == before


def test_nested_repository_can_be_explicitly_selected(engine, tmp_path):
    root = tmp_path / "repo"
    write(root, "main.py")
    nested = root / "nested"
    write(nested, "sub.py")
    (nested / ".git").mkdir()
    result = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(root), str(nested)])
    )
    assert result.repos == 2 and result.modules == 2


def test_root_aliases_canonicalize_without_following_internal_symlinks(
    engine, tmp_path
):
    root = tmp_path / "real"
    write(root, "one.py")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = ingest_code(
        engine, engine.config, CodeIngestRequest(paths=[str(alias)], root=str(root))
    )
    assert result.modules == 1
    assert rows(engine, "MATCH (m:Module) RETURN m._source") == [[str(root / "one.py")]]


def test_authored_nodes_with_source_provenance_are_not_generated_code(engine, tmp_path):
    from grag.core.mutate import upsert_nodes
    from grag.core.types import UpsertNode, UpsertNodesRequest

    root = tmp_path / "repo"
    file = write(root, "one.py")
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(root)]))
    upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[UpsertNode(label="Function", key="agent-note", properties={"name":"authored insight"}, source=str(file))]))
    write(root, ".gragignore", "one.py\n")
    ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(root)]))
    assert rows(engine, "MATCH (f:Function) RETURN f.id") == [["agent-note"]]
