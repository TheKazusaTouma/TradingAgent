"""Validate the draft requirements against the repository schema.

This checks structure and duplicate identifiers. It does not approve a
requirement, execute the referenced tests, or verify course compliance.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]


def validate(requirements_path: Path, schema_path: Path) -> list[str]:
    document = yaml.safe_load(requirements_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)
    errors = [
        f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
        for error in jsonschema.Draft7Validator(schema).iter_errors(document)
    ]
    if isinstance(document, dict) and isinstance(document.get("requirements"), list):
        ids = [
            item["id"]
            for item in document["requirements"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        duplicates = sorted(key for key, count in Counter(ids).items() if key and count > 1)
        if duplicates:
            errors.append("Duplicate requirement IDs: " + ", ".join(duplicates))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=ROOT / "docs/req/requirements.yaml")
    parser.add_argument("--schema", type=Path, default=ROOT / "docs/req/req-schema.json")
    args = parser.parse_args()
    try:
        errors = validate(args.requirements, args.schema)
    except (OSError, ValueError, yaml.YAMLError, jsonschema.SchemaError) as error:
        print(f"Requirements validation could not run: {error}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Requirements match the repository schema; identifiers are unique.")
    print("This structural check does not approve requirements or run their acceptance tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
