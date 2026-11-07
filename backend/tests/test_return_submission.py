import json
from types import SimpleNamespace

import pytest

from app.browser import BrowserError
from app.db import SessionLocal
from app.models import ReturnLine, ReturnRun
from app.return_submission import APPROVAL_PREFIX, validate_preview
from tests.test_agents import Script, call
from tests.test_returns import SAMPLE
from tests.test_returns_run import _batch, _last, no_browser


class FormBrowser:
    def __init__(self, preview, fail=False):
        self.preview = preview
        self.text = "Форма перед отправкой"
        self.clicks = 0
        self.fail = fail

    def open(self, *args):
        pass

    def close(self, *args):
        pass

    def screenshot(self, *args):
        return b"image"

    def submission_preview(self, *args):
        return self.preview

    def read_text(self, *args, **kwargs):
        return self.text

    def send_return(self, *args):
        self.clicks += 1
        if self.fail:
            raise BrowserError("Ответ потерян")
        self.text = "Заявка 987654321 отправлена"


def setup_submission(client, owner, monkeypatch, fail=False):
    bid = _batch(client, owner, "https://itrade.forum-auto.ru")
    with SessionLocal() as db:
        row = db.query(ReturnLine).filter_by(batch_id=bid).first()
        line_id = row.id
        article, brand, qty = row.article, row.brand, row.qty
    browser = FormBrowser({
        "url": "https://itrade.forum-auto.ru/shop/returns/0.html?lid=1",
        "label": "Отправить заявку",
        "text": f"Создание нового возврата {article} {brand}",
        "values": "Отказ клиента",
        "quantities": [qty],
    }, fail=fail)
    monkeypatch.setattr("app.returns_run.manager", browser)
    script = Script(
        call("submit_return", line_ids=[line_id], x=10, y=10),
        call("record_submission", request_no="987654321"),
        call("finish", reason="blocked", summary="Остальные позиции требуют проверки"),
    )
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    return bid, line_id, browser, script


def test_submission_records_receipt_and_protects_history(client, owner, monkeypatch):
    bid, line_id, browser, script = setup_submission(client, owner, monkeypatch)
    url = f"/api/returns/batches/{bid}/submit-run"
    assert client.post(url, headers=owner, json={"confirmed": False}).status_code == 422
    assert browser.clicks == 0
    response = client.post(url, headers=owner, json={"confirmed": True})
    assert response.status_code == 202, response.text
    assert browser.clicks == 1
    with SessionLocal() as db:
        row = db.get(ReturnLine, line_id)
        assert row.status == "submitted" and row.request_no == "987654321"
        assert "Возврат оформлен" in row.note
        run = db.query(ReturnRun).filter_by(batch_id=bid).order_by(ReturnRun.id.desc()).first()
        assert json.loads(run.log)[0].startswith(APPROVAL_PREFIX)
    assert all(not entry.startswith(APPROVAL_PREFIX) for entry in _last(client, owner, bid)["log"])
    assert client.delete(f"/api/returns/batches/{bid}", headers=owner).status_code == 409
    assert client.post(f"/api/returns/batches/{bid}/reset-errors", headers=owner).status_code == 200
    with SessionLocal() as db:
        row = db.get(ReturnLine, line_id)
        assert row.status == "submitted" and row.request_no == "987654321"
    # Same document cannot overwrite the sent positions.
    with SessionLocal() as db:
        from app.models import ReturnBatch
        supplier_id = db.get(ReturnBatch, bid).supplier_id
    r = client.post(f"/api/returns/import?supplier_id={supplier_id}", headers=owner,
                    files={"file": ("r.csv", SAMPLE.read_bytes(), "text/csv")})
    assert r.status_code == 409
    assert client.delete(f"/api/returns/suppliers/{supplier_id}", headers=owner).status_code == 409
    # A subsequent confirmed run excludes the submitted item from its task.
    script = Script(call("finish", reason="blocked", summary="Тест"))
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(url, headers=owner, json={"confirmed": True})
    assert f"line_id={line_id}:" not in script.seen[0][0][0]["content"]
    assert browser.clicks == 1


def test_lost_reply_locks_attempt_instead_of_sending_again(client, owner, monkeypatch):
    bid, line_id, browser, script = setup_submission(client, owner, monkeypatch, fail=True)
    client.post(f"/api/returns/batches/{bid}/submit-run", headers=owner, json={"confirmed": True})
    assert browser.clicks == 1
    with SessionLocal() as db:
        row = db.get(ReturnLine, line_id)
        assert row.status == "uncertain" and not row.request_no
    assert client.delete(f"/api/returns/batches/{bid}", headers=owner).status_code == 409
    client.post(f"/api/returns/batches/{bid}/reset-errors", headers=owner)
    with SessionLocal() as db:
        assert db.get(ReturnLine, line_id).status == "uncertain"
    assert _last(client, owner, bid)["status"] == "failed"


def test_form_validation_rejects_wrong_quantity_and_extra_goods():
    row = SimpleNamespace(article="34 20-6880079", brand="BMW", qty=1, invoice_no="ЮП0682173 /3")
    preview = {
        "url": "https://etp.armtek.ru/invoice/viewReturnInfo/1/2",
        "label": "Оформить возврат",
        "text": "Оформить возврат по фактуре",
        "page_text": "УПД ЮП0682173 /3",
        "values": "Отказ клиента (годный товар)",
        "items": [{"article": "34206880079", "brand": "BMW", "qty": 1}],
    }
    validate_preview(preview, [row], "etp.armtek.ru")
    preview["items"][0]["qty"] = 2
    with pytest.raises(BrowserError):
        validate_preview(preview, [row], "etp.armtek.ru")
    preview["items"].append({"article": "OTHER", "brand": "BMW", "qty": 1})
    with pytest.raises(BrowserError):
        validate_preview(preview, [row], "etp.armtek.ru")


def test_restart_preserves_unknown_submission(client, owner, monkeypatch):
    from app.returns_run import reset_stale
    bid = _batch(client, owner, "https://etp.armtek.ru")
    with SessionLocal() as db:
        row = db.query(ReturnLine).filter_by(batch_id=bid).first()
        line_id = row.id
        row.status = "submitting"
        db.add(ReturnRun(batch_id=bid, mode="submit", status="running"))
        db.commit()
    reset_stale()
    with SessionLocal() as db:
        assert db.get(ReturnLine, line_id).status == "uncertain"


def test_submit_tools_not_available_without_owner_approval(client, owner, monkeypatch):
    bid, line_id, browser, script = setup_submission(client, owner, monkeypatch)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    assert browser.clicks == 0
    with SessionLocal() as db:
        assert db.get(ReturnLine, line_id).status != "submitted"


def test_irreversible_browser_action_is_never_retried():
    from app.browser import BrowserManager
    browser = BrowserManager()
    attempts = []

    async def failing_action():
        attempts.append(1)
        raise RuntimeError("navigation interrupted")

    with pytest.raises(BrowserError):
        browser._run(failing_action, retry=False)
    assert len(attempts) == 1


def test_submission_requires_owner_role(client, owner, monkeypatch):
    from tests.conftest import login
    bid, _, browser, _ = setup_submission(client, owner, monkeypatch)
    client.post("/api/users", headers=owner, json={
        "email": "staff-submit@test.local", "password": "worker-pass-1234", "full_name": "Сотрудник"
    })
    staff = login(client, "staff-submit@test.local", "worker-pass-1234")
    assert client.post(f"/api/returns/batches/{bid}/submit-run", headers=staff,
                       json={"confirmed": True}).status_code == 403
    assert browser.clicks == 0


def test_run_continues_after_step_window_when_items_completed(client, owner, monkeypatch, no_browser):
    from tests.test_agents import say
    bid = _batch(client, owner, "https://etp.armtek.ru")
    with SessionLocal() as db:
        ids = [r.id for r in db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id)]
    script = Script(
        call("record_item_result", line_id=ids[0], status="error", note="Нет товара"),
        say("Продолжаю"),
        call("record_item_result", line_id=ids[1], status="error", note="Нет товара"),
        call("finish", reason="blocked", summary="Нужен вход"),
    )
    monkeypatch.setattr("app.returns_run.MAX_STEPS", 2)
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    assert "Продолжаю оформление" in "\n".join(_last(client, owner, bid)["log"])
    with SessionLocal() as db:
        assert db.get(ReturnLine, ids[1]).status == "failed"



def test_supported_vendor_domains():
    from app.return_submission import supplier_kind
    assert supplier_kind("https://etp.armtek.ru") == "armtek"
    assert supplier_kind("https://itrade.forum-auto.ru") == "forum"
    assert supplier_kind("https://forum-auto.ru.evil.example") is None
    assert supplier_kind("http://itrade.forum-auto.ru") is None


def test_unknown_submission_is_parked_and_next_item_continues(client, owner, monkeypatch):
    bid, line_id, browser, _ = setup_submission(client, owner, monkeypatch)
    visits = []
    browser.wait_response = lambda *args: None
    with SessionLocal() as db:
        row = db.query(ReturnLine).filter(ReturnLine.batch_id == bid, ReturnLine.id != line_id).first()
        next_id, article, brand, qty = row.id, row.article, row.brand, row.qty

    def goto(*args):
        visits.append(args[-1])
        browser.preview = {**browser.preview, "text": f"Создание нового возврата {article} {brand}", "quantities": [qty]}

    def send(*args):
        browser.clicks += 1
        if browser.clicks == 2:
            browser.text = "Заявка 987654322 отправлена"

    browser.goto, browser.send_return = goto, send
    script = Script(
        call("submit_return", line_ids=[line_id], x=10, y=10),
        call("browser_wait"),
        call("finish", reason="blocked", summary="Нет номера заявки, результат требует сверки"),
        call("submit_return", line_ids=[next_id], x=10, y=10),
        call("record_submission", request_no="987654322"),
        call("finish", reason="blocked", summary="Сессия истекла"),
    )
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/submit-run", headers=owner, json={"confirmed": True})
    assert not script.steps and browser.clicks == 2
    assert visits == ["https://itrade.forum-auto.ru"]
    with SessionLocal() as db:
        assert db.get(ReturnLine, line_id).status == "uncertain"
        assert db.get(ReturnLine, next_id).status == "submitted"
        assert db.get(ReturnLine, next_id).request_no == "987654322"
        run = db.query(ReturnRun).filter_by(batch_id=bid).first()
        from app.return_submission import read_approval
        approval = read_approval(run)
        assert "attempt" not in approval
        assert approval["unresolved_attempts"][0]["ids"] == [line_id]
    assert "Требуют сверки отправки: 1" in _last(client, owner, bid)["summary"]
    client.post(f"/api/returns/batches/{bid}/reset-errors", headers=owner)
    with SessionLocal() as db:
        assert db.get(ReturnLine, line_id).status == "uncertain"
