"""Hard guardrails. Anything that raises Refusal is blocked *before* a human is asked,
in every approval mode."""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class Refusal(Exception):
    """A request the agent may never perform, regardless of approval."""


# ----------------------------------------------------------------------------
# Path sandbox
# ----------------------------------------------------------------------------
def safe_path(root: Path, rel: str) -> Path:
    """Resolve `rel` inside `root` or raise Refusal. Rejects absolute paths,
    traversal, NUL bytes, drive letters and symlinks that escape the repo."""
    if not isinstance(rel, str) or not rel.strip():
        raise Refusal("empty path")
    if "\x00" in rel:
        raise Refusal("NUL byte in path")
    normalized = rel.replace("\\", "/").strip()
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or normalized.startswith("~"):
        raise Refusal(f"absolute paths are not allowed: {rel!r}")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise Refusal(f"path traversal is not allowed: {rel!r}")
    root_resolved = root.resolve()
    target = (root_resolved / Path(*parts)).resolve() if parts else root_resolved
    try:
        target.relative_to(root_resolved)
    except ValueError:
        raise Refusal(f"path escapes the repository: {rel!r}") from None
    return target


def rel_posix(root: Path, target: Path) -> str:
    return PurePosixPath(target.resolve().relative_to(root.resolve())).as_posix()


# ----------------------------------------------------------------------------
# Write blocklist / protected files
# ----------------------------------------------------------------------------
_BLOCKED_NAME_PATTERNS = [
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "secrets.*", "secret.*", "credentials*", ".npmrc", ".pypirc", ".netrc", "*.keystore",
]


_EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".tmpl")


def is_secret_name(name: str) -> bool:
    low = name.lower()
    if low.endswith(_EXAMPLE_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(low, pat) for pat in _BLOCKED_NAME_PATTERNS)


def is_secret_path(rel: str) -> bool:
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    return bool(parts) and (".git" in [p.lower() for p in parts] or is_secret_name(parts[-1]))


def check_read_allowed(root: Path, rel: str) -> Path:
    """Secrets must never be read into the model's context."""
    target = safe_path(root, rel)
    for candidate in {rel, rel_posix(root, target)}:
        if is_secret_path(candidate):
            raise Refusal(f"reading {candidate!r} is blocked (it may contain secrets)")
    return target


def check_write_allowed(root: Path, rel: str) -> Path:
    """Return the resolved target if writing is allowed, else raise Refusal.
    Checked on the requested path *and* on the symlink-resolved path."""
    target = safe_path(root, rel)
    for candidate in {rel.replace("\\", "/"), rel_posix(root, target)}:
        parts = [p for p in candidate.split("/") if p not in ("", ".")]
        lowered = [p.lower() for p in parts]
        if ".git" in lowered:
            raise Refusal("writing inside .git is not allowed")
        if parts and is_secret_name(parts[-1]):
            raise Refusal(f"writing to {parts[-1]!r} is blocked (secrets/credentials pattern)")
    return target


_PROTECTED_PATTERNS = [
    ".github/*", ".gitlab-ci.yml", "jenkinsfile", ".circleci/*", "azure-pipelines.yml",
    "dockerfile*", "docker-compose*", "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "pipfile", "poetry.lock", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "go.mod", "go.sum", "cargo.toml", "cargo.lock", "pom.xml", "build.gradle*", "gemfile*", "makefile",
]


def is_protected(rel: str) -> bool:
    lowered = rel.replace("\\", "/").lower()
    while lowered.startswith("./"):
        lowered = lowered[2:]
    lowered = lowered.lstrip("/")
    base = lowered.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(lowered, p) or fnmatch.fnmatch(base, p) for p in _PROTECTED_PATTERNS)


# ----------------------------------------------------------------------------
# Test command allowlist
# ----------------------------------------------------------------------------
_SHELL_CHARS = set(";|&<>$`\n\r(){}*?!\\")
_ARG_RE = re.compile(r"^[A-Za-z0-9_./:=,@%+\-\[\]]+$")
_DENIED_ARGS = {"-c", "-p", "--confcutdir", "--basetemp", "--rootdir", "--pyargs", "-o", "--override-ini"}


def parse_test_command(command: str) -> list[str]:
    """Validate `command` against the allowlist and return the argv to execute
    (no shell). Raises Refusal for anything else."""
    if not isinstance(command, str) or not command.strip():
        raise Refusal("empty test command")
    if len(command) > 200:
        raise Refusal("test command too long")
    if any(ch in _SHELL_CHARS for ch in command):
        raise Refusal("shell operators and metacharacters are not allowed in test commands")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise Refusal(f"could not parse command: {exc}") from None
    if not tokens:
        raise Refusal("empty test command")

    def check_args(args: list[str]) -> list[str]:
        for a in args:
            if a in _DENIED_ARGS or any(a.startswith(d + "=") for d in _DENIED_ARGS if d.startswith("--")):
                raise Refusal(f"argument {a!r} is not allowed")
            if not _ARG_RE.match(a):
                raise Refusal(f"argument {a!r} contains disallowed characters")
            if a.startswith("/") or a.startswith("~") or ".." in a.split("/"):
                raise Refusal(f"argument {a!r} must be a relative path inside the repo")
        return args

    head = tokens[0]
    if head in ("pytest", "py.test"):
        return [sys.executable, "-m", "pytest", *check_args(tokens[1:])]
    if head in ("python", "python3", "py") and tokens[1:3] == ["-m", "pytest"]:
        return [sys.executable, "-m", "pytest", *check_args(tokens[3:])]
    if head in ("python", "python3", "py") and tokens[1:3] == ["-m", "unittest"]:
        return [sys.executable, "-m", "unittest", *check_args(tokens[3:])]
    if head == "npm" and tokens[1:] in (["test"], ["run", "test"], ["t"]):
        return ["npm", "test"]
    if head in ("yarn", "pnpm") and tokens[1:] in (["test"], ["run", "test"]):
        return [head, "test"]
    if head == "go" and tokens[1:2] == ["test"]:
        return ["go", "test", *check_args(tokens[2:])]
    if head == "cargo" and tokens[1:2] == ["test"]:
        return ["cargo", "test", *check_args(tokens[2:])]
    if head == "mvn" and tokens[1:2] == ["test"]:
        return ["mvn", "-q", "test", *check_args(tokens[2:])]
    if head == "gradle" and tokens[1:2] == ["test"]:
        return ["gradle", "test", *check_args(tokens[2:])]
    if head == "make" and tokens[1:] == ["test"]:
        return ["make", "test"]
    raise Refusal(
        "only recognised test runners are allowed (pytest, python -m pytest, python -m unittest, "
        "npm/yarn/pnpm test, go test, cargo test, mvn test, gradle test, make test)"
    )


def scrubbed_env() -> dict[str, str]:
    """Environment for test processes: PATH and locale only. No API keys, no
    tokens, nothing inherited from RepoPilot's own environment."""
    keep = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TERM"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    home = tempfile.mkdtemp(prefix="repopilot-home-")
    env.update({"HOME": home, "USERPROFILE": home, "PYTHONDONTWRITEBYTECODE": "1", "CI": "1", "NO_COLOR": "1"})
    return env


# ----------------------------------------------------------------------------
# Approval policy
# ----------------------------------------------------------------------------
@dataclass
class Decision:
    needs_approval: bool
    reason: str


def approval_policy(
    *, kind: str, mode: str, confidence: float | None, protected: bool, threshold: float
) -> Decision:
    """Decide whether a human must approve. kind: 'edit' | 'test'."""
    if mode != "auto":
        return Decision(True, "approval mode: ask every time")
    if kind == "test":
        return Decision(False, "allowlisted test command")
    if protected:
        return Decision(True, "protected file (CI, Docker or dependency manifest)")
    if confidence is None:
        return Decision(True, "edit arrived without a confidence score")
    if confidence < threshold:
        return Decision(True, f"model confidence {confidence:.2f} is below {threshold:.2f}")
    return Decision(False, f"confident edit ({confidence:.2f})")
