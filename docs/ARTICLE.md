# Closing the loop on chaos engineering

*Or: we built AI to torment the apes back.*

---

While working on NEOOWL with 4 engineers who are already running on fumes
and previously grinding through releases for years.
You realize people hate it when you come at them with *"I turned off
your fridge and now your food is ruined"* defects.
QA and automation don't want to turn off fridges to expose a critical
gap in your chicken.

The theory of CAP theorem is so solid no one thinks about it past the
system design phase in smaller companies.
We are not all Google or Netflix. We just want our code to work.

If I had to rank the most karma-eroding defects, it will be those:

5. Last-moment edge cases in QA.
4. Last-minute perf regressions.
3. PM makes a wish for Christmas that you need to make come true.
2. Security findings discovered after everything else is spotless.
1. **CHAOS findings** — an ape disconnected your service and opened a
   defect that things don't work.

Every dev has a voodoo doll of each of the above and a tasteful needle
collection.

But now we have **AI** to torment.

Will **AI** replace us all? Yes, yes it will.

Should we resist? It doesn't really matter.

Can we torment **AI** back? Yes, yes we can.

This repository is the apparatus.

---

## The gap that eats your weekends

Modern chaos tools (Chaos Mesh, Litmus, AWS FIS, Gremlin) all stop at
the satisfying part. The ape arrives. The pod dies. The network drops.
The cert revokes. Then the tool leaves. A human reads a dashboard,
decides what (if anything) it means, files a ticket, and maybe writes
a fix two sprints later.

That gap — between *we induced a regression* and *we have a reviewed
artifact that addresses it* — is exactly where the karma erodes. The
chaos run did its job. The dashboard lit up. Six weeks later you ship
the same bug to prod anyway because nobody had time to translate the
red squiggle into a PR.

This project does the loop in one command. It hypothesizes what might
break by reading the target's source. It breaks it. It decides whether
anything actually went wrong. It diagnoses why with cited evidence.
And it opens a **draft PR** with a fix and a regression test. Never
auto-merges. The ape is on a short leash. So is the AI.

---

## The cast

Five agents, one orchestrator. Each agent has one job and zero
authority over anyone else's decisions. They communicate through
Pydantic-typed contracts, which means when they lie to each other the
type checker catches it before it reaches main.

<p align="center">
  <img src="cast/diagram_cast.png" alt="orchestrator delegating to tester, chaos, security, diagnostician, fixer" width="780" />
</p>

- **Tester** — reads the target's source for fragility patterns
  (missing retries, sync-in-async, single-replica deployments,
  hard-coded secrets — the usual suspects). Runs probes against
  Prometheus N times to get a statistical baseline, not a vibe check.
- **Chaos** — the ape. Renders one of seven fault-catalogue entries
  into a Chaos Mesh CRD and applies it. Cleanup is deterministic even
  on crash, because uncleaned chaos is just an outage with extra steps.
- **Security** — Trivy + Syft + Grype + gitleaks + cosign + kubescape.
  Security findings flow through the same loop as functional ones. A
  cert that revokes mid-run is just chaos with a smaller blast radius.
- **Diagnostician** — correlates the chaos timeline against logs +
  metrics + the target's code, proposes ranked hypotheses with cited
  evidence. The kind of post-mortem you wish you'd written at 3am.
- **Fixer** — decides on a fix class, writes the patch, adds a
  regression test, opens a **draft PR**. Default-deny path denylist.
  Never auto-merges. We give the AI the wrench. We don't give it the
  keys.

All five share the same `Protocol`, so a deterministic `Static*`
implementation drops in for the LLM-backed one in CI, on the free
tier, or whenever you don't trust the AI today and you just want your
code to work.

---

## The state machine

The orchestrator is deterministic Python. Every transition is
persisted to SQLite, so a mid-run crash is recoverable — the ape can
be revived from the autopsy.

<p align="center">
  <img src="cast/diagram_state_machine.png" alt="state machine: INITIALIZING through pre-flight gates to BASELINE / BASELINE_OK / INJECT / VERIFY branches, then STEADY → RECORDED on the happy path or REGRESSED → DIAGNOSE → PROPOSE_FIX → FIX_PROPOSED → RECORDED, with explicit BASELINE_FAIL / INJECT_FAILED → ABORTED branches" width="780" />
</p>

Hard rules baked at the state-machine level, not left up to the
agent's better judgment:

- **Baseline already showing regression aborts the run before any
  fault.** Chaos-testing a yellow system can turn it red. The gate is
  non-negotiable.
- **One fault per experiment** unless the plan opts in. Attribution
  matters: when two faults run at once, you don't know which ape did
  it.
- **Per-step budget checks.** Every LLM-incurring call refreshes spend
  from the harness, then re-evaluates the hard cap before the next
  step. Over budget → abort cleanly. The AI doesn't bankrupt the
  experiment to finish its thought.
- **The fixer never auto-merges.** Drafts only. Path denylist. Human
  eyes on every diff that leaves the loop.

---

## The meta-harness — keeping the AI on a leash

LLMs are the cognitive surface of this system. They are also the
surface that costs money, leaks data, hallucinates, and loops forever
because *it almost had the answer*. Every call into an LLM-backed
agent passes through a **meta-harness** wrapper.

<p align="right">
  <img src="cast/meta_harness.png" alt="The meta-harness — auditor with clipboard" width="240" />
</p>

The harness is a `__getattr__` proxy. `harness.instrument("tester",
agent)` returns a transparent stand-in that satisfies the same
`Protocol` as the underlying agent. Sync attribute reads pass through;
async method calls get instrumented. The wrapped agent is read-only —
`__setattr__` raises — so a misbehaving caller can't mutate state on
the proxy. The AI gets one job. The harness watches the door.

What it does on every coroutine invocation:

| Concern | How |
|---|---|
| **Observability** | Logs entry / exit / duration / error / one-line input + output summaries. INFO on success, WARNING on raise. |
| **Audit trail** | Builds an `AgentInvocation` record and appends it to `harness.invocations`. The orchestrator attaches the full list to the SQLite record before every save, so a crash mid-run still leaves a forensic trail. The corpse keeps its receipts. |
| **Cost attribution** | Sets a per-task `ContextVar` to the current invocation. When the strategy calls `complete_with_tools`, that function reads the var and credits the call's `response_cost` to the invocation that triggered it — no harness reference plumbed through three constructors, no spend ever lands on the wrong line. |
| **Budget enforcement** | After every step, the orchestrator sums spend across the audit trail and re-checks the hard cap. Over budget → abort. The AI gets exactly as much rope as the budget allows. |
| **Error propagation** | Exceptions always propagate. The harness records the error string and gets out of the way. A failing agent fails the experiment — silent partial success is not allowed. We don't grade on a curve. |

The orchestrator never sees an LLM call directly. It only sees agent
invocations through `Protocol` interfaces. The harness is border
control. Adding a new agent? `harness.instrument("new-agent",
instance)` and inherit observability + audit + cost + budget for free.
The control properties hold under nested calls — N nested
`complete_with_tools` calls all credit their cost to the one
invocation that started the outer agent method, via the ContextVar.

The AI can pontificate exactly as long as the budget allows, exactly
as transparently as the audit trail demands, and exactly as
isolated-from-state as the proxy enforces. The voodoo doll has a
clipboard now.

---

## What an operator actually sees

The UI surfaces the audit trail. The most useful screen is the
timeline: every agent invocation interleaved with every chaos-mesh CRD
lifecycle event, sorted by timestamp, chaos events highlighted with a
peach band so the injection window is unmistakable.

<p align="center">
  <img src="../ui/docs/screenshots/03-timeline.png" alt="Timeline tab of a real chaos run, showing chaos.scheduled / chaos.started / chaos.cleaned-up interleaved with tester.baseline, chaos.execute, tester.verify" width="780" />
</p>

That screenshot is from a real run. The `chaos.started` row carries
the actual `NetworkChaos/network-loss-00ddba11` CRD identifier that
chaos-mesh installed on the cluster. The 30-second gap between
`chaos.execute` start and end is the orchestrator holding while the
ape does its work.

Cross-experiment views aggregate every run in the store:

<p align="center">
  <img src="../ui/docs/screenshots/09-dashboard.png" alt="Dashboard: three section cards for LLM spend, findings, and fix proposals" width="780" />
</p>

LLM spend per run, which fragility patterns recur across the entire
history (the voodoo doll's anatomical map, if you will), what the
fixer has been shipping. The operator clicks Pause / Resume / Abort
from the same UI when something looks off. Putting the ape down
humanely is a single click.

---

## Try it (verified end-to-end on a clean machine)

The recipe below needs **no Kubernetes, no LLM, no API key**. The
orchestrator's `--dry-run` swaps every agent for a mock that produces
a record with the full shape of a real one — eight agent invocations,
a fake `NetworkChaos` lifecycle, a mock diagnosis, a mock fix
proposal. Exactly the data the UI was built around.

Prerequisites: Python 3.11+, Node.js 22+,
[pnpm](https://pnpm.io/installation) 11.

```bash
# 1. install
git clone https://github.com/cadneowl/chaos-loop chaos && cd chaos
python -m venv .venv && source .venv/bin/activate    # POSIX
# .\.venv\Scripts\Activate.ps1                       # Windows PowerShell
pip install -e ".[dev]"

# 2. dry-run an experiment (no cluster required)
chaos run experiments/examples/01-redis-network-loss.yaml --dry-run

# 3. start the UI (two terminals, both inside ui/)
cd ui && pnpm install
pnpm --filter @chaos/ui-server start:dev   # http://127.0.0.1:3000
pnpm --filter @chaos/ui-web    start       # http://localhost:4200
```

Open <http://localhost:4200/>. The experiment from step 2 is on the
list, every tab is populated, every chart has data.

For a real chaos run against a live cluster — kind + chaos-mesh +
Prometheus port-forward — follow
[`ui/README.md#connecting-to-the-chaos-infra`](../ui/README.md#connecting-to-the-chaos-infra).

---

The CLI lives in [`orchestrator/main.py`](../orchestrator/main.py).
The harness, sub-300 lines that buy us everything in the table above,
is [`agents/_harness.py`](../agents/_harness.py).

The voodoo dolls and the needle collection are yours to keep.
