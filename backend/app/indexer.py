"""Repository analysis + BM25 code retriever.

Lexical on purpose: no embedding service, fast, and strong for code where names matter.
`CodeIndex.search` has the interface a vector store would, so an embedding retriever
can be dropped in behind it.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .guardrails import is_secret_path

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "target", ".gradle", ".idea", ".vscode", ".next", ".nuxt", "vendor", "coverage", "htmlcov",
    ".tox", ".eggs", "site-packages",
}
MAX_FILE_BYTES = 200_000
MAX_FILES = 6000
MAX_TOTAL_BYTES = 40 * 1024 * 1024
CHUNK_LINES = 40
CHUNK_OVERLAP = 8

LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".c": "C",
    ".h": "C", ".cpp": "C++", ".cc": "C++", ".cs": "C#", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell",
    ".html": "HTML", ".css": "CSS", ".md": "Markdown", ".json": "JSON", ".yml": "YAML", ".yaml": "YAML",
    ".toml": "TOML", ".sql": "SQL",
}
_STOP = {
    "the", "a", "an", "is", "are", "was", "be", "to", "of", "in", "on", "for", "and", "or", "it", "this",
    "that", "with", "as", "at", "by", "from", "how", "does", "do", "what", "where", "which", "when", "why",
    "i", "my", "we", "you", "not", "no", "if", "then", "else", "should", "can", "fix", "bug", "make",
}
_SPLIT_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*|\d+")
_DEF_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|func|fn|interface|struct|type|const|let|var|public|private|static)\b")


def tokenize(text: str) -> list[str]:
    """Identifier-aware: `paginateItems` and `paginate_items` both yield `paginate`, `items`."""
    tokens: list[str] = []
    for word in _WORD.findall(text):
        for part in _SPLIT_CAMEL.split(word):
            part = part.lower()
            if len(part) >= 2 and part not in _STOP:
                tokens.append(part)
    return tokens


@dataclass
class Chunk:
    path: str
    start: int
    end: int
    text: str
    is_test: bool
    def_tokens: set[str]


def is_probably_text(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" not in fh.read(2048)
    except OSError:
        return False


def iter_repo_files(root: Path):
    count = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink() or is_secret_path(name):
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES or size == 0:
                continue
            if not is_probably_text(full):
                continue
            count += 1
            total += size
            if count > MAX_FILES or total > MAX_TOTAL_BYTES:
                return
            yield full, full.relative_to(root).as_posix()


def _looks_like_test(rel: str) -> bool:
    low = rel.lower()
    base = low.rsplit("/", 1)[-1]
    return ("/tests/" in f"/{low}" or "/test/" in f"/{low}" or base.startswith("test_") or base.endswith(("_test.py", "_test.go", ".test.js", ".test.ts", ".spec.js", ".spec.ts", "test.java")))


class CodeIndex:
    def __init__(self, root: Path):
        self.root = root
        self.chunks: list[Chunk] = []
        self._postings: dict[str, dict[int, int]] = defaultdict(dict)
        self._lengths: list[int] = []
        self.file_count = 0
        self._build()

    def _build(self) -> None:
        for full, rel in iter_repo_files(self.root):
            try:
                lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            self.file_count += 1
            step = CHUNK_LINES - CHUNK_OVERLAP
            for start in range(0, max(len(lines), 1), step):
                block = lines[start:start + CHUNK_LINES]
                if not block:
                    break
                text = "\n".join(block)
                def_tokens: set[str] = set()
                for line in block:
                    if _DEF_RE.match(line):
                        def_tokens.update(tokenize(line))
                idx = len(self.chunks)
                self.chunks.append(Chunk(rel, start + 1, start + len(block), text, _looks_like_test(rel), def_tokens))
                # path tokens count too, so "pricing" finds shop/pricing.py
                tokens = tokenize(text) + tokenize(rel) * 2
                self._lengths.append(len(tokens))
                for term, freq in Counter(tokens).items():
                    self._postings[term][idx] = freq
                if start + CHUNK_LINES >= len(lines):
                    break
        self._avg = (sum(self._lengths) / len(self._lengths)) if self._lengths else 1.0

    def search(self, query: str, limit: int = 8) -> list[dict]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms or not self.chunks:
            return []
        n = len(self.chunks)
        k1, b = 1.5, 0.75
        scores: dict[int, float] = defaultdict(float)
        for term in terms:
            posting = self._postings.get(term)
            if not posting:
                continue
            idf = math.log(1 + (n - len(posting) + 0.5) / (len(posting) + 0.5))
            for idx, freq in posting.items():
                denom = freq + k1 * (1 - b + b * self._lengths[idx] / self._avg)
                scores[idx] += idf * (freq * (k1 + 1)) / denom
                if term in self.chunks[idx].def_tokens:
                    scores[idx] += idf * 0.75  # definitions outrank mere call sites
        for idx in list(scores):
            if self.chunks[idx].is_test:
                scores[idx] *= 0.9
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        results, per_file = [], Counter()
        for idx, score in ranked:
            ch = self.chunks[idx]
            if per_file[ch.path] >= 2:
                continue
            per_file[ch.path] += 1
            snippet = "\n".join(ch.text.splitlines()[:12])
            results.append({"path": ch.path, "start_line": ch.start, "end_line": ch.end,
                            "score": round(score, 3), "snippet": snippet})
            if len(results) >= limit:
                break
        return results


# ----------------------------------------------------------------------------
# Repository analysis
# ----------------------------------------------------------------------------
def detect_test_command(root: Path) -> str | None:
    if (root / "package.json").is_file():
        try:
            scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {})
            if isinstance(scripts, dict) and "test" in scripts:
                return "npm test"
        except (OSError, ValueError):
            pass
    if any((root / f).is_file() for f in ("pytest.ini", "tox.ini", "conftest.py", "pyproject.toml", "setup.cfg", "setup.py", "requirements.txt")) \
            or (root / "tests").is_dir() or (root / "test").is_dir():
        for base, _dirs, files in os.walk(root):
            if any(part in SKIP_DIRS for part in Path(base).relative_to(root).parts):
                continue
            if any(f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py")) for f in files):
                return "pytest -q"
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (root / "pom.xml").is_file():
        return "mvn test"
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return "gradle test"
    mk = root / "Makefile"
    if mk.is_file() and re.search(r"^test\s*:", mk.read_text(encoding="utf-8", errors="replace"), re.M):
        return "make test"
    return None


def analyze_repo(root: Path) -> dict:
    langs: Counter = Counter()
    files = 0
    for _full, rel in iter_repo_files(root):
        files += 1
        langs[LANG_BY_EXT.get(Path(rel).suffix.lower(), "Other")] += 1
    top = [{"language": k, "files": v} for k, v in langs.most_common(5) if k != "Other"]
    return {"files": files, "languages": top, "test_command": detect_test_command(root)}
