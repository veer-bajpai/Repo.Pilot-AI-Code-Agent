"""Git plumbing: URL normalisation, clone, diff, branch, push, GitHub PR.

Notes on behaviour that matters in practice:
* GIT_TERMINAL_PROMPT=0 so a private repo fails fast instead of hanging on a password prompt.
* Commits pass an explicit identity so machines without `git config user.name` still work.
* Tokens are passed via a one-shot http.extraheader, never stored in the remote URL or git config.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .secrets_util import redact

GIT_TIMEOUT = 120
EXCLUDES = [
    "__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".venv/", "venv/",
    "node_modules/", ".DS_Store", "desktop.ini", "target/", ".gradle/", "*.egg-info/", "dist/", "build/",
    ".coverage", "htmlcov/",
]


class GitError(Exception):
    pass


def git_available() -> bool:
    return shutil.which("git") is not None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "GCM_INTERACTIVE": "never", "LC_ALL": "C"})
    # Never let a stray key in RepoPilot's own environment reach a git subprocess.
    for k in list(env):
        if k.upper().endswith(("API_KEY", "_TOKEN")) or k.upper() in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN"}:
            env.pop(k, None)
    return env


def run_git(args: list[str], cwd: Path | None = None, timeout: int = GIT_TIMEOUT, token: str | None = None,
            check: bool = True) -> str:
    cmd = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    cmd += args
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=_env(), capture_output=True,
                              text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout}s") from None
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if check and proc.returncode != 0:
        raise GitError(redact((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed", token))
    return proc.stdout


# ----------------------------------------------------------------------------
# URL handling
# ----------------------------------------------------------------------------
_SSH_RE = re.compile(r"^git@([\w.\-]+):([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def normalize_repo_url(raw: str, allowed_hosts: tuple[str, ...]) -> tuple[str, str, str, str]:
    """Accept what people actually paste and return (clone_url, host, owner, repo).

    Accepts: https://github.com/o/r, .../r.git, .../r/tree/main/src, github.com/o/r,
    and git@github.com:o/r.git. Rejects anything that isn't https on an allowed host,
    including credentials embedded in the URL."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Paste a repository link, for example https://github.com/owner/repo")
    if re.match(r"^(sk-|AIza|ghp_|gho_|ghs_|github_pat_)", raw):
        raise ValueError("That looks like an API key or token, not a repository link. Keys are entered separately and never as a repository.")
    m = _SSH_RE.match(raw)
    if m:
        host, owner, repo = m.group(1).lower(), m.group(2), m.group(3)
    else:
        candidate = raw if "://" in raw else f"https://{raw}"
        parsed = urlparse(candidate)
        if parsed.scheme != "https":
            raise ValueError("Only https repository links are accepted.")
        if parsed.username or parsed.password or "@" in parsed.netloc:
            raise ValueError("Remove credentials from the URL. Private repositories are not supported.")
        host = (parsed.hostname or "").lower()
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) < 2:
            raise ValueError("The link must include an owner and a repository: https://github.com/owner/repo")
        owner, repo = segments[0], segments[1]
        repo = repo[:-4] if repo.endswith(".git") else repo
    if host not in allowed_hosts:
        raise ValueError(f"Host {host!r} is not allowed. Allowed: {', '.join(allowed_hosts)}")
    if not (_NAME_RE.match(owner) and _NAME_RE.match(repo)) or {owner, repo} & {".", ".."}:
        raise ValueError("The owner or repository name contains unsupported characters.")
    return f"https://{host}/{owner}/{repo}.git", host, owner, repo


# ----------------------------------------------------------------------------
# Repository operations
# ----------------------------------------------------------------------------
def force_rmtree(path: Path) -> None:
    """rmtree that copes with read-only files (git objects on Windows)."""
    def onerror(func, p, _exc):  # pragma: no cover - platform dependent
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def clone(clone_url: str, dest: Path, max_bytes: int = 300 * 1024 * 1024) -> str:
    """Shallow-clone into dest. Returns the default branch name."""
    force_rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_git(["clone", "--depth", "1", "--single-branch", "--no-tags", "--", clone_url, str(dest)], timeout=180)
    except GitError as exc:
        msg = str(exc)
        if any(s in msg.lower() for s in ("not found", "authentication", "could not read", "terminal prompts disabled")):
            raise GitError("Repository not found, or it is private. Only public repositories can be cloned.") from None
        raise
    if dir_size(dest) > max_bytes:
        force_rmtree(dest)
        raise GitError("Repository is larger than the 300 MB limit.")
    prepare_workspace(dest)
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest).strip() or "main"


def prepare_workspace(repo: Path) -> None:
    """Exclude build/test byproducts from diffs; make sure git works without a user identity."""
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text("\n".join(EXCLUDES) + "\n", encoding="utf-8")


def head_commit(repo: Path) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=repo).strip()


def reset_to(repo: Path, commit: str) -> None:
    """Detach at `commit` and discard everything else. Detaching keeps any branch
    RepoPilot created earlier intact instead of dragging it back to the baseline."""
    run_git(["checkout", "-q", "-f", "--detach", commit], cwd=repo)
    run_git(["reset", "-q", "--hard", commit], cwd=repo)
    run_git(["clean", "-fdq"], cwd=repo)


def diff_since(repo: Path, commit: str) -> str:
    """Unified diff of all changes (incl. new files) relative to `commit`."""
    run_git(["add", "-A"], cwd=repo)
    return run_git(["diff", "--cached", "--no-color", "--no-ext-diff", commit], cwd=repo)


def changed_files(repo: Path, commit: str) -> list[str]:
    run_git(["add", "-A"], cwd=repo)
    out = run_git(["diff", "--cached", "--name-only", commit], cwd=repo)
    return [line for line in out.splitlines() if line.strip()]


IDENT = ["-c", "user.name=RepoPilot", "-c", "user.email=repopilot@users.noreply.github.com"]


def commit_patch_on_branch(repo: Path, base_commit: str, branch: str, patch: str, message: str) -> str:
    """Apply `patch` on a fresh branch from base_commit and commit. Returns commit sha."""
    if not patch.strip():
        raise GitError("There are no changes to commit.")
    run_git(["checkout", "-q", "-f", "-B", branch, base_commit], cwd=repo)
    run_git(["clean", "-fdq"], cwd=repo)
    patch_file = repo / ".git" / "repopilot.patch"
    patch_file.write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8")
    try:
        run_git(["apply", "--index", "--whitespace=nowarn", str(patch_file)], cwd=repo)
    finally:
        patch_file.unlink(missing_ok=True)
    run_git([*IDENT, "commit", "-q", "-m", message], cwd=repo)
    return head_commit(repo)


def push_branch(repo: Path, branch: str, token: str) -> None:
    try:
        run_git(["push", "--set-upstream", "origin", f"{branch}:{branch}"], cwd=repo, token=token, timeout=120)
    except GitError as exc:
        msg = str(exc)
        if "403" in msg or "denied" in msg.lower() or "permission" in msg.lower():
            raise GitError(
                "GitHub refused the push: this token cannot write to the repository. "
                "You can push to a fork instead, or download the patch and apply it yourself."
            ) from None
        raise


def open_github_pr(token: str, owner: str, repo: str, head: str, base: str, title: str, body: str,
                   client: httpx.Client | None = None) -> str:
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        resp = client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "RepoPilot"},
            json={"title": title, "head": head, "base": base, "body": body},
        )
    finally:
        if own:
            client.close()
    if resp.status_code == 201:
        return resp.json()["html_url"]
    detail = ""
    try:
        detail = resp.json().get("message", "")
    except Exception:
        detail = resp.text[:200]
    raise GitError(redact(f"GitHub API returned {resp.status_code}: {detail}", token))
