# Experiment Plugins

> Customer-supplied lifecycle hooks that own the app-specific scaffolding around
> a chaos experiment — provision an environment, prefill data, arrange a test,
> run **custom validation**, and tear everything down — while the orchestrator
> keeps owning the deterministic state machine, safety gates, budget, and the
> fault itself.

**Contents**

1. [When to reach for a plugin](#when-to-reach-for-a-plugin)
2. [The lifecycle](#the-lifecycle)
3. [Quickstart: your first plugin](#quickstart-your-first-plugin)
4. [Hook reference](#hook-reference)
5. [`PluginContext`](#plugincontext)
6. [Custom validation: `VerifyResult`](#custom-validation-verifyresult)
7. [The steady-state guard](#the-steady-state-guard)
8. [Guaranteed teardown & `ctx.defer`](#guaranteed-teardown--ctxdefer)
9. [Discovery & packaging](#discovery--packaging)
10. [Running](#running)
11. [The persisted audit trail](#the-persisted-audit-trail)
12. [Testing your plugin](#testing-your-plugin)
13. [Cookbook](#cookbook)
14. [Reference plugins](#reference-plugins)
15. [Guarantees & limits](#guarantees--limits)
16. [FAQ](#faq)

---

## When to reach for a plugin

| You need…                                   | Use                                   |
| ------------------------------------------- | ------------------------------------- |
| a new *fault* (network/pod/power/…)         | a renderer in `agents/chaos/faults/`  |
| a new *PromQL probe* / steady-state check   | a probe YAML in `agents/tester/probes/` |
| a muted finding                             | `.chaos/suppress.yaml` (see SUPPRESSION.md) |
| **app-specific setup / validation / teardown** | **an experiment plugin** (this doc)   |

A plugin is the right tool when a run needs *intimate knowledge of the target
application* that the generic tester/security agents can't express: deploying
your app, seeding a database, hitting your real endpoints, asserting your SLOs,
and cleaning up afterwards — reliably, even when the run crashes.

## The lifecycle

The plugin host (`plugins/host.py`) walks these hooks in order. ENV-scoped
stages run once and wrap the TEST-scoped stages. **Teardown is guaranteed**: the
teardown hooks and any `ctx.defer` compensations run in reverse on every exit
path — success, an assertion failure, a crash, or an operator abort.

```
validate                  cheap, side-effect-free preconditions; raise to abort
── env scope (once) ───────────────────────────────────────────────
provision_env             stand up infra / deploy the app
await_ready               block until READY, not merely up
seed                      prefill data / fixtures
── test scope ─────────────────────────────────────────────────────
setup_test                arrange this test's preconditions
capture_baseline          measure steady state BEFORE the fault
run_test                  drive the workload while the fault is injected (optional)
  └ steady_state_guard    polled during the run; trips → abort → teardown
verify                    custom pass/fail + structured failure details
  └ collect_diagnostics   on failure, gather evidence BEFORE teardown
teardown_test             (guaranteed)
── end env scope ──────────────────────────────────────────────────
teardown_env              (guaranteed)
```

Every hook has a neutral default — **implement only the ones you need**. A hook
you don't override is recorded as `SKIPPED` in the audit trail.

### Why these stages, and not just "setup → run → verify → teardown"

The minimal six-step spine is missing four things a real harness needs:

* **`await_ready`** — "up" ≠ "ready". Without a readiness gate, `seed`/`setup_test`
  race a half-booted environment. This is the classic flaky-harness bug.
* **`capture_baseline`** — chaos engineering *is* comparing healthy vs. faulted.
  `verify` needs a baseline; samples you return land on `ctx.baseline`.
* **`collect_diagnostics`** — "get failure details" only works if evidence is
  gathered *before* teardown deletes the namespace / powers off the bench.
* **Guaranteed teardown** — the single most important reliability property: a
  failed run must not leak namespaces, seeded rows, or a powered-on bench.

`steady_state_guard` is the safety addition: an invariant polled *while the fault
is live*. Trip it → the run aborts early and goes straight to teardown.

## Quickstart: your first plugin

A realistic plugin against a Kubernetes service, using `kubectl` and `httpx`.
(The repo's bundled examples fake this I/O so they run offline — see
[Reference plugins](#reference-plugins) — but real plugins look like this.)

```python
# my_app/chaos.py
import asyncio
import httpx

from plugins.base import ExperimentPlugin, PluginContext
from plugins.registry import register_plugin
from shared.contracts import StatisticalSample, VerifyResult, VerifyFailure


@register_plugin
class CheckoutScenario(ExperimentPlugin):
    name = "checkout"                       # used in plan.plugin / --plugin

    async def validate(self, ctx: PluginContext) -> None:
        if "base_url" not in ctx.config:
            raise ValueError("plugin_config.base_url is required")

    # ---- env scope -------------------------------------------------------
    async def provision_env(self, ctx: PluginContext) -> None:
        ns = ctx.plan.safety.namespace
        await _sh(f"kubectl apply -n {ns} -f deploy/checkout.yaml")
        ctx.env["namespace"] = ns
        # teardown_env (below) reverses this; runs even if a later stage throws.

    async def await_ready(self, ctx: PluginContext) -> None:
        ns = ctx.env["namespace"]
        await _sh(f"kubectl rollout status -n {ns} deploy/checkout --timeout=120s")

    async def seed(self, ctx: PluginContext) -> None:
        async with httpx.AsyncClient(base_url=ctx.config["base_url"]) as c:
            for sku in ("A1", "B2", "C3"):
                await c.post("/catalog", json={"sku": sku, "stock": 100})
        ctx.scratch["seeded_skus"] = ["A1", "B2", "C3"]

    # ---- test scope ------------------------------------------------------
    async def capture_baseline(self, ctx: PluginContext) -> list[StatisticalSample]:
        latencies = await self._sample_checkout(ctx, n=20)
        return [StatisticalSample.from_samples("checkout_ms", latencies)]

    async def verify(self, ctx: PluginContext) -> VerifyResult:
        latencies = await self._sample_checkout(ctx, n=20)
        sample = StatisticalSample.from_samples("checkout_ms", latencies)
        baseline = ctx.baseline[0].p95 if ctx.baseline else 200.0
        if sample.p95 <= baseline * 3:
            return VerifyResult(passed=True, summary="checkout p95 within 3x baseline")
        return VerifyResult(
            passed=False,
            summary="checkout latency regressed under fault",
            failures=[VerifyFailure(
                assertion="checkout p95 <= 3x baseline",
                expected=f"<= {baseline * 3:.0f}ms",
                actual=f"{sample.p95:.0f}ms",
                evidence={"baseline_p95": baseline, "fault_p95": sample.p95},
            )],
            metrics=[sample],
        )

    async def collect_diagnostics(self, ctx: PluginContext) -> dict:
        ns = ctx.env["namespace"]
        logs = await _sh(f"kubectl logs -n {ns} deploy/checkout --tail=200")
        return {"checkout_logs_tail": logs}

    # ---- teardown (guaranteed) ------------------------------------------
    async def teardown_env(self, ctx: PluginContext) -> None:
        ns = ctx.env.get("namespace")
        if ns:
            await _sh(f"kubectl delete -n {ns} -f deploy/checkout.yaml --ignore-not-found")

    # ---- helpers ---------------------------------------------------------
    async def _sample_checkout(self, ctx, n: int) -> list[float]:
        out = []
        async with httpx.AsyncClient(base_url=ctx.config["base_url"]) as c:
            for _ in range(n):
                r = await c.post("/checkout", json={"sku": "A1"})
                out.append(r.elapsed.total_seconds() * 1000)
        return out


async def _sh(cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"`{cmd}` failed ({proc.returncode}): {out.decode()[:500]}")
    return out.decode()
```

Wire it from a plan and run:

```yaml
# checkout-experiment.yaml
title: "checkout survives redis latency"
target_app: checkout
plugin: checkout
plugin_config:
  base_url: http://checkout.demo.svc.cluster.local
faults:
  - category: network
    name: network.delay
    target_selector: { app: redis }
    parameters: { latency: "200ms" }
    duration_seconds: 30
    rationale: "Does checkout degrade gracefully when Redis is slow?"
safety:
  cluster_context: kind-chaos-dev
  namespace: demo
```

```bash
chaos run checkout-experiment.yaml --profile static
```

## Hook reference

All hooks are `async def hook(self, ctx: PluginContext)`. Override what you need.

| Hook                  | Scope | When                         | Return / effect |
| --------------------- | ----- | ---------------------------- | --------------- |
| `validate`            | —     | first, before any side effect | raise to abort the run cheaply |
| `provision_env`       | env   | once, up front               | stand up infra; register cleanup |
| `await_ready`         | env   | after provision              | block until ready; raise on timeout |
| `seed`                | env   | after ready                  | prefill data/fixtures |
| `setup_test`          | test  | per test                     | arrange preconditions |
| `capture_baseline`    | test  | before the fault             | `list[StatisticalSample]` → `ctx.baseline` |
| `run_test`            | test  | concurrently with injection  | drive the workload (optional) |
| `steady_state_guard`  | test  | polled during the run        | return a `SteadyStateGuard` or `None` |
| `verify`              | test  | after the fault window       | `VerifyResult` (or `None` = no verdict) |
| `collect_diagnostics` | test  | on failure, before teardown  | `dict` of evidence |
| `teardown_test`       | test  | always (reverse)             | reverse `setup_test` |
| `teardown_env`        | env   | always (reverse)             | reverse `provision_env` |

`steady_state_guard` is the one non-`async` hook (it *returns* a guard whose
`check` is async).

## `PluginContext`

Threaded through every hook. Read/write `scratch` and the typed handle dicts to
pass state forward:

| field           | type                      | purpose |
| --------------- | ------------------------- | ------- |
| `experiment_id` | `str`                     | the run's id |
| `plan`          | `ExperimentPlan`          | the full plan (faults, safety, target_app, …) |
| `config`        | `dict[str, Any]`          | `plan.plugin_config` — your scenario's knobs |
| `scratch`       | `dict[str, Any]`          | free-form state bag you own |
| `env`           | `dict[str, Any]`          | env-scoped handles (cluster client, namespace, …) |
| `test`          | `dict[str, Any]`          | test-scoped handles |
| `baseline`      | `list[StatisticalSample]` | whatever `capture_baseline` returned |
| `log`           | `logging.Logger`          | per-plugin logger |
| `defer(fn)`     | method                    | register a compensation (see below) |

## Custom validation: `VerifyResult`

`verify` returns a `VerifyResult` that **augments** the built-in tester/security
verify. Returning `None` (the default) means "no custom verdict."

```python
from shared.contracts import VerifyResult, VerifyFailure, FindingSeverity

# pass
VerifyResult(passed=True, summary="all SLOs held")

# fail, with structured, diagnosable details
VerifyResult(
    passed=False,
    summary="2 SLO breaches under fault",
    failures=[
        VerifyFailure(
            assertion="p95 latency within SLO",
            expected="<= 150ms", actual="930ms",
            severity=FindingSeverity.HIGH,
            evidence={"p95": 930, "trace_id": "abc123"},
        ),
        VerifyFailure(
            assertion="error rate within budget",
            expected="<= 1%", actual="47%",
            severity=FindingSeverity.CRITICAL,
        ),
    ],
)
```

**Semantics (augment, not replace):**

* `passed=False` marks the run **regressed**, even when tester/security are green.
* The invariant `passed=True` *and* non-empty `failures` is rejected at
  construction — a passing result can't carry failures.
* If the regression is detected **only** by the plugin (tester/security steady),
  the orchestrator records the failure and finishes *without* invoking the
  generic diagnostician — the plugin already produced the structured details.
* If tester/security **also** regressed, the normal diagnose → fix path runs and
  your `VerifyResult` rides along in the record.

## The steady-state guard

A safety invariant polled *while the fault is live*. If `check` ever reports
`healthy=False`, the host trips the guard, cancels the injection, issues a
best-effort `chaos.cleanup` (so no live fault is left behind), and aborts the
run with `SLO_BREACH`.

```python
from plugins.base import SteadyStateGuard, GuardSample

def steady_state_guard(self, ctx):
    async def _check(c):
        depth = await _queue_depth(c.env["broker"])
        return GuardSample(healthy=depth < 10_000,
                           detail=f"queue depth {depth}")
    return SteadyStateGuard(name="queue_backpressure", check=_check, interval_s=1.0)
```

Return `None` to disable it. Use it for "don't let it get *dangerous*" limits
(overheating bench, runaway error budget, unbounded queue) — distinct from
`verify`, which is the *post-hoc* verdict.

## Guaranteed teardown & `ctx.defer`

Two complementary mechanisms, both guaranteed to run in reverse on any exit:

1. **Symmetric hooks** — `teardown_test` reverses `setup_test`; `teardown_env`
   reverses `provision_env`. The host registers them the moment each scope opens,
   so they run even if setup throws partway.
2. **`ctx.defer(cleanup)`** — fine-grained: register a compensation the instant
   you create a resource, so it's cleaned up even if a later stage throws.

```python
async def seed(self, ctx):
    row_id = await db.insert(fixture)
    ctx.defer(lambda: db.delete(row_id), name="delete-fixture")   # reversed first
    blob = await storage.put(payload)
    ctx.defer(lambda: storage.rm(blob), name="rm-blob")           # reversed first-er
```

Unwind order is strict LIFO across both mechanisms, test scope before env scope.
Teardown errors are **logged and recorded, never raised** — they can't mask the
primary failure.

## Discovery & packaging

Two sources feed one registry. `chaos plugins list` shows everything discovered.

### 1. Entry points (ship as a package)

Keep app-intimate code in your own repo and declare it:

```toml
# my-app/pyproject.toml
[project.entry-points."chaos.plugins"]
checkout = "my_app.chaos:CheckoutScenario"
```

`pip install my-app` into the same environment as `chaos`, and it's discovered
automatically. This is the recommended path for anything that outlives a quick
experiment.

### 2. Local directory (quick, in-repo)

Drop a `*.py` defining a plugin into `$CHAOS_PLUGINS_DIR` (default
`./chaos_plugins`). Each module is imported; plugins register via
`@register_plugin`. Modules whose names start with `_` are ignored.

```
chaos_plugins/
  checkout.py        # @register_plugin class CheckoutScenario(...): name = "checkout"
```

```bash
export CHAOS_PLUGINS_DIR=./chaos_plugins
chaos plugins list
```

A local plugin whose `name` collides with an already-registered plugin raises at
registration time (no silent shadowing).

## Running

```bash
chaos plugins list                          # discovered plugins + hooks each implements
chaos run plan.yaml --plugin checkout       # --plugin overrides plan.plugin
chaos run plan.yaml                          # uses plan.plugin if set
chaos run plan.yaml --dry-run --plugin example-keyvalue   # mock agents, real plugin
```

In the plan:

```yaml
plugin: checkout
plugin_config:                # arbitrary; arrives as ctx.config
  base_url: http://checkout.demo
  slo_p95_ms: 150
```

## The persisted audit trail

Every run records the plugin's full activity on the `ExperimentRecord` (SQLite +
API JSON), inspectable with `chaos show <id>`:

| field                  | what |
| ---------------------- | ---- |
| `plugin_name`          | the plugin that ran (`None` if none) |
| `plugin_stage_results` | per-stage `StageResult`: `stage`, `status` (`ok`/`failed`/`skipped`), `duration_ms`, `error`, timestamps |
| `verify_result`        | the `VerifyResult` (verdict + structured failures) |
| `plugin_diagnostics`   | whatever `collect_diagnostics` returned |

## Testing your plugin

Two levels. **Unit** — drive the lifecycle directly via `open_session`, no
orchestrator. **Integration** — run through `ExperimentRunner` with fake agents.

### Unit: drive the hooks via the host

```python
import pytest
from plugins.host import open_session
from shared.contracts import ExperimentPlan, FaultSpec, FaultCategory, SafetyConstraints

def _plan(**cfg):
    return ExperimentPlan(
        title="t", target_app="checkout", plugin="checkout", plugin_config=cfg,
        faults=[FaultSpec(category=FaultCategory.NETWORK, name="network.loss",
                          target_selector={"app": "x"}, duration_seconds=1, rationale="r")],
        safety=SafetyConstraints(cluster_context="kind-dev", namespace="demo",
                                 require_namespace_annotation=False),
    )

async def test_checkout_passes_when_healthy():
    plugin = CheckoutScenario()
    async with open_session(_plan(base_url="http://fake"), plugin) as session:
        await session.capture_baseline()
        await session.drive_run(_noop_inject)       # stands in for fault injection
        result = await session.verify()
    assert result.passed
    # session.records holds the per-stage audit; assert teardown ran, etc.

async def _noop_inject():
    return "ok"
```

`async with` guarantees your `teardown_*` hooks run — so a unit test is also your
leak test: assert your fake cluster/db is empty afterwards.

### Integration: through the orchestrator with fake agents

Supply steady tester/security fakes so the *plugin* owns the verdict, then assert
the loop's behavior. See `tests/test_plugin_web_service.py` for the full pattern;
the skeleton:

```python
from orchestrator.loop import Agents, ExperimentRunner

runner = ExperimentRunner(agents=Agents(tester=SteadyTester(), security=CleanSecurity(),
                                        chaos=FakeChaos(), diagnostician=Spy(), fixer=Fake()),
                          store=store, plugin=CheckoutScenario())
record = asyncio.run(runner.run(_plan(base_url="http://fake")))
assert record.verify_result.passed
assert record.state == ExperimentState.RECORDED
```

### Faking the deployment target

`plugins/examples/_fakes.py` provides a deterministic `FakeCluster` /
`FakeService` (apply / wait-ready / delete; seedable store; `degrade()` to
simulate a fault's effect) so example plugins — and your tests — run offline.
Swap them for `kubectl`/`httpx` in production. Reuse them as a template for your
own fakes.

## Cookbook

**Readiness polling with a timeout** (`await_ready`):

```python
async def await_ready(self, ctx):
    for _ in range(60):
        if await _healthz(ctx.config["base_url"]):
            return
        await asyncio.sleep(2)
    raise TimeoutError("service not healthy after 120s")
```

**DB fixtures with per-row compensation** (`seed`):

```python
async def seed(self, ctx):
    for row in FIXTURES:
        rid = await ctx.env["db"].insert(row)
        ctx.defer(lambda r=rid: ctx.env["db"].delete(r), name=f"del-{rid}")
```

**Driving load during the fault** (`run_test`) — runs concurrently with injection:

```python
async def run_test(self, ctx):
    async with httpx.AsyncClient(base_url=ctx.config["base_url"]) as c:
        results = await asyncio.gather(*(c.get("/api") for _ in range(200)),
                                       return_exceptions=True)
    ctx.scratch["statuses"] = [getattr(r, "status_code", 599) for r in results]
```

**Comparing against the baseline** (`verify`):

```python
async def verify(self, ctx):
    now = await self._sample(ctx)
    base = ctx.baseline[0]              # from capture_baseline
    ok = now.p95 <= base.p95 * 3
    return VerifyResult(passed=ok, summary="p95 within 3x baseline" if ok else "p95 regressed")
```

## Reference plugins

Both ship in `plugins/examples/`, are dependency-free, and are discovered out of
the box (`chaos plugins list`):

* **`example-keyvalue`** (`keyvalue_scenario.py`) — the minimal toy: an in-memory
  KV store. Smallest possible illustration of every hook, including the guard and
  a deterministic failure path (`inject_data_loss: true`).
  Plan: `experiments/examples/04-plugin-keyvalue.yaml`.
* **`example-web-service`** (`web_service_scenario.py`) — the realistic template:
  a web service with a *deployment* lifecycle (apply → wait-ready → seed →
  request burst → SLO verify → delete), an error-budget guard, and structured SLO
  failures. Faked I/O, real shape.
  Plan: `experiments/examples/05-plugin-web-service.yaml`.

```bash
chaos run experiments/examples/05-plugin-web-service.yaml --dry-run --plugin example-web-service
# flip simulate_degradation: true in the plan to watch verify fail the run
```

## Guarantees & limits

* **Teardown always runs.** If `provision_env`/`setup_test` fails partway, the
  host unwinds whatever registered and re-raises. Teardown errors are logged and
  recorded, never raised — they can't mask the primary failure.
* **A tripped guard cleans up the fault.** When the guard cancels injection
  mid-flight, the orchestrator issues a best-effort `chaos.cleanup` so a trip
  never leaves a live fault behind, then aborts (`SLO_BREACH`).
* **No plugin → zero change.** With no plugin configured the host is a no-op
  `NullSession`; existing runs behave exactly as before.
* **Cheap safety gates run before provisioning.** The cluster-denylist and
  blast-radius gates run *before* the session opens, so an unsafe plan never
  stands anything up. (The namespace-annotation gate runs *after* provisioning,
  since provisioning may be what creates/annotates the namespace.)
* **One test per env, for now.** A run is one env + one test. Looping N tests
  over one provisioned env is a planned extension; the env/test scope split
  already models it.
* **A failing `provision_env` surfaces as an error** (teardown still runs). It is
  not yet converted into a graceful `ABORTED` record — that's a small follow-up.

## FAQ

**Do I have to implement every hook?** No. Every hook has a no-op default;
implement only what your scenario needs. Unimplemented hooks show as `SKIPPED`.

**My plugin doesn't show in `chaos plugins list`.** Either it isn't installed
(entry point) — `pip install -e .` your package — or it isn't in
`$CHAOS_PLUGINS_DIR`, or its module raised on import (check logs; bad local
modules are skipped, not fatal). Confirm the class is decorated with
`@register_plugin` and sets a unique `name`.

**Can the plugin replace the built-in verify entirely?** Not in this version —
the design is *augment*. The built-in tester/security verify still runs; your
`VerifyResult` adds to the verdict. If only your plugin fails, the generic
diagnostician is skipped (you already have the details).

**How do I clean up a resource created mid-`seed` if a later stage crashes?**
`ctx.defer(cleanup)` the moment you create it. It's guaranteed to run in reverse.

**Where do failure details go?** `verify`'s `VerifyResult.failures` (structured
assertions) and `collect_diagnostics`' dict, both persisted on the record. View
with `chaos show <id>`.
```
