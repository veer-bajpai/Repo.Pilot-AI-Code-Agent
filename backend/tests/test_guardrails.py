import os
import sys

import pytest

from app.guardrails import (
    Refusal, approval_policy, check_read_allowed, check_write_allowed, is_protected, parse_test_command,
    safe_path, scrubbed_env,
)


class TestPathSandbox:
    @pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", "C:\\Windows\\x", "~/.ssh/id_rsa", "", "  ", "a\x00b", "..\\x"])
    def test_rejects(self, tmp_path, bad):
        with pytest.raises(Refusal):
            safe_path(tmp_path, bad)

    def test_accepts_relative(self, tmp_path):
        assert safe_path(tmp_path, "src/./app.py") == (tmp_path / "src" / "app.py").resolve()

    def test_symlink_escape_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(Refusal):
            safe_path(repo, "link/secret.txt")


class TestWriteBlocklist:
    @pytest.mark.parametrize("name", [".env", ".ENV", ".env.local", "prod.env", "server.pem", "id_rsa", "id_ed25519.pub",
                                      "secrets.json", "credentials.json", ".npmrc", ".git/config", ".GIT/hooks/pre-commit", "a/.git/HEAD"])
    def test_blocked(self, tmp_path, name):
        with pytest.raises(Refusal):
            check_write_allowed(tmp_path, name)

    def test_env_example_is_fine(self, tmp_path):
        assert check_write_allowed(tmp_path, ".env.example").name == ".env.example"

    def test_symlink_into_git_is_blocked(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "innocent").symlink_to(tmp_path / ".git", target_is_directory=True)
        with pytest.raises(Refusal):
            check_write_allowed(tmp_path, "innocent/config")

    def test_reading_secrets_is_blocked(self, tmp_path):
        (tmp_path / ".env").write_text("KEY=1")
        with pytest.raises(Refusal):
            check_read_allowed(tmp_path, ".env")
        (tmp_path / ".env.example").write_text("KEY=")
        assert check_read_allowed(tmp_path, ".env.example")


def test_protected_files():
    for p in [".github/workflows/ci.yml", "Dockerfile", "docker-compose.yml", "requirements.txt", "package.json",
              "sub/pyproject.toml", "go.mod", "Makefile"]:
        assert is_protected(p), p
    assert not is_protected("src/app.py")


class TestTestCommands:
    @pytest.mark.parametrize("cmd,expect", [
        ("pytest", [sys.executable, "-m", "pytest"]),
        ("pytest -q tests/test_a.py::test_x", [sys.executable, "-m", "pytest", "-q", "tests/test_a.py::test_x"]),
        ("python -m pytest -x", [sys.executable, "-m", "pytest", "-x"]),
        ("python3 -m pytest", [sys.executable, "-m", "pytest"]),
        ("npm test", ["npm", "test"]), ("npm run test", ["npm", "test"]),
        ("go test ./...", ["go", "test", "./..."]), ("cargo test", ["cargo", "test"]),
        ("mvn test", ["mvn", "-q", "test"]), ("make test", ["make", "test"]),
    ])
    def test_allowed(self, cmd, expect):
        assert parse_test_command(cmd) == expect

    @pytest.mark.parametrize("cmd", [
        "pytest && rm -rf /", "pytest; ls", "pytest | cat", "pytest $(whoami)", "pytest `id`", "pytest > out.txt",
        "python -c 'import os'", "python evil.py", "pip install requests", "npm install", "npm run build", "curl http://x",
        "rm -rf /", "bash -c pytest", "sh test.sh", "make install", "pytest -c evil.ini", "pytest -p evilplugin",
        "pytest /etc/passwd", "pytest ../other", "pytest --rootdir=/", "go run main.go", "cargo run", "pytest -o addopts=x", "", "   ",
    ])
    def test_refused(self, cmd):
        with pytest.raises(Refusal):
            parse_test_command(cmd)


def test_scrubbed_env_hides_secrets(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")
    env = scrubbed_env()
    assert "PATH" in env
    assert not any("secret" in v.lower() for v in env.values())
    assert "GEMINI_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env


class TestApprovalPolicy:
    def p(self, **kw):
        base = dict(kind="edit", mode="auto", confidence=0.95, protected=False, threshold=0.8)
        base.update(kw)
        return approval_policy(**base)

    def test_ask_mode_always_asks(self):
        assert self.p(mode="ask").needs_approval
        assert self.p(mode="ask", kind="test").needs_approval

    def test_auto_confident_edit_passes(self):
        assert not self.p().needs_approval

    def test_auto_asks_when_unsure_or_protected_or_no_score(self):
        assert self.p(confidence=0.5).needs_approval
        assert self.p(confidence=None).needs_approval
        assert self.p(protected=True).needs_approval

    def test_auto_allows_allowlisted_tests(self):
        assert not self.p(kind="test", confidence=None).needs_approval
