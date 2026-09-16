"""Evaluation-only ranking alternatives; never imported by grag production code."""

import math
from collections import defaultdict
from contextlib import contextmanager
from unittest.mock import patch

from grag.retrieval import lexical, search


def native_table_ranks(engine, candidates, query, properties, *, scaled=False):
    """Fuse disjoint table ranks, retaining equal-score competition ranks.

    Every table winner ties in both variants. Relative-score scaling only
    discounts weaker hits *within* each table; it cannot calibrate its winner.
    """
    tables = defaultdict(dict)
    for hit in candidates:
        if math.isfinite(hit.score):
            old = tables[hit.node.label].get(hit.node.id)
            if old is None or hit.score > old.score:
                tables[hit.node.label][hit.node.id] = hit
    result = []
    for hits in tables.values():
        ordered = sorted(hits.values(), key=lambda h: (-h.score, h.node.id))
        previous = None
        rank = 0
        for i, hit in enumerate(ordered, 1):
            if hit.score != previous:
                rank = i
            previous = hit.score
            scale = max(0.0, hit.score) / ordered[0].score if scaled and ordered[0].score > 0 else 1.0
            result.append(hit.model_copy(update={"score": scale / (60 + rank)}))
    return sorted(result, key=lambda h: (-h.score, h.node.id))


def fields(engine, candidates, query, properties, *, weighted=False):
    columns = {table: list(names) for table, names in properties.items()}
    if "Function" in columns:
        columns["Function"] = [name for name in columns["Function"] if name not in {"id", "identity_signature"}]
        if weighted:
            # Repeated fields are an explicit simple term-frequency experiment,
            # not BM25F with independently normalized fields.
            columns["Function"] = [name for name in columns["Function"]
                                   for _ in range({"name": 3, "docstring": 2}.get(name, 1))]
    return lexical.rank_lexical(engine, candidates, query, columns)


def bounded_fusion(lists, limit, fuse):
    """Experiment: fixed top N unique hits per modality before existing RRF.

    The frozen runner uses top_k=8. Boundary ties use node ID, as in production
    ordering; extending all ties could defeat the intended candidate bound.
    """
    bounded = {}
    for modality, hits in lists.items():
        unique = {}
        for hit in hits:
            if math.isfinite(hit.score):
                old = unique.get(hit.node.id)
                if old is None or hit.score > old.score:
                    unique[hit.node.id] = hit
        bounded[modality] = sorted(unique.values(), key=lambda h: (-h.score, h.node.id))[:limit]
    return fuse(bounded)


VARIANTS = ("baseline", "fields", "fields_weighted", "table_rrf", "table_rrf_scaled", "fusion_top8", "fusion_top16")


@contextmanager
def ranking_variant(name):
    if name == "baseline":
        yield
        return
    if name.startswith("fusion_top"):
        fuse = search._rrf_fuse
        limit = int(name.removeprefix("fusion_top"))
        with patch.object(search, "_rrf_fuse", lambda lists: bounded_fusion(lists, limit, fuse)):
            yield
        return
    functions = {"fields": fields, "fields_weighted": lambda *a: fields(*a, weighted=True),
                 "table_rrf": native_table_ranks, "table_rrf_scaled": lambda *a: native_table_ranks(*a, scaled=True)}
    with patch.object(search, "rank_lexical", functions[name]):
        yield
