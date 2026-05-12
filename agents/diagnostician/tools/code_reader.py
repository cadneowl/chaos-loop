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


# Path-segment names we never want to scan or read. If any segment of a file's
# relative path matches one of these (case-sensitively on segment, not on the
# full path), the file is treated as if it doesn't exist.
DEFAULT_IGNORE_SEGMENTS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    "dist",
    "build",
    "site-packages",
    ".idea",
    ".vscode",
    ".next",
    ".nuxt",
    "target",  # rust/maven build output
    # Test directories — full of intentional bad patterns (mocks, fixtures with
    # secret-shaped strings, etc.) that look like fragilities but aren't.
    "tests",
    "test",
    "__tests__",
    "spec",
})


class TargetCodeReader:
    """Read-only access to files under a target repo root.

    Files whose relative path contains any segment in ``ignore_segments`` are
    invisible to ``list_files``, ``grep``, and ``read_file`` — that's how we
    keep ``.venv`` and friends out of detector output without every detector
    needing its own ignore logic.
    """

    def __init__(
        self,
        root: Path,
        *,
        ignore_segments: frozenset[str] | None = None,
    ) -> None:
        if not root.exists():
            raise CodeReadError(f"target root does not exist: {root}")
        if not root.is_dir():
            raise CodeReadError(f"target root is not a directory: {root}")
        # Resolve symlinks so the root we check against is stable.
        self.root = root.resolve()
        self.ignore_segments = ignore_segments if ignore_segments is not None else DEFAULT_IGNORE_SEGMENTS

    def _is_ignored(self, relative_path: str) -> bool:
        """True if any segment of `relative_path` matches an ignore pattern."""
        # Normalize separators for cross-platform check.
        norm = relative_path.replace("\\", "/")
        return any(seg in self.ignore_segments for seg in norm.split("/"))

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
        file. Files whose path matches an ignored segment are treated as if
        they don't exist (consistent with list_files / grep).
        """
        if self._is_ignored(relative):
            raise CodeReadError(f"file is in an ignored path: {relative}")
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

        # Line range mode. Always 1-indexed inclusive on both ends. We stream
        # the file instead of slurping so a multi-GB file with a 10-line
        # request doesn't OOM the process.
        ls = max(1, line_start or 1)
        le = line_end or 10**9
        if le < ls:
            raise CodeReadError(f"line_end ({le}) < line_start ({ls})")
        # Cap the slurped bytes even in streaming mode to bound runaway lines.
        max_chars = 1_000_000
        collected: list[str] = []
        total = 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i < ls:
                    continue
                if i > le:
                    break
                collected.append(line)
                total += len(line)
                if total > max_chars:
                    raise CodeReadError(
                        f"line range yielded > {max_chars} chars; narrow the range"
                    )
        return "".join(collected)

    def list_files(self, glob: str = "**/*") -> list[str]:
        """List files matching a glob, returned as paths relative to root.

        Paths containing any ignored segment (e.g. ``.venv``) are filtered out.
        """
        out: list[str] = []
        for p in self.root.glob(glob):
            if not p.is_file():
                continue
            rel = str(p.relative_to(self.root))
            if self._is_ignored(rel):
                continue
            out.append(rel)
        return sorted(out)

    def grep(self, pattern: str, *, glob: str = "**/*") -> list[tuple[str, int, str]]:
        """Search for `pattern` in files matching `glob`. Returns (path, lineno, line).

        Paths containing any ignored segment are skipped.
        """
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise CodeReadError(f"invalid regex: {pattern!r}: {e}") from e
        results: list[tuple[str, int, str]] = []
        for path in self.root.glob(glob):
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.root))
            if self._is_ignored(rel):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append((rel, i, line))
        return results
