"""Fail if anything that looks like a real credential (or a stray .env file) is in the tree.

Run it before you push:   python scripts/check_no_secrets.py
It also runs in CI and in the test suite, so a key can't be committed by accident.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{40,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_\-]{40,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "GitHub fine-grained token": re.compile(r"github_pat_[A-Za-z0-9_]{60,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "data", ".mypy_cache"}
SAFE_ENV_NAMES = (".example", ".sample", ".template")


def scan(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts) or not path.is_file():
            continue
        name = path.name.lower()
        if (name == ".env" or name.startswith(".env.")) and not name.endswith(SAFE_ENV_NAMES):
            problems.append(f"{path.relative_to(root)}: environment file must not be committed")
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                problems.append(f"{path.relative_to(root)}: looks like a {label}")
    return problems


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    problems = scan(root)
    if problems:
        print("Possible secrets found:\n  " + "\n  ".join(problems))
        return 1
    print("No secrets found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
