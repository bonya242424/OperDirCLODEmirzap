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


class LLMConnectionError(LLMError):
    """Не удалось соединиться (сеть, прокси). Только такую ошибку имеет смысл повторять через другой прокси."""


def llm_api_key() -> str:
    """Ключ модели: сначала подключение компании «Ключ Claude» из интерфейса, затем ANTHROPIC_API_KEY из .env."""
    from sqlalchemy import select

    from .connections import decrypt
    from .db import SessionLocal
    from .models import Connection

    try:
        with SessionLocal() as db:
            row = db.scalar(select(Connection).where(Connection.kind == "anthropic", Connection.owner_id.is_(None)).order_by(Connection.id.desc()))
            key = decrypt(row.config_encrypted).get("api_key", "").strip() if row else ""
    except Exception:  # noqa: BLE001 - нет таблицы или базы: берём из настроек
        key = ""
    return key or settings.anthropic_api_key.strip()


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, base_url: str = "", proxy_url: str = "") -> None:
        import httpx
        from anthropic import Anthropic

        http_client = httpx.Client(proxy=proxy_url, timeout=60.0) if proxy_url else None
        self._client = Anthropic(api_key=api_key, base_url=base_url or None, timeout=60.0, http_client=http_client)
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
            raise LLMConnectionError("Нет соединения с API модели") from exc
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
            raise LLMConnectionError("Нет соединения с API модели") from exc
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


class FailoverProvider:
    """Пробует прокси по очереди: при ошибке соединения берёт следующий, ошибки самой модели не повторяет."""

    def __init__(self, proxies: list[tuple[int, str]], on_result, api_key: str) -> None:
        self._key = api_key
        self._proxies = proxies  # [(id, url)]
        self._on_result = on_result  # (proxy_id, ok) -> None, чтобы запомнить состояние

    def _run(self, fn):
        last: LLMConnectionError | None = None
        for pid, url in self._proxies:
            provider = AnthropicProvider(self._key, settings.llm_model, settings.llm_base_url, url)
            try:
                result = fn(provider)
            except LLMConnectionError as exc:
                self._on_result(pid, False)
                last = exc
                continue
            self._on_result(pid, True)
            return result
        raise LLMConnectionError("Ни один прокси не отвечает: проверьте список в «Мой кабинет → Прокси»") from last

    def complete(self, system, messages):
        return self._run(lambda p: p.complete(system, messages))

    def step(self, system, messages, tools):
        return self._run(lambda p: p.step(system, messages, tools))


def get_provider() -> LLMProvider | None:
    key = llm_api_key()
    if not key:
        return None
    from .proxies import active_proxies, record_result

    proxies = active_proxies()
    if proxies:
        return FailoverProvider(proxies, record_result, key)
    return AnthropicProvider(key, settings.llm_model, settings.llm_base_url)
