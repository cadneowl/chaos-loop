"""
Scanner wrappers. Each module exposes a `run(target, **kwargs) -> list[SecurityFinding]`
function. Stay shell-thin; this is where we shell out to Syft, Grype, Trivy, ZAP, etc.

Milestone-4 work; for now these are signature stubs.
"""
