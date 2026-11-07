import os
import tempfile

# Настройки должны быть заданы до импорта приложения.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["SEED_PROJECT_CONTEXT"] = "false"
os.environ["SEED_RETURN_SUPPLIERS"] = "false"
os.environ["FILES_DIR"] = f"{_tmp}/files"
os.environ["ADMIN_EMAIL"] = "owner@test.local"
os.environ["ADMIN_PASSWORD"] = "owner-password-123"
os.environ["SECRET_KEY"] = "test-secret-key-for-tests-only-0123456789"
for _name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "BITRIX24_WEBHOOK_URL", "YANDEX_OAUTH_TOKEN", "RNP_SPREADSHEET_IDS"):
    os.environ.pop(_name, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

OWNER = {"email": "owner@test.local", "password": "owner-password-123"}


@pytest.fixture()
def client():
    from app.return_submission import ACTIVE_QUEUES
    from app.return_jobs import ACTIVE_JOBS
    ACTIVE_QUEUES.clear()
    ACTIVE_JOBS.clear()
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


class CompleteOnly:
    """База для подставных моделей, у которых есть только complete(): step() отвечает её текстом."""

    def step(self, system, messages, tools):
        from app.llm import Step

        text = self.complete(system, messages)
        return Step(text=text, assistant_content=[{"type": "text", "text": text}])


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Тесты используют подставные ответы; реальные ключи/данные не уходят наружу."""
    import socket
    original_connect = socket.socket.connect
    original_dns = socket.getaddrinfo

    def local(host):
        return host in ("localhost", "127.0.0.1", "::1", b"localhost", b"127.0.0.1", b"::1")

    def connect(sock, address):
        if isinstance(address, tuple) and not local(address[0]):
            raise OSError("External network disabled in tests")
        return original_connect(sock, address)

    def dns(host, *args, **kwargs):
        if not local(host):
            raise OSError("External network disabled in tests")
        return original_dns(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
