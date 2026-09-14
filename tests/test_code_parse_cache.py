"""Parse reuse must be observationally equivalent to a current, complete parse."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.types import CodeIngestRequest
from grag.ingest import code
from grag.ingest.code_cache import ParseCache


@pytest.fixture()
def parses(monkeypatch):
    seen: list[str] = []
    for suffix, parser in list(code._PARSERS.items()):
        def measured(path, *args, _parser=parser, **kwargs):
            seen.append(path.name)
            return _parser(path, *args, **kwargs)
        monkeypatch.setitem(code._PARSERS, suffix, measured)
    return seen


def ingest(engine, root, **kwargs):
    return code.ingest_code(engine, engine.config, CodeIngestRequest(paths=[str(root)], root=str(root), **kwargs))


def calls(engine):
    return engine.execute('MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.name,b.name ORDER BY a.name,b.name').rows


def declarations(engine):
    return engine.execute('MATCH (f:Function) RETURN f.id,f.name,f.line_start,f.line_end,f.signature ORDER BY f.id').rows


def imports(engine):
    return engine.execute('MATCH (a:Module)-[:IMPORTS]->(b:Module) RETURN a.path,b.path ORDER BY a.path,b.path').rows


def write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')


@pytest.mark.parametrize(('suffix', 'source'), [
    ('.py', 'def hello():\n return 1\n'),
    ('.ts', 'export function hello() { return 1; }'),
    ('.svelte', '<script lang="ts">export function hello() { return 1; }</script>'),
    ('.go', 'package demo\nfunc Hello() int { return 1 }'),
    ('.java', 'class Demo { int hello() { return 1; } }'),
    ('.cs', 'class Demo { int Hello() { return 1; } }'),
    ('.tf', 'module "child" { source = "./child" }'),
])
def test_all_parser_routes_reuse_and_keep_citations(engine, tmp_path, parses, suffix, source):
    if suffix != '.py':
        pytest.importorskip('tree_sitter_language_pack')
    root = tmp_path / 'repo'
    write(root, {'main'+suffix: source})
    first = ingest(engine, root)
    before = declarations(engine)
    second = ingest(engine, root)
    assert parses == ['main'+suffix]
    assert first.files_reused == 0
    assert second.files_parsed == second.files_reused == second.files_unchanged == 1
    assert declarations(engine) == before


def test_content_hash_detects_same_size_same_mtime_edits(engine, tmp_path, parses):
    root = tmp_path / 'repo'
    write(root, {'main.py': 'def old():\n return 1\n'})
    ingest(engine, root)
    path = root / 'main.py'
    original = path.stat()
    path.write_text('def new():\n return 1\n')
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert ingest(engine, root).files_reused == 0
    assert parses == ['main.py', 'main.py']
    assert engine.execute('MATCH (f:Function) RETURN f.name').rows == [['new']]


@pytest.mark.parametrize('language', ['python', 'typescript'])
def test_reexports_update_unchanged_callers_without_reparsing(engine, tmp_path, parses, language):
    root = tmp_path / 'repo'
    if language == 'python':
        files = {'a.py': 'def left():\n return 1\n', 'b.py': 'def right():\n return 2\n',
                 'bridge.py': 'from a import left as chosen\n',
                 'main.py': 'from bridge import chosen\ndef run():\n return chosen()\n'}
        changed, replacement = 'bridge.py', 'from b import right as chosen\n'
    else:
        pytest.importorskip('tree_sitter_language_pack')
        files = {'a.ts': 'export function left() {}', 'b.ts': 'export function right() {}',
                 'bridge.ts': 'export {left as chosen} from "./a";',
                 'main.ts': 'import {chosen} from "./bridge"; export function run() { chosen(); }'}
        changed, replacement = 'bridge.ts', 'export {right as chosen} from "./b";'
    write(root, files)
    ingest(engine, root)
    assert calls(engine) == [['run', 'left']]
    parses.clear()
    write(root, {changed: replacement})
    result = ingest(engine, root)
    assert parses == [changed]
    assert result.files_reused == 3
    assert calls(engine) == [['run', 'right']]
    expected = declarations(engine), calls(engine), imports(engine)
    assert ingest(engine, root, incremental=False).files_reused == 0
    assert (declarations(engine), calls(engine), imports(engine)) == expected


def test_new_ambiguous_generic_import_removes_previous_edge(engine, tmp_path, parses):
    pytest.importorskip('tree_sitter_language_pack')
    root = tmp_path / 'repo'
    write(root, {'models.cs': 'namespace App.Models; class A {}',
                 'main.cs': 'using App.Models; namespace App.Services; class B {}'})
    ingest(engine, root)
    assert imports(engine) == [['main.cs', 'models.cs']]
    parses.clear()
    write(root, {'duplicate.cs': 'namespace App.Models; class C {}'})
    response = ingest(engine, root)
    assert parses == ['duplicate.cs']
    assert response.files_reused == 2
    assert imports(engine) == []
    (root / 'duplicate.cs').unlink()
    parses.clear()
    ingest(engine, root)
    assert parses == []
    assert imports(engine) == [['main.cs', 'models.cs']]


def test_go_manifest_recomputed_on_cache_hit(engine, tmp_path, parses):
    pytest.importorskip('tree_sitter_language_pack')
    root = tmp_path / 'repo'
    write(root, {'go.mod': 'module example.test/one\n',
                 'lib/helper.go': 'package lib\nfunc Helper() {}',
                 'main.go': 'package main\nimport "example.test/one/lib"\nfunc Run() { lib.Helper() }'})
    ingest(engine, root)
    assert calls(engine) == [['Run', 'Helper']]
    parses.clear()
    write(root, {'go.mod': 'module example.test/two\n'})
    changed = ingest(engine, root)
    assert changed.files_reused == 2 and parses == []
    assert calls(engine) == [] and imports(engine) == []
    coverage = engine.execute("MATCH (m:Module {path:'lib/helper.go'}) RETURN m.code_coverage").rows[0][0]
    assert json.loads(coverage)['import_path'] == 'example.test/two/lib'
    write(root, {'go.mod': 'module example.test/one\n'})
    assert ingest(engine, root).files_reused == 2
    assert calls(engine) == [['Run', 'Helper']]


def test_cache_is_not_a_publication_receipt(engine, tmp_path, parses, monkeypatch):
    root = tmp_path / 'repo'
    write(root, {'main.py': 'def hello():\n return 1\n'})
    original = code._record_ingest_hashes
    def fail(*args):
        original(*args)
        raise RuntimeError('publication fault')
    with monkeypatch.context() as fault:
        fault.setattr(code, '_record_ingest_hashes', fail)
        with pytest.raises(RuntimeError, match='publication fault'):
            ingest(engine, root)
    assert declarations(engine) == []
    result = ingest(engine, root)
    assert result.files_reused == 1 and result.files_unchanged == 0
    assert parses == ['main.py'] and len(declarations(engine)) == 1


def test_resolver_failure_cannot_modify_cached_bindings(engine, tmp_path, parses, monkeypatch):
    from grag.ingest import code_python
    root = tmp_path / 'repo'
    write(root, {'main.py': 'def helper():\n return 1\ndef run():\n return helper()\n'})
    def fail(modules, **kwargs):
        modules[0].python.scope.bindings.clear()
        modules[0].module.properties['path'] = 'corrupted'
        raise RuntimeError('resolution fault')
    with monkeypatch.context() as fault:
        fault.setattr(code_python, 'resolve', fail)
        with pytest.raises(RuntimeError, match='resolution fault'):
            ingest(engine, root)
    assert ingest(engine, root).files_reused == 1
    assert parses == ['main.py']
    assert calls(engine) == [['run', 'helper']]
    assert engine.execute('MATCH (m:Module) RETURN m.path').rows == [['main.py']]


def test_failures_and_scope_exclusions_do_not_reuse_stale_parse(engine, tmp_path, parses):
    root = tmp_path / 'repo'
    original = 'def helper():\n return 1\n' + '#' * 1200
    write(root, {'main.py': original})
    ingest(engine, root)
    assert ingest(engine, root, max_file_kb=1).files_parsed == 0
    assert declarations(engine) == []  # smaller size policy removes generated scope
    write(root, {'main.py': 'def broken('})
    failed = ingest(engine, root)
    assert failed.files_parsed == failed.files_reused == 0
    assert any('could not parse' in w for w in failed.warnings)
    write(root, {'main.py': original})
    assert ingest(engine, root).files_reused == 0
    write(root, {'.gragignore': 'main.py\n'})
    assert ingest(engine, root).modules == 0
    assert engine.code_parse_cache.size_bytes == 0
    (root / '.gragignore').unlink()
    assert ingest(engine, root).files_reused == 0


def test_option_revision_parser_and_forced_scan_invalidate_cache(engine, tmp_path, parses, monkeypatch):
    root = tmp_path / 'repo'
    write(root, {'main.py': 'def helper():\n return 1\ndef run():\n return helper()\n'})
    ingest(engine, root)
    assert ingest(engine, root, calls=False).files_reused == 0
    assert calls(engine) == []
    assert ingest(engine, root).files_reused == 0
    assert calls(engine) == [['run', 'helper']]
    monkeypatch.setattr(code, 'PARSER_REVISION', 'test-new-parser')
    assert ingest(engine, root).files_reused == 0
    parser = code._PARSERS['.py']
    monkeypatch.setitem(code._PARSERS, '.py', lambda *a, **k: parser(*a, **k))  # noqa: PLW0108 — distinct parser identity
    assert ingest(engine, root).files_reused == 0
    assert ingest(engine, root, incremental=False).files_reused == 0
    assert ingest(engine, root).files_reused == 1
    assert len(parses) == 6


@pytest.mark.parametrize('limits', [{'max_entries': 1}, {'max_bytes': 1}])
def test_eviction_changes_only_work_not_results(engine, tmp_path, parses, limits):
    engine.code_parse_cache = ParseCache(**limits)
    root = tmp_path / 'repo'
    write(root, {'a.py': 'def helper():\n return 1\n',
                 'main.py': 'from a import helper\ndef run():\n return helper()\n'})
    ingest(engine, root)
    expected = declarations(engine), calls(engine), imports(engine)
    result = ingest(engine, root)
    assert result.files_unchanged == 2
    assert (declarations(engine), calls(engine), imports(engine)) == expected
    assert engine.code_parse_cache.size_bytes <= engine.code_parse_cache.max_bytes


def test_reopen_cold_parse_preserves_graph_and_cache_is_released(tmp_path, parses):
    root = tmp_path / 'repo'
    write(root, {'main.py': 'def hello():\n return 1\n'})
    cfg = GragConfig(db_path=tmp_path / 'graph.lbdb', buffer_pool_size=128*1024**2)
    with Engine(cfg) as engine:
        ingest(engine, root)
        assert ingest(engine, root).files_reused == 1
        before = declarations(engine)
        cache = engine.code_parse_cache
        assert cache.size_bytes > 0
    assert cache.size_bytes == 0
    with Engine(cfg) as engine:
        result = ingest(engine, root)
        assert result.files_reused == 0 and result.files_unchanged == 1
        assert declarations(engine) == before
    assert parses == ['main.py', 'main.py']


def test_cache_admission_recursion_failure_falls_back(engine, tmp_path, monkeypatch):
    from grag.ingest import code_cache
    root = tmp_path / 'repo'
    write(root, {'main.py': 'def hello():\n return 1\n'})
    def too_deep(*a, **k):
        raise RecursionError('summary too deep to copy')
    monkeypatch.setattr(code_cache, 'deepcopy', too_deep)
    assert ingest(engine, root).files_reused == 0
    assert ingest(engine, root).files_unchanged == 1
    assert len(declarations(engine)) == 1


def test_dependency_only_update_preserves_authored_declaration(engine, tmp_path):
    from grag.core.mutate import upsert_nodes
    from grag.core.types import UpsertNodesRequest
    root = tmp_path / 'repo'
    write(root, {'core.py': 'def helper():\n return 1\n',
                 'main.py': 'from core import later\ndef run():\n return later()\n'})
    ingest(engine, root)
    key = f'{code._repo_id(root)}:main.py#run'
    upsert_nodes(engine, engine.config, UpsertNodesRequest(nodes=[{
        'label': 'Function', 'key': key, 'properties': {'docstring': 'Authored note about this caller'},
        'source': str(root / 'main.py'),
    }]))
    before = engine.execute('MATCH (f:Function {id:$key}) RETURN f', {'key': key}).rows
    write(root, {'core.py': 'def helper():\n return 1\ndef later():\n return 2\n'})
    result = ingest(engine, root)
    assert result.files_reused == 1
    assert calls(engine) == [['run', 'later']]
    assert engine.execute('MATCH (f:Function {id:$key}) RETURN f', {'key': key}).rows == before


def test_legacy_single_digest_is_rewritten_once(engine, tmp_path):
    root = tmp_path / 'repo'
    write(root, {'main.py': 'def hello():\n return 1\n'})
    ingest(engine, root)
    engine.execute_write("MATCH (m:Module) SET m._ingest_hash=$hash", {'hash': 'a' * 64})
    assert ingest(engine, root).files_unchanged == 0
    assert ingest(engine, root).files_unchanged == 1


def test_lru_retains_recent_entry_and_never_shares_returned_state():
    from grag.ingest.code import _parse_python
    cache = ParseCache(max_entries=2)
    paths = [Path(f'/repo/{name}.py') for name in ('a', 'b', 'c')]
    modules = [_parse_python(p, 'def hello():\n return 1\n', repo='repo', rel_path=p.name, calls=True) for p in paths]
    for path, module in zip(paths[:2], modules[:2], strict=True):
        cache.put(('repo', str(path)), 'one', _parse_python, module)
    copy = cache.get(('repo', str(paths[0])), 'one', _parse_python)
    assert copy is not None
    copy.functions.clear()
    cache.put(('repo', str(paths[2])), 'one', _parse_python, modules[2])
    assert cache.get(('repo', str(paths[1])), 'one', _parse_python) is None
    fresh = cache.get(('repo', str(paths[0])), 'one', _parse_python)
    assert fresh is not None and len(fresh.functions) == 1
    assert cache.size_bytes <= cache.max_bytes
