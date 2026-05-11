"""gitleaks wrapper. Scans target repo and chaos-induced log dumps for leaked secrets."""

from __future__ import annotations

from shared.contracts import SecurityFinding


async def run(path: str) -> list[SecurityFinding]:
    # TODO: `gitleaks detect --source <path> --report-format json --report-path -`
    raise NotImplementedError("milestone-4")
