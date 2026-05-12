"""Detectors for network / external-dep fragilities.

Most of these are regex jobs. The patterns we look for and what they imply:

    - ``MissingTimeoutDetector`` — http / subprocess calls without ``timeout=``.
      A request without timeout can hang indefinitely; chaos with `network.delay`
      will surface this.
    - ``MissingRetryDetector`` — calls into Redis / HTTP / DB / queue with no
      retry decorator or backoff loop in scope. `network.loss` tests this.
"""

from __future__ import annotations

import re

from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.tester.detectors._base import Issue

# --------------------------------------------------------------------------- #
# Missing-timeout                                                             #
# --------------------------------------------------------------------------- #


# Match call sites for libraries that accept a timeout=. Captures the full call
# expression up to the closing paren on the SAME line — multi-line calls are a
# v2 problem and probably need an AST walker.
_TIMEOUT_CALL_PATTERNS = [
    re.compile(r"\b(?:requests|httpx)\.(?:get|post|put|delete|patch|request)\s*\(([^)]*)\)"),
    re.compile(r"\baiohttp\.ClientSession\([^)]*\)\.(?:get|post)\s*\(([^)]*)\)"),
    re.compile(r"\bsubprocess\.(?:run|Popen|call|check_output)\s*\(([^)]*)\)"),
]


class MissingTimeoutDetector:
    """Find http / subprocess calls that don't pass ``timeout=``.

    Scope: single-line call expressions. Multi-line calls and helper-wrapped
    calls won't be detected — false negatives are OK; false positives are not.
    """

    name = "missing-timeout"

    def find(self, code: TargetCodeReader) -> list[Issue]:
        out: list[Issue] = []
        for path in code.list_files("**/*.py"):
            try:
                text = code.read_file(path)
            except Exception:
                continue
            for line_num, line in enumerate(text.splitlines(), start=1):
                for pat in _TIMEOUT_CALL_PATTERNS:
                    m = pat.search(line)
                    if m and "timeout=" not in m.group(0):
                        out.append(
                            Issue(
                                file=path,
                                line=line_num,
                                snippet=line.strip(),
                                detail=m.group(0),
                            )
                        )
                        break  # one finding per line is enough
        return out


# --------------------------------------------------------------------------- #
# Missing-retry                                                               #
# --------------------------------------------------------------------------- #


_EXTERNAL_DEP_CALL = re.compile(
    r"\b(?:redis|httpx|requests|aiohttp|psycopg2?|asyncpg|kafka|boto3|gcloud)\."
)
# Patterns whose presence ANYWHERE in the file means "retry / resilience is
# already considered" — we suppress findings in those files.
_RETRY_HINTS = re.compile(
    r"\b(?:retry|@retry|tenacity|backoff|RetryError|ConnectionPoolError|max_attempts|attempt\s*[<>=])"
)


class MissingRetryDetector:
    """Find files that call external deps with no retry / backoff in scope.

    Scope: file-level. If the file mentions any retry primitive at all, we
    assume the developer thought about it and skip. This is intentionally
    coarse — file-level avoids flagging defensive code as fragile.
    """

    name = "missing-retry"

    def find(self, code: TargetCodeReader) -> list[Issue]:
        out: list[Issue] = []
        for path in code.list_files("**/*.py"):
            try:
                text = code.read_file(path)
            except Exception:
                continue
            if _RETRY_HINTS.search(text):
                continue
            for line_num, line in enumerate(text.splitlines(), start=1):
                m = _EXTERNAL_DEP_CALL.search(line)
                if not m:
                    continue
                # Avoid firing on import / from lines (those aren't call sites).
                stripped = line.lstrip()
                if stripped.startswith(("import ", "from ")):
                    continue
                out.append(
                    Issue(
                        file=path,
                        line=line_num,
                        snippet=line.strip(),
                        detail=m.group(0),
                    )
                )
                # First call site per file is enough — repeating fragility per file
                # would just spam the report.
                break
        return out
