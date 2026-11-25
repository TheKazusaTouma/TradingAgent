# User Guide — Startup Preflight, Numeric Sanitation and Report Integrity

PR packaging note: approval records and full OpenCode transcripts refer to local development and are retained separately. They are excluded from this PR. Team review and merge approval are still pending. See docs/verification/increment-v3-evidence.md for integration verification.

This guide describes the three capabilities added in increment v3. It is written for someone
running the tool, not for a reviewer of the change. Every example below is copied from a real
run on this repository (`Windows 11`, `Python` from `.venv`, offline verification without live provider requests);
nothing here is illustrative-only.

Design and approval records: `docs/architecture/adr/002-preflight-sanitation-manifest.md`,
`docs/approvals/agent-contract-v3.md`. Verification: `docs/verification/increment-v3-test-spec.md`,
`docs/verification/increment-v3-evidence.md`.

---

## 0. Which modes are involved

| Capability | `agent_contract_mode: off` (default) | `validate` | `validate+repair` |
|---|---|---|---|
| Startup configuration preflight | **always on** | **always on** | **always on** |
| Numeric sanitisation of AI output | **not applied** — baseline behaviour, unchanged | applied | applied |
| Second model call for a bad numeric | never | never | never |
| Semantic repair for a *required* field | never | never | one attempt |
| Report manifest on save | **always written** | always written | always written |
| `python -m tradingagents.report_integrity` | works on any saved tree | works | works |

Two statements matter for honesty:

1. **The default is `off`.** A default run does *not* sanitise AI numbers; the upstream baseline
   coercer is untouched (`"15%" → None`, `"1,2,3" → 123.0`, `True → 1.0` in the baseline model —
   these are upstream behaviours of `tradingagents/agents/schemas.py`, which this increment does
   not modify). Sanitation requires `agent_contract_mode` set to `validate` or
   `validate+repair`.
2. **The preflight and the manifest are not opt-in.** They are active in every mode, because
   they concern configuration mistakes and saved files, not model phrasing.

Set the mode through the environment (the shell variable wins over the file default):

```powershell
$env:TRADINGAGENTS_AGENT_CONTRACT_MODE = "validate"        # or validate+repair
```

---

## 1. Startup configuration preflight

### What it does

Before any LLM client is constructed, before any directory is created and before any network
call, the whole effective configuration is checked. Every offending parameter is named in one
report, with a neutral reason and no secret values. Unknown/extension keys are allowed. A
partial configuration is completed from the defaults; a *supplied* invalid value is reported,
never silently replaced by the default.

It is also the first statement in `TradingAgentsGraph.__init__`, and the CLI checks the same
thing before it starts a run. The analyst tool-round relation
(`2 * max_tool_rounds + 2 < max_recur_limit`) is now part of this check, so it fails *before*
the model clients exist (it previously failed after they were built).

### A real failure

Configuration used for this output:

```python
{
    "agent_contract_mode": "strict",              # not one of the accepted modes
    "max_tokens": 0,                              # must be positive
    "temperature": float("nan"),                  # must be a finite number
    "llm_provider": None,                         # the primary provider cannot be null
    "backend_url": "http://localhost:notaport/v1" # the port is not a number
}
```

Output of `validate_config(...)`, reproduced verbatim:

```text
agent_contract_mode: expected one of off, validate, validate+repair, got 'strict'
max_tokens: must be > 1, got 0
temperature: a finite number is required
llm_provider: llm_provider must name a provider; only a per-tier *_provider may be null
backend_url: the port must be a number in 0..65535: 'http://localhost:notaport/v1'
{"problem_count": 5}
```

When this happens at startup the graph raises `ConfigValidationError`, and the CLI prints

```text
Configuration is invalid; nothing was run.
  <the same lines, one per problem>
```

and exits non-zero. Nothing has been created or called at that point: the verification suite
asserts that the client factory and the node factories have not been invoked and that the
results/cache directories do not exist (`tests/test_config_validation.py`, PF-2 / PF-3 /
PF-13).

### What is accepted

| Setting | Accepted | Rejected |
|---|---|---|
| `agent_contract_mode` | `off`, ` validate `, `validate+repair`, `None` | `strict`, `on`, `""`, `1` |
| `max_debate_rounds`, `max_risk_discuss_rounds` | `0`, `3` | `-1`, `1.5`, `True` |
| `max_tool_rounds`, `max_recur_limit`, `max_tokens`, `global_news_lookback_days` | `1`, `20` | `0`, `-1`, `True` |
| `llm_max_retries`, `max_tokens` | `0`, `"6"`, `None` | `-1`, `1.5`, `"abc"`, `True` |
| `temperature` | `0`, `1.0`, `"0.2"`, `None` | `NaN`, `inf`, `-0.1`, `2.1`, `"hot"` |
| `llm_provider` | `openai`, `Ollama`, `openai_compatible`, `ollama`, any custom model id | `""`, `"  "`, `nonesuch`, `None`, `3` |
| `deep_think_provider`, `quick_think_provider` | any of the above **or `None`** (= inherit `llm_provider`) | `""`, `"nonesuch"` |
| `backend_url`, `*_backend_url` | `None`, `""`, `http://localhost:8000/v1`, `https://example.com/v1`, `http://[::1]:11434/v1` | `localhost:8000`, `ftp://x`, `http://a b`, URLs with credentials, an empty/non-numeric/out-of-range port (`:notaport`, `:99999`, `:`) |
| `checkpoint_enabled` | `True`, `False` | `"yes"`, `1` |
| `data_vendors`, `tool_vendors`, `benchmark_map` | mappings of text to text | a string, `{1: "x"}`, `{"k": 3}` (reported as `tool_vendors.<key>`) |
| any other key | allowed and ignored | — |

Keyless providers (`ollama`, `openai_compatible`) and custom model identifiers are valid: no
API key is required by the preflight, and an unknown model name is not an error.

A URL carrying credentials is reported as `<redacted>`; the credential is never echoed.

---

## 2. Numeric sanitation of AI answers

### What it does

An **optional** numeric field in an AI answer — `entry_price`, `stop_loss`, `price_target` —
that cannot be read as one unambiguous finite number becomes **absent** (`None`) instead of
rejecting the whole answer and instead of being guessed. Every other field is preserved, and
no extra model call is made for a numeric-only problem. Each sanitation is recorded as a
`sanitized_invalid_number` finding naming the field and a neutral reason; the model's raw text
is never copied into the report or the record.

Required fields (`action`/`rating`, the reasoning/narrative text) are still enforced exactly as
before. In `validate+repair` a *required* defect still gets the single repair attempt.

### A real before/after

Mode `validate`, structured answer
`{"action": "Buy", "reasoning": "Add.", "entry_price": <bad>, "stop_loss": 172.0, "position_sizing": "5% of portfolio"}`:

```text
input '15%'      -> entry_price=None stop_loss=172.0 calls=1 sanitized=[('entry_price', 'percentage')]
input '1,2,3'    -> entry_price=None stop_loss=172.0 calls=1 sanitized=[('entry_price', 'grouping')]
input '$1,23.50' -> entry_price=None stop_loss=172.0 calls=1 sanitized=[('entry_price', 'grouping')]
input True       -> entry_price=None stop_loss=172.0 calls=1 sanitized=[('entry_price', 'boolean')]
input nan        -> entry_price=None stop_loss=172.0 calls=1 sanitized=[('entry_price', 'nonfinite')]
```

In the rendered report the line reads `**Entry Price**: not provided`; `stop_loss`, `reasoning`
and `position_sizing` are unchanged, and `calls=1` is the one and only model call.

### Recommended value → result

| Input | Result | Reason recorded |
|---|---|---|
| `189.5`, `"189.5"`, `"$1,234.50"`, `"12,345"`, `"1e3"`, `-3.5`, `0.0` | the number | — (no finding) |
| `None`, key omitted, `"N/A"` | absent | — (no finding: an explicit omission) |
| `"15%"` | absent | `percentage` |
| `"150-160"` | absent | `range` |
| `"around 150"` | absent | `hedge` |
| `"not provided"`, `"abc"` | absent | `not_a_number` |
| `"1,2,3"`, `"$1,23.50"`, `"12,34"`, `"1,234,56"` | absent | `grouping` (malformed thousand groups) |
| `True`, `False` | absent | `boolean` |
| `NaN`, `inf`, `-inf` | absent | `nonfinite` |
| `""`, `"   "` | absent | `placeholder` |
| `[150]`, `{"lo": 1}` | absent | `unsupported_type` |
| the same field stated twice with different values | absent | `conflicting_json` |

Digit grouping is validated **before** separators are removed, so `"1,2,3"` is never read as
`123`.

### What is *not* sanitised

A blank or missing required narrative, an unknown `action`/`rating`, or a missing required field
still fails the run (or gets the one repair in `validate+repair`). Sanitation only ever removes
an absent-able level; it never invents one and never satisfies a required field.

---

## 3. Report integrity manifest

### What it does

Every save through the normal report writer (`TradingAgentGraph.save_reports`, the CLI, the
programmatic `write_report_tree`) writes `report_manifest.json` **after** stale report files
have been reconciled. The manifest lists each file this save actually wrote, with its
root-relative POSIX path, byte size and SHA-256 digest. It never lists itself, never lists a
file the writer does not own (your own notes beside the report survive regeneration and stay
out of the manifest), and an empty or incomplete list is refused rather than written.

The manifest detects drift; it is **not** a signature or authenticity mechanism. Someone who can
rewrite both a report and its manifest can make them agree. There is no key, no network call
and no new dependency.

### Verifying a saved report

```powershell
python -m tradingagents.report_integrity "C:\path\to\my-results\reports\AAPL_20261124_120000"
```

Exit codes: `0` every listed file matches; `1` a listed file is missing/modified or the manifest
lists an unsafe path; `2` the manifest is missing, unreadable, malformed or of an unsupported
version (this includes an empty or inconsistent manifest, so it can never pass vacuously).

A real passing run:

```text
$ python -m tradingagents.report_integrity C:\Users\kevin\AppData\Local\Temp\v3-evidence-yyn04ocx
exit=0
Report integrity check passed for C:\Users\kevin\AppData\Local\Temp\v3-evidence-yyn04ocx: 9 file(s) match report_manifest.json.
```

The same directory after editing one section file and deleting the complete report:

```text
exit=1
Report integrity check FAILED for C:\Users\kevin\AppData\Local\Temp\v3-evidence-yyn04ocx:
  - 1_analysts/market.md: size is 8 bytes, the manifest records 3
  - complete_report.md: the listed file does not exist
```

### Relative paths

The writer accepts a relative `save_path` (it is relative to the working directory, as before
this change, and `write_report_tree` still returns that same path). The manifest is written with
a root-relative contract resolved once, so `save_path=Path("reports/NVDA")` under any working
directory describes that real tree:

```text
files: ['1_analysts/market.md', '1_analysts/news.md', '2_research/bull.md', '2_research/manager.md', '3_trading/trader.md', '4_risk/neutral.md', '5_portfolio/decision.md', 'complete_report.html', 'complete_report.md']
verify exit=0: Report integrity check passed for C:\Users\kevin\AppData\Local\Temp\v3-rel-5tdzl1dr\reports\NVDA: 9 file(s) match report_manifest.json.
```

A previously reported defect — an empty manifest that "verified" because relative paths were
prefixed twice and then silently skipped — is covered by
`tests/test_report_integrity.py::test_ri2b_a_relative_save_path_under_the_cwd_writes_the_real_file_set`.

### Safety limits

* Absolute, drive-qualified, UNC and `..`-traversal entries are refused before any file is
  opened, and nothing outside the report directory is ever read.
* A manifest that is itself a symbolic link is refused before it is read; so is any listed file
  whose path passes through a symbolic link.
* Verification reads file sizes and digests only; it does not modify the tree.

The manifest is separate from the enhanced-mode `## Contract Validation` summary that may also
appear in `complete_report.md`: the summary is prose about agent contracts, the manifest is a
digest list.

---

## 4. Quick reference

```powershell
# 1. turn on numeric sanitation (optional; off by default)
$env:TRADINGAGENTS_AGENT_CONTRACT_MODE = "validate"

# 2. run as usual; a bad configuration stops at startup with named problems
python -m cli.main --ticker NVDA --date 2026-11-24

# 3. verify a saved report tree offline
python -m tradingagents.report_integrity "<results-dir>\reports\NVDA_<stamp>"
```
