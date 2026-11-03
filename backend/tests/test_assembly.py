import time

import pytest
from sqlalchemy import select

from app.assembly_run import AGENT_KEYS, to_csv
from app.db import SessionLocal
from app.models import AssemblyRun, Folder, StoredFile
from app.rdp import rdp_manager
from tests.test_agents import Script, call


@pytest.fixture()
def term(client, owner, monkeypatch):
    r = client.post("/api/connections", headers=owner, json={"kind": "rdp", "scope": "company", "config": {"host": "10.0.0.5", "port": "34821", "user": "ivan", "password": "pw-secret-1"}})
    cid = r.json()["id"]
    state = {"keys": [], "open": True}
    monkeypatch.setattr(rdp_manager, "is_open", lambda key: True)
    monkeypatch.setattr(rdp_manager, "screenshot", lambda key: b"\xff\xd8jpeg")
    monkeypatch.setattr(rdp_manager, "click", lambda *a, **k: None)
    monkeypatch.setattr(rdp_manager, "type_text", lambda *a: None)
    monkeypatch.setattr(rdp_manager, "press", lambda key, k: state["keys"].append(k))
    monkeypatch.setattr(rdp_manager, "clipboard", lambda key: "Артикул\tКол-во\nA1\t5\nB;2\t7")
    return cid, state


def _wait(client, owner, rid):
    for _ in range(100):
        r = client.get(f"/api/assembly/runs/{rid}", headers=owner).json()
        if r["status"] != "running":
            return r
        time.sleep(0.1)
    raise AssertionError("не закончилось")


def test_assembly_saves_per_supplier_and_blocks_dangerous_keys(client, owner, term, monkeypatch):
    cid, state = term
    script = Script(
        call("rdp_view"),
        call("rdp_key", key="Delete"),  # заблокировано
        call("rdp_key", key="Control+s"),  # заблокировано
        call("rdp_key", key="Control+c"),
        call("rdp_clipboard"),
        call("save_document", supplier="Армтек", label="№7 от 01.11.2026"),
        call("save_document", supplier="Рольф/Тест", label="№8"),
        call("finish", summary="Нашёл 2, сохранил 2"),
    )
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    r = client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-26", "date_to": "2026-11-01"})
    assert r.status_code == 202, r.text
    run = _wait(client, owner, r.json()["id"])
    assert run["status"] == "done" and "Сохранено файлов: 2" in run["summary"]
    log = "\n".join(run["log"])
    assert log.count("Отказ: Эта клавиша агенту недоступна") == 2
    assert state["keys"] == ["Control+c"]  # Delete и Ctrl+S до терминала не дошли
    with SessionLocal() as db:
        names = {f.name: f for f in db.scalars(select(Folder))}
        assert "Сборка 1С — Армтек" in names and "Сборка 1С — Рольф Тест" in names
        files = db.scalars(select(StoredFile)).all()
        assert len(files) == 2 and all("сохранено 20" in f.name and f.name.endswith(".csv") for f in files)
    assert "Delete" not in AGENT_KEYS


def test_clipboard_to_csv():
    assert to_csv("a\tb\n1\t2;3") == 'a;b\n1;"2;3"\n'


def test_assembly_owner_only_and_validation(client, owner, term):
    cid, _ = term
    assert client.post("/api/assembly/run", json={"connection_id": cid, "date_from": "2026-10-01", "date_to": "2026-10-02"}).status_code == 401
    bad = client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-11-02", "date_to": "2026-10-01"})
    assert bad.status_code == 422
    assert client.post("/api/assembly/run", headers=owner, json={"connection_id": 999, "date_from": "2026-10-01", "date_to": "2026-10-02"}).status_code == 422


def test_assembly_nudges_when_model_answers_without_actions(client, owner, term, monkeypatch):
    from tests.test_agents import say

    cid, _ = term
    script = Script(
        say("Вот сборка: документ 000007007"),  # ответ по памяти, без действий
        call("rdp_view"),
        call("rdp_key", key="Control+c"),
        call("save_document", supplier="Армтек", label="№1"),
        call("finish", summary="готово"),
    )
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    r = client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-01", "date_to": "2026-10-31"})
    run = _wait(client, owner, r.json()["id"])
    assert run["status"] == "done" and "Сохранено файлов: 1" in run["summary"]
    assert any("без действий" in line for line in run["log"])
    first = script.seen[0][0][0]["content"]
    assert any(b.get("type") == "image" for b in first)  # экран показан сразу


def test_assembly_marks_duplicates_and_shows_saved_live(client, owner, term, monkeypatch):
    from app.assembly_run import doc_number

    assert doc_number("№000007007 от 02.11.2026") == "7007" and doc_number("без номера") == ""
    cid, _ = term
    script = Script(
        call("save_document", supplier="Армтек", label="№000007007 от 02.11.2026"),
        call("save_document", supplier="Армтек", label="№000007007 от 02.11.2026"),  # повтор
        call("save_document", supplier="Армтек", label="№000007008 от 03.11.2026"),
        call("finish", summary="ok"),
    )
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    r = client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-01", "date_to": "2026-11-03"})
    run = _wait(client, owner, r.json()["id"])
    assert "Сохранено файлов: 2" in run["summary"] and "Дублей пропущено: 1" in run["summary"]
    assert sum(1 for x in run["saved"] if x.get("duplicate")) == 1
    with SessionLocal() as db:
        assert len(db.scalars(select(StoredFile)).all()) == 2


def test_assembly_survives_bad_args_and_model_hiccup(client, owner, term, monkeypatch):
    from app.llm import LLMError

    cid, _ = term
    script = Script(call("rdp_click"), call("save_document", supplier="Армтек", label="№1234"), call("finish", summary="ok"))
    real = script.step
    state = {"n": 0}

    def flaky(system, messages, tools):
        state["n"] += 1
        if state["n"] == 2:
            raise LLMError("временный сбой")
        return real(system, messages, tools)

    script.step = flaky
    monkeypatch.setattr("app.assembly_run.time.sleep", lambda s: None)
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    r = client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-01", "date_to": "2026-10-31"})
    run = _wait(client, owner, r.json()["id"])
    assert run["status"] == "done" and "Сохранено файлов: 1" in run["summary"]
    log = "\n".join(run["log"])
    assert "Неверный вызов rdp_click" in log and "Сбой модели" in log
