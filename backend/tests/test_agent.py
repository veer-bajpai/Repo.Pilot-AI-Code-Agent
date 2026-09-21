import dataclasses

import pytest

from app.agent import Agent
from app.llm import LLMError
from app.tools import ToolBox
from .conftest import FakeLLM, turn

FIX = ("replace_in_file", {"path": "shop/pagination.py", "old_string": "start = page * per_page",
                          "new_string": "start = (page - 1) * per_page", "confidence": 0.95, "reason": "off by one"})
FINISH = ("finish", {"summary": "done"})


def run_agent(demo_repo, box_factory, settings, turns, *, mode="ask", approve=True, note="", cancel=lambda: False, **over):
    repo, base = demo_repo
    box = box_factory(repo, base)
    events, gates = [], []
    llm = FakeLLM(turns)
    s = dataclasses.replace(settings, **over) if over else settings

    def gate(action, args, reason):
        gates.append((action.name, action.target, reason))
        return approve, note

    agent = Agent(llm=llm, toolbox=box, settings=s, task="fix pagination", mode=mode, repo_name="demo",
                  emit=lambda k, d: events.append((k, d)), gate=gate, is_cancelled=cancel)
    return agent.run(), llm, events, gates, box


def test_ask_mode_gates_every_edit_and_test(demo_repo, box_factory, settings):
    res, llm, ev, gates, box = run_agent(demo_repo, box_factory, settings,
                                         [turn("", ("run_tests", {})), turn("", FIX), turn("", ("run_tests", {})), turn("", FINISH)])
    assert res.status == "completed" and res.summary == "done"
    assert [g[0] for g in gates] == ["run_tests", "replace_in_file", "run_tests"]
    assert box.last_tests_passed is True and res.steps == 4 and res.cost_usd > 0


def test_auto_mode_skips_gate_for_confident_edit_and_tests(demo_repo, box_factory, settings):
    res, _, ev, gates, _ = run_agent(demo_repo, box_factory, settings, [turn("", FIX), turn("", ("run_tests", {})), turn("", FINISH)], mode="auto")
    assert gates == [] and res.status == "completed"
    assert [k for k, _ in ev].count("auto_approved") == 2


def test_auto_mode_still_asks_when_unsure_or_protected(demo_repo, box_factory, settings):
    unsure = ("replace_in_file", {**FIX[1], "confidence": 0.4})
    protected = ("write_file", {"path": "requirements.txt", "content": "x\n", "confidence": 0.99, "reason": "deps"})
    _, _, _, gates, _ = run_agent(demo_repo, box_factory, settings, [turn("", unsure), turn("", protected), turn("", FINISH)], mode="auto")
    assert [g[1] for g in gates] == ["shop/pagination.py", "requirements.txt"]


def test_rejection_reaches_the_model_and_changes_nothing(demo_repo, box_factory, settings):
    res, llm, _, _, box = run_agent(demo_repo, box_factory, settings, [turn("", FIX), turn("", FINISH)], approve=False, note="use a different approach")
    assert box.files_changed() == 0
    tool_result = llm.seen[1][-1]["content"][0]["content"]
    assert "REJECTED" in tool_result and "different approach" in tool_result


@pytest.mark.parametrize("call", [
    ("write_file", {"path": ".env", "content": "x", "confidence": 1, "reason": "r"}),
    ("replace_in_file", {"path": "../x.py", "old_string": "a", "new_string": "b", "confidence": 1, "reason": "r"}),
    ("run_tests", {"command": "rm -rf /"}),
    ("read_file", {"path": "/etc/passwd"}),
])
def test_refusals_never_reach_a_human_and_the_run_continues(demo_repo, box_factory, settings, call):
    res, llm, ev, gates, _ = run_agent(demo_repo, box_factory, settings, [turn("", call), turn("", FINISH)], mode="auto")
    assert gates == [] and res.status == "completed"
    assert "refused" in [k for k, _ in ev]
    assert llm.seen[1][-1]["content"][0]["content"].startswith("REFUSED")


def test_recoverable_tool_error_is_reported_to_model(demo_repo, box_factory, settings):
    bad = ("replace_in_file", {**FIX[1], "old_string": "not in file"})
    res, llm, _, gates, _ = run_agent(demo_repo, box_factory, settings, [turn("", bad), turn("", FINISH)])
    assert gates == [] and llm.seen[1][-1]["content"][0]["content"].startswith("ERROR")


def test_every_tool_use_gets_a_result_even_alongside_finish(demo_repo, box_factory, settings):
    res, llm, *_ = run_agent(demo_repo, box_factory, settings, [turn("", ("search_code", {"query": "page"}), FINISH, ("list_files", {}))], mode="auto")
    assert res.status == "completed"


def test_step_budget(demo_repo, box_factory, settings):
    loop = [turn("", ("list_files", {})) for _ in range(10)]
    res, *_ = run_agent(demo_repo, box_factory, settings, loop, max_steps=3)
    assert res.status == "budget_exceeded" and res.steps == 3


def test_cost_budget(demo_repo, box_factory, settings):
    loop = [turn("", ("list_files", {}), tin=1_000_000, tout=100_000) for _ in range(5)]
    res, *_ = run_agent(demo_repo, box_factory, settings, loop, max_cost_usd=0.10)
    assert res.status == "budget_exceeded" and "Cost budget" in res.error


def test_model_error_fails_cleanly(demo_repo, box_factory, settings):
    res, *_ = run_agent(demo_repo, box_factory, settings, [LLMError("The AI service rejected this server's API key.", "auth")])
    assert res.status == "failed" and "rejected" in res.error


def test_model_that_never_finishes(demo_repo, box_factory, settings):
    res, *_ = run_agent(demo_repo, box_factory, settings, [turn("thinking"), turn("still thinking"), turn("nope")])
    assert res.status == "failed" and "finish" in res.error


def test_cancel(demo_repo, box_factory, settings):
    res, *_ = run_agent(demo_repo, box_factory, settings, [turn("", ("list_files", {}))] * 5, cancel=lambda: True)
    assert res.status == "cancelled"


def test_old_tool_output_is_compacted():
    big = "x" * 5000
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": big}]} for i in range(10)]
    Agent._compact(msgs)
    assert "elided" in msgs[0]["content"][0]["content"] and msgs[-1]["content"][0]["content"] == big
