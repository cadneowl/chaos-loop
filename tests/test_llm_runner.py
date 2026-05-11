"""Tests for agents._llm — the universal tool-loop runner.

Most tests use a fake litellm injected via monkeypatch — no real LLM calls.
A separate test gated on RUN_LIVE_LLM=1 exercises a real Ollama call so we know
the wiring works against a real provider.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from agents._llm import (
    LLMTool,
    complete_with_tools,
    resolve_model,
)

# ---------------------------------------------------------------------------- #
# resolve_model                                                                #
# ---------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "given,expected",
    [
        ("claude-opus-4-7", "anthropic/claude-opus-4-7"),
        ("anthropic/claude-opus-4-7", "anthropic/claude-opus-4-7"),
        ("gpt-4", "openai/gpt-4"),
        ("openai/gpt-4o", "openai/gpt-4o"),
        ("qwen2.5-coder:14b", "ollama/qwen2.5-coder:14b"),
        ("llama3:70b", "ollama/llama3:70b"),
        ("ollama/qwen2.5-coder:14b", "ollama/qwen2.5-coder:14b"),
        # Unknown bare model names pass through (LiteLLM will error clearly).
        ("totally-made-up-model", "totally-made-up-model"),
    ],
)
def test_resolve_model(given: str, expected: str) -> None:
    assert resolve_model(given) == expected


# ---------------------------------------------------------------------------- #
# Helpers to build a fake litellm response                                     #
# ---------------------------------------------------------------------------- #


def _fake_response(*, content: str | None = None, tool_calls: list[dict] | None = None,
                   cost: float | None = None) -> Any:
    """Build something that quacks like a litellm ModelResponse."""
    msg = SimpleNamespace(
        content=content,
        tool_calls=[
            SimpleNamespace(
                id=tc["id"],
                function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
            )
            for tc in (tool_calls or [])
        ] or None,
    )
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        _hidden_params={"response_cost": cost} if cost is not None else {},
    )
    return resp


class _FakeLiteLLM:
    """Stub for litellm.acompletion that returns canned responses in sequence."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeLiteLLM ran out of canned responses")
        return self._responses.pop(0)


# ---------------------------------------------------------------------------- #
# Single-turn (no tools)                                                       #
# ---------------------------------------------------------------------------- #


def test_single_turn_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM([_fake_response(content="Hello, world.")])
    monkeypatch.setattr("litellm.acompletion", fake.acompletion)

    result = asyncio.run(
        complete_with_tools(model="claude-opus-4-7", system="be brief", user="hi")
    )
    assert result.final_text == "Hello, world."
    assert result.turns == 1
    assert result.tool_calls == []
    assert result.stopped_reason == "no_tool_calls"
    # Model name was prefix-resolved.
    assert fake.calls[0]["model"] == "anthropic/claude-opus-4-7"


# ---------------------------------------------------------------------------- #
# Tool loop                                                                    #
# ---------------------------------------------------------------------------- #


def test_tool_loop_with_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM([
        # Turn 1: model calls the tool
        _fake_response(
            content=None,
            tool_calls=[{
                "id": "call_1",
                "name": "add",
                "arguments": json.dumps({"a": 7, "b": 5}),
            }],
        ),
        # Turn 2: model emits final text
        _fake_response(content="The answer is 12."),
    ])
    monkeypatch.setattr("litellm.acompletion", fake.acompletion)

    captured: list[dict] = []

    async def add(args: dict) -> str:
        captured.append(args)
        return str(args["a"] + args["b"])

    tool = LLMTool(
        name="add",
        description="Add two integers.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        handler=add,
    )

    result = asyncio.run(
        complete_with_tools(model="qwen2.5-coder:14b", system="s", user="u", tools=[tool])
    )

    assert result.final_text == "The answer is 12."
    assert result.turns == 2
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "add"
    assert result.tool_calls[0].result == "12"
    assert captured == [{"a": 7, "b": 5}]


def test_tool_loop_handles_unknown_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model invents a tool that wasn't registered; trace records the error."""
    fake = _FakeLiteLLM([
        _fake_response(
            tool_calls=[{
                "id": "call_1",
                "name": "doesnt_exist",
                "arguments": "{}",
            }],
        ),
        _fake_response(content="ok, gave up."),
    ])
    monkeypatch.setattr("litellm.acompletion", fake.acompletion)

    result = asyncio.run(
        complete_with_tools(model="x/y", system="s", user="u", tools=[])
    )
    assert result.tool_calls[0].is_error
    assert "unknown tool" in result.tool_calls[0].result


def test_tool_loop_handles_handler_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool handler raises; trace captures it as an error string sent to the model."""
    fake = _FakeLiteLLM([
        _fake_response(
            tool_calls=[{"id": "call_1", "name": "boom", "arguments": "{}"}],
        ),
        _fake_response(content="recovered"),
    ])
    monkeypatch.setattr("litellm.acompletion", fake.acompletion)

    async def boom(_args: dict) -> str:
        raise RuntimeError("intentional")

    tool = LLMTool(
        name="boom",
        description="Always raises.",
        parameters={"type": "object", "properties": {}},
        handler=boom,
    )
    result = asyncio.run(
        complete_with_tools(model="x/y", system="s", user="u", tools=[tool])
    )
    assert result.tool_calls[0].is_error
    assert "intentional" in result.tool_calls[0].result


def test_tool_loop_handles_bad_args_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model emits malformed tool args JSON; recorded as error, not raised."""
    fake = _FakeLiteLLM([
        _fake_response(
            tool_calls=[{"id": "call_1", "name": "add", "arguments": "not json"}],
        ),
        _fake_response(content="done"),
    ])
    monkeypatch.setattr("litellm.acompletion", fake.acompletion)

    async def add(args: dict) -> str:
        return "0"

    tool = LLMTool(
        name="add", description="d", parameters={"type": "object"}, handler=add
    )
    result = asyncio.run(
        complete_with_tools(model="x/y", system="s", user="u", tools=[tool])
    )
    assert result.tool_calls[0].is_error
    assert "not JSON" in result.tool_calls[0].result


def test_tool_loop_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loop stops at max_turns even if the model would keep calling tools."""
    # Every turn, the model calls the tool again.
    fake = _FakeLiteLLM([
        _fake_response(tool_calls=[{"id": f"c{i}", "name": "noop", "arguments": "{}"}])
        for i in range(10)
    ])
    monkeypatch.setattr("litellm.acompletion", fake.acompletion)

    async def noop(_args: dict) -> str:
        return "ok"

    tool = LLMTool(
        name="noop", description="d", parameters={"type": "object"}, handler=noop
    )
    result = asyncio.run(
        complete_with_tools(
            model="x/y", system="s", user="u", tools=[tool], max_turns=3
        )
    )
    assert result.turns == 3
    assert result.stopped_reason == "max_turns_reached"


def test_tool_loop_budget_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budget exceeded -> loop stops, reason recorded."""
    fake = _FakeLiteLLM([
        _fake_response(
            tool_calls=[{"id": "c1", "name": "noop", "arguments": "{}"}],
            cost=2.0,  # one expensive turn
        ),
        _fake_response(content="should not be reached"),
    ])
    monkeypatch.setattr("litellm.acompletion", fake.acompletion)

    async def noop(_args: dict) -> str:
        return "ok"

    tool = LLMTool(
        name="noop", description="d", parameters={"type": "object"}, handler=noop
    )
    result = asyncio.run(
        complete_with_tools(
            model="x/y",
            system="s",
            user="u",
            tools=[tool],
            max_budget_usd=1.0,
        )
    )
    assert result.stopped_reason == "budget_exceeded"
    assert result.spend_usd is not None and result.spend_usd >= 1.0


# ---------------------------------------------------------------------------- #
# Live test (gated)                                                            #
# ---------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM") != "1",
    reason="set RUN_LIVE_LLM=1 + have Ollama running with qwen2.5-coder:14b",
)
def test_live_ollama_tool_call() -> None:
    """End-to-end: real Ollama, real model, real tool call. Skipped by default."""
    captured = []

    async def add(args: dict) -> str:
        captured.append(args)
        return str(int(args["a"]) + int(args["b"]))

    tool = LLMTool(
        name="add",
        description="Add two integers and return the sum.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        handler=add,
    )

    result = asyncio.run(
        complete_with_tools(
            model="ollama/qwen2.5-coder:14b",
            api_base="http://localhost:11434",
            system=(
                "You are a math assistant with an 'add' tool. Use the tool ONCE to "
                "compute the answer, then your NEXT response MUST be plain text with "
                "the final number. Do NOT call the tool a second time."
            ),
            user="What is 7 plus 5? Use the add tool, then tell me the answer in text.",
            tools=[tool],
            max_turns=8,
        )
    )

    assert any(t.name == "add" and not t.is_error for t in result.tool_calls)
    # Loop-detection saved us even if the model gets stuck — but ideally it
    # produced final text OR called add at least once with the right args.
    succeeded = "12" in result.final_text or any(
        t.arguments_parsed == {"a": 7, "b": 5} for t in result.tool_calls
    )
    assert succeeded, f"final_text={result.final_text!r}, calls={result.tool_calls}"
