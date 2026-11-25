# Evaluation logbook — evidence register

This log distinguishes observed historical results from checks performed on this submission draft. It is not a claim that every requirement is accepted or that live-provider performance was measured.

| Evidence | Revision or scope | Observed result | Limit |
|---|---|---|---|
| [GitHub CI on main](https://github.com/TheKazusaTouma/TradingAgent/actions/runs/45533884389) | Source commit `a60f3507849108dd2de55880c5597db32539fbdf` | Six checks succeeded: four Python versions, clean-install smoke, Ruff | Does not test documents added later to this draft. |
| Local full regression before PR #4 merge | Integration tree corresponding to PR #4 combined with PR #6, Windows | 2093 passed, 11 skipped, 1 deselected, 100 subtests passed | Skips included Windows POSIX/symlink limitations and optional Bedrock dependency. This is an earlier run, not a new test of the final draft. |
| PR #6 performance example | Mocked Yahoo responses, scoped to the documented example | 10 identical sequential date-range requests used one mocked load | Not live-network latency, token cost, or a general throughput guarantee. |
| PR #2 context example | One offline sample with a 2,000-character budget | PR reports 4,125 characters before and 113 after | Example-specific; characters are not tokens. |

## Checks on the draft package

Record the actual command, date, environment, revision or artifact hash, result, and any skipped cases here after each run. Do not mark a check passed in advance.

| Check | Result | Evidence |
|---|---|---|
| Requirement schema and ID uniqueness | Passed on this draft with `python scripts/check_requirements.py` on Windows; all 59 records structurally valid | The check also rejected a temporary invalid-status sample and a duplicate-ID sample with exit code 1. This does not approve requirements or run acceptance tests. |
| Draft full offline regression | Windows: 2093 passed, 11 skipped, 1 deselected, 100 subtests passed; `pytest -q --disable-warnings` | 2026-11-25; source snapshot plus draft documentation and requirement checker. Skips included POSIX, symlink and optional Bedrock cases. |
| Draft full offline regression in WSL 2 | Ubuntu, Python 3.14.4: 2103 passed, 1 skipped, 1 deselected, 100 subtests passed; `python -m pytest -q --disable-warnings` | 2026-11-25; isolated environment populated with Linux wheels downloaded via Windows and installed offline. The single skip was missing optional `langchain_aws`; pytest used its Linux temporary directory for symlink tests. |
| Draft Ruff | Passed on Windows with `python -m ruff check .` | 2026-11-25; applies to files present at that check. |
| OpenCode configuration resolution | `opencode debug config --pure` completed and resolved a permission object and both draft instruction references | Installed OpenCode 1.18.30; individual permission actions were not exercised, so enforcement is not claimed. |
| Candidate requirement mapping | All 30 proposed IDs occur in the draft team traceability matrix; 19 referenced implementation and test files exist; the 29 original requirement records remain semantically unchanged | Windows local structural check on 2026-11-25; this does not verify the correctness or completeness of each mapping. |
| Source snapshot comparison | The 250 other tracked source files match the selected `main` revision exactly or after CRLF normalization; four requirement/architecture documents were intentionally modified | Windows archive extraction changed line endings in some files. This is a file comparison, not functional proof. New draft files are outside this comparison. |
| Basic secret-pattern scan | 288 draft files scanned; no matching private-key header or selected GitHub/OpenAI-token patterns; no `.env`, `.envrc` or `.env.enterprise` file found | Limited local heuristic, excluding files over 2 MB and generated directories. It is not proof that secrets or private data are absent; manually review final files and transcripts. |
| Live model or Yahoo evaluation | Not performed for this draft | Team to decide and record if required |
