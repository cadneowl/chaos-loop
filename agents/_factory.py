"""
Construct real agents from configuration.

Each agent's constructor takes its backends/strategies as explicit kwargs.
`build_real_agents` wires those defaults from environment variables / CLI flags
and surfaces missing-dep errors as plain ValueError, not deep inside the agent.

A profile selects which cognitive seam implementations to wire:
    - "static"  -> Static* everywhere ($0, no LLM)
    - "hybrid"  -> Hybrid* wrapping Static + Claude (Static floor, LLM augment)
    - "llm"     -> Claude* everywhere (full agentic, requires API)

The orchestrator's `chaos run` command calls this. Tests can call it directly
with overrides to inject fixtures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agents._harness import Harness
from agents.chaos.agent import ClaudeChaosAgent
from agents.chaos.cluster import ClusterIO
from agents.diagnostician.agent import ClaudeDiagnosticianAgent
from agents.diagnostician.diagnoser import (
    ClaudeDiagnoser,
    Diagnoser,
    HybridDiagnoser,
    StaticDiagnoser,
)
from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.diagnostician.tools.loki import HttpxLokiBackend, LokiBackend
from agents.fixer.agent import ClaudeFixerAgent
from agents.fixer.strategy import (
    ClaudeFixerStrategy,
    FixerStrategy,
    HybridFixerStrategy,
    StaticFixerStrategy,
)
from agents.security.agent import ClaudeSecurityAgent
from agents.security.runner import ScannerRunner, SubprocessRunner
from agents.tester.agent import ClaudeTesterAgent
from agents.tester.hypothesizer import (
    ClaudeHypothesizer,
    HybridHypothesizer,
    Hypothesizer,
    StaticHypothesizer,
)
from agents.tester.tools.prometheus import HttpxPromBackend, PromBackend
from orchestrator.loop import Agents

Profile = Literal["static", "hybrid", "llm"]

# Profile semantics (see docstring of build_real_agents).
_VALID_PROFILES: tuple[Profile, ...] = ("static", "hybrid", "llm")


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
    # LLM tuning — used when profile is 'hybrid' or 'llm'.
    model: str = "claude-opus-4-7"
    api_base: str | None = None

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            prom_url=os.environ.get("PROM_URL"),
            loki_url=os.environ.get("LOKI_URL"),
            target_repo_path=os.environ.get("TARGET_REPO_PATH"),
            kubeconfig=os.environ.get("KUBECONFIG"),
            model=os.environ.get("CHAOS_LLM_MODEL", "claude-opus-4-7"),
            api_base=os.environ.get("CHAOS_LLM_API_BASE"),
        )


def build_real_agents(
    config: AgentConfig | None = None,
    *,
    profile: Profile = "static",
    # Per-agent overrides. Tests pass these to inject fixtures. When supplied,
    # they replace the profile-selected default — letting tests drive the loop
    # with deterministic fixtures regardless of profile.
    prom_backend: PromBackend | None = None,
    loki_backend: LokiBackend | None = None,
    cluster: ClusterIO | None = None,
    scanner_runner: ScannerRunner | None = None,
    hypothesizer: Hypothesizer | None = None,
    diagnoser: Diagnoser | None = None,
    fixer_strategy: FixerStrategy | None = None,
    code_reader: TargetCodeReader | None = None,
    harness: Harness | None = None,
) -> Agents:
    """Construct real ClaudeXAgent instances wired to their backends.

    profile:
        - "static": StaticHypothesizer / StaticDiagnoser / StaticFixerStrategy.
          No LLM, no Anthropic API key needed. $0 per run.
        - "hybrid": HybridHypothesizer / HybridDiagnoser / HybridFixerStrategy.
          Each runs Static + Claude (or local LLM) and merges. Static floor
          means the loop survives if the LLM fails.
        - "llm": Claude* everywhere (or whatever model = ...). Full agentic.
          Requires API key (or local model via Ollama + api_base).

    For each agent, prefer the explicit override; fall back to the
    profile-selected strategy.
    """
    if profile not in _VALID_PROFILES:
        raise AgentConfigError(
            f"profile must be one of {_VALID_PROFILES}, got {profile!r}"
        )
    cfg = config or AgentConfig.from_env()

    # Code reader — needed by the hypothesizer and the diagnostician. Try
    # explicit override first, then config path.
    code = code_reader
    if code is None and cfg.target_repo_path:
        code = TargetCodeReader(Path(cfg.target_repo_path))

    # Tester needs a Prom backend at first method call (for baseline/verify).
    # If neither override nor PROM_URL is provided, the tester errors at use-time.
    tester_prom = prom_backend
    if tester_prom is None and cfg.prom_url:
        tester_prom = HttpxPromBackend(cfg.prom_url)

    # Pick the hypothesizer per profile (unless test override).
    tester_hypothesizer = hypothesizer or _build_hypothesizer(profile, cfg)
    tester = ClaudeTesterAgent(
        prom_backend=tester_prom,
        hypothesizer=tester_hypothesizer,
        code=code,
    )

    # Chaos needs a cluster backend (M3.c lands the real one). Override-only for now.
    chaos = ClaudeChaosAgent(cluster=cluster, kubeconfig=cfg.kubeconfig)

    # Security uses SubprocessRunner by default — Trivy on PATH.
    security = ClaudeSecurityAgent(runner=scanner_runner or SubprocessRunner())

    # Diagnostician: tools + a Diagnoser (selected by profile or overridden).
    diag_loki = loki_backend
    if diag_loki is None and cfg.loki_url:
        diag_loki = HttpxLokiBackend(cfg.loki_url)

    diagnostician_diagnoser = diagnoser or _build_diagnoser(profile, cfg)
    diagnostician = ClaudeDiagnosticianAgent(
        diagnoser=diagnostician_diagnoser,
        loki=diag_loki,
        prom=tester_prom,
        code=code,
    )

    # Fixer: strategy selected by profile or overridden.
    fixer_built = fixer_strategy or _build_fixer_strategy(profile, cfg, code=code)
    fixer = ClaudeFixerAgent(strategy=fixer_built)

    # If a Harness was supplied, wrap each agent so the orchestrator gets a
    # uniform invocation log. Wrapped agents satisfy the same Protocol; the
    # orchestrator can't tell them apart.
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


# ---------------------------------------------------------------------------- #
# Profile -> concrete strategy                                                 #
# ---------------------------------------------------------------------------- #


def _build_hypothesizer(profile: Profile, cfg: AgentConfig) -> Hypothesizer:
    if profile == "static":
        return StaticHypothesizer()
    llm = ClaudeHypothesizer(model=cfg.model, api_base=cfg.api_base)
    if profile == "llm":
        return llm
    # hybrid: Static + LLM, with graceful fallback.
    return HybridHypothesizer(static=StaticHypothesizer(), llm=llm)


def _build_diagnoser(profile: Profile, cfg: AgentConfig) -> Diagnoser:
    if profile == "static":
        return StaticDiagnoser()
    llm = ClaudeDiagnoser(model=cfg.model, api_base=cfg.api_base)
    if profile == "llm":
        return llm
    return HybridDiagnoser(static=StaticDiagnoser(), llm=llm)


def _build_fixer_strategy(
    profile: Profile, cfg: AgentConfig, *, code: TargetCodeReader | None
) -> FixerStrategy:
    if profile == "static":
        return StaticFixerStrategy()
    llm = ClaudeFixerStrategy(model=cfg.model, api_base=cfg.api_base, code=code)
    if profile == "llm":
        return llm
    return HybridFixerStrategy(static=StaticFixerStrategy(), llm=llm)
