from app.llm import LLMError


class FakeProvider:
    def __init__(self):
        self.calls = []

    def complete(self, system, messages):
        self.calls.append(messages)
        return f"ответ на: {messages[-1]['content']}"


def test_chat_without_key_is_503(client, owner):
    resp = client.post("/api/chat", headers=owner, json={"message": "привет"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]
    assert client.get("/api/chat/history", headers=owner).json() == []


def test_chat_roundtrip_keeps_history(client, owner, monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr("app.routers.chat.get_provider", lambda: fake)

    first = client.post("/api/chat", headers=owner, json={"message": "первый"})
    assert first.status_code == 200
    assert first.json()["reply"]["content"] == "ответ на: первый"

    client.post("/api/chat", headers=owner, json={"message": "второй"})
    assert [m["content"] for m in fake.calls[1]] == ["первый", "ответ на: первый", "второй"]

    history = client.get("/api/chat/history", headers=owner).json()
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


def test_chat_history_is_per_user(client, owner, monkeypatch):
    from .conftest import login

    monkeypatch.setattr("app.routers.chat.get_provider", lambda: FakeProvider())
    client.post("/api/chat", headers=owner, json={"message": "секрет владельца"})
    client.post("/api/users", headers=owner, json={"email": "a@test.local", "password": "password-aaa-1"})
    emp = login(client, "a@test.local", "password-aaa-1")
    assert client.get("/api/chat/history", headers=emp).json() == []


def test_failed_model_call_saves_nothing(client, owner, monkeypatch):
    class Broken:
        def complete(self, system, messages):
            raise LLMError("API модели ответил кодом 529")

    monkeypatch.setattr("app.routers.chat.get_provider", lambda: Broken())
    resp = client.post("/api/chat", headers=owner, json={"message": "привет"})
    assert resp.status_code == 502
    assert client.get("/api/chat/history", headers=owner).json() == []


def test_empty_message_rejected(client, owner):
    assert client.post("/api/chat", headers=owner, json={"message": "   "}).status_code == 422
