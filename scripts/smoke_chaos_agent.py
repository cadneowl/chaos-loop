"""Live test of ClaudeChaosAgent.execute() — the agent layer above ClusterIO.

Validates that the agent:
    - Performs the preflight (catalogue + renderer + multi-fault check)
    - Sleeps the quiet windows
    - Applies the CRD
    - Sleeps the fault duration
    - Deletes the CRD
    - Emits a successful ChaosTimeline with the right events
And on a forced failure path:
    - Performs cleanup() via label sweep

Usage:
    KUBECONFIG=~/.kube/config-kind \
    python scripts/smoke_chaos_agent.py --context kind-chaos-dev --namespace chaos-smoke
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.chaos.agent import ClaudeChaosAgent
from agents.chaos.cluster import KubernetesClusterIO
from shared.contracts import (
    ExperimentPlan,
    FaultCategory,
    FaultSpec,
    SafetyConstraints,
)

APP_LABEL = "smoketarget-agent"
EXP_LABEL = "chaos.kosta.dev/experiment-id"


def _deployment(namespace: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "smoketarget-agent", "namespace": namespace, "labels": {"app": APP_LABEL}},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": APP_LABEL}},
            "template": {
                "metadata": {"labels": {"app": APP_LABEL}},
                "spec": {
                    "containers": [
                        {
                            "name": "nginx",
                            "image": "nginx:1.27-alpine",
                            "imagePullPolicy": "IfNotPresent",
                        }
                    ]
                },
            },
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


def _experiment_plan(namespace: str, context: str) -> ExperimentPlan:
    return ExperimentPlan(
        title="agent-smoke-test",
        target_app="smoketarget-agent",
        faults=[
            FaultSpec(
                category=FaultCategory.POD,
                name="pod.kill",
                target_selector={"app": APP_LABEL},
                parameters={"mode": "one"},
                duration_seconds=1,  # pod.kill ignores duration but FaultSpec requires >=1
                rationale="agent smoke test",
            ),
        ],
        safety=SafetyConstraints(
            cluster_context=context,
            namespace=namespace,
            require_namespace_annotation=False,
        ),
        quiet_window_pre_seconds=2,
        quiet_window_post_seconds=2,
    )


async def run(namespace: str, context: str, kubectl_cmd: list[str]) -> int:
    print("=== ChaosAgent smoke test ===\n")

    # Setup namespace + target
    print("0) namespace + target deployment...")
    _kubectl(["create", "ns", namespace], context, kubectl_cmd)
    _kubectl(
        ["apply", "-f", "-"], context, kubectl_cmd, input_=json.dumps(_deployment(namespace))
    )

    # Wait for the deployment's pods to be Ready so the chaos selector matches.
    print("   waiting for pods Ready...")
    for _ in range(60):
        rc, out = _kubectl(
            ["-n", namespace, "get", "deploy", "smoketarget-agent",
             "-o", "jsonpath={.status.readyReplicas}"],
            context, kubectl_cmd,
        )
        if rc == 0 and out.strip() == "2":
            print("   2/2 Ready")
            break
        await asyncio.sleep(1)
    else:
        print("   WARN: timed out waiting for pods Ready")

    try:
        cluster = KubernetesClusterIO(context=context)
        agent = ClaudeChaosAgent(cluster=cluster)
        plan = _experiment_plan(namespace, context)

        print(f"\n1) execute() — plan {plan.experiment_id}")
        print("   quiet_pre=2s, fault=network.delay(5s), quiet_post=2s")
        timeline = await agent.execute(plan)

        print("\n2) inspect timeline:")
        print(f"   success={timeline.success}")
        print(f"   error={timeline.error}")
        for ev in timeline.events:
            print(f"   [{ev.fault_name}] {ev.event}  {ev.detail or ''}")

        if not timeline.success:
            print("\nFAIL: execute() reported success=False")
            return 1

        # Should have: scheduled, started, cleaned-up = 3 events per fault.
        expected_events = {"scheduled", "started", "cleaned-up"}
        observed = {ev.event for ev in timeline.events}
        if not expected_events <= observed:
            print(f"\nFAIL: missing events {expected_events - observed}")
            return 1

        print("\n3) verify PodChaos CR is gone after execute()...")
        import time
        deadline = time.time() + 60
        listed: list[dict] = []
        while time.time() < deadline:
            listed = await cluster.list_by_labels(
                "chaos-mesh.org/v1alpha1",
                "PodChaos",
                namespace,
                {EXP_LABEL: plan.experiment_id},
            )
            if len(listed) == 0:
                break
            await asyncio.sleep(1)
        if len(listed) != 0:
            print(f"   FAIL: {len(listed)} PodChaos survived; expected 0")
            return 1
        print("   clean OK")

        print("\n4) cleanup() path — apply 3 resources, then cleanup() sweeps them all")
        # Use a fresh experiment_id so we don't conflict with the prior plan's
        # already-gone label.
        cleanup_plan = ExperimentPlan(
            title="agent-cleanup-test",
            target_app="smoketarget-agent",
            faults=[
                FaultSpec(
                    category=FaultCategory.POD,
                    name="pod.kill",
                    target_selector={"app": APP_LABEL},
                    parameters={"mode": "one"},
                    duration_seconds=1,
                    rationale="cleanup test",
                ),
            ],
            safety=plan.safety,
        )
        from agents.chaos.faults._render import RenderContext
        from agents.chaos.faults.registry import render as render_fault

        ctx = RenderContext(namespace=namespace, experiment_id=cleanup_plan.experiment_id)
        for i in range(3):
            body = render_fault(cleanup_plan.faults[0], ctx)
            body["metadata"]["name"] = f"cleanup-{i}-{cleanup_plan.experiment_id[-8:]}"
            await cluster.apply(body)
        before = await cluster.list_by_labels(
            "chaos-mesh.org/v1alpha1",
            "PodChaos",
            namespace,
            {EXP_LABEL: cleanup_plan.experiment_id},
        )
        print(f"   apply 3 → list_by_labels found {len(before)}")
        if len(before) != 3:
            print(f"   FAIL: expected 3, found {len(before)}")
            return 1

        print(f"   agent.cleanup({cleanup_plan.experiment_id})...")
        await agent.cleanup(cleanup_plan)

        deadline = time.time() + 60
        while time.time() < deadline:
            after = await cluster.list_by_labels(
                "chaos-mesh.org/v1alpha1",
                "PodChaos",
                namespace,
                {EXP_LABEL: cleanup_plan.experiment_id},
            )
            if len(after) == 0:
                break
            await asyncio.sleep(1)
        if len(after) != 0:
            print(f"   FAIL: {len(after)} resource(s) survived agent.cleanup()")
            return 1
        print("   cleanup sweep clean OK")

        print("\nAll agent-level steps passed.")
        return 0
    finally:
        print("\n=== teardown ===")
        _kubectl(
            ["-n", namespace, "delete", "deployment", "smoketarget-agent", "--ignore-not-found"],
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
            "Example for WSL: 'wsl -d Ubuntu-24.04 -- /home/cad/.local/bin/kubectl'"
        ),
    )
    args = parser.parse_args()
    import shlex
    kubectl_cmd = shlex.split(args.kubectl)
    return asyncio.run(run(args.namespace, args.context, kubectl_cmd))


if __name__ == "__main__":
    sys.exit(main())
