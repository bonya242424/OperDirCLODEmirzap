import json

import pytest

import app.llm as llm
from tests.conftest import login


def test_settings_owner_only_and_default(client, owner):
    r = client.get("/api/llm", headers=owner)
    assert r.status_code == 200 and r.json()["provider"] == "anthropic"
    assert "gpt-5" in r.json()["suggestions"]["openai"]


def test_switch_provider_and_model(client, owner):
    r = client.put("/api/llm", headers=owner, json={"provider": "openai", "model": "gpt-4.1"})
    assert r.status_code == 200 and r.json()["provider"] == "openai" and r.json()["models"]["openai"] == "gpt-4.1"
    assert llm.active_provider() == "openai" and llm.active_model() == "gpt-4.1"
    client.put("/api/llm", headers=owner, json={"provider": "anthropic", "model": "claude-opus-5-5"})
    assert llm.active_model() == "claude-opus-5-5"


def test_bad_values_rejected(client, owner):
    assert client.put("/api/llm", headers=owner, json={"provider": "grok"}).status_code == 422
    assert client.put("/api/llm", headers=owner, json={"provider": "openai", "model": "x y; drop"}).status_code == 422


def test_openai_key_connection_owner_only_and_single(client, owner):
    r = client.post("/api/connections", headers=owner, json={"kind": "openai", "scope": "company", "config": {"api_key": "sk-test-1"}})
    assert r.status_code == 201
    assert "sk-test-1" not in json.dumps(r.json())
    assert client.post("/api/connections", headers=owner, json={"kind": "openai", "scope": "company", "config": {"api_key": "sk-test-2"}}).status_code == 409
    assert llm.openai_api_key() == "sk-test-1"


def test_convert_messages_to_openai():
    msgs = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": [{"type": "text", "text": "смотрю"}, {"type": "tool_use", "id": "t1", "name": "browser_view", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}]}]},
    ]
    out = llm._to_openai_messages("sys", msgs)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[2]["tool_calls"][0]["function"]["name"] == "browser_view"
    assert out[3]["role"] == "tool" and out[3]["tool_call_id"] == "t1"
    assert out[4]["role"] == "user" and out[4]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_openai_provider_parses_tool_calls(monkeypatch):
    p = llm.OpenAIProvider("sk-x", "gpt-5")
    seen = {}

    def post(body):
        seen.update(body)
        return {"choices": [{"message": {"content": "ок", "tool_calls": [{"id": "c1", "function": {"name": "list_tasks", "arguments": "{}"}}]}}]}

    monkeypatch.setattr(p, "_post", post)
    step = p.step("s", [{"role": "user", "content": "q"}], [{"name": "list_tasks", "description": "d", "input_schema": {"type": "object", "properties": {}}}])
    assert step.text == "ок" and step.calls[0].name == "list_tasks"
    assert step.assistant_content[1]["type"] == "tool_use" and seen["tools"][0]["function"]["name"] == "list_tasks"


def test_get_provider_uses_selected(client, owner, monkeypatch):
    client.post("/api/connections", headers=owner, json={"kind": "openai", "scope": "company", "config": {"api_key": "sk-abc"}})
    client.put("/api/llm", headers=owner, json={"provider": "openai", "model": "gpt-5-mini"})
    monkeypatch.setattr(llm, "active_proxies", lambda: [], raising=False)
    prov = llm.get_provider()
    assert isinstance(prov, llm.OpenAIProvider) or hasattr(prov, "_make")


def test_context_has_new_sections(client, owner):
    from app.context import build_context
    from app.db import SessionLocal

    with SessionLocal() as db:
        text = build_context(db)
    for head in ("## Возвраты поставщикам", "## ИИ-агенты", "## Недавние файлы"):
        assert head in text


def test_api_error_includes_reason():
    class E(Exception):
        status_code = 403
        message = "model: claude-opus-5-5 is not available for this key"

    assert "403" in llm._api_error(E()) and "not available" in llm._api_error(E())


def test_grok_provider(client, owner):
    r = client.post("/api/connections", headers=owner, json={"kind": "xai", "scope": "company", "config": {"api_key": "xai-key-1"}})
    assert r.status_code == 201 and "xai-key-1" not in r.text
    assert llm.xai_api_key() == "xai-key-1"
    s = client.put("/api/llm", headers=owner, json={"provider": "xai", "model": "grok-4"})
    assert s.status_code == 200 and s.json()["provider"] == "xai" and s.json()["has_key"]["xai"] is True
    prov = llm.get_provider()
    inner = prov if isinstance(prov, llm.OpenAIProvider) else prov._make("")
    assert isinstance(inner, llm.OpenAIProvider) and inner._base == "https://api.x.ai/v1" and inner._model == "grok-4"
