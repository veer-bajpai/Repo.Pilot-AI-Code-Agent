"""REST API + static frontend. Built to be reachable by the public.

* No visitor login and no visitor API key. The Gemini key lives in the server's environment.
* Every browser gets a private, anonymous id cookie. Repositories, runs and approvals are
  scoped to it, so visitors cannot see or control each other's work.
* Abuse limits (per-IP run rate, per-run budget, rolling daily spend cap, session caps and
  auto-cleanup) keep a public deployment from draining the key or filling the disk.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import gitops
from .config import Settings, load_settings
from .db import Database
from .demo.tasks import DEMOS, public_list
from .llm import GeminiLLM, LLMError, ScriptedLLM
from .runner import Runner, RunnerError
from .secrets_util import redact

VERSION = "2.0.0"
OWNER_COOKIE = "rp_owner"
_OWNER_RE = re.compile(r"^[0-9a-f]{32}$")


class RateLimiter:
    """Tiny in-memory sliding-window limiter, keyed by client IP."""

    def __init__(self, limit: int, window_s: int, message: str = "Too many requests. Wait a bit and try again."):
        self.limit, self.window, self.message = limit, window_s, message
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise HTTPException(429, self.message)
            q.append(now)


class SessionIn(BaseModel):
    repo_url: str | None = Field(default=None, max_length=300)
    demo: str | None = Field(default=None, max_length=40)
    local_path: str | None = Field(default=None, max_length=500)


class RunIn(BaseModel):
    task: str = Field(max_length=4000)
    approval_mode: str = "ask"
    mode: str | None = None            # "live" | "simulated" (demos only); default chosen from context


class ApprovalIn(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=500)


class PRIn(BaseModel):
    push: bool = False
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=4000)


def default_llm_factory(api_key: str, model: str, thinking_level: str = ""):
    return GeminiLLM(api_key=api_key, model=model, thinking_level=thinking_level)


def create_app(settings: Settings | None = None, llm_factory: Callable = default_llm_factory,
               start_janitor: bool = False) -> FastAPI:
    settings = settings or load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_path)
    runner = Runner(settings, db, start_janitor=start_janitor)
    clone_limiter = RateLimiter(20, 60)
    run_limiter = RateLimiter(settings.runs_per_ip_per_hour, 3600,
                              "You've reached the hourly limit for AI runs. Try again later, or use the built-in demos.")
    demo_limiter = RateLimiter(60, 3600)
    ai_cache: dict = {"at": 0.0, "value": None}
    ai_lock = threading.Lock()

    app = FastAPI(title="RepoPilot", version=VERSION,
                  description="An approval-gated AI developer agent powered by Google Gemini.")
    app.state.runner, app.state.db, app.state.settings = runner, db, settings

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"],
                           allow_headers=["*"], allow_credentials=False)

    # ---------------------------------------------------------------- middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        owner = request.cookies.get(OWNER_COOKIE, "")
        fresh = not _OWNER_RE.match(owner)
        if fresh:
            owner = secrets.token_hex(16)
        request.state.owner = owner
        response = await call_next(request)
        if fresh:
            secure = request.url.scheme == "https" or (settings.trust_proxy and request.headers.get("x-forwarded-proto") == "https")
            response.set_cookie(OWNER_COOKIE, owner, max_age=30 * 86400, httponly=True, samesite="lax", secure=secure, path="/")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if not path.startswith(("/docs", "/redoc", "/openapi")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        return response

    @app.exception_handler(RunnerError)
    async def runner_error(_req: Request, exc: RunnerError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def unhandled(_req: Request, exc: Exception):
        return JSONResponse({"detail": f"Unexpected server error: {redact(str(exc), settings.gemini_api_key)[:200]}"}, status_code=500)

    # ---------------------------------------------------------------- helpers
    def client_ip(request: Request) -> str:
        if settings.trust_proxy:
            fwd = request.headers.get("x-forwarded-for", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def own_session(sid: str, request: Request) -> dict:
        s = db.get_session(sid)
        if not s or db.session_owner(sid) != request.state.owner:
            raise HTTPException(404, "Repository not found")     # same answer for "missing" and "not yours"
        return s

    def own_run(rid: str, request: Request) -> dict:
        run = db.get_run(rid)
        if not run or db.owner_of_run(rid) != request.state.owner:
            raise HTTPException(404, "Run not found")
        return run

    ai_enabled = bool(settings.gemini_api_key)

    def make_llm():
        return llm_factory(settings.gemini_api_key, settings.model, settings.thinking_level)

    # ---------------------------------------------------------------- meta
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "git": gitops.git_available(), "ai_configured": ai_enabled}

    @app.get("/api/config")
    def config():
        return {
            "version": VERSION, "demos": public_list(), "provider": "Google Gemini", "model": settings.model,
            "ai_available": ai_enabled, "public_mode": settings.public_mode,
            "allow_local_paths": settings.allow_local_paths, "allowed_hosts": list(settings.allowed_git_hosts),
            "git_available": gitops.git_available(),
            "limits": {"max_steps": settings.max_steps, "max_cost_usd": settings.max_cost_usd,
                       "max_files_changed": settings.max_files_changed, "max_changed_lines": settings.max_changed_lines,
                       "test_timeout_s": settings.test_timeout_s, "auto_confidence_threshold": settings.auto_confidence_threshold,
                       "sessions_per_owner": settings.sessions_per_owner, "runs_per_ip_per_hour": settings.runs_per_ip_per_hour},
        }

    @app.get("/api/ai/status")
    def ai_status():
        """Is the server's Gemini key actually working? Cached so visitors can't turn this into a
        way to spend the key: at most one upstream call every 5 minutes."""
        if not ai_enabled:
            return {"available": False, "reason": "Live AI is not configured on this server. The built-in demos still work."}
        with ai_lock:
            if ai_cache["value"] is None or time.time() - ai_cache["at"] > 300:
                try:
                    make_llm().check()
                    ai_cache["value"] = {"available": True, "model": settings.model}
                except LLMError as exc:
                    ai_cache["value"] = {"available": False, "reason": str(exc)}
                ai_cache["at"] = time.time()
            return ai_cache["value"]

    # ---------------------------------------------------------------- sessions
    @app.post("/api/sessions", status_code=201)
    def add_session(body: SessionIn, request: Request):
        given = [x for x in (body.repo_url, body.demo, body.local_path) if x]
        if len(given) != 1:
            raise HTTPException(422, "Provide exactly one of repo_url, demo or local_path.")
        owner = request.state.owner
        if body.demo:
            return runner.add_demo(body.demo, owner)
        if body.local_path:
            return runner.add_local(body.local_path, owner)
        clone_limiter.check("clone:" + client_ip(request))
        return runner.add_repo(body.repo_url or "", owner)

    @app.get("/api/sessions")
    def list_sessions(request: Request):
        return db.list_sessions(request.state.owner)

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str, request: Request):
        s = own_session(sid, request)
        s["runs"] = db.list_runs(sid)
        return s

    @app.delete("/api/sessions/{sid}", status_code=204)
    def delete_session(sid: str, request: Request):
        own_session(sid, request)
        runner.delete_session(sid)

    @app.get("/api/sessions/{sid}/search")
    def search(sid: str, request: Request, q: str = Query(min_length=1, max_length=200)):
        own_session(sid, request)
        return runner.search(sid, q)

    # ---------------------------------------------------------------- runs
    @app.post("/api/sessions/{sid}/runs", status_code=201)
    def start_run(sid: str, body: RunIn, request: Request):
        session = own_session(sid, request)
        is_demo = session["kind"] == "demo"
        mode = body.mode or ("simulated" if is_demo else "live")
        if mode not in ("live", "simulated"):
            raise HTTPException(422, "mode must be 'live' or 'simulated'")
        ip = client_ip(request)
        if mode == "simulated":
            if not is_demo:
                raise HTTPException(422, "Scripted runs are only available for the built-in demo repositories.")
            demo_limiter.check("demo:" + ip)
            llm = ScriptedLLM(DEMOS[session["analysis"]["demo_id"]].script)
        else:
            if not ai_enabled:
                raise HTTPException(503, "Live AI is not configured on this server. The built-in demos still work.")
            runner.check_live_budget()
            run_limiter.check("run:" + ip)
            llm = make_llm()
        return runner.start_run(sid, body.task, body.approval_mode, llm, mode)

    @app.get("/api/runs/{rid}")
    def get_run(rid: str, request: Request):
        return own_run(rid, request)

    @app.get("/api/runs/{rid}/events")
    def run_events(rid: str, request: Request, after: int = 0):
        run = own_run(rid, request)
        return {"run": run, "events": db.events_after(rid, after)}

    @app.get("/api/runs/{rid}/approvals")
    def run_approvals(rid: str, request: Request):
        own_run(rid, request)
        return db.list_approvals(rid)

    @app.post("/api/approvals/{aid}")
    def decide(aid: str, body: ApprovalIn, request: Request):
        if db.get_approval(aid) is None or db.owner_of_approval(aid) != request.state.owner:
            raise HTTPException(404, "Approval not found")
        return runner.decide(aid, body.approved, body.note)

    @app.post("/api/runs/{rid}/cancel")
    def cancel(rid: str, request: Request):
        own_run(rid, request)
        runner.cancel(rid)
        return {"ok": True}

    def _diff(rid: str, request: Request) -> str:
        own_run(rid, request)
        run = db.get_run(rid, include_diff=True)
        return (run or {}).get("diff") or ""

    @app.get("/api/runs/{rid}/diff")
    def run_diff(rid: str, request: Request):
        diff = _diff(rid, request)
        files, cur = [], None
        for line in diff.splitlines():
            if line.startswith("diff --git"):
                cur = {"path": line.split(" b/", 1)[-1], "added": 0, "removed": 0}
                files.append(cur)
            elif cur and line.startswith("+") and not line.startswith("+++"):
                cur["added"] += 1
            elif cur and line.startswith("-") and not line.startswith("---"):
                cur["removed"] += 1
        return {"diff": diff, "files": files}

    @app.get("/api/runs/{rid}/patch")
    def run_patch(rid: str, request: Request):
        diff = _diff(rid, request)
        if not diff.strip():
            raise HTTPException(404, "This run made no changes.")
        return PlainTextResponse(diff if diff.endswith("\n") else diff + "\n",
                                 headers={"Content-Disposition": f'attachment; filename="repopilot-{rid}.patch"'})

    @app.post("/api/runs/{rid}/pr")
    def create_pr(rid: str, body: PRIn, request: Request):
        own_run(rid, request)
        token = (request.headers.get("x-github-token") or "").strip() or None
        if body.push:
            clone_limiter.check("pr:" + client_ip(request))
        result = runner.create_branch(rid, push=body.push, title=body.title, body=body.body, github_token=token)
        if result.get("error"):
            return JSONResponse({"detail": result["error"], **result}, status_code=502)
        return result

    # ---------------------------------------------------------------- frontend
    if settings.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")
    else:
        @app.get("/")
        def root():
            return {"detail": "Frontend directory not found. The API is available at /docs."}

    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    """Lazy module-level `app` so `uvicorn app.main:app` works without importing having side effects
    (tests import create_app and must not create a real data directory)."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app(start_janitor=True)
        return _app
    raise AttributeError(name)
