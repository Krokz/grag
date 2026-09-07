"""Cross-table lexical reranking over a bounded FTS candidate pool.

Native BM25 supplies candidates, but its per-index corpus statistics are not
comparable across labels. Recompute BM25 with one candidate-pool vocabulary,
document frequency and average length before lexical/vector rank fusion. These
are shortlist statistics, not statistics for the entire graph.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

from grag.core.engine import Engine
from grag.core.limits import bounded_work, charge
from grag.core.types import ScoredNode

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_K1 = 1.2
_B = 0.75


def _tokens(text: str) -> list[str]:
    charge("lexical_bytes", len(text.encode("utf-8")))
    tokens = _WORDS.findall(unicodedata.normalize("NFKC", text).casefold())
    charge("lexical_terms", len(tokens))
    return tokens


@bounded_work
def rank_lexical(
    engine: Engine,
    candidates: list[ScoredNode],
    query: str,
    properties: dict[str, list[str]],
) -> list[ScoredNode]:
    """Use shared BM25 statistics when hits span more than one label.

    Single-label retrieval keeps native BM25. Cross-label reranking uses only
    the same public STRING columns indexed by FTS; provenance/vector metadata
    never influence length or term counts. Native Snowball stemming matches
    grag's default English FTS stemmer without another package/model dependency.
    """
    if len({s.node.label for s in candidates}) < 2:
        return sorted(candidates, key=lambda s: (-s.score, s.node.id))
    candidates = sorted(
        {s.node.id: s for s in candidates}.values(), key=lambda s: s.node.id
    )
    documents = [
        _tokens(
            "\n".join(
                value
                for name in properties[scored.node.label]
                if isinstance(value := scored.node.properties.get(name), str)
            )
        )
        for scored in candidates
    ]
    query_tokens = _tokens(query)
    terms = sorted(set(query_tokens).union(*(set(doc) for doc in documents)))
    # The FTS extension was loaded while retrieving candidates. Keep parameters
    # bounded to batches rather than emitting one query per word/document.
    stems: dict[str, str] = {}
    for start in range(0, len(terms), 1024):
        result = engine.execute(
            "UNWIND $terms AS term RETURN term, stem(term, 'english')",
            {"terms": terms[start : start + 1024]},
        )
        stems.update((term, stem) for term, stem in result.rows)
    counts = [Counter(stems[token] for token in doc) for doc in documents]
    query_terms = sorted({stems[token] for token in query_tokens})
    average_length = sum(map(len, documents)) / len(documents) or 1.0
    n = len(documents)
    idf = {}
    for term in query_terms:
        df = sum(term in count for count in counts)
        idf[term] = math.log1p((n - df + 0.5) / (df + 0.5))
    ranked = []
    for candidate, tokens, count in zip(candidates, documents, counts, strict=True):
        norm = _K1 * (1 - _B + _B * len(tokens) / average_length)
        score = sum(
            idf[term] * count[term] * (_K1 + 1) / (count[term] + norm)
            for term in query_terms
            if count[term]
        )
        ranked.append(ScoredNode(node=candidate.node, score=score, match="fts"))
    return sorted(ranked, key=lambda s: (-s.score, s.node.id))
