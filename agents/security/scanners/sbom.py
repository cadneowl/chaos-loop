"""Syft wrapper: SBOM generation for a container image.

Output shape:
    (findings, digest)

The SBOM itself goes into the digest (a sha256 of the canonical SPDX JSON
bytes), which makes drift detection cheap — two SBOMs with the same digest
have the same package set, so the later code/dep state hasn't shifted.

Findings are always ``[]`` for a successful SBOM generation: an SBOM is an
inventory, not a verdict. Failure (e.g., image pull error) is signalled by a
``ScannerError``.

Reference: https://github.com/anchore/syft
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agents.security.runner import ScannerError, ScannerRunner, SubprocessRunner
from shared.contracts import SecurityFinding


async def generate_sbom(
    image: str,
    *,
    runner: ScannerRunner | None = None,
    timeout_seconds: float = 180.0,
) -> tuple[list[SecurityFinding], str, dict[str, Any]]:
    """Generate an SBOM for ``image``.

    Returns ``(findings, digest, sbom_json)``:
        - ``findings`` is always empty on success; SBOM-gen does not produce
          verdicts on its own.
        - ``digest`` is sha256(canonical-SPDX-JSON-bytes); used for drift.
        - ``sbom_json`` is the parsed SPDX document so callers can persist it
          for later Grype runs (``grype sbom:<path>``).
    """
    runner = runner or SubprocessRunner()
    args = ["syft", image, "-o", "spdx-json", "-q"]
    result = await runner.run(args, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise ScannerError(
            f"syft exit {result.returncode} for {image}: {result.stderr[:200]}"
        )
    try:
        sbom = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ScannerError(f"syft stdout was not JSON: {e}") from e

    digest = _digest_sbom(sbom)
    return [], digest, sbom


def _digest_sbom(sbom: dict[str, Any]) -> str:
    """sha256 of the SBOM's *package list* (not the whole document).

    The full SPDX JSON includes a ``creationInfo.created`` timestamp that
    changes every run; hashing it produces noise. We hash just the package
    set + their versions, which is what actually represents "what's in the
    image." Two images with the same packages → identical digest → no drift.
    """
    packages = sbom.get("packages") or []
    canonical = sorted(
        (str(p.get("name", "")), str(p.get("versionInfo", "")))
        for p in packages
        if isinstance(p, dict)
    )
    payload = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
