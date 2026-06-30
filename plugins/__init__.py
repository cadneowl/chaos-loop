"""
Experiment plugins — customer-supplied lifecycle hooks.

A plugin owns the parts of an experiment that need intimate knowledge of the
target application: provisioning the environment, prefilling data, arranging a
specific test, custom success/failure validation, and tearing everything down
again. The orchestrator owns the deterministic state machine and safety gates;
the plugin owns the app-specific scaffolding around it.

See ``plugins/base.py`` for the hook contract, ``plugins/host.py`` for the
lifecycle host (which guarantees teardown), and ``plugins/registry.py`` for
discovery (entry points + a local directory).
"""

from __future__ import annotations

from plugins.base import (
    ExperimentPlugin,
    GuardSample,
    PluginContext,
    SteadyStateGuard,
)
from plugins.registry import (
    available_plugins,
    discover_plugins,
    load_plugin,
    register_plugin,
)

__all__ = [
    "ExperimentPlugin",
    "GuardSample",
    "PluginContext",
    "SteadyStateGuard",
    "available_plugins",
    "discover_plugins",
    "load_plugin",
    "register_plugin",
]
