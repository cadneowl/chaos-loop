"""ZAP wrapper. Baseline scan by default; active scan opt-in."""

from __future__ import annotations

from shared.contracts import SecurityFinding


async def run(endpoint_url: str, *, active: bool = False) -> list[SecurityFinding]:
    # TODO: docker run owasp/zap2docker-stable zap-baseline.py -t <url> -J report.json
    # if active: zap-full-scan.py (gated; respect requires_approval at the orchestrator)
    raise NotImplementedError("milestone-4")
