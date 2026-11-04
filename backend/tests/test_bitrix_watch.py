from datetime import date

from tests.conftest import login

import pytest

from app import bitrix, bitrix_watch as bw
from app.db import SessionLocal
from app.models import User


def _emp(db, bitrix_id=7, name="Иван"):
    u = User(email=f"e{bitrix_id}@t.local", full_name=name, role="worker", password_hash="x", bitrix_user_id=bitrix_id)
    db.add(u)
    db.commit()


def raw(i, status=2, deadline=None, changed="2026-11-04T10:00:00+03:00", title="Задача", resp=7, closed=None):
    return {"id": str(i), "title": title, "status": str(status), "responsibleId": str(resp), "createdBy": "1", "createdDate": "2026-10-10T09:00:00+03:00",
            "changedDate": changed, "closedDate": closed, "deadline": deadline, "groupId": "0"}


class Fake:
    def __init__(self):
        self.tasks, self.comments, self.calls = [], [], []

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "tasks.task.list":
            return {"result": {"tasks": self.tasks}}
        if method == "task.commentitem.getlist":
            return {"result": self.comments}
        raise AssertionError(method)


@pytest.fixture()
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(bw, "call", f)
    bw._CACHE.clear()
    return f


def test_list_period_merges_kinds_and_counts(client, owner, fake):
    with SessionLocal() as db:
        _emp(db)
    fake.tasks = [raw(1, status=5, closed="2026-10-20T10:00:00+03:00"), raw(2, status=3, deadline="2026-10-25T18:00:00+03:00")]
    r = client.get("/api/bitrix/tasks?date_from=2026-10-01&date_to=2026-10-31", headers=owner)
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 2 and d["by_status"] == {"Завершена": 1, "Выполняется": 1}
    assert {t["responsible"] for t in d["tasks"]} == {"Иван"}
    fields = {k for m, p in fake.calls for k in p if k.startswith("filter[>=")}
    assert fields == {"filter[>=CREATED_DATE]", "filter[>=CLOSED_DATE]", "filter[>=DEADLINE]"}
    assert client.get("/api/bitrix/tasks?date_from=2026-10-01&date_to=2026-10-31").status_code == 401
    assert client.get("/api/bitrix/tasks?date_from=2026-11-01&date_to=2026-10-01", headers=owner).status_code == 422


def test_watch_baseline_then_events(client, owner, fake):
    with SessionLocal() as db:
        _emp(db)
        fake.tasks = [raw(1, status=2, deadline="2026-11-10T18:00:00+03:00")]
        first = bw.watch_cycle(db)
        assert first == {"baseline": True, "checked": 1, "events": 0}
        fake.tasks = [
            raw(1, status=3, deadline="2026-11-12T18:00:00+03:00", changed="2026-11-04T12:00:00+03:00", title="Задача"),
            raw(2, title="Новая"),
        ]
        fake.comments = [{"ID": "5", "AUTHOR_NAME": "Пётр", "POST_MESSAGE": "Сделал половину"}]
        second = bw.watch_cycle(db)
        assert second["events"] == 4  # статус, срок, комментарий, новая задача
        texts = " | ".join(e["text"] for e in bw.recent_events(db, 7))
        print("EVENTS", texts)
        assert "Ждёт выполнения → Выполняется" in texts and "10.11.2026 → 12.11.2026" in texts and "Сделал половину" in texts and "Новая задача" in texts
        third = bw.watch_cycle(db)  # ничего не менялось
        assert third["events"] == 0
    ev = client.get("/api/bitrix/events?days=7", headers=owner).json()
    assert len(ev) == 4
    assert client.post("/api/bitrix/watch-now", headers=owner).json()["events"] == 0


def test_no_employees_and_no_webhook_errors(client, owner, monkeypatch):
    r = client.get("/api/bitrix/tasks?date_from=2026-10-01&date_to=2026-10-31", headers=owner)
    assert r.status_code == 422 and "сотрудника" in r.json()["detail"]
    monkeypatch.setattr(bitrix, "webhook_url", lambda: "")
    with pytest.raises(bitrix.BitrixError, match="не подключён"):
        bitrix.call("tasks.task.list", {})


def test_bitrix_connection_kind_and_tools(client, owner, monkeypatch):
    r = client.post("/api/connections", headers=owner, json={"kind": "bitrix24", "scope": "company", "config": {"webhook_url": "https://p.bitrix24.ru/rest/1/secretkey/"}})
    assert r.status_code == 201 and "secretkey" not in r.text
    assert bitrix.webhook_url() == "https://p.bitrix24.ru/rest/1/secretkey"
    from app.agents.owner_tools import OWNER_TOOL_NAMES

    assert "bitrix_tasks" in OWNER_TOOL_NAMES and "bitrix_updates" in OWNER_TOOL_NAMES
    assert date.fromisoformat("2026-10-01")


def test_import_users_and_set_credentials(client, owner, monkeypatch):
    def fake_call(method, params):
        assert method == "user.get"
        uid = int(params["FILTER[ID]"])
        return {"result": [] if uid == 99 else [{"ID": str(uid), "NAME": "Иван", "LAST_NAME": "Петров", "WORK_POSITION": "Менеджер", "EMAIL": "ivan@firm.ru" if uid == 12 else ""}]}

    monkeypatch.setattr("app.routers.bitrix.call", fake_call)
    r = client.post("/api/bitrix/import-users", headers=owner, json={"ids": [12, 15, 99, 12]})
    assert r.status_code == 200
    got = {x["bitrix_user_id"]: x for x in r.json()}
    assert got[12]["status"] == "created" and got[12]["email"] == "ivan@firm.ru" and not got[12]["needs_email"]
    assert got[15]["email"] == "b24-15@pending.invalid" and got[15]["needs_email"] is True
    assert got[99]["status"] == "not_found"
    assert client.post("/api/bitrix/import-users", headers=owner, json={"ids": [12]}).json()[0]["status"] == "exists"
    users = {u["bitrix_user_id"]: u for u in client.get("/api/users", headers=owner).json() if u["bitrix_user_id"]}
    assert users[15]["full_name"] == "Петров Иван" and users[15]["position"] == "Менеджер"
    # войти под временной почтой нельзя, пока владелец не задаст свою
    assert client.post("/api/auth/login", json={"email": "b24-15@pending.invalid", "password": "anything-123456"}).status_code in (400, 401)
    uid = users[15]["id"]
    ok = client.patch(f"/api/users/{uid}", headers=owner, json={"email": "Anna@Firm.ru", "password": "new-pass-123456"})
    assert ok.status_code == 200 and ok.json()["email"] == "anna@firm.ru"
    assert login(client, "anna@firm.ru", "new-pass-123456")
    dup = client.patch(f"/api/users/{uid}", headers=owner, json={"email": "ivan@firm.ru"})
    assert dup.status_code == 409
    client.post("/api/users", headers=owner, json={"email": "w@test.local", "password": "worker-pass-1234", "full_name": "С"})
    h = login(client, "w@test.local", "worker-pass-1234")
    assert client.post("/api/bitrix/import-users", headers=h, json={"ids": [5]}).status_code == 403
    assert client.patch(f"/api/users/{uid}", headers=h, json={"email": "x@y.ru"}).status_code == 403
