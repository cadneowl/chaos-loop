"""Token + tool-call telemetry surfaces from `complete_with_tools` to the
harness invocation log.

Tests the helper extractors in isolation + the contextvar-driven attribution
path. We never call a real LLM here — `_extract_tokens` and
`_tool_record_from_trace` are pure functions; the attribution path is
covered by harness-level tests with a mock current invocation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents._harness import (
    AgentInvocation,
    ToolCallRecord,
    _current_invocation,
    record_llm_tokens,
    record_llm_tool_calls,
)
from agents._llm import (
    ToolCallTrace,
    _extract_tokens,
    _safe_int,
    _tool_record_from_trace,
)

# --------------------------------------------------------------------------- #
# _extract_tokens                                                             #
# --------------------------------------------------------------------------- #


def test_extract_tokens_pulls_from_usage_object() -> None:
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=42, completion_tokens=18))
    assert _extract_tokens(resp) == (42, 18)


def test_extract_tokens_returns_none_when_usage_missing() -> None:
    """Self-hosted Ollama omits usage entirely."""
    resp = SimpleNamespace()  # no `usage` attr
    assert _extract_tokens(resp) == (None, None)


def test_extract_tokens_returns_none_for_partial_usage() -> None:
    """When only one of prompt/completion is reported, the other stays None."""
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=42, completion_tokens=None))
    assert _extract_tokens(resp) == (42, None)


def test_extract_tokens_handles_string_values() -> None:
    """Some providers return numbers as strings; we coerce."""
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens="100", completion_tokens="50"))
    assert _extract_tokens(resp) == (100, 50)


def test_extract_tokens_drops_unparseable_values() -> None:
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens="not-a-number", completion_tokens=10))
    assert _extract_tokens(resp) == (None, 10)


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), (42, 42), ("100", 100), ("garbage", None), (3.14, 3)],
)
def test_safe_int(value, expected) -> None:
    assert _safe_int(value) == expected


# --------------------------------------------------------------------------- #
# _tool_record_from_trace                                                     #
# --------------------------------------------------------------------------- #


def test_tool_record_preserves_string_arguments() -> None:
    trace = ToolCallTrace(
        name="read_file",
        arguments_raw='{"path": "src/x.py"}',
        arguments_parsed={"path": "src/x.py"},
        result="def foo(): pass",
    )
    rec = _tool_record_from_trace(trace)
    assert rec.name == "read_file"
    assert rec.arguments == '{"path": "src/x.py"}'
    assert rec.result_preview == "def foo(): pass"
    assert rec.is_error is False


def test_tool_record_truncates_long_results() -> None:
    """Tool results can be megabyte-sized; the audit log gets a preview only."""
    long_result = "x" * 5000
    trace = ToolCallTrace(
        name="grep",
        arguments_raw='{"pattern": "TODO"}',
        arguments_parsed={"pattern": "TODO"},
        result=long_result,
    )
    rec = _tool_record_from_trace(trace)
    assert len(rec.result_preview) < len(long_result)
    assert rec.result_preview.endswith("…(truncated)")


def test_tool_record_serializes_dict_arguments() -> None:
    """Arguments are normalized to a string for storage."""
    trace = ToolCallTrace(
        name="x",
        arguments_raw={"a": 1, "b": "two"},  # type: ignore[arg-type]
        arguments_parsed={"a": 1, "b": "two"},
        result="ok",
    )
    rec = _tool_record_from_trace(trace)
    assert isinstance(rec.arguments, str)
    assert "a" in rec.arguments
    assert "two" in rec.arguments


def test_tool_record_marks_errors() -> None:
    trace = ToolCallTrace(
        name="bad_tool",
        arguments_raw="{}",
        arguments_parsed={},
        result="error: thing exploded",
        is_error=True,
    )
    rec = _tool_record_from_trace(trace)
    assert rec.is_error is True


# --------------------------------------------------------------------------- #
# Harness attribution: tokens + tool calls                                    #
# --------------------------------------------------------------------------- #


def test_record_llm_tokens_accumulates() -> None:
    inv = AgentInvocation(agent="x", method="y", started_at_ms=0)
    token = _current_invocation.set(inv)
    try:
        record_llm_tokens(100, 50)
        record_llm_tokens(200, 75)
    finally:
        _current_invocation.reset(token)
    assert inv.prompt_tokens == 300
    assert inv.completion_tokens == 125


def test_record_llm_tokens_handles_partial_attribution() -> None:
    """A turn that reports only prompt tokens leaves completion as None."""
    inv = AgentInvocation(agent="x", method="y", started_at_ms=0)
    token = _current_invocation.set(inv)
    try:
        record_llm_tokens(100, None)
    finally:
        _current_invocation.reset(token)
    assert inv.prompt_tokens == 100
    assert inv.completion_tokens is None


def test_record_llm_tokens_accumulates_legitimate_zero() -> None:
    """A turn that legitimately reports zero completion tokens (e.g., a
    streaming response that emits only a tool call with no content tokens)
    must flip the field from None to 0 — not be silently dropped."""
    inv = AgentInvocation(agent="x", method="y", started_at_ms=0)
    token = _current_invocation.set(inv)
    try:
        record_llm_tokens(50, 0)
    finally:
        _current_invocation.reset(token)
    assert inv.prompt_tokens == 50
    assert inv.completion_tokens == 0  # NOT None


def test_record_llm_tokens_with_no_invocation_is_noop() -> None:
    assert _current_invocation.get() is None
    record_llm_tokens(100, 50)  # must not raise


def test_record_llm_tool_calls_extends_invocation_list() -> None:
    inv = AgentInvocation(agent="x", method="y", started_at_ms=0)
    token = _current_invocation.set(inv)
    try:
        record_llm_tool_calls(
            [ToolCallRecord(name="a", arguments="{}", result_preview="ok")]
        )
        record_llm_tool_calls(
            [
                ToolCallRecord(name="b", arguments="{}", result_preview="ok"),
                ToolCallRecord(name="c", arguments="{}", result_preview="err", is_error=True),
            ]
        )
    finally:
        _current_invocation.reset(token)
    assert [t.name for t in inv.tool_calls] == ["a", "b", "c"]
    assert inv.tool_calls[2].is_error is True


def test_record_llm_tool_calls_empty_list_is_noop() -> None:
    inv = AgentInvocation(agent="x", method="y", started_at_ms=0)
    token = _current_invocation.set(inv)
    try:
        record_llm_tool_calls([])
    finally:
        _current_invocation.reset(token)
    assert inv.tool_calls == []
