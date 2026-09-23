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


_CORPUS_STATS: dict[int, tuple[int, float, dict[str, int]]] = {}


def _corpus_stats(engine, properties):
    """Document count, average length and per-stem document frequency over every
    searchable node, computed once per engine (evaluation only)."""
    key = id(engine)
    if key not in _CORPUS_STATS:
        documents = []
        for label, props in sorted(properties.items()):
            columns = ", ".join(f"n.{name}" for name in props)
            if not columns:
                continue
            for row in engine.execute(f"MATCH (n:{label}) RETURN {columns}").rows:
                documents.append(lexical._tokens("\n".join(v for v in row if isinstance(v, str))))
        vocabulary = sorted({t for doc in documents for t in doc})
        stems = {}
        for start in range(0, len(vocabulary), 1024):
            rows = engine.execute("UNWIND $terms AS term RETURN term, stem(term, 'english')",
                                  {"terms": vocabulary[start:start + 1024]}).rows
            stems.update(rows)
        df: dict[str, int] = defaultdict(int)
        for doc in documents:
            for stem in {stems[t] for t in doc}:
                df[stem] += 1
        average = sum(map(len, documents)) / len(documents) if documents else 1.0
        _CORPUS_STATS[key] = (len(documents), average or 1.0, dict(df))
    return _CORPUS_STATS[key]


def corpus_idf(engine, candidates, query, properties):
    """Cross-label BM25 with corpus-wide document frequency and length instead of
    shortlist statistics; single-label retrieval keeps native BM25 as in production."""
    if len({s.node.label for s in candidates}) < 2:
        return sorted(candidates, key=lambda s: (-s.score, s.node.id))
    n, average, df = _corpus_stats(engine, properties)
    candidates = sorted({s.node.id: s for s in candidates}.values(), key=lambda s: s.node.id)
    documents = [lexical._tokens("\n".join(v for name in properties[c.node.label]
                                           if isinstance(v := c.node.properties.get(name), str))) for c in candidates]
    query_tokens = lexical._tokens(query)
    terms = sorted(set(query_tokens).union(*(set(d) for d in documents)))
    stems = {}
    for start in range(0, len(terms), 1024):
        stems.update(engine.execute("UNWIND $terms AS term RETURN term, stem(term, 'english')",
                                    {"terms": terms[start:start + 1024]}).rows)
    query_terms = sorted({stems[t] for t in query_tokens})
    idf = {t: math.log1p((n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5)) for t in query_terms}
    ranked = []
    for candidate, tokens in zip(candidates, documents, strict=True):
        count = defaultdict(int)
        for token in tokens:
            count[stems[token]] += 1
        norm = lexical._K1 * (1 - lexical._B + lexical._B * len(tokens) / average)
        score = sum(idf[t] * count[t] * (lexical._K1 + 1) / (count[t] + norm) for t in query_terms if count[t])
        ranked.append(search.ScoredNode(node=candidate.node, score=score, match="fts"))
    return sorted(ranked, key=lambda s: (-s.score, s.node.id))


VARIANTS = ("baseline", "fields", "fields_weighted", "table_rrf", "table_rrf_scaled", "fusion_top8", "fusion_top16", "corpus_idf")


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
                 "table_rrf": native_table_ranks, "table_rrf_scaled": lambda *a: native_table_ranks(*a, scaled=True),
                 "corpus_idf": corpus_idf}
    with patch.object(search, "rank_lexical", functions[name]):
        yield
