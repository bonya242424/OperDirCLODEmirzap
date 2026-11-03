from sqlalchemy import select

from app.agents.tools import TOOLS, Ctx, ToolError
from app.db import SessionLocal
from app.models import Agent, Folder, Metric, ReturnSupplier, Task, User
import pytest


def _ctx(db):
    uid = db.scalar(select(User.id).where(User.role == "owner"))
    return Ctx(db, None, lambda _t: None, user_id=uid, label="ИИ-чат")


def call(db, tool, **a):
    from app.agents.owner_tools import OWNER_TOOL_NAMES  # noqa: F401

    return TOOLS[tool].handler(_ctx(db), a)


def test_tasks_agents_folders_suppliers(client, owner):
    with SessionLocal() as db:
        out = call(db, "create_task", title="Позвонить поставщику", priority="high", deadline="2026-12-01")
        tid = db.scalar(select(Task.id))
        assert f"id={tid}" in out
        call(db, "update_task", task_id=tid, status="done")
        assert db.get(Task, tid).completed_at is not None
        with pytest.raises(ToolError):
            call(db, "update_task", task_id=tid, status="deleted")
        call(db, "create_agent", name="Утро", instruction="сводка", tools=["list_metrics"], schedule_kind="daily", daily_time="09:00")
        ag = db.scalar(select(Agent))
        assert ag.daily_time == "09:00" and ag.next_run_at is not None
        with pytest.raises(ToolError, match="Нет таких"):
            call(db, "update_agent", agent_id=ag.id, tools=["rm_rf"])
        call(db, "create_folder", name="Отчёты ИИ")
        fid = db.scalar(select(Folder.id).where(Folder.name == "Отчёты ИИ"))
        assert "file_id=" in call(db, "save_file", folder_id=fid, name="../../x", content="a;b\n1;2")
        call(db, "save_supplier", name="Армтек", portal_url="https://armtek.ru")
        call(db, "save_supplier", name="Армтек", contact="Иван")
        assert db.scalar(select(ReturnSupplier)).contact == "Иван"
        call(db, "update_metric", metric_id=1) if db.get(Metric, 1) else None


def test_no_destructive_or_secret_tools():
    from app.routers.chat import OWNER_CHAT_TOOLS

    for bad in ("delete", "password", "connection", "user", "send_return", "submit"):
        assert not any(bad in n for n in OWNER_CHAT_TOOLS)


def test_fetch_url_blocks_internal_and_reads_public(client, owner, monkeypatch):
    import app.agents.tools as t

    with SessionLocal() as db:
        ctx = _ctx(db)
        for bad in ("http://127.0.0.1:8000/api", "http://169.254.169.254/latest", "file:///etc/passwd", "http://user:p@example.com/"):
            with pytest.raises(ToolError, match="не подходит"):
                t.fetch_url(ctx, {"url": bad})

        class R:
            status_code = 200
            headers = {"content-type": "text/html"}
            text = "<html><script>x()</script><body><h1>Переписка</h1>" + "Бренд и небренд, текст страницы. " * 10 + "</body></html>"

        class C:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return R()

        monkeypatch.setattr(t, "_public_host", lambda h: True)
        monkeypatch.setattr("httpx.Client", C)
        out = t.fetch_url(ctx, {"url": "https://example.com/share/1"})
        assert "Переписка" in out and "x()" not in out


def test_cursor_tools(client, owner, monkeypatch):
    import app.agents.owner_tools as ot

    seen = []

    class R:
        status_code = 200
        text = "{}"

        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    def fake(method, url, json=None, auth=None, timeout=None):
        seen.append((method, url, json, auth))
        return R({"id": "bc_1", "status": "CREATING", "target": {"branchName": "cursor/x", "url": "https://cursor.com/agents?id=bc_1"}, "agents": []})

    monkeypatch.setattr("httpx.request", fake)
    with SessionLocal() as db:
        ctx = _ctx(db)
        with pytest.raises(ToolError, match="не подключён"):
            ot.cursor_launch(ctx, {"prompt": "Исправь тест в файле x"})
    r = client.post("/api/connections", headers=owner, json={"kind": "cursor", "scope": "company", "config": {"api_key": "key_abc"}})
    assert r.status_code == 201 and "key_abc" not in r.text
    with SessionLocal() as db:
        out = ot.cursor_launch(_ctx(db), {"prompt": "Исправь тест в файле x, не трогай остальное"})
        assert "bc_1" in out and "cursor/x" in out
        m, url, body, auth = seen[-1]
        assert url.endswith("/v0/agents") and body["target"]["autoCreatePr"] is True and body["source"]["ref"] == "main" and auth == ("key_abc", "")
        with pytest.raises(ToolError):
            ot.cursor_status(_ctx(db), {"agent_id": "../x"})
