"""Security agent. Orchestrates the scanners; emits SecurityReport."""

from __future__ import annotations

from pathlib import Path

import typer

from agents._cli import notice
from shared.contracts import SecurityReport, SecurityRequest

_PROMPT_DIR = Path(__file__).parent / "prompts"


class ClaudeSecurityAgent:
    """Implements `orchestrator.loop.SecurityAgent`.

    Like the chaos agent, most of this agent's work is deterministic: invoke
    scanners, parse JSON results into SecurityFinding, return. The Claude-backed
    cognitive surface is reserved for:
      - Hypothesis generation from SBOM + code reading
      - Triaging findings (deduplication, confidence weighting)
    """

    def __init__(self, *, model: str = "claude-sonnet-4-6") -> None:
        self.model = model

    async def baseline(self, req: SecurityRequest) -> SecurityReport:
        # TODO(milestone-4): orchestrate scanners per req.kind
        #   - scanners.sbom.run(images) -> findings + sbom digest
        #   - scanners.sca.run(sbom) -> CVE findings
        #   - scanners.image.run(images) -> Trivy findings
        #   - scanners.dast.run(endpoints, active=req.enable_active_dast) -> findings
        #   - scanners.secrets.run(repo) -> findings
        #   - scanners.sign.run(images) -> findings
        #   - scanners.posture.run(cluster, namespace) -> findings
        raise NotImplementedError(
            "ClaudeSecurityAgent.baseline is a milestone-4 task; use --dry-run for now"
        )

    async def verify(self, req: SecurityRequest) -> SecurityReport:
        raise NotImplementedError("milestone-4")


# ---------- CLI ----------------------------------------------------------------

app = typer.Typer(help="Security agent — DAST, SBOM/SCA, image, secrets, posture.", no_args_is_help=True)


@app.command()
def baseline(namespace: str = typer.Option("otel-demo", "--namespace")) -> None:
    """Full security baseline scan against a namespace."""
    notice("security", "baseline", "milestone-4.0",
           hint=f"would scan ns={namespace!r}: Syft/Grype/Trivy/ZAP/gitleaks/cosign/kubescape")


@app.command()
def verify(namespace: str = typer.Option("otel-demo", "--namespace")) -> None:
    """Re-run scans, diff against baseline."""
    notice("security", "verify", "milestone-4.4",
           hint=f"would diff against last baseline for ns={namespace!r}")


@app.command()
def drift(namespace: str = typer.Option("otel-demo", "--namespace")) -> None:
    """Cheap SBOM-only drift check."""
    notice("security", "drift", "milestone-4.4",
           hint=f"would SBOM-digest each image in ns={namespace!r}")


@app.command()
def hypothesize(target_repo: str = typer.Option(None, "--target-repo")) -> None:
    """Generate security hypotheses from SBOM + auth code + policies."""
    notice("security", "hypothesize", "milestone-4.5",
           hint=f"would read SBOM + {target_repo} and emit SecurityHypothesis objects")


if __name__ == "__main__":
    app()
