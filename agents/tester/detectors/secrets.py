"""Detector for hardcoded-looking secrets.

This one's heuristic — high false-positive rate by design (better to flag
something innocuous than to miss a real key). Confidence is correspondingly
low so the orchestrator's downstream filtering can deprioritize it.
"""

from __future__ import annotations

import re

from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.tester.detectors._base import Issue

# Match assignments that LOOK like secrets:
#   API_KEY = "sk_live_xxxxxxxxxxxx"
#   password = 'hunter2hunter2'
#   AWS_SECRET = "wJalr..."
# And explicitly EXCLUDE patterns that load from env / config (those are fine):
#   API_KEY = os.environ.get("API_KEY")
#   password = config.get("password")
_SECRET_NAME = r"(?:[A-Z_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASS)|password|secret|token)"
_LITERAL = r"['\"]([A-Za-z0-9_+/=\-]{12,})['\"]"
_HARDCODED_SECRET = re.compile(rf"\b{_SECRET_NAME}\s*[=:]\s*{_LITERAL}")
_LOOKS_LOADED_FROM_ENV = re.compile(
    r"(?:os\.environ|getenv|config\.|settings\.|Field\(|os\.getenv|env\.|secrets\.|"
    r"keyring\.|vault\.|kms\.|getpass)"
)


def _describe_pattern(literal: str) -> str:
    """Generic shape description: ``sk_live_*``, ``hex64``, ``base64-like``, etc.

    Never returns the secret material itself. Length is reported separately by
    the caller so reviewers know how serious the match looks.
    """
    if literal[:8].lower() in ("sk_live_", "sk_test_", "pk_live_", "pk_test_"):
        return f"{literal[:8].lower()}* prefix (likely Stripe-style API key)"
    if all(c in "0123456789abcdefABCDEF" for c in literal):
        return f"hex{len(literal)} (likely token/digest)"
    if all(c in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/=_-" for c in literal):
        return "base64/url-safe string"
    return "high-entropy string"


class HardcodedSecretDetector:
    """Flag string literals that look like API keys or passwords assigned to a
    secret-suggestive name. Skips lines that also reference env-loading helpers.
    """

    name = "hardcoded-secret"

    def find(self, code: TargetCodeReader) -> list[Issue]:
        out: list[Issue] = []
        for path in code.list_files("**/*.py"):
            try:
                text = code.read_file(path)
            except Exception:
                continue
            # Test fixtures (which routinely contain placeholder "secrets")
            # are filtered upstream by TargetCodeReader's default ignore segments.
            for line_num, line in enumerate(text.splitlines(), start=1):
                # Skip comments — they often contain example patterns that
                # match the detector's own regex (we get hits on documentation
                # otherwise). The detector's own source is the canonical case.
                if line.lstrip().startswith("#"):
                    continue
                m = _HARDCODED_SECRET.search(line)
                if not m:
                    continue
                if _LOOKS_LOADED_FROM_ENV.search(line):
                    continue
                # Important: do NOT embed the literal in `detail` — it flows
                # through to `Hypothesis.rationale`, the database, and logs.
                # An 8-char prefix was enough to confirm `sk_live_*` etc.; we
                # publish the pattern description instead.
                literal = m.group(1)
                pattern = _describe_pattern(literal)
                out.append(
                    Issue(
                        file=path,
                        line=line_num,
                        snippet=line.strip(),
                        detail=f"matched {pattern} ({len(literal)} chars)",
                    )
                )
        return out
