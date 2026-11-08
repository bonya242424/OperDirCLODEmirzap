import json
from types import SimpleNamespace

import pytest

from app import connections
from app.browser import BrowserError
from app.db import SessionLocal
from app.models import ReturnSupplier, ReturnLine, ReturnBatch, AppSetting
from app.supplier_credentials import (
    decode_sheet, canonical_login_url, CredentialsError, import_rows,
    ensure_session, capture_owner_approval, launch_approved, APPROVAL_KEY,
)


def sheet(rows, labels=None):
    labels = labels or ["Поставщик", "Адрес", "Логин", "Пароль"]
    return "google.visualization.Query.setResponse(" + json.dumps({
        "status": "ok", "table": {"cols": [{"label": x} for x in labels],
        "rows": [{"c": [{"v": x} for x in row]} for row in rows]}}) + ");"


def test_only_exact_four_column_sheet_and_secret_values_preserved():
    rows = decode_sheet(sheet([["АРМТЕК", "http://etp.armtek.ru/", "user", " secret "]]))
    assert rows[0][3] == " secret "
    with pytest.raises(CredentialsError):
        decode_sheet(sheet([], ["Поставщик", "Возврат Да/Нет", "Логин", "Пароль"]))
    assert canonical_login_url("b2b.part-kom.ru") == "https://b2b.part-kom.ru/"
    assert canonical_login_url("http://etp.armtek.ru/") == "https://etp.armtek.ru/"


@pytest.mark.parametrize("url", ["https://armtek.ru.evil.test", "https://127.0.0.1", "https://user:secret@etp.armtek.ru", "file:///etc/passwd"])
def test_unrelated_or_embedded_credential_address_rejected(url):
    with pytest.raises(CredentialsError):
        canonical_login_url(url)


def test_import_matches_existing_supplier_encrypts_and_does_not_expose_secrets(client, owner):
    with SessionLocal() as db:
        supplier = ReturnSupplier(name="Армтек", portal_url="https://etp.armtek.ru/order_return")
        db.add(supplier)
        db.commit()
        sid = supplier.id
        status = import_rows(db, [("АРМТЕК", "http://etp.armtek.ru/", "account", "unique-password-test")])
        db.commit()
        assert status["supplier_ids"] == [sid]
        assert "unique-password-test" not in json.dumps(status)
        assert "account" not in json.dumps(status)
        assert "unique-password-test" not in supplier.credentials_encrypted
        assert connections.decrypt(supplier.credentials_encrypted)["password"] == "unique-password-test"
        assert db.query(ReturnSupplier).count() == 1
    data = client.get("/api/returns/suppliers", headers=owner).json()
    assert data[0]["has_credentials"] is True
    assert "password" not in json.dumps(data)


def test_auto_login_secret_goes_only_to_driver_and_session_saved(client, owner):
    calls = []
    with SessionLocal() as db:
        supplier = ReturnSupplier(name="Армтек", portal_url="https://etp.armtek.ru/order_return",
            login_url="https://etp.armtek.ru/", credentials_encrypted=connections.encrypt({"username": "account", "password": "test-private"}))
        db.add(supplier)
        db.commit()
        driver = SimpleNamespace(
            auth_required=lambda key: True,
            login=lambda key, url, username, password, target: calls.append((username, password)) or {"cookies": [], "origins": []})
        assert ensure_session(db, supplier, driver, 1) is True
        assert calls == [("account", "test-private")]
        assert supplier.login_status == "ok"
        assert "test-private" not in supplier.session_encrypted
        assert connections.decrypt(supplier.session_encrypted).get("state")
        assert not supplier.login_error


def test_failed_login_is_generic_and_does_not_retry_password(client, owner):
    with SessionLocal() as db:
        supplier = ReturnSupplier(name="Армтек", portal_url="https://etp.armtek.ru/order_return",
            login_url="https://etp.armtek.ru/", credentials_encrypted=connections.encrypt({"username": "account", "password": "test-private"}))
        db.add(supplier)
        db.commit()
        calls = []
        def login(*args):
            calls.append(1)
            raise BrowserError("test-private should never be returned")
        driver = SimpleNamespace(auth_required=lambda key: True, login=login)
        with pytest.raises(BrowserError) as error:
            ensure_session(db, supplier, driver, 1)
        assert "test-private" not in str(error.value)
        assert "test-private" not in supplier.login_error
        with pytest.raises(BrowserError):
            ensure_session(db, supplier, driver, 1)
        assert calls == [1]


def test_approved_start_does_not_expand_to_new_rows_or_repeat_submitted(client, owner):
    from tests.test_returns_run import _batch
    from app.models import ReturnJob
    bid = _batch(client, owner, "https://itrade.forum-auto.ru")
    with SessionLocal() as db:
        batch = db.get(ReturnBatch, bid)
        supplier = db.get(ReturnSupplier, batch.supplier_id)
        supplier.login_status = "ok"
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id))
        rows[0].status, rows[0].request_no = "submitted", "123456"
        db.commit()
        capture_owner_approval(db, [supplier.id])
        db.add(ReturnLine(batch_id=bid, article="NEW-NOT-APPROVED", brand="NEW", qty=1))
        rows[1].qty += 1
        db.commit()
        launch_approved(db)
        launch_approved(db)
        assert db.query(ReturnJob).count() == 1
        payload = json.loads(db.query(ReturnJob).first().payload)
        assert "NEW-NOT-APPROVED" not in json.dumps(payload)
        assert str(rows[0].id) not in {str(x["id"]) for x in json.loads(json.loads(payload["approvals"][str(bid)])[0].split(": ",1)[1])["lines"]}
        assert rows[0].request_no == "123456"


def test_fetch_is_pinned_to_sheet12_and_does_not_guess_headers(monkeypatch):
    from app import supplier_credentials
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        return SimpleNamespace(status_code=200, content=b"ok", text=sheet([]))
    monkeypatch.setattr(supplier_credentials.httpx, "get", get)
    assert supplier_credentials.fetch_sheet() == []
    from urllib.parse import urlsplit, parse_qs
    params = parse_qs(urlsplit(seen[0]).query)
    assert params["sheet"] == ["Лист12"]
    assert params["headers"] == ["1"]
    assert params["range"] == ["A1:D500"]
