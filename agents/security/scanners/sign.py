"""cosign wrapper. Verifies Sigstore signatures on container images."""

from __future__ import annotations

from shared.contracts import SecurityFinding


async def run(image: str, *, public_key: str | None = None) -> list[SecurityFinding]:
    # TODO: `cosign verify --key <pk> <image>` (or keyless with --certificate-identity)
    # Emit a CRITICAL finding when an unsigned image is in use.
    raise NotImplementedError("milestone-4")
