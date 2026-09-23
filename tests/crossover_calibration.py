"""Offline calibration for the crossover study (M24 v2 follow-up).

The parametric calculator (tests/crossover_study.py) assumed constants for
header-listing cost, grep selectivity and grag's per-node packing overhead, and
assumed evidence delivery. This harness MEASURES those constants on a synthetic
memory fixture with the real local stack: FastEmbed retrieval, real Markdown
files, frozen queries, actual MCP reply sizes at the default 2,000-token budget.
No paid calls; no production database.

Per config (store size x record length x topic overlap), four read-only arms:
- markdown-full:    read the whole memory file.
- markdown-headers: read every section header, then sections whose title
                    matches a query term (all-header navigation strategy).
- markdown-grep:    grep the file for query terms, then read the hit sections
                    (targeted passage discovery; bodies are single lines, so a
                    grep hit returns the whole body, as in a real terminal).
- grag:             one search_knowledge call (surface="mcp", budget 2,000).
- grag-total:       the search plus the REJECTED observable recovery rule: when
                    the reply envelope reports truncation or omitted properties,
                    fetch the full records of the first FETCH_TOP_SEEDS seeds
                    (IDs visible in the reply; never the gold claim). Measured
                    outcome on this fixture: the rule fires on every query, adds
                    roughly 800-1,700 tokens, and recovers zero decisive answers;
                    it is retained only as rejected-strategy evidence.

Decisive evidence: each query has exactly one decisive record carrying a unique
claim phrase. An arm "delivers" only if that phrase reaches the agent. Grag
misses are attributed by decisive ID, not by gold text: an ID absent from the
reply seeds is a SELECTION miss; an ID present whose packed body lost the claim
(packing excerpts long values) is a TRUNCATION miss.

Usage: .venv/bin/python tests/crossover_calibration.py [--output report.json]
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
from pathlib import Path

from grag.config import EmbedderConfig, GragConfig
from grag.core.engine import Engine
from grag.core.mutate import define_schema, upsert_nodes
from grag.core.serialize import estimate_tokens
from grag.core.types import (
    ContextRequest,
    DefineSchemaRequest,
    NodeTableSpec,
    PropertySpec,
    SearchRequest,
    UpsertNode,
    UpsertNodesRequest,
)
from grag.retrieval.context import get_context
from grag.retrieval.packing import mcp_retrieval_text
from grag.retrieval.search import search_knowledge
from grag.retrieval.vectors import embed_pending_nodes, searchable_node_tables

BUDGET = 2_000
GRAG_FIXED_TOKENS = 2_400   # skill + MCP instructions (v2 measurement)
ANSWER_OUTPUT_TOKENS = 300
CACHE_READ, CACHE_WRITE_1H, OUTPUT = 0.1, 2.0, 5.0
QUERIES_PER_CONFIG = 6
DISTRACTOR_TOPICS = 30

TOPIC_WORDS = ["cache", "usage", "billing", "schema", "migration", "retry",
               "session", "index", "policy", "backup", "routing", "quota"]
FILLER = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
          "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
          "sigma", "tau", "upsilon"]


def sized_body(rng, fixed: str, target_tokens: int) -> str:
    """Pad with filler until the body measures target_tokens on grag's estimator,
    so size labels are honest (the previous words*2 rule overshot ~2.6x)."""
    body = fixed
    while estimate_tokens(body) < target_tokens:
        body += " " + " ".join(rng.choice(FILLER) for _ in range(16))
    while estimate_tokens(body) > target_tokens and " " in body:
        body = body.rsplit(" ", 8)[0]
    return body.strip()


def build_fixture(n: int, record_len: int, overlap: float, seed: int):
    """Deterministic memory store: Q query topics, each with overlap*N competitor
    records and one decisive record; the rest spread over distractor topics.
    The decisive claim rotates between start/middle/end of the body so bounded
    previews are not guaranteed to deliver it."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture, not crypto
    records = []  # (key, title, body, topic, decisive_query_or_none)
    queries = []
    per_topic = max(2, round(n * overlap))
    for q in range(QUERIES_PER_CONFIG):
        topic = TOPIC_WORDS[q % len(TOPIC_WORDS)] + str(q)
        terms = [f"{topic}alpha", f"{topic}beta"]
        claim = f"decisive-claim-{q}: the {topic} threshold is {30 + q} seconds"
        position = q % 3  # 0 start, 1 middle, 2 end
        for i in range(per_topic):
            decisive = i == 0
            fixed = f"{terms[0]} {terms[1]}"
            body = sized_body(rng, fixed, record_len)
            if decisive:
                filler = body[len(fixed):].strip()
                half = len(filler.split()) // 2
                if position == 0:
                    body = f"{fixed} {claim} {filler}"
                elif position == 1:
                    body = f"{fixed} {' '.join(filler.split()[:half])} {claim} {' '.join(filler.split()[half:])}"
                else:
                    body = f"{fixed} {filler} {claim}"
            key = f"q{q}-rec-{i}"
            title = f"{topic} policy note {i}" if not decisive else f"{topic} threshold ruling"
            records.append((key, title, body.strip(), topic, q if decisive else None))
        queries.append({"q": q, "terms": terms, "claim": claim,
                        "decisive_id": f"Decision:q{q}-rec-0",
                        "text": f"what is the current {terms[0]} {terms[1]} policy?"})
    distractor_n = n - len(records)
    for i in range(max(0, distractor_n)):
        topic = f"distr{i % DISTRACTOR_TOPICS}"
        body = sized_body(rng, f"{topic} note", record_len)
        records.append((f"distractor-{i}", f"{topic} note {i}", body.strip(), topic, None))
    rng.shuffle(records)
    return records, queries


def render_markdown(records) -> str:
    return "\n".join(f"## {title}\n{body}\n" for _, title, body, _, _ in records)


PREVIEW_CHARS = 160  # bounded grep preview, as in rg with truncated lines
FOLLOWUP_SECTIONS = 10  # agent reads at most this many hit sections in full
FETCH_TOP_SEEDS = 3  # rejected observable fetch rule: full records for the first 3 seeds


def markdown_arms(md_text: str, records, query: dict):
    """Payload tokens and delivery for the three Markdown strategies. The grep
    arm returns bounded previews (not whole lines); when no preview answers the
    query the agent reads up to FOLLOWUP_SECTIONS hit sections in full. Neither
    arm may see the gold claim — delivery is scored afterwards."""
    terms = [t.lower() for t in query["terms"]]
    sections = [(f"## {t}\n{b}\n", t, b) for _, t, b, _, _ in records]
    full = estimate_tokens(md_text)

    header_text = "\n".join(f"## {t}" for _, t, _, _, _ in records)
    title_hits = [s for s in sections if any(term in s[1].lower() for term in terms)]
    headers = estimate_tokens(header_text) + sum(estimate_tokens(s[0]) for s in title_hits)

    grep_hits = [s for s in sections if any(term in s[2].lower() for term in terms)]
    previews = []
    for _, _, body in grep_hits:
        first = min(body.lower().find(term) for term in terms if term in body.lower())
        previews.append(body[max(0, first - 20):first + PREVIEW_CHARS])
    preview_tokens = sum(estimate_tokens(p) + 8 for p in previews)
    preview_text = "\n".join(previews)
    # Two strategy variants, both scored with gold afterwards — no gold-shaped
    # trigger. previews-only: the agent settles for previews. followup: the
    # agent also reads the first FOLLOWUP_SECTIONS hit sections in full.
    followup_text = preview_text + "\n" + "\n".join(
        s[2] for s in grep_hits[:FOLLOWUP_SECTIONS])
    followup_tokens = preview_tokens + sum(
        estimate_tokens(s[0]) for s in grep_hits[:FOLLOWUP_SECTIONS])

    def delivered(hits) -> bool:
        return any(query["claim"] in s[2] for s in hits)

    return {
        "markdown-full": {"tokens": full, "delivered": True},
        "markdown-headers": {"tokens": headers, "delivered": delivered(title_hits)},
        "markdown-grep": {"tokens": preview_tokens,
                          "delivered": query["claim"] in preview_text},
        "markdown-grep-followup": {"tokens": followup_tokens,
                                   "delivered": query["claim"] in followup_text},
    }


def grag_arm(engine, config, query: dict):
    """Measured MCP reply plus the REJECTED observable follow-up rule: when the
    reply envelope reports truncation or omitted properties, fetch the full
    records of the first FETCH_TOP_SEEDS seeds (IDs visible in the reply). The
    rule never sees the gold claim or its ID; delivery is scored afterwards.

    Miss stages are attributed by decisive ID, not gold text: response seeds are
    already filtered to nodes that survived packing, so an absent ID is a
    selection miss, while a present ID whose packed body no longer contains the
    claim (packing excerpts long values) is a truncation miss."""
    resp = search_knowledge(engine, config, SearchRequest(
        query=query["text"], labels=["Decision"], token_budget=BUDGET), surface="mcp")
    text = mcp_retrieval_text(resp)
    decisive_id = query["decisive_id"]
    seed_ids = [s.node.id for s in resp.seeds]
    packed_body = next((str(node.properties.get("body", ""))
                        for node in resp.subgraph.nodes if node.id == decisive_id), None)
    packed_bodies = sum(estimate_tokens(str(node.properties.get("body", "")))
                        for node in resp.subgraph.nodes)
    result = {"tokens": resp.response_token_estimate,
              "delivered": query["claim"] in text,  # scored with gold, after the fact
              "nodes_packed": len(resp.subgraph.nodes), "packed_body_tokens": packed_bodies,
              "decisive_selected": decisive_id in seed_ids,
              "decisive_claim_truncated": packed_body is not None and query["claim"] not in packed_body,
              "decisive_in_fetch_ids": False, "fetch_tokens": 0,
              "delivered_after_fetch": query["claim"] in text}
    # Observable trigger only: the reply envelope tells the agent the answer set
    # was cut (truncated) or bodies were omitted. No gold knowledge: the fetched
    # IDs are the first seeds visible in the reply.
    if (resp.truncated or resp.omitted_properties) and resp.seeds:
        fetch_ids = seed_ids[:FETCH_TOP_SEEDS]
        result["decisive_in_fetch_ids"] = decisive_id in fetch_ids
        ctx = get_context(engine, config, ContextRequest(
            node_ids=fetch_ids, hops=0, token_budget=BUDGET), surface="mcp")
        result["fetch_tokens"] = ctx.response_token_estimate
        result["delivered_after_fetch"] = (
            result["delivered"] or query["claim"] in mcp_retrieval_text(ctx))
    return result


def session_cost(payload: int, *, fixed: int, warm: bool, update_freq: float = 0.1) -> float:
    payload_cost = payload * ((update_freq * CACHE_WRITE_1H + CACHE_READ) if warm
                              else (CACHE_WRITE_1H + CACHE_READ))
    fixed_cost = fixed * (CACHE_READ if warm else CACHE_WRITE_1H + CACHE_READ)
    return payload_cost + fixed_cost + ANSWER_OUTPUT_TOKENS * OUTPUT


def run_config(n: int, record_len: int, overlap: float, seed: int) -> dict:
    records, queries = build_fixture(n, record_len, overlap, seed)
    md_text = render_markdown(records)
    with tempfile.TemporaryDirectory() as tmp:
        config = GragConfig(db_path=Path(tmp) / "calib.lbdb",
                            embedder=EmbedderConfig(provider="fastembed"))
        engine = Engine(config)
        try:
            define_schema(engine, config, DefineSchemaRequest(node_tables=[NodeTableSpec(
                name="Decision", properties=[PropertySpec(name="title"),
                                             PropertySpec(name="body")])]))
            upsert_nodes(engine, config, UpsertNodesRequest(nodes=[
                UpsertNode(label="Decision", key=key, properties={"title": title, "body": body})
                for key, title, body, _, _ in records]))
            for table in searchable_node_tables(engine, config):
                embed_pending_nodes(engine, config, table, max_nodes=None)
            arms = {name: [] for name in ("markdown-full", "markdown-headers",
                                          "markdown-grep", "markdown-grep-followup",
                                          "grag", "grag-total")}
            grag_extra = {"fetch_needed": 0, "decisive_selected": 0,
                          "decisive_claim_truncated": 0, "decisive_in_fetch_ids": 0,
                          "nodes_packed": [], "overhead_tokens": []}
            for query in queries:
                md = markdown_arms(md_text, records, query)
                for name, res in md.items():
                    arms[name].append(res)
                g = grag_arm(engine, config, query)
                arms["grag"].append({"tokens": g["tokens"], "delivered": g["delivered"]})
                arms["grag-total"].append({"tokens": g["tokens"] + g["fetch_tokens"],
                                           "delivered": g["delivered_after_fetch"]})
                grag_extra["fetch_needed"] += 1 if g["fetch_tokens"] else 0
                grag_extra["decisive_selected"] += 1 if g["decisive_selected"] else 0
                grag_extra["decisive_claim_truncated"] += 1 if g["decisive_claim_truncated"] else 0
                grag_extra["decisive_in_fetch_ids"] += 1 if g["decisive_in_fetch_ids"] else 0
                grag_extra["nodes_packed"].append(g["nodes_packed"])
                if g["nodes_packed"]:
                    grag_extra["overhead_tokens"].append(
                        (g["tokens"] - g["packed_body_tokens"]) / g["nodes_packed"])
            summary = {}
            for name, runs in arms.items():
                mean_tokens = sum(r["tokens"] for r in runs) / len(runs)
                delivery = sum(1 for r in runs if r["delivered"]) / len(runs)
                fixed = GRAG_FIXED_TOKENS if name.startswith("grag") else 0
                summary[name] = {
                    "mean_payload_tokens": round(mean_tokens, 1),
                    "delivery_rate": round(delivery, 3),
                    "cost_cold": round(session_cost(mean_tokens, fixed=fixed, warm=False), 1),
                    "cost_warm": round(session_cost(mean_tokens, fixed=fixed, warm=True), 1),
                }
            summary["grag-diagnostics"] = {
                "fetch_needed_of": f"{grag_extra['fetch_needed']}/{len(queries)}",
                "decisive_selected_of": f"{grag_extra['decisive_selected']}/{len(queries)}",
                "decisive_claim_truncated_of": f"{grag_extra['decisive_claim_truncated']}/{len(queries)}",
                "decisive_in_fetch_ids_of": f"{grag_extra['decisive_in_fetch_ids']}/{len(queries)}",
                "mean_nodes_packed": round(sum(grag_extra["nodes_packed"]) / len(queries), 1),
                "mean_overhead_per_node": (
                    round(sum(grag_extra["overhead_tokens"]) / len(grag_extra["overhead_tokens"]), 1)
                    if grag_extra["overhead_tokens"] else None),
                "md_header_tokens_per_record": round(
                    estimate_tokens("\n".join(f"## {t}" for _, t, _, _, _ in records)) / n, 1),
            }
            return {"n": n, "record_len": record_len, "overlap": overlap,
                    "measured_body_tokens": round(
                        sum(estimate_tokens(b) for _, _, b, _, _ in records) / n, 1),
                    "arms": summary}
        finally:
            engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    configs = [(n, rl, ov) for n in (100, 300, 1000) for rl in (200, 500)
               for ov in (0.05, 0.15)]
    results = []
    for i, (n, rl, ov) in enumerate(configs):
        print(f"[{i + 1}/{len(configs)}] n={n} record_len={rl} overlap={ov} ...", flush=True)
        results.append(run_config(n, rl, ov, seed=20260922 + i))
        arms = results[-1]["arms"]
        print("  " + " | ".join(
            f"{name}: {a['mean_payload_tokens']:.0f}tok del={a['delivery_rate']:.2f} "
            f"cold={a['cost_cold']:.0f} warm={a['cost_warm']:.0f}"
            for name, a in arms.items() if name != "grag-diagnostics"))
        print(f"  grag-diagnostics: {arms['grag-diagnostics']}")

    report = {
        "method": "measured payloads and delivery on a synthetic fixture; real "
                  "FastEmbed retrieval, real Markdown, frozen queries, budget 2000",
        "cost_model": "input-token units; cache_read=0.1x, cache_write_1h=2x, "
                      "output=5x; warm assumes update_freq=0.1",
        "caveat": "Synthetic fixture with known-decisive records. Calibrates the "
                  "calculator's constants; does not by itself prove session savings.",
        "miss_stages": "decisive_selected = decisive ID among the reply seeds "
                       "(absent means a selection miss); decisive_claim_truncated "
                       "= decisive node packed but its packed body lost the claim "
                       "(a truncation miss); decisive_in_fetch_ids = the rejected "
                       "fetch rule's 3 IDs included the target (absent means the "
                       "rule could not have recovered it).",
        "results": results,
    }
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
