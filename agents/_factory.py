"""
Construct real agents from configuration.

Each agent's constructor takes its backends/strategies as explicit kwargs.
`build_real_agents` wires those defaults from environment variables / CLI flags
and surfaces missing-dep errors as plain ValueError, not deep inside the agent.

The orchestrator's `chaos run` command calls this. Tests can call it directly
with overrides to inject fixtures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agents._harness import Harness
from agents.chaos.agent import ClaudeChaosAgent
from agents.chaos.cluster import ClusterIO
from agents.diagnostician.agent import ClaudeDiagnosticianAgent
from agents.diagnostician.diagnoser import ClaudeDiagnoser, Diagnoser
from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.diagnostician.tools.loki import HttpxLokiBackend, LokiBackend
from agents.fixer.agent import ClaudeFixerAgent
from agents.fixer.strategy import ClaudeFixerStrategy, FixerStrategy
from agents.security.agent import ClaudeSecurityAgent
from agents.security.runner import ScannerRunner, SubprocessRunner
from agents.tester.agent import ClaudeTesterAgent
from agents.tester.tools.prometheus import HttpxPromBackend, PromBackend
from orchestrator.loop import Agents


class AgentConfigError(ValueError):
    """Raised when a required piece of agent configuration is missing."""


@dataclass(frozen=True)
class AgentConfig:
    """Where the agents get their backends from.

    Each field defaults from environment if unset; callers (CLI / tests)
    can override.
    """

    prom_url: str | None = None
    loki_url: str | None = None
    target_repo_path: str | None = None
    kubeconfig: str | None = None

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            prom_url=os.environ.get("PROM_URL"),
            loki_url=os.environ.get("LOKI_URL"),
            target_repo_path=os.environ.get("TARGET_REPO_PATH"),
            kubeconfig=os.environ.get("KUBECONFIG"),
        )


def build_real_agents(
    config: AgentConfig | None = None,
    *,
    # Per-agent overrides. Tests pass these to inject fixtures.
    prom_backend: PromBackend | None = None,
    loki_backend: LokiBackend | None = None,
    cluster: ClusterIO | None = None,
    scanner_runner: ScannerRunner | None = None,
    diagnoser: Diagnoser | None = None,
    fixer_strategy: FixerStrategy | None = None,
    code_reader: TargetCodeReader | None = None,
    harness: Harness | None = None,
) -> Agents:
    """Construct real ClaudeXAgent instances wired to their backends.

    For each agent, prefer the explicit override; fall back to a backend built
    from `config`. Raises AgentConfigError if a required dep is missing AND the
    agent will need it at runtime.

    Note: the LLM-driven strategies (ClaudeDiagnoser, ClaudeFixerStrategy) are
    M5.x/M6.x stubs that raise NotImplementedError when called. Use overrides
    (FixtureDiagnoser / FixtureFixerStrategy) until those land.
    """
    cfg = config or AgentConfig.from_env()

    # Tester needs a Prom backend at first method call. Either an override or
    # PROM_URL must be set; otherwise the agent will fail when invoked.
    tester_prom = prom_backend
    if tester_prom is None and cfg.prom_url:
        tester_prom = HttpxPromBackend(cfg.prom_url)
    tester = ClaudeTesterAgent(prom_backend=tester_prom)

    # Chaos needs a cluster backend. We don't try to auto-build a real
    # KubernetesClusterIO here (it's M3.c); callers must pass an override or
    # the agent's execute() will fail gracefully with a clear error.
    chaos = ClaudeChaosAgent(cluster=cluster, kubeconfig=cfg.kubeconfig)

    # Security uses SubprocessRunner by default — Trivy on PATH.
    security = ClaudeSecurityAgent(runner=scanner_runner or SubprocessRunner())

    # Diagnostician: tools + a Diagnoser. We compose what we can; the diagnoser
    # default is ClaudeDiagnoser (stub raises until M5.x).
    diag_loki = loki_backend
    if diag_loki is None and cfg.loki_url:
        diag_loki = HttpxLokiBackend(cfg.loki_url)

    diag_code = code_reader
    if diag_code is None and cfg.target_repo_path:
        from pathlib import Path

        diag_code = TargetCodeReader(Path(cfg.target_repo_path))

    diagnostician = ClaudeDiagnosticianAgent(
        diagnoser=diagnoser or ClaudeDiagnoser(),
        loki=diag_loki,
        prom=tester_prom,
        code=diag_code,
    )

    # Fixer: strategy default is ClaudeFixerStrategy (stub raises until M6.x).
    fixer = ClaudeFixerAgent(strategy=fixer_strategy or ClaudeFixerStrategy())

    # If a Harness was supplied, wrap each agent so the orchestrator gets a
    # uniform invocation log across all of them. Wrapped agents satisfy the
    # same Protocol; the orchestrator can't tell them apart.
    if harness is not None:
        return Agents(
            tester=harness.instrument("tester", tester),  # type: ignore[arg-type]
            chaos=harness.instrument("chaos", chaos),  # type: ignore[arg-type]
            security=harness.instrument("security", security),  # type: ignore[arg-type]
            diagnostician=harness.instrument("diagnostician", diagnostician),  # type: ignore[arg-type]
            fixer=harness.instrument("fixer", fixer),  # type: ignore[arg-type]
        )

    return Agents(
        tester=tester,
        chaos=chaos,
        security=security,
        diagnostician=diagnostician,
        fixer=fixer,
    )
