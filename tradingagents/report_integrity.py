"""Report integrity manifest: a digest record of the files one save actually wrote.

ADR-002 section 5 (requirement FR-C). :func:`write_manifest` is called by
``tradingagents.reporting.write_report_tree`` after every report file has been written and
after stale files have been reconciled, so the manifest describes exactly what exists at the
end of that save. :func:`verify_report_manifest` and the module's ``__main__`` entry check a
saved directory offline.

Scope, stated plainly:

* The manifest detects divergence between the saved report files and the manifest. It is
  **not** an authenticity or signature mechanism: someone who can rewrite both a report and
  its manifest can produce a matching pair. No key, signature or network call is involved.
* It lists the files this run wrote, never the manifest itself and never a file the writer
  does not own (a reader's own notes survive a regeneration and stay out of the manifest).
* Verification never opens a path that resolves outside the report root, and refuses an
  absolute, drive-qualified, UNC or traversal entry before any file is opened.
* Verification is **not** vacuously successful: an empty entry list, a ``file_count`` that
  disagrees with the list, a repeated path, an unreadable size or a malformed digest is a
  malformed manifest, not a pass.

Exit codes of ``python -m tradingagents.report_integrity REPORT_DIR``:

===== ==========================================================================
``0`` every listed file exists with the recorded size and digest
``1`` a listed file is missing or modified, or the manifest lists an unsafe path
``2`` the manifest is absent, unreadable, malformed or of an unsupported version
===== ==========================================================================

Standard library only: no new dependency and no network path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = "report_manifest.json"
MANIFEST_VERSION = 1
ALGORITHM = "sha256"
GENERATOR = "tradingagents.report_integrity"

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_UNREADABLE = 2

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestWriteError(RuntimeError):
    """A manifest could not be written because a file the save produced is not usable.

    Raised instead of writing an incomplete manifest that would verify as valid: the saved
    report tree would then *claim* integrity it does not have (ADR-002 section 5, RI-1).
    """


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _resolve_written(root: Path, candidate) -> str:
    """The root-relative POSIX path of one file the caller just wrote, or ``ManifestWriteError``.

    The contract is unambiguous: ``root`` is the report directory, and ``candidate`` is either
    absolute or relative **to that root** (never relative to the process's working directory).
    A relative path is joined once, so ``reports/NVDA`` as root and ``complete_report.md`` as
    file resolve to ``reports/NVDA/complete_report.md`` -- not to a duplicated
    ``reports/NVDA/reports/NVDA/...`` that would silently be skipped. A file that does not
    exist, is not a regular file, or resolves outside ``root`` is an error rather than an
    omission, because it means the save and the manifest would disagree.
    """
    path = Path(candidate)
    if not path.is_absolute():
        path = root / path
    if path.name == MANIFEST_NAME:
        raise ManifestWriteError("the manifest never lists itself")
    if not path.exists():
        raise ManifestWriteError(f"the report writer listed a file it did not write: {candidate!r}")
    if path.is_symlink():
        # A link is not a plain file this writer produced; recording it would let a later
        # verification follow it out of the tree (INV-17).
        raise ManifestWriteError(f"the report writer listed a symbolic link: {candidate!r}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ManifestWriteError(f"the report writer listed something that is not a file: {candidate!r}")
    try:
        return _relative(resolved, root.resolve())
    except ValueError:
        raise ManifestWriteError(f"the report writer listed a file outside the report root: {candidate!r}") from None


def write_manifest(root, ticker: str | None = None,
                   files: Iterable | None = None) -> Path:
    """Write ``report_manifest.json`` in ``root`` for the report files that exist there.

    ``root`` is the report directory, relative to the process working directory or absolute.
    ``files`` is the list of files the caller just wrote, given either absolute or relative to
    ``root`` -- this is the source of truth for *what belongs to this save*. Without it, every
    regular file directly under ``root`` (excluding the manifest) is recorded, which is only
    right for a folder this writer owns entirely.

    A listed file that is missing, not a regular file, a symbolic link, the manifest itself or
    outside ``root`` raises :class:`ManifestWriteError`: a manifest that silently omitted it
    would describe a different tree from the one on disk. An empty manifest is likewise
    refused -- a saved report tree always contains at least the complete report.

    Entries are sorted by relative path so the document is deterministic for a given tree. The
    document is written to a uniquely named temporary file in a private temporary directory and
    moved into place, so a save never depends on (or destroys) a pre-existing file at a fixed
    temp name, and an interrupted save never leaves a half-written manifest.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if files is None:
        candidates = sorted(path for path in root.glob("*") if path.is_file())
    else:
        candidates = list(files)
    entries = []
    seen: set[str] = set()
    for candidate in candidates:
        relative = _resolve_written(root, candidate)
        if relative in seen:
            raise ManifestWriteError(f"the report writer listed the same file twice: {candidate!r}")
        seen.add(relative)
        resolved = (root / relative).resolve()
        entries.append({
            "path": relative,
            "size": resolved.stat().st_size,
            "sha256": _digest(resolved),
        })
    entries.sort(key=lambda entry: entry["path"])
    if not entries:
        raise ManifestWriteError(
            "refusing to write an empty report manifest: a saved report tree lists at least "
            "its complete report"
        )
    document = {
        "manifest_version": MANIFEST_VERSION,
        "algorithm": ALGORITHM,
        "generator": GENERATOR,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ticker": ticker,
        "file_count": len(entries),
        "files": entries,
        "excluded": [MANIFEST_NAME],
    }
    target = root / MANIFEST_NAME
    # A unique temporary file in a private directory, then one atomic rename. Nothing at a
    # predictable path in the report tree is opened or reused, so a user file (or a planted
    # symlink) sitting at ``.<MANIFEST_NAME>.tmp`` cannot be clobbered or followed.
    descriptor, temporary = tempfile.mkstemp(prefix="report-manifest-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(document, indent=2) + "\n")
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return target


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass
class ManifestProblem:
    """One verification problem: the affected entry (or the manifest) and a reason."""

    path: str
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.detail}"


@dataclass
class VerificationResult:
    """The outcome of verifying one report directory.

    ``ok`` is true only when the manifest is readable, of this version, well formed, and every
    entry matches. ``problems`` lists every failure, each naming its path. ``checked`` counts
    the entries that were compared, so a caller can assert that a pass actually covered files
    rather than an empty list.
    """

    root: str
    ok: bool
    checked: int = 0
    problems: list[ManifestProblem] = field(default_factory=list)
    manifest: dict[str, Any] | None = None

    def summary(self) -> str:
        if not self.ok:
            lines = [f"Report integrity check FAILED for {self.root}:"]
            lines.extend(f"  - {problem}" for problem in self.problems)
            return "\n".join(lines)
        return (f"Report integrity check passed for {self.root}: "
                f"{self.checked} file(s) match {MANIFEST_NAME}.")


def _unsafe_reason(entry: str) -> str | None:
    """Why ``entry`` may not be opened as a path inside the report root, or ``None``."""
    if not isinstance(entry, str) or not entry.strip():
        return "empty path"
    text = entry.replace("\\", "/")
    if text.startswith("//"):
        return "UNC path"
    if text.startswith("/"):
        return "absolute path"
    if len(text) > 1 and text[1] == ":":
        return "drive-qualified path"
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return "empty path"
    if ".." in parts:
        return "path traversal"
    return None


def _resolve_inside(root: Path, entry: str) -> tuple[Path | None, str | None]:
    """The resolved file for ``entry`` inside ``root``, or a reason it may not be read."""
    reason = _unsafe_reason(entry)
    if reason is not None:
        return None, reason
    if Path(entry).is_absolute():
        return None, "absolute path"
    candidate = root / entry
    # A link anywhere on the path could point outside the root; only a link that resolves
    # back inside the root is acceptable, and the final target itself must be inside.
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None, "resolves outside the report root"
    return candidate, None


def _symlink_on_path(root: Path, candidate: Path) -> str | None:
    """A component of ``candidate`` inside ``root`` that is a symbolic link or reparse point.

    A link is reported by name whether it points inside or outside the root: a manifest entry
    pointing *through* a link is not a plain file inside the tree, and following it is what
    the path-safety rule exists to prevent. ``None`` when the path is plain.
    """
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return "resolves outside the report root"
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink() or os.path.islink(current):
                return f"'{current.relative_to(root).as_posix()}' is a symbolic link"
        except OSError:  # pragma: no cover - a name the OS cannot stat
            return f"'{current.relative_to(root).as_posix()}' cannot be inspected"
    return None


def _manifest_problem(problems: list[ManifestProblem], code: str, detail: str) -> None:
    problems.append(ManifestProblem(MANIFEST_NAME, code, detail))


def _check_entry(entry: Any, problems: list[ManifestProblem]) -> tuple[str, int, str] | None:
    """Validate one entry's shape; return ``(path, size, digest)`` or record a malformed entry."""
    if not isinstance(entry, dict):
        _manifest_problem(problems, "malformed_manifest", "a file entry is not an object")
        return None
    name = entry.get("path")
    if not isinstance(name, str) or not name.strip():
        _manifest_problem(problems, "malformed_manifest", "a file entry has no non-empty 'path'")
        return None
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        problems.append(ManifestProblem(name, "malformed_manifest",
                                        "entry 'size' must be a non-negative integer"))
        return None
    digest = entry.get("sha256")
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest.lower()):
        problems.append(ManifestProblem(name, "malformed_manifest",
                                        "entry 'sha256' must be 64 hexadecimal characters"))
        return None
    return name, size, digest.lower()


def _check_header(document: dict[str, Any], problems: list[ManifestProblem]) -> list | None:
    """Validate the manifest header and entry-list shape; return the list when it is usable."""
    if document.get("manifest_version") != MANIFEST_VERSION:
        _manifest_problem(problems, "unsupported_manifest_version",
                          f"expected manifest_version {MANIFEST_VERSION}, "
                          f"got {document.get('manifest_version')!r}")
        return None
    if document.get("algorithm") != ALGORITHM:
        _manifest_problem(problems, "unsupported_algorithm",
                          f"expected algorithm {ALGORITHM!r}, got {document.get('algorithm')!r}")
        return None
    entries = document.get("files")
    if not isinstance(entries, list):
        _manifest_problem(problems, "malformed_manifest", "'files' is not a list")
        return None
    if not entries:
        # A saved report tree always lists at least its complete report; an empty list would
        # otherwise verify with zero files checked, which is not evidence of anything.
        _manifest_problem(problems, "malformed_manifest", "'files' is empty")
        return None
    count = document.get("file_count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(entries):
        _manifest_problem(problems, "malformed_manifest",
                          f"'file_count' is {count!r} but 'files' lists {len(entries)} entr"
                          f"{'y' if len(entries) == 1 else 'ies'}")
        return None
    return entries


def _check_entries(root: Path, entries: list, problems: list[ManifestProblem]) -> int:
    """Compare every entry against the file on disk; return the number that matched."""
    checked = 0
    seen: set[str] = set()
    for entry in entries:
        parsed = _check_entry(entry, problems)
        if parsed is None:
            continue
        name, size, digest = parsed
        key = name.replace("\\", "/")
        if key in seen:
            problems.append(ManifestProblem(name, "malformed_manifest",
                                            "the manifest lists this path more than once"))
            continue
        seen.add(key)
        candidate, reason = _resolve_inside(root, name)
        if candidate is None:
            problems.append(ManifestProblem(name, "unsafe_path", reason or "unsafe path"))
            continue
        link = _symlink_on_path(root, candidate)
        if link is not None:
            problems.append(ManifestProblem(name, "unsafe_path", link))
            continue
        if not candidate.is_file():
            problems.append(ManifestProblem(name, "missing_file",
                                            "the listed file does not exist"))
            continue
        try:
            actual_size = candidate.stat().st_size
        except OSError as exc:  # pragma: no cover - a file removed after the check
            problems.append(ManifestProblem(name, "missing_file", type(exc).__name__))
            continue
        if actual_size != size:
            problems.append(ManifestProblem(
                name, "modified_file", f"size is {actual_size} bytes, the manifest records {size}"))
            continue
        if _digest(candidate) != digest:
            problems.append(ManifestProblem(
                name, "modified_file", "the file's sha256 differs from the manifest"))
            continue
        checked += 1
    return checked


def verify_report_manifest(report_dir) -> VerificationResult:
    """Check every file listed in ``report_dir``'s manifest against its size and digest."""
    root = Path(report_dir)
    if root.is_symlink():
        return VerificationResult(str(root), False, problems=[
            ManifestProblem(str(root), "unsafe_path", "the report directory is a symbolic link")
        ])
    if not root.is_dir():
        return VerificationResult(str(root), False, problems=[
            ManifestProblem(str(root), "missing_directory", "the report directory does not exist")
        ])
    manifest_path = root / MANIFEST_NAME
    # Refuse a manifest that is a link before opening it: a link could point anywhere, and
    # reading it would be exactly the read outside the tree that INV-17 forbids.
    if manifest_path.is_symlink():
        return VerificationResult(str(root), False, problems=[
            ManifestProblem(MANIFEST_NAME, "unsafe_path", "the manifest is a symbolic link")
        ])
    if not manifest_path.is_file():
        return VerificationResult(str(root), False, problems=[
            ManifestProblem(MANIFEST_NAME, "missing_manifest",
                            "no report manifest in the report directory")
        ])
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return VerificationResult(str(root), False, problems=[
            ManifestProblem(MANIFEST_NAME, "unreadable_manifest",
                            f"the manifest could not be read ({type(exc).__name__})")
        ])
    try:
        document = json.loads(text)
    except ValueError:
        return VerificationResult(str(root), False, problems=[
            ManifestProblem(MANIFEST_NAME, "malformed_manifest", "the manifest is not valid JSON")
        ])
    if not isinstance(document, dict):
        return VerificationResult(str(root), False, problems=[
            ManifestProblem(MANIFEST_NAME, "malformed_manifest", "the manifest is not a JSON object")
        ])

    problems: list[ManifestProblem] = []
    entries = _check_header(document, problems)
    if entries is None:
        return VerificationResult(str(root), False, manifest=document, problems=problems)
    checked = _check_entries(root, entries, problems)
    return VerificationResult(str(root), not problems, checked, problems, document)


def main(argv: list[str] | None = None) -> int:
    """Command entry: verify one report directory; print the outcome and return the exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: python -m tradingagents.report_integrity REPORT_DIR", file=sys.stderr)
        return EXIT_UNREADABLE
    result = verify_report_manifest(arguments[0])
    print(result.summary(), file=sys.stdout if result.ok else sys.stderr)
    if result.ok:
        return EXIT_OK
    unreadable = {"missing_directory", "missing_manifest", "unreadable_manifest",
                  "malformed_manifest", "unsupported_manifest_version", "unsupported_algorithm"}
    if any(problem.code in unreadable for problem in result.problems):
        return EXIT_UNREADABLE
    return EXIT_PROBLEM


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``
    raise SystemExit(main())
