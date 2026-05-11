# Comparison to prior art

As of mid-2026, snapshot of the field.

## Direct overlap — read these before building anything new

### ChaosEater (NTT, ASE 2025 NIER) — closest prior art

- Repo: https://github.com/ntt-dkiku/chaos-eater
- Paper: https://arxiv.org/abs/2511.07865
- Multi-agent LLM workflow over the chaos engineering cycle (hypothesis → experiment → analysis → improvement)
- Targets Kubernetes via Chaos Mesh, uses k6 for load
- Modifies k8s manifests as the "improvement" — **does not open application-code PRs**
- No security dimension
- Research-only license from NTT — not usable as a base for OSS work
- GPT-4o-tuned; "may be unstable with other models"
- Requires Skaffold project layout for the target

**Our differentiation:** application-code PRs to the target repo; security chaos engineering as first-class; Apache 2.0; Claude-native; not Skaffold-bound; statistical baselines.

### Harness AI Reliability Agent (commercial)

- Recommends experiments and gives remediation guidance via natural language
- Inside Harness Chaos Engineering platform
- Closed-source, commercial
- No autonomous code PR generation as of public docs

**Our differentiation:** OSS; opens PRs; security integrated; no vendor lock-in.

### LitmusChaos + MCP integration

- MCP server in front of Litmus for chat-driven experiment trigger
- Not a closed loop — humans still interpret results

## Adjacent but solving different problems

| Project | What it does | Why it's not the same |
|---|---|---|
| Chaos Monkey (Netflix) | Random pod termination | Inject only; no loop |
| Chaos Mesh | k8s-native fault injection via CRDs | The engine we wrap |
| LitmusChaos | k8s-native chaos workflows + Hub | Inject + workflow only |
| Gremlin | Commercial chaos platform | Inject + dashboards |
| AWS FIS / Azure Chaos Studio | Cloud-managed fault injection | Inject only, cloud-specific |
| Netflix ChAP | Internal chaos automation | Automated experiment selection, not OSS |
| Steadybit | Commercial workflow + advice | Closer to our shape but closed |
| Rootly AI, PagerDuty AIOps, Cleric.io | AI-driven RCA for **real** incidents | We do RCA for *chaos-induced* events |
| MicroRemed (arxiv 2511.01166) | LLM remediation **benchmark** for microservices | Eval target for our fixer |
| `deepankarm/agent-chaos` | Chaos engineering **of** AI agents | Inverse direction |
| arxiv 2505.03096 | Robustness of LLM multi-agent systems via chaos | Inverse direction |
| CHESS framework | Evaluation framework for self-healing systems | An eval target |
| ChaoSlingr (Verica) | Security Chaos Engineering tool | Security only, no AI |
| Aaron Rinehart / Kelly Shortridge writings | The SCE methodology we follow | Books / blogs, not code |

## Honest assessment

The space is moving fast. "AI for chaos engineering" is no longer novel as a category — it's table-stakes among research projects and one commercial entrant. What is still differentiated:

1. **Application-code PRs as a first-class output** — none of the AI-driven chaos systems we found do this. They all stop at k8s config edits or human-readable advice.
2. **Security chaos engineering integrated into the same loop** — the SCE literature exists, but there's no AI-driven implementation that treats security and reliability findings symmetrically.
3. **Multi-agent with hard contracts** — ChaosEater is multi-agent but tightly coupled; nobody publishes the inter-agent schemas as a first-class artifact.
4. **Permissive OSS license** — ChaosEater is research-licensed; Harness is closed; Litmus is OSS but not closed-loop.

If by the time this is usable any of those gaps have been closed by someone else, the differentiation collapses to "ours is the Claude-native one." Plan accordingly.
