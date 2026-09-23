"""Opt-in, versioned schema presets — currently the memory preset.

grag needs no preset: agents can define their own labels. The memory preset is a
shared starting point for decisions, insights, tasks and open questions. Adoption
is additive and idempotent. It creates missing tables and adds missing properties.
It never drops, renames or retypes anything: a different key, an incompatible
property type, or a clashing or near-duplicate table is reported and left as it
is. The adopted version is recorded in ``_grag_meta``, so a later grag knows which
migrations a database still needs.
"""

from __future__ import annotations

from collections.abc import Callable

from grag.core.engine import Engine
from grag.core.errors import SchemaError
from grag.core.types import META_TABLE, NodeTableSpec, PresetReport, PropertySpec

_META_KV = "_grag_meta"  # the engine's key/value table (version stamps)

_MEMORY_LABELS = ("Decision", "Insight", "Task", "Question")
_MEMORY_PROPS = ("title", "body", "status", "scope")

# Current table specs per preset. Every adoption ensures these, so additive
# changes in a new version need no migration step.
PRESETS: dict[str, tuple[int, list[NodeTableSpec]]] = {
    "memory": (1, [
        NodeTableSpec(name=label, properties=[PropertySpec(name=p) for p in _MEMORY_PROPS])
        for label in _MEMORY_LABELS
    ]),
}

# Non-additive changes (rename, split, backfill): preset -> {target version: step}.
# A step runs when adoption moves a database past that version. Steps must be
# idempotent: export/import does not carry _grag_meta, so a restored database
# re-adopts from no recorded version.
MIGRATIONS: dict[str, dict[int, Callable[[Engine, PresetReport], None]]] = {"memory": {}}


def adopt_preset(engine: Engine, name: str, *, allow_similar: bool = False) -> PresetReport:
    """Bring the database to the preset's current version; report what changed."""
    version, specs = PRESETS[name]
    key = f"preset.{name}.version"
    if engine.in_write_transaction:
        raise SchemaError("Preset adoption cannot join another write transaction.",
                          hint="Adopt the preset as its own define_schema call.")
    with engine.atomic_writes():
        previous = _recorded_version(engine, key)
        if previous is not None and previous > version:
            raise SchemaError(
                f"This database records {name} preset version {previous}; this grag supports up to {version}.",
                hint="Upgrade gragdb (pip install -U gragdb) before adopting. Nothing was changed.",
            )
        report = PresetReport(name=name, version=version, previous_version=previous)
        for target in sorted(MIGRATIONS[name]):
            if (previous or 0) < target <= version:
                MIGRATIONS[name][target](engine, report)
        altered = _ensure_tables(engine, specs, report, allow_similar=allow_similar)
        _record_version(engine, key, version)
    # DROP/CREATE_FTS_INDEX run only in auto-transaction mode, after the commit.
    from grag.retrieval.search import refresh_fts_index

    for table in altered:
        refresh_fts_index(engine, table)
    return report


def _recorded_version(engine: Engine, key: str) -> int | None:
    from grag.core.mutate import _table_index

    if _META_KV not in _table_index(engine):
        return None
    rows = engine.execute_write(f"MATCH (m:{_META_KV} {{key: $k}}) RETURN m.value", {"k": key}).rows
    if not rows or rows[0][0] is None:
        return None
    try:
        return int(rows[0][0])
    except ValueError:
        raise SchemaError(f"Unreadable preset version {rows[0][0]!r} under '{key}'.",
                          hint="Inspect _grag_meta; nothing was changed.") from None


def _record_version(engine: Engine, key: str, version: int) -> None:
    from grag.core.mutate import _table_index

    if _META_KV not in _table_index(engine):
        engine.execute_write(f"CREATE NODE TABLE {_META_KV}(key STRING PRIMARY KEY, value STRING)")
    engine.execute_write(f"MERGE (m:{_META_KV} {{key: $k}}) ON CREATE SET m.value = $v ON MATCH SET m.value = $v",
                         {"k": key, "v": str(version)})


def _ensure_tables(engine: Engine, specs: list[NodeTableSpec], report: PresetReport, *,
                   allow_similar: bool) -> list[str]:
    """Create missing tables and add missing properties. Returns existing tables
    that gained STRING properties (their FTS index needs the new columns)."""
    from grag.core.mutate import (
        _META_DDL,
        _merge_meta,
        _node_ddl,
        _pk_of,
        _table_columns,
        _table_index,
        similar_table,
    )

    tables = _table_index(engine)
    if META_TABLE not in tables:
        engine.execute_write(_META_DDL)
        tables[META_TABLE] = "NODE"
    altered: list[str] = []
    for spec in specs:
        kind = tables.get(spec.name)
        if kind == "REL":
            report.conflicts.append(f"{spec.name} exists as a relationship table; left unchanged.")
            continue
        if kind is None:
            twin = None if allow_similar else similar_table(
                spec.name, [n for n, k in tables.items() if k == "NODE"])
            if twin is not None:
                report.conflicts.append(
                    f"{spec.name} not created: similar table {twin} exists. Reuse {twin}, "
                    "or adopt with allow_similar=true to create both.")
                continue
            engine.execute_write(_node_ddl(spec))
            tables[spec.name] = "NODE"
            _merge_meta(engine, name=spec.name, kind="node", pk=spec.primary_key,
                        searchable=spec.searchable, from_label="", to_label="")
            report.created.append(spec.name)
            continue
        columns = _table_columns(engine, spec.name)
        pk = _pk_of(engine, spec.name)
        if pk != spec.primary_key or columns.get(pk or "") != "STRING":
            report.conflicts.append(
                f"{spec.name} key is {pk}:{columns.get(pk or '')}; the preset expects "
                f"{spec.primary_key}:STRING. Left unchanged.")
        for prop in spec.properties:
            declared = columns.get(prop.name)
            if declared is None:
                engine.execute_write(
                    f"ALTER TABLE {spec.name} ADD {prop.name} {prop.type} DEFAULT CAST(NULL AS {prop.type})")
                report.added.append(f"{spec.name}.{prop.name}")
                if prop.type == "STRING" and spec.name not in altered:
                    altered.append(spec.name)
            elif declared.upper() != prop.type:
                report.conflicts.append(
                    f"{spec.name}.{prop.name} is {declared}; the preset expects {prop.type}. Left unchanged.")
    return altered
