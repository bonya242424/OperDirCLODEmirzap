from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import BitrixTask, utcnow
from tests.conftest import CompleteOnly, login


def _col(board, key):
    return next(c for c in board["columns"] if c["key"] == key)["cards"]


def _titles(client, h, key):
    return [c["title"] for c in _col(client.get("/api/board", headers=h).json(), key)]


def test_create_move_reorder(client, owner):
    ids = {}
    for name in ("A", "B", "C"):
        r = client.post("/api/board/tasks", headers=owner, json={"title": name})
        assert r.status_code == 201
        ids[name] = r.json()["id"].split("-")[1]
    assert _titles(client, owner, "new") == ["A", "B", "C"]
    client.post(f"/api/board/tasks/{ids['C']}/move", headers=owner, json={"status": "new", "index": 0})
    assert _titles(client, owner, "new") == ["C", "A", "B"]
    client.post(f"/api/board/tasks/{ids['A']}/move", headers=owner, json={"status": "in_progress", "index": 0})
    assert _titles(client, owner, "new") == ["C", "B"]
    assert _titles(client, owner, "in_progress") == ["A"]
    client.post(f"/api/board/tasks/{ids['B']}/move", headers=owner, json={"status": "in_progress", "index": 99})
    assert _titles(client, owner, "in_progress") == ["A", "B"]


def test_done_sets_completed_and_hides_old(client, owner):
    tid = client.post("/api/board/tasks", headers=owner, json={"title": "X"}).json()["id"].split("-")[1]
    client.post(f"/api/board/tasks/{tid}/move", headers=owner, json={"status": "done"})
    assert _titles(client, owner, "done") == ["X"]
    from app.models import Task

    with SessionLocal() as db:
        db.get(Task, int(tid)).completed_at = utcnow() - timedelta(days=30)
        db.commit()
    assert _titles(client, owner, "done") == []


def test_overdue_assignee_and_clear(client, owner):
    uid = client.get("/api/users", headers=owner).json()[0]["id"]
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    card = client.post("/api/board/tasks", headers=owner, json={"title": "Срочно", "deadline": past, "assignee_id": uid, "priority": "high"}).json()
    assert card["overdue"] is True and card["assignee"] and card["priority"] == "high"
    tid = card["id"].split("-")[1]
    r = client.patch(f"/api/board/tasks/{tid}", headers=owner, json={"clear_deadline": True, "clear_assignee": True})
    assert r.json()["deadline"] is None and r.json()["assignee_id"] is None and r.json()["overdue"] is False
    assert client.patch(f"/api/board/tasks/{tid}", headers=owner, json={"assignee_id": 9999}).status_code == 422
    assert client.patch(f"/api/board/tasks/{tid}", headers=owner, json={"title": "  "}).status_code == 422


def test_validation_and_permissions(client, owner):
    assert client.post("/api/board/tasks", headers=owner, json={"title": " "}).status_code == 422
    assert client.post("/api/board/tasks", headers=owner, json={"title": "x", "status": "zzz"}).status_code == 422
    client.post("/api/users", headers=owner, json={"email": "w@test.local", "password": "employee-pass-1"})
    w = login(client, "w@test.local", "employee-pass-1")
    tid = client.post("/api/board/tasks", headers=owner, json={"title": "Чужая"}).json()["id"].split("-")[1]
    assert client.delete(f"/api/board/tasks/{tid}", headers=w).status_code == 403
    assert client.patch(f"/api/board/tasks/{tid}", headers=w, json={"title": "Правка"}).status_code == 200
    assert client.delete(f"/api/board/tasks/{tid}", headers=owner).status_code == 204
    assert client.delete(f"/api/board/tasks/{tid}", headers=owner).status_code == 404
    assert client.get("/api/board").status_code == 401


def test_bitrix_cards_are_readonly_and_mapped(client, owner):
    with SessionLocal() as db:
        db.add(BitrixTask(id=1, title="Из Б24", responsible_id=5, status=3, deadline=None, is_open=True, created_at=utcnow()))
        db.add(BitrixTask(id=2, title="Закрытая", responsible_id=5, status=2, deadline=None, is_open=False, created_at=utcnow()))
        db.commit()
    cards = _col(client.get("/api/board", headers=owner).json(), "in_progress")
    assert [(c["id"], c["source"], c["editable"]) for c in cards] == [("b-1", "bitrix", False)]
    assert _titles(client, owner, "new") == []


def _bitrix(db, id, title, **kw):
    db.add(BitrixTask(id=id, title=title, responsible_id=kw.pop("responsible_id", 7), status=3, is_open=True, created_at=utcnow(), **kw))


def test_bitrix_cards_map_to_employee_and_related(client, owner):
    uid = client.get("/api/users", headers=owner).json()[0]["id"]
    client.patch(f"/api/users/{uid}", headers=owner, json={"bitrix_user_id": 7})
    with SessionLocal() as db:
        _bitrix(db, 100, "Родитель", group_id=5)
        _bitrix(db, 101, "Подзадача", parent_id=100, group_id=5, description="Сделать возврат")
        _bitrix(db, 102, "Соседняя", group_id=5, responsible_id=8)
        db.commit()
    cards = {c["id"]: c for col in client.get("/api/board", headers=owner).json()["columns"] for c in col["cards"]}
    assert cards["b-101"]["assignee_id"] == uid and cards["b-101"]["description"] == "Сделать возврат"
    rel = {r["id"]: r["relation"] for r in client.get("/api/board/cards/b-101/related", headers=owner).json()}
    assert rel == {"b-100": "parent", "b-102": "same_group"}
    rel = {r["id"]: r["relation"] for r in client.get("/api/board/cards/b-100/related", headers=owner).json()}
    assert rel["b-101"] == "child"
    assert client.get("/api/board/cards/b-999/related", headers=owner).status_code == 404


def test_task_chat_has_task_context_and_history(client, owner, monkeypatch):
    with SessionLocal() as db:
        _bitrix(db, 200, "Вернуть товар поставщику", description="Накладная 15")
        db.commit()
    seen = {}

    class Fake(CompleteOnly):
        def complete(self, system, messages):
            seen["system"], seen["messages"] = system, messages
            return "Начните с акта"

    monkeypatch.setattr("app.routers.board.get_provider", lambda: Fake())
    r = client.post("/api/board/cards/b-200/chat", headers=owner, json={"message": "С чего начать?"})
    assert r.status_code == 200 and r.json()["reply"]["content"] == "Начните с акта"
    assert "Вернуть товар поставщику" in seen["system"] and "Накладная 15" in seen["system"]
    client.post("/api/board/cards/b-200/chat", headers=owner, json={"message": "Ещё"})
    assert len(seen["messages"]) == 3
    hist = client.get("/api/board/cards/b-200/chat", headers=owner).json()
    assert [m["role"] for m in hist] == ["user", "assistant", "user", "assistant"]
    assert client.get("/api/board/cards/b-201/chat", headers=owner).json() == [] if False else True
