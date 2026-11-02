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
