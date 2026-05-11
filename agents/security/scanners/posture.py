"""kubescape wrapper. NSA/MITRE k8s posture scan."""

from __future__ import annotations

from shared.contracts import SecurityFinding


async def run(cluster_context: str, namespace: str) -> list[SecurityFinding]:
    # TODO: `kubescape scan framework nsa -e <ns> --format json`
    raise NotImplementedError("milestone-4")
