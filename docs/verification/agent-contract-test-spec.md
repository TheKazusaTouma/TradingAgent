# Agent Contract Test Specification — v1.0 (APPROVED BEFORE CODE)

PR packaging note: approval refers to local development approval. The local approval and
review records referenced below are retained separately, not included in this feature PR.
Team approval and merge review remain pending; no GitHub approval is claimed.

- Version: **1.0**
- Status: **Approved as a specification before implementation.** No test in this document
  has been implemented and no result has been produced. This document defines expected
  outcomes; it is not evidence of passing behaviour.
- Baseline: TradingAgents v0.7.1, commit `c4544913a14ed3df5f1ed7b9e8483270ce20682a`.
- Requirements: `docs/req/requirements.yaml`. Traceability:
  `docs/architecture/trace-matrix.md`. Design:
  `docs/architecture/adr/001-agent-io-contracts.md`.
- Approval record: `docs/approvals/agent-contract-v1.md`.
- Language: English.

## 0. Conventions

- **Mode:** every test states the `agent_contract_mode` it uses. No test may rely on the
  default. Modes: `off`, `validate`, `validate+repair`.
- **Invocation counters:** fixtures wrap the structured callable and the plain callable
  with counters. "Zero calls" means both counters are zero.
- **Fixture naming:** F-01..F-16 are defined in `docs/architecture/evaluation-table.md`.
- **Determinism:** all fixtures use scripted models and literal payloads. No network.
- **Independence:** expected assertions below were derived from requirements, not from any
  implementation. Changing an expected assertion requires new approval.
- **Where:** all tests are offline. Unless stated otherwise, they live in a new
  `tests/test_agent_contracts.py`; checkpoint tests may extend existing checkpoint test
  modules, and full-graph tests reuse the scripted harness pattern in
  `tests/test_graph_end_to_end.py:50-128`.
- **Result column:** empty until a real run. Never filled by inference.

Default accepted-route set for enhanced modes: structured output, compatibility free-text
recovery, and repaired output.

---

## T-1 — Off mode preserves baseline behaviour

- **Requirements:** INV-7, NFR-4
- **Mode:** `off`
- **Fixture:** F-01, plus the existing suite.
- **Setup:** run the complete existing default suite; separately render valid payloads.
- **Expected assertions:**
  1. The complete existing suite passes with zero failures and zero errors.
  2. `render_research_plan`, `render_trader_proposal` and `render_pm_decision` output for
     F-01 is byte-identical to the baseline strings.
  3. No contract record key is produced in mode `off`.
- **Normal/negative/boundary:** regression only.
- **Independence:** assertions come from current renderers and suite membership.

## T-2 — Optional analyst reports are valid when absent, `None` or blank

- **Requirements:** FR-1, INV-6
- **Mode:** `validate`
- **Fixture:** F-01 variants for each analyst key: omitted key, `None`, `""`, `"   "`.
- **Expected assertions:**
  1. For all three agents, no contract failure is raised for any variant.
  2. The agent proceeds to its model call exactly once (structured), and the payload is
     accepted.
  3. No record entry lists an analyst report as an issue.
- **Boundary:** all four analyst reports simultaneously absent still valid.
- **Independence:** `report_or_absent` already treats absence as legitimate
  (`tradingagents/agents/context.py:190-201`).

## T-3 — Invalid input costs zero model calls

- **Requirements:** FR-1, FR-7, INV-3, INV-6, NFR-1
- **Mode:** `validate`
- **Fixture:** F-02 and F-03: (a) Research Manager with `investment_debate_state` absent;
  (b) Trader with `market_report=None` while `investment_plan` missing; (c) Trader with
  `investment_plan` blank; (d) Trader with `investment_plan` of wrong type (integer);
  (e) Portfolio Manager with `risk_debate_state["history"]` missing; (f) any agent receiving
  a contract failure marker as its plan input.
- **Expected assertions:**
  1. Each case raises `AgentContractError`.
  2. Zero structured invocations and zero plain invocations in every case.
  3. The record names the failing field exactly.
  4. Case (b) shows `market_report=None` is **not** the cause; the missing plan is.
- **Note (corrected from the previous draft):** a `market_report` that is absent, `None` or
  blank is valid optional input and must never by itself cause a failure.
- **Independence:** inputs the agent reads are enumerated in ADR §3.

## T-4 — A failure marker is not usable input

- **Requirements:** FR-1, FR-7, FR-8, INV-3, NFR-1
- **Mode:** `validate`
- **Fixture:** F-03.
- **Expected assertions:**
  1. A plan field equal to a contract failure marker raises `AgentContractError` with stage
     `input`.
  2. Zero model invocations.
- **Independence:** markers must never be treated as content (ADR §3).

## T-5 — Required narratives are non-blank, with no length rule

- **Requirements:** FR-2
- **Mode:** `validate`
- **Fixture:** for each required narrative (`rationale`, `strategic_actions`, `reasoning`,
  `executive_summary`, `investment_thesis`): `""`, `"   "`, `"\n\t"`, and a two-character
  string such as `"ok"`.
- **Expected assertions:**
  1. Empty and whitespace-only values raise `AgentContractError`.
  2. `"ok"` is accepted — there is no minimum length.
- **Boundary:** a single non-whitespace character is accepted.
- **Independence:** amendment states nonblank with no arbitrary length.

## T-6 — Nonfinite numbers are rejected

- **Requirements:** FR-3
- **Mode:** `validate`
- **Fixture:** F-05: `entry_price`, `stop_loss` and `price_target` each set to `float("nan")`,
  `float("inf")`, `float("-inf")`.
- **Expected assertions:**
  1. Each raises `AgentContractError` naming the field and the nonfinite reason.
  2. The value is **not** converted to `None` and is **not** written to state.
- **Boundary:** omitted field and explicit `None` remain valid.
- **Independence:** amendment: reject nonfinite deterministically, never coerce to pass.

## T-7 — Legal numeric formatting is supported

- **Requirements:** FR-3
- **Mode:** `validate`
- **Fixture:** F-07: `"189.5"`, `"$1,234.50"`, `189.5`, `-3.5` (negative permitted; no sign
  rule).
- **Expected assertions:**
  1. Each is accepted and preserved as the numeric value.
  2. Negative values are accepted.
  3. No sign or positivity rule is applied.
- **Independence:** no domain evidence supports a universal sign rule.

## T-8 — Action disagreement is informational

- **Requirements:** FR-4
- **Mode:** `validate`
- **Fixture:** F-08: RM recommendation `Sell`, Trader action `Buy`.
- **Expected assertions:**
  1. No `AgentContractError`.
  2. The Trader plan is produced normally.
  3. The record contains an informational entry naming both values, marked informational,
     and no issue of severity failure.
- **Independence:** prompt semantics already permit sizing nuance
  (`tradingagents/agents/trader/trader.py:76-78`).

## T-9 — Consistency checks are skipped when context is absent

- **Requirements:** FR-4
- **Mode:** `validate`
- **Fixture:** F-01 with the referenced context removed (no plan recommendation available
  to compare, no identity context).
- **Expected assertions:**
  1. No failure.
  2. The record marks the check `skipped`, not `failed` and not `passed`.
- **Independence:** amendment: consistency only when available.

## T-10 — One semantic repair then full revalidation

- **Requirements:** FR-5, NFR-2
- **Mode:** `validate+repair`
- **Fixture:** F-09: first payload invalid (`executive_summary` blank), second payload valid
  but with a different value in another field.
- **Expected assertions:**
  1. The emitted decision comes from the repaired payload.
  2. The repaired payload was validated in full: a second, independent invalid field in the
     repaired payload would also be caught (verified by a variant where the repaired payload
     has a blank `investment_thesis` and must fail).
  3. `semantic_repair == 1`.
- **Boundary:** repair of the first invalid output only.
- **Independence:** full revalidation is required by FR-5.

## T-11 — Two failures exhaust the budget

- **Requirements:** FR-5, INV-4, NFR-2
- **Mode:** `validate+repair`
- **Fixture:** F-10: invalid twice.
- **Expected assertions:**
  1. `AgentContractError` raised.
  2. `logical_total == 2` and `semantic_repair == 1`.
  3. No plan written to state.
- **Independence:** ceiling of two logical invocations.

## T-12 — No third call after a compatibility fallback

- **Requirements:** FR-5, INV-4
- **Mode:** `validate+repair`
- **Fixture:** F-11: structured parse fails, free-text recovery returns invalid content.
- **Expected assertions:**
  1. `AgentContractError` raised.
  2. `structured == 1`, `free_text_recovery == 1`, `semantic_repair == 0`,
     `logical_total == 2`.
  3. No further invocation of either callable.
- **Independence:** amendment: no third call, counted or uncounted.

## T-13 — Accounting record completeness

- **Requirements:** FR-6, FR-10, NFR-3
- **Mode:** `validate`
- **Fixture:** F-01.
- **Expected assertions:** the record contains exactly the keys
  `structured`, `free_text_recovery`, `semantic_repair`, `logical_total`,
  `sdk_retry_budget`, `observed_http_attempts`; `logical_total` equals the sum of the three
  call fields; `observed_http_attempts` is the string `"unknown"`;
  `sdk_retry_budget` reflects the configured `llm_max_retries` and states that the provider
  default is not assumed when unset.
- **Independence:** honest accounting requirement.

## T-14 — Records are deterministic

- **Requirements:** FR-6, INV-5
- **Mode:** `validate`
- **Fixture:** the same invalid input evaluated twice in separate agent constructions.
- **Expected assertions:** the two records are byte-identical strings; key order is stable;
  no timestamp or duration field is present.
- **Independence:** INV-5.

## T-15 — Portfolio Manager failure cannot be published

- **Requirements:** FR-8, FR-9, INV-1, INV-2, INV-8
- **Mode:** `validate`
- **Fixture:** F-10 under the full-graph scripted harness.
- **Expected assertions:**
  1. The run raises `AgentContractError` (programmatic) — or, for the CLI test T-20, reports
     failure.
  2. `final_trade_decision` is absent from the returned/final state.
  3. `final_rating` is absent or `REVIEW`; never `Hold` or another rating.
  4. `record_decision` is not called; the memory log gains no entry.
  5. No reflection is requested for the failed run.
  6. `clear_checkpoint_on_success` is not called.
- **Independence:** FR-9 and INV-8.

## T-16 — Research Manager failure stops every dependent LLM

- **Requirements:** FR-8, INV-1
- **Mode:** `validate`
- **Fixture:** F-02 variant: RM output invalid in every arm (`off` excluded).
- **Expected assertions:**
  1. Trader invocation count is zero.
  2. Aggressive, Conservative and Neutral invocation counts are zero.
  3. Portfolio Manager invocation count is zero.
  4. No risk-debate history grows.
- **Independence:** every consumer is enumerated (ADR §3, `docs/bug-diary.md` BD-001).

## T-17 — Trader failure stops the risk debate and PM

- **Requirements:** FR-8, INV-1
- **Mode:** `validate`
- **Fixture:** Trader output invalid.
- **Expected assertions:**
  1. Aggressive, Conservative and Neutral invocation counts are zero.
  2. Portfolio Manager invocation count is zero.
  3. `trader_investment_plan` is absent from state.
- **Independence:** the three risk debators read the field directly
  (`aggressive_debator.py:30`, `conservative_debator.py:30`, `neutral_debator.py:30`).

## T-18 — Failure retains audit and checkpoint

- **Requirements:** FR-8, INV-9, NFR-5
- **Mode:** `validate`
- **Fixture:** F-10 with `checkpoint_enabled=True`.
- **Expected assertions:**
  1. `AgentContractError` propagates to the caller.
  2. A checkpoint step is still readable after the failure.
  3. A failed-run audit entry exists and carries a failure status.
- **Independence:** amendment: preserve audit/checkpoint failure state.

## T-19 — Failed run publishes no successful decision

- **Requirements:** FR-6, FR-9, INV-2, INV-9, NFR-5
- **Mode:** `validate`
- **Fixture:** F-10.
- **Expected assertions:**
  1. The audit entry has no decision text and no rating other than absence or `REVIEW`.
  2. The report contains a `## Contract Validation` block for the failed section.
  3. No report section heading changed and no `FINAL TRANSACTION PROPOSAL:` line is
     fabricated.
- **Independence:** FR-11 and INV-9.

## T-20 — CLI path records no failure decision

- **Requirements:** INV-8
- **Mode:** `validate`
- **Fixture:** F-10 through a CLI run function with `record_decision` and
  `clear_checkpoint_on_success` replaced by spies; no real terminal interaction.
- **Expected assertions:**
  1. `AgentContractError` is caught by the CLI handler.
  2. The `record_decision` spy is called zero times.
  3. The `clear_checkpoint_on_success` spy is called zero times.
  4. A failure message is displayed (captured output contains a failure indication).
- **Independence:** `cli/run.py:353-356` performs both calls unconditionally today.

## T-21 — Absent contract version invalidates an old checkpoint

- **Requirements:** INV-10, NFR-7
- **Mode:** `validate`
- **Fixture:** F-14: a checkpoint written by a run whose signature contains no contract
  version.
- **Expected assertions:**
  1. The computed thread identifier differs from the stored one.
  2. The run starts fresh rather than resuming.
  3. No stale `investment_plan` from the old checkpoint reaches the Trader.
- **Independence:** amendment: include mode and contract version in the signature.

## T-22 — Same-mode resume revalidates saved upstream payloads

- **Requirements:** INV-10, NFR-7
- **Mode:** `validate`, same mode on both runs
- **Fixture:** F-15: a checkpoint whose saved `investment_plan` is blank or a marker.
- **Expected assertions:**
  1. The Trader raises `AgentContractError` with stage `input`.
  2. Zero Trader model invocations.
  3. A valid saved plan resumes normally (control case).
- **Independence:** amendment: old state cannot bypass the contract.

## T-23 — Layering and scope preserved

- **Requirements:** FR-12, INV-12, NFR-6
- **Mode:** n/a
- **Expected assertions:**
  1. `tests/test_layering.py` passes unchanged.
  2. `contracts.py` imports no vendor top-level module.
  3. No new node or edge appears in `graph.get_graph()` compared with baseline.
- **Independence:** SC-02, SC-03.

## T-24 — Valid payload rendering is unchanged

- **Requirements:** FR-11, INV-7, INV-11
- **Mode:** `off` and `validate`
- **Expected assertions:** rendered output for F-01 is byte-identical between modes and equal
  to the baseline expectation string.
- **Independence:** SC-05.

## T-25 — `validate` never semantically repairs

- **Requirements:** NFR-5
- **Mode:** `validate`
- **Fixture:** F-10.
- **Expected assertions:**
  1. `AgentContractError` raised on the first invalid output.
  2. `semantic_repair == 0`.
  3. At most one compatibility fallback is counted if a parse error occurred.
- **Independence:** mode semantics in ADR §2.3.

## T-26 — Raw invalid numerics are rejected; off mode stays compatible

- **Requirements:** FR-3, INV-7
- **Mode:** `validate` (strict path) and `off` (baseline path)
- **Fixture:** F-06: `"15%"`, `"150-160"`, `True`, `"around 150"`, an empty string.
- **Expected assertions:**
  1. Mode `validate`: each raises `AgentContractError` naming the field and the reason; none
     becomes `None` and passes.
  2. Mode `off`: each still follows the baseline coercer and becomes `None` as today, so
     off-mode output is byte-compatible.
  3. A boolean `True` is rejected as a numeric value in `validate`, not accepted as `1.0`.
- **Boundary:** explicit `None` remains a legal absence in both modes.
- **Independence:** amendment: strict before permissive; off unchanged.

## T-27 — `validate` counts a compatibility fallback and stops at two

- **Requirements:** FR-5, INV-4, NFR-2
- **Mode:** `validate`
- **Fixture:** F-11 (structured parse failure then a valid free-text result).
- **Expected assertions:**
  1. The free-text result is used only after being validated.
  2. `structured == 1`, `free_text_recovery == 1`, `semantic_repair == 0`,
     `logical_total == 2`.
  3. No further call is made even when the free-text result is invalid (variant).
- **Independence:** amendment: validate does not repair; fallback counted and validated.

## T-28 — All required fields on all agents and all output routes

- **Requirements:** FR-2, FR-4
- **Mode:** `validate`
- **Fixture:** one invalid case per required field (RM: recommendation, rationale,
  strategic_actions; Trader: action, reasoning; PM: rating, executive_summary,
  investment_thesis), each routed through structured output and through compatibility
  free-text recovery.
- **Expected assertions:**
  1. Every case raises `AgentContractError` naming that exact field.
  2. The record stage distinguishes structured from free-text recovery.
  3. Optional-field absence never produces a failure.
- **Independence:** ADR §3 contract table.

## T-29 — Transport and programming errors are classified, not repaired

- **Requirements:** FR-10, NFR-3
- **Mode:** `validate` and `validate+repair`
- **Fixture:** F-12 (transport exception then a valid free text) and F-13 (a `KeyError`
  raised inside a scripted callable).
- **Expected assertions:**
  1. The record classifies the first failure as transport and the second as programming.
  2. A transport failure is not replaced by a silently accepted fallback: the failure is
     surfaced with its classification and no `semantic_repair` is attempted.
  3. `observed_http_attempts` remains `"unknown"`; `sdk_retry_budget` reports the configured
     value; no measured attempt count is produced.
- **Independence:** amendment: classify and do not hide; honest retry accounting.

## T-30 — Stale artifacts and historical settlement are distinguished

- **Requirements:** INV-8, INV-9
- **Mode:** `validate`
- **Fixture:** F-16: a previously written successful state log and memory entry for the same
  ticker, then a failing run; separate control case where settlement of the earlier pending
  entry proceeds.
- **Expected assertions:**
  1. The failed run writes its own audit artifact with failure status; it neither deletes
     nor overwrites the earlier successful artifact.
  2. The failed run adds no new memory-log entry.
  3. When the memory step settles the earlier pending entry, that settlement is reported as
     independent and is not asserted to be prevented or undone.
  4. No assertion claims the earlier successful decision is invalidated.
- **Independence:** amendment on stale artifacts and settlement independence.

---

## Coverage summary

| Requirement | Tests |
|---|---|
| FR-1 | T-2, T-3, T-4 |
| FR-2 | T-5, T-28 |
| FR-3 | T-6, T-7, T-26 |
| FR-4 | T-8, T-9, T-28 |
| FR-5 | T-10, T-11, T-12, T-27 |
| FR-6 | T-13, T-14, T-19 |
| FR-7 | T-3, T-4 |
| FR-8 | T-15, T-16, T-17, T-18 |
| FR-9 | T-15, T-19 |
| FR-10 | T-13, T-29 |
| FR-11 | T-24 |
| FR-12 | T-23 |
| INV-1 | T-15, T-16, T-17 |
| INV-2 | T-15, T-19 |
| INV-3 | T-3, T-4 |
| INV-4 | T-11, T-12, T-27 |
| INV-5 | T-14 |
| INV-6 | T-2, T-3 |
| INV-7 | T-1, T-24, T-26 |
| INV-8 | T-15, T-20, T-30 |
| INV-9 | T-18, T-19, T-30 |
| INV-10 | T-21, T-22 |
| INV-11 | T-24 |
| INV-12 | T-23 |
| NFR-1 | T-3, T-4 |
| NFR-2 | T-10, T-11, T-27 |
| NFR-3 | T-13, T-29 |
| NFR-4 | T-1 |
| NFR-5 | T-18, T-19, T-25 |
| NFR-6 | T-23 |
| NFR-7 | T-21, T-22 |

Every requirement is covered. Every test maps to at least one requirement.

## Result log

Rows are added only from real command output. A test is marked implemented only when the
named test functions in `tests/test_agent_contracts.py` assert its expected outcomes; no
row is inferred.

Implemented in the **core increment** (helper-level, offline, scripted callables), all in
`tests/test_agent_contracts.py`:

| Test | Mode | Covered by | Result |
|---|---|---|---|
| T-2 | `validate` | analyst-report variants, all three agents | passed |
| T-3 | `validate` | six invalid-input cases, zero-call assertions | passed |
| T-4 | `validate` | failure marker as input | passed |
| T-5 | `validate` | blank and short narratives | passed |
| T-6 | `validate` | NaN / +Inf / -Inf, omitted and None | passed |
| T-7 | `validate` | legal formatting, negatives | passed |
| T-8 | `validate` | informational disagreement | passed |
| T-9 | `validate` | skipped-when-absent check | passed |
| T-10 | `validate+repair` | repair accepted; second bad field rejected | passed |
| T-11 | `validate+repair` | two failures, `logical_total == 2` | passed |
| T-12 | `validate` / `validate+repair` | no third call; repair and fallback never both run | passed |
| T-13 | `validate` | six-key record, retry wording, config/contract agreement | passed |
| T-14 | `validate` | byte-identical records | passed |
| T-24 | `validate` | rendering equals the baseline renderers | passed |
| T-25 | `validate` | `semantic_repair == 0` | passed |
| T-26 | `validate` / `off` | strict rejection; off-mode compatibility | passed |
| T-27 | `validate` | counted, validated fallback | passed |
| T-28 | `validate` | every required field, structured and free-text routes | passed |
| T-29 | `validate` / `validate+repair` | transport and programming classification | passed |

Commands actually run (this agent, offline):

| Command | Result |
|---|---|
| `.venv\Scripts\python.exe -m pytest tests/test_agent_contracts.py -q` | `124 passed` |
| `.venv\Scripts\python.exe -m pytest -q` | `1531 passed, 6 skipped, 1 deselected, 24 warnings, 100 subtests passed in 41.23s` |
| `.venv\Scripts\python.exe -m ruff --version` | not run: `No module named ruff`; ruff was not installed |

> Timestamped evidence, describing that turn only. For the **current** lint, type,
> `jsonschema` and test status see `docs/review-memo.md` §10 and
> `docs/verification/agent-contract-usage.md` §3a-3b; the statements above that
> `ruff`/`mypy`/`jsonschema` were unavailable are not the current state.

**NOT implemented, no result claimed:** T-1, T-15, T-16, T-17, T-18, T-19, T-20, T-21,
T-22, T-23 and T-30. Each needs the integration increment (nodes, graph, CLI, reporting,
checkpoint signature). The full suite was run, but T-1 additionally asserts byte-identical
F-01 rendering across modes and the absence of a contract record key in `off`, which needs
the node wiring, so T-1 is not marked.

Scope caveat: T-2, T-13 and T-27 are exercised against `invoke_with_contract` with scripted
callables, not against a real agent node. The node wiring that makes the same assertions
hold end-to-end is the next increment.

### Correction turn — real-node coverage for T-2 / T-3

A supervisor review found that the helper-level T-2 tests passed while the real nodes still
raised `KeyError` on an absent optional report (defect BD-006 in `docs/bug-diary.md`). The
fix and the following real-node tests live in `tests/test_agent_contracts_integration.py`;
they run the production node factories with mocked models and explicit modes.

| Test | Mode | Covered by | Result |
|---|---|---|---|
| T-2 | `validate`, `validate+repair` | `test_h_trader_accepts_optional_reports_absent_none_or_blank`, `test_h_risk_debators_accept_optional_reports_absent_none_or_blank` | passed |
| T-2 | `validate` | `test_h_supervisor_reproduction_trader_without_market_report`, `test_h_trader_rejects_a_malformed_report_under_the_input_contract`, `test_h_risk_debators_reject_a_malformed_report` | passed |
| T-3 | `validate` | `test_h_trader_rejects_a_required_invalid_plan_with_zero_calls`, `test_h_trader_rejects_a_rendered_plan_missing_its_narrative_with_zero_calls`, `test_h_risk_debators_reject_a_required_invalid_plan_with_zero_calls`, `test_h_risk_state_remains_required` | passed |
| T-1 (off-mode fixture) | `off` | `test_h_off_mode_reads_are_unchanged_for_a_valid_fixture`, `test_h_off_mode_trader_reads_are_unchanged_for_a_valid_fixture` | passed |

Commands actually run in the correction turn:

| Command | Result |
|---|---|
| `.venv\Scripts\python.exe -m pytest tests/test_agent_contracts_integration.py -q -k "test_h_"` | `50 passed`, exit code 0 |
| `.venv\Scripts\python.exe -m pytest -q` | `1638 passed, 6 skipped, 1 deselected, 24 warnings, 100 subtests passed in 70.53s`, exit code 0 |
| `.venv\Scripts\python.exe -c "import ruff"` / `"import mypy"` / `"import jsonschema"` | not available (`ModuleNotFoundError`); nothing installed |

> Timestamped evidence, describing that correction turn only. For the **current** lint, type,
> `jsonschema` and test status see `docs/review-memo.md` §10 and
> `docs/verification/agent-contract-usage.md` §3a-3b.

No expected outcome in this specification was changed. The expectations for T-1, T-15..T-23
and T-30 that depend on the integration increment are unchanged; note that
`tests/test_agent_contracts_integration.py` also contains tests whose names begin `test_t15`,
`test_t16`, `test_t17`, `test_t18`, `test_t19`, `test_t20`, `test_t21`, `test_t22` and
`test_t30`. Those rows are **not** added to the table above by this turn, because a separate
supervisor acceptance of the integration increment was not recorded here; they are named
pytest tests that passed in the full run, and nothing more is claimed.
