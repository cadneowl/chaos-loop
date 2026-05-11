"""Grype wrapper: CVE scan against a stored SBOM."""

from __future__ import annotations

from shared.contracts import SecurityFinding


async def run(sbom_path: str) -> list[SecurityFinding]:
    # TODO: `grype sbom:<path> -o json` -> map each match to SecurityFinding
    raise NotImplementedError("milestone-4")
