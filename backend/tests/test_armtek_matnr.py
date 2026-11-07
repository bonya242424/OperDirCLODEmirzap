import asyncio
from types import SimpleNamespace

import pytest

from app.browser import BrowserError, BrowserManager, _Session


def test_matnr_repair_requeues_only_approved_technical_failures(client, owner):
    import json
    from app.db import SessionLocal
    from app.models import ReturnBatch, ReturnJob, ReturnLine, ReturnRun, ReturnSupplier
    from app.return_jobs import seed_recent_jobs
    from app.return_submission import approval_log
    from tests.test_returns_run import _batch

    bid = _batch(client, owner, "https://etp.armtek.ru")
    with SessionLocal() as db:
        batch = db.get(ReturnBatch, bid)
        supplier = db.get(ReturnSupplier, batch.supplier_id)
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id))
        for row in rows:
            row.status = "failed"
            row.note = "Товар недоступен"
        rows[0].check_evidence = json.dumps({"technical_skip": True, "automation_restarts": 2})
        rows[1].status, rows[1].request_no = "submitted", "71822149"
        rows[2].status = "uncertain"
        log = json.loads(approval_log(rows, supplier.portal_url)) + ["Отказ: Выберите поле поиска артикула из browser_controls"]
        db.add(ReturnRun(batch_id=bid, created_by=supplier.created_by, mode="submit", status="failed", log=json.dumps(log)))
        db.commit()
    seed_recent_jobs("matnr-test", armtek_inputs=True)
    seed_recent_jobs("matnr-test", armtek_inputs=True)
    with SessionLocal() as db:
        assert db.get(ReturnLine, rows[0].id).status == "pending"
        assert json.loads(db.get(ReturnLine, rows[0].id).check_evidence)["automation_restarts"] == 0
        assert db.get(ReturnLine, rows[1].id).request_no == "71822149"
        assert db.get(ReturnLine, rows[2].id).status == "uncertain"
        assert db.get(ReturnLine, rows[3].id).status == "failed"
        assert db.query(ReturnJob).count() == 1


@pytest.mark.parametrize("control", ["", "MATNR", "15-34"])
def test_stale_or_missing_control_resolves_matnr(control):
    async def check():
        writes = []

        async def info(*args):
            return {"tag": "INPUT", "type": "text", "name": "MATNR", "label": ""}

        async def count():
            return 1

        async def fill(value):
            writes.append(value)

        async def type_text(value, **kwargs):
            writes.append(value)

        async def actual():
            return "6V0867174 9B9"

        async def noop(*args):
            return []

        async def stale(*args):
            raise BrowserError("Элемент изменился")

        element = SimpleNamespace(count=count, evaluate=info, fill=fill, press_sequentially=type_text, input_value=actual)
        page = SimpleNamespace(locator=lambda selector: element, wait_for_timeout=noop, evaluate=noop)
        manager = BrowserManager()
        manager._sessions[1] = _Session(None, page)
        manager._control, manager._controls = stale, noop
        result = await manager._search_article(1, control, "6V0867174 9B9")
        assert writes == ["", "6V0867174 9B9"]
        assert result["input_value"] == "6V0867174 9B9"

    asyncio.run(check())


@pytest.mark.parametrize("count,type_", [(0, "text"), (2, "text"), (1, "password"), (1, "date")])
def test_ambiguous_or_wrong_matnr_never_receives_article(count, type_):
    async def check():
        async def amount():
            return count

        async def info(*args):
            return {"tag": "INPUT", "type": type_, "name": "MATNR", "label": ""}

        candidate = SimpleNamespace(count=amount, evaluate=info)
        manager = BrowserManager()
        manager._sessions[1] = _Session(None, SimpleNamespace(locator=lambda selector: candidate))
        with pytest.raises(BrowserError, match="единственное"):
            await manager._search_article(1, "", "AR113")

    asyncio.run(check())


def test_real_dom_controls_stay_stable_and_matnr_accepts_keyboard_input():
    async def check():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            except Exception as exc:
                pytest.skip(f"chromium недоступен: {str(exc).splitlines()[0]}")
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content('<input name="DATE_FROM" value="08.09.2026"><input name="MATNR"><button>Получить</button>')
                manager = BrowserManager()
                manager._sessions[1] = _Session(context, page)
                first = await manager._controls(1)
                second = await manager._controls(1)
                assert [c["control"] for c in first] == [c["control"] for c in second]
                matnr = next(c for c in first if c["name"] == "MATNR")
                assert matnr["label"] == "MATNR"
                await manager._search_article(1, matnr["control"], "AR113")
                await manager._search_article(1, "MATNR", "6V0867174 9B9")
                assert await page.locator('[name="MATNR"]').input_value() == "6V0867174 9B9"
                assert await page.locator('[name="DATE_FROM"]').input_value() == "08.09.2026"
                assert (await manager._controls(1))[1]["control"] == matnr["control"]
            finally:
                await browser.close()

    asyncio.run(check())
