# Runtime Policy Catalogue

Runtime policies for the approved agent I/O contract increment. Each policy names where it
is applied, what it bounds, and the evidence for the bound. "Configured" means the value is
a repository-exposed setting; "unknown" means the value cannot be observed at this layer.

## 1. Operational modes

| Policy | Values | Default | Where |
|---|---|---|---|
| RP-01 Contract mode | `off`, `validate`, `validate+repair` | `off` | New key `agent_contract_mode` in `tradingagents/default_config.py` |

`off` is the default so that a run without the new setting behaves exactly as baseline.

## 2. Invocation budgets

| Policy | Bound | Meaning | Evidence |
|---|---|---|---|
| RP-02 Logical invocations per agent | <= 2 | `structured` plus at most one of `free_text_recovery` or `semantic_repair` | Design decision, ADR §2.4; baseline shows two calls today (`tradingagents/agents/structured.py:92-95`) |
| RP-03 Semantic repairs per agent | <= 1 | Only in `validate+repair`; zero in `validate` and `off` | ADR §2.5 |
| RP-04 Structured-to-free-text recoveries per agent | <= 1 | Retained for compatibility in `validate` and `validate+repair`, counted and validated | ADR §2.3 |
| RP-05 SDK transport retries | Configured budget exposed; observed attempts unknown | `llm_max_retries` forwarded to each provider client; when unset, the provider SDK default applies and is **not** assumed here | `tradingagents/default_config.py:117-120`, `tradingagents/llm_clients/factory.py:123-125` |

No policy claims a measured HTTP attempt count, a token cost, or a latency figure.

## 3. Recovery rules

| Policy | Rule |
|---|---|
| RP-06 Recoverable classes | Parse errors and validation errors only. |
| RP-07 Non-recoverable classes | Transport errors and programming errors. They are classified and surfaced; they are not converted into a successful fallback. |
| RP-08 Revalidation | A recovered payload is revalidated in full against the same contract. |
| RP-09 No third call | No uncounted fallback may be added alongside the single recovery. |

## 4. Failure policies

| Policy | Rule |
|---|---|
| RP-10 Exception type | `AgentContractError` only; no blanket `except Exception` around the abort path. |
| RP-11 Abort scope | Dependent graph execution stops; all risk debators and the PM do not run after a Trader failure. |
| RP-12 Audit | A failed-run audit entry is written with failure status and no decision. |
| RP-13 Recording | `record_decision` is skipped; no new memory-log entry or reflection. |
| RP-14 Checkpoint | `clear_checkpoint_on_success` is skipped; the checkpoint is retained. |
| RP-15 No substitution | No Hold, rating, price, zero or prose invented for a failure. |
| RP-16 Settlement separation | Historical settlement may run concurrently; it is neither undone nor prevented. |

## 5. Checkpoint policies

| Policy | Rule |
|---|---|
| RP-17 Signature content | Mode and `AGENT_CONTRACT_VERSION` participate in `_run_signature` (`tradingagents/graph/trading_graph.py:140-165`). |
| RP-18 Resume revalidation | On enhanced-mode resume, saved `investment_plan` and `trader_investment_plan` are revalidated before downstream use. |
| RP-19 No migration claim | Old state annotations are not treated as proof that no revalidation is needed. |

## 6. Output policies

| Policy | Rule |
|---|---|
| RP-20 Renderer stability | Valid payloads render byte-identically to baseline renderers. |
| RP-21 Report stability | Section paths and headings unchanged; failures add `## Contract Validation`. |
| RP-22 Record stability | Error records are ordered and timestamp-free. |

## 7. Scope policies

| Policy | Rule |
|---|---|
| RP-23 No execution | No brokerage or trading execution. |
| RP-24 No credentials | No credential, `.env`, auth or secret file access. |
| RP-25 No new network path | None added by this increment. |
| RP-26 Layering | Vendor libraries imported only by the data layer (`tests/test_layering.py:26-35`). |
