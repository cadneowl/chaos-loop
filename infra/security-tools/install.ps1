# Windows counterpart to install.sh. Uses winget where possible.
$ErrorActionPreference = "Stop"

function Ensure-Tool($name, $wingetId) {
    if (Get-Command $name -ErrorAction SilentlyContinue) { return }
    Write-Host "Installing $name via winget..."
    winget install --id $wingetId --silent --accept-source-agreements --accept-package-agreements
}

Ensure-Tool "trivy"     "AquaSec.Trivy"
Ensure-Tool "syft"      "Anchore.Syft"
Ensure-Tool "grype"     "Anchore.Grype"
Ensure-Tool "gitleaks"  "Gitleaks.Gitleaks"
Ensure-Tool "cosign"    "Sigstore.Cosign"
Ensure-Tool "kubescape" "Kubescape.Kubescape"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker is required for ZAP. Install Docker Desktop manually."
}
docker pull owasp/zap2docker-stable

Write-Host "Security tooling installed."
