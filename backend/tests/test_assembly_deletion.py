from datetime import date

import pytest

from app.browser import BrowserError
from app.db import SessionLocal
from app.models import AssemblyRun, ReturnBatch, StoredFile, ReturnLine
from app import assembly_scope
from app.assembly_run import _tool
from tests.test_assembly import term, PRINT_FORM


@pytest.mark.parametrize("method", ["save_document", "save_document_text"])
def test_unknown_or_deleted_status_cannot_save_even_when_posted(client, owner, term, method):
    cid, _ = term
    with SessionLocal() as db:
        run = AssemblyRun(connection_id=cid, date_from=date(2026, 11, 6), date_to=date(2026, 11, 6))
        db.add(run)
        db.commit()
        args = {"supplier": "Москворечье", "label": "№000007094 от 06.11.2026", "text": PRINT_FORM}
        with pytest.raises(BrowserError, match="record_document_state"):
            _tool(db, 1, run, "", method, args, [], lambda _: None)
        assembly_scope.record_state(db, run, args["supplier"], args["label"], True, False,
            "Виден красный крестик одновременно с зелёной отметкой проведения")
        with pytest.raises(BrowserError, match="запрещено"):
            _tool(db, 1, run, "", method, args, [], lambda _: None)
        assert db.query(StoredFile).count() == 0
        assert db.query(ReturnBatch).count() == 0
        assert assembly_scope.scope(db, run)["skipped"]["7094"]["kind"] == "deleted"


def test_posted_without_deletion_mark_allows_save_and_is_bound_to_document(client, owner, term):
    cid, _ = term
    with SessionLocal() as db:
        run = AssemblyRun(connection_id=cid, date_from=date(2026, 11, 6), date_to=date(2026, 11, 6))
        db.add(run)
        db.commit()
        label = "№000007128 от 06.11.2026"
        assembly_scope.record_state(db, run, "Москворечье", label, True, False, "Зелёная отметка; красного крестика нет")
        assembly_scope.require_eligible(db, run, "Москворечье", label)
        with pytest.raises(BrowserError):
            assembly_scope.require_eligible(db, run, "Москворечье", "№000007094 от 06.11.2026")
        with pytest.raises(BrowserError):
            assembly_scope.require_eligible(db, run, "Другой", label)
        with pytest.raises(BrowserError):
            assembly_scope.require_eligible(db, run, "Москворечье", "№000007128 от 07.11.2026")
        out = _tool(db, 1, run, "", "save_document_text",
            {"supplier": "Москворечье", "label": label, "text": PRINT_FORM}, [], lambda _: None)
        assert "Сохранено" in out
        assert db.query(StoredFile).count() == 1


def test_unposted_and_unknown_flags_do_not_authorize_collection(client, owner, term):
    cid, _ = term
    with SessionLocal() as db:
        run = AssemblyRun(connection_id=cid, date_from=date(2026, 11, 6), date_to=date(2026, 11, 6))
        db.add(run)
        db.commit()
        with pytest.raises(BrowserError):
            assembly_scope.record_state(db, run, "Шате", "№7141 от 06.11.2026", None, False, "Не видно проведение")
        assembly_scope.record_state(db, run, "Шате", "№7141 от 06.11.2026", False, False, "Нет проведения")
        with pytest.raises(BrowserError):
            assembly_scope.require_eligible(db, run, "Шате", "№7141 от 06.11.2026")


def test_reported_deleted_source_is_quarantined_without_erasing_history(client, owner):
    from tests.test_returns_run import _batch
    from app.return_submission import eligible_rows
    bid = _batch(client, owner, "https://itrade.forum-auto.ru")
    with SessionLocal() as db:
        batch = db.get(ReturnBatch, bid)
        batch.supplier, batch.number = "москворечье возвраты (1)", "Сборка 000007094 от 06.11.2026 00:28:33"
        row = db.query(ReturnLine).filter_by(batch_id=bid).first()
        row.status, row.request_no = "submitted", "123456789"
        db.commit()
        from app.models import AppSetting
        db.query(AppSetting).filter_by(key="assembly-reported-deleted-7094-20261106").delete()
        assembly_scope.seed_reported_deleted(db)
        assert assembly_scope.source_block_reason(db, batch)
        assert "ИСКЛЮЧЁН" in batch.source
        assert eligible_rows(db, bid) == []
        assert row.status == "submitted" and row.request_no == "123456789"
        assert db.get(ReturnBatch, bid) is not None
    response = client.post(f"/api/returns/batches/{bid}/submit-run", headers=owner, json={"confirmed": True})
    assert response.status_code == 409
    assert "исключена" in response.json()["detail"]
