# Modes: `static` / `hybrid` / `llm`

Every cognitive step in this codebase — hypothesizing what to chaos-test, diagnosing what broke, proposing how to fix it — exists behind a Protocol seam with multiple implementations. You pick which mix you want via `--profile` on `chaos run`, or via the corresponding env vars / Python kwargs.

## The three modes at a glance

| Mode | Hypothesize | Diagnose | Propose-fix | Cost | When to use |
|---|---|---|---|---|---|
| `static` | `StaticHypothesizer` | `StaticDiagnoser` | `StaticFixerStrategy` | **$0** | CI, default, $0 baseline, repeatable runs |
| `hybrid` | `HybridHypothesizer` (Static + LLM) | `HybridDiagnoser` | `HybridFixerStrategy` | **$$** | best-effort: Static floor + LLM augmenting |
| `llm` | `ClaudeHypothesizer` (LiteLLM-backed) | `ClaudeDiagnoser` | `ClaudeFixerStrategy` | **$$$** | production runs with explicit LLM budget |

Plus a fourth tier used only inside the test suite:

| `Fixture*` | predetermined output from a list or async callback | $0 | tests, deterministic dry-runs |

All four implement the same Pydantic-typed Protocol — the orchestrator's loop body can't tell them apart.

## What each mode actually does

### `static` — pattern matches, rule tables, templates

- **Hypothesizer** runs a set of detectors (`MissingTimeoutDetector`, `MissingRetryDetector`, `SingleReplicaDetector`, `HardPodAffinityDetector`, `HardcodedSecretDetector`) over the target's source code. Each detector emits `Issue` objects that a templating layer turns into validated `Hypothesis` instances with catalogue-mapped `proposed_fault`. See `agents/tester/detectors/`.
- **Diagnoser** maps `FaultCategory` → candidate `(fix_class, base_confidence)` entries via a lookup table; boosts confidence based on symptom keywords in the failed reports.
- **Fixer strategy** picks one of ten per-fix-class templates and produces a structured proposal (reasoning + files_touched + sketched regression-test path). No actual file edits.

Strengths:
- **Deterministic** — same input, same output. Repeatable, snapshot-testable.
- **Fast** — sub-2s for the full hypothesizer pass on a medium-sized repo.
- **Free** — no API calls. Works offline.
- **Honest** — won't pretend to find what it doesn't see; misses the cases the rules don't model.

Weaknesses:
- Coverage is bounded by what the detectors / rules / templates encode. Novel patterns slip through.
- The diagnostician can't do real cross-evidence reasoning. It does keyword matching, not RCA.
- The fixer can't write the actual patch. Templates describe *what to change*, not the diff.

### `hybrid` — Static floor + LLM augment, with graceful fallback

Each cognitive seam runs **both** the Static and the LLM implementation and merges the output:

- `HybridHypothesizer` and `HybridDiagnoser` merge two lists: duplicates (same fault + overlapping file references for hypotheses; same fix_class + overlapping affected_paths for diagnoses) collapse to the higher-confidence version. Distinct findings from each side are all kept. Output is ranked by confidence.
- `HybridFixerStrategy` is one-or-the-other (a proposal isn't a list): try the LLM first; fall back to Static if the LLM raises **or** returns empty output.

If the LLM raises, hybrid mode silently degrades to Static-only and logs a warning. The loop never breaks because of a transient API issue.

Use when:
- You want the safety net of Static for the things you know matter.
- You have a budget for LLM tokens but don't want to depend on them being there.
- You're running in production and absolutely need a result.

### `llm` — full agentic mode

Calls the LLM (Anthropic Claude by default, but anything LiteLLM supports — Ollama, OpenAI, etc.) with MCP-style tools that read the target's code, logs, and metrics. The LLM drives a multi-turn tool-call loop, then emits a JSON answer that gets validated against the contracts.

Use when:
- You want the broadest possible coverage (the LLM can find novel patterns Static can't).
- The data layer is rich enough (logs, traces, code) that the LLM can do real RCA.
- You're OK paying $0.20–$5 per experiment.

## Choosing per-agent (advanced)

The `build_real_agents` factory accepts per-agent overrides (`hypothesizer=`, `diagnoser=`, `fixer_strategy=`). Tests use this to inject `Fixture*` variants; production code can use it to mix-and-match — e.g., "I want LLM for the hypothesizer but Static for the rest." The Python API:

```python
from agents._factory import AgentConfig, build_real_agents
from agents.diagnostician.diagnoser import StaticDiagnoser
from agents.tester.hypothesizer import ClaudeHypothesizer

agents = build_real_agents(
    AgentConfig(model="claude-opus-4-7"),
    profile="static",                                # baseline
    hypothesizer=ClaudeHypothesizer(model="claude-opus-4-7"),  # but use LLM here
    # diagnoser, fixer_strategy default from profile (StaticDiagnoser, StaticFixerStrategy)
)
```

## Model and provider selection

`--profile hybrid` and `--profile llm` need to know which LLM to call. Specify via `--model` (or `$CHAOS_LLM_MODEL`):

```bash
# Anthropic Claude (default)
chaos run plan.yaml --profile llm --model claude-opus-4-7

# Local Ollama
chaos run plan.yaml --profile hybrid \
  --model ollama/qwen2.5-coder:14b \
  --api-base http://localhost:11434

# OpenAI
chaos run plan.yaml --profile llm --model openai/gpt-4o
```

The model string is passed through to LiteLLM. Bare names like `claude-opus-4-7` are routed to Anthropic; bare `qwen…` / `llama…` to Ollama; `gpt-*` to OpenAI. Use explicit `provider/model` form when in doubt.

## Honest caveats about local models

Our experiments (Qwen 2.5 Coder 14B and Qwen 3 Coder 30B-A3B via Ollama) showed that local models in the 14–30B range:

- **Can** read code through MCP tools when the tool-calling protocol works.
- **Often fail to terminate** multi-turn tool loops cleanly — they either emit tool calls as plain text content (model-side bug, see [ollama#14745](https://github.com/ollama/ollama/issues/14745)) or get stuck in repeated tool calls (the LLM runner has a safety net that breaks the loop after N identical calls).

For these reasons, **`--profile static` is the recommended default**: it's free, deterministic, and works without a working LLM agentic loop. `--profile hybrid` uses the LLM for what it's good at (novel patterns, language nuance) and keeps Static as the safety net.

## Cost expectations

Rough order-of-magnitude per `chaos run`, against a medium-sized target:

| Mode | Cost per run |
|---|---|
| `static` | $0 |
| `hybrid` (Claude) | $0.20–$3 |
| `hybrid` (Ollama, local) | $0 (compute only) |
| `llm` (Claude) | $0.30–$5 |
| `llm` (Ollama, local) | $0 (compute only) |

The orchestrator's budget tracker (`shared.contracts.TokenBudget`) hard-caps spend per experiment so a runaway run can't drain a budget. Default cap: $10 hard, $2 soft.
