"""JSON Schema gate: the strict provider-facing schemas are valid and keep null optional fields.

Run as a focused gate alongside Ruff and the test suite. It is not part of the offline suite
because it is a repository-wide contract check, not an agent test.
"""

from __future__ import annotations

import json

import jsonschema

from tradingagents.agents import contracts
from tradingagents.agents.contracts import strict_schema_for

AGENTS = (contracts.RESEARCH_MANAGER, contracts.TRADER, contracts.PORTFOLIO_MANAGER)
NUMERIC = {"entry_price", "stop_loss", "price_target"}


def main() -> int:
    for agent in AGENTS:
        schema = strict_schema_for(agent).model_json_schema()
        document = dict(schema, **{"$schema": "https://json-schema.org/draft/2020-12/schema"})
        jsonschema.Draft202012Validator.check_schema(document)
        required = set(schema.get("required", []))
        numeric = sorted(NUMERIC & set(schema["properties"]))
        for name in numeric:
            assert set(schema["properties"][name]["type"]) == {"number", "null"}, name
            assert name not in required, name
        print(f"{agent}: draft-2020-12 schema valid; optional numerics {numeric} allow number+null")
    jsonschema.Draft202012Validator.check_schema(
        dict(strict_schema_for(contracts.TRADER).model_json_schema() | {
            "$schema": "https://json-schema.org/draft/2020-12/schema"})
    )
    print(json.dumps({"status": "ok", "agents": len(AGENTS)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
