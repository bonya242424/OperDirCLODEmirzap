"""Заменяемый слой модели ИИ.

Остальной код работает только с LLMProvider. Чтобы подключить другую модель,
достаточно написать класс с методом complete() и вернуть его из get_provider().
"""

from typing import Protocol

from .config import settings


class LLMProvider(Protocol):
    def complete(self, system: str, messages: list[dict[str, str]]) -> str: ...


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


def get_provider() -> LLMProvider | None:
    if not settings.anthropic_api_key.strip():
        return None
    return AnthropicProvider(settings.anthropic_api_key, settings.llm_model, settings.llm_base_url)
