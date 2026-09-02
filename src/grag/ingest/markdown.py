"""Section-aware document ingest: a Markdown file becomes a graph, not a bag
of chunks.

The flat loader (`grag.ingest.loaders`) cuts text into size-bounded chunks
with no notion of structure — fine for notes, wrong for a specification. A
100-page architecture document has a heading hierarchy that *is* its
structure: "3. Order routing > 3.2 Retry policy" is what an engineer cites,
and it is the unit an LLM should be handed as context. This loader keeps it:

    Document ──HAS_SECTION──▶ Section ◀──SUBSECTION_OF── Section
                                 ▲  └──NEXT_SECTION──▶ Section (reading order)
                                 └──IN_SECTION── Chunk (the searchable body)

Chunks stay the FTS/vector seeds (their text is what matches a query); one
hop up lands on the Section, whose `heading_path` names exactly where in the
spec the passage lives, and its neighbours give the surrounding sections.

Code linking. Whenever a section's body names a symbol in backticks
(`OrderRouter`, `retry_send()`, `algo4.routing`) and that symbol exists in
the code graph (from ingest_code) under a unique name, a
MENTIONS_FUNCTION / MENTIONS_CLASS / MENTIONS_MODULE edge is written. That is
the deterministic layer of "spec ↔ code": no LLM in the loop, never wrong
about *what* is named, silent about semantics. The semantic layer — which
function *implements* which section — is left to an agent: the empty
IMPLEMENTS (Function→Section) and IMPLEMENTS_CLASS (Class→Section) tables are
defined here so that pass has a known target instead of inventing labels.

Ids are deterministic: `<doc-identity>#<slug/path>` for sections (the same
`<slug>-<sha256>` document identity the flat loader uses, so re-ingesting a
file in either mode replaces the other's nodes) and `<section-id>@NNNN` for
chunks. Re-ingest is an authoritative sync per document: current sections
and chunks are MERGEd, stale ones pruned.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from grag.config import GragConfig
from grag.core.engine import Engine
from grag.core.errors import GragError
from grag.core.mutate import define_schema, upsert_edges, upsert_nodes
from grag.core.types import (
    DefineSchemaRequest,
    IngestRequest,
    IngestResponse,
    NodeTableSpec,
    PropertySpec,
    RelTableSpec,
    UpsertEdge,
    UpsertEdgesRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.ingest.loaders import (
    _chunk_text,
    _embed_pending,
    _normalized_source,
    _source_identity,
    _source_slug,
)

log = logging.getLogger(__name__)

_S = PropertySpec

DOCUMENT_LABEL = "Document"
SECTION_LABEL = "Section"

_DOC_NODE_TABLES = [
    NodeTableSpec(
        name=DOCUMENT_LABEL,
        primary_key="id",
        searchable=True,
        properties=[_S(name="title"), _S(name="path"), _S(name="sections", type="INT64")],
    ),
    NodeTableSpec(
        name=SECTION_LABEL,
        primary_key="id",
        searchable=True,
        properties=[
            _S(name="title"),
            # "3. Order routing > 3.2 Retry policy" — the citation an engineer
            # would give; also what a search on a heading phrase matches.
            _S(name="heading_path"),
            _S(name="level", type="INT64"),
            _S(name="position", type="INT64"),
            # Opening of the body (not the full text — that lives in chunks)
            # so a Section seed carries a glimpse of content on its own.
            _S(name="preview"),
            _S(name="char_count", type="INT64"),
        ],
    ),
]

_DOC_REL_TABLES = [
    RelTableSpec(name="HAS_SECTION", from_label=DOCUMENT_LABEL, to_label=SECTION_LABEL),
    RelTableSpec(name="SUBSECTION_OF", from_label=SECTION_LABEL, to_label=SECTION_LABEL),
    RelTableSpec(name="NEXT_SECTION", from_label=SECTION_LABEL, to_label=SECTION_LABEL),
]

# Section → code, written by this loader from backtick mentions; and the
# agent-populated semantic hooks (defined empty so the LLM pass has a
# ready-made, describe_schema-visible target).
_CODE_LINK_TABLES = {
    "Function": ("MENTIONS_FUNCTION", "IMPLEMENTS"),
    "Class": ("MENTIONS_CLASS", "IMPLEMENTS_CLASS"),
    "Module": ("MENTIONS_MODULE", None),
}

_ATX_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT_H1_RE = re.compile(r"^=+[ \t]*$")
_SETEXT_H2_RE = re.compile(r"^-+[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# `OrderRouter`, `retry_send()`, `algo4.routing.Router.route` — identifier-ish
# spans only; prose in backticks (`some phrase`) has spaces and is skipped.
_CODE_SPAN_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)(?:\(\))?`")


def chunk_rel_name(label: str) -> str:
    """IN_SECTION for the default Chunk label; suffixed for custom labels
    (rel tables bind one (from, to) pair)."""
    return "IN_SECTION" if label == "Chunk" else f"IN_SECTION_{label.upper()}"


# --- parsing ---------------------------------------------------------------------------


@dataclass
class ParsedSection:
    title: str
    level: int  # 1..6; 0 for the preamble before the first heading
    order: int  # document reading order, 0-based
    body_lines: list[str] = field(default_factory=list)
    parent: int | None = None  # index into the parsed list
    slug_path: str = ""

    @property
    def body(self) -> str:
        return "\n".join(self.body_lines).strip()


def parse_sections(text: str) -> tuple[str | None, list[ParsedSection]]:
    """Split Markdown into a heading tree.

    Returns (title, sections): title is the first level-1 heading (None if
    absent). Headings inside fenced code blocks are ignored. Body text before
    the first heading becomes a level-0 "Preamble" section when non-empty.
    Setext underlines (=== / ---) are honoured when they directly follow a
    text line, so a `---` rule after a blank line is not a heading.
    """
    lines = text.splitlines()
    sections: list[ParsedSection] = [ParsedSection(title="Preamble", level=0, order=0)]
    title: str | None = None
    in_fence = False
    fence_mark = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            mark = fence.group(1)
            if not in_fence:
                in_fence, fence_mark = True, mark
            elif mark == fence_mark:
                in_fence = False
            sections[-1].body_lines.append(line)
            i += 1
            continue
        if in_fence:
            sections[-1].body_lines.append(line)
            i += 1
            continue
        atx = _ATX_RE.match(line)
        heading: tuple[int, str] | None = None
        if atx:
            heading = (len(atx.group(1)), atx.group(2).strip())
        elif (
            i + 1 < len(lines)
            and line.strip()
            and not line.lstrip().startswith(("-", "*", "+", ">", "|", "#"))
            and (_SETEXT_H1_RE.match(lines[i + 1]) or _SETEXT_H2_RE.match(lines[i + 1]))
        ):
            heading = (1 if _SETEXT_H1_RE.match(lines[i + 1]) else 2, line.strip())
            i += 1  # consume the underline
        if heading is None:
            sections[-1].body_lines.append(line)
            i += 1
            continue
        level, heading_title = heading
        if title is None and level == 1:
            title = heading_title
        sections.append(
            ParsedSection(title=heading_title, level=level, order=len(sections))
        )
        i += 1

    # Parent links from a level stack (preamble never parents anything).
    stack: list[int] = []
    for idx, sec in enumerate(sections):
        if sec.level == 0:
            continue
        while stack and sections[stack[-1]].level >= sec.level:
            stack.pop()
        sec.parent = stack[-1] if stack else None
        stack.append(idx)

    if not sections[0].body:
        sections = sections[1:]
        for k, sec in enumerate(sections):
            sec.order = k
            if sec.parent is not None:
                sec.parent -= 1
    return title, sections


def _slug(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return (slug or "section")[:60].rstrip("-")


def _assign_slug_paths(sections: list[ParsedSection]) -> None:
    seen: dict[str, int] = {}
    for sec in sections:
        base = _slug(sec.title)
        if sec.parent is not None:
            base = f"{sections[sec.parent].slug_path}/{base}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        sec.slug_path = base if n == 0 else f"{base}~{n + 1}"


def heading_path(sections: list[ParsedSection], idx: int) -> str:
    parts: list[str] = []
    cur: int | None = idx
    while cur is not None:
        parts.append(sections[cur].title)
        cur = sections[cur].parent
    return " > ".join(reversed(parts))


# --- code mentions ----------------------------------------------------------------------


def _code_tables(engine: Engine) -> set[str]:
    try:
        rows = engine.execute("CALL SHOW_TABLES() RETURN *").rows
    except GragError:
        return set()
    names = {str(r[1]) for r in rows if str(r[2]).upper() == "NODE"}
    return names & set(_CODE_LINK_TABLES)


def _mentions(body: str) -> set[str]:
    return {m.group(1) for m in _CODE_SPAN_RE.finditer(body)}


def _resolve_symbols(
    engine: Engine, names: set[str], tables: set[str]
) -> dict[str, list[tuple[str, str]]]:
    """name -> [(label, node id)] for uniquely named code symbols.

    A bare name matches Function/Class/Module `name`. A dotted name resolves
    as, in order: a Function/Class qualname (`Router.route`), a symbol inside
    a module (`backoff.compute_delay`, `algo.backoff.compute_delay` with the
    repo name), or a module (`algo.storage`, repo-qualified or not).
    Ambiguous names (two `helper` functions in different modules) are skipped
    rather than guessed.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    if not names:
        return out
    plain = sorted(n for n in names if "." not in n)
    dotted = sorted(n for n in names if "." in n)
    for label in ("Function", "Class", "Module"):
        if label not in tables:
            continue
        if plain:
            rows = engine.execute(
                f"MATCH (n:{label}) WHERE n.name IN $names RETURN n.name, n.id",
                {"names": plain},
            ).rows
            for name, nid in rows:
                out.setdefault(str(name), []).append((label, str(nid)))
    for name in dotted:
        hits: list[tuple[str, str]] = []
        prefix, _, last = name.rpartition(".")
        for label in ("Function", "Class"):
            if label not in tables:
                continue
            rows = engine.execute(
                f"MATCH (n:{label}) WHERE n.id ENDS WITH $suffix RETURN n.id",
                {"suffix": f"#{name}"},
            ).rows
            if not rows and "Module" in tables:
                rows = engine.execute(
                    "MATCH (r:Repo)-[:CONTAINS_REPO_MODULE]->(m:Module)"
                    f"-[:CONTAINS_MODULE_{label.upper()}]->(n:{label}) "
                    "WHERE n.name = $last "
                    "AND (m.name = $prefix OR r.name + '.' + m.name = $prefix) "
                    "RETURN n.id",
                    {"last": last, "prefix": prefix},
                ).rows
            hits.extend((label, str(r[0])) for r in rows)
        if not hits and "Module" in tables:
            rows = engine.execute(
                "MATCH (r:Repo)-[:CONTAINS_REPO_MODULE]->(n:Module) "
                "WHERE n.name = $name OR r.name + '.' + n.name = $name RETURN n.id",
                {"name": name},
            ).rows
            hits.extend(("Module", str(r[0])) for r in rows)
        if hits:
            out[name] = hits
    # Unique resolution only: one label, one node.
    return {name: hits for name, hits in out.items() if len(hits) == 1}


# --- public: ingest_markdown ----------------------------------------------------------------


def ingest_markdown(
    engine: Engine, config: GragConfig, req: IngestRequest
) -> IngestResponse:
    """Ingest `req.documents` as Document/Section/Chunk graphs (see module doc)."""
    chunk_label = req.label
    rel_in_section = chunk_rel_name(chunk_label)
    code_tables = _code_tables(engine)
    rel_tables = [
        *_DOC_REL_TABLES,
        RelTableSpec(name=rel_in_section, from_label=chunk_label, to_label=SECTION_LABEL),
    ]
    for label in sorted(code_tables):
        mentions, implements = _CODE_LINK_TABLES[label]
        rel_tables.append(
            RelTableSpec(name=mentions, from_label=SECTION_LABEL, to_label=label)
        )
        if implements:
            rel_tables.append(
                RelTableSpec(name=implements, from_label=label, to_label=SECTION_LABEL)
            )
    define_schema(
        engine,
        config,
        DefineSchemaRequest(
            node_tables=[
                *_DOC_NODE_TABLES,
                NodeTableSpec(
                    name=chunk_label,
                    primary_key="id",
                    properties=[_S(name="text"), _S(name="meta")],
                    searchable=True,
                ),
            ],
            rel_tables=rel_tables,
        ),
    )

    docs: list[UpsertNode] = []
    sections_out: list[UpsertNode] = []
    chunks: list[UpsertNode] = []
    edges: list[UpsertEdge] = []
    code_links = 0
    identities: dict[str, set[str]] = {}
    seen_identity: dict[str, int] = {}

    for doc in req.documents:
        base_identity = _source_identity(doc.source, doc.text, doc.metadata)
        n = seen_identity.get(base_identity, 0)
        seen_identity[base_identity] = n + 1
        identity = base_identity if n == 0 else f"{base_identity}~{n:04d}"
        desired = identities.setdefault(identity, set())

        title, sections = parse_sections(doc.text)
        _assign_slug_paths(sections)
        doc_title = (
            str(doc.metadata.get("title"))
            if doc.metadata.get("title")
            else title or _source_slug(doc.source)
        )
        docs.append(
            UpsertNode(
                label=DOCUMENT_LABEL,
                key=identity,
                properties={
                    "title": doc_title,
                    "path": _normalized_source(doc.source) if doc.source else "",
                    "sections": len(sections),
                },
                source=doc.source,
            )
        )
        section_ids: list[str] = []
        for idx, sec in enumerate(sections):
            sid = f"{identity}#{sec.slug_path}"
            section_ids.append(sid)
            desired.add(sid)
            body = sec.body
            path = heading_path(sections, idx)
            preview = re.sub(r"\s+", " ", body)[:240]
            sections_out.append(
                UpsertNode(
                    label=SECTION_LABEL,
                    key=sid,
                    properties={
                        "title": sec.title,
                        "heading_path": path,
                        "level": sec.level,
                        "position": sec.order,
                        "preview": preview,
                        "char_count": len(body),
                    },
                    source=doc.source,
                )
            )
            edges.append(
                UpsertEdge(
                    type="HAS_SECTION",
                    from_label=DOCUMENT_LABEL,
                    from_key=identity,
                    to_label=SECTION_LABEL,
                    to_key=sid,
                    source=doc.source,
                )
            )
            if sec.parent is not None:
                edges.append(
                    UpsertEdge(
                        type="SUBSECTION_OF",
                        from_label=SECTION_LABEL,
                        from_key=sid,
                        to_label=SECTION_LABEL,
                        to_key=f"{identity}#{sections[sec.parent].slug_path}",
                        source=doc.source,
                    )
                )
            if idx > 0:
                edges.append(
                    UpsertEdge(
                        type="NEXT_SECTION",
                        from_label=SECTION_LABEL,
                        from_key=section_ids[idx - 1],
                        to_label=SECTION_LABEL,
                        to_key=sid,
                        source=doc.source,
                    )
                )
            if body:
                pieces = (
                    _chunk_text(body, req.chunk_size, req.chunk_overlap)
                    if req.chunk
                    else [body]
                )
                meta = {
                    "document": doc_title,
                    "section": sec.title,
                    "heading_path": path,
                    "level": sec.level,
                    **({k: v for k, v in doc.metadata.items() if k != "title"}),
                }
                for i, piece in enumerate(pieces):
                    cid = f"{sid}@{i:04d}"
                    desired.add(cid)
                    chunks.append(
                        UpsertNode(
                            label=chunk_label,
                            key=cid,
                            properties={
                                "text": piece,
                                "meta": json.dumps(meta, sort_keys=True),
                            },
                            source=doc.source,
                        )
                    )
                    edges.append(
                        UpsertEdge(
                            type=rel_in_section,
                            from_label=chunk_label,
                            from_key=cid,
                            to_label=SECTION_LABEL,
                            to_key=sid,
                            source=doc.source,
                        )
                    )
            if code_tables:
                for _name, hits in _resolve_symbols(
                    engine, _mentions(body), code_tables
                ).items():
                    label, nid = hits[0]
                    edges.append(
                        UpsertEdge(
                            type=_CODE_LINK_TABLES[label][0],
                            from_label=SECTION_LABEL,
                            from_key=sid,
                            to_label=label,
                            to_key=nid,
                            source=doc.source,
                        )
                    )
                    code_links += 1

    for batch in (docs, sections_out, chunks):
        if batch:
            upsert_nodes(engine, config, UpsertNodesRequest(nodes=batch))
    pruned = _prune_document_graph(engine, chunk_label, identities)
    if edges:
        by_type: dict[str, list[UpsertEdge]] = {}
        for e in edges:
            by_type.setdefault(e.type, []).append(e)
        for rel in by_type.values():
            upsert_edges(engine, config, UpsertEdgesRequest(edges=rel))
    for label in (chunk_label, SECTION_LABEL, DOCUMENT_LABEL):
        _embed_pending(engine, config, label)
    return IngestResponse(
        label=chunk_label,
        nodes_created=len(chunks),
        nodes_pruned=pruned,
        documents=len(docs),
        sections=len(sections_out),
        code_links=code_links,
    )


def _prune_document_graph(
    engine: Engine, chunk_label: str, identities: dict[str, set[str]]
) -> int:
    """Authoritative sync per document identity: drop sections/chunks that
    are no longer produced (including flat-loader chunks of the same file)."""
    pruned = 0
    for identity, desired in identities.items():
        prefix = f"{identity}#"
        for label in (SECTION_LABEL, chunk_label):
            res = engine.execute_write(
                f"MATCH (n:{label}) WHERE n.id STARTS WITH $prefix "
                "AND NOT n.id IN $keys DETACH DELETE n RETURN count(n)",
                {"prefix": prefix, "keys": sorted(desired)},
            )
            if res.rows:
                pruned += int(res.rows[0][0])
    return pruned
