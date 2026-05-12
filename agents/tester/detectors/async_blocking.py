"""Detector for synchronous blocking calls inside ``async def`` functions.

Sync I/O inside a coroutine blocks the whole event loop — every other
task stalls while that one call runs. ``network.delay`` against the dep
amplifies the effect: a 5s delayed sync request stalls the loop for 5s.

The detector walks the file line-by-line, tracks whether we're "inside an
async def" via indent depth, and flags known-sync calls in that scope.
``await`` and ``to_thread`` / ``run_in_executor`` escapes are ignored.
"""

from __future__ import annotations

import re

from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.tester.detectors._base import Issue

_ASYNC_DEF = re.compile(r"^(\s*)async\s+def\s+\w+\s*\(")
# Any function or class definition at <= async_indent closes the async scope.
# Decorators (``@foo``) sit at the def's indent — we treat them as the start of
# the next definition so they don't prematurely close the current scope.
_DEF_BOUNDARY = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+\w+")
_DECORATOR = re.compile(r"^(\s*)@\w[\w.]*")
# Known-sync blocking calls. Conservative — we'd rather miss a custom blocking
# call than flag e.g. ``async_requests.get``.
_SYNC_CALLS = re.compile(
    r"\b("
    r"time\.sleep|"
    r"requests\.(?:get|post|put|delete|patch|head|request)|"
    r"urllib\.request\.|"
    r"urllib2\.|"
    r"socket\.(?:recv|send|connect)|"
    r"subprocess\.(?:run|call|check_output|check_call)|"
    r"os\.system"
    r")\s*\("
)
# These on the SAME line mean the sync call is properly off-loaded.
_OFFLOAD_HINTS = re.compile(
    r"\b(?:to_thread|run_in_executor|asyncio\.to_thread|loop\.run_in_executor)\b"
)


class SyncCallInAsyncDetector:
    """Flag sync blocking calls inside ``async def`` function bodies.

    Tracks async scope via indent: a line is "inside" an async def while its
    indent is strictly greater than the async def's indent, until we hit a
    same-or-lesser-indent ``def`` / ``async def`` (function boundary).
    """

    name = "sync-call-in-async"

    def find(self, code: TargetCodeReader) -> list[Issue]:
        out: list[Issue] = []
        for path in code.list_files("**/*.py"):
            try:
                text = code.read_file(path)
            except Exception:
                continue
            out.extend(self._scan_file(path, text))
        return out

    def _scan_file(self, path: str, text: str) -> list[Issue]:
        out: list[Issue] = []
        async_indent: int | None = None  # None means "not in an async def"
        for line_num, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            indent = _leading_ws(line)
            # Decorators belong to the next def — skip them entirely so they
            # don't trip the boundary check at the same indent as ``def``.
            if _DECORATOR.match(line):
                continue
            # Function-or-class boundary at <= async_indent ends the async scope.
            if (
                async_indent is not None
                and indent <= async_indent
                and _DEF_BOUNDARY.match(line)
            ):
                async_indent = None  # fall through to async-def check below
            # Entering an async def — record its indent.
            m_async = _ASYNC_DEF.match(line)
            if m_async:
                async_indent = len(m_async.group(1))
                continue
            if async_indent is None:
                continue
            # Inside an async def. Check for sync calls — but ignore comments,
            # awaited expressions, and explicitly off-loaded calls.
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _OFFLOAD_HINTS.search(line):
                continue
            m_sync = _SYNC_CALLS.search(line)
            if not m_sync:
                continue
            # A sync name preceded by ``await`` is being misidentified by us
            # (e.g., ``await asyncio.to_thread(time.sleep, ...)`` — already
            # guarded above, but defense in depth). Skip if the immediate
            # preceding token is ``await``.
            if _await_precedes(line, m_sync.start()):
                continue
            out.append(
                Issue(
                    file=path,
                    line=line_num,
                    snippet=line.strip(),
                    detail=m_sync.group(1),
                )
            )
        return out


def _leading_ws(line: str) -> int:
    return len(line) - len(line.lstrip())


def _await_precedes(line: str, idx: int) -> bool:
    """True if `await ` appears immediately before position `idx` (ignoring whitespace)."""
    prefix = line[:idx].rstrip()
    return prefix.endswith("await")
