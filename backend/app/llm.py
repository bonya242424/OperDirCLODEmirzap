"""Заменяемый слой модели ИИ.

Остальной код работает только с LLMProvider. Чтобы подключить другую модель,
достаточно написать класс с методом complete() и вернуть его из get_provider().
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import settings


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Step:
    """Один ход модели: текст и/или вызовы инструментов. assistant_content возвращается в диалог как есть."""

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    assistant_content: list[dict[str, Any]] = field(default_factory=list)


class LLMProvider(Protocol):
    def complete(self, system: str, messages: list[dict[str, str]]) -> str: ...

    def step(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Step: ...


class LLMError(Exception):
    """Модель недоступна или вернула ошибку. Текст безопасен для показа пользователю."""


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, base_url: str = "") -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key, base_url=base_url or None, timeout=60.0)
        self._model = model

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        from anthropic import APIConnectionError, APIStatusError

        try:
            response = self._client.messages.create(
                model=self._model, max_tokens=2000, system=system, messages=messages
            )
        except APIStatusError as exc:
            raise LLMError(f"API модели ответил кодом {exc.status_code}") from exc
        except APIConnectionError as exc:
            raise LLMError("Нет соединения с API модели") from exc
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise LLMError("Модель вернула пустой ответ")
        return text


    def step(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Step:
        from anthropic import APIConnectionError, APIStatusError

        try:
            kwargs: dict[str, Any] = {"model": self._model, "max_tokens": 3000, "system": system, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            response = self._client.messages.create(**kwargs)
        except APIStatusError as exc:
            raise LLMError(f"API модели ответил кодом {exc.status_code}") from exc
        except APIConnectionError as exc:
            raise LLMError("Нет соединения с API модели") from exc
        out = Step()
        for block in response.content:
            if block.type == "text":
                out.text += block.text
                out.assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                out.calls.append(ToolCall(block.id, block.name, dict(block.input)))
                out.assistant_content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input)})
        out.text = out.text.strip()
        return out


def get_provider() -> LLMProvider | None:
    if not settings.anthropic_api_key.strip():
        return None
    return AnthropicProvider(settings.anthropic_api_key, settings.llm_model, settings.llm_base_url)
