"""
Reference plugin: an in-memory key-value "application" under chaos.

This is the canonical, dependency-free example of the ExperimentPlugin
contract. It stands up a toy app (a dict), prefills it, drives a workload that
may corrupt data, guards an invariant during the run, and validates the
result — exercising every hook including the failure path. Swap the dict for
your real app (kubectl apply, HTTP seeding, DB fixtures) and the shape is the
same.

Config (``plan.plugin_config``)
-------------------------------
seed_keys:        list[str]  keys to prefill (default: k0..k4)
inject_data_loss: bool       if true, run_test deletes a seeded key → verify fails
guard_min_keys:   int        guard trips if the store drops below this (default: 0)
"""

from __future__ import annotations

from typing import Any

from plugins.base import (
    ExperimentPlugin,
    GuardSample,
    PluginContext,
    SteadyStateGuard,
)
from plugins.registry import register_plugin
from shared.contracts import StatisticalSample, VerifyFailure, VerifyResult


@register_plugin
class KeyValueScenario(ExperimentPlugin):
    name = "example-keyvalue"

    # --- validation --------------------------------------------------------
    async def validate(self, ctx: PluginContext) -> None:
        guard_min = ctx.config.get("guard_min_keys", 0)
        if not isinstance(guard_min, int) or guard_min < 0:
            raise ValueError(f"guard_min_keys must be a non-negative int, got {guard_min!r}")

    # --- env scope ---------------------------------------------------------
    async def provision_env(self, ctx: PluginContext) -> None:
        # Stand up the "application". A real plugin would deploy/connect here.
        ctx.env["store"] = {}
        ctx.log.info("provisioned in-memory KV store")

    async def await_ready(self, ctx: PluginContext) -> None:
        # A real plugin would poll a health endpoint until ready.
        assert ctx.env.get("store") is not None, "store not provisioned"

    async def seed(self, ctx: PluginContext) -> None:
        keys = ctx.config.get("seed_keys") or [f"k{i}" for i in range(5)]
        store: dict[str, str] = ctx.env["store"]
        for k in keys:
            store[k] = f"value-of-{k}"
        ctx.scratch["seeded_keys"] = list(keys)
        # Compensation: remove exactly what we seeded, even if a later stage
        # throws. (teardown_env clears everything anyway; this shows the
        # fine-grained primitive.)
        async def _unseed() -> None:
            for k in keys:
                store.pop(k, None)

        ctx.defer(_unseed, name="unseed")
        ctx.log.info("seeded %d keys", len(keys))

    # --- test scope --------------------------------------------------------
    async def setup_test(self, ctx: PluginContext) -> None:
        # Record the invariant this test will check: all seeded keys present.
        ctx.test["expected_keys"] = list(ctx.scratch["seeded_keys"])

    async def capture_baseline(self, ctx: PluginContext) -> list[StatisticalSample]:
        size = float(len(ctx.env["store"]))
        return [StatisticalSample.from_samples("kv_store_size", [size])]

    async def run_test(self, ctx: PluginContext) -> None:
        # Drive the workload concurrently with fault injection. With
        # inject_data_loss set, simulate the bug the experiment is hunting:
        # a seeded key goes missing under fault.
        if ctx.config.get("inject_data_loss"):
            store: dict[str, str] = ctx.env["store"]
            victims = ctx.scratch.get("seeded_keys") or []
            if victims:
                lost = victims[0]
                store.pop(lost, None)
                ctx.scratch["lost_key"] = lost
                ctx.log.warning("workload lost key %s under fault", lost)

    def steady_state_guard(self, ctx: PluginContext) -> SteadyStateGuard | None:
        guard_min = int(ctx.config.get("guard_min_keys", 0))
        if guard_min <= 0:
            return None

        async def _check(c: PluginContext) -> GuardSample:
            size = len(c.env["store"])
            return GuardSample(
                healthy=size >= guard_min,
                detail=f"store has {size} keys, floor is {guard_min}",
            )

        return SteadyStateGuard(name="kv_min_keys", check=_check, interval_s=0.01)

    async def verify(self, ctx: PluginContext) -> VerifyResult:
        store: dict[str, str] = ctx.env["store"]
        expected: list[str] = ctx.test["expected_keys"]
        missing = [k for k in expected if k not in store]
        if not missing:
            return VerifyResult(
                passed=True,
                summary=f"all {len(expected)} seeded keys present after fault",
            )
        return VerifyResult(
            passed=False,
            summary=f"{len(missing)} seeded key(s) lost under fault",
            failures=[
                VerifyFailure(
                    assertion="seeded key survives fault",
                    expected=f"key {k!r} present",
                    actual=f"key {k!r} missing",
                    evidence={"key": k},
                )
                for k in missing
            ],
        )

    async def collect_diagnostics(self, ctx: PluginContext) -> dict[str, Any]:
        store: dict[str, str] = ctx.env["store"]
        return {
            "store_size": len(store),
            "present_keys": sorted(store),
            "lost_key": ctx.scratch.get("lost_key"),
        }

    # --- teardown (guaranteed) --------------------------------------------
    async def teardown_test(self, ctx: PluginContext) -> None:
        ctx.test.pop("expected_keys", None)

    async def teardown_env(self, ctx: PluginContext) -> None:
        store = ctx.env.pop("store", None)
        if store is not None:
            store.clear()
        ctx.log.info("tore down KV store")
