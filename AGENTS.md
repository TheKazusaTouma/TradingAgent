# Coding-agent project guidance — team review draft

This file is a draft for the course submission. The team must review it against the detailed Project Requirement Sheet and the coding agent version actually used before relying on it as an enforced policy.

## Scope and evidence

- Work only on the task authorized by the user and preserve unrelated work.
- Read the applicable requirements and existing tests before changing behavior. Update requirement, design, and traceability records when behavior or acceptance criteria change.
- Record what was actually done and tested, including failures, skipped cases, environment limitations, and the coding agent or tool used. Do not invent approvals, results, member contributions, or transcripts.
- Keep complete coding-agent sessions according to the course's submission rules; review and redact sensitive information in coordination with the team. Do not publish API keys or private information.
- Use OpenCode in accordance with the course requirements. If another agent or a direct edit is used, record the actual tool and action and ask the team to resolve any course-compliance uncertainty.

## Engineering constraints

- Keep provider SDKs in the dataflow layer. Do not add brokerage or trade execution under the existing scope without an explicit requirement.
- Respect historical analysis cutoffs, Memory review semantics, and the distinction between Memory, checkpoint state, and cache.
- Treat trace callback coverage, report-integrity manifests, and mocked data according to their documented limits.
- Preserve backward-compatible defaults unless the team approves a change; Memory review and enhanced contracts are off by default in the reviewed revision.
- Run targeted tests for a change and the relevant full checks when feasible. Do not report skipped or mocked verification as live success.
- Check `.github/workflows/` and actual branch rules separately. An agent instruction file does not itself configure or enforce GitHub protections.

## Verification commands

```bash
python -m pip install -e '.[dev]'
python -m pip install 'PyYAML>=6'
python scripts/check_requirements.py
python -m pytest -q
python -m ruff check .
```

The final submission environment and any additional course-required checks remain to be confirmed. Live-service tests may need separately authorized credentials and are not implied by these commands.
