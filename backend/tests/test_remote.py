import http.server
import threading

import pytest

from app.browser import manager, valid_key
from app.rdp import rdp_manager, to_xdotool
from tests.conftest import login

PAGE = "<!doctype html><title>free</title><input id=i style='position:absolute;left:50px;top:50px;width:300px;height:40px'>".encode()


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


def test_key_validation_and_xdotool_mapping():
    assert all(valid_key(k) for k in ("Enter", "Control+a", "Shift+Tab", "F5", "a", "Control+Shift+t", "PageDown"))
    assert not any(valid_key(k) for k in ("", "Control+", "Ctrl+a", "Control+Control+a", "F13", "Foo", "ab", "Win+r"))
    assert to_xdotool("Control+a") == "ctrl+a" and to_xdotool("Enter") == "Return" and to_xdotool("Shift+Tab") == "shift+Tab"


def test_free_browser_full_control(client, owner, site):
    r = client.post("/api/remote/browser/open", headers=owner, json={"url": site})
    if r.status_code == 409 and "браузер" in r.json()["detail"].lower():
        pytest.skip("chromium недоступен")
    assert r.status_code == 200 and r.json()["address"] is True
    assert client.get("/api/remote/targets", headers=owner).json()["browser"]["open"] is True
    assert client.get("/api/remote/browser/screen", headers=owner).headers["content-type"] == "image/jpeg"
    assert client.post("/api/remote/browser/click", headers=owner, json={"x": 100, "y": 70, "double": True}).status_code == 204
    assert client.post("/api/remote/browser/type", headers=owner, json={"text": "привет"}).status_code == 204
    assert client.post("/api/remote/browser/key", headers=owner, json={"key": "Control+a"}).status_code == 204
    assert client.post("/api/remote/browser/navigate", headers=owner, json={"url": site + "?x=1"}).status_code == 204
    assert client.post("/api/remote/browser/history", headers=owner, json={"action": "back"}).status_code == 204
    assert client.get("/api/remote/browser/info", headers=owner).json()["title"] == "free"
    assert client.post("/api/remote/browser/click", headers=owner, json={"x": 5, "y": 5, "button": "foo"}).status_code == 409
    assert client.post("/api/remote/browser/key", headers=owner, json={"key": "Win+r"}).status_code == 409
    assert client.post("/api/remote/browser/open", headers=owner, json={"url": "javascript:alert(1)"}).status_code == 422
    assert client.post("/api/remote/browser/close", headers=owner).status_code == 204
    assert client.get("/api/remote/browser/screen", headers=owner).status_code == 409


class FakeProc:
    def __init__(self, alive=True):
        self.alive, self.terminated = alive, False

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated = True


@pytest.fixture()
def fake_rdp(monkeypatch):
    calls = {"spawn": [], "run": []}
    procs = []

    def spawn(args, env=None):
        calls["spawn"].append(args)
        p = FakeProc(alive=calls.get("rdp_alive", True))
        procs.append(p)
        return p

    def run(args, env=None, timeout=10):
        calls["run"].append(args)
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(rdp_manager, "spawn", spawn)
    monkeypatch.setattr(rdp_manager, "spawn_logged", lambda args, env, logpath: spawn(args, env))
    monkeypatch.setattr(rdp_manager, "precheck", lambda host, port: None)
    monkeypatch.setattr(rdp_manager, "egress_ip", lambda: "203.0.113.7")
    monkeypatch.setattr(rdp_manager, "run", run)
    monkeypatch.setattr(rdp_manager, "wait_display", lambda d: True)
    monkeypatch.setattr("app.rdp.time.sleep", lambda s: None)
    yield calls
    for k in list(rdp_manager._sessions):
        rdp_manager.close(k)


def _conn(client, owner, **cfg):
    config = {"host": "10.0.0.5", "port": "34821", "user": "ivan", "password": "pw-secret-1"}
    config.update(cfg)
    r = client.post("/api/connections", headers=owner, json={"kind": "rdp", "title": "Терминал 1С", "config": config})
    assert r.status_code == 201, r.text
    return r.json()


def test_rdp_connection_secret_never_returned(client, owner):
    c = _conn(client, owner)
    assert c["scope"] == "company" and c["secrets_set"] == ["password"] and "pw-secret-1" not in str(c)
    assert "pw-secret-1" not in client.get("/api/connections", headers=owner).text
    assert "pw-secret-1" not in client.get("/api/remote/targets", headers=owner).text
    t = client.get("/api/remote/targets", headers=owner).json()["rdp"][0]
    assert t["title"] == "Терминал 1С" and t["host"] == "10.0.0.5"


def test_rdp_session_flow(client, owner, fake_rdp):
    cid = _conn(client, owner, port="34821")["id"]
    r = client.post(f"/api/remote/rdp/{cid}/open", headers=owner)
    assert r.status_code == 200 and r.json() == {"width": 1280, "height": 800, "address": False}
    xfreerdp = next(a for a in fake_rdp["spawn"] if a[0] == "xfreerdp")
    assert "/v:10.0.0.5:34821" in xfreerdp and "/u:ivan" in xfreerdp and "/p:pw-secret-1" in xfreerdp
    assert "pw-secret-1" not in r.text
    assert client.get(f"/api/remote/rdp/{cid}/screen", headers=owner).content.startswith(b"\xff\xd8")
    assert client.post(f"/api/remote/rdp/{cid}/click", headers=owner, json={"x": 100, "y": 200, "double": True}).status_code == 204
    assert client.post(f"/api/remote/rdp/{cid}/type", headers=owner, json={"text": "Отчёт"}).status_code == 204
    assert client.post(f"/api/remote/rdp/{cid}/key", headers=owner, json={"key": "Control+a"}).status_code == 204
    cmds = [c for c in fake_rdp["run"] if c[0] == "xdotool"]
    assert ["xdotool", "mousemove", "100", "200"] in cmds and ["xdotool", "key", "ctrl+a"] in cmds
    assert any(c[:4] == ["xdotool", "type", "--delay", "20"] and c[-1] == "Отчёт" for c in cmds)
    assert client.post(f"/api/remote/rdp/{cid}/click", headers=owner, json={"x": 5000, "y": 1}).status_code == 409
    assert client.post(f"/api/remote/rdp/{cid}/close", headers=owner).status_code == 204
    assert client.get(f"/api/remote/rdp/{cid}/screen", headers=owner).status_code == 409


def test_rdp_connect_failure_is_clear(client, owner, fake_rdp):
    cid = _conn(client, owner)["id"]
    fake_rdp["rdp_alive"] = False
    r = client.post(f"/api/remote/rdp/{cid}/open", headers=owner)
    assert r.status_code == 409 and "подключиться" in r.json()["detail"] and "pw-secret-1" not in r.text


def test_remote_owner_only_and_missing(client, owner):
    cid = _conn(client, owner)["id"]
    client.post("/api/users", headers=owner, json={"email": "w@test.local", "password": "worker-pass-1234", "full_name": "С"})
    h = login(client, "w@test.local", "worker-pass-1234")
    for method, path in (("get", "/api/remote/targets"), ("post", f"/api/remote/rdp/{cid}/open"), ("post", "/api/remote/browser/open"), ("get", "/api/remote/browser/screen")):
        assert getattr(client, method)(path, headers=h, **({"json": {}} if method == "post" else {})).status_code == 403
    assert client.post("/api/connections", headers=h, json={"kind": "rdp", "config": {"host": "h", "user": "u", "password": "p"}}).status_code == 403
    assert client.post("/api/remote/rdp/9999/open", headers=owner).status_code == 404


def test_explain_failure_reasons():
    e = rdp_manager.explain
    assert "логин или пароль" in e("freerdp_connect failed ERRCONNECT_LOGON_FAILURE", "x")
    assert "не отвечает" in e("transport connect failed: timed out", "x")
    assert "NLA" in e("CredSSP: failed", "x")
    out = e("some strange message with pw-secret-1 inside", "pw-secret-1")
    assert "pw-secret-1" not in out and "***" in out


def test_precheck_unreachable_names_our_ip(monkeypatch):
    from app.browser import BrowserError

    monkeypatch.setattr(rdp_manager, "egress_ip", lambda: "203.0.113.7")
    with pytest.raises(BrowserError) as e:
        rdp_manager._precheck("127.0.0.1", 1)
    assert "203.0.113.7" in str(e.value) and "не отвечает" in str(e.value)


def test_tail_hides_password():
    out = rdp_manager.tail("a\nERRCONNECT_X pw-secret-1\nlast line", "pw-secret-1")
    assert "pw-secret-1" not in out and "last line" in out


def test_connect_in_background_shows_steps(client, owner, fake_rdp):
    import time

    r = client.post("/api/connections", headers=owner, json={"kind": "rdp", "scope": "company", "config": {"host": "10.0.0.5", "port": "34821", "user": "ivan", "password": "pw-secret-1"}})
    cid = r.json()["id"]
    assert client.post(f"/api/remote/rdp/{cid}/connect", headers=owner).status_code == 202
    for _ in range(50):
        st = client.get(f"/api/remote/rdp/{cid}/status", headers=owner).json()
        if st["state"] != "connecting":
            break
        time.sleep(0.1)
    assert st["state"] == "ready" and any("Виртуальный экран" in x["text"] for x in st["steps"])
    assert "pw-secret-1" not in str(st)


def test_explain_activation_failure():
    msg = rdp_manager.explain("[rdp_client_wait_for_activation]: ERRCONNECT_CONNECT_TRANSPORT_FAILED | freerdp_post_connect failed", "x")
    assert "подготовке сеанса" in msg


def test_code_extracts_errconnect():
    assert rdp_manager.code("x ERRCONNECT_SECURITY_NEGO_CONNECT_FAILED [0x2000C]") == "SECURITY_NEGO_CONNECT_FAILED"
    assert rdp_manager.code("") == "без сообщения"
