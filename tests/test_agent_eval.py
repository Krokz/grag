"""Prevent favorable-looking evaluation totals from masking failures or leaks."""
import pytest

from agent_eval import (
    grade_citations,
    paired_summary,
    parse_answer,
    parse_events,
    parse_transcript,
)


def test_usage_comes_from_one_terminal_result_including_auxiliary_models():
    final = {"type": "result", "subtype": "success", "total_cost_usd": 0.5,
             "modelUsage": {"main": {"inputTokens": 10, "cacheReadInputTokens": 20, "cacheCreationInputTokens": 30, "outputTokens": 40},
                            "helper": {"inputTokens": 1, "cacheReadInputTokens": 2, "cacheCreationInputTokens": 3, "outputTokens": 4}}}
    tool = {"type": "assistant", "message": {"usage": {"input_tokens": 9999}, "content": [{"type": "tool_use", "id": "one", "name": "Read", "input": {}}]}}
    result = parse_transcript([tool, tool, final, final])
    assert result["total_tokens"] == 110 and result["tool_calls"] == 1
    with pytest.raises(ValueError, match="terminal"):
        parse_transcript([final, {**final, "total_cost_usd": 1}])


def test_missing_usage_is_unknown_not_zero():
    assert parse_transcript([])["total_tokens"] is None
    partial = {"type": "result", "modelUsage": {"main": {"inputTokens": 1}}}
    assert not parse_transcript([partial])["usage_complete"]


def test_budget_failure_keeps_usage_despite_plain_text_permission_events():
    events = [{"type": "system", "subtype": "permission_denied", "message": "Tool denied"},
              {"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
               "total_cost_usd": 0.151,
               "modelUsage": {"haiku": {"inputTokens": 1, "cacheReadInputTokens": 2,
                                         "cacheCreationInputTokens": 3, "outputTokens": 4}}}]
    result = parse_transcript(events)
    assert result["total_tokens"] == 10 and result["reported_cost_usd"] == 0.151
    assert result["terminal_status"] == "error_max_budget_usd" and result["is_error"]


def test_failed_attempts_stay_in_cost_per_success():
    rows = [{"arm": arm, "quality_pass": ok, "total_tokens": tokens, "reported_cost_usd": tokens / 100}
            for arm, ok, tokens in [("normal", True, 100), ("grag", False, 70), ("grag", True, 80)]]
    summary = paired_summary(rows)
    assert summary["grag"]["tokens_per_accepted_completion"] == 150
    assert summary["token_difference_fraction"] == 0.5
    rows[-1]["total_tokens"] = None
    assert paired_summary(rows)["grag"]["total_tokens"] is None


def test_citations_need_bounded_real_lines_and_cannot_escape_workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("def repair():\n    return 1\n")
    (tmp_path / "gold.py").write_text("hidden answer\n")
    requirements = [{"path": "a.py", "anchor": "def repair():"}]
    good = {"citations": [{"path": "a.py", "line_start": 1, "line_end": 2}]}
    assert grade_citations(good, root, requirements)["citations_pass"]
    for path, start, end in [("../gold.py", 1, 1), ("a.py", 2, 2), ("a.py", 0, 2), ("a.py", True, 2)]:
        assert not grade_citations({"citations": [{"path": path, "line_start": start, "line_end": end}]}, root, requirements)["citations_pass"]


def test_answer_parser_does_not_salvage_facts_from_unstructured_prose():
    assert parse_answer('```json\n{"facts": {"found": false}}\n```')["facts"] == {"found": False}
    with pytest.raises(ValueError):
        parse_answer('Some prose {"facts": {"found": false}}')


@pytest.mark.parametrize("citations", [None, "a.py", [None], [{"path": []}]])
def test_malformed_citations_fail_without_aborting_the_cohort(tmp_path, citations):
    assert not grade_citations({"citations": citations}, tmp_path, [])['citations_pass']


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1, True])
def test_invalid_cost_is_not_a_measured_total(cost):
    row = {"arm": "normal", "quality_pass": True, "total_tokens": 20, "reported_cost_usd": cost}
    assert paired_summary([row])["normal"]["reported_cost_usd"] is None


def test_denied_mcp_request_is_not_counted_as_a_successful_graph_read():
    call = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "denied", "name": "mcp__grag__ingest_code", "input": {}}]}}
    reply = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "denied", "is_error": True, "content": "Permission denied"}]}}
    result = parse_transcript([call, reply, reply])
    assert result["tool_calls"] == 1
    assert result["error_tool_result_names"] == {"mcp__grag__ingest_code": 1}
    assert result["nonerror_tool_result_names"] == {}


def test_structured_output_is_retained_without_inventing_text_or_usage():
    answer = {"answer": "Recorded as complete", "facts": {"status": "done"}}
    parsed = parse_transcript([{"type": "result", "subtype": "success", "structured_output": answer}])
    assert parsed["structured_output"] == answer
    assert parsed["result"] is None
    assert parsed["total_tokens"] is None


def test_predeclared_alternate_code_site_can_establish_the_same_fact(tmp_path):
    (tmp_path / "helper.py").write_text("def reusable(key):\n    return cached[key]\n")
    (tmp_path / "caller.py").write_text("if stateful:\n    client = cached[key]\nelse:\n    client = Client()\n")
    requirement = {"any_of": [{"path": "helper.py", "anchor": "return cached[key]"},
                              {"path": "caller.py", "anchor": "client = cached[key]"}]}
    citation = {"path": "caller.py", "line_start": 1, "line_end": 4}
    assert grade_citations({"citations": [citation]}, tmp_path, [requirement])["citations_pass"]
    unrelated = {**citation, "line_start": 3}
    assert not grade_citations({"citations": [unrelated]}, tmp_path, [requirement])["citations_pass"]
    past_eof = {**citation, "line_end": 5}
    assert not grade_citations({"citations": [past_eof]}, tmp_path, [requirement])["citations_pass"]


def test_absolute_citation_inside_snapshot_matches_relative_gold(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("def known():\n    return True\n")
    answer = {"citations": [{"path": str(path), "line_start": 1, "line_end": 2}]}
    assert grade_citations(answer, tmp_path, [{"path": "module.py", "anchor": "def known():"}])["citations_pass"]


def test_indented_harness_events_retain_tool_failures():
    transcript = '''Diagnostic text
  {"type":"assistant","message":{"content":[{"type":"tool_use","id":"format","name":"StructuredOutput","input":{}}]}}
\t{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"format","is_error":true,"content":"Invalid schema"}]}}
'''
    result = parse_transcript(parse_events(transcript))
    assert result["tool_names"] == {"StructuredOutput": 1}
    assert result["error_tool_result_names"] == {"StructuredOutput": 1}
    with pytest.raises(ValueError):
        parse_events(' {"type": broken JSON')
