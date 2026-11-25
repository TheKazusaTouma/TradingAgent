# TradingAgents Team Project Requirements Specification

**Consolidated draft — pending team review**

> This document was derived retrospectively from merged pull requests, source code, and tests. It does not imply that every requirement has been approved by the team or the instructor, or that existing tests constitute final acceptance.
>
> Reviewed revision: GitHub `main` commit `a60f3507849108dd2de55880c5597db32539fbdf`. Draft date: 25 November 2026. This document does not modify the existing `docs/req/requirements.yaml` or `req-schema.json`. After team review, the agreed requirements should be reconciled with those files and the schema in the detailed course project specification.

## 1. Background and objectives

TradingAgents uses multiple analyst roles and model calls, together with market data and historical information, to produce analysis results. This team project enhances the system in four areas: execution traceability, the use of historical memory, Yahoo market-data caching, and agent input/output and report integrity.

The project aims to help the team understand what happened during an analysis, control when historical reflections are used, reduce certain repeated market-data requests, and establish verifiable handling of invalid inputs and outputs for selected agents. External market data, model services, and execution environments remain sources of uncertainty. This specification does not claim that trading recommendations will be correct or profitable.

## 2. Sources and scope

| Area | PR | GitHub PR author | Scope covered here |
|---|---|---|---|
| Memory context budget | [#2](https://github.com/TheKazusaTouma/TradingAgent/pull/2) | Nice-Kirito7 | Character budget, priority ordering, and historical-date filtering |
| Agent input/output and reports | [#4](https://github.com/TheKazusaTouma/TradingAgent/pull/4) | kevinma9 | Contracts for three agents, startup preflight, numeric sanitation, and report integrity |
| Trace ID | [#5](https://github.com/TheKazusaTouma/TradingAgent/pull/5) | TheKazusaTouma | Per-attempt trace ID, lifecycle, and model/tool callbacks |
| Yahoo caching | [#6](https://github.com/TheKazusaTouma/TradingAgent/pull/6) | ShariQri | Market-data caching, request coalescing, recovery, finite-value checks, and statistics |
| Memory review | [#7](https://github.com/TheKazusaTouma/TradingAgent/pull/7) | Nice-Kirito7 | Human approval and revocation of historical reflections, applied at the analysis time |

The table identifies PR authors on GitHub. It **does not establish actual task allocation, every contributor, effort spent, or the tools used to produce the code**. Related PRs by one author are grouped by project area; the team must confirm contributions against its records.

The `G-FR` and `G-NFR` identifiers are specific to this consolidated draft. They do not replace the `FR` and `NFR` identifiers in the existing `requirements.yaml` and do not assert compliance with the detailed project requirement sheet, which has not yet been obtained.

## 3. System boundary and roles

- **User:** starts an analysis through the CLI or Python API and views its results and execution records.
- **Memory reviewer:** uses the CLI to inspect settled reflections, approve or revoke them, and view review history.
- **Developers and testers:** maintain code, configuration, tests, reports, and requirements-traceability materials.
- **External dependencies:** model services, Yahoo market data, and existing data providers; requests may fail, time out, or return invalid data.
- **Scope:** the existing system provides analysis and recommendations. The changes described here do not promise brokerage integration, automatic trade execution, or investment returns.

## 4. Functional requirements and proposed acceptance criteria

The criteria below were derived from the current PRs and code for team review. Referenced tests are **existing evidence entry points**; their presence does not mean they were rerun while preparing this document. Final acceptance should record the tested revision, environment, and results.

### 4.1 Trace ID — PR #5

| ID | Requirement | Proposed acceptance criterion |
|---|---|---|
| G-FR-T01 | Each analysis execution attempt through the CLI or Python API shall receive its own Trace ID. | Two executions produce different full UUID4 values. The ID is available in CLI output or through the Python API's `last_trace_id`. |
| G-FR-T02 | The system shall record the start, successful end, or failure of an execution. | A successful path records `run_start` and `run_end`; an exception path records `run_start` and `run_fail` and propagates the exception to the caller. Success here means analysis execution and decision recording are complete; it excludes a later choice to save a report. |
| G-FR-T03 | For model and tool calls visible to the callbacks, the system shall record start, end, or error events and associate events from the same call. | The start and end or error events of a call can be paired by `run_id`. An end event records duration when a corresponding start event is available. |
| G-FR-T04 | New trace records for different execution attempts shall be stored separately. | The JSONL path includes the ticker, analysis date, and Trace ID. Sequential or tested concurrent attempts do not write different Trace IDs into the same trace file. |

**Evidence entry points:** `tradingagents/observability/`, `tradingagents/graph/trading_graph.py`, `cli/run.py`, and `tests/test_trace_observability.py`.

**Boundary:** Existing callbacks do not cover SDK-internal retries, the complete vendor fallback chain, or calls inside bare threads such as JeV. A normal tool completion does not establish that its business data is valid. Run-level isolation of the existing `message_tool.log`, state logs, report directories, and checkpoints is not completed by this requirement.

### 4.2 Memory — PRs #2 and #7

| ID | Requirement | Proposed acceptance criterion |
|---|---|---|
| G-FR-M01 | The system shall support a configurable character budget for injected Memory context. | When configured, the complete returned text, including headings and separators, does not exceed the limit. A zero budget returns an empty string. |
| G-FR-M02 | When selecting settled historical records within the budget, the system shall consider ticker and date priority and support a shorter form. | Same-ticker records have priority, and records within each group are ordered by decision date from newest to oldest. If a full record does not fit, the system tries a short form built from existing fields; if that also does not fit, it may skip the record and continue. |
| G-FR-M03 | Historical memory selection shall follow analysis-date filtering so that settled records unavailable at the analysis time are not treated as available history. | Construct historical test data across dates and verify that selected records satisfy the existing date-filtering rules. |
| G-FR-M04 | When Memory review is enabled, a reviewer shall be able to inspect, approve, revoke, and view the review history of settled reflections. | The CLI displays reflections for review; approval requires confirmation; revocation creates a corresponding history record; unsettled entries cannot be approved as settled reflections. |
| G-FR-M05 | When review is enabled, the use of a historical reflection shall correspond to its ticker, decision date, reflection version, and analysis time. | A reflection that is unapproved or revoked at the relevant time does not enter model input. Approval of an old version does not automatically approve changed text. Live and historical analyses use the current execution time and the analysis day's UTC end respectively as review cutoffs. |
| G-FR-M06 | Reports shall be able to show the memory actually received by the Portfolio Manager, and review status shall be rechecked when resuming from a checkpoint. | Use controlled examples to compare the report with actual input. Revoke an approval before resume and verify that the reflection is not injected after resume. |

**Evidence entry points:** `tradingagents/memory/log.py`, `tradingagents/memory/review.py`, `cli/memory.py`, `tests/test_memory_log.py`, `tests/test_memory_review.py`, `tests/test_cli_memory_review.py`, and `tests/test_graph_end_to_end.py`.

**Boundary:** `memory_review_enabled` is off by default. When no character budget is configured, the original selection behavior applies. Character count is not token count and cannot establish an exact token saving. Failure handling and report notices for a corrupted review ledger should be checked against the relevant code and tests.

### 4.3 Yahoo market-data caching — PR #6

| ID | Requirement | Proposed acceptance criterion |
|---|---|---|
| G-FR-C01 | Yahoo date-range market-data requests shall support a capacity-bounded, expiring in-process cache. | Identical requests reuse an entry within its TTL; different tickers or date windows do not share entries; expired entries are fetched again. Current defaults are a capacity of 64 entries and a TTL of 900 seconds. |
| G-FR-C02 | Actually overlapping requests for the same cache key within one process shall share data loading. | In a controlled concurrent test, two same-key requests wait for one vendor load and receive independent results. Requests with different keys need not be coalesced. |
| G-FR-C03 | The system shall detect invalid or corrupted Yahoo CSV caches and attempt recovery. | An empty, malformed, or unusable cached file triggers a refetch; an invalid refresh does not overwrite an existing valid file. |
| G-FR-C04 | The date-range and OHLCV paths shall reject data with no usable finite closing price and handle data that fails the applicable freshness condition. | Closing prices consisting only of `inf` or `-inf` are not accepted as valid; stale responses are not stored as successful cache entries; a later request can retry. |
| G-FR-C05 | The system shall provide in-process cache statistics. | Counters cover memory hits, disk hits, coalesced waits, load attempts, and load failures. One failed shared load is counted once, not once per waiting caller. |

**Evidence entry points:** `tradingagents/dataflows/vendors/yahoo/cache.py`, `market.py`, and `ohlcv.py`; `tests/test_yahoo_range_cache.py`, `tests/test_yahoo_coalescing.py`, `tests/test_ohlcv_cache_recovery.py`, and `tests/test_cache_stats.py`; `scripts/README.md`.

**Boundary:** The original project already used `yfinance` and a five-year OHLCV CSV cache. PR #6 adds date-range memory caching and enhances concurrency, recovery, and statistics. The cache and coalescing mechanism operate within a process; cross-process and cross-machine sharing are not promised. A shared load may include retries inside a provider SDK, so load counts are not necessarily HTTP request counts.

### 4.4 Agent contracts, preflight, and reports — PR #4

| ID | Requirement | Proposed acceptance criterion |
|---|---|---|
| G-FR-V01 | The system shall provide input/output contracts and controlled recovery modes for the Research Manager, Trader, and Portfolio Manager. | Test structured and compatible free-text paths separately. In an enabled validation mode, invalid required content cannot enter downstream state as a validated result. |
| G-FR-V02 | For an unrecoverable contract failure, the system shall stop dependent execution and retain an identifiable failure audit. | In a controlled failure scenario, the run records no successful decision and performs no successful checkpoint cleanup; the failure reaches the caller. Concurrent settlement of historical decisions is outside this rollback guarantee. |
| G-FR-V03 | Before starting analysis, the system shall validate configuration and identify invalid parameters. | When an invalid configuration is supplied through the CLI or graph construction, an error is reported before model client calls and workflow execution. |
| G-FR-V04 | In enhanced validation modes, unreadable optional numeric fields shall be sanitized according to the defined rules; required-field failures shall still stop the affected execution. | Invalid optional numbers are marked missing and produce diagnostics; valid content is preserved; sanitation does not fabricate valid required content. |
| G-FR-V05 | When saving reports, the system shall generate an integrity manifest and provide a checking command. | Saving generates `report_manifest.json`. After a listed file is modified or deleted, `python -m tradingagents.report_integrity REPORT_DIR` reports the discrepancy. |

**Evidence entry points:** `tradingagents/agents/contracts.py`, `tradingagents/config_validation.py`, `tradingagents/report_integrity.py`, `docs/architecture/adr/001-agent-io-contracts.md`, `docs/architecture/adr/002-preflight-sanitation-manifest.md`, `tests/test_agent_contracts_integration.py`, `tests/test_config_validation.py`, `tests/test_numeric_sanitation.py`, and `tests/test_report_integrity.py`.

**Boundary:** The default contract mode is `off`. Enhanced validation and optional numeric sanitation require `validate` or `validate+repair`. Startup preflight and the report manifest apply in every mode. The manifest detects divergence; it does not prove authenticity if both the manifest and reports are rewritten.

## 5. Non-functional requirements and proposed verification

| ID | Quality attribute | Verifiable requirement or verification method | Related work |
|---|---|---|---|
| G-NFR-01 | Observability | Trace events from tested executions can be associated by Trace ID and distinguish start, success, and error, within the actual visibility of callbacks. | G-FR-T01–T04 |
| G-NFR-02 | Trace data minimization | Trace events do not record prompts, tool arguments, model responses, API keys, or exception text; errors record only exception class names. Verify that controlled sensitive samples do not appear in the trace. | PR #5; `tests/test_trace_observability.py` |
| G-NFR-03 | Trace fault tolerance | A trace write or serialization failure produces a warning while analysis continues. This is the current implementation policy; its adoption as a final requirement remains to be confirmed. | PR #5 |
| G-NFR-04 | Bounded context | When a character budget is configured, context length does not exceed it. This is not a token hard limit. | PR #2 |
| G-NFR-05 | Request efficiency | With the same mocked market-data sample, date range, and valid TTL, verify that 10 sequential requests produce one mocked vendor load. Do not directly extrapolate this result to live networks or other vendors. | PR #6; `tests/test_cache_stats.py` |
| G-NFR-06 | Concurrency correctness | Controlled same-key concurrent and expiration scenarios do not raise a duplicate-deletion exception; a shared load's result or error is propagated correctly. | PR #6; `tests/test_yahoo_coalescing.py` |
| G-NFR-07 | Cache recovery | An invalid download must not overwrite a valid existing CSV; verify by comparing file bytes before and after refresh. | PR #6; `tests/test_ohlcv_cache_recovery.py` |
| G-NFR-08 | Compatibility | With Memory review disabled and contract mode `off`, retain the compatibility behavior stated by the relevant PRs. Demonstrate specific scope through regression tests rather than claiming all system behavior is unchanged. | PRs #4 and #7 |
| G-NFR-09 | Bounded recovery | In enabled contract-validation modes, allow at most one recovery call per agent per run under the existing rules; an exhausted invalid result must not be fabricated as success. | PR #4; `tests/test_agent_contracts.py` |
| G-NFR-10 | Report integrity | Detect tested modifications or omissions of report files covered by the manifest. The manifest is not a tamper-proof signature. | PR #4; `tests/test_report_integrity.py` |

These are candidate non-functional requirements derived from existing capabilities. The team must confirm their priorities, owners, formal thresholds, and how the course counts the required number of functional and non-functional requirements.

## 6. Requirements traceability and current evidence

| Requirement group | Implementation entry point | Verification entry point | Evidence status |
|---|---|---|---|
| Trace | `tradingagents/observability/` and CLI/API entry points | `tests/test_trace_observability.py` | Tests exist; not rerun for this draft |
| Memory | `tradingagents/memory/`, `cli/memory.py` | `tests/test_memory_log.py`, `tests/test_memory_review.py`, `tests/test_graph_end_to_end.py` | Tests exist; not rerun for this draft |
| Yahoo caching | `tradingagents/dataflows/vendors/yahoo/` | `tests/test_yahoo_range_cache.py`, `tests/test_yahoo_coalescing.py`, `tests/test_cache_stats.py` | Offline tests and mocked demonstrations exist; not rerun for this draft |
| Contracts and reports | `tradingagents/agents/contracts.py`, `tradingagents/config_validation.py`, `tradingagents/report_integrity.py` | `tests/test_agent_contracts_integration.py`, `tests/test_config_validation.py`, `tests/test_report_integrity.py` | Tests exist; not rerun for this draft |

`docs/architecture/trace-matrix.md` already exists, but it mainly covers agent contracts and its closing statement that no tests exist is outdated. After confirming requirements, the team should update the formal traceability matrix to connect requirements across all areas to designs, code, tests, and execution results.

## 7. Dependencies, risks, and commitments outside scope

1. Live market data and model calls depend on external services. Offline mocked results do not directly establish live reliability, latency, or cost.
2. The first Trace version does not cover every underlying retry or vendor fallback and does not isolate every existing run artifact.
3. Memory review and enhanced contract modes are disabled by default. Demonstrations must record their configuration rather than presenting optional features as enabled by default.
4. The Yahoo date-range cache is in-process; the CSV cache follows an existing separate path. They should not be described as one mechanism.
5. Existing evidence is primarily offline automated testing. The team must confirm whether the course requires live model or market demonstrations and how their results should be recorded.
6. The project does not promise correct analysis, perfectly real-time data, investment returns, or unmeasured performance targets.

## 8. Items for team confirmation

1. The formal scope, identifiers, priorities, and owners of the team's four areas, and the course's interpretation of the required number of functional and non-functional requirements.
2. Whether “warn and continue” on trace-write failure should be a formal acceptance policy, and whether checkpoint recovery attempts need linkage.
3. Whether Memory review and contract validation should be enabled in the final demonstration and submission, and which failure cases must be covered.
4. What acceptance evidence is required for mocked versus live services, and whether the course specifies performance, token, or latency thresholds.
5. How PR authors correspond to actual team task allocation, development, and review contributions; retain accurate records of OpenCode, Codex, and any other tools used.
6. After obtaining the course Project Requirement Sheet, check D1 and the other detailed requirements, then decide whether and how to update `requirements.yaml`, the schema, and the formal traceability matrix.
