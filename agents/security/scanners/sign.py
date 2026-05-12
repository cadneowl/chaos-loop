"""cosign wrapper: Sigstore signature verification for container images.

Reference: https://docs.sigstore.dev/cosign/

Two modes:
    - keyed: ``cosign verify --key <pubkey> <image>``
    - keyless: ``cosign verify --certificate-identity <id> --certificate-oidc-issuer <iss> <image>``

The "finding" model is inverted vs. other scanners: cosign success ⇒ no
finding (image is signed); cosign failure ⇒ CRITICAL finding (image is
unsigned or signature doesn't match policy).
"""

from __future__ import annotations

from agents.security.runner import ScannerError, ScannerRunner, SubprocessRunner
from shared.contracts import FindingSeverity, SecurityFinding


async def verify_image(
    image: str,
    *,
    public_key: str | None = None,
    certificate_identity: str | None = None,
    certificate_oidc_issuer: str | None = None,
    runner: ScannerRunner | None = None,
    timeout_seconds: float = 60.0,
) -> list[SecurityFinding]:
    """Verify a container image's signature.

    Exactly one of ``public_key`` or the keyless ``certificate_identity`` +
    ``certificate_oidc_issuer`` pair must be set. We don't infer; callers
    must be explicit about the trust model.

    Returns:
        - ``[]`` on successful verification (image is signed and trusted)
        - ``[SecurityFinding(severity=CRITICAL, ...)]`` on failure
    """
    runner = runner or SubprocessRunner()
    args = _build_args(image, public_key, certificate_identity, certificate_oidc_issuer)
    result = await runner.run(args, timeout_seconds=timeout_seconds)
    if result.returncode == 0:
        return []
    # Returncode > 0 means verification failed. This is signal, not error —
    # we surface it as a finding rather than raising. ``ScannerError`` is
    # reserved for genuine tool failures (binary missing, network broken).
    stderr = result.stderr.strip()
    if "executable file not found" in stderr.lower() or "no such file" in stderr.lower():
        raise ScannerError(f"cosign binary not available: {stderr}")
    # FindingId must match ``^f-[0-9a-z\-]{1,64}$`` — sanitize ``image`` (which
    # routinely has ``:`` ``/`` ``.``) and cap the length.
    safe_image = _sanitize_for_id(image)[:48]
    return [
        SecurityFinding(
            id=f"f-cosign-unsigned-{safe_image}",
            severity=FindingSeverity.CRITICAL,
            title=f"image signature verification failed: {image}",
            description=(stderr or result.stdout.strip() or "(no message)")[:400],
            scanner="cosign",
            cve=None,
            evidence={
                "returncode": result.returncode,
                "mode": "keyless" if certificate_identity else "keyed",
            },
            location=image,
        )
    ]


def _sanitize_for_id(value: str) -> str:
    """Collapse any non-[a-z0-9-] character to ``-`` so the result matches FindingId."""
    out = []
    for ch in value.lower():
        out.append(ch if (ch.isalnum() or ch == "-") else "-")
    # Collapse runs of '-' and trim leading/trailing.
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def _build_args(
    image: str,
    public_key: str | None,
    cert_identity: str | None,
    cert_issuer: str | None,
) -> list[str]:
    if public_key:
        return ["cosign", "verify", "--key", public_key, image]
    if cert_identity and cert_issuer:
        return [
            "cosign", "verify",
            "--certificate-identity", cert_identity,
            "--certificate-oidc-issuer", cert_issuer,
            image,
        ]
    raise ValueError(
        "cosign verify needs --public_key OR (--certificate_identity + "
        "--certificate_oidc_issuer); none provided"
    )
