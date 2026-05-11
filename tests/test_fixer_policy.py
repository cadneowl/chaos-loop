"""Tests for the fixer's deterministic policy: decision tree + denylist + CODEOWNERS."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.fixer.policy import (
    Codeowners,
    PathDenylist,
    decide_action,
)
from shared.contracts import FixAction

# ---------------------------------------------------------------------------- #
# Decision tree                                                                #
# ---------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fix_class,confidence,expected",
    [
        # Low confidence -> NONE regardless of class
        ("missing-retry", 0.4, FixAction.NONE),
        ("working-as-intended", 0.4, FixAction.NONE),
        # working-as-intended -> DOC_ONLY
        ("working-as-intended", 0.9, FixAction.DOC_ONLY),
        # config / image-policy -> CONFIG_CHANGE
        ("config-change", 0.7, FixAction.CONFIG_CHANGE),
        ("image-policy", 0.6, FixAction.CONFIG_CHANGE),
        # Code-fix classes -> CODE_PATCH
        ("missing-retry", 0.8, FixAction.CODE_PATCH),
        ("missing-timeout", 0.8, FixAction.CODE_PATCH),
        ("missing-circuit-breaker", 0.8, FixAction.CODE_PATCH),
        ("missing-fallback", 0.8, FixAction.CODE_PATCH),
        ("auth-control-gap", 0.8, FixAction.CODE_PATCH),
        ("secret-handling", 0.8, FixAction.CODE_PATCH),
        ("test-gap", 0.8, FixAction.CODE_PATCH),
        ("code-patch", 0.8, FixAction.CODE_PATCH),
        # Unknown class -> NONE (don't silently mis-route)
        ("totally-made-up", 0.95, FixAction.NONE),
    ],
)
def test_decide_action(fix_class: str, confidence: float, expected: FixAction) -> None:
    assert decide_action(fix_class, confidence) == expected


def test_decide_action_respects_custom_threshold() -> None:
    # With a stricter threshold, even high-confidence code-patches get blocked.
    assert decide_action("missing-retry", 0.7, min_confidence=0.9) == FixAction.NONE
    # And with a looser one, lower-confidence ones go through.
    assert decide_action("missing-retry", 0.4, min_confidence=0.3) == FixAction.CODE_PATCH


# ---------------------------------------------------------------------------- #
# PathDenylist                                                                 #
# ---------------------------------------------------------------------------- #


def test_denylist_blocks_github_dir() -> None:
    dl = PathDenylist()
    assert dl.is_denied(".github/workflows/ci.yml")
    assert dl.is_denied(".github/CODEOWNERS")


def test_denylist_blocks_secrets() -> None:
    dl = PathDenylist()
    assert dl.is_denied("secrets/db_password.yaml")
    assert dl.is_denied("config/secrets.yaml")
    assert dl.is_denied("infra/keys/server.pem")


def test_denylist_allows_normal_paths() -> None:
    dl = PathDenylist()
    assert not dl.is_denied("src/main.py")
    assert not dl.is_denied("services/cart/handler.py")
    assert not dl.is_denied("README.md")


def test_denylist_handles_backslash_paths() -> None:
    """On Windows, callers may pass backslash separators — we normalize before matching."""
    dl = PathDenylist()
    assert dl.is_denied(".github\\workflows\\ci.yml")
    assert not dl.is_denied("src\\main.py")


def test_denylist_custom_patterns() -> None:
    dl = PathDenylist(patterns=("custom/**",))
    assert dl.is_denied("custom/anything.txt")
    assert not dl.is_denied("src/main.py")
    # Default patterns NOT applied when overridden.
    assert not dl.is_denied(".github/workflows/ci.yml")


def test_denylist_reasons() -> None:
    dl = PathDenylist()
    reasons = dl.reasons([".github/foo", "src/ok.py", "secrets/db"])
    assert len(reasons) == 2
    assert any(".github/foo" in r for r in reasons)
    assert any("secrets/db" in r for r in reasons)


# ---------------------------------------------------------------------------- #
# Codeowners                                                                   #
# ---------------------------------------------------------------------------- #


_SAMPLE_CODEOWNERS = """
# Comments are ignored

*           @default-team
/src/auth/  @security-team
*.tf        @infra-team
/docs/      @docs-team @writers
"""


def test_codeowners_parses_lines() -> None:
    co = Codeowners.parse(_SAMPLE_CODEOWNERS)
    assert len(co.rules) == 4
    assert co.rules[0].pattern == "*"
    assert co.rules[0].owners == ("@default-team",)


def test_codeowners_default_rule_applies() -> None:
    co = Codeowners.parse(_SAMPLE_CODEOWNERS)
    # No specific rule matches a top-level random file -> default.
    assert co.owners_for("README.md") == ("@default-team",)


def test_codeowners_specific_rule_overrides_default() -> None:
    co = Codeowners.parse(_SAMPLE_CODEOWNERS)
    # /src/auth/ rule should match anything under src/auth/.
    assert co.owners_for("src/auth/login.py") == ("@security-team",)


def test_codeowners_last_match_wins() -> None:
    """GitHub CODEOWNERS: the LAST matching rule wins, not the first."""
    text = """
*           @team-a
*.py        @team-b
"""
    co = Codeowners.parse(text)
    assert co.owners_for("foo.py") == ("@team-b",)
    assert co.owners_for("foo.md") == ("@team-a",)


def test_codeowners_directory_pattern() -> None:
    co = Codeowners.parse(_SAMPLE_CODEOWNERS)
    assert co.owners_for("docs/getting-started.md") == ("@docs-team", "@writers")


def test_codeowners_is_owned_by() -> None:
    co = Codeowners.parse(_SAMPLE_CODEOWNERS)
    assert co.is_owned_by("src/auth/login.py", "@security-team")
    assert not co.is_owned_by("src/auth/login.py", "@default-team")
    assert co.is_owned_by("README.md", "@default-team")


def test_codeowners_from_repo_handles_missing_file(tmp_path: Path) -> None:
    co = Codeowners.from_repo(tmp_path)
    assert co.rules == []
    assert co.owners_for("anything") == ()


def test_codeowners_from_repo_reads_github_dir(tmp_path: Path) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "CODEOWNERS").write_text("/src/  @s-team\n", encoding="utf-8")
    co = Codeowners.from_repo(tmp_path)
    assert co.owners_for("src/main.py") == ("@s-team",)


def test_codeowners_ignores_comments_and_blanks() -> None:
    text = """
# leading comment

*.py @team   # trailing comment

# another
"""
    co = Codeowners.parse(text)
    # Just one valid rule.
    assert len(co.rules) == 1
    assert co.rules[0].pattern == "*.py"
