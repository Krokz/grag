"""Schema definition and idempotent upserts — the grag write path.

Every table created here is recorded in META_TABLE (see grag.core.types) so
introspection and canonical node ids never parse DDL. Node tables carry
`_source` / `_created_at` provenance columns, rel tables carry `_source`.
LadybugDB does not parse map-assignment SET (`SET n += $props`), so all
writes emit explicit per-property assignments; MERGE keeps upserts
idempotent.
"""

from __future__ import annotations

from typing import Any

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import NotFoundError, SchemaError
from grag.core.ident import validate_identifier
from grag.core.types import (
    META_TABLE,
    PROVENANCE_CREATED_AT,
    PROVENANCE_SOURCE,
    RESERVED_PREFIX,
    VECTOR_PROPS,
    DefineSchemaRequest,
    MutationSummary,
    NodeTableDoc,
    NodeTableSpec,
    PropertyDoc,
    RelTableDoc,
    RelTableSpec,
    SchemaDocument,
    UpsertEdgesRequest,
    UpsertNodesRequest,
)

_META_DDL = (
    f"CREATE NODE TABLE {META_TABLE}("
    "name STRING PRIMARY KEY, kind STRING, pk STRING, searchable BOOL, "
    "from_label STRING, to_label STRING)"
)

# NOTE: writes to FTS/HNSW-indexed tables require those extensions to be LOADed
# in-process. Engine.__init__ preloads both (see _preload_extensions), so the
# write path needs no per-call handling here.


# --- introspection helpers -------------------------------------------------------


def _table_index(engine: Engine) -> dict[str, str]:
    """table name -> 'NODE' | 'REL'."""
    res = engine.execute("CALL SHOW_TABLES() RETURN *")
    return {row[1]: row[2] for row in res.rows}


def _table_columns(engine: Engine, table: str) -> dict[str, str]:
    res = engine.execute(f"CALL TABLE_INFO('{table}') RETURN *")
    return {row[1]: row[2] for row in res.rows}


def _pk_of(engine: Engine, table: str) -> str | None:
    res = engine.execute(f"CALL TABLE_INFO('{table}') RETURN *")
    for row in res.rows:
        if row[4]:
            return row[1]
    return None


def _connection_of(engine: Engine, rel: str) -> tuple[str, str] | None:
    res = engine.execute(f"CALL SHOW_CONNECTION('{rel}') RETURN *")
    if res.rows:
        return str(res.rows[0][0]), str(res.rows[0][1])
    return None


def _meta_rows(
    engine: Engine, tables: dict[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    tables = tables if tables is not None else _table_index(engine)
    if META_TABLE not in tables:
        return {}
    res = engine.execute(
        f"MATCH (m:{META_TABLE}) "
        "RETURN m.name, m.kind, m.pk, m.searchable, m.from_label, m.to_label"
    )
    return {
        row[0]: {
            "kind": row[1],
            "pk": row[2],
            "searchable": row[3],
            "from_label": row[4],
            "to_label": row[5],
        }
        for row in res.rows
    }


def _node_pks(engine: Engine) -> dict[str, str | None]:
    """Node label -> primary key property (META_TABLE first, TABLE_INFO fallback)."""
    tables = _table_index(engine)
    meta = _meta_rows(engine, tables)
    out: dict[str, str | None] = {}
    for name, kind in tables.items():
        if kind != "NODE" or name == META_TABLE:
            continue
        m = meta.get(name)
        pk = m["pk"] if m and m["kind"] == "node" else None
        out[name] = pk or _pk_of(engine, name)
    return out


def _rel_endpoints(engine: Engine) -> dict[str, tuple[str, str]]:
    """Rel type -> (from_label, to_label) (META_TABLE first, SHOW_CONNECTION fallback)."""
    tables = _table_index(engine)
    meta = _meta_rows(engine, tables)
    out: dict[str, tuple[str, str]] = {}
    for name, kind in tables.items():
        if kind != "REL":
            continue
        m = meta.get(name)
        if m and m["kind"] == "rel" and m["from_label"] and m["to_label"]:
            out[name] = (m["from_label"], m["to_label"])
        else:
            conn = _connection_of(engine, name)
            if conn:
                out[name] = conn
    return out


# --- validation ------------------------------------------------------------------


def _validate_ident(name: str, what: str) -> None:
    validate_identifier(name, what)


def _reject_reserved(prop: str, table: str) -> None:
    if prop.startswith(RESERVED_PREFIX) or prop in VECTOR_PROPS:
        raise SchemaError(
            f"Property '{prop}' on '{table}' is reserved for grag internals.",
            hint=f"Properties starting with '{RESERVED_PREFIX}' and vector columns "
            f"{sorted(VECTOR_PROPS)} are managed by grag; rename the property.",
        )


def _validate_request(req: DefineSchemaRequest) -> None:
    seen_names: list[str] = []
    for spec in req.node_tables:
        _validate_ident(spec.name, "node table name")
        _validate_ident(spec.primary_key, f"primary key of '{spec.name}'")
        _reject_reserved(spec.primary_key, spec.name)
        _validate_props(spec)
        seen_names.append(spec.name)
    for rspec in req.rel_tables:
        _validate_ident(rspec.name, "rel table name")
        _validate_ident(rspec.from_label, f"from_label of '{rspec.name}'")
        _validate_ident(rspec.to_label, f"to_label of '{rspec.name}'")
        _validate_props(rspec)
        seen_names.append(rspec.name)
    dupes = sorted({n for n in seen_names if seen_names.count(n) > 1})
    if dupes:
        raise SchemaError(
            f"Duplicate table name(s) in request: {', '.join(dupes)}.",
            hint="Table names must be unique across node_tables and rel_tables.",
        )
    if META_TABLE in seen_names:
        raise SchemaError(
            f"'{META_TABLE}' is the grag table registry.",
            hint="Choose a different table name.",
        )


def _validate_props(spec: NodeTableSpec | RelTableSpec) -> None:
    seen: set[str] = set()
    for p in spec.properties:
        _validate_ident(p.name, f"property of '{spec.name}'")
        _reject_reserved(p.name, spec.name)
        if p.name in seen:
            raise SchemaError(
                f"Duplicate property '{p.name}' on '{spec.name}'.",
                hint="Property names must be unique within a table spec.",
            )
        seen.add(p.name)


# --- DDL ------------------------------------------------------------------------


def _node_ddl(spec: NodeTableSpec) -> str:
    decl = {p.name: p.type for p in spec.properties}
    cols = [(spec.primary_key, decl.pop(spec.primary_key, "STRING"))]
    cols.extend(decl.items())
    cols.append((PROVENANCE_SOURCE, "STRING"))
    cols.append((PROVENANCE_CREATED_AT, "TIMESTAMP"))
    body = ", ".join(f"{name} {ctype}" for name, ctype in cols)
    return f"CREATE NODE TABLE {spec.name}({body}, PRIMARY KEY({spec.primary_key}))"


def _rel_ddl(spec: RelTableSpec) -> str:
    parts = [f"FROM {spec.from_label} TO {spec.to_label}"]
    parts.extend(f"{p.name} {p.type}" for p in spec.properties)
    parts.append(f"{PROVENANCE_SOURCE} STRING")
    return f"CREATE REL TABLE {spec.name}({', '.join(parts)})"


def _merge_meta(
    engine: Engine,
    *,
    name: str,
    kind: str,
    pk: str,
    searchable: bool,
    from_label: str,
    to_label: str,
) -> None:
    assignments = (
        "m.kind = $kind, m.pk = $pk, m.searchable = $searchable, "
        "m.from_label = $fl, m.to_label = $tl"
    )
    engine.execute_write(
        f"MERGE (m:{META_TABLE} {{name: $name}}) "
        f"ON CREATE SET {assignments} ON MATCH SET {assignments}",
        {
            "name": name,
            "kind": kind,
            "pk": pk,
            "searchable": searchable,
            "fl": from_label,
            "tl": to_label,
        },
    )


# --- public: define_schema --------------------------------------------------------


def define_schema(
    engine: Engine, config: GragConfig, req: DefineSchemaRequest
) -> SchemaDocument:
    _validate_request(req)

    tables = _table_index(engine)
    if META_TABLE not in tables:
        engine.execute_write(_META_DDL)
        tables[META_TABLE] = "NODE"

    for spec in req.node_tables:
        kind = tables.get(spec.name)
        if kind is not None:
            if not req.if_not_exists:
                raise _exists_error(spec.name)
            if kind != "NODE":
                raise SchemaError(
                    f"'{spec.name}' already exists as a rel table.",
                    hint="Choose a different node table name.",
                )
            continue
        engine.execute_write(_node_ddl(spec))
        tables[spec.name] = "NODE"

    for rspec in req.rel_tables:
        kind = tables.get(rspec.name)
        if kind is not None:
            if not req.if_not_exists:
                raise _exists_error(rspec.name)
            if kind != "REL":
                raise SchemaError(
                    f"'{rspec.name}' already exists as a node table.",
                    hint="Choose a different rel table name.",
                )
            continue
        for endpoint in (rspec.from_label, rspec.to_label):
            if tables.get(endpoint) != "NODE":
                node_tables = sorted(
                    n for n, k in tables.items() if k == "NODE" and n != META_TABLE
                )
                raise SchemaError(
                    f"Rel '{rspec.name}' endpoint '{endpoint}' is not an existing node table.",
                    hint=f"Define '{endpoint}' under node_tables (in this or an earlier "
                    f"define_schema call). Existing node tables: {node_tables}.",
                )
        engine.execute_write(_rel_ddl(rspec))
        tables[rspec.name] = "REL"

    for spec in req.node_tables:
        pk = _pk_of(engine, spec.name) or spec.primary_key
        _merge_meta(
            engine,
            name=spec.name,
            kind="node",
            pk=pk,
            searchable=spec.searchable,
            from_label="",
            to_label="",
        )
    for rspec in req.rel_tables:
        conn = _connection_of(engine, rspec.name) or (rspec.from_label, rspec.to_label)
        _merge_meta(
            engine,
            name=rspec.name,
            kind="rel",
            pk="",
            searchable=False,
            from_label=conn[0],
            to_label=conn[1],
        )

    try:
        from grag.core.schema import build_schema_document
    except ImportError:
        return _fallback_schema_document(engine)
    return build_schema_document(engine, config)


def _exists_error(name: str) -> SchemaError:
    return SchemaError(
        f"Table '{name}' already exists.",
        hint="define_schema is idempotent by default (if_not_exists=True); only set "
        "if_not_exists=False when redefinition should fail loudly.",
    )


# --- value coercion -----------------------------------------------------------------


def _coerce_value(declared: str, value: Any) -> tuple[bool, Any]:
    """Minimal type check against the declared column type.

    Returns (ok, coerced). Only str -> INT64/DOUBLE is coerced; everything
    else must already match. DATE/TIMESTAMP and unknown types pass through.
    """
    ctype = declared.upper()
    if ctype == "STRING":
        return (True, value) if isinstance(value, str) else (False, None)
    if ctype == "INT64":
        if isinstance(value, bool):
            return (False, None)
        if isinstance(value, int):
            return (True, value)
        if isinstance(value, str):
            try:
                return (True, int(value.strip()))
            except ValueError:
                return (False, None)
        return (False, None)
    if ctype == "DOUBLE":
        if isinstance(value, bool):
            return (False, None)
        if isinstance(value, (int, float)):
            return (True, float(value))
        if isinstance(value, str):
            try:
                return (True, float(value.strip()))
            except ValueError:
                return (False, None)
        return (False, None)
    if ctype == "BOOL":
        return (True, value) if isinstance(value, bool) else (False, None)
    return (True, value)


def _sanitize_props(
    *,
    props: dict[str, Any],
    columns: dict[str, str],
    alias: str,
    owner: str,
    warnings: list[str],
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """Filter caller props to declared, non-reserved, type-compatible columns.

    Returns (SET assignments, params, accepted values); rejected props become
    warnings.
    """
    sets: list[str] = []
    params: dict[str, Any] = {}
    accepted: dict[str, Any] = {}
    for i, (name, value) in enumerate(props.items()):
        if name.startswith(RESERVED_PREFIX) or name in VECTOR_PROPS:
            warnings.append(
                f"{owner}: property '{name}' is grag-internal (reserved prefix "
                f"'{RESERVED_PREFIX}' or a vector column); skipped."
            )
            continue
        declared = columns.get(name)
        if declared is None:
            visible = sorted(
                c
                for c in columns
                if not c.startswith(RESERVED_PREFIX) and c not in VECTOR_PROPS
            )
            warnings.append(
                f"{owner}: no column '{name}' declared; skipped. "
                f"Declared properties: {visible}."
            )
            continue
        ok, coerced = _coerce_value(declared, value)
        if not ok:
            warnings.append(
                f"{owner}: column '{name}' expects {declared}, got "
                f"{type(value).__name__} {value!r}; skipped."
            )
            continue
        pname = f"p{i}"
        params[pname] = coerced
        accepted[name] = coerced
        sets.append(f"{alias}.{name} = ${pname}")
    return sets, params, accepted


def _searchable_text_changed(
    engine: Engine,
    *,
    label: str,
    pk: str,
    key: Any,
    columns: dict[str, str],
    accepted: dict[str, Any],
) -> bool:
    """Whether an existing node's incoming embedding input has changed."""

    if not VECTOR_PROPS.intersection(columns):
        return False
    text_props = [
        name
        for name in accepted
        if columns[name].upper() == "STRING"
        and not name.startswith(RESERVED_PREFIX)
        and name not in VECTOR_PROPS
    ]
    if not text_props:
        return False
    projection = ", ".join(f"n.{name}" for name in text_props)
    rows = engine.execute(
        f"MATCH (n:{label} {{{pk}: $key}}) RETURN {projection}", {"key": key}
    ).rows
    if not rows:
        return False
    return any(
        previous != accepted[name]
        for name, previous in zip(text_props, rows[0], strict=True)
    )


# --- public: upsert_nodes ------------------------------------------------------------


def upsert_nodes(
    engine: Engine, config: GragConfig, req: UpsertNodesRequest
) -> MutationSummary:
    warnings: list[str] = []
    pks = _node_pks(engine)
    unknown = sorted({n.label for n in req.nodes} - set(pks))
    if unknown:
        raise SchemaError(
            f"Unknown node label(s): {', '.join(unknown)}.",
            hint="Call define_schema with a NodeTableSpec for each new label first. "
            f"Existing node tables: {sorted(pks)}.",
        )

    col_cache: dict[str, dict[str, str]] = {}
    for node in req.nodes:
        pk = pks[node.label]
        if not pk:
            raise SchemaError(
                f"Node table '{node.label}' has no primary key.",
                hint="grag node tables need a PRIMARY KEY; redefine via define_schema.",
            )
        if node.label not in col_cache:
            col_cache[node.label] = _table_columns(engine, node.label)
        columns = col_cache[node.label]

        create_sets, params, accepted = _sanitize_props(
            props=node.properties,
            columns=columns,
            alias="n",
            owner=f"Node {node.label}:{node.key}",
            warnings=warnings,
        )
        match_sets = list(create_sets)
        if _searchable_text_changed(
            engine,
            label=node.label,
            pk=pk,
            key=node.key,
            columns=columns,
            accepted=accepted,
        ):
            match_sets.extend(
                f"n.{prop} = NULL" for prop in sorted(VECTOR_PROPS) if prop in columns
            )
        if PROVENANCE_CREATED_AT in columns:
            create_sets.append(f"n.{PROVENANCE_CREATED_AT} = current_timestamp()")
        if node.source is not None and PROVENANCE_SOURCE in columns:
            params["src"] = node.source
            create_sets.append(f"n.{PROVENANCE_SOURCE} = $src")
            match_sets.append(f"n.{PROVENANCE_SOURCE} = $src")

        cypher = f"MERGE (n:{node.label} {{{pk}: $key}})"
        if create_sets:
            cypher += " ON CREATE SET " + ", ".join(create_sets)
        if match_sets:
            cypher += " ON MATCH SET " + ", ".join(match_sets)
        params["key"] = node.key
        engine.execute_write(cypher, params)

    return MutationSummary(nodes=len(req.nodes), warnings=warnings)


# --- public: upsert_edges ------------------------------------------------------------


def upsert_edges(
    engine: Engine, config: GragConfig, req: UpsertEdgesRequest
) -> MutationSummary:
    warnings: list[str] = []
    rels = _rel_endpoints(engine)
    pks = _node_pks(engine)

    for edge in req.edges:
        endpoints = rels.get(edge.type)
        if endpoints is None:
            raise SchemaError(
                f"Unknown rel type '{edge.type}'.",
                hint=f"Call define_schema with a RelTableSpec for '{edge.type}' first. "
                f"Existing rel types: {sorted(rels)}.",
            )
        if (edge.from_label, edge.to_label) != endpoints:
            raise SchemaError(
                f"Rel '{edge.type}' connects ({endpoints[0]})-[:{edge.type}]->({endpoints[1]}), "
                f"not ({edge.from_label})-[:{edge.type}]->({edge.to_label}).",
                hint=f"Use from_label='{endpoints[0]}' and to_label='{endpoints[1]}', "
                "or define a new rel table via define_schema.",
            )

    missing: list[str] = []
    checked: list[tuple[str, Any]] = []
    for edge in req.edges:
        for label, key in (
            (edge.from_label, edge.from_key),
            (edge.to_label, edge.to_key),
        ):
            if (label, key) in checked:
                continue
            checked.append((label, key))
            pk = pks.get(label)
            if pk is None:
                missing.append(f"{label}:{key}")
                continue
            res = engine.execute(
                f"MATCH (x:{label} {{{pk}: $k}}) RETURN count(*)", {"k": key}
            )
            if res.rows[0][0] == 0:
                missing.append(f"{label}:{key}")
    if missing:
        raise NotFoundError(
            f"Edge endpoint node(s) do not exist: {', '.join(sorted(set(missing)))}.",
            hint="Upsert the endpoint nodes first (upsert_nodes), then retry upsert_edges.",
        )

    col_cache: dict[str, dict[str, str]] = {}
    for edge in req.edges:
        if edge.type not in col_cache:
            col_cache[edge.type] = _table_columns(engine, edge.type)
        columns = col_cache[edge.type]

        create_sets, params, _accepted = _sanitize_props(
            props=edge.properties,
            columns=columns,
            alias="r",
            owner=f"Edge {edge.type}({edge.from_label}:{edge.from_key}"
            f"->{edge.to_label}:{edge.to_key})",
            warnings=warnings,
        )
        match_sets = list(create_sets)
        if edge.source is not None and PROVENANCE_SOURCE in columns:
            params["src"] = edge.source
            create_sets.append(f"r.{PROVENANCE_SOURCE} = $src")
            match_sets.append(f"r.{PROVENANCE_SOURCE} = $src")

        cypher = (
            f"MATCH (a:{edge.from_label} {{{pks[edge.from_label]}: $fk}}), "
            f"(b:{edge.to_label} {{{pks[edge.to_label]}: $tk}}) "
            f"MERGE (a)-[r:{edge.type}]->(b)"
        )
        if create_sets:
            cypher += " ON CREATE SET " + ", ".join(create_sets)
        if match_sets:
            cypher += " ON MATCH SET " + ", ".join(match_sets)
        params["fk"] = edge.from_key
        params["tk"] = edge.to_key
        # execute_write auto-evicts the cached plan for MERGE statements;
        # without that, ladybug collapses every edge onto the first edge's
        # endpoints. See Engine.execute_write.
        engine.execute_write(cypher, params)

    return MutationSummary(edges=len(req.edges), warnings=warnings)


# --- fallback schema document (until grag.core.schema lands) -------------------------


def _render_schema_text(
    node_docs: list[NodeTableDoc], rel_docs: list[RelTableDoc]
) -> str:
    lines = ["Node tables:"]
    for d in node_docs:
        props = ", ".join(
            f"{p.name} {p.type}{' PK' if p.is_primary_key else ''}"
            for p in d.properties
        )
        lines.append(f"  {d.name}({props}) [{d.row_count} rows]")
    lines.append("Rel tables:")
    for r in rel_docs:
        props = ", ".join(f"{p.name} {p.type}" for p in r.properties)
        rel = f"{r.name}({props})" if props else r.name
        lines.append(
            f"  ({r.from_label})-[:{rel}]->({r.to_label}) [{r.row_count} rows]"
        )
    return "\n".join(lines)


def _fallback_schema_document(engine: Engine) -> SchemaDocument:
    """Minimal introspection via SHOW_TABLES/TABLE_INFO, used only while
    grag.core.schema.build_schema_document is not implemented."""
    tables = _table_index(engine)
    meta = _meta_rows(engine, tables)
    node_docs: list[NodeTableDoc] = []
    rel_docs: list[RelTableDoc] = []
    for name, kind in tables.items():
        if name == META_TABLE:
            continue
        cols = engine.execute(f"CALL TABLE_INFO('{name}') RETURN *").rows
        props = [
            PropertyDoc(name=r[1], type=r[2], is_primary_key=bool(r[4])) for r in cols
        ]
        if kind == "NODE":
            count = engine.execute(f"MATCH (n:{name}) RETURN count(*)").rows[0][0]
            m = meta.get(name) or {}
            node_docs.append(
                NodeTableDoc(
                    name=name,
                    properties=props,
                    row_count=int(count),
                    searchable=bool(m.get("searchable", False)),
                )
            )
        else:
            conn = _connection_of(engine, name) or ("", "")
            count = engine.execute(f"MATCH ()-[r:{name}]->() RETURN count(*)").rows[0][
                0
            ]
            rel_docs.append(
                RelTableDoc(
                    name=name,
                    from_label=conn[0],
                    to_label=conn[1],
                    properties=props,
                    row_count=int(count),
                )
            )
    return SchemaDocument(
        node_tables=node_docs,
        rel_tables=rel_docs,
        text=_render_schema_text(node_docs, rel_docs),
    )
