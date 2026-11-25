# Option ledger — team review draft

This ledger records options and trade-offs inferred from merged PRs. It does not assert that the team formally evaluated every option before implementation.

| Topic | Option reflected in code | Alternative | Trade-off / evidence |
|---|---|---|---|
| Trace failures | Warn and continue | Abort analysis on trace failure | Availability versus guaranteed trace completeness; PR #5 and `tests/test_trace_observability.py`. Final policy pending team approval. |
| Memory | Optional human review | Always use automatically selected reflections | More control versus reviewer effort; PR #7. Review is disabled by default. |
| Context size | Character budget | Token-based budget | Simple deterministic bound versus model-dependent token accounting; PR #2. |
| Caching | In-process bounded cache and coalescing | Shared distributed cache | Simpler deployment versus no cross-process sharing; PR #6. |
| Contracts | Optional enhanced validation with bounded recovery | Mandatory validation or unlimited retry | Compatibility and bounded cost versus an explicit opt-in and possible unrecovered failures; PR #4. |
| Report integrity | File manifest | Signed or externally anchored manifest | Detects divergence with low setup cost; does not authenticate a jointly rewritten manifest and report. |
