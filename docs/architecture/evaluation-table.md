# Evaluation table — draft

| Area | Baseline / problem | Change | Evidence currently available | Limit or missing result |
|---|---|---|---|---|
| Trace | No per-attempt trace in PR #5's baseline | UUID per attempt, callback-visible model/tool events and lifecycle JSONL | PR #5 and `tests/test_trace_observability.py` | No complete SDK retry or vendor-fallback visibility; write-failure policy needs confirmation. |
| Memory budget | Long historical context can consume model context | Configurable character limit and priority | PR #2 reports one offline 4,125-to-113 character example | Example-specific; no exact token or quality improvement established. |
| Memory review | Historical reflections are automatically eligible under old behavior | Optional approval and revocation with analysis-time cutoff | PR #7 and `tests/test_memory_review.py` | Disabled by default; actual human-review effort not measured. |
| Yahoo cache | Repeated same-window downloads in the tested baseline | Bounded cache, coalescing and recovery | PR #6 reports 10 mocked sequential calls reduced from 10 to 1 | Mocked example, not a live benchmark; SDK internal retries are separate. |
| Agent validation | Existing routes could accept or propagate invalid outputs | Optional contracts, bounded recovery, preflight, integrity manifest | PR #4 and contract/report tests | Optional contract mode defaults to off; no claim about financial truth. |

The values above are descriptions of cited PR evidence, not newly measured results. Add reproducible environment, data, commands, sample size, baseline revision, and results before making broader performance or quality claims.
