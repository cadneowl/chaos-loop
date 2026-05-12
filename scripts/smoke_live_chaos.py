"""Live integration test: deploy a tiny target, run real chaos against it.

Flow:
    1. Apply a Deployment of nginx (3 replicas) with label app=smoketarget
    2. Wait for all pods Ready
    3. Apply a PodChaos via KubernetesClusterIO that kills one pod (mode=one)
    4. Watch for the pod to actually be killed (count Ready -> 2 -> 3)
    5. Delete the PodChaos
    6. Sweep multi-resource cleanup by label
    7. Tear down Deployment + namespace

This is NOT a pytest test — it requires a live cluster + Chaos Mesh.

Usage:
    python scripts/smoke_live_chaos.py \\
        --context kind-chaos-dev \\
        --kubectl "wsl -d Ubuntu-24.04 -- /home/<user>/.local/bin/kubectl"

The ``--kubectl`` flag is the invocation prefix (shlex-split). Plain
``kubectl`` works when kubectl is on PATH and the kubeconfig is set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.chaos.cluster import KubernetesClusterIO

EXP_LABEL = "chaos.kosta.dev/experiment-id"
APP_LABEL = "smoketarget"


def _deployment(namespace: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "smoketarget", "namespace": namespace, "labels": {"app": APP_LABEL}},
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": APP_LABEL}},
            "template": {
                "metadata": {"labels": {"app": APP_LABEL}},
                "spec": {
                    "containers": [
                        {
                            "name": "nginx",
                            "image": "nginx:1.27-alpine",
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"containerPort": 80}],
                        }
                    ]
                },
            },
        },
    }


def _podchaos(namespace: str, experiment_id: str) -> dict:
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "PodChaos",
        "metadata": {
            "name": f"smoke-{experiment_id[-8:]}",
            "namespace": namespace,
            "labels": {EXP_LABEL: experiment_id, "chaos.kosta.dev/fault-name": "pod.kill"},
        },
        "spec": {
            "action": "pod-kill",
            "mode": "one",
            "selector": {"namespaces": [namespace], "labelSelectors": {"app": APP_LABEL}},
            "gracePeriod": 0,
        },
    }


def _kubectl(
    args: list[str],
    context: str,
    kubectl_cmd: list[str],
    input_: str | None = None,
) -> tuple[int, str]:
    cmd = [*kubectl_cmd, "--context", context, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, input=input_, timeout=60)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _wait_for_ready_pods(
    namespace: str,
    expected: int,
    context: str,
    kubectl_cmd: list[str],
    timeout_s: int = 60,
) -> int:
    deadline = time.time() + timeout_s
    last = -1
    while time.time() < deadline:
        rc, out = _kubectl(
            ["-n", namespace, "get", "pods", "-l", f"app={APP_LABEL}", "-o", "json"],
            context, kubectl_cmd,
        )
        if rc != 0:
            time.sleep(1)
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            time.sleep(1)
            continue
        ready = 0
        for p in data.get("items", []):
            conds = p.get("status", {}).get("conditions", [])
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds):
                ready += 1
        if ready != last:
            print(f"   ready={ready} (target {expected})")
            last = ready
        if ready == expected:
            return ready
        time.sleep(1)
    return last


async def run(namespace: str, context: str, kubectl_cmd: list[str]) -> int:
    cluster = KubernetesClusterIO(context=context)
    experiment_id = f"exp-{uuid.uuid4().hex[:12]}"

    print(f"=== Live chaos smoke test (exp={experiment_id}) ===\n")
    try:
        print(f"0) ensure namespace {namespace!r} exists...")
        _kubectl(["create", "ns", namespace], context, kubectl_cmd)

        print(f"1) deploy 3 nginx pods (app={APP_LABEL})...")
        rc, out = _kubectl(
            ["apply", "-f", "-"], context, kubectl_cmd, input_=json.dumps(_deployment(namespace)),
        )
        if rc != 0:
            print(f"   FAIL: {out}")
            return 1
        print(f"   {out}")

        print("2) wait for 3 Ready pods...")
        ready = _wait_for_ready_pods(namespace, 3, context, kubectl_cmd, timeout_s=120)
        if ready != 3:
            print(f"   FAIL: only {ready}/3 pods became Ready")
            return 1

        print("3) apply PodChaos via KubernetesClusterIO...")
        applied = await cluster.apply(_podchaos(namespace, experiment_id))
        print(f"   applied {applied['kind']}/{applied['metadata']['name']}")

        print("4) wait for ready-count to dip (pod was killed)...")
        dipped = False
        deadline = time.time() + 30
        while time.time() < deadline:
            rc, out = _kubectl(
                ["-n", namespace, "get", "pods", "-l", f"app={APP_LABEL}", "--no-headers"],
                context, kubectl_cmd,
            )
            running = out.count("Running")
            if running < 3:
                dipped = True
                print(f"   observed dip: {running}/3 Running")
                break
            time.sleep(0.5)
        if not dipped:
            print("   WARN: never observed a dip (pod-kill may have completed too fast)")

        print("5) wait for full recovery (3 Ready)...")
        ready_after = _wait_for_ready_pods(namespace, 3, context, kubectl_cmd, timeout_s=60)
        if ready_after != 3:
            print(f"   FAIL: did not recover, only {ready_after}/3 Ready after chaos")
            return 1

        print("6) delete PodChaos (via KubernetesClusterIO)...")
        deleted = await cluster.delete(
            "chaos-mesh.org/v1alpha1", "PodChaos", applied["metadata"]["name"], namespace
        )
        print(f"   deleted={deleted}")

        print("7) cleanup() by label sweep (multi-resource test)...")
        for i in range(2):
            body = _podchaos(namespace, experiment_id)
            body["metadata"]["name"] = f"sweep-{i}-{experiment_id[-8:]}"
            await cluster.apply(body)
        listed = await cluster.list_by_labels(
            "chaos-mesh.org/v1alpha1", "PodChaos", namespace, {EXP_LABEL: experiment_id}
        )
        print(f"   list_by_labels found {len(listed)}")
        if len(listed) != 2:
            print(f"   FAIL: expected 2 swept resources, found {len(listed)}")
            return 1
        for r in listed:
            await cluster.delete(
                r["apiVersion"], r["kind"], r["metadata"]["name"], r["metadata"]["namespace"]
            )
        # Chaos Mesh resources have finalizers — DELETE returns immediately but
        # the resource isn't gone until the controller processes. Poll for up to 30s.
        deadline = time.time() + 30
        remaining: list[dict] = []
        while time.time() < deadline:
            remaining = await cluster.list_by_labels(
                "chaos-mesh.org/v1alpha1", "PodChaos", namespace, {EXP_LABEL: experiment_id}
            )
            if len(remaining) == 0:
                break
            await asyncio.sleep(1)
        if len(remaining) != 0:
            print(f"   FAIL: {len(remaining)} resource(s) survived cleanup after 30s")
            return 1
        print("   cleanup sweep clean")

        print("\nAll steps passed.")
        return 0
    finally:
        print(f"\n=== teardown: deleting Deployment + namespace {namespace!r} ===")
        _kubectl(
            ["-n", namespace, "delete", "deployment", "smoketarget", "--ignore-not-found"],
            context, kubectl_cmd,
        )
        _kubectl(["delete", "ns", namespace, "--ignore-not-found"], context, kubectl_cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="chaos-smoke")
    parser.add_argument("--context", default="kind-chaos-dev")
    parser.add_argument(
        "--kubectl",
        default="kubectl",
        help=(
            "kubectl invocation prefix (shlex-split). "
            "Example for WSL: 'wsl -d Ubuntu-24.04 -- /home/<user>/.local/bin/kubectl'"
        ),
    )
    args = parser.parse_args()
    kubectl_cmd = shlex.split(args.kubectl)
    return asyncio.run(run(args.namespace, args.context, kubectl_cmd))


if __name__ == "__main__":
    sys.exit(main())
