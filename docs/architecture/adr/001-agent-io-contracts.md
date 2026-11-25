# ADR 001 — Agent Input/Output Contract Validation and Controlled Recovery

PR packaging note: this ADR was accepted for local development and is proposed here for
team integration review. The local approval record referenced below is retained separately;
its existence does not constitute team or GitHub approval of this PR.

- Status: **Accepted (design)** — approved by the student and supervisor before any
  implementation. See `docs/approvals/agent-contract-v1.md`.
- Date of record: recorded in the repository at the time of this file's creation. No
  exact approval timestamp is claimed because none was captured.
- Baseline: TradingAgents v0.7.1, commit `c4544913a14ed3df5f1ed7b9e8483270ce20682a`.
- Safety-critical: **yes**. This ADR governs a change that can stop a run from producing
  a trading signal; it requires human approval, which this record documents.

## 1. Context

The Research Manager, Trader and Portfolio Manager produce the only structured decisions
in the pipeline, and their output flows to every later agent and to persisted artifacts.
Today the boundary between them is a plain string assignment.

Confirmed facts (repository evidence):

1. Structured output is typed, but parsed free text is not field-checked.
   `tradingagents/agents/structured.py:84-95` returns `plain_llm.invoke(prompt).content`
   as the agent's product whenever the structured attempt is unavailable or fails.
2. The Portfolio Manager has an equivalent inline fallback at
   `tradingagents/agents/managers/portfolio_manager.py:85-86`.
3. Failures are swallowed with a warning and no structured reason
   (`tradingagents/agents/structured.py:76-81`).
4. Permissive numeric coercion silently converts invalid values to `None`
   (`tradingagents/agents/schemas.py:33-58`), by design for the baseline (#1058, #1288).
5. The three risk debators interpolate `state["trader_investment_plan"]` verbatim
   (`aggressive_debator.py:30`, `conservative_debator.py:30`, `neutral_debator.py:30`).
6. Run completion records a decision, writes a state log and clears the checkpoint
   (`tradingagents/graph/trading_graph.py:359-371`, `:400`, `:403`).
7. `run_rating` falls back to reading the rating from text and returns `REVIEW` when none
   is readable (`tradingagents/agents/rating.py:106-112`).
8. The checkpoint signature digests config except `_NOT_IN_SIGNATURE`
   (`tradingagents/graph/trading_graph.py:46-48`, `:140-165`).
9. Memory settlement of previously recorded pending decisions runs as a graph step
   beside the analysts (`tradingagents/graph/trading_graph.py:309-336`;
   `tradingagents/memory/settlement.py:136-193`).

Consequence: a failed or unparsed agent output is consumable downstream today, including
by the risk debate, the PM, the memory log and the saved reports.

## 2. Decision

Adopt **bounded incremental contract validation** with a typed abort. Summary:

1. **Contracts module.** New `tradingagents/agents/contracts.py` holds typed field rules,
   an `AgentContractError(RuntimeError)`, stable error records, and
   `AGENT_CONTRACT_VERSION`.
2. **Strict numeric path.** An isolated strict parser (same module or a separate
   `contracts_strict.py`) validates provided numeric values *before* any permissive
   coercion. NaN/±Infinity and ambiguous values (invalid percentages, ranges, booleans)
   are rejected; they must not become `None` and pass. Omitted fields remain legal
   `None`. Explicit numeric formatting remains supported. No sign rule is imposed on
   prices.
3. **Modes.** `agent_contract_mode` in `tradingagents/default_config.py`:
   - `off` — default; baseline behaviour, shared schema untouched, byte-compatible.
   - `validate` — input and output contracts enforced on all accepted routes; the
     compatibility free-text fallback is retained, counted and validated; no semantic
     repair.
   - `validate+repair` — as `validate` plus at most one semantic repair.
4. **Invocation ceiling.** At most **two** logical invocations per agent per run
   (`structured` plus at most one of `free_text_recovery` or `semantic_repair`). No
   third call may exist, counted or uncounted.
5. **Recoverability.** Only parse and validation problems are recoverable. Transport
   errors and programming errors are surfaced as their own classification and are not
   converted into a successful fallback.
6. **Abort semantics.** Unrecovered invalid output raises `AgentContractError`. Graph
   execution stops; dependent nodes do not run.
7. **Risk-debator guards.** All three risk debators validate `trader_investment_plan`
   before invoking their model and raise the same error. This covers the case where a
   failure marker could otherwise reach the debate.
8. **Narrow outer handling.** `trading_graph._run_graph` (`:373-405`) and the CLI stream
   completion (`cli/run.py:344-359`) catch **only** `AgentContractError`, write a
   failed-run audit entry, skip `record_decision`, skip `clear_checkpoint_on_success`,
   and surface an explicit failure. The checkpoint is retained.
9. **No substitution.** No Hold, rating, price, zero or prose is invented. `final_rating`
   is not set by a failed run; readers may still show absence or `REVIEW` as they do
   today.
10. **Checkpoint signature.** `_run_signature` includes the mode and
    `AGENT_CONTRACT_VERSION`. Enhanced-mode resume revalidates saved upstream payloads
    (`investment_plan`, `trader_investment_plan`) before use, so an old state cannot
    bypass the contract.
11. **Compatibility.** Renderers and report sections unchanged for valid payloads. A
    failure adds a `## Contract Validation` block.
12. **Honest accounting.** Records expose observed logical calls and the configured
    `llm_max_retries` budget. Observed HTTP attempt counts are reported as unknown; no
    provider default is assumed.

## 3. Per-agent contract (approved)

Required narrative fields must be non-blank after stripping whitespace. No minimum
length.

| Agent | Required (validated) | Optional | Optional-absence rule |
|---|---|---|---|
| Research Manager | `recommendation` in `PortfolioRating`; `rationale`; `strategic_actions` | none | — |
| Trader | `action` in `TraderAction`; `reasoning` | `entry_price`; `stop_loss`; `position_sizing` | absent or `None` legal; if provided, must be finite and unambiguous |
| Portfolio Manager | `rating` in `PortfolioRating`; `executive_summary`; `investment_thesis` | `price_target`; `time_horizon` | absent or `None` legal; if provided, must be finite and unambiguous |

Input requirements (all three):

- Research Manager: `investment_debate_state` mapping with `history` (str; may be blank
  when `count` is 0) and an integer `count`.
- Trader: `investment_plan` present, string, non-blank.
- Portfolio Manager: `risk_debate_state["history"]`, `investment_plan` and
  `trader_investment_plan` present, string, non-blank.
- Analyst report keys (`market_report`, `sentiment_report`, `news_report`,
  `fundamentals_report`) are **not** required inputs of these three agents. Absent, `None`
  or blank values are valid optional input; there is no requirement that every analyst was
  selected.
- Any value that is a contract failure marker, where markers are introduced, is invalid
  input, not a usable plan.

Consistency checks (informational only, run only when the referenced context exists):

- Trader action vs Research Manager recommendation disagreement is recorded as
  informational; it does not invalidate the contract.
- No ticker-detection-in-prose heuristic.
- No claim that a thesis or a number is financially true.

## 4. Alternatives considered

| Option | Why rejected |
|---|---|
| Minimal text-regex wrapper around the existing helper | Free text has no typed fields, so required-field checks become prose heuristics; no field-level record; risk debators and abort semantics unaddressed. |
| Broad state rewrite with typed sub-models and new validation nodes/edges | Strong guarantees, but changes graph shape, checkpoint signature semantics, `_log_state`, `reporting._SECTIONS`, CLI display and many baseline tests. Too large for this increment and risks baseline compatibility. |
| Fail closed with no recovery | Cheapest, but turns one malformed value into a total run failure despite a recoverable syntactic cause. |
| Structural retry with the same prompt | Repeats the same semantic error; wastes the single allowed extra call. |
| Marker strings instead of an exception | Confirmed consumable by the risk debators and PM; would require per-node checks at every reader and still permit accidental writes. |

## 5. Consequences

Positive: no invalid output is consumed; invalid input costs no model calls; failures are
auditable; failures cannot be published as decisions; baseline behaviour is preserved in
`off`.

Negative and accepted: an aborted run produces no signal, so a user loses the analysis
instead of receiving a degraded one; the single repair adds bounded cost in
`validate+repair`; the risk-debator guards duplicate a check already implied by the abort
(defence in depth, deliberately kept).

Residual risks:

- Historical settlement of previously recorded decisions may already be running when a
  run fails. This increment does **not** claim to undo or prevent it; it only ensures no
  **new** failed decision or reflection is created.
- Old successful artifacts on disk (state logs, reports) are not deleted; a failed run
  writes its own failure audit and must not be mistaken for a success. See T-30.
- SDK transport retries remain unobserved.
- Financial truth of accepted claims is not verified.

## 6. Approved source scope

May be created or modified for this increment:

- `tradingagents/agents/contracts.py` (new)
- `tradingagents/agents/contracts_strict.py` (new, optional, isolated strict parsing)
- `tradingagents/agents/structured.py`
- `tradingagents/agents/managers/research_manager.py`
- `tradingagents/agents/managers/portfolio_manager.py`
- `tradingagents/agents/trader/trader.py`
- `tradingagents/agents/risk_mgmt/aggressive_debator.py`
- `tradingagents/agents/risk_mgmt/conservative_debator.py`
- `tradingagents/agents/risk_mgmt/neutral_debator.py`
- `tradingagents/agents/state.py`
- `tradingagents/default_config.py`
- `tradingagents/graph/trading_graph.py`
- `tradingagents/reporting.py`
- `cli/run.py`
- `tests/test_agent_contracts.py` (new)
- focused additions to existing checkpoint and graph tests only where the test spec
  requires them

Explicitly out of scope: `tradingagents/agents/schemas.py` shared behaviour changes for
`off`, `tradingagents/llm_clients/*`, `tradingagents/dataflows/*`, `tradingagents/memory/*`
behaviour, `tradingagents/graph/setup.py` structure, `tradingagents/graph/conditional_logic.py`,
packaging, `pyproject.toml`, CI workflows, `.opencode/rules.md`, `.env*`, credentials,
brokerage or trading execution.

## 7. Implementation order (gated)

1. Governance artifacts (this turn, documentation only).
2. `contracts.py` (+ optional strict module).
3. `default_config.py` mode key.
4. `structured.py` classified invocation and single recovery.
5. The three contract agents.
6. The three risk-debator guards.
7. `state.py`, `trading_graph.py`, `reporting.py`, `cli/run.py`.
8. `tests/test_agent_contracts.py` plus focused checkpoint/graph tests.

Each step is verified by the tiers in
`docs/code-governance/verification-constraints.md`.
