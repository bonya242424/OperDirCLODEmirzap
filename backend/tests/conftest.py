import os
import tempfile

# Настройки должны быть заданы до импорта приложения.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["FILES_DIR"] = f"{_tmp}/files"
os.environ["ADMIN_EMAIL"] = "owner@test.local"
os.environ["ADMIN_PASSWORD"] = "owner-password-123"
os.environ["SECRET_KEY"] = "test-secret-key-for-tests-only-0123456789"
for _name in ("ANTHROPIC_API_KEY", "BITRIX24_WEBHOOK_URL", "YANDEX_OAUTH_TOKEN", "RNP_SPREADSHEET_IDS"):
    os.environ.pop(_name, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

OWNER = {"email": "owner@test.local", "password": "owner-password-123"}


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as c:  # lifespan создаёт таблицы и владельца
        yield c


def login(client: TestClient, email: str, password: str) -> dict[str, str]:
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return login(client, **OWNER)
