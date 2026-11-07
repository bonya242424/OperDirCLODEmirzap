import http.server
import threading

import pytest

from app.browser import manager
from tests.conftest import login
from tests.test_returns import _supplier

PAGE = """<!doctype html><title>login</title>
<input id=p style="position:absolute;left:100px;top:100px;width:300px;height:40px" type=password>
<script>
document.getElementById('p').addEventListener('keydown', e => {
  if (e.key === 'Enter') { document.cookie = 'sid=' + document.getElementById('p').value.length + '; path=/'; document.title = 'logged'; }
});
</script>""".encode()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


@pytest.fixture()
def site():
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()
    manager.close(1)


def _b(sid, path):
    return f"/api/returns/suppliers/{sid}/browser/{path}"


def test_login_flow_saves_only_cookies(client, owner, site):
    sup = _supplier(client, owner, name="Сайт", portal_url=site).json()
    sid = sup["id"]
    r = client.post(_b(sid, "open"), headers=owner)
    if r.status_code == 409 and "браузер" in r.json()["detail"].lower():
        pytest.skip("chromium недоступен")
    assert r.status_code == 200, r.text
    img = client.get(_b(sid, "screen"), headers=owner)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content[:2] == b"\xff\xd8"
    assert client.post(_b(sid, "click"), headers=owner, json={"x": 200, "y": 120}).status_code == 204
    assert client.post(_b(sid, "type"), headers=owner, json={"text": "секрет123"}).status_code == 204
    assert client.post(_b(sid, "key"), headers=owner, json={"key": "Enter"}).status_code == 204
    assert client.get(_b(sid, "info"), headers=owner).json()["title"] == "logged"
    saved = client.post(_b(sid, "save"), headers=owner).json()
    assert saved["has_session"] and saved["session_saved_at"] and "секрет123" not in str(saved)
    from app.db import SessionLocal
    from app.models import ReturnSupplier

    with SessionLocal() as db:
        raw = db.get(ReturnSupplier, sid).session_encrypted
    assert raw and "sid" not in raw and "секрет123" not in raw  # зашифровано
    assert client.post(_b(sid, "close"), headers=owner).status_code == 204
    # повторное открытие поднимает сохранённый вход: cookie sid на месте
    assert client.post(_b(sid, "open"), headers=owner).status_code == 200
    assert "sid=9" in str(manager.state(sid)["cookies"]) or any(c["name"] == "sid" and c["value"] == "9" for c in manager.state(sid)["cookies"])
    assert client.delete(_b(sid, "session"), headers=owner).status_code == 204
    assert not next(x for x in client.get("/api/returns/suppliers", headers=owner).json() if x["id"] == sid)["has_session"]


def test_validation_and_permissions(client, owner):
    sid = _supplier(client, owner, name="Без сайта", portal_url="").json()["id"]
    assert client.post(_b(sid, "open"), headers=owner).status_code == 422
    assert client.get(_b(sid, "screen"), headers=owner).status_code == 409  # окно не открыто
    assert client.post(_b(sid, "click"), headers=owner, json={"x": 5000, "y": 1}).status_code == 409
    assert client.post(_b(sid, "type"), headers=owner, json={"text": ""}).status_code == 422
    assert client.post(_b(sid, "key"), headers=owner, json={"key": "F12"}).status_code == 409
    client.post("/api/users", headers=owner, json={"email": "w@test.local", "password": "worker-pass-1234", "full_name": "Сотрудник"})
    h = login(client, "w@test.local", "worker-pass-1234")
    assert client.post(_b(sid, "open"), headers=h).status_code == 403
    assert client.get(_b(sid, "screen"), headers=h).status_code == 403
    assert client.get(_b(9999, "screen"), headers=owner).status_code == 404


def test_safe_click_follows_same_site_popup_and_keeps_foreign_popup_out():
    import asyncio
    from types import SimpleNamespace
    from app.browser import BrowserError, BrowserManager, _Session

    async def check(popup_url):
        popup = SimpleNamespace(url=popup_url)

        async def loaded(*args, **kwargs):
            return None

        popup.wait_for_load_state = loaded

        class Popup:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            @property
            def value(self):
                async def result():
                    return popup
                return result()

        async def label(*args):
            return "Фактура 9047166247"

        async def click(*args):
            return None

        page = SimpleNamespace(
            url="https://etp.armtek.ru/order_return",
            wait_for_load_state=loaded, evaluate=label,
            mouse=SimpleNamespace(click=click),
            expect_popup=lambda **kwargs: Popup(),
        )
        browser = BrowserManager()
        browser._sessions[1] = _Session(None, page)
        if popup_url.startswith("https://etp.armtek.ru/"):
            await browser._safe_click(1, 10, 10)
            assert browser._sessions[1].page is popup
        else:
            with pytest.raises(BrowserError, match="другой сайт"):
                await browser._safe_click(1, 10, 10)
            assert browser._sessions[1].page is page

    asyncio.run(check("https://etp.armtek.ru/invoice/view/123"))
    asyncio.run(check("https://outside.example/invoice"))


@pytest.mark.parametrize("kind, accepted", [("confirm", True), ("alert", True), ("prompt", False)])
def test_validated_submission_handles_native_dialog_once(kind, accepted):
    import asyncio
    from types import SimpleNamespace
    from app.browser import BrowserManager, _Session

    async def check():
        listeners, actions = {}, []

        async def accept():
            actions.append("accept")

        async def dismiss():
            actions.append("dismiss")

        async def click(*args):
            actions.append("click")
            await listeners["dialog"](SimpleNamespace(type=kind, message="Подтвердить возврат?", accept=accept, dismiss=dismiss))

        async def wait(*args):
            pass

        page = SimpleNamespace(
            mouse=SimpleNamespace(click=click), wait_for_timeout=wait,
            on=lambda name, cb: listeners.update({name: cb}),
            remove_listener=lambda name, cb: listeners.pop(name),
        )
        browser = BrowserManager()
        browser._sessions[1] = _Session(None, page)

        async def preview(*args):
            return {"verified": True}

        browser._submission_preview = preview
        await browser._send_return(1, 10, 10, {"verified": True})
        assert actions == ["click", "accept" if accepted else "dismiss"]
        assert not listeners
        assert browser._sessions[1].submission_messages == [f"{kind}: Подтвердить возврат?"]

    asyncio.run(check())
