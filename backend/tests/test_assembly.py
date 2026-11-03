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


def test_restart_resumes_running_assembly(client, owner, term, monkeypatch):
    from app import assembly_run

    cid, _ = term
    started = []
    monkeypatch.setattr(assembly_run, "run_assembly", lambda rid: started.append(rid))
    with SessionLocal() as db:
        r = AssemblyRun(connection_id=cid, date_from=__import__("datetime").date(2026, 10, 1), date_to=__import__("datetime").date(2026, 10, 31), status="running", note="Армтек", created_by=1)
        db.add(r)
        db.commit()
        old = r.id
    assembly_run.reset_stale()
    time.sleep(0.3)
    with SessionLocal() as db:
        rows = db.scalars(select(AssemblyRun).order_by(AssemblyRun.id)).all()
        first = db.get(AssemblyRun, old)
        assert first.status == "failed" and "продолжена автоматически" in first.summary
        nxt = rows[-1]
        assert nxt.id != old and nxt.note.endswith("[автопродолжение 1]") and nxt.id in started


def test_agent_asks_owner_and_answer_goes_to_knowledge(client, owner, term, monkeypatch):
    from app.models import InstrSection

    cid, _ = term
    script = Script(call("ask_owner", question="Где кнопка печатной формы?"), call("save_document", supplier="Армтек", label="№5555"), call("finish", summary="ok"))
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    real_sleep = time.sleep
    monkeypatch.setattr("app.assembly_run.time.sleep", lambda s: real_sleep(0.05))
    r = client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-01", "date_to": "2026-10-31"})
    rid = r.json()["id"]
    q = None
    for _ in range(100):
        got = client.get(f"/api/assembly/runs/{rid}", headers=owner).json()
        if got["question"]:
            q = got["question"]
            break
        real_sleep(0.05)
    assert q and q["question"] == "Где кнопка печатной формы?"
    assert client.post(f"/api/assembly/questions/{q['id']}/answer", headers=owner, json={"answer": "  "}).status_code == 422
    assert client.post(f"/api/assembly/questions/{q['id']}/answer", headers=owner, json={"answer": "Меню Печать вверху документа"}).status_code == 200
    assert client.post(f"/api/assembly/questions/{q['id']}/answer", headers=owner, json={"answer": "ещё"}).status_code == 409
    run = _wait(client, owner, rid)
    assert run["status"] == "done" and run["qa"][0]["answer"] == "Меню Печать вверху документа"
    with SessionLocal() as db:
        sec = db.scalar(select(InstrSection).where(InstrSection.title.like("Уточнения агента%")))
        assert sec and "Где кнопка печатной формы?" in sec.notes and "Меню Печать вверху документа" in sec.notes
        from app.assembly_run import instructions_text

        assert "Меню Печать вверху документа" in instructions_text(db)
    hist = client.get("/api/assembly/runs", headers=owner).json()
    assert hist[0]["qa"] and hist[0]["id"] == rid


def test_assembly_waits_for_desktop(client, owner, term, monkeypatch):
    cid, _ = term
    monkeypatch.setattr(rdp_manager, "is_open", lambda key: False)
    monkeypatch.setattr(rdp_manager, "open", lambda *a, **k: {"width": 1280, "height": 800})
    seq = iter([True, True, False])
    monkeypatch.setattr(rdp_manager, "is_blank", lambda key: next(seq, False))
    monkeypatch.setattr("app.assembly_run.time.sleep", lambda s: None)
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: Script(call("save_document", supplier="Армтек", label="№9999"), call("finish", summary="ok")))
    r = client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-01", "date_to": "2026-10-31"})
    run = _wait(client, owner, r.json()["id"])
    assert run["status"] == "done" and any("Жду, пока" in x for x in run["log"])


PRINT_FORM = (
    "Сборка № 000006738 от 04.10.2026\t\t\t\n"
    "Поставщик: Армтек\t\t\t\n"
    "№\tАртикул\tНаименование\tКоличество\tНомер счета\n"
    "1\tSLKH073\tФильтр масляный\t2\tЮП0663567 /3\n"
    "2\tA-55\tКолодки\t1,000\tЮП0622527\n"
    "\tИтого\t\t3\t\n"
)


def test_assembly_creates_return_document_and_remembers(client, owner, term, monkeypatch):
    from app.models import InstrSection, ReturnBatch, ReturnLine

    cid, _ = term
    monkeypatch.setattr(rdp_manager, "clipboard", lambda key: PRINT_FORM)
    script = Script(
        call("remember_1c", topic="Журнал Документы Сборка", how="Склад → Сборки, период кнопкой над списком"),
        call("remember_1c", topic="журнал документы сборка", how="Склад → Сборки товаров; период: «Установить период»"),
        call("save_document", supplier="армтек", label="№000006738 от 04.10.2026"),
        call("finish", summary="Сохранил 1", found=1, remaining=0),
    )
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    run = _wait(client, owner, client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-04", "date_to": "2026-11-03"}).json()["id"])
    assert run["status"] == "done" and "Документов возврата создано: 1" in run["summary"], run
    assert run["saved"][0]["batch_id"]
    with SessionLocal() as db:
        b = db.scalars(select(ReturnBatch)).one()
        assert b.number == "№000006738 от 04.10.2026" and str(b.doc_date) == "2026-10-04"
        lines = db.scalars(select(ReturnLine).where(ReturnLine.batch_id == b.id).order_by(ReturnLine.id)).all()
        assert [(x.article, x.qty, x.invoice_no) for x in lines] == [("SLKH073", 2, "ЮП0663567 /3"), ("A-55", 1, "ЮП0622527")]
        mem = db.scalar(select(InstrSection).where(InstrSection.title.like("Память агентов 1С%")))
        assert mem.notes.count("•") == 1 and "Сборки товаров" in mem.notes  # та же тема перезаписана
    # память идёт первой в поручении следующего агента
    from app.assembly_run import instructions_text

    with SessionLocal() as db:
        assert instructions_text(db).startswith("### Память агентов 1С")


def test_assembly_rejects_early_finish_and_reconnects(client, owner, term, monkeypatch):
    cid, _ = term
    alive = {"v": True, "opens": 0}
    monkeypatch.setattr(rdp_manager, "is_open", lambda key: alive["v"])

    def fake_open(*a, **k):
        alive["opens"] += 1
        alive["v"] = True

    monkeypatch.setattr(rdp_manager, "open", fake_open)
    monkeypatch.setattr(rdp_manager, "is_blank", lambda key: False)

    def click(*a, **k):
        alive["v"] = False  # сеанс Windows завершился прямо во время действия
        from app.browser import BrowserError

        raise BrowserError("Подключение к терминалу завершилось")

    monkeypatch.setattr(rdp_manager, "click", click)
    script = Script(
        call("finish", summary="Сохранён 1, остальное не успел", found=14, remaining=13),
        call("rdp_click", x=10, y=10),
        call("finish", summary="Всё", found=14, remaining=0),
    )
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    run = _wait(client, owner, client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-04", "date_to": "2026-11-03"}).json()["id"])
    log = "\n".join(run["log"])
    assert "осталось 13 документов" in log
    assert "переподключаюсь (1 из" in log and alive["opens"] == 1
    assert run["status"] == "done" and run["summary"].startswith("Всё")


def test_assembly_continues_itself_when_documents_left(client, owner, term, monkeypatch):
    cid, _ = term
    monkeypatch.setattr(rdp_manager, "close", lambda key: None)
    script = Script(*[call("finish", summary="Не успел", found=5, remaining=4)] * 4)
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    first = client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-10-04", "date_to": "2026-11-03"}).json()["id"]
    run = _wait(client, owner, first)
    assert "продолжена новым запуском" in run["summary"]
    for _ in range(100):
        runs = client.get("/api/assembly/runs", headers=owner).json()
        if len(runs) > 1:
            break
        time.sleep(0.1)
    assert any("[автопродолжение 1]" in (r["note"] or "") for r in runs)


def test_return_document_from_saved_file_and_view(client, owner):
    from app.assembly_run import save_to_files

    with SessionLocal() as db:
        f = save_to_files(db, 1, "Армтек", "000006738 от 04.10.2026", "", PRINT_FORM)
        fid = f.id
    view = client.get(f"/api/files/{fid}/view", headers=owner).json()
    assert view["kind"] == "table" and view["rows"][2][1] == "Артикул"
    assert client.get(f"/api/returns/from-file/{fid}", headers=owner).json()["batch_id"] is None
    r = client.post(f"/api/returns/from-file/{fid}", headers=owner)
    assert r.status_code == 201, r.text
    assert client.get(f"/api/returns/from-file/{fid}", headers=owner).json()["batch_id"]
    # повторно: тот же документ заменяется, а не дублируется
    assert client.post(f"/api/returns/from-file/{fid}", headers=owner).status_code == 201
    from app.models import ReturnBatch

    with SessionLocal() as db:
        assert len(db.scalars(select(ReturnBatch)).all()) == 1


def test_print_form_without_goods_is_explained():
    from app.assembly_returns import parse_print_form

    with pytest.raises(ValueError, match="шапка"):
        parse_print_form("Номер\tДата\n7\t01.11.2026\n")
