"""Render every fault we have a renderer for and dry-run-apply it against a
live Chaos Mesh installation. Catches schema drift between our renderer and
the actual CRD definitions.

Usage:
    python scripts/validate_renderers.py [--namespace default] [--context kind-chaos-dev]

Requires `kubectl` on PATH and a kubeconfig context pointing at a cluster
where Chaos Mesh is installed. Each rendered CRD is sent to
``kubectl apply --dry-run=server`` — the API server validates against the
real CRD schema without actually creating the resource.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

# Make ``agents`` / ``shared`` importable when invoked from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.chaos.faults._render import RenderContext
from agents.chaos.faults.registry import RENDERERS, render
from shared.contracts import FaultCategory, FaultSpec

# Sensible per-fault parameter overrides so the rendered CRDs are valid and
# minimally affect the cluster (no actual injection — dry-run only).
_FIXTURES: dict[str, dict] = {
    # FaultSpec requires duration_seconds >= 1; pod.kill is one-shot and
    # the renderer ignores duration, but the contract gate fires anyway.
    "pod.kill": {"target_selector": {"app": "doesnotexist"}, "duration_seconds": 1},
    "pod.failure": {"target_selector": {"app": "doesnotexist"}, "duration_seconds": 30},
    "network.loss": {
        "target_selector": {"app": "doesnotexist"},
        "duration_seconds": 30,
        "parameters": {"loss_percent": 50},
    },
    "network.delay": {
        "target_selector": {"app": "doesnotexist"},
        "duration_seconds": 30,
        "parameters": {"latency_ms": 100, "jitter_ms": 10},
    },
    "network.partition": {
        "target_selector": {"app": "doesnotexist"},
        "duration_seconds": 30,
        "parameters": {"target_selector_other": {"app": "alsodoesnotexist"}},
    },
    "stress.cpu": {
        "target_selector": {"app": "doesnotexist"},
        "duration_seconds": 30,
        "parameters": {"workers": 1, "load_percent": 50},
    },
    "stress.memory": {
        "target_selector": {"app": "doesnotexist"},
        "duration_seconds": 30,
        "parameters": {"workers": 1, "size": "64MB"},
    },
}

_CATEGORY_BY_FAULT: dict[str, FaultCategory] = {
    "pod.kill": FaultCategory.POD,
    "pod.failure": FaultCategory.POD,
    "network.loss": FaultCategory.NETWORK,
    "network.delay": FaultCategory.NETWORK,
    "network.partition": FaultCategory.NETWORK,
    "stress.cpu": FaultCategory.STRESS,
    "stress.memory": FaultCategory.STRESS,
}


def _make_fault(name: str) -> FaultSpec:
    cfg = _FIXTURES[name]
    return FaultSpec(
        name=name,
        category=_CATEGORY_BY_FAULT[name],
        target_selector=cfg["target_selector"],
        duration_seconds=cfg.get("duration_seconds", 0),
        parameters=cfg.get("parameters", {}),
        rationale="renderer-validation",
    )


def _kubectl_dry_run(
    crd: dict, namespace: str, context: str | None, kubectl_cmd: list[str]
) -> tuple[bool, str]:
    args = [
        *kubectl_cmd,
        "apply",
        "--dry-run=server",
        "-f",
        "-",
        "-n",
        namespace,
    ]
    if context:
        args.extend(["--context", context])
    proc = subprocess.run(
        args,
        input=json.dumps(crd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--context", default=None)
    parser.add_argument(
        "--kubectl",
        default="kubectl",
        help=(
            "kubectl invocation prefix (space-separated). "
            "Example for WSL: 'wsl -d Ubuntu-24.04 -- /home/cad/.local/bin/kubectl'"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    import shlex
    # shlex.split handles paths with spaces / quoted args; plain .split() did not.
    kubectl_cmd = shlex.split(args.kubectl)

    ctx = RenderContext(namespace=args.namespace, experiment_id="exp-validate0001")
    failures: list[tuple[str, str]] = []

    for fault_name in sorted(RENDERERS):
        fault = _make_fault(fault_name)
        crd = render(fault, ctx)
        ok, msg = _kubectl_dry_run(crd, args.namespace, args.context, kubectl_cmd)
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {fault_name:24}  {msg.splitlines()[0] if msg else ''}")
        if not ok:
            failures.append((fault_name, msg))

    if failures:
        print(f"\n{len(failures)} renderer(s) failed:")
        for name, msg in failures:
            print(f"\n--- {name} ---\n{msg}")
        return 1
    print(f"\nAll {len(RENDERERS)} renderers validated against live Chaos Mesh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
