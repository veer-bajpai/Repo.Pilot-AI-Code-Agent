from __future__ import annotations

import copy
import dataclasses
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.demo.tasks import materialize
from app.indexer import CodeIndex
from app.llm import LLMResponse, ToolCall
from app.main import create_app
from app.tools import ToolBox

# Built from pieces so the secrets scanner never sees a key-shaped literal in the source.
TEST_KEY = "AIza" + "T" * 35


@pytest.fixture
def settings(tmp_path):
    base = load_settings()
    return dataclasses.replace(
        base, data_dir=tmp_path / "data", workspace_dir=tmp_path / "data" / "workspaces",
        database_path=tmp_path / "data" / "repopilot.db", test_timeout_s=30, gemini_api_key=TEST_KEY, public_mode=False,
        allow_local_paths=False, cors_origins=(), runs_per_ip_per_hour=1000, daily_cost_cap_usd=100.0, sessions_per_owner=50,
        max_sessions=500)


@pytest.fixture
def demo_repo(tmp_path):
    dest = tmp_path / "repo"
    base = materialize("pagination", dest)
    return dest, base


@pytest.fixture
def box_factory(settings):
    def make(repo: Path, base: str, **overrides):
        s = dataclasses.replace(settings, **overrides) if overrides else settings
        return ToolBox(repo, CodeIndex(repo), s, base, "pytest -q", True)
    return make


class FakeLLM:
    """Replays a list of LLMResponse objects and records what it was sent."""
    simulated = False
    model = "fake-model"

    def __init__(self, turns, api_key=None):
        self.turns, self.i, self.seen = list(turns), 0, []
        self._api_key = api_key

    def complete(self, system, messages, tools):
        self.seen.append(copy.deepcopy(messages))
        if self.i >= len(self.turns):
            return LLMResponse(text="done", tool_calls=[], stop_reason="end_turn")
        turn = self.turns[self.i]
        self.i += 1
        if isinstance(turn, Exception):
            raise turn
        return turn

    def check(self):
        return None


def turn(text="", *calls, tin=100, tout=50):
    return LLMResponse(text=text, tool_calls=[ToolCall(id=f"call_{n}_{i}", name=name, input=args)
                                              for i, (name, args) in enumerate(calls, 1) for n in [id(calls) % 10000]],
                       input_tokens=tin, output_tokens=tout, stop_reason="tool_use" if calls else "end_turn")


@pytest.fixture
def client(settings):
    app = create_app(settings, llm_factory=lambda key, model, thinking="": FakeLLM([], api_key=key))
    with TestClient(app) as c:
        c.app_state = app.state
        yield c


def wait_run(client, run_id, *, approve=True, timeout=30):
    """Poll a run to completion, optionally approving every gate."""
    end = time.time() + timeout
    while time.time() < end:
        for a in client.get(f"/api/runs/{run_id}/approvals").json():
            if a["status"] == "pending":
                client.post(f"/api/approvals/{a['id']}", json={"approved": approve, "note": ""})
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("running", "awaiting_approval"):
            return run
        time.sleep(0.1)
    raise AssertionError("run did not finish in time: " + str(client.get(f"/api/runs/{run_id}").json()))
