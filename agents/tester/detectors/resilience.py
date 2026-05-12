"""Detectors for missing resilience patterns: circuit breakers + cache fallbacks.

Both are file-level heuristics in the same spirit as MissingRetryDetector — if the
file shows no awareness of the pattern at all, flag the first relevant call site.

    - ``MissingCircuitBreakerDetector`` — external-dep calls in a file with no
      circuit-breaker primitive in scope. ``network.partition`` will surface
      this: without a breaker, the caller's thread pool / connection pool
      exhausts while every request times out against the gone-dep.
    - ``NoFallbackForCacheDetector`` — cache GET calls (redis/memcached/valkey)
      in a file with no ``try/except`` anywhere. ``pod.kill`` of the cache
      surfaces this: the caller raises instead of falling through to source-
      of-truth.
"""

from __future__ import annotations

import re

from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.tester.detectors._base import EXTERNAL_DEP_CALL as _EXTERNAL_DEP_CALL
from agents.tester.detectors._base import Issue

# --------------------------------------------------------------------------- #
# Missing-circuit-breaker                                                     #
# --------------------------------------------------------------------------- #


# Any of these strings anywhere in the file means "circuit breaker is at least
# considered" — same file-level heuristic as MissingRetryDetector.
_BREAKER_HINTS = re.compile(
    r"\b(?:pybreaker|circuitbreaker|CircuitBreaker|aiobreaker|hyx\.circuit|"
    r"@circuit|@breaker|circuit_breaker|breaker\.call|polly|resilience4j)\b"
)


class MissingCircuitBreakerDetector:
    """Find files with external-dep calls and no circuit-breaker primitive.

    Scope: file-level. Complementary to MissingRetryDetector — retry handles
    transient failure, circuit breakers handle *sustained* failure. Both can
    fire on the same file (the fragilities are distinct).
    """

    name = "missing-circuit-breaker"

    def find(self, code: TargetCodeReader) -> list[Issue]:
        out: list[Issue] = []
        for path in code.list_files("**/*.py"):
            try:
                text = code.read_file(path)
            except Exception:
                continue
            if _BREAKER_HINTS.search(text):
                continue
            for line_num, line in enumerate(text.splitlines(), start=1):
                m = _EXTERNAL_DEP_CALL.search(line)
                if not m:
                    continue
                stripped = line.lstrip()
                if stripped.startswith(("import ", "from ", "#")):
                    continue
                out.append(
                    Issue(
                        file=path,
                        line=line_num,
                        snippet=line.strip(),
                        detail=m.group(0),
                    )
                )
                break  # one finding per file
        return out


# --------------------------------------------------------------------------- #
# No-fallback-for-cache                                                       #
# --------------------------------------------------------------------------- #


# Cache library detection (file-level gate). One of these must match for the
# detector to consider the file at all.
_CACHE_IMPORT_OR_INSTANCE = re.compile(
    r"\b(?:"
    r"import\s+(?:redis|valkey|memcache|aiocache)|"
    r"from\s+(?:redis|valkey|memcache|aiocache)\b"
    r")"
)
# Cache CLIENT instantiation: capture the LHS variable so we can match its
# subsequent ``.get(...)`` calls precisely (avoiding e.g. dict.get noise).
_CACHE_CLIENT_ASSIGN = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*=\s*"
    r"(?:redis\.Redis|valkey\.Valkey|memcache\.Client|aiocache\.\w+)\s*\(",
    re.MULTILINE,
)
# Direct module-prefixed calls always count regardless of variable name:
#     redis.Redis().get(...)
#     valkey.Valkey().hget(...)
_DIRECT_CACHE_CALL = re.compile(
    r"\b(?:redis|valkey|memcache|memcached|aiocache)\b[\w.]*"
    r"\.(?:get|hget|hgetall|mget|exists|fetch)\s*\("
)
# GET-shaped call on a known cache variable.
_GETOPS = "(?:get|hget|hgetall|mget|exists|fetch)"
# Coarse signal that the file has at least *some* error handling.
_HAS_TRY = re.compile(r"^\s*try\s*:", re.MULTILINE)


class NoFallbackForCacheDetector:
    """Find files that read from a cache with no ``try/except`` anywhere.

    Two-pass design to suppress ``dict.get`` noise:

        1. File-level gate: file must either import a cache module OR
           instantiate a cache client (binding to a variable we capture).
        2. Line-level match: ``.get/.hget/...`` is flagged only when invoked
           on (a) a module path like ``redis.Redis().get(...)`` or (b) a
           variable we saw bound to a cache client.

    Files that import the cache lib but never instantiate a client still pass
    the file gate; in that case we fall back to module-prefixed direct calls.
    """

    name = "no-fallback-for-cache"

    def find(self, code: TargetCodeReader) -> list[Issue]:
        out: list[Issue] = []
        for path in code.list_files("**/*.py"):
            try:
                text = code.read_file(path)
            except Exception:
                continue
            client_vars = {m.group(1) for m in _CACHE_CLIENT_ASSIGN.finditer(text)}
            if not (client_vars or _CACHE_IMPORT_OR_INSTANCE.search(text)):
                continue
            if _HAS_TRY.search(text):
                continue
            # Precompile a per-file regex for the known client variables — kept
            # local so we don't build it for files with no client variables.
            var_call_re: re.Pattern[str] | None = None
            if client_vars:
                alt = "|".join(re.escape(v) for v in client_vars)
                var_call_re = re.compile(rf"\b({alt})\.{_GETOPS}\s*\(")
            for line_num, line in enumerate(text.splitlines(), start=1):
                stripped = line.lstrip()
                if stripped.startswith(("import ", "from ", "#")):
                    continue
                m_direct = _DIRECT_CACHE_CALL.search(line)
                m_var = var_call_re.search(line) if var_call_re else None
                match = m_direct or m_var
                if match is None:
                    continue
                detail = match.group(0)
                out.append(
                    Issue(
                        file=path,
                        line=line_num,
                        snippet=line.strip(),
                        detail=detail,
                    )
                )
                break  # one finding per file
        return out
