"""Environment-driven settings.

The Gemini API key is read from the server's environment (GEMINI_API_KEY) or a local
.env file that is git-ignored. It is never in the repository, never sent to the browser,
never written to the database or logs, and is hidden from repr().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent


def _load_dotenv() -> None:
    """Tiny .env loader (no dependency). Real environment variables win."""
    for candidate in (Path.cwd() / ".env", BACKEND_DIR / ".env", ROOT_DIR / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        break


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name)
    raw = default if raw is None else raw
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(repr=False)
    model: str
    thinking_level: str
    price_input_per_mtok: float
    price_output_per_mtok: float
    max_steps: int
    max_cost_usd: float
    daily_cost_cap_usd: float
    runs_per_ip_per_hour: int
    max_files_changed: int
    max_changed_lines: int
    auto_confidence_threshold: float
    test_timeout_s: int
    max_file_write_bytes: int
    max_repo_mb: int
    cors_origins: tuple[str, ...]
    allowed_git_hosts: tuple[str, ...]
    allow_local_paths: bool
    public_mode: bool
    trust_proxy: bool
    max_sessions: int
    sessions_per_owner: int
    session_ttl_hours: float
    max_concurrent_runs: int
    approval_timeout_s: int
    data_dir: Path
    workspace_dir: Path
    database_path: Path
    frontend_dir: Path


def load_settings() -> Settings:
    _load_dotenv()
    data_dir = Path(os.environ.get("DATA_DIR", ROOT_DIR / "data")).resolve()
    workspace = Path(os.environ.get("WORKSPACE_DIR", data_dir / "workspaces")).resolve()
    database = Path(os.environ.get("DATABASE_PATH", data_dir / "repopilot.db")).resolve()
    frontend = Path(os.environ.get("FRONTEND_DIR", ROOT_DIR / "frontend")).resolve()
    return Settings(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash",
        thinking_level=os.environ.get("GEMINI_THINKING_LEVEL", "").strip().lower(),
        # Conservative (standard) rates. Google lists a lower introductory rate for some Flash
        # models; using the higher figure makes the cost caps err on the safe side.
        price_input_per_mtok=_num("PRICE_INPUT_PER_MTOK", 1.50),
        price_output_per_mtok=_num("PRICE_OUTPUT_PER_MTOK", 7.50),
        max_steps=int(_num("MAX_STEPS", 30)),
        max_cost_usd=_num("MAX_COST_USD", 0.25),
        daily_cost_cap_usd=_num("DAILY_COST_CAP_USD", 5.00),
        runs_per_ip_per_hour=int(_num("RUNS_PER_IP_PER_HOUR", 6)),
        max_files_changed=int(_num("MAX_FILES_CHANGED", 10)),
        max_changed_lines=int(_num("MAX_CHANGED_LINES", 400)),
        auto_confidence_threshold=_num("AUTO_CONFIDENCE_THRESHOLD", 0.8),
        test_timeout_s=int(_num("TEST_TIMEOUT_S", 120)),
        max_file_write_bytes=int(_num("MAX_FILE_WRITE_BYTES", 200_000)),
        max_repo_mb=int(_num("MAX_REPO_MB", 100)),
        cors_origins=_list("CORS_ORIGINS", ""),
        allowed_git_hosts=_list("ALLOWED_GIT_HOSTS", "github.com"),
        allow_local_paths=_bool("ALLOW_LOCAL_PATHS", False),
        public_mode=_bool("PUBLIC_MODE", True),
        trust_proxy=_bool("TRUST_PROXY", False),
        max_sessions=int(_num("MAX_SESSIONS", 60)),
        sessions_per_owner=int(_num("SESSIONS_PER_OWNER", 5)),
        session_ttl_hours=_num("SESSION_TTL_HOURS", 24),
        max_concurrent_runs=int(_num("MAX_CONCURRENT_RUNS", 4)),
        approval_timeout_s=int(_num("APPROVAL_TIMEOUT_S", 600)),
        data_dir=data_dir,
        workspace_dir=workspace,
        database_path=database,
        frontend_dir=frontend,
    )
