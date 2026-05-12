"""gitleaks wrapper: secret scan over a git repository or filesystem path.

Reference: https://github.com/gitleaks/gitleaks

Exit code semantics:
    - 0: no leaks found
    - 1: one or more leaks found
    - >=2: tool error (config invalid, can't read path, etc.)

gitleaks treats "leaks found" as a non-zero exit, which is fine for CI but a
poor fit for our model: a successful scan that found two secrets is still a
successful scan. We translate rc=1 with parseable stdout as "scan succeeded
with findings"; only rc>=2 or unparseable output raises.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.security.runner import ScannerError, ScannerRunner, SubprocessRunner
from shared.contracts import FindingSeverity, SecurityFinding


async def scan_repo(
    path: str | Path,
    *,
    runner: ScannerRunner | None = None,
    timeout_seconds: float = 120.0,
) -> list[SecurityFinding]:
    """Run gitleaks against a repo / filesystem path. Returns findings (possibly empty)."""
    runner = runner or SubprocessRunner()
    args = [
        "gitleaks",
        "detect",
        "--source",
        str(path),
        "--report-format",
        "json",
        "--report-path",
        "/dev/stdout",
        "--no-banner",
        "--exit-code",  # explicitly request the 0/1 distinction
        "1",
    ]
    result = await runner.run(args, timeout_seconds=timeout_seconds)
    # 0 = clean, 1 = leaks found, >=2 = tool error.
    if result.returncode >= 2:
        raise ScannerError(
            f"gitleaks exit {result.returncode} for {path}: {result.stderr[:200]}"
        )
    return _parse_gitleaks(result.stdout, location=str(path))


def _parse_gitleaks(stdout: str, *, location: str) -> list[SecurityFinding]:
    """Parse gitleaks JSON report.

    An empty repo or clean scan emits ``[]`` (or no output at all in some
    versions); we handle both. Each finding has Description, RuleID,
    Commit, File, StartLine, etc.
    """
    if not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ScannerError(f"gitleaks stdout was not JSON: {e}") from e

    # Some gitleaks versions wrap findings in {"findings": [...]}; current
    # versions emit a bare array. Accept either.
    items = payload if isinstance(payload, list) else payload.get("findings") or []

    out: list[SecurityFinding] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        rule = item.get("RuleID") or item.get("ruleId") or "unknown-rule"
        file_ = item.get("File") or item.get("file") or "?"
        line = item.get("StartLine") or item.get("startLine") or 0
        commit = item.get("Commit") or item.get("commit") or ""
        desc = item.get("Description") or item.get("description") or ""
        # gitleaks doesn't grade leaks; every leak is HIGH by policy.
        # Hardcoded test secrets in fixtures usually trip too; reviewers
        # confirm severity case-by-case.
        out.append(
            SecurityFinding(
                id=f"f-gitleaks-{rule}-{i}".lower().replace("_", "-"),
                severity=FindingSeverity.HIGH,
                title=f"{rule}: leaked secret in {file_}:{line}",
                description=desc,
                scanner="gitleaks",
                cve=None,
                evidence={
                    "rule_id": rule,
                    "file": file_,
                    "start_line": line,
                    "commit": commit,
                },
                location=f"{location}:{file_}",
            )
        )
    return out
