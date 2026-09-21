import subprocess
from pathlib import Path

import httpx
import pytest

from app import gitops
from app.demo.tasks import materialize
from app.indexer import CodeIndex, analyze_repo, detect_test_command, tokenize

HOSTS = ("github.com",)


class TestUrls:
    @pytest.mark.parametrize("raw", [
        "https://github.com/owner/repo", "https://github.com/owner/repo.git", "https://github.com/owner/repo/",
        "https://github.com/owner/repo/tree/main/src", "github.com/owner/repo", "git@github.com:owner/repo.git",
        "  https://GitHub.com/owner/repo  ", "https://github.com/owner/repo/blob/main/README.md?plain=1",
    ])
    def test_accepts_what_people_paste(self, raw):
        url, host, owner, repo = gitops.normalize_repo_url(raw, HOSTS)
        assert url == "https://github.com/owner/repo.git" and (owner, repo) == ("owner", "repo")

    @pytest.mark.parametrize("raw", [
        "", "http://github.com/o/r", "https://evil.com/o/r", "https://github.com/onlyowner", "ftp://github.com/o/r",
        "https://user:pw@github.com/o/r", "https://token@github.com/o/r", "https://github.com/o/r name",
        "file:///etc/passwd", "/etc/passwd", "https://github.com/../etc",
    ])
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            gitops.normalize_repo_url(raw, HOSTS)

    @pytest.mark.parametrize("raw", ["sk-proj-abcdefghijklmnop", "AIza" + "x" * 35, "ghp_" + "a" * 30, "github_pat_" + "b" * 30])
    def test_keys_are_not_repositories(self, raw):
        with pytest.raises(ValueError, match="API key or token"):
            gitops.normalize_repo_url(raw, HOSTS)


class TestGit:
    def test_diff_excludes_byproducts_and_includes_new_files(self, tmp_path):
        repo = tmp_path / "r"
        base = materialize("pagination", repo)
        (repo / "shop" / "__pycache__").mkdir()
        (repo / "shop" / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
        (repo / ".pytest_cache").mkdir()
        (repo / ".pytest_cache" / "v").write_text("x")
        (repo / "NEW.md").write_text("hi\n")
        diff = gitops.diff_since(repo, base)
        assert "NEW.md" in diff and "pycache" not in diff and ".pytest_cache" not in diff

    def test_branch_commit_works_without_git_identity_and_reset_keeps_branch(self, tmp_path, monkeypatch):
        empty = tmp_path / "emptyhome"
        empty.mkdir()
        for k in ("HOME", "XDG_CONFIG_HOME"):
            monkeypatch.setenv(k, str(empty))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        repo = tmp_path / "r"
        base = materialize("pagination", repo)
        (repo / "shop" / "pagination.py").write_text((repo / "shop" / "pagination.py").read_text().replace("page * per", "(page - 1) * per"))
        patch = gitops.diff_since(repo, base)
        gitops.reset_to(repo, base)
        assert "page * per_page" in (repo / "shop" / "pagination.py").read_text()
        sha = gitops.commit_patch_on_branch(repo, base, "repopilot/fix-1", patch, "Fix pagination")
        gitops.reset_to(repo, base)   # next run: must not drag the branch back
        assert gitops.run_git(["rev-parse", "repopilot/fix-1"], cwd=repo).strip() == sha
        assert gitops.run_git(["rev-parse", "HEAD"], cwd=repo).strip() == base

    def test_empty_patch_refused(self, tmp_path):
        repo = tmp_path / "r"
        base = materialize("pagination", repo)
        with pytest.raises(gitops.GitError):
            gitops.commit_patch_on_branch(repo, base, "b", "  \n", "m")

    def test_push_to_bare_remote(self, tmp_path):
        repo = tmp_path / "r"
        base = materialize("pagination", repo)
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        gitops.run_git(["remote", "add", "origin", str(bare)], cwd=repo)
        (repo / "a.txt").write_text("x\n")
        patch = gitops.diff_since(repo, base)
        gitops.commit_patch_on_branch(repo, base, "repopilot/x", patch, "add a")
        gitops.push_branch(repo, "repopilot/x", "ghp_" + "t" * 30)
        assert "repopilot/x" in gitops.run_git(["branch", "--list"], cwd=bare)

    def test_clone_local_source_and_missing_repo_message(self, tmp_path):
        src = tmp_path / "src"
        materialize("slugify", src)
        dest = tmp_path / "dest"
        branch = gitops.clone(str(src), dest)
        assert branch == "main" and (dest / "blog" / "slugs.py").exists()
        with pytest.raises(gitops.GitError):
            gitops.clone(str(tmp_path / "does-not-exist"), tmp_path / "d2")

    def test_pr_creation_and_token_never_leaks(self):
        token = "ghp_" + "s" * 30

        def handler(request: httpx.Request):
            assert request.headers["authorization"] == f"Bearer {token}"
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7"})

        url = gitops.open_github_pr(token, "o", "r", "b", "main", "t", "body", httpx.Client(transport=httpx.MockTransport(handler)))
        assert url.endswith("/pull/7")
        bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": f"nope {token}"})))
        with pytest.raises(gitops.GitError) as ei:
            gitops.open_github_pr(token, "o", "r", "b", "main", "t", "body", bad)
        assert token not in str(ei.value) and "403" in str(ei.value)


class TestIndex:
    def test_identifier_aware_tokens(self):
        assert tokenize("paginateItems") == tokenize("paginate_items") == ["paginate", "items"]
        assert "http" in tokenize("HTTPServer") and "server" in tokenize("HTTPServer")

    @pytest.mark.parametrize("demo,query,expected", [
        ("pagination", "paginate page items per_page", "shop/pagination.py"),
        ("pagination", "how are total pages computed", "shop/pagination.py"),
        ("slugify", "turn a title into a url slug", "blog/slugs.py"),
        ("discounts", "discount percent price", "store/pricing.py"),
    ])
    def test_definition_outranks_tests(self, tmp_path, demo, query, expected):
        repo = tmp_path / "r"
        materialize(demo, repo)
        assert CodeIndex(repo).search(query)[0]["path"] == expected

    def test_secrets_and_junk_are_not_indexed(self, tmp_path):
        (tmp_path / ".env").write_text("SUPER_SECRET_TOKEN=abc123\n")
        (tmp_path / "server.pem").write_text("-----BEGIN " + "PRIVATE KEY-----\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "junk.js").write_text("function supersecretjunk() {}\n")
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01supersecretjunk")
        (tmp_path / "ok.py").write_text("def visible_function():\n    return 1\n")
        idx = CodeIndex(tmp_path)
        assert idx.search("SUPER_SECRET_TOKEN abc123 private key supersecretjunk") == []
        assert idx.search("visible function")[0]["path"] == "ok.py"

    def test_test_command_detection(self, tmp_path):
        assert detect_test_command(tmp_path) is None
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
        assert detect_test_command(tmp_path) == "npm test"
        py = tmp_path / "py"
        materialize("pagination", py)
        assert detect_test_command(py) == "pytest -q"
        info = analyze_repo(py)
        assert info["test_command"] == "pytest -q" and info["languages"][0]["language"] == "Python"
