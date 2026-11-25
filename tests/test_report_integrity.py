"""Report integrity manifest — ADR-002 section 5, tests RI-1..RI-11.

Every expectation is written from the ADR and the repository code it cites. The suite drives
the real writer (``write_report_tree``) and the real command (``main``), never a copy of the
hashing logic, and it asserts a *non-empty* verified set: a manifest that lists nothing must
never be reported as valid (INV-17).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tradingagents import report_integrity
from tradingagents.report_integrity import (
    EXIT_OK,
    EXIT_PROBLEM,
    EXIT_UNREADABLE,
    MANIFEST_NAME,
    ManifestWriteError,
    main,
    verify_report_manifest,
    write_manifest,
)
from tradingagents.reporting import write_report_tree

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]

FULL_STATE = {
    "market_report": "MKT",
    "news_report": "NEWS",
    "investment_debate_state": {"bull_history": "BULL"},
    "investment_plan": "RM PLAN",
    "trader_investment_plan": "TRADE",
    "risk_debate_state": {"neutral_history": "NEUTRAL"},
    "final_trade_decision": "PM DECISION",
}


def _manifest(root):
    return json.loads((Path(root) / MANIFEST_NAME).read_text(encoding="utf-8"))


def _listed(root):
    return [entry["path"] for entry in _manifest(root)["files"]]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# RI-1 — saving writes a valid manifest
# ---------------------------------------------------------------------------

def test_ri1_saving_writes_a_manifest_matching_the_files_on_disk(tmp_path):
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=True)
    document = _manifest(tmp_path)

    assert document["manifest_version"] == 1
    assert document["algorithm"] == "sha256"
    assert document["generator"] == "tradingagents.report_integrity"
    assert document["ticker"] == "AAPL"

    on_disk = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    )
    listed = sorted(_listed(tmp_path))
    assert listed == on_disk
    assert listed  # never an empty manifest
    assert "complete_report.md" in listed
    assert "complete_report.html" in listed
    assert "1_analysts/market.md" in listed

    for entry in document["files"]:
        assert not Path(entry["path"]).is_absolute()
        assert "\\" not in entry["path"]
        assert entry["size"] == (tmp_path / entry["path"]).stat().st_size
        assert entry["sha256"] == _sha(tmp_path / entry["path"])
    assert document["file_count"] == len(listed)


def test_ri1_without_html_the_html_file_is_not_listed(tmp_path):
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=False)
    assert not (tmp_path / "complete_report.html").exists()
    assert "complete_report.html" not in _listed(tmp_path)


# ---------------------------------------------------------------------------
# RI-2 — a pristine tree verifies, and the verified count is the listed set
# ---------------------------------------------------------------------------

def test_ri2_a_pristine_tree_verifies_with_every_listed_file_checked(tmp_path):
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=True)
    listed = _listed(tmp_path)
    result = verify_report_manifest(tmp_path)

    assert result.ok is True
    assert result.problems == []
    assert result.checked == len(listed) > 0
    assert main([str(tmp_path)]) == EXIT_OK


@pytest.mark.parametrize("html", [True, False])
def test_ri2_the_writing_program_verifies_its_own_output(tmp_path, html):
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=html)
    listed = set(_listed(tmp_path))
    result = verify_report_manifest(tmp_path)
    assert result.ok and result.checked == len(listed)
    assert {entry["path"] for entry in result.manifest["files"]} == listed


# ---------------------------------------------------------------------------
# RI-2b — the relative-save-path regression the supervisor asked for
# ---------------------------------------------------------------------------

def test_ri2b_a_relative_save_path_under_the_cwd_writes_the_real_file_set(tmp_path, monkeypatch):
    """``save_path=Path("reports/NVDA")`` with cwd ``tmp_path`` must describe the real tree.

    Reported defect: the writer handed relative paths to the manifest writer, which prepended
    the root a second time, found nothing, skipped every file and left an empty manifest that
    then verified "successfully". The manifest must list the actual files and verification must
    check exactly that many.
    """
    monkeypatch.chdir(tmp_path)
    relative = Path("reports") / "NVDA"
    out = write_report_tree(FULL_STATE, "NVDA", relative, html=True)

    assert out == relative / "complete_report.md"          # the original return semantics
    assert (tmp_path / relative / "complete_report.md").is_file()

    document = _manifest(tmp_path / relative)
    listed = [entry["path"] for entry in document["files"]]
    expected = sorted(
        path.relative_to(tmp_path / relative).as_posix()
        for path in (tmp_path / relative).rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    )
    assert sorted(listed) == expected
    assert len(listed) == document["file_count"] >= 7
    assert "1_analysts/market.md" in listed

    result = verify_report_manifest(tmp_path / relative)
    assert result.ok is True, result.summary()
    assert result.checked == len(listed)


def test_ri2b_a_relative_save_path_detects_a_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_report_tree(FULL_STATE, "NVDA", Path("reports/NVDA"), html=True)
    target = tmp_path / "reports" / "NVDA" / "1_analysts" / "market.md"

    target.write_text("TAMPERED", encoding="utf-8")
    result = verify_report_manifest(tmp_path / "reports" / "NVDA")
    assert result.ok is False
    assert [(p.path, p.code) for p in result.problems] == [("1_analysts/market.md", "modified_file")]
    assert main([str(tmp_path / "reports" / "NVDA")]) == EXIT_PROBLEM

    target.unlink()
    result = verify_report_manifest(tmp_path / "reports" / "NVDA")
    assert result.ok is False
    assert [(p.path, p.code) for p in result.problems] == [("1_analysts/market.md", "missing_file")]


# ---------------------------------------------------------------------------
# RI-3 / RI-4 — an edited byte and a deleted file
# ---------------------------------------------------------------------------

def test_ri3_an_edited_file_is_reported_as_modified(tmp_path):
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=True)
    target = tmp_path / "1_analysts" / "market.md"
    original = target.read_bytes()
    target.write_bytes(original[::-1])
    result = verify_report_manifest(tmp_path)
    assert result.ok is False
    assert [(p.path, p.code) for p in result.problems] == [("1_analysts/market.md", "modified_file")]
    assert main([str(tmp_path)]) == EXIT_PROBLEM


def test_ri4_a_deleted_file_is_reported_as_missing(tmp_path):
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=True)
    (tmp_path / "complete_report.md").unlink()
    result = verify_report_manifest(tmp_path)
    assert result.ok is False
    assert [(p.path, p.code) for p in result.problems] == [("complete_report.md", "missing_file")]
    assert main([str(tmp_path)]) == EXIT_PROBLEM


# ---------------------------------------------------------------------------
# RI-5 — regeneration describes only the current run and keeps user notes
# ---------------------------------------------------------------------------

def test_ri5_regeneration_lists_only_the_current_run_and_keeps_user_notes(tmp_path):
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=True)
    (tmp_path / "notes.md").write_text("my own notes", encoding="utf-8")
    (tmp_path / "1_analysts" / "notes.md").write_text("analyst notes", encoding="utf-8")

    write_report_tree({"market_report": "NEW MARKET"}, "AAPL", tmp_path, html=False)

    listed = _listed(tmp_path)
    assert listed == ["1_analysts/market.md", "complete_report.md"]
    assert "complete_report.html" not in listed
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "my own notes"
    assert (tmp_path / "1_analysts" / "notes.md").read_text(encoding="utf-8") == "analyst notes"
    assert verify_report_manifest(tmp_path).ok is True


# ---------------------------------------------------------------------------
# RI-6 — absent, malformed or unsupported manifests fail clearly
# ---------------------------------------------------------------------------

def test_ri6_absent_manifest(tmp_path):
    (tmp_path / "complete_report.md").write_text("x", encoding="utf-8")
    result = verify_report_manifest(tmp_path)
    assert result.ok is False and result.checked == 0
    assert [p.code for p in result.problems] == ["missing_manifest"]
    assert main([str(tmp_path)]) == EXIT_UNREADABLE


def test_ri6_malformed_json(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert [p.code for p in verify_report_manifest(tmp_path).problems] == ["malformed_manifest"]
    assert main([str(tmp_path)]) == EXIT_UNREADABLE


def test_ri6_wrong_algorithm(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({
        "manifest_version": 1, "algorithm": "md5", "file_count": 0, "files": [],
    }), encoding="utf-8")
    assert [p.code for p in verify_report_manifest(tmp_path).problems] == ["unsupported_algorithm"]
    assert main([str(tmp_path)]) == EXIT_UNREADABLE


def test_ri6_wrong_version(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({
        "manifest_version": 2, "algorithm": "sha256", "file_count": 0, "files": [],
    }), encoding="utf-8")
    assert [p.code for p in verify_report_manifest(tmp_path).problems] == ["unsupported_manifest_version"]
    assert main([str(tmp_path)]) == EXIT_UNREADABLE


def test_ri6_missing_directory(tmp_path):
    assert [p.code for p in verify_report_manifest(tmp_path / "nope").problems] == ["missing_directory"]
    assert main([str(tmp_path / "nope")]) == EXIT_UNREADABLE


def test_ri6_usage_without_an_argument_is_a_failure():
    assert main([]) == EXIT_UNREADABLE


# ---------------------------------------------------------------------------
# RI-6b — a vacuous or inconsistent manifest is malformed, not a pass
# ---------------------------------------------------------------------------

def _write_json(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")


def test_ri6b_an_empty_list_is_not_a_valid_manifest(tmp_path):
    (tmp_path / "complete_report.md").write_text("x", encoding="utf-8")
    _write_json(tmp_path / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 0, "files": [],
    })
    result = verify_report_manifest(tmp_path)
    assert result.ok is False and result.checked == 0
    assert [p.code for p in result.problems] == ["malformed_manifest"]


def test_ri6b_an_entry_without_a_path_is_malformed(tmp_path):
    _write_json(tmp_path / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1,
        "files": [{"size": 1, "sha256": "0" * 64}],
    })
    codes = [p.code for p in verify_report_manifest(tmp_path).problems]
    assert codes == ["malformed_manifest"]


def test_ri6b_an_empty_path_entry_is_malformed(tmp_path):
    _write_json(tmp_path / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1,
        "files": [{"path": "  ", "size": 1, "sha256": "0" * 64}],
    })
    assert [p.code for p in verify_report_manifest(tmp_path).problems] == ["malformed_manifest"]


def test_ri6b_an_inconsistent_file_count_is_malformed(tmp_path):
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    _write_json(tmp_path / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 5,
        "files": [{"path": "a.md", "size": 1, "sha256": _sha(tmp_path / "a.md")}],
    })
    assert [p.code for p in verify_report_manifest(tmp_path).problems] == ["malformed_manifest"]


def test_ri6b_duplicate_paths_are_malformed(tmp_path):
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    entry = {"path": "a.md", "size": 1, "sha256": _sha(tmp_path / "a.md")}
    _write_json(tmp_path / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 2, "files": [entry, dict(entry)],
    })
    result = verify_report_manifest(tmp_path)
    assert result.ok is False
    assert [p.code for p in result.problems] == ["malformed_manifest"]


@pytest.mark.parametrize("bad", [
    {"path": "a.md", "size": "1", "sha256": "0" * 64},
    {"path": "a.md", "size": -1, "sha256": "0" * 64},
    {"path": "a.md", "size": True, "sha256": "0" * 64},
    {"path": "a.md", "size": 1.5, "sha256": "0" * 64},
    {"path": "a.md", "size": 1, "sha256": "abc"},
    {"path": "a.md", "size": 1, "sha256": None},
    {"path": "a.md", "size": 1},
])
def test_ri6b_an_invalid_size_or_digest_is_malformed(tmp_path, bad):
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    _write_json(tmp_path / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1, "files": [bad],
    })
    result = verify_report_manifest(tmp_path)
    assert result.ok is False
    assert "malformed_manifest" in {p.code for p in result.problems}


def test_ri6b_a_files_entry_that_is_not_an_object_is_malformed(tmp_path):
    _write_json(tmp_path / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1, "files": ["a.md"],
    })
    assert [p.code for p in verify_report_manifest(tmp_path).problems] == ["malformed_manifest"]


# ---------------------------------------------------------------------------
# RI-7 — path escapes are refused and never read
# ---------------------------------------------------------------------------

UNSAFE_ENTRIES = [
    "../outside.md",
    "/etc/passwd",
    "C:\\Windows\\win.ini",
    "\\\\server\\share\\x",
    "sub/../../x",
    "1_analysts/../../x",
]


@pytest.mark.parametrize("entry", UNSAFE_ENTRIES)
def test_ri7_an_escaping_entry_is_refused_and_not_read(tmp_path, entry):
    root = tmp_path / "report"
    root.mkdir()
    (tmp_path / "outside.md").write_text("secret outside the root", encoding="utf-8")
    _write_json(root / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1,
        "files": [{"path": entry, "size": 1, "sha256": "0" * 64}],
    })
    result = verify_report_manifest(root)
    assert result.ok is False and result.checked == 0
    assert [p.code for p in result.problems] == ["unsafe_path"]
    assert main([str(root)]) == EXIT_PROBLEM


def test_ri7_a_file_just_outside_the_root_is_not_read(tmp_path):
    root = tmp_path / "report"
    root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("top secret", encoding="utf-8")
    digest = _sha(secret)
    _write_json(root / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1,
        "files": [{"path": "../secret.md", "size": secret.stat().st_size, "sha256": digest}],
    })
    result = verify_report_manifest(root)
    assert result.ok is False
    # Not "modified": the path itself is refused before any read.
    assert [(p.path, p.code) for p in result.problems] == [("../secret.md", "unsafe_path")]


# ---------------------------------------------------------------------------
# RI-8 — symbolic links
# ---------------------------------------------------------------------------

def _symlink_or_skip(link: Path, target: Path, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable here ({type(exc).__name__}: {exc})")
    if not (os.path.islink(link) or link.is_symlink()):
        pytest.skip("the filesystem did not create a symbolic link")


def test_ri8_a_listed_file_that_is_a_link_outside_the_root_is_refused(tmp_path):
    root = tmp_path / "report"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("payload", encoding="utf-8")
    _symlink_or_skip(root / "link.md", outside)
    _write_json(root / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1,
        "files": [{"path": "link.md", "size": outside.stat().st_size, "sha256": _sha(outside)}],
    })
    result = verify_report_manifest(root)
    assert result.ok is False
    assert [(p.path, p.code) for p in result.problems] == [("link.md", "unsafe_path")]


def test_ri8_a_directory_link_inside_the_root_is_refused(tmp_path):
    root = tmp_path / "report"
    (root / "1_analysts").mkdir(parents=True)
    (root / "1_analysts" / "market.md").write_text("MKT", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "market.md").write_text("MKT", encoding="utf-8")
    _symlink_or_skip(root / "linked", outside, directory=True)
    _write_json(root / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1,
        "files": [{"path": "linked/market.md", "size": 3, "sha256": _sha(outside / "market.md")}],
    })
    result = verify_report_manifest(root)
    assert result.ok is False
    assert [p.code for p in result.problems] == ["unsafe_path"]


def test_ri8_the_manifest_itself_may_not_be_a_link(tmp_path):
    root = tmp_path / "report"
    (root / "1_analysts").mkdir(parents=True)
    (root / "1_analysts" / "market.md").write_text("MKT", encoding="utf-8")
    elsewhere = tmp_path / "manifest-target.json"
    write_manifest(root, "AAPL", [root / "1_analysts" / "market.md"])
    (root / MANIFEST_NAME).rename(elsewhere)
    # The target is a valid manifest for a *different* file, so a verifier that followed the
    # link would accept the pair and check the wrong tree.
    _symlink_or_skip(root / MANIFEST_NAME, elsewhere)
    result = verify_report_manifest(root)
    assert result.ok is False and result.checked == 0
    assert [(p.path, p.code) for p in result.problems] == [(MANIFEST_NAME, "unsafe_path")]
    assert main([str(root)]) == EXIT_PROBLEM


def test_ri8_a_link_pointing_inside_the_root_is_refused_but_a_plain_tree_verifies(tmp_path):
    """Links are never followed; the same content as a plain file verifies normally."""
    root = tmp_path / "report"
    root.mkdir()
    real = root / "real.md"
    real.write_text("MKT", encoding="utf-8")
    entry = {"path": "alias.md", "size": real.stat().st_size, "sha256": _sha(real)}
    _symlink_or_skip(root / "alias.md", real)
    _write_json(root / MANIFEST_NAME, {
        "manifest_version": 1, "algorithm": "sha256", "file_count": 1, "files": [entry],
    })
    assert [p.code for p in verify_report_manifest(root).problems] == ["unsafe_path"]
    (root / "alias.md").unlink()
    (root / "alias.md").write_text("MKT", encoding="utf-8")
    assert verify_report_manifest(root).ok is True


# ---------------------------------------------------------------------------
# RI-9 — the CLI exit codes and real command output
# ---------------------------------------------------------------------------

def _run_module(directory):
    return subprocess.run(
        [sys.executable, "-m", "tradingagents.report_integrity", str(directory)],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
    )


def test_ri9_the_real_command_reports_missing_and_modified_files(tmp_path):
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=True)

    clean = _run_module(tmp_path)
    assert clean.returncode == EXIT_OK
    assert "passed" in clean.stdout and f"match {MANIFEST_NAME}" in clean.stdout

    (tmp_path / "1_analysts" / "market.md").write_text("TAMPERED", encoding="utf-8")
    (tmp_path / "complete_report.md").unlink()
    broken = _run_module(tmp_path)
    assert broken.returncode == EXIT_PROBLEM
    assert "1_analysts/market.md" in broken.stderr and "size is 8 bytes" in broken.stderr
    assert "complete_report.md" in broken.stderr and "does not exist" in broken.stderr


def test_ri9_the_command_exits_two_for_a_missing_manifest(tmp_path):
    result = _run_module(tmp_path)
    assert result.returncode == EXIT_UNREADABLE
    assert MANIFEST_NAME in result.stderr


# ---------------------------------------------------------------------------
# RI-10 — the manifest is not the contract audit summary
# ---------------------------------------------------------------------------

def test_ri10_a_contract_summary_and_a_digest_manifest_coexist(tmp_path):
    from tradingagents.agents.contracts import ValidationRecord

    record = ValidationRecord(agent="Trader", mode="validate", stage="structured", ok=True)
    write_report_tree(FULL_STATE, "AAPL", tmp_path, html=False,
                      contract_records={"Trader": record})
    complete = (tmp_path / "complete_report.md").read_text(encoding="utf-8")
    assert "## Contract Validation" in complete
    document = _manifest(tmp_path)
    assert document["algorithm"] == "sha256"
    assert all(set(entry) == {"path", "size", "sha256"} for entry in document["files"])
    assert "Contract Validation" not in json.dumps(document)
    assert not any("sha256" in str(value) and " " in str(value) for value in [document["ticker"]])


# ---------------------------------------------------------------------------
# RI-11 — no network, no new dependency
# ---------------------------------------------------------------------------

def test_ri11_no_vendor_or_network_module_is_imported():
    code = (
        "import sys\n"
        "import tradingagents.report_integrity\n"
        "bad = [m for m in ('requests', 'httpx', 'openai', 'langchain_openai', "
        "'tradingagents.llm_clients', 'tradingagents.graph', 'tradingagents.dataflows') "
        "if m in sys.modules]\n"
        "print(','.join(bad) or 'clean')\n"
    )
    output = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                            text=True, encoding="utf-8")
    assert output.returncode == 0, output.stderr
    assert output.stdout.strip().splitlines()[-1] == "clean"


def test_ri11_the_module_uses_only_the_standard_library():
    source = Path(report_integrity.__file__).read_text(encoding="utf-8")
    top_level = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith(("from __future__", "from tradingagents"))
    }
    assert top_level <= {"contextlib", "hashlib", "json", "os", "re", "sys", "tempfile",
                         "collections", "dataclasses", "datetime", "pathlib", "typing"}, top_level


# ---------------------------------------------------------------------------
# RI-12 — the writer refuses to produce an incomplete or vacuous manifest
# ---------------------------------------------------------------------------

def test_ri12_a_missing_written_file_raises_instead_of_being_omitted(tmp_path):
    (tmp_path / "present.md").write_text("x", encoding="utf-8")
    with pytest.raises(ManifestWriteError):
        write_manifest(tmp_path, "AAPL", [tmp_path / "present.md", tmp_path / "missing.md"])
    assert not (tmp_path / MANIFEST_NAME).exists()


def test_ri12_a_missing_file_never_produces_a_vacuous_manifest(tmp_path):
    """Only an unwritten file listed: the failure is raised, no manifest is created."""
    with pytest.raises(ManifestWriteError):
        write_manifest(tmp_path, "AAPL", [tmp_path / "missing.md"])
    assert not (tmp_path / MANIFEST_NAME).exists()
    assert [p.code for p in verify_report_manifest(tmp_path).problems] == ["missing_manifest"]


def test_ri12_an_empty_file_list_raises(tmp_path):
    with pytest.raises(ManifestWriteError):
        write_manifest(tmp_path, "AAPL", [])
    assert not (tmp_path / MANIFEST_NAME).exists()


def test_ri12_a_file_outside_the_root_raises(tmp_path):
    root = tmp_path / "report"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ManifestWriteError):
        write_manifest(root, "AAPL", [outside])


def test_ri12_a_duplicate_path_raises(tmp_path):
    root = tmp_path / "report"
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    with pytest.raises(ManifestWriteError):
        write_manifest(root, "AAPL", [root / "a.md", "a.md"])


def test_ri12_a_preexisting_fixed_name_temp_file_is_not_overwritten(tmp_path):
    root = tmp_path / "report"
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    user_file = root / f".{MANIFEST_NAME}.tmp"
    user_file.write_text("a user's own notes", encoding="utf-8")
    write_manifest(root, "AAPL", [root / "a.md"])
    assert user_file.read_text(encoding="utf-8") == "a user's own notes"
    assert verify_report_manifest(root).ok is True
    assert [p.name for p in root.iterdir() if p.name.startswith("report-manifest-")] == []


def test_ri12_a_planted_symlink_at_the_temp_name_is_not_followed(tmp_path):
    root = tmp_path / "report"
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    target = tmp_path / "outside-target.txt"
    target.write_text("untouched", encoding="utf-8")
    planted = root / f".{MANIFEST_NAME}.tmp"
    try:
        planted.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable here ({type(exc).__name__})")
    write_manifest(root, "AAPL", [root / "a.md"])
    assert target.read_text(encoding="utf-8") == "untouched"
    assert verify_report_manifest(root).ok is True


# ---------------------------------------------------------------------------
# RI-13 — the link checks themselves, independent of the OS privilege
#
# Creating a symlink needs a privilege Windows does not grant by default, which would leave
# the refusal path asserted only when the OS allows it. These tests drive the same code with
# an explicitly patched inspection, so the *logic* of refusal is exercised everywhere; the
# precondition (can a link be created here?) is what RI-8 covers.
# ---------------------------------------------------------------------------

def test_ri13_a_link_on_the_path_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "report"
    (root / "1_analysts").mkdir(parents=True)
    (root / "1_analysts" / "market.md").write_text("MKT", encoding="utf-8")
    write_manifest(root, "AAPL", [root / "1_analysts" / "market.md"])
    real_islink = os.path.islink
    monkeypatch.setattr(
        os.path, "islink",
        lambda path: True if Path(path).name == "1_analysts" else real_islink(path),
    )
    result = verify_report_manifest(root)
    assert result.ok is False
    assert [(p.path, p.code) for p in result.problems] == [("1_analysts/market.md", "unsafe_path")]
    assert "symbolic link" in result.problems[0].detail


def test_ri13_a_linked_manifest_is_refused_before_it_is_read(tmp_path, monkeypatch):
    root = tmp_path / "report"
    root.mkdir()
    (root / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == MANIFEST_NAME)
    opened = []
    real_read_text = Path.read_text

    def guard(self, *args, **kwargs):
        opened.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guard)
    result = verify_report_manifest(root)
    assert result.ok is False and result.checked == 0
    assert [(p.path, p.code) for p in result.problems] == [(MANIFEST_NAME, "unsafe_path")]
    assert opened == []  # the manifest was never opened


def test_ri13_a_linked_report_directory_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "report"
    root.mkdir()
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "report")
    result = verify_report_manifest(root)
    assert [(p.code,) for p in result.problems] == [("unsafe_path",)]


def test_ri13_the_writer_refuses_a_linked_written_file(tmp_path, monkeypatch):
    root = tmp_path / "report"
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "a.md")
    with pytest.raises(ManifestWriteError, match="symbolic link"):
        write_manifest(root, "AAPL", [root / "a.md"])


def test_ri13_a_manifest_refused_through_a_link_never_reads_outside(tmp_path):
    """Static guarantee: the manifest path is checked with ``is_symlink`` before ``read_text``."""
    source = Path(report_integrity.__file__).read_text(encoding="utf-8")
    body = source.split("def verify_report_manifest")[1].split("\ndef ")[0]
    assert body.index("manifest_path.is_symlink()") < body.index("manifest_path.read_text")
