"""Validate docs/req/requirements.yaml against docs/req/req-schema.json.

This performs three structural checks and nothing else:

1. The document parses as YAML and validates against the JSON Schema with a real
   ``jsonschema`` draft-07 validator.
2. Requirement identifiers are unique.
3. Declared dependencies hold. The schema already encodes ``metric -> threshold``;
   this script enforces it explicitly so the rule survives a schema change and so
   a failure names the offending requirement rather than a path.

It does **not** approve a requirement, execute the referenced acceptance tests, or
prove course compliance. A green run means "the document is structurally sound",
which is exactly the honest claim printed on success.

Exit codes: 0 all checks pass; 1 a check failed; 2 the check could not run
(missing file, missing parser, invalid schema).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_REQUIREMENTS = ROOT / "docs/req/requirements.yaml"
DEFAULT_SCHEMA = ROOT / "docs/req/req-schema.json"

# Fields that are only meaningful together. The schema encodes ``metric ->
# threshold``; we re-check here with a human-readable message and keep the list
# in one place so the rule is explicit in code as well as in the schema.
DEPENDENCIES: tuple[tuple[str, str], ...] = (("metric", "threshold"),)


class ValidationUnavailable(Exception):
    """The check could not run at all (as opposed to the document being invalid)."""


def _load(path: Path) -> tuple[object, object]:
    import jsonschema
    import yaml

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationUnavailable(f"cannot read {path}: {error}") from error
    except yaml.YAMLError as error:
        raise ValidationUnavailable(f"{path} is not valid YAML: {error}") from error

    schema_path = DEFAULT_SCHEMA
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationUnavailable(f"cannot read {schema_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValidationUnavailable(f"{schema_path} is not valid JSON: {error}") from error

    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as error:
        raise ValidationUnavailable(f"{schema_path} is not a valid draft-07 schema: {error.message}") from error

    return document, schema


def _schema_errors(document: object, schema: object) -> list[str]:
    import jsonschema

    validator = jsonschema.Draft7Validator(schema)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        location = ".".join(map(str, error.absolute_path)) or "<root>"
        errors.append(f"{location}: {error.message}")
    return errors


def _requirements(document: object) -> list[dict]:
    if isinstance(document, dict) and isinstance(document.get("requirements"), list):
        return [item for item in document["requirements"] if isinstance(item, dict)]
    return []


def _duplicate_ids(document: object) -> list[str]:
    ids = [str(item["id"]) for item in _requirements(document) if isinstance(item.get("id"), str)]
    duplicates = sorted(identifier for identifier, count in Counter(ids).items() if count > 1)
    return [f"Duplicate requirement ID: {identifier}" for identifier in duplicates]


def _dependency_errors(document: object) -> list[str]:
    errors: list[str] = []
    for requirement in _requirements(document):
        identifier = requirement.get("id", "<unknown>")
        for trigger, required in DEPENDENCIES:
            if requirement.get(trigger) and not requirement.get(required):
                errors.append(
                    f"{identifier}: specifies '{trigger}' but is missing dependent '{required}'"
                )
    return errors


def validate(requirements_path: Path = DEFAULT_REQUIREMENTS) -> list[str]:
    """Return a list of human-readable problems; an empty list means valid."""
    document, schema = _load(requirements_path)
    errors = _schema_errors(document, schema)
    # The remaining checks are only trustworthy when the document has the shape we
    # expect; the schema check already reports structural breakage with useful paths.
    errors.extend(_duplicate_ids(document))
    errors.extend(_dependency_errors(document))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    args = parser.parse_args(argv)

    try:
        errors = validate(args.requirements)
    except ValidationUnavailable as error:
        print(f"Requirements validation could not run: {error}", file=sys.stderr)
        return 2

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"{len(errors)} problem(s) in {args.requirements}", file=sys.stderr)
        return 1

    print(f"OK: {args.requirements} matches req-schema.json; identifiers are unique; dependencies hold.")
    print("This structural check does not approve requirements or run their acceptance tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
