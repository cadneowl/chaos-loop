# scripts — human-facing operational scripts

Every script here is **idempotent** (safe to re-run) and **noisy** (tells you what it's doing).

Bash variants for macOS / Linux / WSL; PowerShell variants for native Windows. Pick the file ending matching your shell.

## Quick reference

| Script | What it does | When to run |
|---|---|---|
| `install` | Install everything: Python deps, kind cluster, Chaos Mesh, observability stack, security tools, target app | Once, after cloning |
| `start` | Bring up cluster (if down), port-forward observability + target | Start of a session |
| `stop` | Stop port-forwards; leave cluster running | End of a session, keep state |
| `doctor` | Diagnose: what's installed, what's running, what's missing | When something feels off |
| `run-experiment` | Activate venv and run an experiment YAML | Every time you experiment |
| `abort` | Halt a running experiment, clean up Chaos Mesh CRDs | If an experiment misbehaves |
| `clean` | Destroy cluster, venv, run history. **Nuclear.** | When you want a fresh slate |

## Typical session

```bash
# First time (mac/linux/WSL)
bash scripts/install.sh

# Every session
bash scripts/start.sh
bash scripts/run-experiment.sh experiments/examples/01-redis-network-loss.yaml --dry-run
bash scripts/stop.sh

# When something's off
bash scripts/doctor.sh

# Full reset
bash scripts/clean.sh
```

```powershell
# Windows equivalents
powershell -File scripts\install.ps1
powershell -File scripts\start.ps1
powershell -File scripts\run-experiment.ps1 experiments\examples\01-redis-network-loss.yaml --dry-run
powershell -File scripts\stop.ps1
powershell -File scripts\doctor.ps1
powershell -File scripts\clean.ps1
```

## Prerequisites these scripts assume

- `docker` (Docker Desktop, Rancher Desktop, or docker-ce) running
- `kubectl` on PATH
- `kind` on PATH (will install via these scripts if missing)
- `helm` on PATH
- Python 3.11+
- `uv` (preferred) or `pip`
- `gh` CLI (only needed for milestone 6+, the fixer's PR creation)

`scripts/doctor.sh` and `scripts/doctor.ps1` will tell you which of these are missing.

## What scripts do NOT do

- Open browsers / IDEs
- Modify your shell rc files
- Push to remote registries
- Touch anything outside the repo + the `kind-chaos` cluster + `$HOME/.local/share/chaos/` (the SQLite db)
