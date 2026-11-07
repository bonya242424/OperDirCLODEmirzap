import json

import pytest

from app.db import SessionLocal
from app.models import AppSetting, ReturnLine
from app.return_verification import MIGRATION_KEY, invalidate_legacy, on_page
from tests.test_agents import Script, call as raw_call
from tests.test_returns_run import _batch, _last, call, no_browser


def test_invoice_bulk_result_and_other_article_cannot_change_rows(client, owner, monkeypatch, no_browser):
    bid = _batch(client, owner, "https://example.com")
    with SessionLocal() as db:
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id))
        first, other = rows[:2]
        first.article, first.brand = "BNZ1221ZW", "KRAUF"
        other.article, other.brand = "03C115561H", "VAG"
        first.invoice_no = other.invoice_no = "251026-246"
        ids = first.id, other.id
        db.commit()
    script = Script(
        raw_call("record_result", invoice_no="251026-246", status="ok", note="03C115561H найден"),
        raw_call("record_item_result", line_id=ids[0], article="03C115561H", brand="VAG", available_qty=2, status="ok", note="Найден"),
        call("finish", reason="blocked", summary="Стоп"),
    )
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    with SessionLocal() as db:
        assert all(db.get(ReturnLine, lid).status == "pending" for lid in ids)
    assert "весь счёт запрещена" in "\n".join(_last(client, owner, bid)["log"])

    script = Script(call("record_item_result", line_id=ids[0], status="ok", note="03C115561H найден"),
                    call("finish", reason="blocked", summary="Стоп"))
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    with SessionLocal() as db:
        row = db.get(ReturnLine, ids[0])
        assert row.status == "checked"
        assert "BNZ1221ZW" in row.note and "03C115561H" not in row.note
        assert json.loads(row.check_evidence)["article"] == "BNZ1221ZW"
        assert db.get(ReturnLine, ids[1]).status == "pending"


def test_missing_matching_product_on_page_cannot_be_verified(client, owner, monkeypatch, no_browser):
    from app import returns_run
    bid = _batch(client, owner, "https://example.com")
    with SessionLocal() as db:
        row = db.query(ReturnLine).filter_by(batch_id=bid).first()
        lid = row.id
    returns_run.manager.read_text = lambda *args, **kwargs: "03C115561H VAG доступно 2"
    script = Script(call("record_item_result", line_id=lid, status="ok", note="ОК"), call("finish", reason="blocked", summary="Стоп"))
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    with SessionLocal() as db:
        assert db.get(ReturnLine, lid).status == "pending"


def test_legacy_checks_are_invalidated_without_touching_submissions(client, owner):
    bid = _batch(client, owner, "https://example.com")
    with SessionLocal() as db:
        db.delete(db.get(AppSetting, MIGRATION_KEY))
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id))
        for row, state in zip(rows, ["checked", "failed", "submitted", "uncertain", "submitting", "checked", "checked"]):
            row.status, row.note = state, "03C115561H найден"
        rows[2].request_no = "sent-123"
        rows[5].request_no = "protected-123"
        rows[6].check_evidence = '{"version":1}'
        db.commit()
        invalidate_legacy(db)
        assert [row.status for row in rows[:7]] == ["pending", "pending", "submitted", "uncertain", "submitting", "checked", "checked"]
        assert json.loads(rows[0].check_evidence)["legacy_note"] == "03C115561H найден"
        assert "03C115561H" not in rows[0].note
        rows[0].status = "failed"
        db.commit()
        invalidate_legacy(db)
        assert rows[0].status == "failed"


@pytest.mark.parametrize("article,text,matched", [
    ("ALN3470ZD", "KRAUF ALN-3470/ZD генератор", True),
    ("BNZ1221ZW", "BNZ1221ZW KRAUF", True),
    ("BNZ1221ZW", "BNZ1221ZW2 KRAUF", False),
    ("BNZ1221ZW", "03C115561H VAG", False),
])
def test_product_evidence_uses_complete_article(article, text, matched):
    assert on_page(article, text) is matched
