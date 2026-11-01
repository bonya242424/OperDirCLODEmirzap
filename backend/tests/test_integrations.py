from datetime import timedelta

import httpx

from app.config import settings
from app.db import SessionLocal
from app.integrations import service
from app.models import utcnow

KEYS = {"google_sheets_rnp", "bitrix24", "onec_files", "yandex_metrika", "yandex_webmaster", "llm"}


def _by_key(payload):
    return {item["key"]: item for item in payload["items"]}


def test_list_has_all_integrations_and_interval(client, owner):
    data = client.get("/api/integrations", headers=owner).json()
    assert set(_by_key(data)) == KEYS
    assert data["check_interval_minutes"] == 60


def test_check_all_updates_status_and_time(client, owner):
    data = client.post("/api/integrations/check", headers=owner).json()
    items = _by_key(data)
    for key in ("bitrix24", "google_sheets_rnp", "yandex_metrika", "yandex_webmaster", "llm", "onec_files"):
        assert items[key]["status"] == "not_configured", key
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
        service.record_sync(db, "onec_files")
    item = client.post("/api/integrations/onec_files/check", headers=owner).json()
    assert item["status"] == "ok"
    assert item["last_sync_at"] is not None

    with SessionLocal() as db:
        service.record_sync(db, "onec_files", utcnow() - timedelta(hours=settings.onec_expected_interval_hours + 5))
    item = client.post("/api/integrations/onec_files/check", headers=owner).json()
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
