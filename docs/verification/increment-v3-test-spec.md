# Increment v3 Test Specification — Preflight, Numeric Sanitation, Report Integrity

PR packaging note: approval records and full OpenCode transcripts refer to local development and are retained separately. They are excluded from this PR. Team review and merge approval are still pending. See docs/verification/increment-v3-evidence.md for integration verification.

- Version: **3.0** (concrete, implementation-independent expectations)
- Status: **Specification approved for implementation by the student's latest authorisation**
  (`docs/approvals/agent-contract-v3.md`). Rows in §7 are filled only from real command
  output.
- Amends: `docs/verification/agent-contract-test-spec.md` v1.0, for the optional-numeric
  policy only (§5). Every other v1.0 expectation is retained unchanged.
- Design: `docs/architecture/adr/002-preflight-sanitation-manifest.md`.
- Baseline: TradingAgents v0.7.1, commit `c4544913a14ed3df5f1ed7b9e8483270ce20682a`.
- Language: English.

## 0. Conventions

- **Offline.** No network, no provider, no credential, no `.env`. The suite's autouse
  fixtures already refuse sockets (`tests/conftest.py:59-76`).
- **Independence.** Every expectation below is written from the ADR and the repository
  evidence cited in it, not from the implementation. Where an expectation supersedes a v1.0
  assertion, §5 states the old and the new wording side by side.
- **Modes.** Every test names its `agent_contract_mode`. No test relies on the default.
- **Files.** `tests/test_config_validation.py` (new), `tests/test_numeric_sanitation.py`
  (new), `tests/test_report_integrity.py` (new), plus focused additions to
  `tests/test_agent_contracts.py`, `tests/test_agent_contracts_integration.py`,
  `tests/test_reporting.py`.
- **Test ids.** `PF-n` (preflight), `NS-n` (numeric sanitation), `RI-n` (report integrity).
- **"Spy"** means an object that records its own invocation; "zero calls" means the recorded
  count is zero for **every** spy in the test.

## 1. Requirement identifiers

| ID | Statement |
|---|---|
| FR-A | Invalid configuration is reported, naming every offending parameter, before any LLM client is constructed or called and before any filesystem side effect. |
| FR-B | An invalid **optional** numeric field in an AI answer becomes absent (`None`); every other field is preserved and the answer is accepted when its required fields are valid. No extra model call is spent on a numeric-only defect. |
| FR-C | Every saved report tree carries `report_manifest.json` listing the versioned relative path, byte size and SHA-256 digest of each report file actually written, excluding the manifest; an offline command verifies it. |
| INV-13 | Mode `off` output, shared schema behaviour and existing valid-configuration behaviour stay byte-identical to baseline. |
| INV-14 | At most two logical model invocations per agent per run stays true; sanitation never adds one. |
| INV-15 | No credential, `.env`, provider-config or secret value is read, logged or echoed; configuration errors are redacted. |
| INV-16 | A failed preflight performs no filesystem side effect: no results directory, no cache directory, no report, no audit file. |
| INV-17 | Report verification never reads outside the report root and refuses absolute, traversal and unsafe-symlink entries. |
| NFR-8 | No dependency, no network path, no new graph node or edge is added. |

## 2. Fixtures

| Fixture | Content |
|---|---|
| Valid programmatic config | `copy.deepcopy(DEFAULT_CONFIG)` with `results_dir`, `data_cache_dir`, `memory_log_path` under `tmp_path`. |
| Valid keyless/custom-endpoint config | `llm_provider="openai_compatible"`, `backend_url="http://localhost:8000/v1"`, `deep_think_llm`/`quick_think_llm` = a custom identifier not in any catalogue, no key consulted. |
| Valid local-provider config | `llm_provider="ollama"`, `backend_url` unset, custom model identifier. |
| Malformed programmatic configs | One per rule in §3, each with exactly the named key damaged. |
| Multiple-defect config | Contract mode `"strict"`, `max_debate_rounds=-1`, `max_tokens=0`, `temperature=float("nan")`, `deep_think_llm="   "`, `backend_url="not a url"`. Six problems expected. |
| Malformed env configs | The same defects supplied as `TRADINGAGENTS_*` strings, e.g. `TRADINGAGENTS_MAX_DEBATE_ROUNDS="-1"`, `TRADINGAGENTS_MAX_TOKENS="0"`, `TRADINGAGENTS_TEMPERATURE="nan"`, `TRADINGAGENTS_AGENT_CONTRACT_MODE="strict"`, `TRADINGAGENTS_LLM_BACKEND_URL="not a url"`, `TRADINGAGENTS_LLM_PROVIDER=" "`. |
| F-03 trader payload | `{"action": "Buy", "reasoning": "...", "entry_price": 189.5, "stop_loss": 172.0, "position_sizing": "5% of portfolio"}` (the v1.0 F-01 Trader fixture). |
| F-03 PM payload | `{"rating": "Overweight", "executive_summary": "...", "investment_thesis": "...", "price_target": 210.0, "time_horizon": "3-6 months"}`. |
| Report tree | `write_report_tree` over a small state with `market_report`, `news_report`, `investment_plan`, `trader_investment_plan`, `final_trade_decision`. |

## 3. Configuration rules to be demonstrated (Requirement A)

Every rule below is asserted twice unless stated: once as a failure case (the key is named)
and once as an accepted case (the legal boundary).

| Key(s) | Rule | Rejected examples | Accepted examples |
|---|---|---|---|
| `agent_contract_mode` | one of `off`, `validate`, `validate+repair`, case/space insensitive | `"strict"`, `"on"`, `""`, `1` | `"off"`, `" Validate "`, `None` |
| `max_debate_rounds`, `max_risk_discuss_rounds` | non-negative `int`, never `bool` | `-1`, `1.5`, `"two"`, `True` | `0`, `3` |
| `holding_period_days`, `news_article_limit`, `global_news_article_limit` | non-negative `int` | `-1`, `1.5` | `0`, `5` |
| `global_news_lookback_days`, `max_tool_rounds`, `max_recur_limit`, `max_tokens` | positive `int` | `0`, `-1`, `True` | `1`, `20` |
| `llm_max_retries` | non-negative `int`, numeric string allowed | `-1`, `1.5`, `"abc"`, `True` | `0`, `"6"`, `None` |
| `max_tool_rounds` + `max_recur_limit` | `2 * max_tool_rounds + 2 < max_recur_limit` | `(20, 42)`, `(20, 41)` | `(20, 100)`, `(0, 3)` |
| `temperature` | finite `float` in `[0, 2]`, numeric string allowed | `float("nan")`, `float("inf")`, `-0.1`, `2.1`, `"hot"`, `True` | `0`, `1.0`, `"0.2"`, `None` |
| `deep_think_llm`, `quick_think_llm` | non-blank `str` | `""`, `"   "`, `None`, `5` | any custom identifier such as `"my-local-model"` |
| `llm_provider`, `deep_think_provider`, `quick_think_provider` | non-blank `str` naming a supported or registry-known provider (case insensitive); `None` = "use `llm_provider`" | `""`, `"   "`, `"nonesuch"`, `3` | `"openai"`, `"Ollama"`, `"openai_compatible"`, `None` |
| `backend_url`, `deep_think_backend_url`, `quick_think_backend_url` | `None`, `""` or an `http`/`https` URL with a host, no whitespace, no userinfo | `"localhost:8000"`, `"ftp://x"`, `"http://"`, `"http://a b"`, `"http://user:pw@h/v1"` | `None`, `"http://localhost:8000/v1"`, `"https://example.com/v1"` |
| `checkpoint_enabled` | `bool` | `"yes"`, `1` | `True`, `False` |
| `data_vendors`, `tool_vendors`, `benchmark_map` | mapping with `str` keys and `str` values | `"yfinance"` (a string), `{"k": 3}`, `{1: "v"}` | `{}`, `{"core_stock_apis": "yfinance"}` |
| `benchmark_ticker` | `None` or non-blank `str` | `""`, `5` | `None`, `"SPY"` |
| `memory_log_max_entries` | `None` or non-negative `int` | `-1`, `1.5` | `None`, `0`, `10` |
| `output_language` | non-blank `str` | `""`, `None` | `"English"` |
| any other key | allowed | — | `{"future_extension_key": object()}` is not reported |

Redaction: a detail never contains a value that looks like a credential. A supplied
`backend_url` with userinfo, or a value of the shape `sk-…`/`key=…`, appears as
`<redacted>`.

## 4. Expected behaviour

### Requirement A — startup preflight

**PF-1 — Every invalid parameter is named, all at once.**
- **Given** the multiple-defect config of §2.
- **Then** `validate_config` returns exactly six problems, covering
  `agent_contract_mode`, `max_debate_rounds`, `max_tokens`, `temperature`,
  `deep_think_llm`, `backend_url`; each problem's `parameter` is the dotted key; each
  `detail` is non-empty; the message of `ConfigValidationError` names all six.

**PF-2 — A malformed programmatic config fails at graph construction with no side effect.**
- **Given** one damaged config per §3 rule, `TradingAgentsGraph(config=cfg)`.
- **Spies:** `create_tier_client` (patched on `tradingagents.graph.trading_graph`), the node
  factories reached through `GraphSetup.setup_graph`, and a `results_dir`/`data_cache_dir`
  pair that does not exist before the call.
- **Then** `ConfigValidationError` is raised; the client-factory spy recorded **zero** calls;
  the node-factory spy recorded **zero** calls; neither directory exists afterwards.

**PF-3 — The order is preflight first even when the graph's own later check would also fail.**
- **Given** `max_tool_rounds=20`, `max_recur_limit=30` (which the old code rejected after LLM
  creation) with a **valid** everything else.
- **Then** `ConfigValidationError` names both keys, and the client-factory spy recorded zero
  calls. This is the regression for the old ordering
  (`tradingagents/graph/trading_graph.py:103-119`).

**PF-4 — A valid configuration is constructed unchanged.**
- **Given** the valid programmatic config and a patched client factory returning a stub.
- **Then** no error, the factory is called exactly twice (deep, quick), and the results and
  cache directories exist.

**PF-5 — Custom endpoints and keyless providers are accepted.**
- **Given** the keyless/custom-endpoint config and the local-provider config.
- **Then** `validate_config` returns no problem for either, no environment variable is read
  by the validator (asserted by patching `os.environ.__getitem__`-driven reads off via a
  sentinel mapping), and `TradingAgentsGraph` with a patched client factory constructs.

**PF-6 — A partial config is merged with defaults explicitly, and an invalid supplied value is
not replaced.**
- **Then** `effective_config({"max_debate_rounds": 2})` has every `DEFAULT_CONFIG` key, with
  `max_debate_rounds == 2`; `effective_config({"max_debate_rounds": -2})` still reports the
  problem and does not silently substitute the default `1`.

**PF-7 — Environment-derived defaults fail at import/build time.**
- **Given** each malformed env config of §2 applied with `monkeypatch.setenv` and every other
  `TRADINGAGENTS_*` variable blanked.
- **Then** `build_default_config()` raises `ConfigValidationError` naming the offending key;
  the variable's own name appears in the detail so a reader can fix it.

**PF-8 — A valid environment overlay builds a valid config.**
- **Given** `TRADINGAGENTS_MAX_DEBATE_ROUNDS="3"`, `TRADINGAGENTS_TEMPERATURE="0.2"`,
  `TRADINGAGENTS_LLM_MAX_RETRIES="6"`, `TRADINGAGENTS_AGENT_CONTRACT_MODE="validate"`.
- **Then** no error; the merged config keeps the same coercion results as before
  (`3`, `"0.2"`, `"6"`, `"validate"`).

**PF-9 — The CLI path fails before any side effect and before the graph.**
- **Given** `flags` and `TRADINGAGENTS_*` selected so `cli.run._build_run_config` yields a
  config with `max_tokens="0"` and `temperature="hot"` (damage injected in the selections
  the CLI takes from the user).
- **Spies:** `cli.run.TradingAgentsGraph`, `cli.run.Live`, `pathlib.Path.mkdir`.
- **Then** `run_analysis(...)` raises `ConfigValidationError` naming `max_tokens` and
  `temperature`; the graph spy recorded zero calls; no `Live` block was entered; no
  `mkdir` call happened for the run directory; `cli.main.analyze` with the same input prints
  a red failure block naming both keys and exits with a non-zero code.

**PF-10 — Unknown extension keys stay allowed.**

**PF-11 — No import cycle and no runtime dependency on the vendor layer.**
- `import tradingagents.config_validation` succeeds in a fresh interpreter without importing
  `tradingagents.dataflows`, `tradingagents.graph` or `tradingagents.llm_clients`.
- `tradingagents/config_validation.py` imports no vendor top-level module (the existing
  layering test still passes unchanged).

### Requirement B — optional-numeric sanitation

**NS-1 — One invalid price becomes `None`; every other field is exactly preserved.**
- **Mode** `validate`. **Route** structured (a `StrictTraderProposal`-shaped mapping for the
  Trader; the native `with_structured_output` path later in NS-7).
- **Given** `entry_price="15%"` and otherwise the valid F-03 payload.
- **Then** the invocation succeeds; `payload.entry_price is None`; `payload.stop_loss == 172.0`,
  `payload.action == "Buy"`, `payload.reasoning` and `payload.position_sizing` are the exact
  supplied strings; `record.ok is True`; `record.calls.structured == 1`,
  `free_text_recovery == 0`, `semantic_repair == 0`, `logical_total == 1`; the structured and
  plain callables were each invoked **once** (no extra call); `record.sanitized` contains
  exactly one issue `(field="entry_price", code="sanitized_invalid_number")` whose detail is
  one of the neutral reasons and does **not** contain `"15%"`.

**NS-2 — Several invalid prices all become `None`, charged once.**
- **Given** `entry_price="150-160"`, `stop_loss=float("nan")`.
- **Then** both are `None`, every other field is preserved, two sanitised issues are recorded,
  one structured call only.

**NS-3 — Every invalid class is sanitised, for both agents and both numeric fields.**
- Parameterised over `"15%"`, `"150-160"`, `"around 150"`, `True`, `float("nan")`,
  `float("inf")`, `float("-inf")`, `""`, `"N/A"`, `"abc"`, `[150]`, `{"lo": 1}`, and the fields
  `entry_price`, `stop_loss`, `price_target`.
- **Then** the field is `None`, the answer is accepted when everything else is valid, and one
  sanitised issue names the field.

**NS-4 — Valid numerics are preserved unchanged; no sign or magnitude rule.**
- Parameterised over `189.5`, `"189.5"`, `"$1,234.50"`, `-3.5`, `0.0`, `1e3`.
- **Then** the field equals the expected finite float, no sanitised issue is recorded.

**NS-5 — An explicit valid omission stays absent, with no diagnostic.**
- `None`, the key omitted, `"not provided"`, `"N/A"`.
- **Then** the field is `None` and `record.sanitized == []`.

**NS-6 — A bad required field is still refused.**
- `rating="Neutral"`, `action="Long"`, `executive_summary="  "`, `reasoning=""` — each combined
  with a **valid** price.
- **Then** `AgentContractError` raised, the required field named, and no state written. With
  `validate+repair` the existing single repair applies (exactly one `semantic_repair`).

**NS-7 — The native `with_structured_output` path sanitises before schema rejection.**
- **Given** a real LangChain `with_structured_output(strict schema)` runnable whose tool
  argument JSON contains `"entry_price": "15%"`, driven through the real Trader node.
- **Then** the node returns a plan whose entry price line reads `not provided`, the other
  fields are the model's exact text, no `ValidationError` was raised inside the bind, and the
  `contract_records` entry carries the sanitised issue. Equivalently, at the schema boundary:
  `strict_schema.model_validate({"action": ..., "entry_price": "15%"})` succeeds and the
  value is marked invalid rather than raising.

**NS-8 — The plain-text routes sanitise identically.**
- Labelled Markdown (`**Entry Price**: 15%`), bare JSON and fenced JSON, through
  `Scripted` plain callables that return a real `AIMessage`.
- **Then** as NS-1 for each route; `logical_total` is the same as without the defect.

**NS-9 — A repaired answer is sanitised too.**
- **Mode** `validate+repair`; the first answer has a blank `executive_summary`, the repaired
  answer has a valid narrative and `price_target="8%"`.
- **Then** the repaired answer is accepted, `price_target is None`, its narrative preserved,
  and the sanitised issue is recorded against the repair stage.

**NS-10 — A numeric-only defect does not spend the recovery slot.**
- The structured answer has `entry_price=True` and **no** plain callable is available.
- **Then** the invocation still succeeds with one structured call; a plain callable, when
  supplied, is never invoked.

**NS-11 — A conflicting numeric statement becomes `None` with a diagnostic.**
- A text answer stating `**Entry Price**: 189.5` and `**Entry Price**: 200`, and a JSON object
  stating `"entry_price": 189.5` twice.
- **Then** the field is `None`, the other fields are preserved, and one sanitised issue with
  reason `conflicting_json` is recorded.

**NS-12 — The diagnostic record is stable, content-free and JSON-safe.**
- The same defect twice yields byte-identical `ValidationRecord.canonical_json()`; the raw
  model text never appears in the record, the `## Contract Validation` block or the failure
  audit; the payload's invalid-number marker is JSON-serialisable.

**NS-13 — Off mode is unchanged.**
- The baseline coercer still maps `"15%"`, `"150-160"`, `"around 150"`, `""` to `None`, and
  `True` to `1.0`; the rendered report for a valid payload is byte-identical to the baseline
  renderer; no contract record key appears.
- **Then** every existing off-mode test passes unchanged.

**NS-14 — The saved-plan revalidation follows the same policy.**
- A rendered trader plan with `Entry Price: 15%` is revalidated for the Trader's input and for
  the risk-debator guard: the plan is **not** refused for that numeric alone (its required
  action and reasoning are present); a plan with a blank reasoning is still refused.

**NS-15 — The contract version moved on.**
- `AGENT_CONTRACT_VERSION == "2"` in `tradingagents/agents/contracts.py`, in
  `tradingagents/default_config.py`, and in the test that asserts they agree; a checkpoint
  signature built under `"1"` differs from one built under `"2"`.

### Requirement C — report integrity manifest

**RI-1 — Saving writes a valid manifest.**
- **Given** `write_report_tree(state, "AAPL", tmp_path, html=True)`.
- **Then** `report_manifest.json` exists, parses as JSON, has `manifest_version == 1`,
  `algorithm == "sha256"`, and its `files` entry set equals exactly the set of report files
  written on disk (section markdown, `complete_report.md`, `complete_report.html`); every
  `size` equals `len(bytes)` and every `sha256` equals `hashlib.sha256(bytes).hexdigest()` of
  the real file; the manifest does not list itself; paths are relative with `/` separators.

**RI-2 — A pristine tree verifies.**
- **Then** `verify_report_manifest(tmp_path).ok is True`, with zero missing and zero modified,
  and the CLI command exits 0.

**RI-3 — An edited byte is reported as modified.**
- Append/overwrite one byte in `1_analysts/market.md`.
- **Then** verification fails, names that path as modified, and reports no other problem; the
  CLI exits non-zero.

**RI-4 — A deleted file is reported as missing.**
- Delete `complete_report.md`.
- **Then** verification fails and names it as missing.

**RI-5 — Regeneration describes only the current run.**
- Save again into the same folder with fewer sections and `html=False`, after writing
  `notes.md` and `1_analysts/notes.md`.
- **Then** the new manifest lists exactly the files present for the new state (no deleted
  section files, no HTML); verification passes; both user notes still exist with their
  original content and are absent from the manifest.

**RI-6 — The manifest is absent or malformed: a clear failure, not a crash.**
- No manifest → a failure naming the missing manifest; malformed JSON → a failure naming it
  as malformed; a manifest with `algorithm != "sha256"` or `manifest_version != 1` → an
  unsupported-version/algorithm failure. The CLI exits non-zero in each case and prints the
  reason; a report directory that does not exist exits non-zero.

**RI-7 — Path escapes are refused and never read.**
- A manifest listing `../outside.md`, `/etc/passwd`, `C:\Windows\win.ini`, `\\server\share\x`,
  `sub/../../x`, `1_analysts/../../x`: verification reports an unsafe-path problem for each
  and opens none of them. A file placed just outside the root with a manifest pointing at it is
  not read.

**RI-8 — Symbolic links are handled safely.**
- A listed file that is a symlink to a target outside the root is refused; a directory inside
  the root that is a symlink is refused; a symlink pointing **inside** the root is accepted
  when its content matches. On Windows, where creating a symlink may require a privilege, the
  test skips with an explicit reason instead of a false pass.

**RI-9 — The CLI exit codes are exactly as specified.**
- `python -m tradingagents.report_integrity <dir>`: `0` valid; `1` missing/modified/unsafe;
  `2` manifest missing or malformed. Output names each offending path.

**RI-10 — The manifest is not the contract audit summary.**
- A report with enhanced-mode contract records contains both a `## Contract Validation`
  block in `complete_report.md` and a `report_manifest.json` whose content is a digest list,
  not prose.

**RI-11 — No network, no new dependency.**
- `tradingagents/report_integrity.py` imports only `hashlib`, `json`, `os`, `pathlib`,
  `datetime` and the `sys`-based CLI entry; the existing layering test still passes.

## 5. Superseded expectations and traceability

The student's latest authorisation explicitly changes the optional-numeric policy. A v1.0
expectation that required an abort for an invalid **optional** numeric is therefore
superseded, and only that: this table lists every change.

| v1.0 item | Old expectation (verbatim summary) | New expectation | Authorised by |
|---|---|---|---|
| T-6 (`test_t6_nonfinite_rejected`) | "Each raises `AgentContractError` naming the field and the nonfinite reason. The value is **not** converted to `None`…" | The field becomes `None`, one `sanitized_invalid_number` issue with reason `nonfinite` is recorded, the answer is accepted, other fields preserved. Explicit `None`/omitted still valid. See NS-3. | ADR 002 §4; student authorisation §1 |
| T-26 (`test_t26_strict_path_rejects_raw_invalid_numbers`) | "each raises `AgentContractError` naming the field and the reason; none becomes `None` and passes" | Each becomes `None` and the answer is accepted when required fields are valid; one sanitised issue names the field. Off-mode half of T-26 retained verbatim. See NS-3, NS-13. | ADR 002 §4 |
| T-26 (`test_t26_boolean_is_not_accepted_as_one`) | raise on `True` | `True` becomes `None` (still never read as `1.0`) and is recorded as sanitised. Off-mode behaviour (`True`→`1.0`) retained as an observation. See NS-3, NS-13. | ADR 002 §4 |
| T-26 (`test_t26_off_mode_boolean_behaviour_is_unchanged`) | off mode `True`→`1.0`; validate raises | off-mode half retained unchanged; the validate half now asserts sanitation. See NS-13. | ADR 002 §4 |
| T-29 (`test_t29_plain_numeric_label_is_parsed_strictly_before_coercion`) | a plain `**Entry Price**: 15%` raises | it becomes `None`, the other fields are kept, and one sanitised issue names the field. See NS-8. | ADR 002 §4 |
| T-22 variant (`test_d_stale_saved_payload_is_refused_before_consumption`, row `trader_investment_plan`/`entry_price`) | a saved plan with `15%` is refused at INPUT | a saved plan whose only defect is an optional numeric is accepted (numeric absent); a saved plan with a blank required narrative is still refused. See NS-14. | ADR 002 §4 |
| T-22 (`test_d_risk_debator_refuses_a_plan_with_an_invalid_price`) | the debator refuses such a plan | the debator accepts it (numeric absent) and calls its model; the refusal case for a blank required field is retained. See NS-14. | ADR 002 §4 |

Retained **without change** (explicitly not superseded): T-2, T-3, T-4, T-5, T-7, T-8, T-9,
T-10, T-11, T-12, T-13, T-14, T-15..T-21, T-23, T-24, T-25, T-27, T-28, T-30, every off-mode
assertion, and the `mismatched_schema` rejection of a baseline-coerced model (T-29).

Nothing in this section authorises weakening an unrelated test. A test that is neither listed
above nor about optional numeric output keeps its assertion.

## 6. Requirement traceability

| Test | Requirements |
|---|---|
| PF-1 | FR-A, INV-15 |
| PF-2 | FR-A, INV-16 |
| PF-3 | FR-A (ordering), INV-16 |
| PF-4 | FR-A, INV-13 |
| PF-5 | FR-A (no key requirement; custom models), INV-15 |
| PF-6 | FR-A (explicit default merge; no silent substitution) |
| PF-7, PF-8 | FR-A (env-derived defaults), INV-13 |
| PF-9 | FR-A (CLI path), INV-16 |
| PF-10, PF-11 | FR-A, NFR-8 |
| NS-1, NS-2, NS-3, NS-4, NS-5, NS-8, NS-9 | FR-B |
| NS-6 | FR-B (required fields unchanged), INV-14 |
| NS-7 | FR-B (native parser) |
| NS-10 | FR-B, INV-14 |
| NS-11, NS-12 | FR-B (diagnostics), INV-15 |
| NS-13 | INV-13 |
| NS-14 | FR-B, INV-14 |
| NS-15 | FR-B (version bump) |
| RI-1, RI-2, RI-3, RI-4, RI-5 | FR-C |
| RI-6, RI-7, RI-8, RI-9 | FR-C, INV-17 |
| RI-10, RI-11 | FR-C, NFR-8 |

## 7. Result log

Unedited command output and a per-test matrix are recorded after implementation in
`docs/verification/increment-v3-evidence.md`. Rows are filled only from a real run.
