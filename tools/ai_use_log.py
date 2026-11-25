"""Machine-checkable AI-use log: change detection and log schema validation.

Policy enforced here (a small, verifiable subset of the course's AI-disclosure
requirement): a pull request that changes application code, prompts, or model
configuration must also change the AI-use log, ``docs/ai-use-log.yaml``.

What this script actually verifies
----------------------------------
1. **Change detection.** Given a git ``base`` and ``head`` ref it lists the files
   that differ (added, modified, renamed, deleted, copied) using NUL-delimited
   ``git diff --name-status -z --find-renames`` so non-ASCII and space-containing
   paths are never C-quoted or mis-split. A change is *governed* when any touched
   path is source code anywhere in the repository by file extension (see
   ``CODE_SUFFIXES`` / ``CODE_FILENAMES``: ``.py .pyi .sh .js .jsx .ts .tsx`` and
   ``Dockerfile`` variants), a prompt (a ``prompts/`` directory, a ``*prompt*``
   module, or a prompt-suffixed template), or model configuration (an explicit
   allow-list). A rename or copy counts as both its old and new path; the AI-use
   log itself (``docs/ai-use-log.yaml``) is excluded from code classification.
2. **Log update.** If a governed change is present, the same diff must touch the
   log file. A missing or empty update fails.
3. **Log validity.** The whole log must parse as YAML and satisfy a small schema:
   a ``reviewed_source_revision`` commit string and a non-empty ``records`` list of
   ``{id, scope, tool, basis}`` objects with unique ``id`` values.

What this script does **not** do
--------------------------------
- It does not authenticate who wrote an entry, attribute a change to a particular
  agent from a branch name or author, or verify that a transcript exists. A log
  that is present and well-formed does not authenticate its content.
- It cannot prove every possible coding language is covered: coverage is defined
  by the explicit extension/filename lists below, which are documented and can be
  widened in review. A new language not in those lists is a known gap.
- It does not decide *which* record describes *which* file: the requirement is
  that the log was updated in the same PR, not that every line is attributed.
- It reads git locally; it never contacts GitHub and never sees secrets. In CI it
  is fed ``github.event.pull_request.base.sha`` / ``...head.sha`` on a normal
  ``pull_request`` checkout (never ``pull_request_target``), so no token beyond the
  read-only checkout is needed.

Exit codes: 0 checks pass; 1 a policy/schema failure; 2 the check could not run
(bad ref, missing git, unavailable parser).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOG_PATH = "docs/ai-use-log.yaml"

# Source-code classification is repository-wide by explicit extension/filename,
# not restricted to a package directory: tests/, tools/, scripts/ and nested
# modules are code too. The lists are deliberately explicit and documented so the
# rule is auditable; widening them is a reviewable change.
CODE_SUFFIXES = (
    ".py",
    ".pyi",
    ".sh",
    ".bash",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
)
# Container build files have no extension; match the Dockerfile family by name.
CODE_FILENAMES = ("Dockerfile",)
CODE_FILENAME_PREFIXES = ("Dockerfile.",)  # e.g. Dockerfile.dev

# Prompt files: prompt text lives in markdown/templates, or in a module whose
# name says "prompt". These are matched against the whole path.
PROMPT_DIR_SEGMENTS = ("prompts",)
PROMPT_SUFFIXES = (".md", ".txt", ".j2", ".jinja")
PROMPT_NAME_TOKENS = ("prompt",)

# Model configuration: files that decide which provider/model is used or how it
# is called. Kept explicit rather than "anything with a model name" so the rule
# is auditable and does not silently widen.
MODEL_CONFIG_PATHS = {
    "tradingagents/default_config.py",
    "tradingagents/llm_clients/model_catalog.py",
    "tradingagents/llm_clients/capabilities.py",
    "tradingagents/llm_clients/validators.py",
    "tradingagents/llm_clients/factory.py",
    "tradingagents/llm_clients/openai_client.py",
    "tradingagents/llm_clients/anthropic_client.py",
    "tradingagents/llm_clients/azure_client.py",
    "tradingagents/llm_clients/google_client.py",
    "tradingagents/llm_clients/bedrock_client.py",
    "tradingagents/llm_clients/base_client.py",
    "tradingagents/llm_clients/chatgpt_auth.py",
    ".env.example",
    ".env.enterprise.example",
}

RECORD_REQUIRED = ("id", "scope", "tool", "basis")
REVISION_RE = re.compile(r"^[0-9a-f]{7,40}$")


class Unavailable(Exception):
    """The check could not run (as opposed to the policy being violated)."""


@dataclass
class Change:
    """One file in a base..head diff."""

    status: str  # A, M, D, R, C, T, U, X, B or a combined form
    paths: list[str]

    @property
    def all_paths(self) -> list[str]:
        return self.paths


@dataclass
class Classification:
    code: list[str] = field(default_factory=list)
    prompt: list[str] = field(default_factory=list)
    model_config: list[str] = field(default_factory=list)
    log: bool = False

    @property
    def governed(self) -> bool:
        return bool(self.code or self.prompt or self.model_config)


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def _run_git_bytes(args: list[str], cwd: Path) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise Unavailable("git executable not found") from error
    if completed.returncode != 0:
        message = (
            completed.stderr.decode("utf-8", "replace").strip()
            or completed.stdout.decode("utf-8", "replace").strip()
        )
        raise Unavailable(f"git {' '.join(args)} failed: {message}")
    return completed.stdout


def parse_name_status_z(output: bytes) -> list[Change]:
    """Parse NUL-delimited ``git diff --name-status -z`` output into Changes.

    With ``-z`` git emits each record as fields separated by NUL bytes and never
    quotes paths: ``STATUS\\0PATH\\0`` for add/modify/delete, and
    ``STATUS\\0OLD\\0NEW\\0`` for renames/copies. Paths may therefore contain any
    byte, including spaces and non-ASCII characters, without C-quoting. Both sides
    of a rename/copy are kept so a move out of a governed path is still governed.

    Decoding uses ``surrogateescape`` so a path that is not valid UTF-8 is preserved
    losslessly instead of raising; classification only compares suffixes/segments,
    which is unaffected by the surrogate round-trip.
    """
    fields = output.split(b"\x00")
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("utf-8", "surrogateescape").strip()
        index += 1
        if not status:
            continue
        # Rename (R) and copy (C) statuses carry two paths; all others carry one.
        is_pair = status[:1] in ("R", "C")
        if is_pair:
            if index + 1 >= len(fields):
                break
            old = fields[index].decode("utf-8", "surrogateescape")
            new = fields[index + 1].decode("utf-8", "surrogateescape")
            index += 2
            if old or new:
                changes.append(Change(status=status, paths=[p for p in (old, new) if p]))
        else:
            if index >= len(fields):
                break
            path = fields[index].decode("utf-8", "surrogateescape")
            index += 1
            if path:
                changes.append(Change(status=status, paths=[path]))
    return changes


def diff_changes(base: str, head: str, cwd: Path = ROOT) -> list[Change]:
    """Diff ``base...head`` with local git, NUL-delimited and rename-aware.

    ``base`` and ``head`` are passed as argv to git (never through a shell), so
    they cannot be interpreted as shell fragments.
    """
    output = _run_git_bytes(
        ["diff", "--name-status", "-z", "--find-renames", f"{base}...{head}"],
        cwd=cwd,
    )
    return parse_name_status_z(output)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _is_code(path: str) -> bool:
    # The log itself is not source code even if someone names it .py-like.
    if path == LOG_PATH:
        return False
    base = path.rsplit("/", 1)[-1]
    if base in CODE_FILENAMES or base.startswith(CODE_FILENAME_PREFIXES):
        return True
    return path.endswith(CODE_SUFFIXES)


def _is_prompt(path: str) -> bool:
    parts = path.split("/")
    if any(part in PROMPT_DIR_SEGMENTS for part in parts[:-1]):
        return True
    if any(token in parts[-1].lower() for token in PROMPT_NAME_TOKENS):
        return True
    return path.endswith(PROMPT_SUFFIXES) and any("prompt" in part.lower() for part in parts)


def _is_model_config(path: str) -> bool:
    return path in MODEL_CONFIG_PATHS


def classify(changes: list[Change]) -> Classification:
    result = Classification()
    for change in changes:
        for path in change.all_paths:
            if path == LOG_PATH:
                result.log = True
            if _is_code(path):
                result.code.append(path)
            if _is_prompt(path):
                result.prompt.append(path)
            if _is_model_config(path):
                result.model_config.append(path)
    return result


# ---------------------------------------------------------------------------
# Log validation
# ---------------------------------------------------------------------------


def load_log(path: Path = ROOT / LOG_PATH) -> object:
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Unavailable(f"cannot read {path}: {error}") from error
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise Unavailable(f"{path} is not valid YAML: {error}") from error


def validate_log(document: object) -> list[str]:
    """Return problems with the AI-use log's structure; empty means valid."""
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["log must be a YAML mapping at the top level"]

    revision = document.get("reviewed_source_revision")
    if not isinstance(revision, str) or not REVISION_RE.match(revision):
        errors.append("reviewed_source_revision must be a 7-40 character hex commit id")

    records = document.get("records")
    if not isinstance(records, list) or not records:
        errors.append("records must be a non-empty list")
        return errors

    seen: set[str] = set()
    for index, record in enumerate(records):
        where = f"records[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{where} must be a mapping")
            continue
        for key in RECORD_REQUIRED:
            value = record.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where} is missing non-empty '{key}'")
        identifier = record.get("id")
        if isinstance(identifier, str):
            if identifier in seen:
                errors.append(f"{where} reuses id '{identifier}'")
            seen.add(identifier)
    return errors


# ---------------------------------------------------------------------------
# Top-level check
# ---------------------------------------------------------------------------


def evaluate(base: str, head: str, log_path: Path = ROOT / LOG_PATH, cwd: Path = ROOT) -> list[str]:
    """Return policy/schema problems for the base..head diff; empty means pass."""
    changes = diff_changes(base, head, cwd=cwd)
    classification = classify(changes)

    log_errors = validate_log(load_log(log_path))
    # A malformed log is always a failure, even when no governed change is present:
    # a log that does not parse cannot be relied upon to disclose anything.
    if log_errors:
        return [f"{LOG_PATH}: {error}" for error in log_errors]

    if classification.governed and not classification.log:
        touched = sorted(set(classification.code + classification.prompt + classification.model_config))
        detail = ", ".join(f"{kind}={sorted(set(getattr(classification, kind)))}"
                           for kind in ("code", "prompt", "model_config")
                           if getattr(classification, kind))
        return [
            "governed change without an AI-use log update: "
            f"{detail}. Touched: {', '.join(touched)}. "
            f"Update {LOG_PATH} in the same pull request."
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="git base ref (PR base sha)")
    parser.add_argument("--head", required=True, help="git head ref (PR head sha)")
    parser.add_argument("--log", type=Path, default=ROOT / LOG_PATH)
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    try:
        problems = evaluate(args.base, args.head, log_path=args.log, cwd=args.repo)
    except Unavailable as error:
        print(f"AI-use log check could not run: {error}", file=sys.stderr)
        return 2

    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1

    print(f"OK: AI-use log policy satisfied for {args.base}...{args.head}.")
    print("This checks that the log was updated and is well-formed; it does not attribute changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
