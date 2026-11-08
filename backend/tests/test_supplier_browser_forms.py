"""Изолированный Chromium: все URL перехватываются, реальные кабинеты не вызываются."""
import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from playwright.async_api import async_playwright

from app.browser import BrowserError, BrowserManager, _Session
from app.return_submission import validate_preview

FORM = """<!doctype html><meta charset=utf-8>
<form><h2>Возврат товара. Поставка 123456-99</h2>
<table><thead><tr><th></th><th>Артикул</th><th>Бренд</th><th>Количество к возврату</th></tr></thead>
<tbody>
<tr><td><input type=checkbox checked></td><td>305664</td><td>LOBRO</td><td><input type=hidden name=ProductId value=987><input type=number name=quantity min=0 value=1></td></tr>
<tr><td><input type=checkbox></td><td>OTHER</td><td>OTHER</td><td><input type=number name=otherQuantity min=0 value=3></td></tr>
<tr><td colspan=3>Итого</td><td>1</td></tr>
</tbody></table>
<label>Причина<input name=reason value="Отказ" required></label>
<button type=button id=final>Вернуть</button></form>"""


async def parsed_form(markup):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.route("https://lk.favorit-parts.ru/**", lambda route: route.fulfill(content_type="text/html", body=markup))
            await page.goto("https://lk.favorit-parts.ru/returns/new")
            manager = BrowserManager()
            manager._sessions[1] = _Session(context, page)
            box = await page.locator("#final").bounding_box()
            return await manager._submission_preview(1, box["x"] + box["width"]/2, box["y"] + box["height"]/2)
        finally:
            await browser.close()


def expected():
    return SimpleNamespace(article="305664", brand="LOBRO", qty=1, invoice_no="123456-99", check_evidence="")


def test_real_form_parser_ignores_unselected_offer_and_total_but_not_product():
    data = asyncio.run(parsed_form(FORM))
    assert len(data["items"]) == 1 and data["items"][0]["qty"] == 1
    assert not data["unmapped_quantities"]
    validate_preview(data, [expected()], "lk.favorit-parts.ru")


@pytest.mark.parametrize("markup", [
    FORM.replace("<input type=checkbox>", "<input type=checkbox checked>"),
    FORM.replace("name=quantity min=0 value=1", "name=quantity min=0 value=-1"),
    FORM.replace("</form>", '<input type=text name=extraQuantity value="1,0"></form>'),
    FORM.replace("</form>", '<input type=hidden name=hiddenQuantity value="2"></form>'),
])
def test_real_form_parser_rejects_extra_or_unmapped_quantities(markup):
    data = asyncio.run(parsed_form(markup))
    with pytest.raises(BrowserError):
        validate_preview(data, [expected()], "lk.favorit-parts.ru")


def test_real_login_submits_once_and_confirms_authenticated_session():
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                context = await browser.new_context()
                page = await context.new_page()
                requests = []
                state = {"authenticated": False}
                async def handler(route):
                    if route.request.method == "POST":
                        fields = parse_qs(route.request.post_data)
                        assert fields == {"username": ["fake-account"], "password": ["fake-password"]}
                        requests.append("login")
                        state["authenticated"] = True
                    if state["authenticated"]:
                        body = '<a href="/logout">Выйти</a><h1>Личный кабинет</h1>'
                        headers = {"Set-Cookie": "test_session=fake-session; HttpOnly; Secure; Path=/"}
                    else:
                        body = '<form action="/login" method="post"><input name="username" autocomplete="username"><input name="password" type="password"><button type="submit">Войти</button></form>'
                        headers = {}
                    await route.fulfill(content_type="text/html", body='<!doctype html><meta charset=utf-8>' + body, headers=headers)
                await page.route("https://lk.favorit-parts.ru/**", handler)
                manager = BrowserManager()
                manager._sessions[1] = _Session(context, page)
                result = await manager._login(1, "https://lk.favorit-parts.ru/", "fake-account", "fake-password", "https://lk.favorit-parts.ru/returns")
                assert requests == ["login"]
                assert result["cookies"]
                assert await manager._session_authenticated(1)
            finally:
                await browser.close()
    asyncio.run(check())
