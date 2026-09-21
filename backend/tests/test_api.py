import dataclasses
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.demo.tasks import materialize
from app.llm import LLMError
from app.main import create_app
from .conftest import TEST_KEY, FakeLLM, turn, wait_run

FINISH = turn("", ("finish", {"summary": "ok"}))


def make_client(settings, factory=None):
    factory = factory or (lambda key, model, thinking="": FakeLLM([FINISH], api_key=key))
    return TestClient(create_app(settings, llm_factory=factory))


def demo_session(client, demo="pagination"):
    r = client.post("/api/sessions", json={"demo": demo})
    assert r.status_code == 201 and r.json()["status"] == "ready", r.text
    return r.json()["id"]


def run_demo(client, sid, mode="auto", task="Fix the pagination bug please", **extra):
    return client.post(f"/api/sessions/{sid}/runs", json={"task": task, "approval_mode": mode, **extra})


# --------------------------------------------------------------------------- no visitor key, server key stays private
class TestServerKey:
    def test_key_comes_from_the_environment_and_is_hidden(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", TEST_KEY)
        s = load_settings()
        assert s.gemini_api_key == TEST_KEY and TEST_KEY not in repr(s) and s.public_mode is True

    def test_never_appears_in_any_api_response(self, settings):
        with make_client(settings) as c:
            sid = demo_session(c)
            texts = [c.get("/api/health").text, c.get("/api/config").text, c.get("/api/ai/status").text, c.get("/api/sessions").text,
                     c.get(f"/api/sessions/{sid}").text, c.get("/").text]
            assert all(TEST_KEY not in t for t in texts)
            assert c.get("/api/config").json()["ai_available"] is True

    def test_there_are_no_key_or_token_endpoints_or_headers_left(self, settings):
        with make_client(settings) as c:
            assert c.post("/api/key/check", json={}).status_code in (404, 405)
            assert c.get("/api/sessions", headers={"Authorization": "Bearer nope"}).status_code == 200   # nothing to gate

    def test_used_server_side_but_never_persisted_or_echoed(self, settings):
        seen = []
        turns = [turn(f"debug: my key is {TEST_KEY}", ("finish", {"summary": f"done with {TEST_KEY}"}))]

        def factory(key, model, thinking=""):
            seen.append((key, model))
            return FakeLLM(list(turns), api_key=key)

        with make_client(settings, factory) as c:
            sid = demo_session(c)
            run = c.post(f"/api/sessions/{sid}/runs",
                         json={"task": "Fix the pagination bug", "mode": "live", "approval_mode": "auto"}).json()
            done = wait_run(c, run["id"])
            assert seen == [(TEST_KEY, settings.model)] and done["llm_kind"] == "live"
            everything = c.get(f"/api/runs/{run['id']}/events").text + c.get(f"/api/runs/{run['id']}").text + c.get("/api/sessions").text
            assert TEST_KEY not in everything and "[redacted]" in everything
        for f in Path(settings.data_dir).rglob("*"):          # database, WAL and workspaces
            if f.is_file():
                assert TEST_KEY.encode() not in f.read_bytes(), f


class TestAiAvailability:
    def test_without_a_server_key_demos_work_and_live_is_refused_politely(self, settings):
        with make_client(dataclasses.replace(settings, gemini_api_key="")) as c:
            assert c.get("/api/config").json()["ai_available"] is False
            assert c.get("/api/ai/status").json()["available"] is False
            sid = demo_session(c)
            r = c.post(f"/api/sessions/{sid}/runs", json={"task": "Fix the pagination bug", "mode": "live"})
            assert r.status_code == 503 and "not configured" in r.json()["detail"]
            assert wait_run(c, run_demo(c, sid).json()["id"])["tests_passed"] == 1

    def test_status_is_checked_once_and_cached(self, settings):
        calls = []

        class Probe:
            def check(self):
                calls.append(1)

        with make_client(settings, lambda k, m, t="": Probe()) as c:
            answers = [c.get("/api/ai/status").json() for _ in range(5)]
            assert all(a == {"available": True, "model": settings.model} for a in answers) and len(calls) == 1

    def test_a_dead_key_is_reported_without_leaking_it(self, settings):
        class Dead:
            def check(self):
                raise LLMError("The AI service rejected this server's API key.", "auth")

        with make_client(settings, lambda k, m, t="": Dead()) as c:
            r = c.get("/api/ai/status").json()
            assert r["available"] is False and "rejected" in r["reason"] and TEST_KEY not in str(r)


# --------------------------------------------------------------------------- visitors can't touch each other's work
class TestIsolation:
    def test_another_visitor_cannot_see_or_control_your_run(self, settings):
        with make_client(settings) as a:
            b = TestClient(a.app)
            sid = demo_session(a)
            run = a.post(f"/api/sessions/{sid}/runs", json={"task": "Fix the pagination bug", "approval_mode": "ask"}).json()
            time.sleep(0.8)   # parked at the first approval gate
            aid = a.get(f"/api/runs/{run['id']}/approvals").json()[0]["id"]

            assert b.get("/api/sessions").json() == []
            for method, url, body in [
                ("get", f"/api/sessions/{sid}", None), ("delete", f"/api/sessions/{sid}", None),
                ("get", f"/api/sessions/{sid}/search?q=page", None), ("post", f"/api/sessions/{sid}/runs", {"task": "Steal this run"}),
                ("get", f"/api/runs/{run['id']}", None), ("get", f"/api/runs/{run['id']}/events", None),
                ("get", f"/api/runs/{run['id']}/approvals", None), ("get", f"/api/runs/{run['id']}/diff", None),
                ("get", f"/api/runs/{run['id']}/patch", None), ("post", f"/api/runs/{run['id']}/cancel", None),
                ("post", f"/api/runs/{run['id']}/pr", {"push": False}), ("post", f"/api/approvals/{aid}", {"approved": True}),
            ]:
                r = getattr(b, method)(url, **({"json": body} if body is not None else {}))
                assert r.status_code == 404, (method, url, r.status_code)

            assert a.post(f"/api/approvals/{aid}", json={"approved": True}).status_code == 200   # the owner still can
            a.post(f"/api/runs/{run['id']}/cancel")
            wait_run(a, run["id"], approve=False)

    def test_cookie_is_private_and_never_echoed(self, settings):
        with make_client(settings) as c:
            r = c.get("/")
            cookie = r.headers["set-cookie"]
            assert "rp_owner=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
            owner = c.cookies.get("rp_owner")
            sid = demo_session(c)
            everything = c.get("/api/sessions").text + c.get(f"/api/sessions/{sid}").text + c.get("/api/config").text
            assert len(owner) == 32 and owner not in everything

    def test_a_forged_cookie_is_replaced_not_trusted(self, settings):
        with make_client(settings) as c:
            c.cookies.set("rp_owner", "../../admin")
            r = c.get("/api/sessions")
            assert r.status_code == 200 and "rp_owner=" in r.headers["set-cookie"] and "admin" not in r.headers["set-cookie"]

    def test_each_visitor_gets_their_own_session_allowance(self, settings):
        with make_client(dataclasses.replace(settings, sessions_per_owner=2)) as a:
            b = TestClient(a.app)
            demo_session(a), demo_session(a)
            r = a.post("/api/sessions", json={"demo": "slugify"})
            assert r.status_code == 429 and "2 repositories" in r.json()["detail"]
            demo_session(b)   # someone else is unaffected


# --------------------------------------------------------------------------- abuse limits on a public server
class TestAbuseLimits:
    def test_server_wide_capacity(self, settings):
        with make_client(dataclasses.replace(settings, max_sessions=1)) as a:
            demo_session(a)
            r = TestClient(a.app).post("/api/sessions", json={"demo": "slugify"})
            assert r.status_code == 429 and "capacity" in r.json()["detail"]

    def test_hourly_live_run_limit_per_ip_but_demos_are_not_charged_against_it(self, settings):
        with make_client(dataclasses.replace(settings, runs_per_ip_per_hour=2)) as c:
            sid = demo_session(c)
            live = lambda: c.post(f"/api/sessions/{sid}/runs", json={"task": "Fix the pagination bug", "mode": "live", "approval_mode": "auto"})
            for _ in range(2):
                r = live()
                assert r.status_code == 201
                wait_run(c, r.json()["id"])
            blocked = live()
            assert blocked.status_code == 429 and "hourly limit" in blocked.json()["detail"]
            assert run_demo(c, sid).status_code == 201     # scripted demos cost nothing

    def test_forwarded_ip_header_is_ignored_unless_you_run_behind_a_proxy(self, settings):
        def two_runs(trust):
            with make_client(dataclasses.replace(settings, runs_per_ip_per_hour=1, trust_proxy=trust)) as c:
                sid = demo_session(c)
                codes = []
                for ip in ("1.1.1.1", "2.2.2.2"):
                    r = c.post(f"/api/sessions/{sid}/runs", headers={"X-Forwarded-For": ip},
                               json={"task": "Fix the pagination bug", "mode": "live", "approval_mode": "auto"})
                    codes.append(r.status_code)
                    if r.status_code == 201:
                        wait_run(c, r.json()["id"])
                return codes
        assert two_runs(False) == [201, 429]      # spoofing the header gets you nothing
        assert two_runs(True) == [201, 201]       # behind a trusted proxy, real client IPs are used

    def test_daily_spend_cap_stops_live_runs_but_not_demos(self, settings):
        s = dataclasses.replace(settings, daily_cost_cap_usd=0.30, max_cost_usd=0.25)
        spender = lambda k, m, t="": FakeLLM([turn("", ("finish", {"summary": "ok"}), tin=100_000, tout=0)], api_key=k)   # ~$0.15
        with make_client(s, spender) as c:
            sid = demo_session(c)
            r = c.post(f"/api/sessions/{sid}/runs", json={"task": "Fix the pagination bug", "mode": "live", "approval_mode": "auto"})
            assert r.status_code == 201
            assert wait_run(c, r.json()["id"])["cost_usd"] == pytest.approx(0.15, abs=0.01)
            r2 = c.post(f"/api/sessions/{sid}/runs", json={"task": "Fix the pagination bug", "mode": "live", "approval_mode": "auto"})
            assert r2.status_code == 429 and "daily" in r2.json()["detail"]
            assert run_demo(c, sid).status_code == 201

    def test_old_workspaces_are_cleaned_up(self, settings):
        with make_client(settings) as c:
            sid = demo_session(c)
            ws = Path(settings.workspace_dir) / sid
            runner = c.app.state.runner
            assert runner.sweep() == 0 and ws.exists()                                   # fresh: kept
            assert runner.sweep(now=time.time() + settings.session_ttl_hours * 3600 + 60) == 1
            assert not ws.exists() and c.get("/api/sessions").json() == []


# --------------------------------------------------------------------------- repo intake
class TestRepoIntake:
    @pytest.mark.parametrize("value,fragment", [
        ("AIza" + "x" * 35, "API key or token"), ("sk-proj-abcdefghijklmnop", "API key or token"), ("ghp_" + "a" * 30, "API key or token"),
        ("http://github.com/o/r", "https"), ("https://evil.example/o/r", "not allowed"), ("https://github.com/only", "owner"),
    ])
    def test_rejects_keys_and_bad_links(self, settings, value, fragment):
        with make_client(settings) as c:
            r = c.post("/api/sessions", json={"repo_url": value})
            assert r.status_code == 422 and fragment in r.json()["detail"] and c.get("/api/sessions").json() == []

    def test_exactly_one_source(self, settings):
        with make_client(settings) as c:
            assert c.post("/api/sessions", json={}).status_code == 422
            assert c.post("/api/sessions", json={"demo": "pagination", "repo_url": "https://github.com/o/r"}).status_code == 422

    def test_local_paths_disabled_by_default(self, settings, tmp_path):
        with make_client(settings) as c:
            assert c.post("/api/sessions", json={"local_path": str(tmp_path)}).status_code == 403


# --------------------------------------------------------------------------- the approval workflow itself
class TestDemoRuns:
    def test_ask_mode_full_flow_with_approvals(self, settings):
        with make_client(settings) as c:
            sid = demo_session(c)
            run = run_demo(c, sid, mode="ask").json()
            assert run["llm_kind"] == "simulated"
            done = wait_run(c, run["id"])
            assert done["status"] == "completed" and done["tests_passed"] == 1 and done["files_changed"] == 1
            assert [a["tool"] for a in c.get(f"/api/runs/{run['id']}/approvals").json()] == ["run_tests", "replace_in_file", "run_tests"]
            d = c.get(f"/api/runs/{run['id']}/diff").json()
            assert d["files"] == [{"path": "shop/pagination.py", "added": 1, "removed": 1}]
            patch = c.get(f"/api/runs/{run['id']}/patch")
            assert "attachment" in patch.headers["content-disposition"] and "(page - 1)" in patch.text

    def test_rejecting_everything_leaves_no_changes(self, settings):
        with make_client(settings) as c:
            run = run_demo(c, demo_session(c), mode="ask").json()
            done = wait_run(c, run["id"], approve=False)
            assert done["files_changed"] == 0 and done["tests_passed"] is None
            assert c.get(f"/api/runs/{run['id']}/patch").status_code == 404

    def test_auto_mode_needs_no_clicks_and_can_make_a_branch(self, settings):
        with make_client(settings) as c:
            run = run_demo(c, demo_session(c, "discounts"), task="Fix the discount maths please").json()
            done = wait_run(c, run["id"], approve=False)
            assert done["status"] == "completed" and done["tests_passed"] == 1 and c.get(f"/api/runs/{run['id']}/approvals").json() == []
            branch = c.post(f"/api/runs/{run['id']}/pr", json={"push": False}).json()
            assert branch["branch"].startswith("repopilot/") and branch["commit"] and not branch["pushed"]
            assert c.post(f"/api/runs/{run['id']}/pr", json={"push": True}, headers={"X-GitHub-Token": "ghp_" + "x" * 30}).status_code == 422

    def test_second_run_starts_from_a_clean_baseline(self, settings):
        with make_client(settings) as c:
            sid = demo_session(c)
            for _ in range(2):
                assert wait_run(c, run_demo(c, sid).json()["id"])["files_changed"] == 1

    def test_only_one_active_run_per_repo_and_cancel(self, settings):
        with make_client(settings) as c:
            sid = demo_session(c)
            run = run_demo(c, sid, mode="ask").json()
            time.sleep(0.8)
            assert c.post(f"/api/sessions/{sid}/runs", json={"task": "Another task here"}).status_code == 409
            assert c.post(f"/api/runs/{run['id']}/cancel").status_code == 200
            assert wait_run(c, run["id"], approve=False)["status"] == "cancelled"

    def test_input_validation(self, settings):
        with make_client(settings) as c:
            sid = demo_session(c)
            assert c.post(f"/api/sessions/{sid}/runs", json={"task": "x"}).status_code == 422
            assert c.post(f"/api/sessions/{sid}/runs", json={"task": "Fix the bug", "approval_mode": "yolo"}).status_code == 422
            assert c.post("/api/sessions/nope/runs", json={"task": "Fix the bug"}).status_code == 404

    def test_double_decision_is_a_conflict(self, settings):
        with make_client(settings) as c:
            run = run_demo(c, demo_session(c), mode="ask").json()
            time.sleep(0.8)
            aid = c.get(f"/api/runs/{run['id']}/approvals").json()[0]["id"]
            assert c.post(f"/api/approvals/{aid}", json={"approved": True}).status_code == 200
            assert c.post(f"/api/approvals/{aid}", json={"approved": True}).status_code == 409
            c.post(f"/api/runs/{run['id']}/cancel")
            wait_run(c, run["id"], approve=False)


class TestPublicModeAndHardening:
    def test_public_mode_disables_tests_for_non_demo_repos_only(self, settings, tmp_path):
        src = tmp_path / "userrepo"
        materialize("pagination", src)
        s = dataclasses.replace(settings, public_mode=True, allow_local_paths=True)
        turns = [turn("", ("run_tests", {})), turn("", ("finish", {"summary": "unverified"}))]
        with make_client(s, lambda k, m, t="": FakeLLM(list(turns), api_key=k)) as c:
            sid = c.post("/api/sessions", json={"local_path": str(src)}).json()["id"]
            run = c.post(f"/api/sessions/{sid}/runs", json={"task": "Fix the pagination bug", "approval_mode": "auto"}).json()
            wait_run(c, run["id"])
            out = " ".join(e["data"].get("output", "") for e in c.get(f"/api/runs/{run['id']}/events").json()["events"] if e["kind"] == "tool_result")
            assert "disabled" in out
            assert c.post(f"/api/sessions/{sid}/runs", json={"task": "Fix the bug", "mode": "simulated"}).status_code == 422
            assert wait_run(c, run_demo(c, demo_session(c)).json()["id"])["tests_passed"] == 1   # demos still verify with tests

    def test_security_headers_and_frontend(self, settings):
        with make_client(settings) as c:
            r = c.get("/")
            assert r.status_code == 200 and "RepoPilot" in r.text and "api key" not in r.text.lower().replace("api key or token", "")
            assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
            assert c.get("/api/health").headers["cache-control"] == "no-store"

    def test_delete_session_removes_files(self, settings):
        with make_client(settings) as c:
            sid = demo_session(c)
            ws = Path(settings.workspace_dir) / sid
            assert ws.exists() and c.delete(f"/api/sessions/{sid}").status_code == 204
            assert not ws.exists() and c.get(f"/api/sessions/{sid}").status_code == 404

    def test_runs_left_active_by_a_crash_are_marked_interrupted(self, settings):
        with make_client(settings) as c:
            sid = demo_session(c)
            rid = c.app.state.db.create_run(sid, "some task", "ask", "live", "m")["id"]
        with make_client(settings) as c2:
            assert c2.app.state.db.get_run(rid)["status"] == "interrupted"

    def test_missing_workspace_after_storage_reset_is_reported(self, settings):
        import shutil
        with make_client(settings) as c:
            sid = demo_session(c)
            cookie = c.cookies.get("rp_owner")
        shutil.rmtree(Path(settings.workspace_dir) / sid)
        with make_client(settings) as c2:
            c2.cookies.set("rp_owner", cookie)
            s = c2.get(f"/api/sessions/{sid}").json()
            assert s["status"] == "error" and "Add the repository again" in s["error"]
