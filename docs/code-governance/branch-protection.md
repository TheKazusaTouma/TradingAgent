# GitHub main protection — observed configuration

On 2026-11-25, after the user requested assistance preparing the project, Codex configured and read back the `main` branch protection through the GitHub API for `TheKazusaTouma/TradingAgent`. The read-back showed:

- Pull requests required; **zero** approving reviews required by this rule. The team may still request human reviews.
- Six existing CI status contexts required: `tests (py3.11)`, `tests (py3.12, America/New_York)`, `tests (py3.13)`, `tests (py3.14)`, `clean-install smoke`, and `ruff (strict, full repo)`.
- A branch must be up to date before merging; administrators are included; review conversations must be resolved.
- Force pushes and branch deletion are disabled.

These facts describe the API read-back at the time, not a permanent guarantee: an administrator can change settings later, and no intentionally failing PR was submitted to test end-to-end enforcement. Verify the current settings and run a safe branch-protection exercise before claiming such an exercise passed.

The rule was applied to the live GitHub repository. It affects future work by all collaborators, including administrators: they must use PRs and the listed checks unless an administrator changes the rule. It does not require a human approval because `required_approving_review_count` is zero.

The `requirements-schema` workflow in this draft is **not in `main`** and is not one of the six required checks. If the team proposes it in a PR, validate the workflow and decide explicitly whether to add its actual status context to the branch rule. Adding a required context before it runs could block merges.
