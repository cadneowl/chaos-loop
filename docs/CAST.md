# The Cast

<p align="center">
  <img src="cast/the_group.png" alt="The seven characters who run the loop: orchestrator, tester, chaos goblin, security bouncer, diagnostician, fixer, meta-harness" width="800" />
</p>

Seven characters do all the work. Two are not agents — the
**orchestrator** is the unflappable referee, the **meta-harness** is the
border control between the agents and the LLM bill. The other five are
the agents themselves.

Drop them into your dashboards. Print them on stickers. Cite them in PRs.

---

## The Orchestrator · "the conductor"

<img src="cast/orchestrator.png" alt="The Orchestrator: a composed cartoon conductor in a tuxedo holding a baton" width="320" align="right" />

> *"Everyone in their lane. The state machine doesn't take requests."*

Refuses to delegate state transitions to any model. Persists every step
to SQLite before moving to the next. If your experiment crashed
mid-flight, the orchestrator already wrote down where.

**Stat block**
- Uses LLMs: never
- Safety gates: 4 hard
- Forgiveness: zero

Code: [`orchestrator/loop.py`](../orchestrator/loop.py)

<br clear="all" />

---

## The Tester · "the statistician"

<img src="cast/tester.png" alt="The Tester: a cartoon scientist in a lab coat holding a clipboard with bell curves" width="320" align="right" />

> *"Trust nothing. Sample everything. Especially 'looks fine to me'."*

Does **not** do single snapshots. Runs every probe N times, computes a
proper distribution, and post-chaos asks "is this sample from the same
distribution?" — not "do the numbers match?" Also reads source code and
generates fragility hypotheses without an LLM if you ask.

**Stat block**
- Probes per baseline: 5×
- Z-score threshold: 3.0σ
- Coffee thermoses: 4

Code: [`agents/tester/`](../agents/tester/README.md)

<br clear="all" />

---

## The Chaos Goblin · "small, fast, gleeful"

<img src="cast/chaos_destroyer.png" alt="The Chaos Goblin: a small green goblin gleefully wielding an oversized red wrench on top of a tipped server" width="320" align="right" />

> *"replicas: 1? For ME? You shouldn't have."*

Renders Chaos Mesh CRDs, applies them, waits, cleans up. Cannot run
unless the orchestrator approves the blast radius. Cannot target
production substrings. Cannot multi-fault without explicit consent.
Otherwise: extremely enthusiastic.

**Stat block**
- gracePeriod: 0s
- Remorse: 0
- One fault per experiment: yes

Code: [`agents/chaos/`](../agents/chaos/README.md)

<br clear="all" />

---

## The Security Bouncer · "your name's not on the SBOM"

<img src="cast/security.png" alt="The Security Bouncer: a burly bouncer in sunglasses with arms crossed in front of a velvet rope" width="320" align="right" />

> *"Your image is unsigned, your deps are stale, and we're not friends."*

Trivy, Syft, Grype, gitleaks, cosign, kubescape — one runner, one
report. Captures SBOM digests at baseline and flips
`sbom_drift_from_baseline=true` if anything changed post-chaos.

**Stat block**
- Scanners wielded: 6
- Has critical/high → abort: yes
- SBOM drift detection: live

Code: [`agents/security/`](../agents/security/README.md)

<br clear="all" />

---

## The Diagnostician · "the detective"

<img src="cast/diagnostician.png" alt="The Diagnostician: a cartoon detective in a deerstalker hat holding a magnifying glass that comically enlarges one eye" width="320" align="right" />

> *"It was the missing retry, in the cart service, with the 503 at 14:02:18."*

Outputs **hypotheses**, never assertions. Every claim points at a log
line, a metric, a span, or a source line. `working-as-intended` is a
legitimate verdict — some fragilities are by design and the right
response is a documented note, not a PR.

**Stat block**
- Citations per claim: 100%
- Trusts confessions: no
- working-as-intended: valid verdict

Code: [`agents/diagnostician/`](../agents/diagnostician/README.md)

<br clear="all" />

---

## The Fixer · "the handyman"

<img src="cast/fixer.png" alt="The Fixer: a friendly handyman in denim overalls and a beanie holding an olive branch and a clipboard" width="320" align="right" />

> *"I draft. I don't merge. You merge. That's the deal."*

Decision tree first: low confidence → `NONE`. `working-as-intended` →
`DOC_ONLY` with a fragility note. Otherwise the LLM strategy proposes
files + reasoning + a regression test sketch. Every output is run
through a path denylist. PRs are always draft.

**Stat block**
- Merges autonomously: never
- `--force` pushes: 0
- Path denylist enforced: yes

Code: [`agents/fixer/`](../agents/fixer/README.md)

<br clear="all" />

---

## The Meta-Harness · "the customs officer"

<img src="cast/meta_harness.png" alt="The Meta-Harness: a meticulous customs officer in a peaked cap holding a rubber stamp above an open ledger" width="320" align="right" />

> *"Permit, please. Spending report. Audit log. Move along."*

Sits between the orchestrator and the agents as a `__getattr__` proxy.
Wraps every async method, records `AgentInvocation` per call, sets a
`ContextVar` so `complete_with_tools` can attribute LLM cost to the
right invocation without plumbing a harness reference through three
constructors. Errors propagate. Spend lands. The orchestrator gets the
final audit trail before the experiment record hits SQLite.

**Stat block**
- LLM calls observed: all of them
- Silent failures: 0
- ContextVar magic: yes

Code: [`agents/_harness.py`](../agents/_harness.py)

<br clear="all" />

---

## The loop, in one line

> The **orchestrator** runs the band. The **meta-harness** audits every
> musician. The five agents play their parts. Nobody auto-merges.
