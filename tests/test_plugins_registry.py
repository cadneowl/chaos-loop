"""Plugin registration & discovery."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from plugins import registry
from plugins.base import ExperimentPlugin
from plugins.registry import (
    PluginError,
    available_plugins,
    discover_plugins,
    load_plugin,
    register_plugin,
)


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot/restore the global registry around each test."""
    saved = dict(registry.PLUGINS)
    saved_discovered = registry._discovered
    yield
    registry.PLUGINS.clear()
    registry.PLUGINS.update(saved)
    registry._discovered = saved_discovered


def test_register_and_load() -> None:
    @register_plugin
    class MyPlugin(ExperimentPlugin):
        name = "my-test-plugin"

    inst = load_plugin("my-test-plugin")
    assert isinstance(inst, MyPlugin)


def test_register_rejects_non_subclass() -> None:
    class NotAPlugin:
        name = "nope"

    with pytest.raises(PluginError, match="not an ExperimentPlugin subclass"):
        register_plugin(NotAPlugin)  # type: ignore[arg-type]


def test_register_rejects_unnamed() -> None:
    with pytest.raises(PluginError, match="unique class attribute"):

        @register_plugin
        class Bad(ExperimentPlugin):
            pass  # name stays "base"


def test_register_rejects_name_collision() -> None:
    @register_plugin
    class One(ExperimentPlugin):
        name = "dup"

    with pytest.raises(PluginError, match="already registered"):

        @register_plugin
        class Two(ExperimentPlugin):
            name = "dup"


def test_reregistering_same_class_is_noop() -> None:
    @register_plugin
    class Same(ExperimentPlugin):
        name = "same"

    # Re-running the decorator on the same class (e.g. module re-import) is fine.
    assert register_plugin(Same) is Same


def test_load_unknown_raises_with_hint() -> None:
    with pytest.raises(PluginError, match="unknown plugin"):
        load_plugin("does-not-exist")


def test_example_plugin_discovered() -> None:
    # The first-party example is importable via discovery.
    assert "example-keyvalue" in available_plugins()


def test_local_dir_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = tmp_path / "myplugins"
    plugin_dir.mkdir()
    (plugin_dir / "local_scenario.py").write_text(
        textwrap.dedent(
            """
            from plugins.base import ExperimentPlugin
            from plugins.registry import register_plugin

            @register_plugin
            class LocalScenario(ExperimentPlugin):
                name = "local-scenario"
            """
        ),
        encoding="utf-8",
    )
    # An underscore-prefixed module is ignored.
    (plugin_dir / "_private.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("CHAOS_PLUGINS_DIR", str(plugin_dir))
    discover_plugins(force=True)
    assert "local-scenario" in registry.PLUGINS


def test_local_dir_bad_module_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "boom.py").write_text("raise RuntimeError('import boom')\n", encoding="utf-8")
    monkeypatch.setenv("CHAOS_PLUGINS_DIR", str(plugin_dir))
    # Discovery must not raise even if a local module blows up on import.
    discover_plugins(force=True)


def test_missing_local_dir_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAOS_PLUGINS_DIR", "/no/such/dir/anywhere")
    discover_plugins(force=True)  # no crash


def test_discover_is_idempotent_without_force() -> None:
    first = discover_plugins()
    registry._discovered = True
    second = discover_plugins()  # returns immediately
    assert first is second


# --- entry-point mechanism (monkeypatched importlib.metadata) ----------------


class _FakeEntryPoint:
    def __init__(self, name: str, loader: object) -> None:
        self.name = name
        self._loader = loader

    def load(self) -> object:
        if isinstance(self._loader, Exception):
            raise self._loader
        return self._loader


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch, eps: list[_FakeEntryPoint]
) -> None:
    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        assert group == "chaos.plugins"
        return eps

    monkeypatch.setattr(registry.metadata, "entry_points", fake_entry_points)


def test_entry_point_registers_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    class EPPlugin(ExperimentPlugin):
        name = "ep-plugin"

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("ep-plugin", EPPlugin)])
    discover_plugins(force=True)
    assert "ep-plugin" in registry.PLUGINS


def test_entry_point_load_failure_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("broken", RuntimeError("load failed"))]
    )
    discover_plugins(force=True)  # must not raise
    assert "broken" not in registry.PLUGINS


def test_entry_point_non_plugin_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("notaplugin", object())])
    discover_plugins(force=True)
    assert "notaplugin" not in registry.PLUGINS


def test_entry_point_registration_conflict_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry point whose name collides with an already-registered, different
    class is skipped (logged), not fatal."""

    @register_plugin
    class Incumbent(ExperimentPlugin):
        name = "contested"

    class Challenger(ExperimentPlugin):
        name = "contested"

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("contested", Challenger)])
    discover_plugins(force=True)  # must not raise
    assert registry.PLUGINS["contested"] is Incumbent


def test_entry_point_backend_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*, group: str) -> list[_FakeEntryPoint]:
        raise RuntimeError("metadata backend broke")

    monkeypatch.setattr(registry.metadata, "entry_points", boom)
    discover_plugins(force=True)  # must not raise
