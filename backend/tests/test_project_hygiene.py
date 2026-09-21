import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from check_no_secrets import scan  # noqa: E402


def test_repository_contains_no_secrets():
    assert scan(ROOT) == []


def test_scanner_actually_detects_secrets(tmp_path):
    (tmp_path / "a.py").write_text('KEY = "AIza' + "A" * 35 + '"\n')
    (tmp_path / "b.py").write_text('KEY = "sk-ant-api03-' + "B" * 60 + '"\n')
    (tmp_path / ".env").write_text("GEMINI_API_KEY=x\n")
    (tmp_path / ".env.example").write_text("GEMINI_MODEL=gemini-3.8-flash\n")
    found = " ".join(scan(tmp_path))
    assert "a.py: looks like a Google API key" in found and "b.py" in found
    assert ".env: environment file" in found and ".env.example" not in found


def test_offline_evals_pass():
    proc = subprocess.run([sys.executable, "-m", "evals.run"], cwd=ROOT / "backend", capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL EVALS PASSED" in proc.stdout
