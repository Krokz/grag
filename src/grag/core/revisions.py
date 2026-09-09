"""Opaque content revisions, computed from stored evidence rather than counters.

No DDL/backfill is needed. Internal ingestion, relocation, and imports change
these tokens whenever the evidence changes; embedding/index maintenance does not.
Reverting to identical content can restore a prior token: these are preconditions
on state, not an edit history or a guarantee that no intervening write occurred.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import TypeAdapter

from grag.core.types import VECTOR_PROPS

_JSON: TypeAdapter[Any] = TypeAdapter(Any)
RELATIONSHIP_REVISION_PREFIX = "r2:"
_REVISION_METADATA = {
    "_LABEL",
    "_TYPE",
    "_SRC",
    "_DST",
    "_source",
    "_created_at",
    "_document_owner",
    "_evidence_state", "_review_state", "_expires_at", "_superseded_by",
    "_evidence_seq", "_document_state", "_source_state",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        _JSON.dump_python(value, mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_revision(value: dict) -> str:
    # Relationship identity is selected by type and logical endpoint keys on
    # the guarded upsert. Its content token must not depend on storage offsets.
    # No endpoint lookup is needed, including for RETURN r alone or nested paths.
    relationship = "_SRC" in value and "_DST" in value
    evidence = {
        k: v
        for k, v in value.items()
        if v is not None
        and k not in VECTOR_PROPS
        and not (relationship and k in {"_SRC", "_DST"})
        and (not k.startswith("_") or k in _REVISION_METADATA)
    }
    # Older raw tables can contain non-finite floats; JSON's explicit encoding
    # keeps such evidence readable and deterministically fingerprinted too.
    encoded = json.dumps(
        _JSON.dump_python(evidence, mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    namespace = "grag-relationship-content-v2:" if relationship else "grag-content-v1:"
    digest = hashlib.sha256((namespace + encoded).encode()).hexdigest()
    return RELATIONSHIP_REVISION_PREFIX + digest if relationship else digest


def annotate_revisions(value: Any) -> Any:
    """Present whole entities without derived vectors; projections stay exact.

    Recurse through paths, collections and maps, retaining native identity and
    provenance. An explicitly projected vector is a plain list and is untouched.
    Revisions always fingerprint the original evidence before presentation.
    """
    if isinstance(value, dict):
        if "_LABEL" in value and "_ID" in value:
            return {
                **{k: v for k, v in value.items() if k not in VECTOR_PROPS},
                "_revision": content_revision(value),
            }
        return {key: annotate_revisions(v) for key, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [annotate_revisions(v) for v in value]
    return value
