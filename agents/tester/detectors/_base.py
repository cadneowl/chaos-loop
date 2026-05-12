"""Detector Protocol + helpers shared across concrete detectors."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from agents.diagnostician.tools.code_reader import TargetCodeReader


@dataclass(frozen=True)
class Issue:
    """A pattern match: where it is + what it looks like + why it matters.

    Detectors return these; templates turn them into Hypothesis objects.
    """

    file: str
    line: int
    snippet: str  # the actual matched text, untruncated
    detail: str = ""  # detector-specific context (e.g., function name)


class Detector(Protocol):
    """Something that scans the target and emits ``Issue`` objects."""

    name: str

    def find(self, code: TargetCodeReader) -> list[Issue]: ...


def slug(text: str, max_len: int = 24) -> str:
    """Deterministic short slug for hypothesis ids. Lowercase, hyphen-only."""
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:max_len]
    return h


def hypothesis_id(detector_name: str, issue: Issue) -> str:
    """Deterministic id matching ``^h-[0-9a-z\\-]{1,64}$`` per the contract.

    Format: ``h-<detector>-<8-char-hash-of-file:line>``. Always under 64 chars
    even when paths are long.
    """
    short = slug(f"{issue.file}:{issue.line}", 8)
    name = detector_name.replace("_", "-").lower()
    return f"h-{name}-{short}"
