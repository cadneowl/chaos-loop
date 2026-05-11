#!/usr/bin/env bash
# Run an ACTIVE ZAP scan. Intrusive — runs against the target's endpoints.
# Use only on test environments. Requires confirmation.
set -euo pipefail
ENDPOINT="${1:?usage: dast-active.sh <url>}"

echo "ACTIVE DAST will send malicious-looking probes to: ${ENDPOINT}"
read -r -p "Type the target hostname to confirm: " confirm
if [ "$confirm" != "$(echo "$ENDPOINT" | sed -E 's|https?://([^/]+).*|\1|')" ]; then
  echo "hostname mismatch; aborting"; exit 1
fi

mkdir -p .cache/zap
docker run --rm -t -v "$(pwd)/.cache/zap:/zap/wrk:rw" owasp/zap2docker-stable \
  zap-full-scan.py -t "${ENDPOINT}" -J zap-full.json -r zap-full.html
echo "report: .cache/zap/zap-full.html"
