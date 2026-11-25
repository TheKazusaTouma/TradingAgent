"""Fixed offline evaluation replay.

Purpose
-------
Prompt and model-configuration changes can quietly alter what the agents are told
or how a model is called without touching a data path. This replay pins a fixed,
concrete set of existing, deterministic pytest **node IDs** that exercise the
actual model/agent/prompt paths with scripted models and no credentials or
network, and fails if any of them regresses or disappears.

It may run unconditionally (not only when prompt/model files change) so a
regression is caught regardless of which PR introduced it.

What is replayed (fixed node IDs, not whole modules)
----------------------------------------------------
Each entry is a specific test, so removing an individual case is detected (a
missing node ID makes pytest report it and the replay fails), rather than being
masked by other tests in the same module. The set covers:

* full-graph runs with scripted models (free-text and structured decisions) - the
  closest offline stand-in for a real run;
* analyst briefs reach the model as text and no prompt asks for unread output;
* structured-output agents receive their constraint prompts;
* model-name/provider validation behaviour;
* the contract layer the agents call through (optional reports, zero calls on
  invalid input).

What this actually tests, precisely
-----------------------------------
The models here are *scripted fixtures*, not live providers. A green replay means
the fixed behaviours these tests assert still hold - the graph still reaches a
decision, the prompts are still delivered intact, the validators still behave.
It is NOT evidence of live-provider correctness, model output quality, latency or
cost. Those are out of scope for an offline replay and are not claimed.

Limitations
-----------
- Offline only: vendors are mocked and sockets are refused by tests/conftest.py.
- The set is frozen here; changing it must be an explicit PR that also updates
  docs/course-ci-gates.md, so the evaluation cannot silently shrink.
- Depends on the repository's pytest configuration; run from the repo root with
  dev dependencies installed.

Exit codes: 0 all node IDs pass; 1 a node ID failed or no longer exists; 2 the
replay could not run (missing pytest / missing repository).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Frozen: concrete node IDs. Edit only in a PR that also updates
# docs/course-ci-gates.md and explains why the evaluation set changed.
REPLAY: tuple[str, ...] = (
    # Whole graph, scripted models (free-text + structured decisions).
    "tests/test_graph_end_to_end.py::test_a_full_run_reaches_a_logged_decision[free-text]",
    "tests/test_graph_end_to_end.py::test_a_full_run_reaches_a_logged_decision[structured]",
    "tests/test_graph_end_to_end.py::test_the_last_turn_is_offered_no_tools_and_reads_its_tool_results_as_text",
    # Analyst briefs are delivered as text and no unread trade call is requested.
    "tests/test_prompt_integrity.py::test_an_analyst_brief_is_text_not_a_python_object[market_analyst]",
    "tests/test_prompt_integrity.py::test_an_analyst_is_not_asked_for_a_trade_call_nothing_reads[market_analyst]",
    # Structured-output agents receive their constraint prompt.
    "tests/test_structured_agent_prompts.py::test_trader_prompt_states_constraint",
    "tests/test_structured_agent_prompts.py::test_research_manager_prompt_states_constraint",
    "tests/test_structured_agent_prompts.py::test_portfolio_manager_prompt_states_constraint",
    "tests/test_structured_agent_prompts.py::test_constraint_text_is_unambiguous",
    # Model/provider validation behaviour.
    "tests/test_model_validation.py::ModelValidationTests::test_unknown_model_emits_warning_for_strict_provider",
    "tests/test_model_validation.py::test_an_explicit_alias_of_a_listed_model_is_known",
    # Contract layer: optional reports never fail; invalid input costs zero calls.
    "tests/test_agent_contracts.py::test_t2_optional_analyst_reports_never_fail[omitted-Research Manager]",
    "tests/test_agent_contracts.py::test_t3_invalid_input_costs_zero_calls[Research Manager-state0-investment_debate_state]",
    "tests/test_agent_contracts.py::test_t29_real_aimessage_json_and_fenced_json_are_parsed",
)


class ReplayUnavailable(Exception):
    """The replay could not run at all."""


def _files_exist(cwd: Path) -> None:
    modules = sorted({node.split("::", 1)[0] for node in REPLAY})
    missing = [name for name in modules if not (cwd / name).exists()]
    if missing:
        raise ReplayUnavailable(
            "evaluation replay references files that no longer exist: "
            f"{missing}. Update tools/eval_replay.py REPLAY and docs/course-ci-gates.md "
            "together if you intend to change the evaluation set."
        )


def _verify_collects(cwd: Path) -> None:
    """Ensure every node ID still exists, so removal of a single case fails loudly.

    ``pytest --collect-only <node-id>`` exits non-zero (usage error) when a node
    ID is unknown. We check collection before running, so the failure names the
    missing case rather than only a generic "no tests ran".
    """
    missing: list[str] = []
    for node in REPLAY:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", node],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            missing.append(node)
    if missing:
        raise ReplayUnavailable(
            "these evaluation node IDs no longer exist (case removed or renamed): "
            + ", ".join(missing)
        )


def run(cwd: Path = ROOT) -> int:
    try:
        import pytest  # noqa: F401
    except ImportError as error:
        raise ReplayUnavailable("pytest is not installed") from error

    _files_exist(cwd)
    _verify_collects(cwd)

    print(f"Offline evaluation replay: {len(REPLAY)} fixed node IDs (scripted models, no network):")
    for node in REPLAY:
        print(f"  - {node}")
    print(
        "Limitation: green here proves these scripted-fixture behaviours hold; it does "
        "not test live model quality, latency or cost."
    )

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *REPLAY],
        cwd=cwd,
        check=False,
    )
    if completed.returncode == 0:
        print("replay: PASS")
    else:
        print("replay: FAIL", file=sys.stderr)
    return 0 if completed.returncode == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        return run(cwd=args.repo)
    except ReplayUnavailable as error:
        print(f"evaluation replay could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
