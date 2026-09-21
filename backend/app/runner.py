"""Sessions, background runs, the approval broker and the branch / PR flow.

Secret handling: the model key reaches this module only inside an `llm` object
built by the API layer for one run. It is never stored in the database, in events,
or in module state, and the reference is dropped when the run's thread ends.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .agent import Agent, _summarize_args
from .config import Settings
from .db import Database
from .demo.tasks import DEMOS, materialize
from .indexer import CodeIndex, analyze_repo
from .secrets_util import redact
from .tools import Action, ToolBox


class RunnerError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    note: str = ""


def _redact_data(value, secret: str | None):
    if isinstance(value, str):
        return redact(value, secret)
    if isinstance(value, dict):
        return {k: _redact_data(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_data(v, secret) for v in value]
    return value


class Runner:
    def __init__(self, settings: Settings, db: Database, start_janitor: bool = False):
        self.settings = settings
        self.db = db
        self._lock = threading.RLock()
        self._cancel: dict[str, threading.Event] = {}
        self._waiters: dict[str, _Waiter] = {}
        self._session_run: dict[str, str] = {}
        settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        db.mark_interrupted()
        self._validate_sessions()
        if start_janitor:
            threading.Thread(target=self._janitor_loop, daemon=True).start()

    # ------------------------------------------------------------------ housekeeping
    def sweep(self, now: float | None = None) -> int:
        """Delete sessions older than the TTL (and their files). Returns how many were removed."""
        cutoff = (now or time.time()) - self.settings.session_ttl_hours * 3600
        removed = 0
        for sid in self.db.expired_session_ids(cutoff):
            if sid in self._session_run:
                continue
            try:
                gitops.force_rmtree(self.settings.workspace_dir / sid)
                self.db.delete_session(sid)
                removed += 1
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        return removed

    def _janitor_loop(self) -> None:  # pragma: no cover - timing loop
        while True:
            time.sleep(600)
            try:
                self.sweep()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def check_live_budget(self) -> None:
        """Refuse live runs once the rolling 24h spend estimate would pass the daily cap."""
        cap = self.settings.daily_cost_cap_usd
        if cap <= 0:
            return
        spent = self.db.live_cost_since(time.time() - 86400)
        in_flight = sum(1 for r in self.db.active_runs())
        if spent + (in_flight + 1) * self.settings.max_cost_usd > cap:
            raise RunnerError("The daily free-usage limit for the live AI has been reached. Try again tomorrow, "
                              "or use the built-in demos, which don't use the AI.", 429)
    # ------------------------------------------------------------------ sessions
    def repo_dir(self, session_id: str) -> Path:
        return self.settings.workspace_dir / session_id / "repo"

    def _validate_sessions(self) -> None:
        for s in self.db.list_sessions():
            if s["status"] == "cloning":
                self.db.update_session(s["id"], status="error", error="Server restarted while cloning. Add the repository again.")
            elif s["status"] == "ready" and not (self.repo_dir(s["id"]) / ".git").exists():
                self.db.update_session(s["id"], status="error", error="Workspace files are gone (the server storage was reset). Add the repository again.")

    def _check_capacity(self, owner: str) -> None:
        if self.db.count_sessions(owner) >= self.settings.sessions_per_owner:
            raise RunnerError(f"You can keep {self.settings.sessions_per_owner} repositories at a time. Remove one first.", 429)
        if self.db.count_sessions() >= self.settings.max_sessions:
            raise RunnerError("The server is at capacity right now. Try again in a little while.", 429)

    def add_repo(self, raw_url: str, owner: str) -> dict:
        if not gitops.git_available():
            raise RunnerError("git is not installed on the server. Install git and restart RepoPilot.", 503)
        try:
            clone_url, host, gh_owner, repo = gitops.normalize_repo_url(raw_url, self.settings.allowed_git_hosts)
        except ValueError as exc:
            raise RunnerError(str(exc), 422) from None
        for s in self.db.list_sessions(owner):
            if s["source"] == clone_url and s["status"] in ("cloning", "ready"):
                return s
        self._check_capacity(owner)
        session = self.db.create_session(clone_url, f"{gh_owner}/{repo}", "repo", owner)
        info = {"host": host, "owner": gh_owner, "repo": repo}
        threading.Thread(target=self._clone_worker, args=(session["id"], clone_url, info), daemon=True).start()
        return session

    def _clone_worker(self, sid: str, clone_url: str, info: dict) -> None:
        dest = self.repo_dir(sid)
        try:
            branch = gitops.clone(clone_url, dest, max_bytes=self.settings.max_repo_mb * 1024 * 1024)
            self._finish_prepare(sid, dest, {**info, "default_branch": branch})
        except gitops.GitError as exc:
            self.db.update_session(sid, status="error", error=redact(str(exc)))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.db.update_session(sid, status="error", error=f"Unexpected error while cloning: {redact(str(exc))[:200]}")

    def _finish_prepare(self, sid: str, dest: Path, extra: dict) -> None:
        base = gitops.head_commit(dest)
        analysis = {**analyze_repo(dest), **extra}
        self.db.update_session(sid, status="ready", base_commit=base, analysis=analysis, error=None)

    def add_demo(self, demo_id: str, owner: str) -> dict:
        if demo_id not in DEMOS:
            raise RunnerError(f"Unknown demo {demo_id!r}", 404)
        if not gitops.git_available():
            raise RunnerError("git is not installed on the server. Install git and restart RepoPilot.", 503)
        self._check_capacity(owner)
        session = self.db.create_session(f"demo:{demo_id}", f"demo/{demo_id}", "demo", owner)
        dest = self.repo_dir(session["id"])
        try:
            materialize(demo_id, dest)
            self._finish_prepare(session["id"], dest, {"demo_id": demo_id, "default_branch": "main"})
        except Exception as exc:  # noqa: BLE001
            self.db.update_session(session["id"], status="error", error=redact(str(exc)))
        return self.db.get_session(session["id"])  # type: ignore[return-value]

    def add_local(self, path: str, owner: str) -> dict:
        if not self.settings.allow_local_paths:
            raise RunnerError("Local folders are disabled. Set ALLOW_LOCAL_PATHS=true (development only).", 403)
        src = Path(path).expanduser().resolve()
        if not src.is_dir():
            raise RunnerError(f"{path!r} is not a folder", 422)
        self._check_capacity(owner)
        session = self.db.create_session(str(src), src.name, "local", owner)
        dest = self.repo_dir(session["id"])
        try:
            gitops.force_rmtree(dest)
            if (src / ".git").exists():
                gitops.run_git(["clone", "-q", "--no-hardlinks", str(src), str(dest)])
            else:
                shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__"))
                gitops.run_git(["init", "-q"], cwd=dest)
                gitops.run_git(["add", "-A"], cwd=dest)
                gitops.run_git([*gitops.IDENT, "commit", "-q", "-m", "Initial snapshot"], cwd=dest)
            gitops.prepare_workspace(dest)
            self._finish_prepare(session["id"], dest, {"default_branch": "main"})
        except Exception as exc:  # noqa: BLE001
            self.db.update_session(session["id"], status="error", error=redact(str(exc))[:300])
        return self.db.get_session(session["id"])  # type: ignore[return-value]

    def delete_session(self, sid: str) -> None:
        if self.db.get_session(sid) is None:
            raise RunnerError("Session not found", 404)
        rid = self._session_run.get(sid)
        if rid:
            self.cancel(rid)
            for _ in range(30):
                if sid not in self._session_run:
                    break
                time.sleep(0.1)
        gitops.force_rmtree(self.settings.workspace_dir / sid)
        self.db.delete_session(sid)

    def search(self, sid: str, query: str) -> list[dict]:
        session = self._ready_session(sid)
        return CodeIndex(self.repo_dir(session["id"])).search(query, 8)

    def _ready_session(self, sid: str) -> dict:
        session = self.db.get_session(sid)
        if not session:
            raise RunnerError("Session not found", 404)
        if session["status"] != "ready":
            raise RunnerError(session.get("error") or "Repository is not ready yet", 409)
        return session

    # ------------------------------------------------------------------ runs
    def start_run(self, sid: str, task: str, mode: str, llm, llm_kind: str) -> dict:
        session = self._ready_session(sid)
        task = (task or "").strip()
        if len(task) < 5:
            raise RunnerError("Describe the task in a sentence or two.", 422)
        if len(task) > 4000:
            raise RunnerError("The task description is too long (4000 characters max).", 422)
        if mode not in ("ask", "auto"):
            raise RunnerError("approval_mode must be 'ask' or 'auto'", 422)
        if llm_kind == "live":
            self.check_live_budget()
        with self._lock:
            if sid in self._session_run:
                raise RunnerError("A run is already active for this repository.", 409)
            if len(self._session_run) >= self.settings.max_concurrent_runs:
                raise RunnerError("The server is busy with other runs. Try again shortly.", 429)
            run = self.db.create_run(sid, task, mode, llm_kind, getattr(llm, "model", None))
            self._session_run[sid] = run["id"]
            self._cancel[run["id"]] = threading.Event()
        allow_tests = (not self.settings.public_mode) or session["kind"] == "demo"
        threading.Thread(target=self._run_thread, args=(run["id"], session, task, mode, llm, allow_tests), daemon=True).start()
        return run

    def _emit(self, run_id: str, kind: str, data: dict, secret: str | None) -> None:
        self.db.add_event(run_id, kind, _redact_data(data, secret))
        if kind == "usage":   # keep spend visible to the daily cap while a run is still going
            self.db.update_run(run_id, steps=data["steps"], tokens_in=data["tokens_in"], tokens_out=data["tokens_out"],
                               cost_usd=data["cost_usd"], files_changed=data["files_changed"], lines_changed=data["lines_changed"])

    def _run_thread(self, run_id: str, session: dict, task: str, mode: str, llm, allow_tests: bool) -> None:
        sid = session["id"]
        secret = self.settings.gemini_api_key or None
        emit = lambda kind, data: self._emit(run_id, kind, data, secret)  # noqa: E731
        repo = self.repo_dir(sid)
        try:
            gitops.reset_to(repo, session["base_commit"])
            box = ToolBox(repo, CodeIndex(repo), self.settings, session["base_commit"],
                          (session.get("analysis") or {}).get("test_command"), allow_tests)
            emit("run_started", {"task": task, "mode": mode, "simulated": bool(getattr(llm, "simulated", False)),
                                 "model": getattr(llm, "model", None), "tests_enabled": allow_tests,
                                 "test_command": box.default_test_command})
            agent = Agent(llm=llm, toolbox=box, settings=self.settings, task=task, mode=mode, repo_name=session["name"],
                          emit=emit, gate=lambda a, args, why: self._gate(run_id, a, args, why, emit),
                          is_cancelled=lambda: self._cancel[run_id].is_set())
            result = agent.run()
            diff = gitops.diff_since(repo, session["base_commit"])
            self.db.update_run(
                run_id, status=result.status, summary=redact(result.summary, secret), error=redact(result.error or "", secret) or None,
                steps=result.steps, tokens_in=result.tokens_in, tokens_out=result.tokens_out, cost_usd=result.cost_usd,
                files_changed=box.files_changed(), lines_changed=box.lines_changed(),
                tests_passed=None if box.last_tests_passed is None else int(box.last_tests_passed),
                diff=diff, finished_at=time.time())
            emit("run_finished", {"status": result.status, "summary": result.summary, "error": result.error,
                                  "tests_passed": box.last_tests_passed, **agent._usage()})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            msg = redact(f"{type(exc).__name__}: {exc}", secret)[:300]
            self.db.update_run(run_id, status="failed", error=f"Internal error: {msg}", finished_at=time.time())
            emit("run_finished", {"status": "failed", "error": f"Internal error: {msg}"})
        finally:
            llm = None  # drop the only reference to the key held by this run
            with self._lock:
                self._session_run.pop(sid, None)
                self._cancel.pop(run_id, None)

    # ------------------------------------------------------------------ approvals
    def _gate(self, run_id: str, action: Action, args: dict, reason: str, emit) -> tuple[bool, str]:
        approval = self.db.create_approval(run_id, action.name, _summarize_args(args), action.preview, reason)
        aid = approval["id"]
        waiter = _Waiter()
        self._waiters[aid] = waiter
        self.db.update_run(run_id, status="awaiting_approval")
        emit("approval_requested", {"approval_id": aid, "tool": action.name, "target": action.target,
                                    "preview": action.preview[:6000], "reason": reason,
                                    "protected": action.protected, "confidence": action.confidence})
        deadline = time.time() + self.settings.approval_timeout_s
        try:
            while not waiter.event.wait(0.4):
                if self._cancel.get(run_id) and self._cancel[run_id].is_set():
                    self.db.update_approval(aid, status="expired", decided_at=time.time())
                    return False, "run cancelled"
                if time.time() > deadline:
                    self.db.update_approval(aid, status="expired", decided_at=time.time())
                    emit("approval_decided", {"approval_id": aid, "approved": False, "note": "timed out"})
                    return False, "Timed out waiting for approval"
            return waiter.approved, waiter.note
        finally:
            self._waiters.pop(aid, None)
            run = self.db.get_run(run_id)
            if run and run["status"] == "awaiting_approval":
                self.db.update_run(run_id, status="running")

    def decide(self, approval_id: str, approved: bool, note: str = "") -> dict:
        approval = self.db.get_approval(approval_id)
        if not approval:
            raise RunnerError("Approval not found", 404)
        waiter = self._waiters.get(approval_id)
        if approval["status"] != "pending" or waiter is None:
            raise RunnerError("This approval was already decided or has expired.", 409)
        note = (note or "")[:500]
        self.db.update_approval(approval_id, status="approved" if approved else "rejected", note=note, decided_at=time.time())
        self.db.add_event(approval["run_id"], "approval_decided", {"approval_id": approval_id, "approved": approved, "note": note})
        waiter.approved, waiter.note = approved, note
        waiter.event.set()
        return self.db.get_approval(approval_id)  # type: ignore[return-value]

    def cancel(self, run_id: str) -> None:
        ev = self._cancel.get(run_id)
        if ev is None:
            raise RunnerError("Run is not active.", 409)
        ev.set()

    # ------------------------------------------------------------------ branch / PR
    def create_branch(self, run_id: str, *, push: bool, title: str, body: str, github_token: str | None) -> dict:
        run = self.db.get_run(run_id, include_diff=True)
        if not run:
            raise RunnerError("Run not found", 404)
        if run["status"] in ("running", "awaiting_approval"):
            raise RunnerError("The run is still active.", 409)
        patch = run.get("diff") or ""
        if not patch.strip():
            raise RunnerError("This run made no changes, so there is nothing to commit.", 409)
        session = self.db.get_session(run["session_id"])
        if not session:
            raise RunnerError("Session not found", 404)
        with self._lock:
            if session["id"] in self._session_run:
                raise RunnerError("Another run is active for this repository.", 409)
        repo = self.repo_dir(session["id"])
        slug = re.sub(r"[^a-z0-9]+", "-", run["task"].lower()).strip("-")[:32].strip("-") or "change"
        branch = f"repopilot/{slug}-{run_id[:6]}"
        title = (title or run["task"].splitlines()[0])[:120]
        result: dict = {"branch": branch, "pushed": False, "pr_url": None}
        try:
            result["commit"] = gitops.commit_patch_on_branch(repo, session["base_commit"], branch, patch, f"{title}\n\nCreated by RepoPilot.")
            gitops.reset_to(repo, session["base_commit"])
        except gitops.GitError as exc:
            raise RunnerError(f"Could not create the branch: {exc}", 500) from None
        if not push:
            return result
        analysis = session.get("analysis") or {}
        if session["kind"] != "repo" or analysis.get("host") != "github.com":
            raise RunnerError("Push and pull requests are only supported for github.com repositories.", 422)
        if not github_token:
            raise RunnerError("Enter a GitHub token to push and open a pull request.", 422)
        try:
            gitops.push_branch(repo, branch, github_token)
            result["pushed"] = True
            pr_body = (body or run.get("summary") or "") + "\n\n---\nProposed by RepoPilot. Review carefully before merging."
            result["pr_url"] = gitops.open_github_pr(github_token, analysis["owner"], analysis["repo"], branch,
                                                     analysis.get("default_branch", "main"), title, pr_body)
        except gitops.GitError as exc:
            result["error"] = redact(str(exc), github_token)
        return result
