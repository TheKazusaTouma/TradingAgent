# Verification constraints — proposed

1. Identify the applicable requirement, affected paths, and existing tests before changes.
2. Run targeted tests on normal, boundary, and failure cases appropriate to the change; record environment, exact command, result, and skips.
3. Run the full offline suite and lint when appropriate. Treat integration/live checks separately; do not infer a live result from mocks.
4. Validate `docs/req/requirements.yaml` against `docs/req/req-schema.json` and check identifier uniqueness with `python scripts/check_requirements.py`. This structural check does not approve requirements or run acceptance tests.
5. Review AI changes and preserve complete session evidence under the course's rules. Do not claim a draft, PR or automated run is human-approved without evidence.
6. For a failing check, preserve the failure and investigate; do not delete or weaken an assertion merely to pass CI.
7. Verify actual GitHub branch rules independently from workflow results.

Status: proposal for team review; no retrospective claim that all historic work followed these rules.
