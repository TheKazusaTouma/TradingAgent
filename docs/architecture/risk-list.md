# Risk list — draft

| ID | Risk | Current handling or evidence | Residual issue / action |
|---|---|---|---|
| R-01 | Trace misses SDK retries, vendor fallback, or bare-thread work | Callback boundaries documented in PR #5 | Do not claim complete trace coverage; decide whether expansion is in scope. |
| R-02 | Same-ticker/date runs can collide in existing logs or state | New JSONL traces use per-attempt IDs | Existing report, state, and checkpoint paths are not all isolated. |
| R-03 | Unapproved or outdated memory reaches a model | Optional review binds a reflection version and cutoff; tests cover review and resume | Review is off by default; verify demo configuration and corrupted-ledger behavior. |
| R-04 | Stale, invalid, or duplicated Yahoo loads affect results | Cache freshness, recovery, coalescing, and offline tests in PR #6 | Offline mocked tests do not establish live provider quality or availability. |
| R-05 | Invalid agent output reaches dependent execution | Optional contract modes, bounded recovery and audit in PR #4 | Mode is off by default; contracts do not establish financial truth. |
| R-06 | Report manifest and files are both rewritten | Manifest checks divergence | A digest manifest is not an authenticity guarantee. |
| R-07 | Sensitive data appears in transcripts or ZIP | `.env` is Git-ignored; draft uses `git archive` | Manually inspect all final artifacts and exports; do not publish credentials. |
| R-08 | Course compliance assumed from incomplete material | Introductory slides reviewed | Obtain the detailed Project Requirement Sheet and clarify D1–D9 and count/format requirements. |
| R-09 | CI is mistaken for a blocking rule | Six source-revision checks passed; a main protection rule was configured and read back on 2026-11-25. | The new requirements workflow is not in `main` or required by the rule; verify settings remain active and conduct a safe enforcement exercise if needed. |
| R-10 | Contribution or process claims are inaccurate | PRs and current conversation establish limited facts | Each member supplies and confirms contributions and actual agent session records. |
