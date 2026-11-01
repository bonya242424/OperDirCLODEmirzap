import io

from app.db import SessionLocal
from app.config import settings
from tests.conftest import login


def _make_user(client, owner, n):
    client.post("/api/users", headers=owner, json={"email": f"f{n}@test.local", "password": "employee-pass-1", "full_name": f"Ф{n}"})
    return login(client, f"f{n}@test.local", "employee-pass-1")


def _upload(client, headers, folder_id, name="a.csv", data=b"x,y\n1,2\n"):
    return client.post(f"/api/folders/{folder_id}/files", headers=headers, files={"file": (name, io.BytesIO(data), "text/csv")})


def test_folder_upload_download_delete(client, owner):
    f = client.post("/api/folders", headers=owner, json={"name": "Заказы 1С", "description": "Выгрузка заказов"}).json()
    assert client.post("/api/folders", headers=owner, json={"name": "Заказы 1С"}).status_code == 409
    up = _upload(client, owner, f["id"])
    assert up.status_code == 201 and up.json()["size"] == 8
    folders = client.get("/api/folders", headers=owner).json()
    assert folders[0]["files_count"] == 1 and folders[0]["last_upload_at"].endswith(("Z", "+00:00"))
    dl = client.get(f"/api/files/{up.json()['id']}/download", headers=owner)
    assert dl.content == b"x,y\n1,2\n" and "attachment" in dl.headers["content-disposition"]
    assert client.delete(f"/api/folders/{f['id']}", headers=owner).status_code == 409
    assert client.delete(f"/api/files/{up.json()['id']}", headers=owner).status_code == 204
    assert client.delete(f"/api/folders/{f['id']}", headers=owner).status_code == 204


def test_upload_marks_source_fresh(client, owner):
    f = client.post("/api/folders", headers=owner, json={"name": "УТ", "source_key": "onec_trade"}).json()
    assert _upload(client, owner, f["id"]).status_code == 201
    item = client.post("/api/integrations/onec_trade/check", headers=owner).json()
    assert item["status"] == "ok" and item["last_sync_at"] is not None
    assert client.post("/api/folders", headers=owner, json={"name": "Z", "source_key": "nope"}).status_code == 422


def test_path_traversal_name_is_sanitized(client, owner):
    f = client.post("/api/folders", headers=owner, json={"name": "Р"}).json()
    up = _upload(client, owner, f["id"], name="../../etc/passwd")
    assert up.json()["name"] == "passwd"


def test_limits_and_empty(client, owner, monkeypatch):
    f = client.post("/api/folders", headers=owner, json={"name": "Л"}).json()
    assert _upload(client, owner, f["id"], data=b"").status_code == 422
    import os

    before = set(os.listdir(settings.files_dir))
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    assert _upload(client, owner, f["id"], data=b"abc").status_code == 413
    assert set(os.listdir(settings.files_dir)) == before


def test_permissions(client, owner):
    a, b = _make_user(client, owner, 1), _make_user(client, owner, 2)
    f = client.post("/api/folders", headers=a, json={"name": "Общая"}).json()
    up = _upload(client, a, f["id"]).json()
    assert client.get(f"/api/folders/{f['id']}/files", headers=b).json()[0]["can_delete"] is False
    assert client.delete(f"/api/files/{up['id']}", headers=b).status_code == 403
    assert client.patch(f"/api/folders/{f['id']}", headers=b, json={"name": "X"}).status_code == 403
    assert client.patch(f"/api/folders/{f['id']}", headers=owner, json={"name": "Новая"}).json()["name"] == "Новая"
    assert client.get("/api/folders").status_code == 401
    assert client.get("/api/files/999/download", headers=b).status_code == 404
