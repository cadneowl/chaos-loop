"""Trivy wrapper: image vulnerability + misconfiguration scan."""

from __future__ import annotations

from shared.contracts import SecurityFinding


async def run(image: str) -> list[SecurityFinding]:
    # TODO: `trivy image --format json --severity CRITICAL,HIGH,MEDIUM <image>`
    # Parse Vulnerabilities + Misconfigurations.
    raise NotImplementedError("milestone-4")
