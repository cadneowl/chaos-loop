"""Live round-trip test for KubernetesClusterIO against a real cluster.

Creates a PodChaos resource targeting a label that matches no pods (so no
chaos actually fires), exercises every ClusterIO method, then deletes it.

Usage:
    KUBECONFIG=~/.kube/config-kind \
    python scripts/smoke_kubernetes_cluster_io.py --context kind-chaos-dev

This is NOT a pytest test — it requires a live cluster + Chaos Mesh and is
meant for manual / CI-with-cluster validation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.chaos.cluster import KubernetesClusterIO

API = "chaos-mesh.org/v1alpha1"
KIND = "PodChaos"
EXP_LABEL = "chaos.kosta.dev/experiment-id"


def _crd(name: str, namespace: str, experiment_id: str) -> dict:
    return {
        "apiVersion": API,
        "kind": KIND,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {EXP_LABEL: experiment_id},
        },
        "spec": {
            "action": "pod-kill",
            "mode": "one",
            "selector": {
                "namespaces": [namespace],
                "labelSelectors": {"app": "doesnotexist"},
            },
            "gracePeriod": 0,
        },
    }


async def run(namespace: str, context: str, experiment_id: str) -> int:
    cluster = KubernetesClusterIO(context=context)
    name = f"smoke-{experiment_id[-8:]}"
    body = _crd(name, namespace, experiment_id)

    print("1) apply...")
    applied = await cluster.apply(body)
    uid = applied["metadata"].get("uid", "(no uid)")
    print(f"   created {applied['kind']}/{applied['metadata']['name']} uid={uid}")

    print("2) get...")
    got = await cluster.get(API, KIND, name, namespace)
    assert got is not None, "get returned None for resource we just created"
    print(f"   got kind={got['kind']} name={got['metadata']['name']}")

    print("3) list_by_labels...")
    listed = await cluster.list_by_labels(API, KIND, namespace, {EXP_LABEL: experiment_id})
    print(f"   found {len(listed)} resource(s) with label {EXP_LABEL}={experiment_id}")
    assert len(listed) >= 1, "list_by_labels missed our resource"

    print("4) apply again (idempotency via 409 -> replace)...")
    re_applied = await cluster.apply(body)
    print(f"   re-applied resourceVersion={re_applied['metadata'].get('resourceVersion')}")

    print("5) delete...")
    deleted = await cluster.delete(API, KIND, name, namespace)
    print(f"   deleted={deleted}")
    assert deleted is True

    print("6) get after delete (expect None)...")
    after = await cluster.get(API, KIND, name, namespace)
    print(f"   after_delete={after}")
    assert after is None

    print("7) delete again (idempotency, expect False)...")
    redeleted = await cluster.delete(API, KIND, name, namespace)
    print(f"   redeleted={redeleted}")
    assert redeleted is False

    print("\nAll 7 steps passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--context", default="kind-chaos-dev")
    parser.add_argument("--experiment-id", default="exp-smoke0001")
    args = parser.parse_args()
    return asyncio.run(run(args.namespace, args.context, args.experiment_id))


if __name__ == "__main__":
    sys.exit(main())
