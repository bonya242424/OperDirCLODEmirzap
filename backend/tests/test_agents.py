from datetime import timedelta
from pathlib import Path

from app.agents.service import compute_next, run_due_agents
from app.config import settings
from app.db import SessionLocal
from app.llm import Step, ToolCall
from app.models import Agent, AgentRun, Folder, Metric, MetricValue, StoredFile, utcnow
from app.schemas import _utc
from tests.conftest import login


class Script:
    """Подставная модель: отдаёт заранее заданные ходы и запоминает, что ей показали."""

    def __init__(self, *steps):
        self.steps, self.systems, self.seen = list(steps), [], []

    def step(self, system, messages, tools):
        self.systems.append(system)
        self.seen.append((list(messages), [t["name"] for t in tools]))
        return self.steps.pop(0)


def say(text):
    return Step(text=text, assistant_content=[{"type": "text", "text": text}])


def call(name, **args):
    c = ToolCall("tu_" + name, name, args)
    return Step(calls=[c], assistant_content=[{"type": "tool_use", "id": c.id, "name": name, "input": args}])


def use(monkeypatch, script):
    monkeypatch.setattr("app.agents.runtime.get_provider", lambda: script)
    return script


def _create(client, h, **kw):
    body = {"name": "Агент", "instruction": "Сделай дело", "tools": [], **kw}
    return client.post("/api/agents", headers=h, json=body)


def _last(client, h, aid):
    return client.get(f"/api/agents/{aid}/runs", headers=h).json()[0]


def _employee(client, owner):
    r = client.post("/api/users", headers=owner, json={"email": "e@test.local", "password": "employee-pass-123", "full_name": "Сотрудник"})
    assert r.status_code == 201, r.text
    return login(client, "e@test.local", "employee-pass-123")


def test_validation(client, owner):
    assert _create(client, owner, instruction="").status_code == 422
    assert _create(client, owner, tools=["rm_rf"]).status_code == 422
    assert _create(client, owner, schedule_kind="interval", interval_minutes=1).status_code == 422
    assert _create(client, owner, schedule_kind="daily", daily_time="25:00").status_code == 422
    ok = _create(client, owner, schedule_kind="daily", daily_time="09:30", tools=["list_metrics", "list_metrics"])
    assert ok.status_code == 201 and ok.json()["next_run_at"] and ok.json()["tools"] == ["list_metrics"]


def test_presets_and_tools_listed(client, owner):
    assert {"custom", "analyst", "manager"} <= {p["key"] for p in client.get("/api/agents/presets", headers=owner).json()}
    tools = {t["key"]: t["kind"] for t in client.get("/api/agents/tools", headers=owner).json()}
    assert tools["set_metric_value"] == "write" and tools["propose_task"] == "confirm" and tools["read_file"] == "read"


def test_compute_next_daily_moscow():
    from datetime import datetime, timezone

    a = Agent(schedule_kind="daily", daily_time="09:00", enabled=True)
    assert compute_next(a, datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    assert compute_next(a, datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
    a.enabled = False
    assert compute_next(a) is None


def test_agent_chooses_tools_and_writes_metric_from_csv(client, owner, monkeypatch):
    Path(settings.files_dir).mkdir(parents=True, exist_ok=True)
    (Path(settings.files_dir) / "disk1").write_text("Дата;Заказов\n02.11;120\n", encoding="utf-8")
    with SessionLocal() as db:
        f = Folder(name="1С Торговля", source_key="onec_trade", created_by=1)
        m = Metric(direction="ops", name="Заказов в день", unit="шт", better="up", created_by=1)
        db.add_all([f, m])
        db.flush()
        db.add(StoredFile(folder_id=f.id, name="заказы.csv", size=30, disk_name="disk1", uploaded_by=1))
        db.commit()
        mid, fid = m.id, db.query(StoredFile).first().id
    script = use(monkeypatch, Script(
        call("list_files", folder="торговля"),
        call("read_file", file_id=fid),
        call("set_metric_value", metric_id=mid, period="2026-11-02", value=120),
        say("Записал 120 заказов"),
    ))
    aid = _create(client, owner, tools=["list_files", "read_file", "set_metric_value"]).json()["id"]
    client.post(f"/api/agents/{aid}/run", headers=owner)
    r = _last(client, owner, aid)
    assert r["status"] == "done" and r["summary"] == "Записал 120 заказов"
    assert script.seen[0][1] == ["list_files", "read_file", "set_metric_value"]
    with SessionLocal() as db:
        v = db.query(MetricValue).one()
        assert v.metric_id == mid and v.value == 120
    # результаты инструментов дошли до модели
    last_msgs = script.seen[-1][0]
    assert any("Дата;Заказов" in str(m["content"]) for m in last_msgs)
    assert any("Вызываю" in l["text"] or "Прочитать файл" in l["text"] for l in r["log"])


def test_tool_not_granted_is_refused(client, owner, monkeypatch):
    with SessionLocal() as db:
        m = Metric(direction="ops", name="X", unit="", better="up", created_by=1)
        db.add(m)
        db.commit()
        mid = m.id
    use(monkeypatch, Script(call("set_metric_value", metric_id=mid, period="2026-11-02", value=5), say("Не вышло")))
    aid = _create(client, owner, tools=["list_metrics"]).json()["id"]  # записи не выдали
    client.post(f"/api/agents/{aid}/run", headers=owner)
    with SessionLocal() as db:
        assert db.query(MetricValue).count() == 0
    assert any("не выдан" in l["text"] for l in _last(client, owner, aid)["log"])


def test_proposed_task_waits_for_human(client, owner, monkeypatch):
    use(monkeypatch, Script(call("propose_task", title="Проверить поставщика А", description="Брак"), say("Предложил задачу")))
    aid = _create(client, owner, tools=["propose_task"], level="manager").json()["id"]
    client.post(f"/api/agents/{aid}/run", headers=owner)
    assert client.get("/api/board", headers=owner).json()["columns"][0]["cards"] == []  # сама не создалась
    acts = client.get("/api/agents/actions", headers=owner).json()
    assert len(acts) == 1 and acts[0]["title"].startswith("Создать задачу")
    assert client.get("/api/agents", headers=owner).json()[0]["pending_actions"] == 1
    done = client.post(f"/api/agents/actions/{acts[0]['id']}/approve", headers=owner).json()
    assert done["status"] == "done"
    assert [c["title"] for c in client.get("/api/board", headers=owner).json()["columns"][0]["cards"]] == ["Проверить поставщика А"]
    assert client.post(f"/api/agents/actions/{acts[0]['id']}/approve", headers=owner).status_code == 409


def test_reject_action_and_foreign_access(client, owner, monkeypatch):
    emp = _employee(client, owner)
    use(monkeypatch, Script(call("propose_task", title="Т"), say("ok")))
    aid = _create(client, owner, tools=["propose_task"]).json()["id"]
    client.post(f"/api/agents/{aid}/run", headers=owner)
    act = client.get("/api/agents/actions", headers=owner).json()[0]["id"]
    assert client.get("/api/agents/actions", headers=emp).json() == []
    assert client.post(f"/api/agents/actions/{act}/approve", headers=emp).status_code == 404
    assert client.post(f"/api/agents/actions/{act}/reject", headers=owner).json()["status"] == "rejected"
    assert client.get("/api/board", headers=owner).json()["columns"][0]["cards"] == []


def test_failures_are_readable(client, owner, monkeypatch):
    aid = _create(client, owner).json()["id"]
    monkeypatch.setattr("app.agents.runtime.get_provider", lambda: None)
    client.post(f"/api/agents/{aid}/run", headers=owner)
    r = _last(client, owner, aid)
    assert r["status"] == "failed" and "ключ" in r["summary"]


def test_step_limit(client, owner, monkeypatch):
    use(monkeypatch, Script(*[call("list_metrics") for _ in range(12)]))
    aid = _create(client, owner, tools=["list_metrics"]).json()["id"]
    client.post(f"/api/agents/{aid}/run", headers=owner)
    r = _last(client, owner, aid)
    assert r["status"] == "done" and "предел шагов" in r["summary"]


def test_schedule_tick_runs_due_agent_once(client, owner, monkeypatch):
    use(monkeypatch, Script(say("тик"), say("тик2")))
    aid = _create(client, owner, schedule_kind="interval", interval_minutes=10).json()["id"]
    with SessionLocal() as db:
        db.get(Agent, aid).next_run_at = utcnow() - timedelta(minutes=1)
        db.commit()
    run_due_agents()
    run_due_agents()
    runs = client.get(f"/api/agents/{aid}/runs", headers=owner).json()
    assert len(runs) == 1 and runs[0]["trigger"] == "schedule" and runs[0]["summary"] == "тик"
    with SessionLocal() as db:
        assert _utc(db.get(Agent, aid).next_run_at) > utcnow()
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
    assert len(client.get("/api/agents", headers=owner).json()) == 2
    assert client.delete(f"/api/agents/{mine}", headers=owner).status_code == 204


def test_structure_reports_and_run_subordinate(client, owner, monkeypatch):
    mgr = _create(client, owner, name="Рук", level="manager").json()
    ana = _create(client, owner, name="Ан", level="analyst", parent_id=mgr["id"], tools=["run_subordinate"]).json()
    ops = _create(client, owner, name="Опер", level="ops", parent_id=ana["id"]).json()
    assert _create(client, owner, level="ops", parent_id=mgr["id"]).status_code == 422
    assert _create(client, owner, level="manager", parent_id=ana["id"]).json()["parent_id"] is None
    with SessionLocal() as db:
        db.add(AgentRun(agent_id=ops["id"], status="done", summary="Заказов за день: 120"))
        db.commit()
    script = use(monkeypatch, Script(call("run_subordinate", agent_id=ops["id"]), say("отчёт опера получен"), say("Вывод")))
    client.post(f"/api/agents/{ana['id']}/run", headers=owner)
    assert "Заказов за день: 120" in script.systems[0] and "subordinate_reports" in script.systems[0]
    assert _last(client, owner, ana["id"])["summary"] == "Вывод"
    # чужого не запустить
    use(monkeypatch, Script(call("run_subordinate", agent_id=mgr["id"]), say("ок")))
    client.post(f"/api/agents/{ana['id']}/run", headers=owner)
    assert any("только своих" in l["text"] for l in _last(client, owner, ana["id"])["log"])
    client.delete(f"/api/agents/{ana['id']}", headers=owner)
    ops_now = next(a for a in client.get("/api/agents", headers=owner).json() if a["id"] == ops["id"])
    assert ops_now["parent_id"] is None
