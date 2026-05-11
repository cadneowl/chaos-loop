#!/usr/bin/env bash
# Close fixer PRs older than N days (default 14) that aren't merged.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"

REPO="${1:?usage: cleanup-stale.sh <owner/repo> [days]}"
DAYS="${2:-14}"

command -v gh >/dev/null || { echo "gh CLI required"; exit 1; }

gh pr list --repo "${REPO}" --label chaos-fixer-proposal --state open --json number,createdAt,title \
  | python -c "
import json,sys,datetime
prs = json.load(sys.stdin)
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=${DAYS})
for pr in prs:
    created = datetime.datetime.fromisoformat(pr['createdAt'].replace('Z','+00:00'))
    if created < cutoff:
        print(f\"stale: #{pr['number']} {pr['title']}\")
"
