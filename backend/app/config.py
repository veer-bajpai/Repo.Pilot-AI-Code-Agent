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
    database_url: str = ""
    auth_required: bool = False
    auth_auto_create: bool = False
    jwt_secret: str = field(default="", repr=False)
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_tls: bool = True
    app_base_url: str = "http://localhost:8000"
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    github_client_id: str = ""
    github_client_secret: str = field(default="", repr=False)
    redis_url: str = "redis://localhost:6379/0"
    stripe_secret_key: str = field(default="", repr=False)
    stripe_webhook_secret: str = field(default="", repr=False)
    stripe_price_id: str = ""
    free_monthly_runs: int = 20
    paid_monthly_runs: int = 500
    free_concurrent_runs: int = 1
    paid_concurrent_runs: int = 5
    queue_enabled: bool = False
    admin_emails: tuple[str, ...] = ()


def load_settings() -> Settings:
    _load_dotenv()
    data_dir = Path(os.environ.get("DATA_DIR", ROOT_DIR / "data")).resolve()
    workspace = Path(os.environ.get("WORKSPACE_DIR", data_dir / "workspaces")).resolve()
    database = Path(os.environ.get("DATABASE_PATH", data_dir / "repopilot.db")).resolve()
    built_frontend = ROOT_DIR / "frontend-react" / "dist"
    frontend_default = built_frontend if built_frontend.is_dir() else ROOT_DIR / "frontend"
    frontend = Path(os.environ.get("FRONTEND_DIR", frontend_default)).resolve()
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
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        auth_required=_bool("AUTH_REQUIRED", bool(os.environ.get("DATABASE_URL"))),
        auth_auto_create=_bool("AUTH_AUTO_CREATE", False),
        jwt_secret=os.environ.get("JWT_SECRET", "").strip(),
        access_token_minutes=int(_num("ACCESS_TOKEN_MINUTES", 15)),
        refresh_token_days=int(_num("REFRESH_TOKEN_DAYS", 30)),
        email_from=os.environ.get("EMAIL_FROM", "").strip(),
        smtp_host=os.environ.get("SMTP_HOST", "").strip(),
        smtp_port=int(_num("SMTP_PORT", 587)),
        smtp_user=os.environ.get("SMTP_USER", "").strip(),
        smtp_password=os.environ.get("SMTP_PASSWORD", "").strip(),
        smtp_tls=_bool("SMTP_TLS", True),
        app_base_url=os.environ.get("APP_BASE_URL", "http://localhost:8000").strip(),
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        github_client_id=os.environ.get("GITHUB_CLIENT_ID", "").strip(),
        github_client_secret=os.environ.get("GITHUB_CLIENT_SECRET", "").strip(),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0").strip(),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", "").strip(),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip(),
        stripe_price_id=os.environ.get("STRIPE_PRICE_ID", "").strip(),
        free_monthly_runs=int(_num("FREE_MONTHLY_RUNS", 20)),
        paid_monthly_runs=int(_num("PAID_MONTHLY_RUNS", 500)),
        free_concurrent_runs=int(_num("FREE_CONCURRENT_RUNS", 1)),
        paid_concurrent_runs=int(_num("PAID_CONCURRENT_RUNS", 5)),
        queue_enabled=_bool("QUEUE_ENABLED", False),
        admin_emails=_list("ADMIN_EMAILS", ""),
    )
