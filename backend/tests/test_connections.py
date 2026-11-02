import httpx
import pytest

from tests.conftest import login


@pytest.fixture()
def two(client, owner):
    out = []
    for n in (1, 2):
        client.post(
            "/api/users",
            headers=owner,
            json={"email": f"e{n}@test.local", "password": "employee-pass-1", "full_name": f"Сотрудник {n}"},
        )
        out.append(login(client, f"e{n}@test.local", "employee-pass-1"))
    return out


def _tg(monkeypatch, status=200):
    monkeypatch.setattr(
        httpx, "get", lambda url, headers=None, timeout=None: httpx.Response(status, json={}, request=httpx.Request("GET", url))
    )


def test_everyone_sees_everything_but_secrets_stay_hidden(client, two, monkeypatch):
    _tg(monkeypatch)
    a, b = two
    body = {"kind": "telegram", "scope": "personal", "title": "Мой ТГ", "config": {"bot_token": "123:SECRET_TOKEN"}}
    created = client.post("/api/connections", headers=a, json=body)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "ok"
    client.post("/api/connections", headers=b, json={**body, "title": "ТГ второго", "config": {"bot_token": "9:OTHER"}})
    client.post("/api/connections", headers=a, json={**body, "scope": "company", "title": "ТГ компании"})

    seen = client.get("/api/connections", headers=b)
    assert len(seen.json()) == 3
    assert "SECRET_TOKEN" not in seen.text and "OTHER" not in seen.text
    by_title = {c["title"]: c for c in seen.json()}
    assert by_title["Мой ТГ"]["scope"] == "personal"
    assert by_title["Мой ТГ"]["owner_name"] == "Сотрудник 1"
    assert by_title["Мой ТГ"]["secrets_set"] == ["bot_token"]
    assert by_title["ТГ компании"]["scope"] == "company"
    # Второй сотрудник не может менять чужое личное подключение.
    assert by_title["Мой ТГ"]["can_edit"] is False
    assert client.delete(f"/api/connections/{by_title['Мой ТГ']['id']}", headers=b).status_code == 403
    assert client.delete(f"/api/connections/{by_title['ТГ второго']['id']}", headers=b).status_code == 204


def test_owner_can_edit_any_and_empty_secret_keeps_old(client, owner, two, monkeypatch):
    _tg(monkeypatch)
    a, _ = two
    cid = client.post(
        "/api/connections", headers=a, json={"kind": "telegram", "config": {"bot_token": "1:KEEP"}}
    ).json()["id"]
    r = client.patch(f"/api/connections/{cid}", headers=owner, json={"title": "Новое имя", "config": {"bot_token": ""}})
    assert r.status_code == 200 and r.json()["title"] == "Новое имя"
    assert r.json()["secrets_set"] == ["bot_token"]
    assert r.json()["status"] == "ok"


def test_validation_and_failed_check(client, two, monkeypatch):
    a, _ = two
    assert client.post("/api/connections", headers=a, json={"kind": "nope"}).status_code == 422
    assert client.post("/api/connections", headers=a, json={"kind": "telegram", "config": {"x": "1"}}).status_code == 422
    empty = client.post("/api/connections", headers=a, json={"kind": "telegram"}).json()
    assert empty["status"] == "not_configured"
    _tg(monkeypatch, 401)
    bad = client.post("/api/connections", headers=a, json={"kind": "telegram", "config": {"bot_token": "1:BAD"}}).json()
    assert bad["status"] == "error" and "BAD" not in bad["message"]


def test_sheet_id_from_link():
    from app.connections import spreadsheet_id

    assert spreadsheet_id("https://docs.google.com/spreadsheets/d/ABC123/edit?usp=sharing") == "ABC123"
    assert spreadsheet_id("ABC123") == "ABC123"


def test_requires_login(client):
    assert client.get("/api/connections").status_code == 401


def test_model_key_from_interface(client, owner, monkeypatch):
    from app.llm import get_provider, llm_api_key

    monkeypatch.setattr("app.config.settings.anthropic_api_key", "")
    monkeypatch.setattr("app.connections.check_connection", lambda kind, cfg: __import__("app.integrations.checks", fromlist=["x"]).CheckResult("ok", "ok"))
    assert llm_api_key() == "" and get_provider() is None
    r = client.post("/api/connections", headers=owner, json={"kind": "anthropic", "scope": "personal", "config": {"api_key": "sk-ant-secret-123"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["scope"] == "company" and body["secrets_set"] == ["api_key"] and "secret-123" not in r.text
    assert llm_api_key() == "sk-ant-secret-123" and get_provider() is not None
    assert client.post("/api/connections", headers=owner, json={"kind": "anthropic", "config": {"api_key": "sk-ant-x"}}).status_code == 409
    client.delete(f"/api/connections/{body['id']}", headers=owner)
    assert llm_api_key() == ""


def test_model_key_only_for_owner(client, owner):
    from tests.conftest import login

    client.post("/api/users", headers=owner, json={"email": "w@test.local", "password": "worker-pass-1234", "full_name": "Сотрудник"})
    h = login(client, "w@test.local", "worker-pass-1234")
    assert client.post("/api/connections", headers=h, json={"kind": "anthropic", "config": {"api_key": "sk-ant-x"}}).status_code == 403
