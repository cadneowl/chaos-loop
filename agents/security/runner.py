"""
ScannerRunner — abstracts "invoke a security CLI with args, get back stdout".

Real scanners (Trivy, Syft, Grype, gitleaks, cosign, kubescape) all expose the
same shape: invoke the binary, pass JSON-output flags, parse the result. We
abstract the invocation so tests can drive parsers with canned bytes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class ScannerError(RuntimeError):
    """Raised when a scanner exits non-zero or its output can't be parsed."""


@dataclass(frozen=True)
class ScannerResult:
    """One scanner invocation's outcome."""

    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


class ScannerRunner(Protocol):
    """Run a scanner CLI; return its result."""

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        stdin: str | None = None,
    ) -> ScannerResult: ...


# ---------------------------------------------------------------------------- #
# Real subprocess runner                                                       #
# ---------------------------------------------------------------------------- #


class SubprocessRunner:
    """Production runner. Uses asyncio.create_subprocess_exec — no shell, no injection risk."""

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        stdin: str | None = None,
    ) -> ScannerResult:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin.encode("utf-8") if stdin else None),
                timeout=timeout_seconds,
            )
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise ScannerError(
                f"scanner timed out after {timeout_seconds}s: {' '.join(args)}"
            ) from e
        return ScannerResult(
            args=tuple(args),
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            returncode=proc.returncode if proc.returncode is not None else -1,
        )


# ---------------------------------------------------------------------------- #
# Fixture runner (tests)                                                       #
# ---------------------------------------------------------------------------- #


class FixtureRunner:
    """
    Tests register canned results keyed by the first arg (the binary name) or by
    a tuple matching the full args. Lookup tries the most specific match first.
    """

    def __init__(self) -> None:
        self._exact: dict[tuple[str, ...], ScannerResult] = {}
        self._by_prog: dict[str, ScannerResult] = {}
        self.calls: list[tuple[str, ...]] = []

    def register(
        self,
        args: Sequence[str] | str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        result = ScannerResult(
            args=tuple(args) if not isinstance(args, str) else (args,),
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
        )
        if isinstance(args, str):
            self._by_prog[args] = result
        else:
            self._exact[tuple(args)] = result

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        stdin: str | None = None,
    ) -> ScannerResult:
        key = tuple(args)
        self.calls.append(key)
        if key in self._exact:
            return self._exact[key]
        if key and key[0] in self._by_prog:
            return self._by_prog[key[0]]
        raise ScannerError(f"no fixture registered for: {' '.join(args)}")
