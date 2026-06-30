"""
Reference plugin: a web service under chaos, with a *deployment* lifecycle.

Where ``KeyValueScenario`` is the minimal toy, this is the realistic template.
It mirrors what a customer's plugin actually does against a real target — only
the I/O is faked (``plugins/examples/_fakes.py``) so it runs offline and
deterministically:

    provision_env   -> cluster.apply("web-service", image=..., replicas=2)
    await_ready     -> cluster.wait_ready(...) (polls health until ready)
    seed            -> POST fixture records into the service store
    setup_test      -> snapshot the SLO this test enforces
    capture_baseline-> sample request latency before the fault
    run_test        -> drive a request burst while the fault is injected;
                       optionally trip the service into a degraded mode
    guard           -> abort if the live error rate exceeds the budget
    verify          -> enforce the SLO: p95 latency, error rate, data intact
    collect_diag    -> dump the recent request log + service state on failure
    teardown_*      -> delete the deployment (guaranteed)

Swap ``FakeCluster``/``FakeService`` for ``kubectl`` + ``httpx`` and the shape
is identical.

Config (``plan.plugin_config``)
-------------------------------
image:               str    container image (default: web-service:latest)
replicas:            int    replica count (default: 2)
seed_records:        dict   key->value fixtures (default: 3 sample records)
slo_p95_ms:          float  max acceptable p95 latency (default: 150)
slo_error_rate:      float  max acceptable error rate 0..1 (default: 0.01)
simulate_degradation:bool   if true, run_test degrades the service -> verify fails
guard_max_error_rate:float  trip the run if live error rate exceeds this (0 = off)
request_burst:       int    requests sent during run_test (default: 20)
"""

from __future__ import annotations

from typing import Any

from plugins.base import (
    ExperimentPlugin,
    GuardSample,
    PluginContext,
    SteadyStateGuard,
)
from plugins.examples._fakes import FakeCluster, FakeDeployment
from plugins.registry import register_plugin
from shared.contracts import (
    FindingSeverity,
    StatisticalSample,
    VerifyFailure,
    VerifyResult,
)

_DEPLOYMENT = "web-service"


@register_plugin
class WebServiceScenario(ExperimentPlugin):
    name = "example-web-service"

    # --- validation --------------------------------------------------------
    async def validate(self, ctx: PluginContext) -> None:
        err_rate = float(ctx.config.get("slo_error_rate", 0.01))
        if not 0.0 <= err_rate <= 1.0:
            raise ValueError(f"slo_error_rate must be in [0, 1], got {err_rate}")
        if int(ctx.config.get("replicas", 2)) < 1:
            raise ValueError("replicas must be >= 1")

    # --- env scope ---------------------------------------------------------
    async def provision_env(self, ctx: PluginContext) -> None:
        cluster = FakeCluster()
        dep = await cluster.apply(
            _DEPLOYMENT,
            image=ctx.config.get("image", "web-service:latest"),
            replicas=int(ctx.config.get("replicas", 2)),
        )
        ctx.env["cluster"] = cluster
        ctx.env["dep"] = dep
        ctx.log.info("applied deployment %s (%d replicas)", _DEPLOYMENT, dep.replicas)

    async def await_ready(self, ctx: PluginContext) -> None:
        cluster: FakeCluster = ctx.env["cluster"]
        await cluster.wait_ready(_DEPLOYMENT, max_polls=10)
        ctx.log.info("deployment %s is ready", _DEPLOYMENT)

    async def seed(self, ctx: PluginContext) -> None:
        records: dict[str, str] = ctx.config.get("seed_records") or {
            "record:1": "alpha",
            "record:2": "bravo",
            "record:3": "charlie",
        }
        dep: FakeDeployment = ctx.env["dep"]
        for k, v in records.items():
            dep.service.put(k, v)
        ctx.scratch["seeded"] = dict(records)
        ctx.log.info("seeded %d records", len(records))

    # --- test scope --------------------------------------------------------
    async def setup_test(self, ctx: PluginContext) -> None:
        ctx.test["slo_p95_ms"] = float(ctx.config.get("slo_p95_ms", 150.0))
        ctx.test["slo_error_rate"] = float(ctx.config.get("slo_error_rate", 0.01))

    async def capture_baseline(self, ctx: PluginContext) -> list[StatisticalSample]:
        dep: FakeDeployment = ctx.env["dep"]
        latencies = [dep.service.request("/health").latency_ms for _ in range(10)]
        return [StatisticalSample.from_samples("request_latency_ms", latencies)]

    async def run_test(self, ctx: PluginContext) -> None:
        dep: FakeDeployment = ctx.env["dep"]
        if ctx.config.get("simulate_degradation"):
            dep.service.degrade()
            ctx.log.warning("service degraded under fault")
        burst = int(ctx.config.get("request_burst", 20))
        statuses = [dep.service.request("/api").status for _ in range(burst)]
        ctx.scratch["run_statuses"] = statuses

    def steady_state_guard(self, ctx: PluginContext) -> SteadyStateGuard | None:
        max_err = float(ctx.config.get("guard_max_error_rate", 0.0))
        if max_err <= 0.0:
            return None

        async def _check(c: PluginContext) -> GuardSample:
            dep: FakeDeployment = c.env["dep"]
            log = dep.service.request_log
            recent = log[-10:]
            if not recent:
                return GuardSample(healthy=True, detail="no traffic yet")
            errors = sum(1 for r in recent if r["status"] >= 500)
            rate = errors / len(recent)
            return GuardSample(
                healthy=rate <= max_err,
                detail=f"live error rate {rate:.0%} (budget {max_err:.0%})",
            )

        return SteadyStateGuard(name="error_budget", check=_check, interval_s=0.001)

    # --- validation --------------------------------------------------------
    async def verify(self, ctx: PluginContext) -> VerifyResult:
        dep: FakeDeployment = ctx.env["dep"]
        failures: list[VerifyFailure] = []

        # 1. Latency SLO under load.
        sample = StatisticalSample.from_samples(
            "verify_latency_ms",
            [dep.service.request("/api").latency_ms for _ in range(20)],
        )
        slo_p95 = ctx.test["slo_p95_ms"]
        if sample.p95 > slo_p95:
            failures.append(
                VerifyFailure(
                    assertion="p95 request latency within SLO",
                    expected=f"<= {slo_p95}ms",
                    actual=f"{sample.p95:.1f}ms",
                    severity=FindingSeverity.HIGH,
                    evidence={"p95": sample.p95, "p99": sample.p99},
                )
            )

        # 2. Error-rate SLO across the run burst.
        statuses: list[int] = ctx.scratch.get("run_statuses", [])
        if statuses:
            err_rate = sum(1 for s in statuses if s >= 500) / len(statuses)
            slo_err = ctx.test["slo_error_rate"]
            if err_rate > slo_err:
                failures.append(
                    VerifyFailure(
                        assertion="error rate within SLO",
                        expected=f"<= {slo_err:.0%}",
                        actual=f"{err_rate:.0%}",
                        severity=FindingSeverity.CRITICAL,
                        evidence={"errors": sum(1 for s in statuses if s >= 500)},
                    )
                )

        # 3. Seeded data survived the fault.
        seeded: dict[str, str] = ctx.scratch.get("seeded", {})
        lost = [k for k, v in seeded.items() if dep.service.get(k) != v]
        if lost:
            failures.append(
                VerifyFailure(
                    assertion="seeded records intact",
                    expected=f"{len(seeded)} records present",
                    actual=f"{len(lost)} lost/corrupted",
                    evidence={"lost_keys": lost},
                )
            )

        if failures:
            return VerifyResult(
                passed=False,
                summary=f"{len(failures)} SLO/integrity check(s) failed under fault",
                failures=failures,
                metrics=[sample],
            )
        return VerifyResult(
            passed=True,
            summary="all SLOs held and data intact under fault",
            metrics=[sample],
        )

    async def collect_diagnostics(self, ctx: PluginContext) -> dict[str, Any]:
        dep: FakeDeployment = ctx.env["dep"]
        log = dep.service.request_log
        return {
            "deployment": dep.name,
            "image": dep.image,
            "replicas": dep.replicas,
            "total_requests": len(log),
            "error_count": sum(1 for r in log if r["status"] >= 500),
            "recent_requests": log[-5:],
        }

    # --- teardown (guaranteed) --------------------------------------------
    async def teardown_test(self, ctx: PluginContext) -> None:
        ctx.test.clear()

    async def teardown_env(self, ctx: PluginContext) -> None:
        cluster: FakeCluster | None = ctx.env.get("cluster")
        if cluster is not None:
            await cluster.delete(_DEPLOYMENT)
            ctx.log.info("deleted deployment %s", _DEPLOYMENT)
