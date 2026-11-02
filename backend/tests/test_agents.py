from datetime import timedelta

from app.agents.service import compute_next, run_due_agents
from app.db import SessionLocal
from app.models import Agent, AgentRun, Folder, StoredFile, utcnow
from app.schemas import _utc
from tests.conftest import login


def _create(client, h, **kw):
    body = {"name": "Агент", "kind": "onec_files", **kw}
    return client.post("/api/agents", headers=h, json=body)


def _employee(client, owner):
    r = client.post("/api/users", headers=owner, json={"email": "e@test.local", "password": "employee-pass-123", "full_name": "Сотрудник"})
    assert r.status_code == 201, r.text
    return login(client, "e@test.local", "employee-pass-123")


def test_validation(client, owner):
    assert _create(client, owner, kind="nope").status_code == 422
    assert _create(client, owner, kind="llm_task").status_code == 422  # нужно поручение
    assert _create(client, owner, schedule_kind="interval", interval_minutes=1).status_code == 422
    assert _create(client, owner, schedule_kind="daily", daily_time="25:00").status_code == 422
    ok = _create(client, owner, schedule_kind="daily", daily_time="09:30")
    assert ok.status_code == 201 and ok.json()["next_run_at"]


def test_compute_next_daily_moscow():
    a = Agent(schedule_kind="daily", daily_time="09:00", enabled=True)
    from datetime import datetime, timezone

    now = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 08:00 по Москве
    assert compute_next(a, now) == datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    now = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)  # 10:00 по Москве: завтра
    assert compute_next(a, now) == datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
    a.enabled = False
    assert compute_next(a, now) is None


def test_run_now_onec_files(client, owner):
    with SessionLocal() as db:
        f = Folder(name="1С Бухгалтерия", source_key="onec_accounting", created_by=1)
        db.add(f)
        db.flush()
        db.add(StoredFile(folder_id=f.id, name="выгрузка.xlsx", size=2048, disk_name="x1", uploaded_by=1))
        db.commit()
    aid = _create(client, owner).json()["id"]
    assert client.post(f"/api/agents/{aid}/run", headers=owner).status_code == 202
    runs = client.get(f"/api/agents/{aid}/runs", headers=owner).json()
    assert runs[0]["status"] == "done" and "Новых выгрузок из 1С: 1" in runs[0]["summary"]
    # второй запуск: новых файлов с прошлого запуска нет
    client.post(f"/api/agents/{aid}/run", headers=owner)
    assert "нет" in client.get(f"/api/agents/{aid}/runs", headers=owner).json()[0]["summary"]
    listed = client.get("/api/agents", headers=owner).json()[0]
    assert listed["last_run"]["status"] == "done" and listed["editable"] is True


def test_failures_are_readable(client, owner):
    aid = _create(client, owner).json()["id"]  # нет папок 1С
    client.post(f"/api/agents/{aid}/run", headers=owner)
    r = client.get(f"/api/agents/{aid}/runs", headers=owner).json()[0]
    assert r["status"] == "failed" and "папок" in r["summary"]
    bid = _create(client, owner, kind="returns_browser").json()["id"]
    client.post(f"/api/agents/{bid}/run", headers=owner)
    assert "Браузерный исполнитель" in client.get(f"/api/agents/{bid}/runs", headers=owner).json()[0]["summary"]


def test_llm_task_gets_company_data(client, owner, monkeypatch):
    seen = {}

    class Fake:
        def complete(self, system, messages):
            seen["system"], seen["messages"] = system, messages
            return "Сводка готова"

    monkeypatch.setattr("app.agents.executors.get_provider", lambda: Fake())
    aid = _create(client, owner, kind="llm_task", instruction="Дай утреннюю сводку").json()["id"]
    client.post(f"/api/agents/{aid}/run", headers=owner)
    r = client.get(f"/api/agents/{aid}/runs", headers=owner).json()[0]
    assert r["summary"] == "Сводка готова" and "<company_data>" in seen["system"]
    assert seen["messages"][0]["content"] == "Дай утреннюю сводку"
    monkeypatch.setattr("app.agents.executors.get_provider", lambda: None)
    client.post(f"/api/agents/{aid}/run", headers=owner)
    assert "ключ" in client.get(f"/api/agents/{aid}/runs", headers=owner).json()[0]["summary"]


def test_schedule_tick_runs_due_agent_once(client, owner):
    aid = _create(client, owner, schedule_kind="interval", interval_minutes=10).json()["id"]
    with SessionLocal() as db:
        db.get(Agent, aid).next_run_at = utcnow() - timedelta(minutes=1)
        db.commit()
    run_due_agents()
    run_due_agents()  # время уже сдвинуто: второй тик ничего не запускает
    runs = client.get(f"/api/agents/{aid}/runs", headers=owner).json()
    assert len(runs) == 1 and runs[0]["trigger"] == "schedule"
    with SessionLocal() as db:
        assert _utc(db.get(Agent, aid).next_run_at) > utcnow()
        # запуск в работе: следующий тик его пропускает
        db.get(Agent, aid).next_run_at = utcnow() - timedelta(minutes=1)
        db.add(AgentRun(agent_id=aid, status="running"))
        db.commit()
    run_due_agents()
    assert len(client.get(f"/api/agents/{aid}/runs", headers=owner).json()) == 2


def test_access_own_agents_only(client, owner):
    emp = _employee(client, owner)
    mine = _create(client, emp).json()["id"]
    theirs = _create(client, owner).json()["id"]
    assert [a["id"] for a in client.get("/api/agents", headers=emp).json()] == [mine]
    assert client.post(f"/api/agents/{theirs}/run", headers=emp).status_code == 404
    assert client.delete(f"/api/agents/{theirs}", headers=emp).status_code == 404
    assert len(client.get("/api/agents", headers=owner).json()) == 2  # владелец видит всех
    assert client.delete(f"/api/agents/{mine}", headers=owner).status_code == 204


def test_structure_levels_and_reports(client, owner, monkeypatch):
    mgr = _create(client, owner, name="Руководитель", kind="llm_task", instruction="Поставь задачи", level="manager").json()
    ana = _create(client, owner, name="Аналитик заказов", kind="llm_task", instruction="Проанализируй", level="analyst", parent_id=mgr["id"]).json()
    ops = _create(client, owner, name="Опер-штаб 1С", level="ops", parent_id=ana["id"]).json()
    assert ops["parent_id"] == ana["id"] and ana["level"] == "analyst"
    # неверная подчинённость: опер-штаб не подчиняется руководителю напрямую, руководитель никому
    assert _create(client, owner, level="ops", parent_id=mgr["id"]).status_code == 422
    assert _create(client, owner, level="manager", parent_id=ana["id"]).json()["parent_id"] is None
    with SessionLocal() as db:  # у опер-штаба есть готовый отчёт
        db.add(AgentRun(agent_id=ops["id"], status="done", summary="Заказов за день: 120"))
        db.commit()
    seen = {}

    class Fake:
        def complete(self, system, messages):
            seen["system"] = system
            return "Вывод"

    monkeypatch.setattr("app.agents.executors.get_provider", lambda: Fake())
    client.post(f"/api/agents/{ana['id']}/run", headers=owner)
    assert "Заказов за день: 120" in seen["system"] and "subordinate_reports" in seen["system"]
    # удаление аналитика отвязывает подчинённых
    client.delete(f"/api/agents/{ana['id']}", headers=owner)
    ops_now = next(a for a in client.get("/api/agents", headers=owner).json() if a["id"] == ops["id"])
    assert ops_now["parent_id"] is None
