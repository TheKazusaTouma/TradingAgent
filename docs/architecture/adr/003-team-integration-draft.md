# ADR 003 — Integrate the team's four increments

**Status:** Draft for team review. **Source revision:** `a60f3507849108dd2de55880c5597db32539fbdf`.

## Context

The merged PRs add trace records, bounded and optionally reviewed memory, Yahoo cache safeguards, and agent contract/report safeguards. These changes meet different concerns but share the CLI, graph, Portfolio Manager, configuration, and tests. PR #4 had conflicts with previously merged work in those boundaries.

## Decision reflected in the merged implementation

Keep the existing graph and dataflow layers, integrate the changes at their existing boundaries, and use deterministic regression tests to exercise interactions. PR #4 was merged after `main` was integrated into its branch; the resulting source revision passed the six hosted CI jobs. Optional Memory review and enhanced agent contracts remain off by default.

## Alternatives and consequences

- An invasive graph redesign might provide a uniform framework but would increase integration and regression risk; the merged work uses existing integration points.
- Enabling every safeguard by default would change existing behavior; current defaults preserve the documented compatibility paths but require explicit configuration for demonstrations of optional features.
- Trace events improve diagnosis but are incomplete for SDK retries and vendor fallbacks. Separate report and state artifacts can still collide on repeated same-date runs.
- A report manifest can detect file divergence but cannot authenticate a report if the manifest itself is rewritten.

## Evidence and pending decisions

See [PR #4](https://github.com/TheKazusaTouma/TradingAgent/pull/4), `tests/test_agent_contracts_integration.py` (including combined trace/memory/contract cases), and the [main CI run](https://github.com/TheKazusaTouma/TradingAgent/actions/runs/45533884389). The team still needs to approve the architecture, feature defaults, trace-write failure policy, and course-specific acceptance criteria.
