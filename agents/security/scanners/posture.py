"""kubescape wrapper: cluster-posture scan against NSA / MITRE frameworks.

Reference: https://github.com/kubescape/kubescape

We invoke ``kubescape scan framework <fw> --include-namespaces <ns> --format
json``. The output is a results document with controls / resources /
verdicts; we map each failed-control occurrence to a SecurityFinding.

Default framework: ``nsa`` (NSA Kubernetes Hardening Guidance). Operators
can pass ``framework="mitre"`` or any other supported framework name.

Kubescape's exit code reflects a configurable score threshold and is not
useful for "did the scan run?" — we ignore it as long as we got parseable
JSON on stdout.
"""

from __future__ import annotations

import json
from typing import Any

from agents.security.runner import ScannerError, ScannerRunner, SubprocessRunner
from shared.contracts import FindingSeverity, SecurityFinding

# Kubescape grades controls by an integer 0-9 "severity" field. Map to ours.
_NUM_SEVERITY_MAP = {
    9: FindingSeverity.CRITICAL,
    8: FindingSeverity.CRITICAL,
    7: FindingSeverity.HIGH,
    6: FindingSeverity.HIGH,
    5: FindingSeverity.MEDIUM,
    4: FindingSeverity.MEDIUM,
    3: FindingSeverity.LOW,
    2: FindingSeverity.LOW,
    1: FindingSeverity.INFO,
    0: FindingSeverity.INFO,
}


async def scan_namespace(
    namespace: str,
    *,
    framework: str = "nsa",
    runner: ScannerRunner | None = None,
    timeout_seconds: float = 300.0,
) -> list[SecurityFinding]:
    """Run a posture scan against ``namespace``.

    Requires kubescape to find kubeconfig on its own (``KUBECONFIG`` env var
    or ``~/.kube/config``). We don't pass --kubeconfig to keep the surface
    small.
    """
    runner = runner or SubprocessRunner()
    args = [
        "kubescape",
        "scan",
        "framework",
        framework,
        "--include-namespaces",
        namespace,
        "--format",
        "json",
        "--format-version",
        "v2",
    ]
    result = await runner.run(args, timeout_seconds=timeout_seconds)
    # We intentionally ignore returncode here — kubescape uses it as a policy
    # signal (score-vs-threshold), not a tool-error signal.
    return _parse_kubescape(result.stdout, namespace=namespace, framework=framework)


def _parse_kubescape(stdout: str, *, namespace: str, framework: str) -> list[SecurityFinding]:
    """Parse kubescape JSON v2 output.

    Shape (simplified):
        { "summaryDetails": { "controls": { "<control-id>": {
              "name": "...", "status": {"status": "failed"},
              "severity": 7, "resourcesIDs": [...] } } } }

    We emit one finding per failed control (not per resource), evidence-listing
    the affected resource IDs. This keeps the report compact while still
    pointing reviewers at the right things.
    """
    if not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ScannerError(f"kubescape stdout was not JSON: {e}") from e

    controls_summary: dict[str, Any] = (
        (payload.get("summaryDetails") or {}).get("controls") or {}
    )
    findings: list[SecurityFinding] = []
    for control_id, control in controls_summary.items():
        if not isinstance(control, dict):
            continue
        status = (control.get("status") or {}).get("status", "").lower()
        if status not in ("failed", "warning"):
            continue
        sev_num = control.get("severity") or control.get("scoreFactor") or 0
        try:
            sev = _NUM_SEVERITY_MAP.get(int(sev_num), FindingSeverity.INFO)
        except (TypeError, ValueError):
            sev = FindingSeverity.INFO
        name = control.get("name") or control_id
        # ``resourceIDs`` (note the casing) gives the K8s objects this fired on.
        affected = control.get("resourceIDs") or []
        findings.append(
            SecurityFinding(
                id=f"f-kubescape-{control_id}".lower().replace("_", "-"),
                severity=sev,
                title=f"{control_id}: {name}",
                description=control.get("description") or "",
                scanner="kubescape",
                cve=None,
                evidence={
                    "framework": framework,
                    "control_id": control_id,
                    "affected_resource_ids": list(affected)[:50],  # cap noise
                    "score_factor": sev_num,
                },
                location=f"namespace/{namespace}",
            )
        )
    return findings
