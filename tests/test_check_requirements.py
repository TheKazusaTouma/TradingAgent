"""Tests for tools/check_requirements.py.

These exercise the validator itself: a valid document passes, and each failure
mode is detected. They use synthetic fixtures and the real schema, so they fail
if the validator stops checking the thing it claims to check. They do not assert
that the committed requirements.yaml is approved or that its tests passed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tools import check_requirements

pytestmark = pytest.mark.unit

SCHEMA = check_requirements.DEFAULT_SCHEMA


def _minimal_requirement(**overrides) -> dict:
    """A single requirement that satisfies the schema with no optional fields set."""
    requirement = {
        "id": "FR-1",
        "type": "functional",
        "title": "A title",
        "statement": "A statement that is long enough to pass.",
        "priority": "must",
        "fit_criterion": "Fits when the condition is named; does not fit otherwise.",
        "verification": ["T-1"],
        "status": "proposed",
    }
    requirement.update(overrides)
    return requirement


def _write(path: Path, document: object) -> Path:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_valid_document_passes(tmp_path):
    path = _write(tmp_path / "requirements.yaml", {"requirements": [_minimal_requirement()]})
    assert check_requirements.validate(path) == []


def test_committed_requirements_file_is_structurally_valid():
    """The file this repo ships must pass the check, or CI's premise is false."""
    assert check_requirements.validate(check_requirements.DEFAULT_REQUIREMENTS) == []


def test_unknown_field_fails_schema(tmp_path):
    path = _write(
        tmp_path / "requirements.yaml",
        {"requirements": [_minimal_requirement(unrecognised="x")]},
    )
    errors = check_requirements.validate(path)
    assert any("unrecognised" in error for error in errors)


def test_bad_id_pattern_fails_schema(tmp_path):
    path = _write(tmp_path / "requirements.yaml", {"requirements": [_minimal_requirement(id="XX-1")]})
    errors = check_requirements.validate(path)
    assert any("XX-1" in error or "does not match" in error for error in errors)


def test_missing_required_field_fails_schema(tmp_path):
    requirement = _minimal_requirement()
    del requirement["verification"]
    path = _write(tmp_path / "requirements.yaml", {"requirements": [requirement]})
    errors = check_requirements.validate(path)
    assert any("verification" in error for error in errors)


def test_empty_requirements_array_fails_schema(tmp_path):
    path = _write(tmp_path / "requirements.yaml", {"requirements": []})
    errors = check_requirements.validate(path)
    assert errors


def test_duplicate_ids_fail(tmp_path):
    path = _write(
        tmp_path / "requirements.yaml",
        {"requirements": [_minimal_requirement(), _minimal_requirement(id="FR-1", title="Other")]},
    )
    errors = check_requirements.validate(path)
    assert any("Duplicate requirement ID" in error and "FR-1" in error for error in errors)


def test_metric_without_threshold_fails_dependency(tmp_path):
    path = _write(
        tmp_path / "requirements.yaml",
        {"requirements": [_minimal_requirement(metric="some observable")]},
    )
    errors = check_requirements.validate(path)
    assert any("FR-1" in error and "threshold" in error for error in errors)


def test_metric_with_threshold_passes(tmp_path):
    path = _write(
        tmp_path / "requirements.yaml",
        {"requirements": [_minimal_requirement(metric="some observable", threshold="0")]},
    )
    assert check_requirements.validate(path) == []


def test_malformed_yaml_is_unavailable_not_invalid(tmp_path):
    path = tmp_path / "requirements.yaml"
    path.write_text("requirements: [unclosed\n", encoding="utf-8")
    with pytest.raises(check_requirements.ValidationUnavailable):
        check_requirements.validate(path)


def test_missing_file_is_unavailable(tmp_path):
    with pytest.raises(check_requirements.ValidationUnavailable):
        check_requirements.validate(tmp_path / "does-not-exist.yaml")


def test_main_returns_one_on_invalid(tmp_path, capsys):
    path = _write(tmp_path / "requirements.yaml", {"requirements": []})
    assert check_requirements.main(["--requirements", str(path)]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_main_returns_two_on_unavailable(tmp_path, capsys):
    assert check_requirements.main(["--requirements", str(tmp_path / "missing.yaml")]) == 2
    assert "could not run" in capsys.readouterr().err


def test_main_returns_zero_on_valid(tmp_path):
    path = _write(tmp_path / "requirements.yaml", {"requirements": [_minimal_requirement()]})
    assert check_requirements.main(["--requirements", str(path)]) == 0


def test_schema_is_draft07():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert "draft-07" in schema.get("$schema", "")
