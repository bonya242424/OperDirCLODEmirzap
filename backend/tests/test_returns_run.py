import http.server
import json
import threading

import pytest

from app import connections as conn_lib
from app.db import SessionLocal
from app.models import ReturnLine, ReturnSupplier, utcnow
from tests.conftest import login
from tests.test_agents import Script, call, say
from tests.test_returns import SAMPLE, _supplier

INV = "ЮП0655627 /3"
PAGE = """<!doctype html><title>t</title><body style="margin:0">
<div style="position:absolute;left:10px;top:10px">Счёт ЮП0655627 найден, ранее не возвращался</div>
<button id=b style="position:absolute;left:100px;top:100px;width:200px;height:50px" onclick="document.title='SENT'">Отправить заявку</button>
<input type=password style="position:absolute;left:100px;top:300px;width:200px;height:40px">
</body>""".encode()


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


def _batch(client, owner, url, session=True):
    sup = _supplier(client, owner, name="Сайт", portal_url=url).json()
    if session:
        with SessionLocal() as db:
            s = db.get(ReturnSupplier, sup["id"])
            s.session_encrypted = conn_lib.encrypt({"state": json.dumps({"cookies": [], "origins": []})})
            s.session_saved_at = utcnow()
            db.commit()
    r = client.post(f"/api/returns/import?supplier_id={sup['id']}", headers=owner, files={"file": ("t.csv", SAMPLE.read_bytes(), "text/csv")})
    return r.json()["id"]


def _last(client, owner, bid):
    return client.get(f"/api/returns/batches/{bid}/runs", headers=owner).json()[0]


def test_check_run_never_sends(client, owner, site, monkeypatch):
    bid = _batch(client, owner, site)
    script = Script(
        call("browser_read"),
        call("browser_click", x=150, y=120),  # кнопка «Отправить заявку»: должна быть заблокирована
        call("browser_view"),
        call("browser_goto", url="https://evil.example/"),
        call("record_result", invoice_no=INV, status="already_returned", note="на сайте уже возвращено"),
        call("finish", summary="Проверил один счёт"),
    )
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    r = client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    assert r.status_code == 202, r.text
    run = _last(client, owner, bid)
    if run["status"] == "failed" and "браузер" in run["summary"].lower():
        pytest.skip("chromium недоступен")
    assert run["status"] == "done", run
    log = "\n".join(run["log"])
    assert "заблокирована" in log and "только сайт поставщика" in log
    assert "Счёт ЮП0655627 /3: уже возвращено" in log
    assert "Не проверено счетов: 9" in run["summary"]
    # скриншот ушёл в модель картинкой
    assert any(isinstance(b.get("content"), list) and b["content"][0].get("type") == "image" for m in script.seen[3:4] for b in m[0][-1]["content"])
    inv = {i["invoice_no"]: i for i in client.get(f"/api/returns/batches?supplier_id=1", headers=owner).json()[0]["invoices"]}[INV]
    assert inv["status"] == "failed" and "уже возвращено" in inv["note"]
    with SessionLocal() as db:
        assert db.query(ReturnLine).filter(ReturnLine.status == "submitted").count() == 0


def test_type_into_password_blocked(client, owner, site, monkeypatch):
    bid = _batch(client, owner, site)
    script = Script(call("browser_click", x=150, y=320), call("browser_type", text="hunter2"), call("finish", summary="стоп"))
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    run = _last(client, owner, bid)
    if run["status"] == "failed" and "браузер" in run["summary"].lower():
        pytest.skip("chromium недоступен")
    assert "Пароли агент не вводит" in "\n".join(run["log"])


def test_preconditions_and_permissions(client, owner, site):
    bid = _batch(client, owner, site, session=False)
    assert client.post(f"/api/returns/batches/{bid}/check-run", headers=owner).status_code == 422  # нет входа
    assert client.post("/api/returns/batches/9999/check-run", headers=owner).status_code == 404
    client.post("/api/users", headers=owner, json={"email": "w@test.local", "password": "worker-pass-1234", "full_name": "С"})
    h = login(client, "w@test.local", "worker-pass-1234")
    assert client.post(f"/api/returns/batches/{bid}/check-run", headers=h).status_code == 403
    assert client.post("/api/returns/runs/1/stop", headers=h).status_code == 403
    assert client.get(f"/api/returns/batches/{bid}/runs", headers=owner).json() == []


def test_no_model_key_fails_clearly(client, owner, site, monkeypatch):
    bid = _batch(client, owner, site)
    monkeypatch.setattr("app.returns_run.get_provider", lambda: None)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    run = _last(client, owner, bid)
    assert run["status"] == "failed" and "ключ" in run["summary"]


def test_second_run_checks_only_unchecked_and_clears_field(client, owner, site, monkeypatch):
    bid = _batch(client, owner, site)
    first = Script(call("record_result", invoice_no=INV, status="ok", note="ок"), call("finish", summary="один"))
    monkeypatch.setattr("app.returns_run.get_provider", lambda: first)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    run = _last(client, owner, bid)
    if run["status"] == "failed" and "браузер" in run["summary"].lower():
        pytest.skip("chromium недоступен")
    second = Script(call("browser_click", x=150, y=320), call("finish", summary="готово"))
    monkeypatch.setattr("app.returns_run.get_provider", lambda: second)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    plan = second.seen[0][0][0]["content"]
    assert INV not in plan and "ЮП0656162" in plan  # проверенный счёт не повторяется


def test_prune_helpers():
    from app.returns_run import _prune_images, _prune_text

    img = {"type": "image", "source": {}}
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": [dict(img)]}]} for i in range(4)]
    _prune_images(msgs)
    assert [m["content"][0]["content"][0]["type"] for m in msgs] == ["text", "text", "image", "image"]
    txt = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": "x" * 2000}]} for i in range(6)]
    _prune_text(txt, keep=4)
    assert [m["content"][0]["content"] == "x" * 2000 for m in txt] == [False, False, True, True, True, True]


def test_supplier_runs_endpoint(client, owner, site, monkeypatch):
    bid = _batch(client, owner, site)
    monkeypatch.setattr("app.returns_run.get_provider", lambda: None)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    sid = client.get("/api/returns/suppliers", headers=owner).json()[0]["id"]
    runs = client.get(f"/api/returns/suppliers/{sid}/runs", headers=owner).json()
    assert len(runs) == 1 and runs[0]["batch_id"] == bid
    assert client.get("/api/returns/suppliers/9999/runs", headers=owner).status_code == 404
