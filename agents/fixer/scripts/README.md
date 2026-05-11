# fixer/scripts — developer scripts

| Script | Purpose |
|---|---|
| `propose.sh` | Run the fixer against a diagnosis (by experiment id), write proposed patch to disk — no PR |
| `dry-pr.sh` | Render the PR body that would be opened, to stdout |
| `open-pr.sh` | Take a proposal artifact and actually open a draft PR on a test fork |
| `check-denylist.sh` | Validate a path against the denylist |
| `cleanup-stale.sh` | Close stale fixer PRs (>14 days old) |
