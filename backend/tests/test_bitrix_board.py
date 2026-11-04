import json

import pytest

from app import bitrix_board as bb, bitrix_watch as bw
from app.db import SessionLocal
from app.models import BitrixTask, User
from tests.conftest import CompleteOnly
from datetime import datetime, timedelta, timezone

from tests.test_bitrix_watch import Fake, _emp, raw as _raw


def raw(i, status=2, **kw):
    now = datetime.now(timezone.utc).isoformat()
    kw.setdefault("changed", now)
    created = kw.pop("created", None) or now
    t = _raw(i, status, **kw)
    t["createdDate"] = created
    return t


@pytest.fixture()
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(bw, "call", f)
    monkeypatch.setattr(bb, "call", f)
    bw._CACHE.clear()
    return f


def _col(client, h, key):
    b = client.get("/api/board", headers=h).json()
    return {c["title"]: c for c in next(c for c in b["columns"] if c["key"] == key)["cards"]}


def test_columns_and_statuses(client, owner, fake):
    with SessionLocal() as db:
        _emp(db)
    fake.tasks = [raw(1, 2, title="План"), raw(2, 3, title="Идёт"), raw(3, 6, title="Пауза"), raw(4, 7, title="Отказ"), raw(5, 5, title="Сделано"), raw(6, 4, title="Контроль")]
    with SessionLocal() as db:
        res = bb.sync_board(db)
    assert res["total"] == 6
    b = client.get("/api/board", headers=owner).json()
    assert [c["title"] for c in b["columns"]] == ["План", "В работе", "Отмена", "Завершено", "На паузе"]
    assert set(_col(client, owner, "new")) == {"План"}
    assert set(_col(client, owner, "in_progress")) == {"Идёт", "Контроль"}
    assert set(_col(client, owner, "paused")) == {"Пауза"}
    assert set(_col(client, owner, "cancelled")) == {"Отказ"}
    assert set(_col(client, owner, "done")) == {"Сделано"}


class Tagger(CompleteOnly):
    def complete(self, system, messages):
        assert "<tasks>" in messages[0]["content"]
        return json.dumps([{"id": 1, "process": "Закупки и поставщики" }, {"id": 2, "process": "Нет такого" }])


def test_tagging_and_owner_override(client, owner, fake, monkeypatch):
    with SessionLocal() as db:
        _emp(db)
        fake.tasks = [raw(1, 3, title="Сменить поставщика"), raw(2, 3, title="Позвонить")]
        bb.sync_board(db)
        monkeypatch.setattr(bb, "get_provider", lambda: Tagger())
        assert bb.tag_tasks(db) == 2
    cards = _col(client, owner, "in_progress")
    assert cards["Сменить поставщика"]["process"] == "Закупки и поставщики"
    assert cards["Позвонить"]["process"] == "Прочее" and cards["Позвонить"]["tag_source"] == "ai"
    r = client.patch("/api/board/cards/b-2/tags", headers=owner, json={"process": "Возвраты"})
    assert r.status_code == 200 and r.json()["tag_source"] == "owner"
    assert client.patch("/api/board/cards/b-2/tags", headers=owner, json={"process": "Выдумка"}).status_code == 422
    with SessionLocal() as db:
        assert bb.tag_tasks(db) == 0  # размеченное владельцем и уже размеченное не трогаем


def test_detail_comments_and_processes(client, owner, fake):
    with SessionLocal() as db:
        _emp(db)
    t = raw(1, 3, title="С комментами")
    t["commentsCount"] = "1"
    fake.tasks = [t]
    fake.comments = [{"ID": "5", "AUTHOR_NAME": "Иван", "POST_MESSAGE": "Готово наполовину", "POST_DATE": "2026-11-04T11:00:00+03:00"}]
    with SessionLocal() as db:
        bb.sync_board(db)
    d = client.get("/api/board/cards/b-1/detail", headers=owner).json()
    assert d["comments_count"] == 1 and d["comments"][0]["text"] == "Готово наполовину" and d["status_name"] == "Выполняется"
    assert "Возвраты" in client.get("/api/board/processes", headers=owner).json()
    assert client.put("/api/board/processes", headers=owner, json={"items": ["А", "Б", "А"]}).json() == ["А", "Б"]
    assert client.put("/api/board/processes", headers=owner, json={"items": []}).status_code == 422


def test_old_untouched_tasks_not_pulled(client, owner, fake):
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as db:
        _emp(db, name="Иван Петров")
        fake.tasks = [raw(1, 2, title="Свежая"), raw(2, 2, title="Апрельская", changed=now_iso, created=old)]
        bb.sync_board(db)
        db.add(BitrixTask(id=3, title="Старая из прошлой выгрузки", responsible_id=7, status=2, is_open=True))
        db.commit()
    plan = _col(client, owner, "new")
    assert set(plan) == {"Свежая"}
    assert plan["Свежая"]["assignee"] == "Иван Петров"
