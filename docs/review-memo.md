# Review memo — known evidence and pending review

## Known review and integration activities

- PR #6: two issues were reproduced in an earlier review: a concurrent expired-cache deletion could raise `KeyError`, and `inf` closing prices could be accepted. The later PR includes fixes and regression tests. The `inf` gap also existed in the earlier baseline; do not attribute its origin solely to PR #6.
- PR #6: two Windows tests initially failed because their expected CSV used Linux line endings. A subsequent Codex-authored test-only change adjusted the expected platform newline. The associated run reported 71 targeted tests passed and the updated PR's six hosted CI checks passed.
- PR #4: integration with already merged Trace and Memory changes required conflict resolution in the CLI, graph, and Portfolio Manager. Integration tests were added, the latest `main` was incorporated, and six hosted checks passed before the PR was merged.
- Source `main` commit `a60f3507849108dd2de55880c5597db32539fbdf`: [hosted CI](https://github.com/TheKazusaTouma/TradingAgent/actions/runs/45533884389) reports six successful checks. This does not approve later documentation changes.

## Pending team review

- Review the actual PR diffs, current code, 30 proposed requirement records and all new draft documents.
- Confirm individual contribution, original OpenCode sessions and any Codex use; do not claim a person's review or approval without their confirmation.
- Review defaults, trace boundaries, branch enforcement, live-versus-mocked evaluation, and the detailed course requirement sheet.
- Record reviewer, revision, date, findings, resolution, and approval only when known. No team approval is asserted in this file.
