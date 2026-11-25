# Bug diary — evidence-based draft

| ID | Finding | Discovery / effect | Change / verification | Evidence boundary |
|---|---|---|---|---|
| BD-01 | Concurrent expiry of a date-range cache entry could cause a double deletion. | A controlled offline interleaving reproduced one `KeyError` in the old PR #6 head. | PR #6 added a lock around cache operations and concurrency tests. | Timing-dependent; a forced test interleaving is not a production incident report. |
| BD-02 | `inf` / `-inf` closing prices were treated as usable. | An offline fake provider returned `inf`; the function accepted it and overwrote an existing CSV. | PR #6 added finite-value checks and tests for invalid refresh and mixed values. | The earlier baseline also lacked the finite-value check; do not state PR #6 introduced the original gap. |
| BD-03 | Two Yahoo range-cache tests failed on Windows due to CSV newline expectations. | Windows returned CRLF within the CSV body while tests expected LF; comparison showed the same Windows output before and after the PR for the tested sample. | Codex changed the test expectation to account for the platform newline; targeted tests and hosted CI passed. | Test portability issue; no demonstrated business-output regression in the compared sample. |
| BD-04 | PR #4 conflicted with merged changes in three integration files. | Git merge reported conflicts in CLI, graph and Portfolio Manager. | Codex integrated both sides and added interaction tests; the final integration passed hosted checks. | Do not claim the conflicts themselves were production bugs or that every possible combination was tested. |

Each member should review this account and add their own verified findings, dates, evidence and corrections from their complete sessions. This is not a substitute for the original records.
