"""Load probe sets from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from agents.tester.probes._base import Probe


def default_probes_dir() -> Path:
    return Path(__file__).parent


def load_probe_set(path: Path) -> list[Probe]:
    """Parse a probe-set YAML file. Format:

        probes:
          - name: my_probe
            description: ...
            query: <promql>
            mode: instant
            expect:
              kind: value_below
              threshold: 0.5
            metric_name: my_metric
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Probe.model_validate(p) for p in raw.get("probes", [])]


def probes_for_target(target_app: str, *, probes_dir: Path | None = None) -> list[Probe]:
    """Load the default probe set for a target (looks up `<target_app>.yaml`).

    Raises FileNotFoundError if the target has no probe set yet — the caller
    should treat this as a configuration error, not silently skip.
    """
    d = probes_dir or default_probes_dir()
    path = d / f"{target_app}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No probe set for target {target_app!r} at {path}. "
            f"Available: {[p.stem for p in d.glob('*.yaml')]}"
        )
    return load_probe_set(path)
