# Roadmap

## Milestone 0 — scaffold (this commit)

- [x] Directory layout
- [x] README, ARCHITECTURE, SECURITY_CHAOS, SAFETY, COMPARISON docs
- [x] Pydantic contracts in `shared/`
- [x] Orchestrator skeleton with state machine + safety gates (stubs)
- [x] Agent skeletons with intended tool surfaces
- [x] Example experiments YAML
- [x] Infra manifests for kind + Chaos Mesh + observability + security scanners
- [x] OpenTelemetry Demo bundling instructions

## Milestone 1 — dry-run end-to-end

- [ ] Orchestrator runs the state machine end-to-end with **mocked** agent responses
- [ ] Experiment record persists to SQLite
- [ ] Safety gates trip correctly in unit tests (target-cluster gate, blast radius, abort)
- [ ] `chaos run examples/01-redis-network-loss.yaml --dry-run` prints a coherent trace

## Milestone 2 — first real agent (tester)

- [ ] tester agent reads OTel demo source code, generates 1+ hypothesis
- [ ] tester baseline produces a real TesterReport for OTel demo running in kind
- [ ] Statistical baseline over N runs (default 5), records distributions

## Milestone 3 — first real injection (chaos)

- [ ] chaos agent applies a real `NetworkChaos` against OTel demo's Redis
- [ ] ChaosTimeline recorded with real timestamps
- [ ] Cleanup verified

## Milestone 4 — security baseline

- [ ] Wrappers for Syft, Grype, Trivy, gitleaks, kubescape, cosign
- [ ] SecurityReport produced for OTel demo baseline
- [ ] One security hypothesis generated from the SBOM

## Milestone 5 — diagnostician

- [ ] Loki query tool
- [ ] Code-reading tool
- [ ] Produces a real DiagnosisReport against a known-broken OTel demo variant

## Milestone 6 — fixer (draft PRs only)

- [ ] gh-PR tool
- [ ] Test-writing tool
- [ ] Opens a real draft PR against a fork of OTel demo with a proposed fix + regression test

## Milestone 7 — first full real loop

- [ ] All 5 agents working
- [ ] One end-to-end experiment runs against OTel demo, opens a real PR
- [ ] Documented walkthrough

## Future

- Multi-fault experiments with timeline-correlated attribution
- Approval workflows (Slack, GitHub Issues)
- Hypothesis-priority queue (don't rediscover known fragilities)
- Inter-experiment learning (was this fragility already PR'd? skip)
- Non-Claude agent implementations (interface is generic)
- VSCode / JetBrains extension that visualizes a running loop
- A scheduled "always-on" mode that picks low-risk hypotheses overnight
