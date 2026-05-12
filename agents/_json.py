"""
Robust JSON-from-LLM extraction.

The three Claude-backed strategies (hypothesizer, diagnoser, fixer) all need to
pull a JSON value from a model's final text, which may include surrounding
prose, code fences, or a single object instead of the expected array. This
module is the one place that lives.

The helpers do not validate against Pydantic — callers do that themselves, so
they can choose between drop-bad-items and fail-on-any-bad-item semantics.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json_blob(text: str) -> str | None:
    """Find the JSON value in `text`. Returns the substring, or None.

    Stripping order:
        1. ```...``` fence (with or without `json` tag) wins if present
        2. Otherwise, take from the earliest of ``[`` or ``{`` to end of input

    We do not attempt to parse-and-truncate — callers feed the returned slice
    to ``json.loads`` which already errors clearly on trailing garbage.
    """
    if not text or not text.strip():
        return None
    fence = _FENCE.search(text)
    if fence:
        return fence.group(1).strip()
    indices = [(text.find(ch), ch) for ch in "[{"]
    valid = [(i, ch) for i, ch in indices if i != -1]
    if not valid:
        return None
    valid.sort()
    return text[valid[0][0]:].strip()


def parse_json_list(text: str) -> list[dict[str, Any]]:
    """Pull a JSON array of objects from `text`.

    Robust to:
        - surrounding prose
        - ``json`` code-fence wrappers
        - a single object instead of an array (we wrap it)

    Returns an empty list when nothing parseable is found. Callers are
    responsible for validating each item.
    """
    blob = extract_json_blob(text)
    if blob is None:
        return []
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Pull a single JSON object from `text`. Returns None if missing/invalid.

    A bare array at the top level returns None — use ``parse_json_list`` for
    that shape.
    """
    blob = extract_json_blob(text)
    if blob is None:
        return None
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None
