"""
Each agent is a thin wrapper around a Claude Agent SDK session.

Agents:
- tester:        functional baseline, hypothesis generation, post-chaos verify
- chaos:         Chaos Mesh wrapper + security-flavored faults
- security:      DAST, SBOM/SCA, image scan, secrets, k8s posture
- diagnostician: RCA from logs + traces + reports + chaos timeline
- fixer:         draft PR + docs, never auto-merges

Each agent implements the corresponding Protocol from orchestrator.loop. Swap
in non-Claude implementations by satisfying the Protocol — the orchestrator
doesn't care.
"""
