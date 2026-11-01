from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import BitrixTask, utcnow
from tests.conftest import login


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
        db.add(BitrixTask(id=1, title="Из Б24", responsible_id=5, status=3, deadline=None, is_open=True))
        db.add(BitrixTask(id=2, title="Закрытая", responsible_id=5, status=2, deadline=None, is_open=False))
        db.commit()
    cards = _col(client.get("/api/board", headers=owner).json(), "in_progress")
    assert [(c["id"], c["source"], c["editable"]) for c in cards] == [("b-1", "bitrix", False)]
    assert _titles(client, owner, "new") == []
