"""
Universal LLM tool-loop runner via LiteLLM.

Why this exists: claude-agent-sdk is Anthropic-only. Switching to LiteLLM lets
the same Hypothesizer / Diagnoser / FixerStrategy code work against:
    - anthropic/claude-opus-4-7  (default; needs ANTHROPIC_API_KEY)
    - ollama/qwen2.5-coder:14b   (local; free; needs Ollama running)
    - openai/gpt-4               (or any other LiteLLM-supported provider)

The tradeoff vs the SDK: we hand-roll the tool-call loop instead of getting it
for free. That's the file you're reading.

Tools are described as Python objects with a JSON Schema and an async handler;
we translate to OpenAI-style tool specs (which Ollama and Anthropic both accept
through LiteLLM's normalization).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMTool:
    """A function the model can call. JSON Schema parameters; async handler."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the args object
    handler: Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class ToolCallTrace:
    """One model -> tool -> result triple, for telemetry / debugging."""

    name: str
    arguments_raw: str
    arguments_parsed: dict[str, Any] | None
    result: str
    is_error: bool = False


@dataclass
class CompletionResult:
    """What `complete_with_tools` returned."""

    final_text: str
    turns: int
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    spend_usd: float | None = None
    stopped_reason: str = "ok"


def resolve_model(model: str) -> str:
    """Add a provider prefix if missing.

    LiteLLM expects ``provider/model``. Bare names like ``claude-opus-4-7``
    have historically meant Anthropic in this codebase, so we keep that mapping
    so existing tests / configs don't change.
    """
    if "/" in model:
        return model
    if model.startswith(("claude-", "anthropic-")):
        return f"anthropic/{model}"
    if model.startswith(("gpt-", "o1-", "o3-")):
        return f"openai/{model}"
    if model.startswith(("qwen", "llama", "mistral", "deepseek", "phi")):
        return f"ollama/{model}"
    # Fall through unchanged; LiteLLM will error if the prefix is missing,
    # which is a clearer signal than a silent miss.
    return model


async def complete_with_tools(
    *,
    model: str,
    system: str,
    user: str,
    tools: Sequence[LLMTool] = (),
    max_turns: int = 25,
    temperature: float = 0.0,
    api_base: str | None = None,
    max_budget_usd: float | None = None,
    stop_on_repeated_tool_call: int = 3,
) -> CompletionResult:
    """
    Run a multi-turn tool-calling completion. Returns the final assistant text
    plus a per-call trace.

    Stopping conditions:
        - The model produced text and made no tool calls (natural completion)
        - max_turns reached
        - max_budget_usd reached (best-effort; only when LiteLLM exposes cost)
        - N consecutive identical tool calls (safety net for small models that
          get stuck looping; set ``stop_on_repeated_tool_call=0`` to disable)
        - LiteLLM raises (propagated)
    """
    # Lazy import so non-LLM paths (tests, dry-run) don't pay the cost.
    import litellm

    model = resolve_model(model)
    tools_by_name = {t.name: t for t in tools}
    tool_specs = [_to_openai_spec(t) for t in tools] if tools else None

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    trace: list[ToolCallTrace] = []
    final_text = ""
    spend_usd: float | None = None
    stopped_reason = "ok"
    last_call_signature: tuple[str, str] | None = None
    repeated_call_count = 0

    turn = -1
    for turn in range(max_turns):  # noqa: B007 — turn used after loop for the count
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tool_specs:
            kwargs["tools"] = tool_specs
        if api_base is not None:
            kwargs["api_base"] = api_base

        resp = await litellm.acompletion(**kwargs)

        # Cost accounting (best-effort; some providers don't report).
        cost = _extract_cost(resp)
        if cost is not None:
            spend_usd = (spend_usd or 0.0) + cost

        msg = resp.choices[0].message
        if msg.content:
            final_text = msg.content

        if not getattr(msg, "tool_calls", None):
            stopped_reason = "no_tool_calls"
            break

        # Append the assistant turn and process each tool call.
        messages.append(_assistant_message_with_tools(msg))
        for tc in msg.tool_calls:
            entry = await _execute_one_tool(tc, tools_by_name)
            trace.append(entry)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": entry.result,
                }
            )

        # Loop-detection: smaller models sometimes call the same tool with the
        # same args repeatedly instead of answering. After N consecutive identical
        # calls, give up the loop with a clear reason.
        if stop_on_repeated_tool_call > 0 and len(msg.tool_calls) == 1:
            sig = (msg.tool_calls[0].function.name, msg.tool_calls[0].function.arguments)
            if sig == last_call_signature:
                repeated_call_count += 1
                if repeated_call_count >= stop_on_repeated_tool_call:
                    stopped_reason = "repeated_tool_call"
                    log.warning(
                        "complete_with_tools: tool %s called %d times with identical args; stopping",
                        sig[0], repeated_call_count + 1,
                    )
                    break
            else:
                repeated_call_count = 0
                last_call_signature = sig
        else:
            repeated_call_count = 0
            last_call_signature = None

        if max_budget_usd is not None and spend_usd is not None and spend_usd >= max_budget_usd:
            stopped_reason = "budget_exceeded"
            log.warning(
                "complete_with_tools: spend %.4f USD >= budget %.4f USD; stopping",
                spend_usd, max_budget_usd,
            )
            break
    else:
        stopped_reason = "max_turns_reached"

    return CompletionResult(
        final_text=final_text,
        turns=turn + 1,
        tool_calls=trace,
        spend_usd=spend_usd,
        stopped_reason=stopped_reason,
    )


# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


def _to_openai_spec(tool: LLMTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _assistant_message_with_tools(msg: Any) -> dict[str, Any]:
    """Re-serialize an assistant message with tool calls into the messages-list shape.

    LiteLLM returns Pydantic-like message objects; we need plain dicts to feed back.
    """
    return {
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ],
    }


async def _execute_one_tool(
    tc: Any, tools_by_name: dict[str, LLMTool]
) -> ToolCallTrace:
    """Parse args, dispatch, capture errors. Never raises."""
    name = tc.function.name
    raw = tc.function.arguments
    tool = tools_by_name.get(name)
    if tool is None:
        return ToolCallTrace(
            name=name,
            arguments_raw=raw,
            arguments_parsed=None,
            result=f"error: unknown tool {name!r}",
            is_error=True,
        )
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        return ToolCallTrace(
            name=name,
            arguments_raw=raw,
            arguments_parsed=None,
            result=f"error: tool args were not JSON: {e}",
            is_error=True,
        )
    if not isinstance(args, dict):
        return ToolCallTrace(
            name=name,
            arguments_raw=raw,
            arguments_parsed=None,
            result=f"error: tool args must be an object, got {type(args).__name__}",
            is_error=True,
        )
    try:
        out = await tool.handler(args)
    except Exception as e:
        return ToolCallTrace(
            name=name,
            arguments_raw=raw,
            arguments_parsed=args,
            result=f"error: {type(e).__name__}: {e}",
            is_error=True,
        )
    return ToolCallTrace(
        name=name,
        arguments_raw=raw,
        arguments_parsed=args,
        result=out,
    )


def _extract_cost(resp: Any) -> float | None:
    """LiteLLM stashes the response cost in _hidden_params for many providers."""
    hidden = getattr(resp, "_hidden_params", None)
    if not hidden:
        return None
    val = hidden.get("response_cost")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
