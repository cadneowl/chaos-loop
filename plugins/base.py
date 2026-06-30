"""
The experiment-plugin contract.

``ExperimentPlugin`` is a base class with no-op defaults for every hook: a
customer overrides only the hooks their scenario needs. The plugin host
(``plugins/host.py``) invokes the hooks in lifecycle order and guarantees the
teardown hooks run on every exit path.

Design notes
------------
* Hooks are ``async`` so a plugin can ``await`` kubectl / HTTP / serial I/O.
* ``PluginContext`` threads state between hooks. It is a runtime object (it
  holds live handles and callables), so it is a dataclass, not a Pydantic
  model. The *serializable* results — ``VerifyResult``, ``StageResult`` — live
  in ``shared.contracts`` and land on the persisted ``ExperimentRecord``.
* ``ctx.defer(...)`` registers a fine-grained compensation that the host
  unwinds in reverse, in addition to the symmetric ``teardown_test`` /
  ``teardown_env`` hooks. Use it for resources created mid-stage (e.g. a row
  seeded in ``seed``) that should be cleaned up even if a later stage throws.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from shared.contracts import ExperimentPlan, StatisticalSample, VerifyResult

if TYPE_CHECKING:
    from shared.contracts import LifecycleStage

# A compensation callback the host runs during teardown.
Compensation = Callable[[], Awaitable[None]]


@dataclass
class GuardSample:
    """One reading from a steady-state guard."""

    healthy: bool
    detail: str = ""


@dataclass
class SteadyStateGuard:
    """A safety invariant polled *while the fault is active*.

    If ``check`` ever reports ``healthy=False`` the host trips the guard, stops
    the run early, and proceeds straight to teardown — the experiment aborts
    rather than letting a dangerous condition (overheating bench, error-budget
    burn) continue. Returning ``None`` from ``steady_state_guard`` disables it.
    """

    name: str
    check: Callable[[PluginContext], Awaitable[GuardSample]]
    interval_s: float = 1.0


@dataclass
class PluginContext:
    """State threaded through every hook of one experiment run.

    Hooks read/write ``scratch`` and the typed handle dicts (``env``, ``test``)
    to pass state forward. ``defer`` registers a compensation; ``baseline``
    stores whatever ``capture_baseline`` returned for ``verify`` to compare
    against.
    """

    experiment_id: str
    plan: ExperimentPlan
    config: dict[str, Any] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)
    env: dict[str, Any] = field(default_factory=dict)
    test: dict[str, Any] = field(default_factory=dict)
    baseline: list[StatisticalSample] = field(default_factory=list)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("plugins"))

    # Injected by the host. Pushes a compensation onto the current scope's
    # teardown stack (test scope while a test is open, else env scope).
    _register: Callable[[Compensation, str], None] | None = field(
        default=None, repr=False
    )

    def defer(self, cleanup: Compensation, *, name: str = "") -> None:
        """Register ``cleanup`` to run during teardown, in reverse order.

        Runs even if a later stage raises. No-op if called outside a host
        session (the context wasn't wired) — which only happens in unit tests
        that construct a bare context.
        """
        if self._register is None:
            self.log.debug("ctx.defer(%s) ignored: no host registered", name)
            return
        self._register(cleanup, name or getattr(cleanup, "__name__", "compensation"))


class ExperimentPlugin:
    """Base class for customer scenarios. Override only the hooks you need.

    Lifecycle order (see ``LifecycleStage``):

        validate
        ── env scope (once) ───────────────────────────────────────
        provision_env  →  await_ready  →  seed
        ── test scope ─────────────────────────────────────────────
        setup_test  →  capture_baseline  →  run_test  →  verify
                                                   └ collect_diagnostics (on fail)
        teardown_test           (guaranteed)
        ── end env scope ──────────────────────────────────────────
        teardown_env            (guaranteed)

    Every hook is a no-op / neutral default. The host records a hook the plugin
    didn't override as ``SKIPPED`` in the audit trail.
    """

    #: Stable identifier used in ``plan.plugin`` and ``chaos plugins list``.
    name: str = "base"

    # --- validation --------------------------------------------------------
    async def validate(self, ctx: PluginContext) -> None:
        """Cheap, side-effect-free checks of config/preconditions. Raise to abort."""

    # --- env scope ---------------------------------------------------------
    async def provision_env(self, ctx: PluginContext) -> None:
        """Stand up infra / deploy the app. Register cleanup via ctx.defer or
        rely on ``teardown_env`` (the host auto-runs it if this hook ran)."""

    async def await_ready(self, ctx: PluginContext) -> None:
        """Block until the env is *ready*, not merely *up*. Raise on timeout."""

    async def seed(self, ctx: PluginContext) -> None:
        """Prefill data / fixtures the test depends on."""

    # --- test scope --------------------------------------------------------
    async def setup_test(self, ctx: PluginContext) -> None:
        """Arrange this test's preconditions."""

    async def capture_baseline(self, ctx: PluginContext) -> list[StatisticalSample]:
        """Measure steady state before the fault. Returned samples land on
        ``ctx.baseline`` for ``verify`` to compare against."""
        return []

    async def run_test(self, ctx: PluginContext) -> None:
        """Drive the workload while the fault is injected (optional override).

        Runs concurrently with the orchestrator's fault injection. Leave as the
        default no-op to let the chaos agent's injection stand alone.
        """

    def steady_state_guard(self, ctx: PluginContext) -> SteadyStateGuard | None:
        """Return a guard polled during the run, or None to disable."""
        return None

    async def verify(self, ctx: PluginContext) -> VerifyResult | None:
        """Custom success/failure validation with structured failure details.

        Returning ``None`` means "no custom verdict" — the run is judged purely
        by the built-in tester/security verify. A returned ``VerifyResult``
        *augments* those: ``passed=False`` marks the run regressed.
        """
        return None

    async def collect_diagnostics(self, ctx: PluginContext) -> dict[str, Any]:
        """Gather evidence on failure, before teardown destroys it."""
        return {}

    # --- teardown (guaranteed) --------------------------------------------
    async def teardown_test(self, ctx: PluginContext) -> None:
        """Reverse ``setup_test``. Runs even if the test failed or crashed."""

    async def teardown_env(self, ctx: PluginContext) -> None:
        """Reverse ``provision_env``. Runs even if everything above failed."""


# The hooks the host treats as "no-op unless overridden", in lifecycle order.
# Used to mark SKIPPED stages and to decide whether to invoke optional hooks.
_HOOK_NAMES: tuple[str, ...] = (
    "validate",
    "provision_env",
    "await_ready",
    "seed",
    "setup_test",
    "capture_baseline",
    "run_test",
    "verify",
    "collect_diagnostics",
    "teardown_test",
    "teardown_env",
)


def class_overrides(cls: type[ExperimentPlugin], hook: str) -> bool:
    """True if ``cls`` provides its own implementation of ``hook``.

    Compares the (possibly inherited) function to the base class's, so a plugin
    that leaves a hook as the inherited no-op is reported as not overriding it.
    Works off the class alone — no instance needed — so callers like
    ``chaos plugins list`` can introspect without constructing the plugin.
    """
    own = getattr(cls, hook, None)
    base = getattr(ExperimentPlugin, hook, None)
    return own is not None and own is not base


def overrides(plugin: ExperimentPlugin, hook: str) -> bool:
    """True if ``plugin`` provides its own implementation of ``hook``.

    Lets the host record SKIPPED and skip optional invocations. Inherited
    overrides (a plugin subclassing another plugin) are detected.
    """
    return class_overrides(type(plugin), hook)


def stage_for(hook: str) -> LifecycleStage:
    """Map a hook name to its ``LifecycleStage`` enum member."""
    from shared.contracts import LifecycleStage

    return LifecycleStage(hook)
