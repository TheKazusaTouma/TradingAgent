# Course CI governance gates

This document describes the blocking CI gates added for the CS5351 course
requirements and states, in concrete terms, what each one does and does **not**
prove. It exists because the detailed Word requirement sheet is unavailable and
the gap list calls for several governance controls; the honest position is to
implement the subset that is verifiable in this repository and to record the
rest as explicit gaps.

Nothing in this file claims a remote CI run is green, that branch protection is
configured, or that any human approval was obtained. Those are separate facts to
be read from GitHub and from real review records.

## Check names to require in branch protection

After this change is merged and the workflow has run at least once on a pull
request, a repository administrator can require these check names in GitHub
branch protection. Configuring branch protection is a manual GitHub step; this
workflow does not do it, and no one should claim it was done from this file.

| Job (and check name) | What it gates |
|---|---|
| `requirements-schema` | `docs/req/requirements.yaml` is valid per the JSON Schema, IDs are unique, declared dependencies hold. |
| `type-check (scoped)` | A small, explicitly listed set of modules still type-checks under pinned mypy. |
| `dependency-check` | `pip check` finds no broken/conflicting installed requirements. |
| `security-checks (scoped)` | Pattern scan of tracked Python + a check that no key/cert/env file is committed. |
| `offline-eval-replay` | A fixed set of node IDs exercising the scripted model/agent/prompt paths still passes. |
| `ai-use-log` | A PR that changes code/prompts/model config also updates `docs/ai-use-log.yaml`, and the log is well-formed. |

Existing jobs `tests (py…)`, `clean-install smoke`, and `ruff (strict, full repo)`
are unchanged and are also candidates to require.

## Provenance of this change

This governance work was directed by Codex: the human user instructed Codex,
Codex launched and directed the OpenCode session that implemented it, and Codex
independently reviewed the output and the further review findings. The
implementation text was produced by the OpenCode assistant model, not written
personally by the student, and the student is not asserted to have authored the
OpenCode prompts. The raw OpenCode session is preserved locally. Full human review
of the result is still pending and is not claimed anywhere in this file. See
`docs/ai-use-log.yaml` (record `AI-GOV-01`).

## Requirements source discrepancy (must be reconciled before submission)

There are two requirements files with different sizes; this change does **not**
resolve which one is final and does **not** overwrite the approved file:

- `docs/req/requirements.yaml` in this repository: **29** records, `meta.status:
  approved`.
- The separately supplied team draft (a separate checkout outside this
  repository; its `docs/req/requirements.yaml`): **59** records,
  `meta.status: draft`. It contains all 29 repository records unchanged and
  **30 additional** records (FR-13..FR-32, NFR-9..NFR-18).

The checker validates whichever file it is pointed at. Running it against the
draft is a *structural* check only:

```
python -m tools.check_requirements --requirements <path-to-draft>/docs/req/requirements.yaml
```

Observed result at the time of writing: the draft **passes** the schema, unique-ID,
and dependency checks (exit 0). This structural pass does **not** mean the draft is
reviewed, approved, or the intended final document — its own `meta.status` is
`draft`. Before submission the team must decide which set is authoritative and
reconcile them (e.g. promote the draft through the real review flow, or trim it),
then re-run the checker on the chosen file. Until that decision is recorded, this
document makes no claim about which requirements are final.

## What each gate does, and its limits

### 1. `requirements-schema` — `tools/check_requirements.py`

- Parses `docs/req/requirements.yaml` as YAML and validates it against
  `docs/req/req-schema.json` with a **real** `jsonschema` draft-07 validator.
- Fails on duplicate requirement IDs.
- Fails when a declared dependency is missing (currently `metric` requires
  `threshold`; the same rule is encoded in the schema, re-checked here so a
  failure names the requirement rather than a schema path).
- Returns exit code 2 (not 1) when the check cannot run at all (missing file,
  bad YAML, invalid schema), so "could not check" is never reported as "valid".

**Proves:** the document is structurally sound. **Does not prove:** that any
requirement is approved, correct, or that its acceptance tests pass. The success
message says so explicitly.

### 2. `type-check (scoped)` — `tools/ci_checks.py type`

- Runs pinned `mypy==1.11.2` over four modules with flags
  `--follow-imports=skip --ignore-missing-imports --check-untyped-defs
  --warn-unused-ignores --warn-redundant-casts --warn-return-any
  --no-implicit-optional --strict-equality`.
- Scope (`TYPE_SCOPE`): `tradingagents/llm_clients/validators.py`,
  `model_catalog.py`, `base_client.py`, `tradingagents/config_validation.py`.
  These pass today; the list is a constant so widening it is a reviewable change.
- Every run prints the documented **upstream type debt**: provider clients under
  `tradingagents/llm_clients/*_client.py` (~15 mypy errors at an SDK boundary),
  `agents/*` untyped helpers, and `dataflows/*` pandas/yfinance interfaces.

**Proves:** these four modules type-check under those flags. **Does not prove:**
the whole project is typed. This is deliberately a first, honest step, not a
blanket typing claim.

Isolation: `mypy==1.11.2` is **not** in the common `dev` extra, so the existing
Python 3.11-3.14 test matrix never installs it. It is pinned and installed only in
the dedicated Python 3.12 `type-check` job. If mypy is absent, `tools.ci_checks
type` exits 2 and the job fails; it can never silently pass for lack of mypy. The
tooling unit test for the type check may skip when mypy is not installed (e.g. a
bare interpreter), but the CI gate itself does not skip.

### 3. `dependency-check` — `tools/ci_checks.py deps`

- Runs `pip check` against the installed dev environment (from `pip install -e ".[dev]"`).

**Proves:** installed requirements are internally consistent (no unmet or
conflicting versions). **Does not prove:** the declared version bounds resolve in
every environment, or that dependencies are free of known vulnerabilities.

### 4. `security-checks (scoped)` — `tools/ci_checks.py security`

- Scans **tracked** `.py` files for `eval`, `exec`, `os.system`, and
  `subprocess(..., shell=True)`, and for a few high-signal hard-coded credential
  shapes (OpenAI/Anthropic/AWS/GitHub key prefixes, PEM private-key headers).
- Fails if a tracked file looks like a secret store or key file (`.env`,
  `id_rsa`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.keystore`, …), exempting
  `*.example`/`*.sample` templates.
- Comments are skipped, and the checker and its tests are excluded from their own
  pattern scan.

**Proves:** those specific patterns are absent from tracked Python and no
obvious key file is committed. **Does not prove:** the code is secure. It is not
a dependency-vulnerability scan, not a git-history audit (a secret removed
earlier is still in history), and not a logic review.

### 5. `offline-eval-replay` — `tools/eval_replay.py`

- Replays a **fixed list of concrete pytest node IDs** (not whole modules) that
  exercise the real model/agent/prompt paths with scripted models and no network:
  full-graph runs (free-text and structured decisions), analyst brief delivery,
  structured-agent constraint prompts, model validation, and the contract layer.
- Before running, it collects every node ID individually; if a case was removed
  or renamed, the replay fails and **names the missing case**, so the evaluation
  set cannot silently shrink.
- Runs **unconditionally** (not only on prompt/model diffs) for reliability.

**Proves:** the fixed, scripted-fixture behaviours those tests assert still hold.
**Does not prove:** live-provider correctness, model output quality, latency, or
cost. The model here is a fixture, not a provider.

### 6. `ai-use-log` — `tools/ai_use_log.py`

- Runs on `pull_request` only (see safety note below), with the PR's base and
  head SHAs from the event payload, and compares them with local `git diff
  --name-status -z --find-renames`. The `-z` form is NUL-delimited, so paths with
  spaces or non-ASCII characters are never C-quoted or mis-split; both sides of a
  rename/copy are read. Base and head are passed to git as argv, never through a
  shell, and a git failure is fail-closed (exit 2, "could not run").
- Classifies the diff into **code**, **prompt**, and **model configuration**:
  - **code** — any tracked source file anywhere in the repo by extension:
    `.py .pyi .sh .bash .js .jsx .ts .tsx`, plus `Dockerfile` and `Dockerfile.*`.
    Not restricted to a package directory, so changes under `tests/`, `tools/`,
    `scripts/` and nested modules count. The AI-use log itself is excluded.
  - **prompt** — files under a `prompts/` directory, `*prompt*` modules, or
    prompt-suffixed templates.
  - **model configuration** — an explicit allow-list: `default_config.py`, the
    `llm_clients/*` provider/model modules, `.env.example`,
    `.env.enterprise.example`.
- If any governed change is present and `docs/ai-use-log.yaml` is **not** in the
  same diff, the check fails.
- The whole log must parse and satisfy a small schema: a hex
  `reviewed_source_revision` and a non-empty `records` list of
  `{id, scope, tool, basis}` with unique ids. A malformed log always fails.

**Proves:** the log was updated in the same PR and is well-formed. **Does not
prove:** who wrote a change, that it was a particular agent, or that a transcript
exists; nor does a present, well-formed log authenticate its own content. It
never infers the tool from a PR author or branch name.

**Known coverage limit:** code coverage is defined by the explicit extension and
filename lists above. A source file in a language not listed (e.g. `.rs`, `.go`,
`.c`) is not classified as code until the list is extended in review, so the rule
cannot claim to cover every possible language.

**Safety:** the workflow uses `pull_request` (never `pull_request_target`), a
read-only `contents: read` permission, and no secrets. It reads git locally only.

## Test-before-implementation approval: an explicit gap

The gap list asks for a gate that proves implementation was approved **before**
tests were written / code was merged. This cannot be honestly automated from the
facts available inside this repository:

- A GitHub **required human approval** is a review of the PR's current state. It
  does not, by itself, prove the approval happened *before* implementation, nor
  that it covered the specific tests first.
- Self-asserted text in a PR description, commit message, or this file is not
  authenticated review. Treating it as an approval would be forging evidence.
- No signed review artifacts with trustworthy timestamps relative to code changes
  exist here, and we will not invent signatures or reviewers.

Therefore **no automatic gate for "test-first / pre-implementation approval" is
claimed**. Recording it as if it were implemented would be a false claim. What is
implemented instead is the smaller, verifiable control: the AI-use log and the
other gates make the *process* visible, but they do not authenticate a reviewer.

### How future team members obtain and retain genuine review evidence

1. Use GitHub's built-in review flow: open a PR, request a human reviewer, and
   have them submit **Approve / Request changes** on GitHub. That produces an
   authenticated, timestamped review attached to the exact commit reviewed.
2. For test-first evidence specifically, open the PR **with failing tests and no
   implementation first**, let a reviewer approve that state (the review records
   the commit SHA and time), then push the implementation as a follow-up commit.
   The commit order plus the review timestamp on GitHub is the evidence; export
   the PR's review history or keep the PR link in the team's records.
3. Do not describe a local mock result, a self-comment, or an agent's assertion as
   a human approval. If a reviewer signs off, cite the GitHub review, not a
   message that merely says "approved".
4. Retain: the merged PR URL, the review event (approver + timestamp + reviewed
   commit), and the AI-use log entries covering that work. These are the real,
   checkable artifacts.

## Branch-protection and remote status

- This change does **not** modify any live GitHub setting. Configuring required
  checks is a manual admin action.
- Whether the workflow is green on GitHub must be read from an actual run after
  push. This document claims only local results (see below); it does not claim
  remote CI green.

## Local verification performed for this change

Environment: the project's existing Windows virtual environment (Python 3.11)
for the application suite, with `jsonschema` supplied from a temporary,
non-repository install location so the venv itself was not modified; a separate
isolated tooling interpreter with `jsonschema`/`PyYAML`/`pytest` (and `mypy` for
the type check) for the gate tools and their tests. No machine-specific paths are
recorded here.

| Check | Command | Result |
|---|---|---|
| Full suite (recorded baseline, not rerun) | pytest (venv, jsonschema via temp path) | 2093 passed, 11 skipped (OS-specific), 100 subtests passed |
| Requirements validator (valid) | `python -m tools.check_requirements` | exit 0 |
| Requirements validator (invalid) | `--requirements <empty list>` | exit 1 |
| Requirements validator (cannot run) | `--requirements <missing>` | exit 2 |
| Requirements validator on team draft (59 records) | `python -m tools.check_requirements --requirements <draft>` | exit 0 — structurally valid and IDs unique; NOT an approval (draft `status: draft`) |
| Requirements tests | `pytest tests/test_check_requirements.py --noconftest` | 15 passed |
| Type check | `python -m tools.ci_checks type` (mypy 1.11.2) | PASS (scoped); exits 2 if mypy missing |
| Dependency check | `python -m tools.ci_checks deps` | PASS (`pip check`) |
| Security scan (clean tree, new files tracked via intent-to-add) | `python -m tools.ci_checks security` | 0 findings |
| Security scan (planted fake key) | same, with a scratch file | 1 finding, exit 1 |
| Security tests | `pytest tests/test_ci_checks.py --noconftest` | 20 passed |
| Eval replay | `python -m tools.eval_replay` (venv) | 14 node IDs, PASS |
| Eval replay tests | `pytest tests/test_eval_replay.py` (venv) | 5 passed, 1 skipped in bare interp; all pass in full env |
| AI-use log tests | `pytest tests/test_ai_use_log.py --noconftest` | 55 passed |
| AI-use log (correct reference) | `python -m tools.ai_use_log --base origin/main --head HEAD` | exit 0 — `origin/main` is a60f350, identical to HEAD, so the committed range is empty (no governed change). The local `main` ref is stale (44263cf) and must not be used as the base. |
| AI-use log (change detected) | `python -m tools.ai_use_log --base HEAD~1 --head HEAD` | exit 1 — the HEAD merge commit touched code without a log update, which is the intended failure |
| New tooling tests, all four files | `pytest tests/test_{check_requirements,ai_use_log,ci_checks,eval_replay}.py` | 95 passed, 1 skipped (full-env-only eval collection; type test skips only where mypy absent) |
| Ruff | `ruff check .` | All checks passed |

Skipped / not claimed:

- The full-env-only eval-replay collection test is skipped under a bare tooling
  interpreter (it imports the application); CI runs it with dev deps.
- The type-check unit test skips where mypy is not installed; the dedicated
  python 3.12 `type-check` CI job always installs pinned mypy and fails (exit 2)
  if it is missing, so the gate never silently passes.
- Remote CI results and branch protection are not claimed; they depend on a push
  and a manual admin step.
- Live provider behaviour, model quality, and dependency-vulnerability scanning
  are out of scope and not claimed.
- Test-before-implementation approval is **not implemented** (see the explicit gap
  above). This change does not fabricate it or claim it from any artifact.
