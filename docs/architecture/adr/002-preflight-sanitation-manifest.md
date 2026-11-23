# ADR 002 — Startup Preflight, Optional-Numeric Sanitation, Report Integrity Manifest

- Status: **Accepted for implementation** — authorised directly by the student in the
  OpenCode session that produced this file. See §1 and
  `docs/approvals/agent-contract-v3.md`.
- Amends: `docs/architecture/adr/001-agent-io-contracts.md` (ADR 001). Where this document
  and ADR 001 disagree, **this document governs** for the three requirements below.
  Everything in ADR 001 not named here remains in force unchanged.
- Baseline: TradingAgents v0.7.1, commit `c4544913a14ed3df5f1ed7b9e8483270ce20682a`.
- Companion test specification: `docs/verification/increment-v3-test-spec.md`.
- Language: English.

## 1. Authorisation, recorded honestly

On the turn that produced this document the student sent a new instruction that:

1. explicitly stated that the new student authorisation **supersedes the old scope
   restrictions where necessary**;
2. explicitly **waived per-step confirmation** for this work;
3. enumerated the three requirements (startup preflight, optional-numeric sanitation in AI
   output, report integrity manifest) and required that this ADR amendment and a concrete
   test specification be written **before** implementation, recording the authorisation
   honestly;
4. retained every prohibition that had to remain: no changes to the separate
   `team-agent-io-validation` checkout, no GitHub branch/PR/remote work, no commits, no
   pushes, no merges, no broker or live trading, no external model tests, no
   credentials/`.env`/provider-config reads, no subagents, `.opencode/rules.md` is never
   touched.

Honest limits of this record:

- No verbatim quotation of the student's message is reproduced here. The list above is a
  paraphrase of the instruction as received in this session.
- No timestamp is claimed. No student statement is attributed to an earlier turn.
- This authorisation approves **this policy change and the tests needed to demonstrate it**.
  It is **not** a retrospective approval of anything previously recorded, and it does not
  convert any earlier supervisor interpretation into a student statement.
- The prohibition on changing a previously approved test expectation still applies to
  **unrelated** expectations. The only previous expectations this document authorises to
  change are the ones that state the old strict-reject policy for **optional numeric output
  fields**; each such change is listed with traceability in
  `docs/verification/increment-v3-test-spec.md` §5.

## 2. Context and defects this ADR addresses

Repository evidence at the time of writing:

1. **Configuration is validated late, and partly not at all.** `TradingAgentsGraph.__init__`
   creates the cache and results directories (`tradingagents/graph/trading_graph.py:99-100`)
   and builds the deep and quick LLM clients (`:103-104`) *before* it checks the
   `max_tool_rounds` / `max_recur_limit` relation (`:115-119`). A negative
   `max_debate_rounds` reaches `ConditionalLogic` (`:108-111`) unchecked. `temperature` is
   coerced with a bare `float()` only at client-construction time
   (`tradingagents/llm_clients/factory.py:117-119`), `max_retries` and `max_tokens` likewise
   (`:123-132`). A programmatic `config` dict is accepted as supplied (`:94`) with no shape
   check for `data_vendors` / `tool_vendors`.
2. **An invalid optional numeric discards an otherwise valid AI answer.** The strict
   schemas reject any non-numeric optional value
   (`tradingagents/agents/contracts.py:187-222`, `:232-241`) and the contracts then abort or
   spend the single recovery slot on the whole payload (`:1022-1025`, `:1035-1091`). The
   baseline path (`tradingagents/agents/schemas.py:33-58`) already treats one bad optional
   level as an empty field, so the enhanced modes are *stricter than the baseline about a
   field the contract calls optional*.
3. **Saved reports have no integrity record.** `write_report_tree`
   (`tradingagents/reporting.py:157-208`) writes section files, `complete_report.md` and
   optionally `complete_report.html` and reconciles stale files, but nothing records which
   bytes were written. A reader cannot tell an edited or truncated report from the one the
   run produced.

## 3. Decision — Requirement A: startup preflight

**A1. One central validator.** A new leaf module `tradingagents/config_validation.py`
provides `validate_config(config) -> list[ConfigProblem]` and
`effective_config(config) -> dict`. It imports only the standard library and
`tradingagents.default_config`, so it cannot create an import cycle (`default_config` is a
leaf imported during `tradingagents.dataflows` initialisation, see
`tradingagents/default_config.py:1-11`). Provider *names* are validated in the module; the
provider registry itself is not imported, so the layering rule
(`tradingagents/llm_clients/*` is not importable from the config layer) is preserved.

**A2. Coverage.** Supported keys, types, ranges and cross-field constraints actually used by
the graph and CLI: the contract mode; non-negative integer round counts (`max_debate_rounds`,
`max_risk_discuss_rounds`, `holding_period_days`, the article limits); positive integers
(`max_recur_limit`, `max_tool_rounds`, `max_tokens`, `global_news_lookback_days`); non-negative
`llm_max_retries`; the analyst-tool-round relation
`2 * max_tool_rounds + 2 < max_recur_limit`; finite numeric options (`temperature` in
[0, 2], `llm_max_retries`, `max_tokens`); required non-blank model and provider strings;
a syntactically valid supplied backend URL (http/https with a host, no whitespace, no
embedded credentials) for `backend_url` and the two tier endpoints; booleans
(`checkpoint_enabled`); and mapping-shaped containers (`data_vendors`, `tool_vendors`,
`benchmark_map`) with string keys and string values. Non-negative `memory_log_max_entries`,
when set, is an integer. Full detail, with exact rules and messages, is in the test
specification §3.

**A3. Nothing that was previously legal becomes illegal.**

- Custom or unknown model identifiers are **not** rejected: local servers, relays and
  OpenAI-compatible endpoints accept any model string, and the baseline only warns for an
  unknown model (`tradingagents/llm_clients/openai_client.py:409-411`).
- No API key is required or read. Keyless and local providers (`ollama`,
  `openai_compatible`) stay valid, and no credential is ever inspected. The validator
  never reads an environment variable other than through the config it is given.
- A *partial* config is normal: `effective_config` fills every key the caller omitted from
  `DEFAULT_CONFIG` and reports no problem for absence. A value the caller **did** supply is
  never replaced by a default; if it is invalid it is reported.
- Unknown extension keys stay allowed (forward compatibility with newer configs).

**A4. Error reporting.** `validate_config` collects **every** problem, not just the first.
Each `ConfigProblem` carries the configuration key path, a stable code and a neutral,
actionable detail that never echoes a secret; a value that looks like a credential
(a `sk-…`/`key=…` shape, a long opaque token, a URL userinfo section) is redacted to
`<redacted>` before it appears anywhere. The raising helper
`ConfigValidationError(ValueError)` groups the problems under one message.

**A5. Where preflight runs.**

1. `TradingAgentsGraph.__init__` validates the merged config **as its first statement**,
   before `set_config`, before any directory is created, before any LLM client is
   constructed and before any node factory runs. The existing
   `max_tool_rounds`/`max_recur_limit` check moves into the validator, so it is no longer
   evaluated after LLM creation.
2. `build_default_config()` validates the config **after** the environment overlay, so a
   malformed `TRADINGAGENTS_*` value fails at import time, before any run begins.
3. `cli.run_analysis` validates the assembled CLI config before it constructs the graph, and
   `cli.main.analyze` prints the problems as a red block and exits non-zero (nothing is
   written, no model is called). The graph keeps its own check, so a caller that bypasses
   the CLI is protected too.

**A6. Compatibility.** No valid configuration behaves differently. The validator adds no
network access, reads no credential, and changes no vendor, LLM-SDK or memory behaviour.

## 4. Decision — Requirement B: sanitise invalid optional numerics instead of rejecting

**B1. The rule.** In the enhanced modes, an optional numeric output field
(`entry_price`, `stop_loss` for the Trader; `price_target` for the Portfolio Manager) that
cannot be read as one unambiguous finite number is **set to `None` (absent)**. Every other
field of the same answer is kept exactly as the model supplied it, and the answer is
accepted if its **required** fields are valid. This mirrors the baseline coercer's intent
(`tradingagents/agents/schemas.py:33-58`) while staying deterministic and diagnosable.

**B2. What "cannot be read" means.** Booleans, NaN and ±Infinity, percentages (`"15%"`),
ranges (`"150-160"`), hedges (`"around 150"`), placeholders (`"N/A"`, `"none"`, `"tbd"`),
empty strings and non-numeric text all become `None`. Accepted formatting is unchanged:
a finite JSON number, and an explicit numeric string such as `"189.5"`, `"$1,234.50"`,
`"-3.5"`, `"1e3"`. No positivity, sign or magnitude rule is added; a zero or negative price
stays valid.

**B3. Every accepted route.** Sanitation runs for a structured result (a strict-schema
instance, a mapping), a plain-text answer (labelled Markdown, bare or fenced JSON) and a
repaired answer. When the provider's `with_structured_output` uses the native JSON-schema
parser, the strict schema no longer *rejects* an optional numeric; the raw value is carried
into the validated payload as an `InvalidNumber` marker and sanitised by the contract layer,
so the native parser cannot discard the whole answer for one optional field.

**B4. Required fields are unchanged.** An invalid `rating`/`action`/`recommendation`, or a
blank required narrative, is still a contract violation that aborts under `validate` and
gets the existing single bounded repair under `validate+repair` (ADR 001 §2.3-2.5). Nothing
here weakens that.

**B5. Cost.** A numeric-only defect spends **no** additional model call: it is repaired
locally. The recovery slot is not used, and the accounting counts exactly one structured
call.

**B6. Diagnostics.** Each sanitised field produces a `ContractIssue` with the stable code
`sanitized_invalid_number` and a neutral reason (`nonfinite`, `boolean`, `percentage`,
`range`, `hedge`, `placeholder`, `not_a_number`, `unsupported_type`, `conflicting_json`).
The issue holds the field name, the code and the reason — never the model's text — so the
record, the `## Contract Validation` block and the audit file cannot leak content. The
issues are collected in `ValidationRecord.sanitized` and in the sanitised payload itself, so
a caller can report them. A sanitised payload carries the strict model's
`invalid_numbers` field: a mapping of field name to reason, JSON-serialisable, kept out of
the rendered markdown (which therefore stays byte-identical to the baseline renderer for the
same content).

**B7. Protocol for the native parser.** The strict schemas accept an *invalid* optional
numeric only in a form that a JSON-schema parser can produce (string, number, boolean,
`null`, a list, a mapping) and store it as `InvalidNumber(value)`. Any other Python object
still raises. A text answer that states a numeric field twice with different values, or a
JSON object that states one, becomes `None` with a `conflicting_json` diagnostic rather than
an arbitrary pick.

**B8. Off mode is untouched.** `off` continues to bind the baseline schemas, whose coercer
and rendered output are unchanged byte for byte. Sanitation exists only in enhanced modes.

**B9. Version bump.** `AGENT_CONTRACT_VERSION` becomes `"2"` in its three declared places
(`tradingagents/agents/contracts.py`, `tradingagents/default_config.py`, and the asserted
twin in `tests/test_agent_contracts.py`), so a checkpoint written under the old strict-reject
assumption is not resumed (`tradingagents/graph/trading_graph.py:196-205`).

**B10. Superseded expectations.** T-6, T-26 and the free-text and stale-plan numeric
assertions that required an abort for an invalid *optional* numeric are superseded **only**
for optional numeric output fields. T-5, T-28, T-10, T-11, T-29 and every off-mode assertion
are retained unchanged. The supersession list, with old and new expectations and
traceability, is `docs/verification/increment-v3-test-spec.md` §5.

## 5. Decision — Requirement C: report integrity manifest

**C1. A manifest beside the reports.** Whenever `tradingagents.reporting.write_report_tree`
saves a report tree (which is what both the CLI and `TradingAgentsGraph.save_reports` do),
it writes `report_manifest.json` in the report root **after** the stale-file reconciliation
and after every report file, including `complete_report.html` when HTML was requested.

**C2. Contents.** A versioned JSON document:

```json
{
  "manifest_version": 1,
  "algorithm": "sha256",
  "generator": "tradingagents.report_integrity",
  "generated_at": "2026-11-23T12:00:00",
  "ticker": "AAPL",
  "file_count": 3,
  "files": [{"path": "1_analysts/market.md", "size": 3, "sha256": "…"}],
  "excluded": ["report_manifest.json"]
}
```

`path` is always relative, `/`-separated, and recorded in sorted order for determinism.
The manifest lists every file this run wrote and nothing else: the section markdown,
`complete_report.md`, `complete_report.html` when requested. It never lists itself, and it
never lists unrelated files a user keeps in the same folder.

**C3. Regeneration describes only the current run.** A second save into the same folder with
fewer sections, or with HTML turned off, writes a manifest that names only the files that
exist after the reconciliation. User notes and any other file the writer does not own are
kept on disk and simply absent from the manifest.

**C4. Offline verification.** A new leaf module `tradingagents/report_integrity.py` provides
`verify_report_manifest(report_dir) -> VerificationResult` and a command

```powershell
.venv\Scripts\python.exe -m tradingagents.report_integrity <REPORT_DIR>
```

Exit code 0 when every listed file exists with the recorded size and digest; non-zero when
any listed file is **missing** or **modified**, or when the manifest itself is missing,
unreadable, malformed or for an unsupported version. The report states each problem by path.

**C5. Path safety.** An entry that is absolute, contains a drive or UNC prefix, contains a
`..` component or is otherwise not a plain relative path inside the report root is rejected
before any file is opened. A listed path that resolves (following links) outside the report
root, or whose ancestor inside the root is a symbolic link or a reparse point, is rejected.
Verification never reads a file outside the root.

**C6. What this does *not* claim.** The manifest detects divergence between the saved
report files and the manifest. It is **not** an authenticity or signature mechanism: an
attacker who can rewrite both a report and its manifest can produce a matching pair. No
key, signature or network call is involved.

**C7. This is not the contract audit summary.** The `## Contract Validation` block
(`tradingagents/reporting.py:92-106`) is a prose summary of agent contract outcomes. The
manifest is a file-integrity record with digests. They are separate artifacts with separate
purposes and neither substitutes for the other.

**C8. No new dependency or network path.** The implementation uses `hashlib`, `json`, `os`,
`pathlib` and `datetime` from the standard library only.

## 6. Source scope for this increment

May be created or modified:

- `tradingagents/config_validation.py` (new)
- `tradingagents/report_integrity.py` (new)
- `tradingagents/default_config.py`
- `tradingagents/agents/contracts.py`
- `tradingagents/agents/contracts_plain.py`
- `tradingagents/graph/trading_graph.py`
- `tradingagents/reporting.py`
- `cli/run.py`, `cli/main.py`
- `tests/test_config_validation.py` (new)
- `tests/test_numeric_sanitation.py` (new)
- `tests/test_report_integrity.py` (new)
- focused additions to `tests/test_agent_contracts.py`,
  `tests/test_agent_contracts_integration.py`, `tests/test_reporting.py` and
  `tests/test_report_html.py` only where this ADR or its test specification requires them
- governance and verification documents under `docs/`, plus `AGENTS.md` and `Prompt.md`

Explicitly out of scope (unchanged from ADR 001 §6, plus): `tradingagents/agents/schemas.py`
(*any* change, because `off` must stay byte-identical), `tradingagents/report_html.py`
behaviour, `tradingagents/llm_clients/*`, `tradingagents/dataflows/*`,
`tradingagents/memory/*`, `tradingagents/graph/setup.py`, `tradingagents/graph/conditional_logic.py`,
packaging, `pyproject.toml`, CI workflows, `.opencode/rules.md`, `.env*`, credentials,
brokerage or trading execution, any GitHub operation.

## 7. Consequences

Positive: a bad configuration fails before any side effect and names every offending
parameter; one bad optional price no longer throws away a valid decision or a model call; a
saved report can be checked against what the run wrote.

Negative and accepted: the validator must be kept in step with new configuration keys (a new
key not listed is simply not checked, never wrongly rejected); a sanitised field is silently
absent downstream unless a reader looks at the diagnostics, which is why the diagnostics are
recorded in the contract record and the report's `## Contract Validation` block; the manifest
is regenerated only by a save, so a folder edited by hand after a save is detectable but a
folder edited before the manifest existed is not.

Residual risks: the manifest inherits the trust of the filesystem it sits on (C6); a model
that writes a price as prose ("around 150") loses the level, by design; a configuration key
added by a future version is not range-checked until it is added to the validator.

## 8. Implementation order

1. This ADR and `docs/verification/increment-v3-test-spec.md`, plus the authorisation record
   `docs/approvals/agent-contract-v3.md` — documents only.
2. `tradingagents/config_validation.py`, then its call sites; tests.
3. Numeric sanitation in `contracts.py` / `contracts_plain.py`, version bump; tests.
4. `tradingagents/report_integrity.py`, then the writer integration and the CLI command;
   tests.
5. Verification: focused tests, full offline suite, `ruff check .`, the agreed `mypy`
   command and the JSON-schema check; evidence recorded under `docs/verification/evidence/`.
