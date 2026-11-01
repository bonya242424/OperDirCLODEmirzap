from datetime import timedelta

import httpx

from app.config import settings
from app.db import SessionLocal
from app.integrations import service
from app.models import utcnow

KEYS = {
    "google_sheets_rnp", "google_docs", "google_calendar", "yandex_calendar", "bitrix24",
    "onec_accounting", "onec_trade", "diadoc", "yandex_metrika", "yandex_webmaster", "yandex_disk",
    "telegram", "email_imap", "ftp", "mirzap_mcp", "llm",
}


def _by_key(payload):
    return {item["key"]: item for item in payload["items"]}


def test_list_has_all_integrations_and_interval(client, owner):
    data = client.get("/api/integrations", headers=owner).json()
    assert set(_by_key(data)) == KEYS
    assert data["check_interval_minutes"] == 60


def test_check_all_updates_status_and_time(client, owner):
    data = client.post("/api/integrations/check", headers=owner).json()
    items = _by_key(data)
    for key in KEYS:
        assert items[key]["status"] == "not_configured", key
        assert items[key]["category"], key
        assert items[key]["last_check_at"] is not None
        assert items[key]["last_check_at"].endswith(("Z", "+00:00"))
    assert items["llm"]["has_sync"] is False


def test_history_records_each_check(client, owner):
    client.post("/api/integrations/bitrix24/check", headers=owner)
    client.post("/api/integrations/bitrix24/check", headers=owner)
    hist = client.get("/api/integrations/bitrix24/history", headers=owner).json()
    assert len(hist) == 2


def test_unknown_integration_404(client, owner):
    assert client.post("/api/integrations/nope/check", headers=owner).status_code == 404
    assert client.get("/api/integrations/nope/history", headers=owner).status_code == 404


def test_onec_freshness(client, owner):
    with SessionLocal() as db:
        service.record_sync(db, "onec_accounting")
    item = client.post("/api/integrations/onec_accounting/check", headers=owner).json()
    assert item["status"] == "ok"
    assert item["last_sync_at"] is not None

    with SessionLocal() as db:
        service.record_sync(db, "onec_accounting", utcnow() - timedelta(hours=settings.onec_expected_interval_hours + 5))
    item = client.post("/api/integrations/onec_accounting/check", headers=owner).json()
    assert item["status"] == "warning"
    assert "устарели" in item["message"]


def test_bitrix_ok_and_webhook_secret_never_leaks(client, owner, monkeypatch):
    secret_url = "https://example.bitrix24.ru/rest/1/SECRET_TOKEN_123/"
    monkeypatch.setattr(settings, "bitrix24_webhook_url", secret_url)

    def fake_ok(url, headers=None, timeout=None):
        return httpx.Response(200, json={"result": {"ID": "1"}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_ok)
    item = client.post("/api/integrations/bitrix24/check", headers=owner).json()
    assert item["status"] == "ok"

    def fake_denied(url, headers=None, timeout=None):
        return httpx.Response(401, json={"error": "x"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_denied)
    item = client.post("/api/integrations/bitrix24/check", headers=owner).json()
    assert item["status"] == "error"
    assert "SECRET_TOKEN_123" not in item["message"]

    def fake_down(url, headers=None, timeout=None):
        raise httpx.ConnectError(f"cannot connect to {url}")

    monkeypatch.setattr(httpx, "get", fake_down)
    item = client.post("/api/integrations/bitrix24/check", headers=owner).json()
    assert item["status"] == "error"
    assert "SECRET_TOKEN_123" not in item["message"]


def test_crashing_check_becomes_error_not_500(client, owner, monkeypatch):
    from app.integrations import registry

    def boom(db, row):
        raise RuntimeError("secret detail")

    monkeypatch.setitem(registry.BY_KEY, "bitrix24", registry.IntegrationDef("bitrix24", "x", "y", boom))
    item = client.post("/api/integrations/bitrix24/check", headers=owner).json()
    assert item["status"] == "error"
    assert "secret detail" not in item["message"]


def test_telegram_token_never_leaks(client, owner, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:SECRET_BOT")

    def ok(url, headers=None, timeout=None):
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", ok)
    assert client.post("/api/integrations/telegram/check", headers=owner).json()["status"] == "ok"

    def bad(url, headers=None, timeout=None):
        raise httpx.ConnectError(f"cannot connect to {url}")

    monkeypatch.setattr(httpx, "get", bad)
    item = client.post("/api/integrations/telegram/check", headers=owner).json()
    assert item["status"] == "error"
    assert "SECRET_BOT" not in item["message"]


def test_yandex_disk(client, owner, monkeypatch):
    monkeypatch.setattr(settings, "yandex_oauth_token", "tok")
    monkeypatch.setattr(
        httpx, "get", lambda url, headers=None, timeout=None: httpx.Response(403, request=httpx.Request("GET", url))
    )
    assert client.post("/api/integrations/yandex_disk/check", headers=owner).json()["status"] == "error"
    monkeypatch.setattr(
        httpx, "get", lambda url, headers=None, timeout=None: httpx.Response(200, json={}, request=httpx.Request("GET", url))
    )
    assert client.post("/api/integrations/yandex_disk/check", headers=owner).json()["status"] == "ok"


def test_imap(client, owner, monkeypatch):
    import imaplib

    monkeypatch.setattr(settings, "imap_host", "imap.example.com")
    monkeypatch.setattr(settings, "imap_user", "u")
    monkeypatch.setattr(settings, "imap_password", "p-SECRET")

    class Good:
        def __init__(self, host, port, timeout=None): pass
        def login(self, u, p): return ("OK", [])
        def logout(self): pass

    class BadLogin(Good):
        def login(self, u, p): raise imaplib.IMAP4.error("AUTH p-SECRET failed")

    class Down(Good):
        def __init__(self, host, port, timeout=None): raise OSError("no route")

    for cls, expected in ((Good, "ok"), (BadLogin, "error"), (Down, "error")):
        monkeypatch.setattr(imaplib, "IMAP4_SSL", cls)
        item = client.post("/api/integrations/email_imap/check", headers=owner).json()
        assert item["status"] == expected
        assert "p-SECRET" not in item["message"]


def test_ftp(client, owner, monkeypatch):
    import ftplib

    monkeypatch.setattr(settings, "ftp_host", "ftp.example.com")
    monkeypatch.setattr(settings, "ftp_user", "u")
    monkeypatch.setattr(settings, "ftp_password", "p")

    class Good:
        def __init__(self, timeout=None): pass
        def connect(self, h, p): pass
        def login(self, u, p): pass
        def cwd(self, d): pass
        def quit(self): pass

    class BadLogin(Good):
        def login(self, u, p): raise ftplib.error_perm("530 denied")

    for cls, expected in ((Good, "ok"), (BadLogin, "error")):
        monkeypatch.setattr(ftplib, "FTP", cls)
        assert client.post("/api/integrations/ftp/check", headers=owner).json()["status"] == expected


def test_pending_integration_warns_when_configured(client, owner, monkeypatch):
    monkeypatch.setattr(settings, "mirzap_mcp_url", "https://mcp.example.com")
    item = client.post("/api/integrations/mirzap_mcp/check", headers=owner).json()
    assert item["status"] == "warning"
    assert "mcp.example.com" not in item["message"]
