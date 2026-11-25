# Structural constraints — proposed

| ID | Constraint | Existing evidence / next step |
|---|---|---|
| SC-T01 | Agent-level code should use the dataflow boundary for vendors; provider SDK imports remain in the data layer. | `tests/test_layering.py`; confirm exact scope before asserting universal coverage. |
| SC-T02 | Do not introduce broker execution without a separately approved requirement. | Architecture scope in `architecture-blueprint.md`; team review needed. |
| SC-T03 | Keep trace session, callback, and writer responsibilities distinguishable. | `tradingagents/observability/`; existing callback coverage remains limited. |
| SC-T04 | Preserve time-aware Memory review and separate Memory from checkpoint and cache responsibilities. | `tradingagents/memory/`, `tests/test_memory_review.py`; verify changes with relevant tests. |
| SC-T05 | Do not delete or weaken existing tests solely to make CI green. | Proposed team rule, not yet an enforced GitHub policy. |
