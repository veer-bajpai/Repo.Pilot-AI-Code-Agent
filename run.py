"""One-command launcher:  python run.py

* Binds to 127.0.0.1 by default (containers set HOST=0.0.0.0).
* Checks the things that usually trip up a first run and explains them in plain language.
* Reads GEMINI_API_KEY from the environment or a git-ignored .env file. Nothing is ever typed into the web page.
"""
import os
import socket
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND))


def port_free(host: str, port: int) -> bool:
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((probe, port)) != 0


def main() -> int:
    if sys.version_info < (3, 10):
        print("RepoPilot needs Python 3.10 or newer (you have %s.%s)." % sys.version_info[:2])
        return 1
    try:
        import fastapi, uvicorn, httpx  # noqa: F401
        from google import genai  # noqa: F401
    except ImportError as exc:
        print(f"A dependency is missing ({exc.name}). Install them with:\n\n    pip install -r requirements.txt\n")
        return 1
    from app.gitops import git_available
    if not git_available():
        print("git was not found on your PATH. Install it from https://git-scm.com, then run again.")
        return 1
    host = os.environ.get("HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        print("PORT must be a number, for example: PORT=8001 python run.py")
        return 1
    if not port_free(host, port):
        print(f"Port {port} is already in use. Pick another one, for example:\n\n    PORT={port + 1} python run.py\n")
        return 1
    import uvicorn
    from app.config import load_settings
    live = "on (Gemini)" if load_settings().gemini_api_key else "OFF - set GEMINI_API_KEY to enable it (demos work without it)"
    print(f"\n  RepoPilot is running at  http://localhost:{port}\n"
          f"  Live AI: {live}\n"
          "  Press Ctrl+C to stop.\n")
    uvicorn.run("app.main:app", host=host, port=port, app_dir=str(BACKEND), log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
