import asyncio
from types import SimpleNamespace

import pytest

from app.browser import BrowserError, BrowserManager, _Session


def test_verified_product_gets_budget_to_finish_without_restarting(client, owner, monkeypatch):
    import json
    from app import returns_run
    from app.db import SessionLocal
    from app.models import ReturnLine, ReturnRun
    from app.return_submission import approval_log
    from tests.test_agents import Script
    from tests.test_returns_run import _batch, call

    bid = _batch(client, owner, "https://etp.armtek.ru")
    with SessionLocal() as db:
        row = db.query(ReturnLine).filter_by(batch_id=bid).first()
        run = ReturnRun(batch_id=bid, mode="submit", log=approval_log([row]))
        db.add(run)
        db.commit()
        rid, lid = run.id, row.id

    def tool(db, mgr, key, host, batch, name, args, done, say, run, approval):
        if name == "armtek_find_product":
            return json.dumps({"rows": [{"text": "Совпавший товар"}]})
        assert name == "record_item_result"
        db.get(ReturnLine, lid).status = "failed"
        db.get(ReturnLine, lid).note = "Товар найден, количество недоступно"
        db.commit()
        return "Сохранено"

    monkeypatch.setattr(returns_run, "_tool", tool)
    monkeypatch.setattr(returns_run, "MAX_ITEM_STEPS", 1)
    monkeypatch.setattr(returns_run, "manager", SimpleNamespace(open=lambda *a: None, close=lambda *a: None, screenshot=lambda *a: b"image"))
    script = Script(call("armtek_find_product", line_id=lid), call("record_item_result", line_id=lid, status="error"))
    monkeypatch.setattr(returns_run, "get_provider", lambda: script)
    returns_run.run_check(rid)
    with SessionLocal() as db:
        log = "\n".join(json.loads(db.get(ReturnRun, rid).log))
        assert "время на заполнение формы выделено отдельно" in log
        assert "Восстановление текущего товара" not in log
        assert db.get(ReturnLine, lid).note == "Товар найден, количество недоступно"


def browser_stub(values, responses):
    state = {"writes": [], "waits": [], "reads": list(values), "responses": list(responses)}

    async def info(*args):
        return {"tag": "INPUT", "type": "text", "name": "MATNR", "label": ""}

    async def fill(value):
        state["writes"].append(("clear", value))

    async def type_text(value, **kwargs):
        state["writes"].append((kwargs["delay"], value))

    async def actual():
        return state["reads"].pop(0) if len(state["reads"]) > 1 else state["reads"][0]

    async def wait(ms):
        state["waits"].append(ms)

    async def suggestions(*args):
        return state["responses"].pop(0) if len(state["responses"]) > 1 else state["responses"][0]

    async def element(*args):
        return SimpleNamespace(evaluate=info, fill=fill, press_sequentially=type_text, input_value=actual)

    async def controls(*args):
        return []

    manager = BrowserManager()
    manager._sessions[1] = _Session(None, SimpleNamespace(wait_for_timeout=wait, evaluate=suggestions))
    manager._control, manager._controls = element, controls
    return manager, state


def test_delayed_suggestions_are_read_before_trying_short_query():
    manager, state = browser_stub(["BNZ1046ZW"], [[], [], [], [], [], ["BNZ1046ZW KRAUF"]])
    result = asyncio.run(manager._search_article(1, "1-1", "BNZ1046ZW"))
    assert result["suggestions"] == ["BNZ1046ZW KRAUF"]
    assert result["waited_ms"] == 1250
    assert result["requested_value"] == result["input_value"] == "BNZ1046ZW"
    assert state["writes"] == [("clear", ""), (35, "BNZ1046ZW")]


def test_partial_input_is_retyped_and_verified():
    manager, state = browser_stub(["AR11", "AR113", "AR113"], [["AR113 AREOL"]])
    result = asyncio.run(manager._search_article(1, "1-1", "AR113"))
    assert result["input_value"] == "AR113"
    assert state["writes"] == [("clear", ""), (35, "AR113"), ("clear", ""), (100, "AR113")]


def test_persistent_truncation_is_not_reported_as_completed_search():
    manager, state = browser_stub(["BNZ1"], [[]])
    with pytest.raises(BrowserError, match="введён не полностью"):
        asyncio.run(manager._search_article(1, "1-1", "BNZ1046ZW"))
    assert len(state["writes"]) == 4


def test_input_changed_during_wait_is_rejected():
    manager, _ = browser_stub(["AR113", "AR11"], [["AR113 AREOL"]])
    with pytest.raises(BrowserError, match="Поле изменилось"):
        asyncio.run(manager._search_article(1, "1-1", "AR113"))


def test_empty_menu_wait_is_bounded():
    manager, state = browser_stub(["AR113"], [[]])
    result = asyncio.run(manager._search_article(1, "1-1", "AR113"))
    assert not result["suggestions"] and result["waited_ms"] == 8000
    assert len(state["writes"]) == 2


def test_real_dom_reads_linked_menu_without_known_css_class():
    async def check():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(args=["--no-sandbox"])
            except Exception as exc:
                pytest.skip(str(exc).splitlines()[0])
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content('''<input name="MATNR" aria-controls="products">
                    <div id="products"><div>AR113 AREOL</div><div>AR114 OTHER</div></div>''')
                await page.locator('input').focus()
                manager = BrowserManager()
                manager._sessions[1] = _Session(context, page)
                assert await manager._article_suggestions(1, "AR113") == ["AR113 AREOL"]
                controls = await manager._controls(1)
                assert any(c["label"] == "AR113 AREOL" for c in controls)
                # Крестик без button/a раньше наследовал рискованное название всей формы.
                await page.set_content('''<table><tr><td><div class="modal">
                    <span class="close" data-dismiss="modal" style="display:block;width:30px;height:30px"
                        onclick="window.closedByTest=true">×</span>
                    Оформить возврат по фактуре №9047322883
                    <button type="submit">Отправить заявку</button>
                    </div></td></tr></table>''')
                close = await page.locator('.close').bounding_box()
                await manager._safe_click(1, close["x"] + 5, close["y"] + 5)
                assert await page.evaluate('window.closedByTest') is True
                send = await page.locator('button').bounding_box()
                with pytest.raises(BrowserError, match="заблокирована"):
                    await manager._safe_click(1, send["x"] + 5, send["y"] + 5)
            finally:
                await browser.close()

    asyncio.run(check())
