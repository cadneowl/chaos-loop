# tester/scripts — developer scripts

These run the tester agent in isolation, outside the orchestrator loop. Useful for iterating on prompts, debugging tool calls, and sanity-checking new probes.

| Script | Purpose |
|---|---|
| `run-baseline.sh` | Invoke the tester in baseline mode against the running target |
| `run-verify.sh` | Invoke the tester in verify mode (requires a prior baseline) |
| `run-hypothesize.sh` | Generate hypotheses from a target repo checkout |
| `replay.sh` | Re-run the tester on a recorded fixture (no live cluster needed) |

All scripts source `.venv/bin/activate` and run from the repo root.
