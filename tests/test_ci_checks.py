"""Tests for tools/ci_checks.py: type, dependency, and security checks.

The security checks are the interesting ones to test because they make claims
about the repository; here we verify they detect a planted problem (negative
control) and do not fire on ordinary code (positive control). The type and
dependency checks call real tools, so they are exercised only for their
"unavailable" path and for reporting shape.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import ci_checks

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def tracked_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    return tmp_path


def _track(repo: Path, relative: str, content: str = "x = 1\n") -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relative)


# ---------------------------------------------------------------------------
# Security: pattern scan
# ---------------------------------------------------------------------------


def test_clean_code_has_no_findings(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "def f(x):\n    return x + 1\n")
    assert ci_checks.scan_patterns(tracked_repo) == []


def test_eval_is_flagged(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "y = eval('1 + 1')\n")
    findings = ci_checks.scan_patterns(tracked_repo)
    assert findings and "eval" in findings[0]


def test_exec_and_os_system_are_flagged(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "import os\nexec('x=1')\nos.system('ls')\n")
    findings = ci_checks.scan_patterns(tracked_repo)
    assert len(findings) >= 2


def test_subprocess_shell_true_is_flagged(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "import subprocess\nsubprocess.run('ls', shell=True)\n")
    findings = ci_checks.scan_patterns(tracked_repo)
    assert any("dangerous call" in f for f in findings)


def test_subprocess_without_shell_is_not_flagged(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "import subprocess\nsubprocess.run(['ls'])\n")
    assert ci_checks.scan_patterns(tracked_repo) == []


def test_hardcoded_openai_key_is_flagged(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "KEY = 'sk-abc123abc123abc123abcdef'\n")
    findings = ci_checks.scan_patterns(tracked_repo)
    assert any("possible hard-coded secret" in f for f in findings)


def test_aws_key_is_flagged(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "AWS = 'AKIAIOSFODNN7EXAMPLE'\n")
    assert ci_checks.scan_patterns(tracked_repo)


def test_comment_examples_are_not_flagged(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "# never call eval(...) like this\nx = 1\n")
    assert ci_checks.scan_patterns(tracked_repo) == []


def test_only_tracked_files_are_scanned(tracked_repo):
    (tracked_repo / "untracked.py").write_text("eval('x')\n", encoding="utf-8")
    _git(tracked_repo, "add", "untracked.py")
    # tracked now; a plain untracked file should have been ignored
    (tracked_repo / "loose.py").write_text("eval('x')\n", encoding="utf-8")
    findings = ci_checks.scan_patterns(tracked_repo)
    assert len(findings) == 1  # only untracked.py, not loose.py


def test_checker_excludes_itself_and_its_tests(tracked_repo):
    _track(tracked_repo, "tools/ci_checks.py", "eval('x')\n")
    _track(tracked_repo, "tests/test_ci_checks.py", "eval('x')\n")
    assert ci_checks.scan_patterns(tracked_repo) == []


# ---------------------------------------------------------------------------
# Security: secret-like files
# ---------------------------------------------------------------------------


def test_tracked_dotenv_is_flagged(tracked_repo):
    _track(tracked_repo, ".env", "SECRET=1\n")
    assert any(".env" in f for f in ci_checks.scan_secret_files(tracked_repo))


def test_env_example_is_not_flagged(tracked_repo):
    _track(tracked_repo, ".env.example", "KEY=changeme\n")
    assert ci_checks.scan_secret_files(tracked_repo) == []


def test_tracked_pem_is_flagged(tracked_repo):
    _track(tracked_repo, "certs/server.pem", "x\n")
    assert any(".pem" in f for f in ci_checks.scan_secret_files(tracked_repo))


def test_ordinary_files_are_not_flagged(tracked_repo):
    _track(tracked_repo, "README.md", "hello\n")
    _track(tracked_repo, "pyproject.toml", "[project]\n")
    assert ci_checks.scan_secret_files(tracked_repo) == []


def test_run_security_passes_on_clean_repo(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "x = 1\n")
    code, report = ci_checks.run_security(tracked_repo)
    assert code == 0
    assert "findings: 0" in report and "security scope" in report


def test_run_security_fails_on_finding(tracked_repo):
    _track(tracked_repo, "pkg/a.py", "eval('x')\n")
    code, report = ci_checks.run_security(tracked_repo)
    assert code == 1
    assert "findings: 1" in report


# ---------------------------------------------------------------------------
# Type / deps reporting and unavailability
# ---------------------------------------------------------------------------


def test_type_check_reports_scope():
    try:
        import mypy  # noqa: F401
    except ImportError:
        pytest.skip("mypy not installed in this environment; the CI type-check job installs it")
    code, report = ci_checks.run_type(ci_checks.ROOT)
    assert code == 0
    for name in ci_checks.TYPE_SCOPE:
        assert name in report
    assert "not enforced" in report  # the debt disclaimer is always printed


def test_type_check_unavailable_when_scope_missing(tmp_path):
    with pytest.raises(ci_checks.CheckUnavailable):
        ci_checks.run_type(tmp_path)


def test_deps_check_runs():
    code, report = ci_checks.run_deps(ci_checks.ROOT)
    assert code in (0, 1)
    assert isinstance(report, str)


def test_cli_unknown_check_exits_two():
    with pytest.raises(SystemExit):
        ci_checks.main(["not-a-check"])
