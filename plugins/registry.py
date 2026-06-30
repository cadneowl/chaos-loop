"""
Plugin discovery and lookup.

Two tiers, both feeding one registry:

1. **Entry points** (``chaos.plugins`` group) — a customer ships their scenario
   as a pip package and declares it in ``pyproject.toml``::

       [project.entry-points."chaos.plugins"]
       my-app = "my_app.chaos:MyAppScenario"

   This keeps app-intimate code in the customer's own repo.

2. **Local directory** — for quick, in-repo work, drop a ``*.py`` defining a
   plugin into ``$CHAOS_PLUGINS_DIR`` (default ``./chaos_plugins``). Each module
   is imported; plugins register via the ``@register_plugin`` decorator.

First-party example plugins live in ``plugins.examples`` and are imported on
discovery so ``chaos plugins list`` shows them out of the box.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from importlib import metadata
from pathlib import Path

from plugins.base import ExperimentPlugin

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "chaos.plugins"
DEFAULT_LOCAL_DIR = "chaos_plugins"

# name -> plugin class. Populated by register_plugin (decorator / local dir) and
# by discover_plugins (entry points). Class, not instance: the host constructs a
# fresh instance per run so plugins can hold per-run state on self if they wish.
PLUGINS: dict[str, type[ExperimentPlugin]] = {}

_discovered = False


class PluginError(Exception):
    """Raised for unknown plugins or malformed registrations."""


def register_plugin(cls: type[ExperimentPlugin]) -> type[ExperimentPlugin]:
    """Class decorator registering an ``ExperimentPlugin`` subclass by its ``name``.

    Raises if ``name`` is the unset base default or collides with a different
    class already registered under that name (re-registering the *same* class —
    e.g. a module imported twice — is a no-op).
    """
    if not issubclass(cls, ExperimentPlugin):
        raise PluginError(f"{cls!r} is not an ExperimentPlugin subclass")
    name = getattr(cls, "name", None)
    if not name or name == "base":
        raise PluginError(
            f"{cls.__name__} must set a unique class attribute `name` (got {name!r})"
        )
    existing = PLUGINS.get(name)
    if existing is not None and existing is not cls:
        raise PluginError(
            f"plugin name {name!r} already registered to {existing.__name__}; "
            f"refusing to shadow it with {cls.__name__}"
        )
    PLUGINS[name] = cls
    return cls


def _load_entry_points() -> None:
    """Register every plugin advertised under the ``chaos.plugins`` group."""
    try:
        eps = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as e:  # pragma: no cover - importlib backend quirks
        log.warning("entry-point discovery failed: %r", e)
        return
    for ep in eps:
        try:
            obj = ep.load()
        except Exception as e:
            log.warning("failed loading plugin entry point %s: %r", ep.name, e)
            continue
        if isinstance(obj, type) and issubclass(obj, ExperimentPlugin):
            try:
                register_plugin(obj)
            except PluginError as e:
                log.warning("entry point %s did not register: %r", ep.name, e)
        else:
            log.warning(
                "entry point %s resolved to %r, not an ExperimentPlugin subclass",
                ep.name, obj,
            )


def _load_local_dir(directory: Path) -> None:
    """Import every ``*.py`` in ``directory`` so its plugins self-register."""
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod_name = f"chaos_plugins._{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            log.warning("failed importing local plugin %s: %r", path, e)


def discover_plugins(*, force: bool = False) -> dict[str, type[ExperimentPlugin]]:
    """Populate ``PLUGINS`` from all sources. Idempotent unless ``force``.

    Order: first-party examples, then entry points, then the local dir — so a
    local override can't accidentally shadow a packaged plugin of the same name
    (that raises in ``register_plugin``), but local plugins with fresh names are
    always available.
    """
    global _discovered
    if _discovered and not force:
        return PLUGINS

    # First-party examples (best-effort; absence shouldn't break discovery).
    try:
        importlib.import_module("plugins.examples")
    except Exception as e:  # pragma: no cover
        log.debug("no first-party example plugins: %r", e)

    _load_entry_points()

    local = Path(os.environ.get("CHAOS_PLUGINS_DIR", DEFAULT_LOCAL_DIR))
    _load_local_dir(local)

    _discovered = True
    return PLUGINS


def available_plugins() -> list[str]:
    """Sorted names of all discovered plugins."""
    discover_plugins()
    return sorted(PLUGINS)


def load_plugin(name: str) -> ExperimentPlugin:
    """Construct the plugin registered under ``name``.

    Raises ``PluginError`` (with the list of known names) if not found, so the
    orchestrator surfaces a clear configuration error rather than a KeyError.
    """
    discover_plugins()
    cls = PLUGINS.get(name)
    if cls is None:
        raise PluginError(
            f"unknown plugin {name!r}. Known: {sorted(PLUGINS) or '(none)'}. "
            f"Register via the '{ENTRY_POINT_GROUP}' entry-point group or drop a "
            f"module in ${{CHAOS_PLUGINS_DIR:-{DEFAULT_LOCAL_DIR}}}."
        )
    return cls()
