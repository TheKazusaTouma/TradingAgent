# CS5351 team project report — draft

**Submission status: incomplete.** This is an evidence-based draft for team review, not a final report. Member identities, actual contributions, human decisions, session transcripts, course-specific acceptance, and live demonstration results remain to be supplied and verified. The detailed Project Requirement Sheet was not available when this draft was prepared.

## 1. Project overview

The team extended the TradingAgents multi-agent analysis application in four areas: execution traceability, Memory selection and human review, Yahoo market-data caching and recovery, and agent input/output validation with preflight and report-integrity checks.

The reviewed source is `main` commit `a60f3507849108dd2de55880c5597db32539fbdf`. The human-readable requirements are in `docs/team-requirements-draft.md`; the consolidated YAML and trace mapping are drafts pending team approval.

## 2. Original problems and intended changes

| Area | Problem addressed | Implemented direction | Known limit |
|---|---|---|---|
| Trace | Analysis failures and callback-visible calls are difficult to correlate across attempts. | Per-attempt UUID, JSONL lifecycle events, model/tool callback events. | SDK-internal retries, complete fallback chains, and some bare-thread work remain invisible. |
| Memory | Historical context may be long, and unrestricted reflection reuse may not match a review policy. | Optional character budget and optional human approval/revocation tied to reflection version and analysis time. | A character budget is not a token budget; review is off by default. |
| Yahoo cache | Repeated requests and unusable CSV data can waste work or affect analysis. | In-process cache, same-key coalescing, invalid-cache recovery, finite-close validation and statistics. | Mocked evaluation is not a live-network benchmark; coordination is process-local. |
| Agent I/O and reports | Invalid configuration or decision-agent content may not be caught at desired boundaries. | Preflight, optional contracts and bounded recovery, numeric sanitation, report manifest. | Contract mode defaults to `off`; a manifest is not an authenticity signature. |

## 3. Requirements and design

The draft proposes 20 team-wide functional and 10 non-functional requirement records, in addition to the 29 original PR #4 records. These counts are document records, not a claim about how the instructor counts the required N meaningful requirements. IDs, priorities and acceptance criteria need team and course-specification confirmation.

The conceptual architecture is in `architecture-blueprint.md`; the team-wide mapping from proposed requirements to code and tests is in `docs/architecture/team-traceability.md`. The existing contract-specific trace matrix is retained with a correction to its stale status note. The team must review coverage and complete results, owners and formal acceptance.

## 4. Implementation and integration evidence

- Memory budget: PR #2; optional review and analysis-time adoption: PR #7.
- Agent I/O, preflight, sanitation and report integrity: PR #4.
- Trace ID: PR #5.
- Yahoo cache and recovery: PR #6.
- After PRs #5, #6 and #7 were merged, PR #4 incorporated the then-current `main`; GitHub reports its final merge at `a60f3507849108dd2de55880c5597db32539fbdf`.

These PRs identify repository changes; they do not by themselves prove member workload, authorship method, or fulfillment of every course criterion.

## 5. Evaluation

The [source revision's GitHub CI run](https://github.com/TheKazusaTouma/TradingAgent/actions/runs/45533884389) reports six successful checks: tests on Python 3.11, 3.12, 3.13 and 3.14, clean-install smoke, and Ruff. These results apply to that source revision, not automatically to this later documentation draft.

On 2026-11-25, a local Windows run on this source snapshot with the draft documentation and requirement checker reported 2093 passed, 11 skipped, 1 deselected and 100 subtests passed; skipped cases included platform-specific checks and an optional Bedrock dependency. The requirement schema check passed and rejected controlled invalid-status and duplicate-ID examples. PR #6 reports one mocked comparison in which 10 sequential same-key date-range requests used one mocked load. PR #2 reports one offline context example of 4,125 characters before and 113 after with a 2,000-character budget. These are scoped observations, not general live-service performance or token-savings claims.

A separate WSL 2 Ubuntu run with Python 3.14.4 reported 2103 passed, 1 skipped, 1 deselected and 100 subtests passed. The skipped case required the optional `langchain_aws` package. Linux dependencies were downloaded via Windows and installed offline because WSL could not reach the package index. These results describe the tested draft at that time, not later untested changes.

See `docs/architecture/evaluation-logbook.md` and `evaluation-table.md`. Add independently verified commands, environment, revision, sample data, metrics, limitations and actual outcomes for final acceptance. Live-service verification is **not claimed** here.

## 6. AI use, review and human supervision

The introductory course slides state that assessment is based on OpenCode and that full coding-agent session transcripts are primary evidence of student direction and supervision. PR #4's description explicitly mentions OpenCode; the user supplied OpenCode implementation reports for the Trace work. The available PR records do not establish the coding tool used for every commit.

Codex was used in this conversation to resolve PR #4 integration conflicts and add integration tests, to adjust a Windows test expectation in PR #6, to review and merge PRs when instructed, and to prepare this documentation draft and its schema-check script. These activities must be attributed accurately; they must not be presented as OpenCode sessions. The team should check with the instructor how the course treats mixed coding-agent use.

`docs/ai-use-log.yaml`, `review-memo.md`, `bug-diary.md`, and `debt-register.md` contain draft records of known evidence and open gaps. The team must supply actual complete, annotated session records in accordance with course instructions; `transcripts/README.md` is only a collection guide.

On 2026-11-25, Codex configured the repository's `main` protection to require a PR and the six existing checks, including for administrators; the API read-back is documented in `docs/code-governance/branch-protection.md`. No end-to-end failing-PR enforcement exercise is claimed. The new requirements-schema workflow is not in `main` and is not yet a required GitHub check.

## 7. Team contributions — information pending

Do not infer actual contributions or hours from GitHub PR authors alone. Each member should replace the relevant row with verified facts and approve the description.

| Member / student ID | Confirmed responsibilities and outputs | Evidence | Contribution measure |
|---|---|---|---|
| To be supplied | To be supplied | To be supplied | To be agreed |
| To be supplied | To be supplied | To be supplied | To be agreed |
| To be supplied | To be supplied | To be supplied | To be agreed |
| To be supplied | To be supplied | To be supplied | To be agreed |

## 8. Reflections and limitations — team input required

Each member should add their own accurately attributed reflection: what they instructed the agent to do, how they inspected the result, an actual mistake or trade-off encountered, how it was corrected, and what they would change. Do not replace individual reflections with invented examples.

The team must also resolve: course counting of the N requirements; the full Project Requirement Sheet; final configuration for optional features; the Trace fail-open policy; runtime MCP inventory; real-service demonstration scope; and whether draft governance files are actually loaded and enforced by the installed tool version.

## 9. Final submission checklist

- [ ] Team approves requirements and traceability; verify detailed course schema and directory structure.
- [ ] Update and verify architecture, governance, risk, review, debt and bug records.
- [ ] Record all four members' contributions and individual reflections.
- [ ] Add complete, appropriately annotated and safely handled session transcripts for all members.
- [ ] Run and record checks for the exact submission revision; distinguish skips and live versus mocked results.
- [ ] Record a separate 10-minute before/after demo covering software-engineering ideas, traceability and agent governance.
- [ ] Inspect for API keys and private data. Prepare the project ZIP without the demo video.
