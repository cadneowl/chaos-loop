"""
Deterministic fixer policy: decision tree + path denylist + CODEOWNERS.

Pure functions. The agent applies these gates BEFORE invoking the cognitive
strategy, and AGAIN before returning a FixProposal — so even a misbehaving
strategy can't push us past the policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shared.contracts import FixAction

# ---------------------------------------------------------------------------- #
# Decision tree: fix_class -> action                                           #
# ---------------------------------------------------------------------------- #


# Minimum confidence on the top hypothesis to attempt any fix. Below this we
# emit action=NONE and let humans triage.
DEFAULT_MIN_CONFIDENCE = 0.5


def decide_action(
    fix_class: str,
    confidence: float,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> FixAction:
    """Map a diagnostician's fix_class + confidence to a FixAction.

    See agents/fixer/README.md decision tree for the full rationale.
    """
    if confidence < min_confidence:
        return FixAction.NONE
    if fix_class == "working-as-intended":
        return FixAction.DOC_ONLY
    if fix_class in {"config-change", "image-policy"}:
        return FixAction.CONFIG_CHANGE
    if fix_class in {
        "code-patch",
        "missing-retry",
        "missing-timeout",
        "missing-circuit-breaker",
        "missing-fallback",
        "auth-control-gap",
        "secret-handling",
        "test-gap",
    }:
        return FixAction.CODE_PATCH
    # Unknown class -> defer to humans rather than silently mis-route.
    return FixAction.NONE


# ---------------------------------------------------------------------------- #
# Path denylist                                                                #
# ---------------------------------------------------------------------------- #


DEFAULT_DENYLIST: tuple[str, ...] = (
    ".github/**",
    ".gitlab-ci.yml",
    ".gitlab/**",
    "infra/**",
    "secrets/**",
    "**/secrets.yaml",
    "**/secrets.yml",
    "**/credentials.json",
    "**/*.pem",
    "**/*.key",
    "Makefile.deploy",
)


@dataclass(frozen=True)
class PathDenylist:
    """Glob-based path denylist. is_denied(path) -> bool.

    Patterns use fnmatch syntax. `**` matches across directory separators,
    `*` matches within one segment. Paths are normalized to forward-slash
    before matching so callers don't have to worry about OS.
    """

    patterns: tuple[str, ...] = DEFAULT_DENYLIST

    def is_denied(self, path: str) -> bool:
        norm = _normalize(path)
        return any(_glob_match(pat, norm) for pat in self.patterns)

    def reasons(self, paths: list[str]) -> list[str]:
        """For each denied path, return a 'path matches pattern' explanation."""
        out: list[str] = []
        for p in paths:
            norm = _normalize(p)
            for pat in self.patterns:
                if _glob_match(pat, norm):
                    out.append(f"{p} matches {pat}")
                    break
        return out


def _normalize(path: str) -> str:
    """Forward-slash separator + drop a leading './'. Keep leading '.' (e.g. .github)."""
    norm = path.replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    return norm


def _glob_match(pattern: str, path: str) -> bool:
    """Glob match supporting `**`. fnmatch doesn't natively, so we expand it."""
    # Translate `**` to a regex that crosses directory separators.
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", ".")
    return re.fullmatch(regex, path) is not None


# ---------------------------------------------------------------------------- #
# CODEOWNERS                                                                   #
# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CodeownersRule:
    """One line of a CODEOWNERS file."""

    pattern: str
    owners: tuple[str, ...]


@dataclass
class Codeowners:
    """Parsed CODEOWNERS file. Last-matching-rule wins, per GitHub semantics."""

    rules: list[CodeownersRule] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> Codeowners:
        rules: list[CodeownersRule] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            rules.append(CodeownersRule(pattern=parts[0], owners=tuple(parts[1:])))
        return cls(rules=rules)

    @classmethod
    def from_repo(cls, repo_root: Path) -> Codeowners:
        """Look up CODEOWNERS in the usual GitHub locations. Empty if not found."""
        for rel in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            p = repo_root / rel
            if p.is_file():
                return cls.parse(p.read_text(encoding="utf-8"))
        return cls()

    def owners_for(self, path: str) -> tuple[str, ...]:
        """Return owner list for a path. Last matching rule wins."""
        norm = _normalize(path)
        matched: tuple[str, ...] = ()
        for rule in self.rules:
            pat = _codeowners_pattern_to_glob(rule.pattern)
            if _glob_match(pat, norm) or _glob_match(pat, "/" + norm):
                matched = rule.owners
        return matched

    def is_owned_by(self, path: str, owner: str) -> bool:
        return owner in self.owners_for(path)


def _codeowners_pattern_to_glob(pattern: str) -> str:
    """GitHub CODEOWNERS supports gitignore-style patterns; we map a subset.

    Specifically: a leading `/` anchors to repo root. A trailing `/` means
    "directory and everything under it". Bare names match in any directory.
    """
    p = pattern
    if p.startswith("/"):
        p = p[1:]  # we always match relative paths, so drop the anchor
    if p.endswith("/"):
        p = p + "**"
    if "/" not in p:
        # Bare name like `*.py` or `Dockerfile` — match anywhere in tree.
        p = "**/" + p
    return p
