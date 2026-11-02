import httpx
import pytest

from app import llm
from app.db import SessionLocal
from app.models import Proxy
from app.proxies import active_proxies, build_url


def _add(client, owner, **kw):
    body = {"name": "P", "scheme": "http", "host": "proxy.example.com", "port": 3128, **kw}
    return client.post("/api/proxies", headers=owner, json=body)


def test_crud_and_secret_never_returned(client, owner):
    r = _add(client, owner, username="u", password="s3cret-pass")
    assert r.status_code == 201
    body = r.json()
    assert body["has_password"] is True and "s3cret" not in r.text and "password" not in body
    assert "s3cret" not in client.get("/api/proxies", headers=owner).text
    with SessionLocal() as db:
        p = db.get(Proxy, body["id"])
        assert "s3cret-pass" not in p.password_encrypted  # зашифровано
        assert build_url(p) == "http://u:s3cret-pass@proxy.example.com:3128"
    # пароль не прислали при изменении: прежний остаётся
    r = client.put(f"/api/proxies/{body['id']}", headers=owner, json={"name": "P2", "scheme": "socks5", "host": "p2.example.com", "port": 1080, "username": "u"})
    assert r.json()["has_password"] is True
    with SessionLocal() as db:
        assert build_url(db.get(Proxy, body["id"])) == "socks5://u:s3cret-pass@p2.example.com:1080"
    assert client.delete(f"/api/proxies/{body['id']}", headers=owner).status_code == 204


def test_validation(client, owner):
    assert _add(client, owner, host="http://x.com").status_code == 422
    assert _add(client, owner, host="a b").status_code == 422
    assert _add(client, owner, port=0).status_code == 422
    assert _add(client, owner, scheme="ftp").status_code == 422


def test_special_chars_in_password_are_quoted(client, owner):
    pid = _add(client, owner, username="u@x", password="p:a/ss@").json()["id"]
    with SessionLocal() as db:
        assert build_url(db.get(Proxy, pid)) == "http://u%40x:p%3Aa%2Fss%40@proxy.example.com:3128"


def test_active_proxies_order_and_disabled(client, owner):
    _add(client, owner, host="b.example.com", priority=20)
    _add(client, owner, host="a.example.com", priority=10)
    _add(client, owner, host="off.example.com", priority=1, enabled=False)
    assert [u for _, u in active_proxies()] == ["http://a.example.com:3128", "http://b.example.com:3128"]


def test_check_reports_result(client, owner, monkeypatch):
    pid = _add(client, owner).json()["id"]

    class Ok:
        def __init__(self, **kw):
            self.proxy = kw.get("proxy")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return httpx.Response(404)

    monkeypatch.setattr("app.proxies.httpx.Client", Ok)
    r = client.post(f"/api/proxies/{pid}/check", headers=owner).json()
    assert r["last_status"] == "ok" and r["last_ms"] is not None

    class Bad(Ok):
        def get(self, url):
            raise httpx.ProxyError("407")

    monkeypatch.setattr("app.proxies.httpx.Client", Bad)
    r = client.post(f"/api/proxies/{pid}/check", headers=owner).json()
    assert r["last_status"] == "error" and "логин" in r["last_message"]


def test_failover_uses_next_proxy_and_records(client, owner, monkeypatch):
    first = _add(client, owner, host="a.example.com", priority=1).json()["id"]
    second = _add(client, owner, host="b.example.com", priority=2).json()["id"]
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "k")
    used = []

    class Fake:
        def __init__(self, key, model, base_url="", proxy_url=""):
            self.url = proxy_url

        def complete(self, system, messages):
            used.append(self.url)
            if "a.example.com" in self.url:
                raise llm.LLMConnectionError("нет связи")
            return "ответ"

    monkeypatch.setattr(llm, "AnthropicProvider", Fake)
    assert llm.get_provider().complete("s", []) == "ответ"
    assert used == ["http://a.example.com:3128", "http://b.example.com:3128"]
    listed = {p["id"]: p["last_status"] for p in client.get("/api/proxies", headers=owner).json()}
    assert listed == {first: "error", second: "ok"}


def test_model_errors_are_not_retried_and_all_down(client, owner, monkeypatch):
    _add(client, owner, host="a.example.com", priority=1)
    _add(client, owner, host="b.example.com", priority=2)
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "k")
    calls = []

    class Api:
        mode = "model"

        def __init__(self, key, model, base_url="", proxy_url=""):
            pass

        def complete(self, system, messages):
            calls.append(1)
            if Api.mode == "model":
                raise llm.LLMError("API модели ответил кодом 429")
            raise llm.LLMConnectionError("нет связи")

    monkeypatch.setattr(llm, "AnthropicProvider", Api)
    with pytest.raises(llm.LLMError, match="429"):
        llm.get_provider().complete("s", [])
    assert len(calls) == 1  # на ошибке самой модели второй прокси не пробуем
    Api.mode = "net"
    with pytest.raises(llm.LLMConnectionError, match="Ни один прокси"):
        llm.get_provider().complete("s", [])


def test_no_proxies_goes_direct(client, owner, monkeypatch):
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "k")
    assert isinstance(llm.get_provider(), llm.AnthropicProvider)


def test_provider_with_proxy_builds_with_installed_sdk():
    """Регрессия: SDK anthropic 2.x отвергал обычный httpx.Client, и вся работа через прокси падала с TypeError."""
    from app.integrations.checks import check_anthropic_key
    from app.llm import AnthropicProvider

    AnthropicProvider("sk-ant-x", "m", "", "socks5://127.0.0.1:1")
    AnthropicProvider("sk-ant-x", "m", "", "http://127.0.0.1:1")
    # проверка ключа без прокси не должна падать TypeError: сеть может быть недоступна, но ответ всегда CheckResult
    assert check_anthropic_key("sk-ant-x").status in ("ok", "error", "warning", "not_configured")
