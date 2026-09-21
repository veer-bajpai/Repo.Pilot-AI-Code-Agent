"""The agent's tools. Every path goes through the sandbox in guardrails.py.

Two phases so approvals can show a real diff:
  classify(name, args) -> Action   validates, raises Refusal (blocked) or ToolError (recoverable)
  execute(name, args)  -> str      performs it
"""
from __future__ import annotations

import difflib
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .config import Settings
from .guardrails import (
    Refusal, check_read_allowed, check_write_allowed, is_protected, parse_test_command, rel_posix, safe_path,
    scrubbed_env,
)
from .indexer import CodeIndex, SKIP_DIRS, is_probably_text

MAX_OUTPUT_CHARS = 12_000
READ_MAX_LINES = 300
READ_MAX_CHARS = 24_000


class ToolError(Exception):
    """A recoverable problem, reported back to the model as an ERROR result."""


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "update_plan",
        "description": "Record or revise your plan as a short list of steps. Call this first, and again if the plan changes.",
        "input_schema": {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
                         "required": ["steps"]},
    },
    {
        "name": "search_code",
        "description": "Search the repository with a natural-language or identifier query. Returns ranked snippets with file paths and line ranges.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                         "required": ["query"]},
    },
    {
        "name": "list_files",
        "description": "List files and directories under a path (default: repository root), two levels deep.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "read_file",
        "description": "Read a file with line numbers. Use start_line/end_line to read a range (max 300 lines per call).",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                                                          "end_line": {"type": "integer"}}, "required": ["path"]},
    },
    {
        "name": "replace_in_file",
        "description": "Replace exactly one occurrence of old_string with new_string in a file. old_string must match the file exactly once (include enough surrounding context). Provide a confidence between 0 and 1 and a one-sentence reason.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
            "confidence": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["path", "old_string", "new_string", "confidence", "reason"]},
    },
    {
        "name": "write_file",
        "description": "Create a new file or overwrite an existing one with the full content. Prefer replace_in_file for small edits. Provide a confidence between 0 and 1 and a one-sentence reason.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "confidence": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["path", "content", "confidence", "reason"]},
    },
    {
        "name": "run_tests",
        "description": "Run the repository's test suite. Omit `command` to use the detected default. Only recognised test runners are permitted.",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
    },
    {
        "name": "git_diff",
        "description": "Show the cumulative diff of all your changes so far.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "finish",
        "description": "Call when the task is complete (or cannot be completed). Summarise what you changed, what you verified, and anything left uncertain.",
        "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}, "confidence": {"type": "number"}},
                         "required": ["summary"]},
    },
]
READ_ONLY = {"update_plan", "search_code", "list_files", "read_file", "git_diff", "finish"}


@dataclass
class Action:
    kind: str                        # read | edit | test
    name: str
    target: str | None = None
    preview: str = ""
    protected: bool = False
    confidence: float | None = None
    argv: list[str] | None = None


def _text_diff(rel: str, old: str | None, new: str) -> str:
    diff = difflib.unified_diff((old or "").splitlines(), new.splitlines(),
                                fromfile=f"a/{rel}" if old is not None else "/dev/null", tofile=f"b/{rel}", lineterm="", n=3)
    return "\n".join(diff)


def _count_changed(old: str | None, new: str | None) -> int:
    n = 0
    for line in difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            n += 1
    return n


def _read_preserving(path: Path, strict: bool = False) -> str:
    """Read text without translating line endings (so CRLF files stay CRLF).
    strict=True refuses files that are not valid UTF-8 rather than corrupting them on write-back."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        if strict:
            raise Refusal(f"{path.name} is not valid UTF-8 text, so it cannot be edited safely") from None
        return raw.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + f"\n... [{len(text) - limit} characters omitted] ...\n" + text[-(limit - head):]


class ToolBox:
    def __init__(self, root: Path, index: CodeIndex, settings: Settings, base_commit: str,
                 default_test_command: str | None, allow_tests: bool = True):
        self.root = root.resolve()
        self.index = index
        self.settings = settings
        self.base_commit = base_commit
        self.default_test_command = default_test_command
        self.allow_tests = allow_tests
        self.originals: dict[str, str | None] = {}
        self.last_tests_passed: bool | None = None
        self.tests_run = 0

    # -- budgets -----------------------------------------------------------
    def files_changed(self) -> int:
        return sum(1 for rel, orig in self.originals.items() if self._current(rel) != orig)

    def lines_changed(self) -> int:
        return sum(_count_changed(orig, self._current(rel)) for rel, orig in self.originals.items())

    def _current(self, rel: str) -> str | None:
        p = self.root / rel
        return _read_preserving(p) if p.is_file() else None

    # -- phase 1: validate -------------------------------------------------
    def classify(self, name: str, args: dict) -> Action:
        if not isinstance(args, dict):
            raise ToolError("tool arguments must be an object")
        if name in READ_ONLY:
            return Action("read", name)
        if name == "run_tests":
            return self._classify_tests(args)
        if name in ("replace_in_file", "write_file"):
            return self._classify_edit(name, args)
        raise ToolError(f"unknown tool {name!r}")

    def _classify_tests(self, args: dict) -> Action:
        if not self.allow_tests:
            raise ToolError("Running tests is disabled on this server for this repository (PUBLIC_MODE). "
                            "Finish with your best analysis and say the change is unverified.")
        command = (args.get("command") or self.default_test_command or "").strip()
        if not command:
            raise ToolError("No test command was detected for this repository. Pass `command`, e.g. 'pytest -q'.")
        argv = parse_test_command(command)   # Refusal propagates
        return Action("test", "run_tests", target=command, preview=f"$ {command}", argv=argv)

    def _classify_edit(self, name: str, args: dict) -> Action:
        path = args.get("path")
        if not isinstance(path, str):
            raise ToolError("`path` is required")
        target = check_write_allowed(self.root, path)     # Refusal propagates
        rel = rel_posix(self.root, target)
        conf = args.get("confidence")
        confidence = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0 <= conf <= 1 else None
        if target.is_dir():
            raise ToolError(f"{rel} is a directory")
        if target.is_file() and not is_probably_text(target):
            raise Refusal(f"{rel} is a binary file")
        old = _read_preserving(target, strict=True) if target.is_file() else None
        new = self._compute_new(name, args, rel, old)
        if len(new.encode("utf-8")) > self.settings.max_file_write_bytes:
            raise Refusal(f"write of {len(new.encode('utf-8'))} bytes exceeds the {self.settings.max_file_write_bytes}-byte limit")
        self._check_budget(rel, old, new)
        return Action("edit", name, target=rel, preview=_text_diff(rel, old, new) or "(no textual change)",
                      protected=is_protected(rel), confidence=confidence)

    def _compute_new(self, name: str, args: dict, rel: str, old: str | None) -> str:
        if name == "write_file":
            content = args.get("content")
            if not isinstance(content, str):
                raise ToolError("`content` is required")
            return content
        if old is None:
            raise ToolError(f"{rel} does not exist. Use write_file to create it.")
        old_s, new_s = args.get("old_string"), args.get("new_string")
        if not isinstance(old_s, str) or not old_s or not isinstance(new_s, str):
            raise ToolError("`old_string` (non-empty) and `new_string` are required")
        if "\r\n" in old and "\r\n" not in old_s:      # keep Windows line endings working
            old_s, new_s = old_s.replace("\n", "\r\n"), new_s.replace("\n", "\r\n")
        count = old.count(old_s)
        if count == 0:
            raise ToolError(f"old_string was not found in {rel}. Re-read the file and copy the text exactly.")
        if count > 1:
            raise ToolError(f"old_string matches {count} places in {rel}. Include more surrounding lines so it matches once.")
        return old.replace(old_s, new_s, 1)

    def _check_budget(self, rel: str, old: str | None, new: str) -> None:
        touched = set(self.originals) | {rel}
        if len(touched) > self.settings.max_files_changed:
            raise Refusal(f"file budget exceeded: at most {self.settings.max_files_changed} files may change per run")
        originals = dict(self.originals)
        originals.setdefault(rel, old)
        total = 0
        for r, orig in originals.items():
            total += _count_changed(orig, new if r == rel else self._current(r))
        if total > self.settings.max_changed_lines:
            raise Refusal(f"change budget exceeded: at most {self.settings.max_changed_lines} changed lines per run (this edit would make {total})")

    # -- phase 2: execute --------------------------------------------------
    def execute(self, name: str, args: dict) -> str:
        if name == "update_plan":
            steps = args.get("steps")
            if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
                raise ToolError("`steps` must be a list of strings")
            return f"Plan recorded ({len(steps)} steps)."
        if name == "search_code":
            return self._search(args)
        if name == "list_files":
            return self._list(args)
        if name == "read_file":
            return self._read(args)
        if name == "git_diff":
            diff = gitops.diff_since(self.root, self.base_commit)
            return _truncate(diff) if diff.strip() else "(no changes yet)"
        if name == "finish":
            return "Acknowledged."
        if name in ("replace_in_file", "write_file"):
            return self._apply_edit(name, args)
        if name == "run_tests":
            return self._run_tests(args)
        raise ToolError(f"unknown tool {name!r}")

    def _search(self, args: dict) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("`query` is required")
        limit = args.get("limit") if isinstance(args.get("limit"), int) else 6
        results = self.index.search(query, max(1, min(limit, 10)))
        if not results:
            return "No matches. Try different words, or an identifier name."
        out = []
        for r in results:
            out.append(f"{r['path']}:{r['start_line']}-{r['end_line']}  (score {r['score']})\n{r['snippet']}\n")
        return _truncate("\n".join(out))

    def _list(self, args: dict) -> str:
        rel = args.get("path") or "."
        target = self.root if rel in (".", "") else safe_path(self.root, rel)
        if not target.is_dir():
            raise ToolError(f"{rel} is not a directory")
        lines: list[str] = []

        def walk(directory: Path, depth: int) -> None:
            for entry in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if entry.name in SKIP_DIRS or entry.is_symlink() or len(lines) >= 200:
                    continue
                shown = rel_posix(self.root, entry)
                if entry.is_dir():
                    lines.append(shown + "/")
                    if depth < 2:
                        walk(entry, depth + 1)
                else:
                    lines.append(shown)

        walk(target, 1)
        return "\n".join(lines) if lines else "(empty)"

    def _read(self, args: dict) -> str:
        path = args.get("path")
        if not isinstance(path, str):
            raise ToolError("`path` is required")
        target = check_read_allowed(self.root, path)
        if not target.is_file():
            raise ToolError(f"{path} is not a file")
        if not is_probably_text(target):
            raise ToolError(f"{path} looks like a binary file")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, args.get("start_line") if isinstance(args.get("start_line"), int) else 1)
        end = args.get("end_line") if isinstance(args.get("end_line"), int) else start + 199
        end = min(end, start + READ_MAX_LINES - 1, len(lines))
        if start > len(lines):
            return f"(file has only {len(lines)} lines)"
        body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
        if len(body) > READ_MAX_CHARS:
            body = body[:READ_MAX_CHARS] + "\n... [truncated]"
        suffix = f"\n[showing lines {start}-{end} of {len(lines)}]"
        return body + suffix

    def _apply_edit(self, name: str, args: dict) -> str:
        action = self._classify_edit(name, args)   # re-validate against the current file state
        target = self.root / action.target  # type: ignore[operator]
        old = _read_preserving(target, strict=True) if target.is_file() else None
        new = self._compute_new(name, args, action.target, old)  # type: ignore[arg-type]
        self.originals.setdefault(action.target, old)  # type: ignore[arg-type]
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(new.encode("utf-8"))
        return f"OK: {'created' if old is None else 'updated'} {action.target}"

    def _run_tests(self, args: dict) -> str:
        action = self._classify_tests(args)
        argv = action.argv or []
        kwargs: dict = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        env = scrubbed_env()
        try:
            try:
                proc = subprocess.Popen(argv, cwd=str(self.root), env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kwargs)
            except FileNotFoundError:
                self.last_tests_passed = None
                raise ToolError(f"The test runner {argv[0]!r} is not installed in RepoPilot's environment.") from None
            try:
                output, _ = proc.communicate(timeout=self.settings.test_timeout_s)
            except subprocess.TimeoutExpired:
                _kill(proc)
                proc.communicate()
                self.last_tests_passed = False
                return f"TIMEOUT: tests were killed after {self.settings.test_timeout_s}s"
        finally:
            shutil.rmtree(env.get("HOME", ""), ignore_errors=True)
        self.tests_run += 1
        self.last_tests_passed = proc.returncode == 0
        hint = ""
        if proc.returncode != 0 and ("ModuleNotFoundError" in output or "Cannot find module" in output
                                     or "No module named" in output):
            hint = ("\n[hint: a dependency is missing from RepoPilot's environment. That is an environment "
                    "limitation, not necessarily a bug in the code. Report it in your summary instead of editing dependency files.]")
        return f"exit code {proc.returncode}\n{_truncate(output)}{hint}"


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover
            proc.kill()
    except (ProcessLookupError, OSError):
        pass
