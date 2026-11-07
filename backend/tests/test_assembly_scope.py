import json
from datetime import date

from app import assembly_scope
from app.assembly_run import continue_run
from app.db import SessionLocal
from app.models import AssemblyQuestion, AssemblyRun
from tests.test_assembly import term, _wait, PRINT_FORM
from tests.test_agents import Script, call, say
from app.rdp import rdp_manager


def test_explicit_date_and_owner_answers_survive_continuation(client, owner, term):
    cid, _ = term
    with SessionLocal() as db:
        run = AssemblyRun(connection_id=cid, date_from=date(2026, 10, 9), date_to=date(2026, 11, 8), note="Собери все сборки за 06.11.2026")
        db.add(run)
        db.commit()
        assembly_scope.scope(db, run)
        assert run.date_from == run.date_to == date(2026, 11, 6)
        db.add(AssemblyQuestion(run_id=run.id, question="Все контрагенты?", answer="Только 11 поставщиков из задания, только проведённые"))
        db.commit()
        assembly_scope.register(db, run, ["7141", "7142", "7143"], True)
        rid = continue_run(db, run)
        next_run = db.get(AssemblyRun, rid)
        assert next_run.inventory == run.inventory
        assert next_run.date_from == next_run.date_to == date(2026, 11, 6)
        assert "Только 11" in assembly_scope.clarifications(db, next_run)


def test_saved_one_of_three_does_not_complete_inventory(client, owner, term, monkeypatch):
    from app.assembly_run import save_to_files
    cid, _ = term
    with SessionLocal() as db:
        run = AssemblyRun(connection_id=cid, date_from=date(2026, 11, 6), date_to=date(2026, 11, 6))
        db.add(run)
        db.commit()
        assembly_scope.register(db, run, ["7141", "7142", "7143"], True)
        save_to_files(db, 1, "Шате", "№000007141 от 06.11.2026", "", PRINT_FORM)
        complete, left, errors = assembly_scope.progress(db, run)
        assert complete and left == ["7142", "7143"] and not errors
        assembly_scope.skip(db, run, "7142", "unposted", "Красный крестик: документ не проведён")
        assembly_scope.skip(db, run, "7143", "error", "Печатная форма не открывается")
        complete, left, errors = assembly_scope.progress(db, run)
        assert complete and not left and errors == ["7143"]


def test_one_supplier_cannot_confirm_entire_scope(client, owner, term):
    from app.models import ReturnSupplier
    cid, _ = term
    with SessionLocal() as db:
        db.add_all([ReturnSupplier(name="Армтек"), ReturnSupplier(name="Шате")])
        db.commit()
        run = AssemblyRun(connection_id=cid, date_from=date(2026, 11, 6), date_to=date(2026, 11, 6))
        db.add(run)
        db.commit()
        assembly_scope.register(db, run, ["7141"], True, source="000007141\tШате")
        assert not assembly_scope.progress(db, run)[0]
        assembly_scope.register(db, run, [], supplier="Армтек")  # явно проверен пустой отбор
        assert assembly_scope.progress(db, run)[0]


def test_plain_text_after_saved_file_is_not_success(client, owner, term, monkeypatch):
    cid, _ = term
    monkeypatch.setattr(rdp_manager, "clipboard", lambda key: PRINT_FORM)
    script = Script(call("save_document", supplier="Шате", label="№000007141 от 06.11.2026"),
                    say("Записал в память. Готово"), say("Готово"), say("Готово"), say("Готово"))
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    monkeypatch.setattr("app.assembly_run.MAX_CONTINUES", 0)
    run = _wait(client, owner, client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-11-06", "date_to": "2026-11-06"}).json()["id"])
    assert run["status"] == "partial" and run["saved"]


def test_finish_cannot_claim_zero_while_registered_documents_remain(client, owner, term, monkeypatch):
    cid, _ = term
    script = Script(call("check_saved", numbers=["7141", "7142"], all_suppliers=True),
                    *[call("finish", found=2, remaining=0, summary="Готово") for _ in range(4)])
    monkeypatch.setattr("app.assembly_run.get_provider", lambda: script)
    monkeypatch.setattr("app.assembly_run.MAX_CONTINUES", 0)
    run = _wait(client, owner, client.post("/api/assembly/run", headers=owner, json={"connection_id": cid, "date_from": "2026-11-06", "date_to": "2026-11-06"}).json()["id"])
    assert run["status"] == "partial"
    assert any("осталось 2" in row for row in run["log"])


def test_upgrade_rechecks_only_latest_unverified_all_task(client, owner, term, monkeypatch):
    from app.assembly_run import recover_unverified_completions
    from app.models import AppSetting
    from types import SimpleNamespace
    cid, _ = term
    started = []
    # threading импортируется внутри функции: запуск заменяем, сам терминал не открываем.
    monkeypatch.setattr("threading.Thread", lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs["args"][0])))
    with SessionLocal() as db:
        db.delete(db.get(AppSetting, "assembly-completion-recovery-v1"))
        run = AssemblyRun(connection_id=cid, date_from=date(2026, 10, 9), date_to=date(2026, 11, 8),
                          note="Собери все сборки за 06.11.2026 [автопродолжение 1]", status="done", created_by=1)
        db.add(run)
        db.commit()
    recover_unverified_completions()
    recover_unverified_completions()
    assert len(started) == 1
    with SessionLocal() as db:
        nxt = db.get(AssemblyRun, started[0])
        assert nxt.date_from == nxt.date_to == date(2026, 11, 6)
        assert db.get(AssemblyRun, run.id).status == "continued"
