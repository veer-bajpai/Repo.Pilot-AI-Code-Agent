import pytest

from app.guardrails import Refusal
from app.tools import ToolError


def edit(path, old, new, conf=0.9):
    return {"path": path, "old_string": old, "new_string": new, "confidence": conf, "reason": "test"}


class TestEdits:
    def test_replace_once(self, demo_repo, box_factory):
        repo, base = demo_repo
        box = box_factory(repo, base)
        args = edit("shop/pagination.py", "start = page * per_page", "start = (page - 1) * per_page")
        action = box.classify("replace_in_file", args)
        assert action.kind == "edit" and "+    start = (page - 1)" in action.preview and action.confidence == 0.9
        assert "(page - 1)" not in (repo / "shop/pagination.py").read_text()   # classify never writes
        assert box.execute("replace_in_file", args).startswith("OK")
        assert "(page - 1)" in (repo / "shop/pagination.py").read_text()
        assert box.files_changed() == 1 and box.lines_changed() == 2

    def test_must_match_exactly_once(self, demo_repo, box_factory):
        repo, base = demo_repo
        box = box_factory(repo, base)
        with pytest.raises(ToolError, match="not found"):
            box.classify("replace_in_file", edit("shop/pagination.py", "no such text", "x"))
        with pytest.raises(ToolError, match="matches"):
            box.classify("replace_in_file", edit("shop/pagination.py", "per_page", "n"))
        with pytest.raises(ToolError):
            box.classify("replace_in_file", edit("missing.py", "a", "b"))

    def test_crlf_files_still_match(self, tmp_path, box_factory, demo_repo):
        repo, base = demo_repo
        (repo / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
        box = box_factory(repo, base)
        box.execute("replace_in_file", edit("win.txt", "one\ntwo", "ONE\nTWO"))
        assert (repo / "win.txt").read_bytes() == b"ONE\r\nTWO\r\nthree\r\n"

    def test_non_utf8_files_are_not_edited(self, demo_repo, box_factory):
        repo, base = demo_repo
        (repo / "latin.txt").write_bytes("caf\xe9 au lait\n".encode("latin-1"))
        with pytest.raises(Refusal, match="UTF-8"):
            box_factory(repo, base).classify("replace_in_file", edit("latin.txt", "au lait", "noir"))

    @pytest.mark.parametrize("path", [".env", "../escape.txt", "/abs.txt", ".git/config", "key.pem"])
    def test_refused_paths(self, demo_repo, box_factory, path):
        repo, base = demo_repo
        with pytest.raises(Refusal):
            box_factory(repo, base).classify("write_file", {"path": path, "content": "x", "confidence": 1, "reason": "r"})

    def test_write_file_creates_and_previews(self, demo_repo, box_factory):
        repo, base = demo_repo
        box = box_factory(repo, base)
        args = {"path": "docs/new.md", "content": "hello\n", "confidence": 0.9, "reason": "docs"}
        assert "/dev/null" in box.classify("write_file", args).preview
        box.execute("write_file", args)
        assert (repo / "docs/new.md").read_text() == "hello\n"

    def test_size_limit(self, demo_repo, box_factory):
        repo, base = demo_repo
        with pytest.raises(Refusal, match="exceeds"):
            box_factory(repo, base).classify("write_file", {"path": "big.txt", "content": "x" * 300_000, "confidence": 1, "reason": "r"})

    def test_file_and_line_budgets(self, demo_repo, box_factory):
        repo, base = demo_repo
        box = box_factory(repo, base, max_files_changed=2)
        for i in range(2):
            box.execute("write_file", {"path": f"f{i}.txt", "content": "x\n", "confidence": 1, "reason": "r"})
        with pytest.raises(Refusal, match="file budget"):
            box.classify("write_file", {"path": "f3.txt", "content": "x\n", "confidence": 1, "reason": "r"})
        tight = box_factory(repo, base, max_changed_lines=5)
        with pytest.raises(Refusal, match="change budget"):
            tight.classify("write_file", {"path": "long.txt", "content": "line\n" * 50, "confidence": 1, "reason": "r"})

    def test_protected_flag(self, demo_repo, box_factory):
        repo, base = demo_repo
        a = box_factory(repo, base).classify("write_file", {"path": "requirements.txt", "content": "x\n", "confidence": 1, "reason": "r"})
        assert a.protected


class TestReading:
    def test_read_numbers_lines_and_blocks_secrets(self, demo_repo, box_factory):
        repo, base = demo_repo
        box = box_factory(repo, base)
        out = box.execute("read_file", {"path": "shop/pagination.py", "start_line": 1, "end_line": 3})
        assert "    1\t" in out and "total_pages" in out and "showing lines 1-3" in out
        (repo / ".env").write_text("TOKEN=abc")
        with pytest.raises(Refusal):
            box.execute("read_file", {"path": ".env"})

    def test_list_files_skips_junk(self, demo_repo, box_factory):
        repo, base = demo_repo
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "x.js").write_text("1")
        out = box_factory(repo, base).execute("list_files", {})
        assert "shop/pagination.py" in out and "node_modules" not in out

    def test_search(self, demo_repo, box_factory):
        repo, base = demo_repo
        assert "shop/pagination.py" in box_factory(repo, base).execute("search_code", {"query": "paginate items"})


class TestRunTests:
    def test_fail_then_pass(self, demo_repo, box_factory):
        repo, base = demo_repo
        box = box_factory(repo, base)
        assert box.execute("run_tests", {}).startswith("exit code 1") and box.last_tests_passed is False
        box.execute("replace_in_file", edit("shop/pagination.py", "start = page * per_page", "start = (page - 1) * per_page"))
        assert box.execute("run_tests", {}).startswith("exit code 0") and box.last_tests_passed is True

    def test_only_allowlisted_commands(self, demo_repo, box_factory):
        repo, base = demo_repo
        with pytest.raises(Refusal):
            box_factory(repo, base).classify("run_tests", {"command": "pip install evil && pytest"})

    def test_disabled_in_public_mode(self, demo_repo, box_factory):
        repo, base = demo_repo
        box = box_factory(repo, base)
        box.allow_tests = False
        with pytest.raises(ToolError, match="disabled"):
            box.classify("run_tests", {})

    def test_missing_dependency_hint(self, demo_repo, box_factory):
        repo, base = demo_repo
        (repo / "tests" / "test_dep.py").write_text("import surely_not_installed_pkg\n\ndef test_x():\n    pass\n")
        out = box_factory(repo, base).execute("run_tests", {})
        assert "hint" in out and "environment limitation" in out

    def test_timeout_kills_the_process(self, demo_repo, box_factory):
        repo, base = demo_repo
        (repo / "tests" / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(30)\n")
        box = box_factory(repo, base, test_timeout_s=2)
        out = box.execute("run_tests", {"command": "pytest -q tests/test_slow.py"})
        assert out.startswith("TIMEOUT") and box.last_tests_passed is False

    def test_test_process_cannot_see_secrets(self, demo_repo, box_factory, monkeypatch):
        repo, base = demo_repo
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-LEAKME-1234567890")
        (repo / "tests" / "test_env.py").write_text(
            "import os\n\ndef test_env():\n    assert 'GEMINI_API_KEY' not in os.environ\n    assert 'LEAKME' not in ''.join(os.environ.values())\n")
        out = box_factory(repo, base).execute("run_tests", {"command": "pytest -q tests/test_env.py"})
        assert out.startswith("exit code 0"), out
