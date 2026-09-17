"""Opt-in local agent evaluation helpers; never imported by grag at runtime.

Private repositories, questions and transcripts belong outside tracked fixtures.
Provider totals are taken from the terminal result, not summed streaming events.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path


def parse_events(text: str) -> list[dict]:
    """Read JSONL events, including valid JSON with leading whitespace.

    Harness diagnostics outside the JSONL stream may be ignored; malformed
    JSON-looking records must not silently disappear from accounting.
    """
    events = []
    for line in text.splitlines():
        if line.lstrip().startswith("{"):
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Harness event must be an object")
            events.append(event)
    return events


def file_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def parse_transcript(events: list[dict]) -> dict:
    """Retain failed-run usage and reject ambiguous or incomplete accounting."""
    finals = [e for e in events if e.get("type") == "result"]
    distinct = {json.dumps(e, sort_keys=True) for e in finals}
    if len(distinct) > 1:
        raise ValueError("Multiple distinct terminal results; do not double-count or pick one")
    final = finals[-1] if finals else {}
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    models = final.get("modelUsage")
    fields = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
    complete = bool(models) and all(
        isinstance(row.get(key), int) and not isinstance(row[key], bool) and row[key] >= 0
        for row in models.values() for key in fields
    )
    totals = {key: sum(row[key] for row in models.values()) if complete else None for key in fields}
    tools: dict[str, dict] = {}
    results: dict[str, str] = {}
    result_errors: dict[str, bool] = {}
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                key = block["id"]
                if key in tools and tools[key] != block:
                    raise ValueError("Conflicting tool-use records")
                tools[key] = block
            elif block.get("type") == "tool_result":
                key = block["tool_use_id"]
                value = json.dumps(block.get("content"), ensure_ascii=False)
                is_error = bool(block.get("is_error"))
                if key in results and (results[key] != value or result_errors[key] != is_error):
                    raise ValueError("Conflicting tool-result records")
                results[key] = value
                result_errors[key] = is_error
    return {
        "model": init.get("model"), "harness_version": init.get("claude_code_version"),
        "terminal_status": final.get("subtype"), "is_error": final.get("is_error"),
        "usage_complete": complete, **totals,
        "total_tokens": sum(totals.values()) if complete else None,
        "reported_cost_usd": final.get("total_cost_usd"),
        "primary_usage": final.get("usage"), "model_usage": models,
        "tool_calls": len(tools), "tool_names": dict(Counter(t["name"] for t in tools.values())),
        "nonerror_tool_result_names": dict(Counter(
            tools[key]["name"] for key in results if key in tools and not result_errors[key]
        )),
        "error_tool_result_names": dict(Counter(
            tools[key]["name"] for key in results if key in tools and result_errors[key]
        )),
        "tool_result_utf8_bytes": sum(len(t.encode("utf-8")) for t in results.values()),
        "permission_denials": final.get("permission_denials"),
        "result": final.get("result"), "init": init,
        "structured_output": final.get("structured_output"),
    }


def parse_answer(text: str) -> dict:
    """Accept a single JSON answer, optionally wrapped in a markdown fence."""
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    elif text.startswith("```\n") and text.endswith("\n```"):
        text = text[4:-4]
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Answer must be an object")
    return result


def grade_citations(answer: dict, root: Path, requirements: list[dict]) -> dict:
    """Require bounded source evidence, with predeclared alternative witnesses.

    A requirement is either a path/anchor pair or an ``any_of`` list of pairs.
    Alternatives allow the same fact to be established at distinct code sites;
    they do not relax path, line, or evidence checks for the citation itself.
    """
    valid = []
    invalid = []
    citations = answer.get("citations", [])
    if not isinstance(citations, list):
        return {"valid_citations": 0, "invalid_citations": [citations],
                "missing_citations": requirements, "citations_pass": False}
    for citation in citations:
        if not isinstance(citation, dict) or not isinstance(citation.get("path"), str):
            invalid.append(citation)
            continue
        path = root / citation.get("path", "")
        start, end = citation.get("line_start"), citation.get("line_end")
        if (not path.resolve().is_relative_to(root.resolve()) or not path.is_file()
                or type(start) is not int or type(end) is not int):
            invalid.append(citation)
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not 1 <= start <= end <= len(lines) or end - start > 120:
            invalid.append(citation)
            continue
        valid.append((path.resolve().relative_to(root.resolve()).as_posix(), "\n".join(lines[start - 1:end])))
    def supported(requirement):
        alternatives = requirement.get("any_of", [requirement])
        return any(path == witness["path"] and witness["anchor"] in text
                   for witness in alternatives for path, text in valid)
    missing = [r for r in requirements if not supported(r)]
    return {"valid_citations": len(valid), "invalid_citations": invalid,
            "missing_citations": missing, "citations_pass": not invalid and not missing}


def paired_summary(rows: list[dict]) -> dict:
    """All attempts count; failed or unmeasured runs cannot disappear from cost."""
    groups = {arm: [r for r in rows if r["arm"] == arm] for arm in ("normal", "grag")}
    summary = {}
    for arm, runs in groups.items():
        successes = sum(bool(r["quality_pass"]) for r in runs)
        measured = bool(runs) and all(
            type(r.get("total_tokens")) is int and r["total_tokens"] >= 0
            and type(r.get("reported_cost_usd")) in (int, float)
            and math.isfinite(r["reported_cost_usd"]) and r["reported_cost_usd"] >= 0
            for r in runs
        )
        tokens = sum(r["total_tokens"] for r in runs) if measured else None
        cost = sum(r["reported_cost_usd"] for r in runs) if measured else None
        summary[arm] = {"attempts": len(runs), "successes": successes, "usage_complete": measured,
                        "total_tokens": tokens, "reported_cost_usd": cost,
                        "tokens_per_accepted_completion": tokens / successes if measured and successes else None,
                        "cost_per_accepted_completion": cost / successes if measured and successes else None}
    baseline, candidate = summary["normal"], summary["grag"]
    summary["token_difference_fraction"] = (candidate["total_tokens"] / baseline["total_tokens"] - 1
                                           if candidate["total_tokens"] is not None and baseline["total_tokens"] else None)
    return summary
