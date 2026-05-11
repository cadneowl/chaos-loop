# chaos/scripts — developer scripts

| Script | Purpose |
|---|---|
| `list-faults.sh` | Print the full fault catalogue with categories and approval requirements |
| `render.sh` | Render one FaultSpec to YAML on stdout (no apply). Useful for debugging |
| `inject.sh` | Apply a single fault from a YAML for `duration_seconds`, then clean up. Bypasses the orchestrator |
| `force-cleanup.sh` | Find and delete every Chaos Mesh CRD in a namespace. Use after a crash |
| `tail-status.sh` | Watch chaos-controller-manager logs |
