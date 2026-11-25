"""Tests for tools/ai_use_log.py.

Two layers:

* Pure parsing/classification/schema tests: no git, no subprocess. They pin the
  change-detection rules (added/modified/deleted/renamed paths) and the log schema.
* An end-to-end test that builds a throwaway git repository and runs the real
  ``git diff`` path, so the base/head comparison is exercised, not mocked.

These prove the *policy they claim to enforce* and no more: that a governed change
must come with a log update, and that the log is well-formed. They do not prove
who authored a change or that any transcript exists.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import ai_use_log

pytestmark = pytest.mark.unit


VALID_LOG = {
    "reviewed_source_revision": "a60f3507849108dd2de55880c5597db32539fbdf",
    "records": [
        {"id": "AI-1", "scope": "s", "tool": "OpenCode", "basis": "b", "verification": "v"},
    ],
}


def _write_log(path: Path, document: object) -> Path:
    import yaml

    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Parsing NUL-delimited git --name-status -z output
# ---------------------------------------------------------------------------


def _z(*fields: str) -> bytes:
    """Build ``-z`` output: NUL-terminated fields (git emits a trailing NUL)."""
    return b"".join(f.encode("utf-8") + b"\x00" for f in fields)


def test_parse_added_modified_deleted():
    output = _z("A", "tradingagents/new.py", "M", "cli/run.py", "D", "old.py")
    changes = ai_use_log.parse_name_status_z(output)
    assert [c.status for c in changes] == ["A", "M", "D"]
    assert changes[0].paths == ["tradingagents/new.py"]
    assert changes[2].paths == ["old.py"]


def test_parse_rename_keeps_both_paths():
    output = _z(
        "R100",
        "tradingagents/agents/analysts/old.py",
        "tradingagents/agents/analysts/new.py",
    )
    (change,) = ai_use_log.parse_name_status_z(output)
    assert change.status == "R100"
    assert change.paths == [
        "tradingagents/agents/analysts/old.py",
        "tradingagents/agents/analysts/new.py",
    ]


def test_parse_copy_keeps_both_paths():
    output = _z("C075", "src/a.ts", "src/b.ts")
    (change,) = ai_use_log.parse_name_status_z(output)
    assert change.status == "C075"
    assert change.paths == ["src/a.ts", "src/b.ts"]


def test_parse_non_ascii_and_spaces_are_preserved():
    """NUL-delimited output must not C-quote or mangle non-ASCII/space paths."""
    output = _z("A", "tests/test_中文 模块.py", "M", "tools/my file.sh")
    changes = ai_use_log.parse_name_status_z(output)
    assert changes[0].paths == ["tests/test_中文 模块.py"]
    assert changes[1].paths == ["tools/my file.sh"]


def test_parse_rename_non_ascii():
    output = _z("R087", "scripts/旧 脚本.sh", "scripts/新 脚本.sh")
    (change,) = ai_use_log.parse_name_status_z(output)
    assert change.paths == ["scripts/旧 脚本.sh", "scripts/新 脚本.sh"]


def test_parse_empty_output():
    assert ai_use_log.parse_name_status_z(b"") == []


def test_parse_ignores_blank_status():
    assert ai_use_log.parse_name_status_z(_z("", "A", "x.py")) == [
        ai_use_log.Change("A", ["x.py"])
    ]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_application_code_is_governed():
    changes = [ai_use_log.Change("M", ["tradingagents/graph/trading_graph.py"])]
    assert ai_use_log.classify(changes).governed


def test_cli_and_toplevel_python_is_governed():
    cli = ai_use_log.classify([ai_use_log.Change("M", ["cli/main.py"])])
    top = ai_use_log.classify([ai_use_log.Change("A", ["main.py"])])
    assert cli.code and top.code


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_ai_use_log.py",
        "tests/sub/test_nested.pyi",
        "tools/ci_checks.py",
        "scripts/eval.sh",
        "tradingagents/agents/contracts.py",
        "web/app.js",
        "web/app.jsx",
        "web/app.ts",
        "web/app.tsx",
        "Dockerfile",
        "Dockerfile.dev",
        "deep/nested/pkg/module.py",
    ],
)
def test_source_code_anywhere_is_governed(path):
    """Code coverage is repo-wide by extension, not limited to the package."""
    assert ai_use_log.classify([ai_use_log.Change("A", [path])]).code, path


def test_prompt_files_are_governed():
    for path in (
        "tradingagents/agents/analysts/prompts/market.md",
        "prompts/system.j2",
        "tradingagents/agents/analysts/news_prompt.py",
    ):
        assert ai_use_log.classify([ai_use_log.Change("M", [path])]).prompt, path


def test_model_config_is_governed():
    for path in ("tradingagents/default_config.py", "tradingagents/llm_clients/model_catalog.py", ".env.example"):
        assert ai_use_log.classify([ai_use_log.Change("M", [path])]).model_config, path


def test_rename_out_of_code_is_still_governed():
    """A rename whose OLD side is code must count, or moving a module out of a
    governed extension would silently escape the rule."""
    change = ai_use_log.Change(
        "R100", ["tradingagents/agents/x.py", "docs/x.py.bak"]
    )
    assert ai_use_log.classify([change]).governed


def test_rename_of_nonascii_code_is_governed():
    change = ai_use_log.Change("R090", ["tests/测 试.py", "tests/新 名字.py"])
    assert ai_use_log.classify([change]).governed


def test_docs_only_change_is_not_governed():
    changes = [
        ai_use_log.Change("M", ["docs/architecture/invariants.md"]),
        ai_use_log.Change("M", ["README.md"]),
    ]
    assert not ai_use_log.classify(changes).governed


def test_log_change_is_recognised():
    assert ai_use_log.classify([ai_use_log.Change("M", [ai_use_log.LOG_PATH])]).log


def test_log_path_is_not_classified_as_code():
    """The log itself must not be treated as code even though it is checked."""
    classification = ai_use_log.classify([ai_use_log.Change("M", [ai_use_log.LOG_PATH])])
    assert classification.log
    assert not classification.code


def test_deletion_of_code_is_governed():
    assert ai_use_log.classify([ai_use_log.Change("D", ["tradingagents/old.py"])]).governed


def test_deletion_of_nested_test_is_governed():
    assert ai_use_log.classify([ai_use_log.Change("D", ["tests/test_old.py"])]).governed


# ---------------------------------------------------------------------------
# Log schema validation
# ---------------------------------------------------------------------------


def test_valid_log_document():
    assert ai_use_log.validate_log(VALID_LOG) == []


def test_log_must_be_mapping():
    assert ai_use_log.validate_log(["not", "a", "mapping"])


def test_missing_revision_fails():
    document = dict(VALID_LOG)
    del document["reviewed_source_revision"]
    assert any("reviewed_source_revision" in e for e in ai_use_log.validate_log(document))


def test_bad_revision_fails():
    document = {**VALID_LOG, "reviewed_source_revision": "not-a-sha"}
    assert any("reviewed_source_revision" in e for e in ai_use_log.validate_log(document))


def test_empty_records_fail():
    assert ai_use_log.validate_log({**VALID_LOG, "records": []})


def test_missing_record_field_fails():
    document = {**VALID_LOG, "records": [{"id": "AI-1", "tool": "t"}]}
    errors = ai_use_log.validate_log(document)
    assert any("scope" in e for e in errors) and any("basis" in e for e in errors)


def test_duplicate_record_id_fails():
    document = {
        **VALID_LOG,
        "records": [
            {"id": "AI-1", "scope": "s", "tool": "t", "basis": "b"},
            {"id": "AI-1", "scope": "s2", "tool": "t", "basis": "b"},
        ],
    }
    assert any("reuses id" in e for e in ai_use_log.validate_log(document))


def test_blank_field_fails():
    document = {**VALID_LOG, "records": [{"id": "AI-1", "scope": "  ", "tool": "t", "basis": "b"}]}
    assert any("scope" in e for e in ai_use_log.validate_log(document))


# ---------------------------------------------------------------------------
# evaluate(): combine detection + log presence + validity
# ---------------------------------------------------------------------------


def test_governed_change_without_log_update_fails(tmp_path):
    log = _write_log(tmp_path / "ai-use-log.yaml", VALID_LOG)
    changes = [ai_use_log.Change("M", ["tradingagents/agents/contracts.py"])]
    original = ai_use_log.diff_changes
    ai_use_log.diff_changes = lambda base, head, cwd=ai_use_log.ROOT: changes
    try:
        problems = ai_use_log.evaluate("base", "head", log_path=log)
    finally:
        ai_use_log.diff_changes = original
    assert problems and "without an AI-use log update" in problems[0]


def test_governed_change_with_log_update_passes(tmp_path):
    log = _write_log(tmp_path / "ai-use-log.yaml", VALID_LOG)
    changes = [
        ai_use_log.Change("M", ["tradingagents/agents/contracts.py"]),
        ai_use_log.Change("M", [ai_use_log.LOG_PATH]),
    ]
    original = ai_use_log.diff_changes
    ai_use_log.diff_changes = lambda base, head, cwd=ai_use_log.ROOT: changes
    try:
        assert ai_use_log.evaluate("base", "head", log_path=log) == []
    finally:
        ai_use_log.diff_changes = original


def test_malformed_log_fails_even_without_governed_change(tmp_path):
    path = tmp_path / "ai-use-log.yaml"
    path.write_text("records: [unclosed\n", encoding="utf-8")
    with pytest.raises(ai_use_log.Unavailable):
        ai_use_log.evaluate("base", "head", log_path=path)


def test_docs_only_change_needs_no_log_update(tmp_path):
    log = _write_log(tmp_path / "ai-use-log.yaml", VALID_LOG)
    changes = [ai_use_log.Change("M", ["docs/architecture/invariants.md"])]
    original = ai_use_log.diff_changes
    ai_use_log.diff_changes = lambda base, head, cwd=ai_use_log.ROOT: changes
    try:
        assert ai_use_log.evaluate("base", "head", log_path=log) == []
    finally:
        ai_use_log.diff_changes = original


# ---------------------------------------------------------------------------
# End to end against a real throwaway git repository
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    # Force the default C-quoting that -z must bypass: with -z, paths come raw.
    _git(tmp_path, "config", "core.quotepath", "true")
    (tmp_path / "tradingagents").mkdir()
    (tmp_path / "tradingagents" / "a.py").write_text("x = 1\n")
    (tmp_path / "docs").mkdir()
    _write_log(tmp_path / ai_use_log.LOG_PATH, VALID_LOG)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _evaluate(repo: Path, base: str) -> list[str]:
    head = _git(repo, "rev-parse", "HEAD")
    return ai_use_log.evaluate(base, head, log_path=repo / ai_use_log.LOG_PATH, cwd=repo)


def test_e2e_code_change_without_log_fails(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tradingagents" / "a.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "code only")
    problems = _evaluate(repo, base)
    assert problems and "without an AI-use log update" in problems[0]


def test_e2e_code_change_with_log_update_passes(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tradingagents" / "a.py").write_text("x = 3\n")
    updated = {**VALID_LOG, "records": [*VALID_LOG["records"],
               {"id": "AI-2", "scope": "s2", "tool": "t", "basis": "b"}]}
    _write_log(repo / ai_use_log.LOG_PATH, updated)
    _git(repo, "commit", "-qam", "code plus log")
    assert _evaluate(repo, base) == []


def test_e2e_rename_out_of_package_is_governed(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tradingagents" / "a.py").rename(repo / "moved.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rename out")
    problems = _evaluate(repo, base)
    assert problems and "without an AI-use log update" in problems[0]


@pytest.mark.parametrize(
    "relative",
    [
        "tests/test_added.py",
        "tools/new_tool.py",
        "scripts/run.sh",
        "web/app.ts",
    ],
)
def test_e2e_nested_source_addition_is_governed(repo, relative):
    """Additions under tests/, tools/, scripts/ and other modules are code."""
    base = _git(repo, "rev-parse", "HEAD")
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {relative}")
    problems = _evaluate(repo, base)
    assert problems and "without an AI-use log update" in problems[0]


def test_e2e_nested_source_deletion_is_governed(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tradingagents" / "a.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete code")
    problems = _evaluate(repo, base)
    assert problems and "without an AI-use log update" in problems[0]


def test_e2e_nonascii_space_filename_change_is_detected(repo):
    """A non-ASCII, space-containing code path with core.quotepath=true must
    still be classified as code (the -z output is not C-quoted)."""
    target = repo / "tests" / "测 试 file.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add nonascii test")
    base = _git(repo, "rev-parse", "HEAD~1")
    problems = _evaluate(repo, base)
    assert problems and "without an AI-use log update" in problems[0]
    assert "测 试 file.py" in problems[0]


def test_e2e_nonascii_rename_is_detected(repo):
    """Renaming a non-ASCII code file must still count through both paths."""
    old = repo / "tests" / "旧 名字.py"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add old")
    base = _git(repo, "rev-parse", "HEAD")
    new = repo / "tests" / "新 名字.py"
    old.rename(new)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rename nonascii")
    problems = _evaluate(repo, base)
    assert problems and "without an AI-use log update" in problems[0]


def test_e2e_rename_into_noncode_is_still_governed(repo):
    """Moving a .py file to a .txt keeps the old code path in the diff."""
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tradingagents" / "a.py").rename(repo / "notes.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rename to txt")
    problems = _evaluate(repo, base)
    assert problems and "without an AI-use log update" in problems[0]


def test_e2e_bad_ref_is_unavailable(repo):
    with pytest.raises(ai_use_log.Unavailable):
        ai_use_log.evaluate("nonexistent-ref", "HEAD", log_path=repo / ai_use_log.LOG_PATH, cwd=repo)


def test_cli_returns_two_on_bad_ref(repo, capsys):
    code = ai_use_log.main(["--base", "nope", "--head", "HEAD",
                            "--log", str(repo / ai_use_log.LOG_PATH), "--repo", str(repo)])
    assert code == 2
    assert "could not run" in capsys.readouterr().err
