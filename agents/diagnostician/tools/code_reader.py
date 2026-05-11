"""
Sandboxed file reader for the target repo.

The diagnostician + fixer both read source code as part of their cognitive work.
This module enforces a single rule: every path access stays within `root`. No
".." escape, no symlinks pointing outside, no absolute paths to other parts of
the filesystem.

A malformed path raises CodeReadError. The caller — typically an LLM tool wrapper —
should propagate this as a tool error, not silently fall back.
"""

from __future__ import annotations

import re
from pathlib import Path


class CodeReadError(RuntimeError):
    """Raised when a file read is blocked by sandbox policy or the file doesn't exist."""


class TargetCodeReader:
    """Read-only access to files under a target repo root."""

    def __init__(self, root: Path) -> None:
        if not root.exists():
            raise CodeReadError(f"target root does not exist: {root}")
        if not root.is_dir():
            raise CodeReadError(f"target root is not a directory: {root}")
        # Resolve symlinks so the root we check against is stable.
        self.root = root.resolve()

    def _resolve_within_root(self, relative: str) -> Path:
        """Resolve a caller-supplied path; raise if it escapes root."""
        # Forbid absolute paths outright. On Windows `Path('/etc/passwd').is_absolute()`
        # returns False (no drive letter), so we also explicitly reject leading-slash forms.
        if Path(relative).is_absolute() or relative.startswith(("/", "\\")):
            raise CodeReadError(f"absolute path not allowed: {relative}")
        candidate = (self.root / relative).resolve()
        # On Windows, .resolve() with strict=False still resolves "..".
        try:
            candidate.relative_to(self.root)
        except ValueError as e:
            raise CodeReadError(
                f"path escapes target root: {relative} -> {candidate}"
            ) from e
        return candidate

    def read_file(
        self, relative: str, *, line_start: int | None = None, line_end: int | None = None
    ) -> str:
        """Read a file, optionally limited to a line range (1-indexed, inclusive).

        Files larger than 1 MB are rejected unless a line range is provided —
        the diagnostician should ask for a specific window, not slurp a whole
        file.
        """
        path = self._resolve_within_root(relative)
        if not path.exists():
            raise CodeReadError(f"file not found: {relative}")
        if not path.is_file():
            raise CodeReadError(f"not a file: {relative}")

        if line_start is None and line_end is None:
            size = path.stat().st_size
            if size > 1_000_000:
                raise CodeReadError(
                    f"file too large ({size} bytes); request a line range"
                )
            return path.read_text(encoding="utf-8", errors="replace")

        # Line range mode. Always 1-indexed inclusive on both ends.
        ls = max(1, line_start or 1)
        le = line_end or 10**9
        if le < ls:
            raise CodeReadError(f"line_end ({le}) < line_start ({ls})")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        return "".join(lines[ls - 1 : le])

    def list_files(self, glob: str = "**/*") -> list[str]:
        """List files matching a glob, returned as paths relative to root."""
        # Glob is interpreted from root; results are relative.
        return sorted(
            str(p.relative_to(self.root)) for p in self.root.glob(glob) if p.is_file()
        )

    def grep(self, pattern: str, *, glob: str = "**/*") -> list[tuple[str, int, str]]:
        """Search for `pattern` in files matching `glob`. Returns (path, lineno, line)."""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise CodeReadError(f"invalid regex: {pattern!r}: {e}") from e
        results: list[tuple[str, int, str]] = []
        for path in self.root.glob(glob):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append((str(path.relative_to(self.root)), i, line))
        return results
