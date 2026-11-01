from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from tests.conftest import login


def _setup(client, owner, monkeypatch, bitrix_id=7):
    monkeypatch.setattr(settings, "bitrix24_webhook_url", "https://x.bitrix24.ru/rest/1/SECRETHOOK/")
    uid = client.get("/api/users", headers=owner).json()[0]["id"]
    assert client.patch(f"/api/users/{uid}", headers=owner, json={"bitrix_user_id": bitrix_id}).status_code == 200


def _iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _fake(pages):
    calls = []

    def post(url, data=None, timeout=None):
        calls.append(data)
        page = pages[len(calls) - 1]
        return httpx.Response(200, json=page, request=httpx.Request("POST", url))

    return post, calls


def test_sync_counts_overdue_and_pagination(client, owner, monkeypatch):
    _setup(client, owner, monkeypatch)
    pages = [
        {"result": {"tasks": [
            {"id": "1", "title": "Просрочена", "status": "2", "deadline": _iso(-2), "responsibleId": "7"},
            {"id": "2", "title": "Завтра", "status": "3", "deadline": _iso(1), "responsibleId": "7"},
        ]}, "next": 50},
        {"result": {"tasks": [
            {"id": "3", "title": "Без срока", "status": "2", "deadline": None, "responsibleId": "7"},
            {"id": "4", "title": "Закрыта", "status": "5", "deadline": _iso(5), "responsibleId": "7"},
        ]}},
    ]
    post, calls = _fake(pages)
    monkeypatch.setattr(httpx, "post", post)
    assert client.post("/api/tasks/sync", headers=owner).json() == {"open_tasks": 3}
    assert [c["start"] for c in calls] == [0, 50]
    data = client.get("/api/tasks", headers=owner).json()
    assert (data["total_open"], data["overdue"], data["due_soon"], data["no_deadline"]) == (3, 1, 1, 1)
    assert [t["title"] for t in data["items"]] == ["Просрочена", "Завтра", "Без срока"]
    assert data["people"][0]["overdue"] == 1
    item = client.post("/api/integrations/bitrix24/check", headers=owner).json()
    assert item["last_sync_at"] is not None


def test_disappeared_task_is_closed_but_failed_sync_changes_nothing(client, owner, monkeypatch):
    _setup(client, owner, monkeypatch)
    one = {"result": {"tasks": [{"id": "1", "title": "A", "status": "2", "deadline": _iso(2), "responsibleId": "7"}]}}
    monkeypatch.setattr(httpx, "post", _fake([one])[0])
    client.post("/api/tasks/sync", headers=owner)

    def down(url, data=None, timeout=None):
        raise httpx.ConnectError(f"boom {url}")

    monkeypatch.setattr(httpx, "post", down)
    r = client.post("/api/tasks/sync", headers=owner)
    assert r.status_code == 502 and "SECRETHOOK" not in r.text
    assert client.get("/api/tasks", headers=owner).json()["total_open"] == 1

    empty = {"result": {"tasks": []}}
    monkeypatch.setattr(httpx, "post", _fake([empty])[0])
    client.post("/api/tasks/sync", headers=owner)
    assert client.get("/api/tasks", headers=owner).json()["total_open"] == 0


def test_sync_needs_webhook_and_mapped_users(client, owner, monkeypatch):
    monkeypatch.setattr(settings, "bitrix24_webhook_url", "")
    assert client.post("/api/tasks/sync", headers=owner).status_code == 502
    monkeypatch.setattr(settings, "bitrix24_webhook_url", "https://x.bitrix24.ru/rest/1/H/")
    r = client.post("/api/tasks/sync", headers=owner)
    assert r.status_code == 502 and "сотрудник" in r.json()["detail"]
    assert client.get("/api/tasks").status_code == 401


def test_bad_response_shape(client, owner, monkeypatch):
    _setup(client, owner, monkeypatch)
    monkeypatch.setattr(httpx, "post", _fake([{"error": "ACCESS_DENIED"}])[0])
    r = client.post("/api/tasks/sync", headers=owner)
    assert r.status_code == 502 and "права" in r.json()["detail"]
