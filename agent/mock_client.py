"""Deterministic OpenAI-compatible clients used for offline tests and demos."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def final_turn(text: str) -> AssistantTurn:
    return AssistantTurn(content=text)


def tool_call_turn(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "mock-call-1",
) -> AssistantTurn:
    return AssistantTurn(
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ]
    )


def _response(turn: AssistantTurn) -> Any:
    message = SimpleNamespace(
        content=turn.content,
        tool_calls=turn.tool_calls or None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return " ".join(part for part in parts if part)
    return ""


class ScriptedMockClient:
    """Return a fixed sequence of assistant turns and record every request."""

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.turns:
            turn = self.turns.pop(0)
        else:
            turn = final_turn("Mock script exhausted.")
        return _response(turn)


class StableMockClient(ScriptedMockClient):
    """A deterministic model that never performs network requests."""

    def __init__(self) -> None:
        super().__init__([])

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = _latest_user_text(kwargs.get("messages", []))
        preview = text.strip().replace("\n", " ")[:500]
        if not preview:
            preview = "(empty request)"
        return _response(
            final_turn(
                "Mock model response: received "
                f"{preview}. Tool execution is tested separately by "
                "`python -m agent.selftest`."
            )
        )


__all__ = [
    "AssistantTurn",
    "ScriptedMockClient",
    "StableMockClient",
    "final_turn",
    "tool_call_turn",
]
