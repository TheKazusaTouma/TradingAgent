# Yahoo cache evaluation scripts

Offline evaluation and demonstration scripts for the Yahoo market-data cache
work (Stages 1–4). Every script uses mocked Yahoo responses and temporary cache
directories — no network access — and only depends on the project's normal
requirements (`pyproject.toml`, notably `pandas`). No personal paths are needed.

Run everything from the repository root with your project interpreter (after
installing dependencies, e.g. `pip install -r requirements.txt` or an editable
install). Examples below use plain `python`; substitute `.venv/bin/python` (or
your virtualenv's interpreter) if that is how you run the project.

## Stage 4 cache statistics demo

Self-contained: runs six scenarios and asserts their cache-stat deltas and mock
provider call counts (the two are reported separately), exiting non-zero on any
mismatch.

```bash
python scripts/demo_cache_stats.py
```

## Revision comparisons

The two comparison scripts are revision-agnostic: they inspect whichever
`tradingagents` package is importable, so you compare revisions by pointing the
import at an isolated checkout of each commit (e.g. a `git worktree`). Both
scripts print the path of the `tradingagents` package they actually imported
(`imported_pkg`; `eval_coalescing.py` also prints `yahoo_market_module`), so you
can confirm each run used the intended revision.

**Important — run each comparison from inside the checkout under test.** An
older commit's worktree does not contain these scripts; the scripts exist only in
this evaluation-materials checkout. Run the script from *here*, but `cd` into the
worktree first so its `tradingagents` package is the one imported (the current
directory wins over `PYTHONPATH` on `sys.path`). Set `EVAL` to the absolute path
of this `scripts/` directory.

```bash
EVAL=/absolute/path/to/this/checkout/scripts   # the evaluation-materials scripts
```

Reference commits:

| Stage    | Commit    |
|----------|-----------|
| Baseline | `307d15e` |
| Stage 1  | `003082f` |
| Stage 2  | `97bb88e` |
| Stage 3  | `9e431ea` |

### Stage 1 vs Stage 2 — request coalescing (`eval_coalescing.py`)

Two overlapping same-key cold requests per path (date-range and five-year OHLCV);
proves overlap while the first load is blocked and reports provider call counts
(Stage 1: 2 provider calls per path; Stage 2: 1).

```bash
git worktree add --detach /tmp/eval-s1 003082f
git worktree add --detach /tmp/eval-s2 97bb88e

# run from inside each worktree so its package is imported
(cd /tmp/eval-s1 && PYTHONPATH="$PWD" python "$EVAL/eval_coalescing.py" 003082f)
(cd /tmp/eval-s2 && PYTHONPATH="$PWD" python "$EVAL/eval_coalescing.py" 97bb88e)

git worktree remove --force /tmp/eval-s1
git worktree remove --force /tmp/eval-s2
```

### Baseline / Stage 3 — acceptance scenarios (`eval_acceptance.py`)

Runs the same scenarios (ten sequential range queries, overlapping cold requests
on both paths, and recovery/cache-preservation cases) on whichever revision is
importable. Pass the expected commit short SHA and the checkout path as the third
argument; the script verifies `imported_from_expected_worktree`.

```bash
git worktree add --detach /tmp/eval-base 307d15e
git worktree add --detach /tmp/eval-s3   9e431ea

(cd /tmp/eval-base && PYTHONPATH="$PWD" python "$EVAL/eval_acceptance.py" baseline 307d15e "$PWD")
(cd /tmp/eval-s3   && PYTHONPATH="$PWD" python "$EVAL/eval_acceptance.py" stage3   9e431ea "$PWD")

git worktree remove --force /tmp/eval-base
git worktree remove --force /tmp/eval-s3
```
