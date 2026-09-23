"""Offline cost crossover study: grag vs Markdown memory stores (M24 v2, P3).

A parametric break-even model for read-only memory reuse, grounded in the
m24-longitudinal-v2 measurements. It answers: at what store size and conditions
could grag's targeted retrieval pay off against reading Markdown? It cannot
prove session savings — it identifies the conditions worth a later paid run.

Arms per read-only recall session (the answer must be available in every arm):
- markdown-full:          read the whole memory file.
- markdown-targeted:      list section headers, then read the matching sections.
- markdown-grep:          bounded previews (160 chars) of matching sections only.
- markdown-grep-followup: previews plus a full read of the first 10 hit sections.
- grag:                   fixed session context (skill + MCP instructions) plus
                          one budget-bounded search reply (search only).
- grag-fetch-rejected:    the same search plus the REJECTED recovery rule from
                          tests/crossover_calibration.py: on a truncation signal,
                          re-fetch the first 3 seed records. On the fixture it
                          fired on 72/72 queries, cost a measured 808-1,727 mean
                          tokens per session and recovered ZERO decisive answers
                          (44/72 delivered with and without it). Modeled strictly
                          separately; never selected for break-even.

Break-even here is cost-only. Delivery rates differ between arms and strategies;
they are measured in tests/crossover_calibration.py, not modeled here. Overlap
adds near-duplicate distractors competing with one decisive record; a higher
overlap is not a genuinely broader question.

Pricing in input-token units (m24-v2 reconciliation): plain input 1x, cache
read 0.1x, one-hour cache write 2x, output 5x. A cache hit reads the matching
prefix; only new content is written. Session edits invalidate the memory file's
cache line; grag's static skill/instructions stay cached across sessions.

Usage: .venv/bin/python tests/crossover_study.py [--output report.json]
"""

from __future__ import annotations

import argparse
import json
from itertools import product

# Input-token-unit rates (m24-longitudinal-v2 billing reconciliation).
CACHE_READ = 0.1
CACHE_WRITE_1H = 2.0
OUTPUT = 5.0

# Grounded defaults from the v2 traces; per-record constants MEASURED by
# tests/crossover_calibration.py (audits/2026-09-22-crossover-calibration.json).
GRAG_FIXED_TOKENS = 2_400   # skill file (~8 KB) + MCP instructions
GRAG_FOOTER_TOKENS = 120    # JSON metadata footer per search reply
GRAG_NODE_OVERHEAD = 65     # measured 52-64: id + citation + _revision per packed node
GRAG_BUDGET = 2_000         # replies are budget-bounded; matches beyond it are NOT sent
# Rejected recovery rule, measured by tests/crossover_calibration.py: re-fetching
# the first 3 seeds on a truncation signal fired on 72/72 fixture queries, cost
# 808-1,727 mean tokens per session by record length, and recovered zero decisive
# answers. Its fetch payload is modeled as an upper bound (3 seed records plus
# footer); the arm is reported separately and never blended into search-only.
GRAG_FETCH_TOP_SEEDS = 3
MD_HEADER_TOKENS = 6        # measured 5.2-6.4 per record (all-header listing arm)
MD_PREVIEW_TOKENS = 45      # 160-char bounded grep preview + line prefix
MD_FOLLOWUP_SECTIONS = 10   # full sections read when previews are not enough
ANSWER_OUTPUT_TOKENS = 300  # equal across arms in read-only sessions


def session_cost(arm: str, *, n: int, record_len: int, matches: int,
                 update_freq: float, warm: bool) -> float:
    """Cost of one read-only recall session in input-token units."""
    if arm == "markdown-full":
        payload = n * record_len
        fixed = 0
    elif arm == "markdown-targeted":
        payload = n * MD_HEADER_TOKENS + matches * record_len
        fixed = 0
    elif arm == "markdown-grep":
        payload = matches * MD_PREVIEW_TOKENS
        fixed = 0
    elif arm == "markdown-grep-followup":
        payload = (matches * MD_PREVIEW_TOKENS
                   + min(matches, MD_FOLLOWUP_SECTIONS) * record_len)
        fixed = 0
    elif arm == "grag":
        # Measured: the reply is packed against the budget, so matches beyond it
        # are not sent. Search only; no follow-up fetch is modeled (see below).
        payload = min(GRAG_BUDGET, matches * (record_len + GRAG_NODE_OVERHEAD) + GRAG_FOOTER_TOKENS)
        fixed = GRAG_FIXED_TOKENS
    elif arm == "grag-fetch-rejected":
        # The rejected rule re-fetches the first 3 seeds only. Of the 28 fixture
        # misses, 23 were selection misses (decisive ID absent from the seeds)
        # and 5 truncation misses (packed, claim excerpted); it recovered none.
        # Kept for contrast, never break-even eligible.
        fetch = min(GRAG_BUDGET, GRAG_FETCH_TOP_SEEDS * (record_len + GRAG_NODE_OVERHEAD)
                    + GRAG_FOOTER_TOKENS)
        payload = (min(GRAG_BUDGET, matches * (record_len + GRAG_NODE_OVERHEAD) + GRAG_FOOTER_TOKENS)
                   + fetch)
        fixed = GRAG_FIXED_TOKENS
    else:
        raise ValueError(arm)

    # The memory file/payload is cache-written when cold or invalidated by an
    # edit since the last session, then read back for the answer turn.
    if not warm:
        payload_cost = payload * (CACHE_WRITE_1H + CACHE_READ)
    else:
        # Warm: unchanged payload is cache-read; edits rewrite it with probability u.
        payload_cost = payload * (update_freq * CACHE_WRITE_1H + CACHE_READ)
    # grag's fixed context is static across sessions: written once when cold,
    # cache-read thereafter. It dominates the modeled COLD overhead (5,040 vs
    # ~4,121 payload units at the smallest measured configuration) but NOT warm
    # usage (240 vs ~589 there); do not generalize fixed-cost dominance.
    fixed_cost = fixed * (CACHE_READ if warm else CACHE_WRITE_1H + CACHE_READ)
    return payload_cost + fixed_cost + ANSWER_OUTPUT_TOKENS * OUTPUT


def break_even_n(record_len: int, overlap: float | None, update_freq: float, warm: bool,
                 sizes: list[int], grag_arm: str = "grag") -> int | None:
    """Smallest N where the given grag strategy costs no more than the cheaper Markdown arm."""
    for n in sizes:
        matches = 1 if overlap is None else max(1, round(n * overlap))
        md = min(session_cost(arm, n=n, record_len=record_len, matches=matches,
                              update_freq=update_freq, warm=warm)
                 for arm in ("markdown-full", "markdown-targeted",
                             "markdown-grep", "markdown-grep-followup"))
        grag = session_cost(grag_arm, n=n, record_len=record_len, matches=matches,
                            update_freq=update_freq, warm=warm)
        if grag <= md:
            return n
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    sizes = [3, 10, 30, 100, 300, 1_000, 3_000]
    grid = []
    for record_len, overlap, update_freq, warm in product(
        (80, 200, 500), (None, 0.05, 0.15), (0.0, 0.1, 0.3), (False, True)
    ):
        be = break_even_n(record_len, overlap, update_freq, warm, sizes)
        be_fetch = break_even_n(record_len, overlap, update_freq, warm, sizes,
                                grag_arm="grag-fetch-rejected")
        grid.append({
            "record_len": record_len,
            "overlap": "single" if overlap is None else overlap,
            "update_freq": update_freq,
            "cache": "warm" if warm else "cold",
            "break_even_n": be,
            "break_even_n_fetch_rejected": be_fetch,
        })

    report = {
        "model": "input-token units; cache_read=0.1x, cache_write_1h=2x, output=5x",
        "grounding": {
            "grag_fixed_tokens": GRAG_FIXED_TOKENS,
            "grag_footer_tokens": GRAG_FOOTER_TOKENS,
            "grag_node_overhead_tokens": GRAG_NODE_OVERHEAD,
            "md_header_tokens": MD_HEADER_TOKENS,
            "answer_output_tokens": ANSWER_OUTPUT_TOKENS,
        },
        "rejected_strategies": {
            "grag-fetch-rejected": "On a truncation signal, re-fetch the first 3 "
                "seed records. Measured on the calibration fixture: fired 72/72, "
                "cost 808-1,727 mean tokens per session by record length, recovered "
                "zero decisive answers (44/72 delivered with and without it). "
                "Modeled as an upper-bound fetch payload; never break-even "
                "eligible.",
        },
        "caveat": "A parametric model, not a session measurement, compared at "
                  "UNEQUAL measured delivery rates. A break-even means bounded "
                  "grag replies can cost less than these Markdown strategies in "
                  "some larger synthetic configurations; reliable delivery "
                  "remains unresolved (44/72 decisive on the calibration "
                  "fixture). Overlap adds near-duplicate distractors to a "
                  "single-answer query, not a genuinely broader question. Fixed "
                  "context dominates modeled cold overhead but not warm usage.",
        "grid": grid,
    }
    text = json.dumps(report, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    print(f"{'record_len':>10} {'overlap':>8} {'updates':>8} {'cache':>5} "
          f"| break-even N: search-only | fetch-rejected")
    print("-" * 80)
    for row in grid:
        be = row["break_even_n"]
        be_fetch = row["break_even_n_fetch_rejected"]
        print(f"{row['record_len']:>10} {row['overlap']!s:>8} {row['update_freq']:>8} "
              f"{row['cache']:>5} | {be if be is not None else '>3000'} "
              f"| {be_fetch if be_fetch is not None else '>3000'}")


if __name__ == "__main__":
    main()
