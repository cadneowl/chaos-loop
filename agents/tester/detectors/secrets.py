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
                out.append(
                    Issue(
                        file=path,
                        line=line_num,
                        snippet=line.strip(),
                        detail=f"matched literal: {m.group(1)[:8]}…",
                    )
                )
        return out
