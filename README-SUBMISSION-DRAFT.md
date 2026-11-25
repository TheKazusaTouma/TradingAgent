# CS5351 submission draft — team review required

This directory is an independent working draft based on TradingAgent `main` commit `a60f3507849108dd2de55880c5597db32539fbdf`. It includes the source tree exported by `git archive` and draft course artifacts. It is not the team's final submission; any draft branch must be reviewed before merging.

## Read first

1. Confirm the course's detailed Project Requirement Sheet. Only the introductory slides were available when this draft was prepared; their directory diagram does not establish every field, process rule, or acceptance threshold.
2. Review `docs/team-requirements-draft.md` and the candidate records added to `docs/req/requirements.yaml`. Proposed requirement IDs, classifications, criteria, and priorities are for review, not evidence of team approval.
3. Complete `Report.md` and the contribution table with the four members' actual details. Verify every project-specific claim, date, and result against evidence.
4. Supply each student's complete, accurately attributed, annotated coding-agent transcript under `transcripts/`. The templates are not transcripts. Inspect for credentials before submission and follow the instructor's instructions for handling redacted information.
5. Record the separate ten-minute demonstration described in `docs/demo-script.md`. No video is included in this draft.
6. Check README installation instructions, test results and optional integrations on the exact revision selected for submission. Confirm applicable licenses and attribution.
7. Review `AGENTS.md`, `.agent/`, `.opencode/`, and the agent-governance documents before enabling any policy. A policy document is not proof that a tool enforces it. The actual GitHub `main` protection configured during this work is documented separately in `docs/code-governance/branch-protection.md`.

## Files prepared here

- `architecture-blueprint.md` and `docs/architecture/`: a project-level architecture draft, decision and evaluation records, and requirement traceability supplement the existing contract-specific artifacts.
- `docs/req/requirements.yaml`: existing entries plus proposed team-wide records; `docs/req/legacy-requirements.yaml` preserves the source revision's original file. Proposed records are not approved.
- `docs/team-requirements-draft.md`: the human-readable English requirement draft.
- `docs/code-governance/`, `.agent/`, `.opencode/`, `AGENTS.md`, `Prompt.md`, `prompts/`: draft operating guidance, permission configuration, and prompt inventory. OpenCode configuration resolution was checked, but individual permission enforcement and application runtime prompt versioning have not been established.
- `docs/ai-use-log.yaml`, `docs/review-memo.md`, `docs/debt-register.md`, `docs/bug-diary.md`: evidence-linked records with explicit gaps and unknowns.
- `docs/demo-script.md`, `Report.md`, and `transcripts/README.md`: presentation and submission material needing team input.

## Evidence already available

The six GitHub CI checks for the source `main` revision completed successfully: [run 45533884389](https://github.com/TheKazusaTouma/TradingAgent/actions/runs/45533884389). Those hosted results apply to the source revision, **not** to the new draft documents or any later changes. The independent Windows checks of this draft are recorded in `docs/architecture/evaluation-logbook.md`. PR descriptions and versioned tests are evidence of specific scopes only; do not treat mocked data as live performance.

## Packaging

The course slides call for a project-directory ZIP and a separate 10-minute demonstration video. This draft deliberately omits secrets, user-specific caches, actual transcripts, and video; it is therefore incomplete for submission. Do not distribute a ZIP until the team checks the full contents, confirms the requirements, and adds its evidence. The actual project uses `tradingagents/`, `cli/`, and `tests/` rather than the example `src/` and `test/` names; verify whether the detailed requirement sheet permits this layout before changing or submitting it.
