"""Scoped static-type, dependency, and security checks for CI.

Three independent checks live here so one CI job or several can call them via
``python -m tools.ci_checks <check>``:

``type``
    Run ``mypy`` over a small, explicitly listed set of modules that already type
    clean. This is an honest first step, not whole-repo typing: the rest of the
    package has type debt (untyped functions, upstream SDK gaps). The scope is a
    constant below and is printed on every run so a green result cannot be read as
    "the project is fully typed".

``deps``
    Run ``pip check`` to detect broken/conflicting installed requirements. This
    checks the *installed* environment for unmet or inconsistent dependencies; it
    does not audit versions for vulnerabilities (that is ``security``'s job) and
    does not prove the declared bounds resolve in every other environment.

``security``
    A meaningful, dependency-light set with a stated scope:
      * statically scan tracked Python for a small set of high-signal patterns
        (``eval``/``exec``/``os.system``/``subprocess(..., shell=True)`` and
        obvious hard-coded credential literals);
      * confirm no tracked file that looks like a secret store or key file is
        committed (``.env``, ``*.pem``, ``*.key``, ``id_rsa``, ``*.p12``, ...).
    This is NOT a blanket security audit. It does not run a vulnerability scanner
    against dependencies, does not prove the absence of secrets in history, and
    does not review logic. Its scope is stated in ``SECURITY_SCOPE`` and printed.

Exit codes: 0 pass; 1 a real finding; 2 the check could not run.

Run from the repository root.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Modules that pass `mypy --check-untyped-defs --disallow-untyped-calls
# --warn-unused-ignores --follow-imports=skip` today. Chosen because they are
# self-contained configuration/reading modules with no typed-upstream SDK
# boundary crossed. Widen this list only when the additions actually pass.
TYPE_SCOPE: tuple[str, ...] = (
    "tradingagents/llm_clients/validators.py",
    "tradingagents/llm_clients/model_catalog.py",
    "tradingagents/llm_clients/base_client.py",
    "tradingagents/config_validation.py",
)

MYPY_FLAGS: tuple[str, ...] = (
    "--follow-imports=skip",
    "--ignore-missing-imports",
    "--check-untyped-defs",
    "--warn-unused-ignores",
    "--warn-redundant-casts",
    "--warn-return-any",
    "--no-implicit-optional",
    "--strict-equality",
    "--no-error-summary",
)

# Documented debt: these are the modules most likely to be widened next, and the
# reason they are not in scope yet. Printed with every run so the gap is visible.
TYPE_DEBT = (
    "Whole-repo typing is not enforced. Known debt: provider clients under "
    "tradingagents/llm_clients/*_client.py (upstream SDK boundary, ~15 errors), "
    "agents/* (untyped helpers), dataflows/* (heavy pandas/yfinance interfaces). "
    "TYPE_SCOPE lists only what this check actually guarantees."
)

SECURITY_SCOPE = (
    "Pattern scan of tracked .py for eval/exec/os.system/subprocess(shell=True) and "
    "obvious hard-coded secrets, plus a check that no plausible key/cert/env file is "
    "committed. Not a dependency-vulnerability scan, not a history audit, not a logic review."
)

_DANGEROUS_CALLS = (
    re.compile(r"(?<![\w.])eval\s*\("),
    re.compile(r"(?<![\w.])exec\s*\("),
    re.compile(r"os\.system\s*\("),
    re.compile(r"subprocess\.(?:run|call|Popen|check_\w+)\s*\([^)]*shell\s*=\s*True"),
)

# A few high-signal credential shapes. Deliberately narrow: we want near-zero
# false positives so a finding is worth reading. Known key prefixes are the most
# reliable signal.
_SECRET_LITERALS = (
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),          # OpenAI-style key
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"),   # Anthropic key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),           # AWS access key id
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),        # GitHub PAT
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)

_SECRET_FILE_NAMES = {".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")

# Files whose contents are full of the very patterns above (this checker, its
# tests, docs) must not be scanned for their own regexes.
_SCAN_EXCLUDES = (
    "tools/ci_checks.py",
    "tests/test_ci_checks.py",
)


class CheckUnavailable(Exception):
    """A check could not run (missing tool, no git, bad environment)."""


def _tracked_python(cwd: Path) -> list[Path]:
    try:
        output = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as error:
        raise CheckUnavailable("git executable not found") from error
    if output.returncode != 0:
        raise CheckUnavailable(f"git ls-files failed: {output.stderr.strip()}")
    return [cwd / line for line in output.stdout.splitlines() if line.strip()]


def _tracked_files(cwd: Path) -> list[str]:
    try:
        output = subprocess.run(
            ["git", "ls-files"], cwd=cwd, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as error:
        raise CheckUnavailable("git executable not found") from error
    if output.returncode != 0:
        raise CheckUnavailable(f"git ls-files failed: {output.stderr.strip()}")
    return [line for line in output.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Type check
# ---------------------------------------------------------------------------


def run_type(cwd: Path = ROOT) -> tuple[int, str]:
    try:
        import mypy  # noqa: F401
    except ImportError as error:
        raise CheckUnavailable("mypy is not installed (pip install 'mypy==1.11.2')") from error

    missing = [name for name in TYPE_SCOPE if not (cwd / name).exists()]
    if missing:
        raise CheckUnavailable(f"type-check scope files are missing: {missing}")

    completed = subprocess.run(
        [sys.executable, "-m", "mypy", *TYPE_SCOPE, *MYPY_FLAGS],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    report = [
        f"mypy scope ({len(TYPE_SCOPE)} files):",
        *(f"  - {name}" for name in TYPE_SCOPE),
        TYPE_DEBT,
        completed.stdout.strip(),
        completed.stderr.strip(),
    ]
    return completed.returncode, "\n".join(part for part in report if part)


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------


def run_deps(cwd: Path = ROOT) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    # pip check writes findings to stdout and prints its own return code semantics;
    # a non-zero code is the signal.
    return completed.returncode, (completed.stdout + completed.stderr).strip()


# ---------------------------------------------------------------------------
# Security check
# ---------------------------------------------------------------------------


def _is_excluded(relative: str) -> bool:
    return any(relative == entry or relative.endswith("/" + entry) for entry in _SCAN_EXCLUDES)


def scan_patterns(cwd: Path) -> list[str]:
    findings: list[str] = []
    for path in _tracked_python(cwd):
        relative = path.relative_to(cwd).as_posix()
        if _is_excluded(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            # Skip pure comments so an example in a docstring/comment is not a hit.
            if stripped.startswith("#"):
                continue
            for pattern in _DANGEROUS_CALLS:
                if pattern.search(line):
                    findings.append(f"{relative}:{lineno}: dangerous call: {line.strip()[:100]}")
            for pattern in _SECRET_LITERALS:
                if pattern.search(line):
                    findings.append(f"{relative}:{lineno}: possible hard-coded secret")
    return findings


def scan_secret_files(cwd: Path) -> list[str]:
    findings: list[str] = []
    for name in _tracked_files(cwd):
        base = name.rsplit("/", 1)[-1]
        lower = base.lower()
        if base in _SECRET_FILE_NAMES or lower.endswith(_SECRET_FILE_SUFFIXES):
            # .env.example is a documented template with placeholders, not a secret.
            if lower.endswith(".example") or lower.endswith(".sample"):
                continue
            findings.append(f"tracked secret-like file: {name}")
    return findings


def run_security(cwd: Path = ROOT) -> tuple[int, str]:
    findings = scan_patterns(cwd) + scan_secret_files(cwd)
    report = [f"security scope: {SECURITY_SCOPE}"]
    report.append(f"findings: {len(findings)}")
    report.extend(f"  - {finding}" for finding in findings)
    return (1 if findings else 0), "\n".join(report)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

CHECKS = {"type": run_type, "deps": run_deps, "security": run_security}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=sorted(CHECKS))
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    try:
        code, report = CHECKS[args.check](cwd=args.repo)
    except CheckUnavailable as error:
        print(f"{args.check} check could not run: {error}", file=sys.stderr)
        return 2
    stream = sys.stdout if code == 0 else sys.stderr
    print(report, file=stream)
    print(f"{args.check}: {'PASS' if code == 0 else 'FAIL'}", file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
