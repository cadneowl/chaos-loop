"""Security agent. Orchestrates the scanners; emits SecurityReport."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import typer

from agents._cli import notice
from agents.security.runner import ScannerError, ScannerRunner, SubprocessRunner
from agents.security.scanners import posture as posture_scan
from agents.security.scanners import sbom as sbom_scan
from agents.security.scanners import sca as sca_scan
from agents.security.scanners import secrets as secrets_scan
from agents.security.scanners import sign as sign_scan
from agents.security.scanners.image import scan_image
from shared.contracts import (
    SecurityFinding,
    SecurityReport,
    SecurityRequest,
)

log = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent / "prompts"

# Return-value type of a scanner call; differs per scanner (Syft returns a
# 3-tuple, the others return list[SecurityFinding]). ``_safe_call`` is generic
# in this so the type checker preserves the per-call return type.
T = TypeVar("T")


@dataclass(frozen=True)
class SecurityScanConfig:
    """Per-experiment toggles for which scanners run.

    Defaults are conservative — only Trivy (image vuln scan) fires unconditionally.
    Other scanners need either their inputs (SBOM path, repo path, namespace) or
    explicit opt-in to avoid silent dependency on binaries that may not be present.
    """

    enable_trivy: bool = True
    enable_syft: bool = False  # SBOM generation; turns on baseline -> verify drift check
    enable_grype: bool = False  # CVE scan; uses SBOM when available, else image
    enable_gitleaks: bool = False  # secret scan over target_repo
    enable_cosign: bool = False  # signature verification (needs a key or keyless config)
    enable_kubescape: bool = False  # cluster posture (needs kubeconfig)
    # cosign trust config (only one mode is used):
    cosign_public_key: str | None = None
    cosign_certificate_identity: str | None = None
    cosign_certificate_oidc_issuer: str | None = None
    # kubescape framework name.
    kubescape_framework: str = "nsa"


@dataclass
class _BaselineState:
    """Per-experiment SBOM digests captured at baseline; used for drift in verify."""

    sbom_digests_by_image: dict[str, str] = field(default_factory=dict)


class ClaudeSecurityAgent:
    """Implements `orchestrator.loop.SecurityAgent`.

    Deterministic scanner orchestration. The Claude-backed cognitive surface
    (security hypothesizer) is gated separately by a SecurityHypothesizer
    Protocol — that lands in M4.5.

    What runs when:
      - Trivy: image vuln + misconfig scan, every image in target_images
      - Syft (optional): SBOM gen per image; digest captured for drift
      - Grype (optional): CVE scan per image (or via stored SBOM when Syft also ran)
      - gitleaks (optional): secret scan over target_repo
      - cosign (optional): signature verification; emits CRITICAL when unsigned
      - kubescape (optional): cluster-posture scan against namespace

    All scanners go through the same ScannerRunner, so a single
    ``runner=SubprocessRunner()`` is enough in production and a single
    ``FixtureRunner`` covers all of them in tests.
    """

    def __init__(
        self,
        *,
        runner: ScannerRunner | None = None,
        config: SecurityScanConfig | None = None,
        skip_on_scanner_error: bool = True,
        model: str = "claude-sonnet-4-6",
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._config = config or SecurityScanConfig()
        # If True, a single scanner failure logs and yields no findings for that
        # input rather than aborting the whole baseline. Production default.
        self._skip_on_scanner_error = skip_on_scanner_error
        # Per-experiment SBOM digests for drift detection. Keyed by experiment_id
        # so concurrent experiments don't cross-contaminate.
        self._baselines: dict[str, _BaselineState] = {}
        self.model = model

    async def baseline(self, req: SecurityRequest) -> SecurityReport:
        """Run the configured scanners and capture SBOM digests for drift."""
        state = _BaselineState()
        all_findings: list[SecurityFinding] = []

        for image in req.target_images:
            all_findings.extend(await self._scan_one_image(image, state, mode="baseline"))

        if self._config.enable_gitleaks and req.target_repo:
            all_findings.extend(await self._scan_repo(req.target_repo))

        if self._config.enable_kubescape:
            all_findings.extend(await self._scan_cluster(req.target_app))

        self._baselines[req.experiment_id] = state
        return SecurityReport(
            request_kind="baseline",
            experiment_id=req.experiment_id,
            findings=all_findings,
            sbom_digest=self._aggregate_digest(state),
            sbom_drift_from_baseline=False,  # by definition false on baseline
        )

    async def verify(self, req: SecurityRequest) -> SecurityReport:
        """Re-run scanners post-chaos and compare SBOM digests to baseline."""
        verify_state = _BaselineState()
        all_findings: list[SecurityFinding] = []

        for image in req.target_images:
            all_findings.extend(await self._scan_one_image(image, verify_state, mode="verify"))

        if self._config.enable_gitleaks and req.target_repo:
            all_findings.extend(await self._scan_repo(req.target_repo))

        if self._config.enable_kubescape:
            all_findings.extend(await self._scan_cluster(req.target_app))

        baseline_state = self._baselines.get(req.experiment_id)
        drift = self._compute_drift(baseline_state, verify_state) if baseline_state else False
        return SecurityReport(
            request_kind="verify",
            experiment_id=req.experiment_id,
            findings=all_findings,
            sbom_digest=self._aggregate_digest(verify_state),
            sbom_drift_from_baseline=drift,
        )

    # ------ scanner dispatch -------------------------------------------------

    async def _scan_one_image(
        self,
        image: str,
        state: _BaselineState,
        *,
        mode: str,
    ) -> list[SecurityFinding]:
        out: list[SecurityFinding] = []
        if self._config.enable_trivy:
            out.extend(await self._safe_call(
                "trivy", image, scan_image(image, runner=self._runner), default=[],
            ))
        if self._config.enable_syft:
            sbom_result = await self._safe_call(
                "syft", image, sbom_scan.generate_sbom(image, runner=self._runner),
                default=([], "", {}),
            )
            sbom_findings, digest, _ = sbom_result
            out.extend(sbom_findings)
            if digest:
                state.sbom_digests_by_image[image] = digest
        if self._config.enable_grype:
            out.extend(await self._safe_call(
                "grype", image, sca_scan.scan_image(image, runner=self._runner),
                default=[],
            ))
        if self._config.enable_cosign:
            out.extend(await self._safe_call(
                "cosign", image,
                sign_scan.verify_image(
                    image,
                    public_key=self._config.cosign_public_key,
                    certificate_identity=self._config.cosign_certificate_identity,
                    certificate_oidc_issuer=self._config.cosign_certificate_oidc_issuer,
                    runner=self._runner,
                ),
                default=[],
            ))
        return out

    async def _scan_repo(self, repo: str) -> list[SecurityFinding]:
        return await self._safe_call(
            "gitleaks", repo, secrets_scan.scan_repo(repo, runner=self._runner),
            default=[],
        )

    async def _scan_cluster(self, namespace: str) -> list[SecurityFinding]:
        return await self._safe_call(
            "kubescape", namespace,
            posture_scan.scan_namespace(
                namespace, framework=self._config.kubescape_framework, runner=self._runner,
            ),
            default=[],
        )

    async def _safe_call(
        self,
        scanner_name: str,
        location: str,
        coro: Awaitable[T],
        *,
        default: T,
    ) -> T:
        """Run a scanner coroutine; swallow ScannerError when configured to.

        Generic in the scanner's return type so the type checker preserves it
        across the call boundary (lists for most scanners, a tuple for Syft).
        """
        try:
            return await coro
        except ScannerError as e:
            if self._skip_on_scanner_error:
                log.warning(
                    "%s scan of %s failed: %s — continuing",
                    scanner_name, location, e,
                )
                return default
            raise

    # ------ drift ------------------------------------------------------------

    @staticmethod
    def _aggregate_digest(state: _BaselineState) -> str | None:
        """Combine per-image SBOM digests into one stable digest for the report.

        Concatenating the per-image digests (sorted) and re-hashing gives a
        single short identifier that's deterministic across runs but changes
        whenever any image's package set shifts.
        """
        if not state.sbom_digests_by_image:
            return None
        joined = "\n".join(
            f"{img}={dig}" for img, dig in sorted(state.sbom_digests_by_image.items())
        )
        return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()

    @staticmethod
    def _compute_drift(baseline: _BaselineState, verify: _BaselineState) -> bool:
        """Return True iff any image's SBOM digest changed between baseline and verify."""
        if not baseline.sbom_digests_by_image:
            return False  # no baseline -> no drift signal
        for image, base_digest in baseline.sbom_digests_by_image.items():
            if verify.sbom_digests_by_image.get(image) != base_digest:
                return True
        return False


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
    target_repo: str | None = typer.Option(
        None, "--target-repo", help="local repo path (enables gitleaks if --gitleaks)",
    ),
    syft: bool = typer.Option(False, "--syft/--no-syft", help="generate SBOM per image"),
    grype: bool = typer.Option(False, "--grype/--no-grype", help="CVE scan per image"),
    gitleaks: bool = typer.Option(
        False, "--gitleaks/--no-gitleaks", help="secret scan over --target-repo",
    ),
    cosign: bool = typer.Option(False, "--cosign/--no-cosign", help="verify image signatures"),
    cosign_key: str | None = typer.Option(None, "--cosign-key", help="path to cosign public key"),
    kubescape: bool = typer.Option(
        False, "--kubescape/--no-kubescape", help="cluster-posture scan over --namespace",
    ),
    kubescape_framework: str = typer.Option(
        "nsa", "--kubescape-framework", help="nsa, mitre, etc.",
    ),
) -> None:
    """Run the configured scanners against the given inputs.

    By default only Trivy fires. Enable the others by flag — each has its own
    binary dependency, so opt-in keeps "default config works without setup."
    """
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

    config = SecurityScanConfig(
        enable_trivy=True,
        enable_syft=syft,
        enable_grype=grype,
        enable_gitleaks=gitleaks,
        enable_cosign=cosign,
        enable_kubescape=kubescape,
        cosign_public_key=cosign_key,
        kubescape_framework=kubescape_framework,
    )
    agent = ClaudeSecurityAgent(config=config)
    req = SecurityRequest(
        kind="baseline",
        experiment_id=f"exp-{uuid4().hex[:12]}",
        target_app=namespace,
        target_repo=target_repo,
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
