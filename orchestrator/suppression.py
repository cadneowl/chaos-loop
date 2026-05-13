"""
Hypothesis suppression.

The loop is allowed to be confidently wrong about things the operator has
already accepted as wontfix. Without a way to muzzle it, every run wastes
15-20 minutes re-discovering issues that are tracked, deferred, or known
false positives — and the system gets uninstalled.

A `.chaos/suppress.yaml` in the repo root plus a per-plan `suppress:`
field carry rules. A rule matches a hypothesis by any of:

    - hypothesis_id      stable fingerprint of (fix_class, paths, summary)
    - fix_class          the diagnostician's suggested_fix_class slug
    - path_glob          fnmatch glob against any entry in affected_paths
    - summary_contains   case-insensitive substring match against the summary

Each rule carries a `reason` (kept for the audit trail) and an optional
`expires_at` so stale suppressions don't accumulate forever.

Suppressions are **tagging**, not deletion: the diagnosis still records every
hypothesis the diagnostician produced. The orchestrator just skips the
fixer for the suppressed ones, and the UI can show "muted" alongside
"high-confidence". The receipts stay intact.

The `SuppressionRule` model itself lives in `shared.contracts` so it can
sit on `ExperimentPlan.suppress` without a circular import. This module
owns the evaluation logic (matching + fingerprinting + file loading).
"""

from __future__ import annotations

import fnmatch
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from shared.contracts import (
    DiagnosisReport,
    ExperimentPlan,
    RootCauseHypothesis,
    SuppressionRule,
)


class SuppressList(BaseModel):
    """A list of suppression rules. Empty by default."""

    model_config = ConfigDict(extra="forbid")

    rules: list[SuppressionRule] = Field(default_factory=list)

    def merge(self, other: SuppressList) -> SuppressList:
        return SuppressList(rules=[*self.rules, *other.rules])


def rule_matches(
    rule: SuppressionRule, h: RootCauseHypothesis, *, now: datetime
) -> bool:
    """True iff the rule fires against `h` at time `now`.

    All set match fields must be satisfied (AND across fields). An expired
    rule never matches.
    """
    if rule.expires_at is not None and now > rule.expires_at:
        return False
    if rule.hypothesis_id is not None and rule.hypothesis_id != hypothesis_fingerprint(h):
        return False
    if rule.fix_class is not None and rule.fix_class != h.suggested_fix_class:
        return False
    if rule.path_glob is not None and not _any_path_matches(rule.path_glob, h.affected_paths):
        return False
    return not (
        rule.summary_contains is not None
        and rule.summary_contains.lower() not in h.summary.lower()
    )


def describe_rule(rule: SuppressionRule) -> str:
    """Short label for the audit trail. `reason` if set, else the matcher."""
    if rule.reason:
        return rule.reason
    for label in ("hypothesis_id", "fix_class", "path_glob", "summary_contains"):
        val = getattr(rule, label)
        if val:
            return f"matched {label}={val!r}"
    return "matched"


def hypothesis_fingerprint(h: RootCauseHypothesis) -> str:
    """Stable 12-hex-digit fingerprint of a hypothesis.

    Computed once on the Pydantic model as ``RootCauseHypothesis.id``;
    this function is a passthrough so callers can keep the symmetry with
    ``rule_matches(rule, h, now=...)``. The actual hash lives in
    ``shared.contracts`` to avoid a contracts → orchestrator import cycle.
    """
    return h.id


def load_repo_suppress_list(repo_root: Path | None = None) -> SuppressList:
    """Read `.chaos/suppress.yaml` from `repo_root` (defaults to CWD).

    Missing file → empty list. Malformed YAML or schema → raises so the
    operator finds out early. We intentionally do not silently ignore a
    broken file — a suppression rule the loop never reads is worse than
    no suppression at all.
    """
    root = repo_root or Path.cwd()
    path = root / ".chaos" / "suppress.yaml"
    if not path.exists():
        return SuppressList()
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SuppressList.model_validate(raw)


def build_active_list(plan: ExperimentPlan, repo_root: Path | None = None) -> SuppressList:
    """Combine repo-level `.chaos/suppress.yaml` and plan-level `suppress:`.

    Repo rules come first so plan rules layer on top. Ordering is preserved
    for the audit trail; the first matching rule wins inside `apply_to_diagnosis`.
    """
    return load_repo_suppress_list(repo_root).merge(SuppressList(rules=list(plan.suppress)))


def apply_to_diagnosis(
    diagnosis: DiagnosisReport,
    suppress_list: SuppressList,
    *,
    now: datetime | None = None,
) -> None:
    """Tag the diagnosis in place with which hypotheses are suppressed.

    Mutates `diagnosis.suppressed_fingerprints` and
    `diagnosis.suppression_notes` so the existing hypothesis list stays
    intact and the receipts survive the suppression decision. The fixer
    reads the same fields downstream to decide what to act on.
    """
    when = now or datetime.now(tz=UTC)
    for h in diagnosis.hypotheses:
        for rule in suppress_list.rules:
            if rule_matches(rule, h, now=when):
                fp = hypothesis_fingerprint(h)
                if fp not in diagnosis.suppressed_fingerprints:
                    diagnosis.suppressed_fingerprints.append(fp)
                    diagnosis.suppression_notes[fp] = describe_rule(rule)
                break  # first matching rule wins, audit-friendly


def active_hypotheses(diagnosis: DiagnosisReport) -> list[RootCauseHypothesis]:
    """Return only the hypotheses that survived suppression."""
    suppressed = set(diagnosis.suppressed_fingerprints)
    return [h for h in diagnosis.hypotheses if hypothesis_fingerprint(h) not in suppressed]


def _any_path_matches(glob: str, paths: list[str]) -> bool:
    return any(fnmatch.fnmatch(p, glob) for p in paths)
