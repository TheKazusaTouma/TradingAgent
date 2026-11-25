# Ten-minute project demonstration — proposed script

The course slides request a separate ten-minute video comparing system behavior before and after new requirements, highlighting software-engineering ideas, and demonstrating traceability and agent governance. The detailed requirement sheet may add constraints. Times below are suggestions, not official rules. No video has been recorded by this draft.

## Preparation

- Choose and record the baseline revision for **each** feature. PRs have different bases; do not assume a single baseline without checking. Prepare a separate checkout or controlled fixture for each comparison.
- Record the reviewed revision, configuration, data source, commands, and whether the run is live or mocked. Enable `memory_review_enabled` and the desired `agent_contract_mode` only when demonstrating those optional features.
- Rehearse offline fixtures first. If live provider calls are required, use authorized credentials and disclose that outcome and timing can vary.
- Hide API keys, private files, unrelated chats, and confidential data. Do not invent before/after results or show a passing run recorded for a different revision as current evidence.

| Time | Segment | Suggested evidence |
|---|---|---|
| 00:00–01:00 | Explain the original project, four improvement areas, and versions. | Architecture diagram and one-sentence problem per area. |
| 01:00–02:00 | Trace ID. | Show one controlled run and its trace file; match start, model/tool callback and end/fail by ID. Explain coverage limits. |
| 02:00–03:00 | Memory budget and review. | Use a prepared settled reflection; compare selection and show approve/revoke at a documented cutoff. Explain review is off by default. |
| 03:00–04:00 | Yahoo caching. | Use the documented mocked demo to compare request and load counts; clearly label it offline and show invalid-refresh preservation if time allows. |
| 04:00–05:00 | Agent validation and report integrity. | Show an invalid input or output in an enabled mode and the corresponding failure; demonstrate integrity check after a controlled report-file change. |
| 05:00–07:00 | Requirement-to-test traceability. | Pick representative requirements and show their mapping to design, code, tests and actual results on the selected revision. |
| 07:00–09:00 | Human supervision and governance. | Show sanitized, genuinely attributed session excerpts, a discovered defect and correction, review records, CI and actual blocking rules. Do not treat a policy file as proof of enforcement. |
| 09:00–10:00 | Results and limits. | State observed results, skipped or mocked checks, default-off features and unresolved limitations. |

Team members should confirm who appears or speaks with the instructor; the available slides do not explicitly require every member to appear or record a separate video. The video is submitted separately, not inside the project ZIP.
