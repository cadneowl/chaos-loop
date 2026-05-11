"""Syft wrapper: generate SBOM for a container image."""

from __future__ import annotations

from shared.contracts import SecurityFinding


async def run(image: str) -> tuple[list[SecurityFinding], str]:
    """Returns (findings, sbom_digest). Findings empty for baseline SBOM gen."""
    # TODO: `syft <image> -o spdx-json` -> parse -> store SBOM -> compute sha256
    raise NotImplementedError("milestone-4")
