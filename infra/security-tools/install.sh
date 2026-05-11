#!/usr/bin/env bash
# Install host-side security CLIs. Most are mac/linux; the powershell.ps1 is the Windows counterpart.
set -euo pipefail

# --- Trivy (image / fs scanner) ---
if ! command -v trivy >/dev/null; then
  brew install aquasecurity/trivy/trivy 2>/dev/null || \
    sudo apt-get install -y trivy
fi

# --- Syft (SBOM generator) ---
if ! command -v syft >/dev/null; then
  curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin
fi

# --- Grype (CVE scanner against SBOM) ---
if ! command -v grype >/dev/null; then
  curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /usr/local/bin
fi

# --- gitleaks (secret scanner) ---
if ! command -v gitleaks >/dev/null; then
  brew install gitleaks 2>/dev/null || \
    go install github.com/gitleaks/gitleaks/v8@latest
fi

# --- cosign (signature verification) ---
if ! command -v cosign >/dev/null; then
  brew install cosign 2>/dev/null || \
    curl -sSfLo /usr/local/bin/cosign \
      "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64" && \
    chmod +x /usr/local/bin/cosign
fi

# --- kubescape (k8s posture) ---
if ! command -v kubescape >/dev/null; then
  curl -sSfL https://raw.githubusercontent.com/kubescape/kubescape/master/install.sh | /bin/bash
fi

# --- ZAP runs as a Docker container; just verify Docker exists ---
if ! command -v docker >/dev/null; then
  echo "Docker is required for ZAP; install Docker Desktop or docker-ce." >&2
  exit 1
fi
docker pull owasp/zap2docker-stable

echo "Security tooling installed."
echo "Versions:"
trivy --version | head -1
syft version | head -1
grype version | head -1
gitleaks version
cosign version | head -1
kubescape version
