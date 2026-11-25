# Agent I/O Contracts — Usage

PR packaging note: this branch contains the feature implementation, tests and selected
design documents. Local approval records, AI-use logs, full session exports, mypy output
files and real-market/model response artifacts referenced in historical verification
notes are retained separately and intentionally excluded from this PR. The commands below
reproduce the offline checks. No team-wide governance configuration is introduced.

Status: **enhancement-scoped usage note** for the approved increment only. It describes how
to turn the validation on and how to check it offline. Static/lint/type results below were
produced by a later supervised verification turn; they are stated as measured for the
enhancement files only, not as a clean whole-project result.

Design: `docs/architecture/adr/001-agent-io-contracts.md`.
Test specification: `docs/verification/agent-contract-test-spec.md`.

## 1. Select the mode

The mode is read from the repository configuration key `agent_contract_mode`
(`tradingagents/default_config.py`) and can be set with the environment variable
`TRADINGAGENTS_AGENT_CONTRACT_MODE`.

| Mode | Effect |
|---|---|
| `off` | **Default.** Baseline behaviour, byte-compatible with the baseline commit. The validation and recovery described below do **not** run. |
| `validate` | Input and output contracts are enforced on every accepted route. Invalid output aborts the run; no semantic repair is attempted. |
| `validate+repair` | As `validate`, plus at most one semantic repair attempt with full revalidation. |

**Important:** leaving the mode at `off` means the protection is **disabled**. A successful
run in `off` tells you nothing about contract enforcement.

Any other value is a configuration error, not a silent fallback to `off`.

## 2. What is protected (enhanced modes only)

- Research Manager, Trader and Portfolio Manager: input and output contracts, at most two
  logical model invocations per agent per run.
- The three risk debators: an upstream guard checks the trader plan and their own input
  before the model is called.
- The graph and CLI: an unrecovered `AgentContractError` stops the run, writes a failed-run
  audit, records no decision and does not clear the checkpoint.

Analyst reports (`market_report`, `sentiment_report`, `news_report`, `fundamentals_report`)
are **optional**. An absent key, an explicit `None` or a blank string means "no report" and
is valid input. A non-text value is an input-contract failure.

## 3. Offline checks

Run from the repository root. All commands are offline; no model call is made.

```powershell
# Real-node contract tests: Trader and the three risk debators, absent/None/blank reports
.venv\Scripts\python.exe -m pytest tests/test_agent_contracts_integration.py -q -k "test_h_"

# The contract layer and the node/graph/CLI integration tests
.venv\Scripts\python.exe -m pytest tests/test_agent_contracts.py tests/test_agent_contracts_integration.py -q

# The whole offline suite
.venv\Scripts\python.exe -m pytest -q
```

Last recorded results (see `docs/ai-use-log.yaml` and `docs/review-memo.md` for the entries
with commands and timings):

- `tests/test_agent_contracts_integration.py -k "test_h_"`: 50 passed.
- Both contract modules: 231 passed.
- Whole offline suite: 1638 passed, 6 skipped, 1 deselected, exit code 0.

## 3a. Static checks (lint and type)

Tools were installed in the isolated virtual environment only for this verification;
`pyproject.toml` and application dependencies were **not** changed. Measured versions:
`ruff 0.17.6`, `mypy 2.4.1`, `jsonschema 4.26.0`.

```powershell
# Whole-repository lint
.venv\Scripts\ruff.exe check .

# Whole-application type check (the exact command agreed with the supervisor)
.venv\Scripts\mypy.exe tradingagents cli --ignore-missing-imports --follow-imports=silent `
    --check-untyped-defs --no-incremental --show-error-codes
```

Results actually produced:

| Check | Result |
|---|---|
| `ruff check .` (whole repository) | `All checks passed!`, exit code 0, **0 findings** |
| `mypy ...` (whole application) | `Found 173 errors in 31 files`, exit code 1 |
| Enhancement type findings | **0** (all 15 reported in the enhancement files are resolved) |
| Existing upstream type debt | **173**, identical to the unmodified `c454491` baseline |

Distinguish the two type lines carefully:

- The **173** are pre-existing upstream/vendor findings. The unmodified baseline commit
  `c454491` run under the same command reports exactly the same 173 errors in the same 31
  files; a path+message multiset comparison against the baseline is empty in both directions
  (no new finding, no resolved upstream finding). These are **out of scope** for this
  increment and were **not** fixed.
- The **enhancement files** (contracts, the three contract agents, the debators, the graph
  wiring and the two contract test modules) contribute **no** type error and **no** lint
  finding. They are the 15 findings (14 Ruff, 15 mypy) recorded as new in the previous turn;
  all are now resolved without blanket ignores, without disabling checks, without widening
  annotations to `Any` and without weakening any test.

This is **not** a claim that a whole-project type check passes. The whole application still
reports the 173 upstream errors above.

Reproduce the comparison: `docs/verification/evidence/` holds the unedited mypy output for
the working tree (`mypy-after.txt`), the unmodified baseline commit
(`mypy-baseline-c454491.txt`) and the pre-fix working tree (`mypy-before.txt`).

## 3b. Requirements-file schema validation

`docs/req/requirements.yaml` was validated against `docs/req/req-schema.json` with the real
`jsonschema` `Draft7Validator` (4.26.0): **0 errors** over 29 requirement records. This
replaces the earlier manual-only check, which is retained as a historical statement.

## 4. Known limitations

- **No full multi-agent live analysis was run.** Every result above comes from scripted offline fixtures.
  Real provider behaviour, latency and cost are unmeasured.
- **Lint and type results are split, not uniformly clean.** Whole-repository `ruff` reports 0
  findings. The enhancement contributes 0 type findings, but the whole-application `mypy`
  run still reports **173 pre-existing upstream errors** that are out of scope here; they
  must not be described as passing. The tools were installed in the isolated virtual
  environment for verification only; `pyproject.toml` was not changed and no type-checker
  configuration was added.
- **No CI or branch-protection result is claimed.** A green local run is not CI.
- **Observed HTTP attempt counts are unknown.** The record exposes only the configured
  `llm_max_retries` value; provider SDK transport retries are not observed.
- **`docs/req/requirements.yaml` schema validation** was performed with the real
  `jsonschema` `Draft7Validator` and reported 0 errors. The earlier "package not installed,
  manual check only" statement is retained as history, not as the current state.
- **Financial truth is not verified.** The contract checks shape and type, not whether a
  claim is correct.
- **`.opencode/rules.md` is human-only.** Agents do not create or edit it.
- This increment adds **no** brokerage or trading execution. Do not treat these modes as a
  safety guarantee for real money.


## Subsequent supervisor-run actual-model check

The production Research Manager node was called once through the CityU API with 23 actual Yahoo Finance NVDA observations (latest 2026-11-20). Its actual output passed the enabled contract. Provider-reported usage was 2045 input tokens and 822 output tokens, 2867 total. No fake news, fundamentals, holdings or debate were supplied. This is not a full workflow or a financial-truth evaluation. See real-model-verification.md and evidence/live-research-manager-20261123/ for the raw input, output and response. Earlier no-live-test statements refer to the offline verification rounds before this separate probe.
