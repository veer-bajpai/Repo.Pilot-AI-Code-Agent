"""Redis/Celery execution boundary for production workers."""
from __future__ import annotations

from celery import Celery

from .config import load_settings

settings = load_settings()
celery_app = Celery("repopilot", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(task_track_started=True, task_serializer="json", accept_content=["json"], result_serializer="json")


def enqueue_run(run_id: str, session_id: str, task: str, mode: str, llm_kind: str) -> None:
    run_agent_job.delay(run_id, session_id, task, mode, llm_kind)


@celery_app.task(name="repopilot.run_agent")
def run_agent_job(run_id: str, session_id: str, task: str, mode: str, llm_kind: str) -> None:
    from .db import Database
    from .demo.tasks import DEMOS
    from .llm import GeminiLLM, ScriptedLLM
    from .runner import Runner

    runtime_db = settings.database_url if settings.database_url.startswith(("postgresql://", "postgresql+psycopg://")) else settings.database_path
    db = Database(runtime_db)
    runner = Runner(settings, db, recover_on_start=False)
    session = db.get_session(session_id)
    if not session:
        return
    if llm_kind == "simulated":
        llm = ScriptedLLM(DEMOS[session["analysis"]["demo_id"]].script)
    else:
        llm = GeminiLLM(settings.gemini_api_key, settings.model, thinking_level=settings.thinking_level)
    runner._run_thread(run_id, session, task, mode, llm, allow_tests=not settings.public_mode or session["kind"] == "demo")
