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


def get_setting(key: str, default: str = "") -> str:
    from .db import SessionLocal
    from .models import AppSetting

    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, key)
            return row.value if row and row.value else default
    except Exception:  # noqa: BLE001
        return default


def set_setting(db, key: str, value: str) -> None:
    from .models import AppSetting

    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def openai_api_key() -> str:
    from sqlalchemy import select

    from .connections import decrypt
    from .db import SessionLocal
    from .models import Connection

    try:
        with SessionLocal() as db:
            row = db.scalar(select(Connection).where(Connection.kind == "openai", Connection.owner_id.is_(None)).order_by(Connection.id.desc()))
            key = decrypt(row.config_encrypted).get("api_key", "").strip() if row else ""
    except Exception:  # noqa: BLE001
        key = ""
    return key or settings.openai_api_key.strip()


def active_provider() -> str:
    return "openai" if get_setting("llm_provider", "anthropic") == "openai" else "anthropic"


def active_model(provider: str | None = None) -> str:
    provider = provider or active_provider()
    if provider == "openai":
        return get_setting("llm_model_openai", settings.openai_model)
    return get_setting("llm_model_anthropic", settings.llm_model)


def _to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Внутренний формат диалога (как у Claude) в формат Chat Completions."""
    import json

    def part(b: dict[str, Any]) -> dict[str, Any] | None:
        if b.get("type") == "text":
            return {"type": "text", "text": b.get("text", "")}
        if b.get("type") == "image":
            src = b.get("source", {})
            return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/jpeg')};base64,{src.get('data', '')}"}}
        return None

    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
            continue
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in c if b.get("type") == "text")
            calls = [
                {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}}
                for b in c
                if b.get("type") == "tool_use"
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        extra: list[dict[str, Any]] = []
        for b in c:
            if b.get("type") == "tool_result":
                inner = b.get("content", "")
                if isinstance(inner, list):
                    text = "\n".join(x.get("text", "") for x in inner if x.get("type") == "text") or "Результат: изображение ниже"
                    extra += [p for x in inner if x.get("type") == "image" and (p := part(x))]
                else:
                    text = str(inner)
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": text})
            elif (p := part(b)) is not None:
                extra.append(p)
        if extra:
            out.append({"role": "user", "content": extra})
    return out


class OpenAIProvider:
    """GPT и совместимые API (Chat Completions) через httpx, чтобы не тянуть лишнюю библиотеку."""

    def __init__(self, api_key: str, model: str, base_url: str = "", proxy_url: str = "") -> None:
        self._key, self._model = api_key, model
        self._base = (base_url or settings.openai_base_url).rstrip("/")
        self._proxy = proxy_url

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        try:
            with httpx.Client(timeout=90.0, proxy=self._proxy or None) as client:
                resp = client.post(f"{self._base}/chat/completions", json=body, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.TransportError as exc:
            raise LLMConnectionError("Нет соединения с API модели") from exc
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("error", {}).get("message", ""))[:160]
            except Exception:  # noqa: BLE001
                pass
            raise LLMError(f"API модели ответил кодом {resp.status_code}" + (f": {detail}" if detail else ""))
        return resp.json()

    def _call(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> Step:
        import json

        body: dict[str, Any] = {"model": self._model, "max_completion_tokens": 8000, "messages": _to_openai_messages(system, messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        data = self._post(body)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        out = Step(text=(msg.get("content") or "").strip())
        if out.text:
            out.assistant_content.append({"type": "text", "text": out.text})
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            out.calls.append(ToolCall(c["id"], c["function"]["name"], args if isinstance(args, dict) else {}))
            out.assistant_content.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args if isinstance(args, dict) else {}})
        return out

    def complete(self, system: str, messages: list[dict[str, Any]]) -> str:
        out = self._call(system, messages, None)
        if not out.text:
            raise LLMError("Модель вернула пустой ответ")
        return out.text

    def step(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Step:
        return self._call(system, messages, tools)


def _api_error(exc) -> str:
    """Код и причина ответа API без лишнего: по ним видно, что не так (ключ, модель, права, регион)."""
    detail = " ".join(str(getattr(exc, "message", "") or "").split())[:200]
    return f"API модели ответил кодом {exc.status_code}" + (f": {detail}" if detail else "")


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, base_url: str = "", proxy_url: str = "") -> None:
        from anthropic import Anthropic, DefaultHttpxClient  # DefaultHttpxClient: SDK 2.x не принимает обычный httpx.Client

        http_client = DefaultHttpxClient(proxy=proxy_url, timeout=60.0) if proxy_url else None
        self._client = Anthropic(api_key=api_key, base_url=base_url or None, timeout=60.0, http_client=http_client)
        self._model = model

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        from anthropic import APIConnectionError, APIStatusError

        try:
            response = self._client.messages.create(
                model=self._model, max_tokens=8000, system=system, messages=messages
            )
        except APIStatusError as exc:
            raise LLMError(_api_error(exc)) from exc
        except APIConnectionError as exc:
            raise LLMConnectionError("Нет соединения с API модели") from exc
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            reason = {"refusal": "модель отказалась отвечать на этот материал", "max_tokens": "ответ не уместился в лимит"}.get(
                str(response.stop_reason), f"причина: {response.stop_reason}"
            )
            raise LLMError(f"Модель вернула пустой ответ ({reason})")
        return text


    def step(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Step:
        from anthropic import APIConnectionError, APIStatusError

        try:
            kwargs: dict[str, Any] = {"model": self._model, "max_tokens": 8000, "system": system, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            response = self._client.messages.create(**kwargs)
        except APIStatusError as exc:
            raise LLMError(_api_error(exc)) from exc
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

    def __init__(self, proxies: list[tuple[int, str]], on_result, api_key: str, make=None) -> None:
        self._key = api_key
        self._make = make or (lambda url: AnthropicProvider(api_key, active_model("anthropic"), settings.llm_base_url, url))
        self._proxies = proxies  # [(id, url)]
        self._on_result = on_result  # (proxy_id, ok) -> None, чтобы запомнить состояние

    def _run(self, fn):
        last: LLMConnectionError | None = None
        for pid, url in self._proxies:
            provider = self._make(url)
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
    from .proxies import active_proxies, record_result

    if active_provider() == "openai":
        key = openai_api_key()
        if not key:
            return None
        model = active_model("openai")

        def make(url: str = ""):
            return OpenAIProvider(key, model, "", url)
    else:
        key = llm_api_key()
        if not key:
            return None
        model = active_model("anthropic")

        def make(url: str = ""):
            return AnthropicProvider(key, model, settings.llm_base_url, url)

    proxies = active_proxies()
    if proxies:
        return FailoverProvider(proxies, record_result, key, make)
    return make()
