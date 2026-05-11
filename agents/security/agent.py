"""Security agent. Orchestrates the scanners; emits SecurityReport."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from agents._cli import notice
from agents.security.runner import ScannerError, ScannerRunner, SubprocessRunner
from agents.security.scanners.image import scan_image
from shared.contracts import (
    SecurityFinding,
    SecurityReport,
    SecurityRequest,
)

log = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent / "prompts"


class ClaudeSecurityAgent:
    """Implements `orchestrator.loop.SecurityAgent`.

    Most of the work is deterministic: invoke scanners, parse JSON, aggregate.
    The Claude-backed cognitive surface is reserved for hypothesize mode
    (security hypotheses from SBOM + auth code + policies) and is gated by a
    SecurityHypothesizer Protocol — deferred to M4.5.

    M4.2: baseline + verify run Trivy against target_images. Additional scanners
    (Syft / Grype / gitleaks / cosign / kubescape) land in M4.1+.
    """

    def __init__(
        self,
        *,
        runner: ScannerRunner | None = None,
        skip_on_scanner_error: bool = True,
        model: str = "claude-sonnet-4-6",
    ) -> None:
        self._runner = runner or SubprocessRunner()
        # If True, a single scanner failure logs and yields no findings for that
        # image rather than aborting the whole baseline. Production default.
        self._skip_on_scanner_error = skip_on_scanner_error
        self.model = model

    async def baseline(self, req: SecurityRequest) -> SecurityReport:
        """Run the configured scanners against req.target_images."""
        findings = await self._scan_images(req.target_images)
        return SecurityReport(
            request_kind="baseline",
            experiment_id=req.experiment_id,
            findings=findings,
            sbom_digest=None,  # M4.1 wires Syft for SBOM
            sbom_drift_from_baseline=False,
        )

    async def verify(self, req: SecurityRequest) -> SecurityReport:
        """Re-run scanners post-chaos. Same as baseline in M4.2; drift detection lands in M4.4."""
        findings = await self._scan_images(req.target_images)
        return SecurityReport(
            request_kind="verify",
            experiment_id=req.experiment_id,
            findings=findings,
            sbom_digest=None,
            sbom_drift_from_baseline=False,
        )

    async def _scan_images(self, images: list[str]) -> list[SecurityFinding]:
        """Trivy each image; aggregate findings."""
        if not images:
            return []
        all_findings: list[SecurityFinding] = []
        for image in images:
            try:
                all_findings.extend(await scan_image(image, runner=self._runner))
            except ScannerError as e:
                if self._skip_on_scanner_error:
                    log.warning("scan_image(%s) failed: %s — continuing", image, e)
                    continue
                raise
        return all_findings


# ---------- CLI ----------------------------------------------------------------

app = typer.Typer(
    help="Security agent — DAST, SBOM/SCA, image, secrets, posture.", no_args_is_help=True
)


@app.command()
def baseline(
    namespace: str = typer.Option("otel-demo", "--namespace"),
    image: list[str] = typer.Option(
        [], "--image", help="image refs to scan (repeatable)"
    ),
) -> None:
    """Run image scanners (Trivy) against the given images. Image discovery is M4.3."""
    if not image:
        notice(
            "security",
            "baseline",
            "milestone-4.3",
            hint=(
                f"would discover images from ns={namespace!r}; "
                "for now pass --image <ref> repeatedly"
            ),
        )
        return
    import asyncio
    import json as _json
    from uuid import uuid4

    agent = ClaudeSecurityAgent()
    req = SecurityRequest(
        kind="baseline",
        experiment_id=f"exp-{uuid4().hex[:12]}",
        target_app=namespace,
        target_images=image,
    )
    report = asyncio.run(agent.baseline(req))
    typer.echo(_json.dumps(report.model_dump(mode="json"), indent=2))


@app.command()
def verify(namespace: str = typer.Option("otel-demo", "--namespace")) -> None:
    """Re-run scans, diff against baseline. Drift detection is M4.4."""
    notice(
        "security",
        "verify",
        "milestone-4.4",
        hint=f"would diff against last baseline for ns={namespace!r}",
    )


@app.command()
def drift(namespace: str = typer.Option("otel-demo", "--namespace")) -> None:
    """Cheap SBOM-only drift check."""
    notice(
        "security",
        "drift",
        "milestone-4.4",
        hint=f"would SBOM-digest each image in ns={namespace!r}",
    )


@app.command()
def hypothesize(target_repo: str = typer.Option(None, "--target-repo")) -> None:
    """Generate security hypotheses from SBOM + auth code + policies."""
    notice(
        "security",
        "hypothesize",
        "milestone-4.5",
        hint=f"would read SBOM + {target_repo} and emit SecurityHypothesis objects",
    )


if __name__ == "__main__":
    app()
