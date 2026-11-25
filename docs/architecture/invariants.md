# Invariants — Agent I/O Contracts (v1)

An invariant is a property that must hold in every run where its mode is enabled. Each
invariant lists the requirement it implements and the tests that must demonstrate it.
The table's "Proposed" labels are historical design-stage labels: implementation and
tests have since been added. They do not establish current team approval or a result for
the final submission. See `docs/verification/increment-v3-evidence.md` for historical
scoped evidence and record current results separately.

| ID | Invariant | Req | Tests | Status |
|---|---|---|---|---|
| INV-1 | No validation-failure text appears in `investment_plan`, `trader_investment_plan`, `final_trade_decision`, or any risk-debate prompt. | FR-8, FR-9 | T-15, T-16, T-17 | Proposed |
| INV-2 | A failed Portfolio Manager writes no `final_trade_decision` and no `final_rating` other than absence or `REVIEW`. | FR-9 | T-15, T-19 | Proposed |
| INV-3 | An input-invalid agent makes zero structured and zero plain model invocations. | FR-7 | T-3, T-4 | Proposed |
| INV-4 | At most two logical invocations per agent per run: `structured` + at most one of `free_text_recovery` / `semantic_repair`. | FR-5, FR-10 | T-11, T-12, T-27 | Proposed |
| INV-5 | The same input yields a byte-identical error record. | FR-6 | T-14 | Proposed |
| INV-6 | Absent, `None` or blank analyst reports are valid optional input; no analyst selection is required. | FR-1, FR-4 | T-2, T-3 | Proposed |
| INV-7 | Mode `off` output and shared schema behaviour are byte-identical to baseline. | FR-3, FR-11 | T-1, T-24, T-26 | Proposed |
| INV-8 | A failed run writes no new memory-log decision, no reflection for a new decision, no success checkpoint cleanup. Historical settlement may run concurrently and is neither undone nor prevented. | FR-8 | T-15, T-20, T-30 | Proposed |
| INV-9 | A failed run writes a failed-run audit entry distinguishable from a successful decision, and retains its checkpoint when checkpointing is enabled. | FR-6, FR-8 | T-18, T-19, T-30 | Proposed |
| INV-10 | Mode and contract version participate in the checkpoint signature; enhanced-mode resume revalidates saved upstream payloads. | NFR-7 | T-21, T-22 | Proposed |
| INV-11 | A valid payload renders byte-identically to baseline renderers and keeps every report section path/heading. | FR-11 | T-24 | Proposed |
| INV-12 | No new graph node, edge, vendor, credential path or trading execution is introduced. | FR-12 | T-23 | Proposed |

## Enforcement notes

- Invariants 1-4 are structural: they follow from the abort exception plus the risk
  guards and the invocation ceiling, and are asserted by counters and marker absence.
- Invariants 5 and 11 are stability properties and are asserted by repeated evaluation and
  byte comparison, not by inspection.
- Invariant 7 is a compatibility property; it is asserted by the existing suite in mode
  `off` plus a rendering comparison.
- Invariant 8 deliberately excludes historical settlement. The memory step runs beside
  the analysts (`tradingagents/graph/trading_graph.py:309-336`), and
  `tradingagents/memory/settlement.py:136-193` may settle earlier decisions in the same
  run. The invariant constrains **new** decisions and their reflections only.

## Non-invariants (explicitly not claimed)

- Financial truth of any claim in a thesis, rationale or reasoning field.
- Presence of evidence identifiers; no such structure exists for these three agents.
- Observed HTTP attempt counts or token costs.
- That a failed run removes previously written successful artifacts.
