import shutil
import subprocess

import pytest

from tests.conftest import login
from tests.test_guides import Seer


@pytest.fixture()
def video(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("нет ffmpeg")
    p = tmp_path / "d.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=5", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p.read_bytes()


def _section(client, h, **kw):
    body = {"title": "Сборка возвратов", "notes": "Открываю журнал документов, ставлю период"}
    body.update(kw)
    return client.post("/api/instructions/sections", headers=h, json=body)


def test_section_crud_and_permissions(client, owner):
    s = _section(client, owner).json()
    assert s["title"] == "Сборка возвратов" and s["videos"] == 0
    assert _section(client, owner, title=" ").status_code == 422
    r = client.patch(f"/api/instructions/sections/{s['id']}", headers=owner, json={"notes": "Новое описание"}).json()
    assert r["notes"] == "Новое описание" and r["title"] == "Сборка возвратов"
    assert client.get("/api/instructions/sections", headers=owner).json()[0]["id"] == s["id"]
    client.post("/api/users", headers=owner, json={"email": "w@test.local", "password": "worker-pass-1234", "full_name": "С"})
    h = login(client, "w@test.local", "worker-pass-1234")
    assert client.get("/api/instructions/sections", headers=h).status_code == 200  # читать могут все
    assert _section(client, h).status_code == 403
    assert client.delete(f"/api/instructions/sections/{s['id']}", headers=h).status_code == 403
    assert client.delete(f"/api/instructions/sections/{s['id']}", headers=owner).status_code == 204
    assert client.get("/api/instructions/sections", headers=owner).json() == []
    assert client.get("/api/instructions/sections/9999/videos", headers=owner).status_code == 404


def test_video_becomes_1c_instruction_with_owner_notes(client, owner, video, monkeypatch):
    seer = Seer("1. Открыть «Журнал документов»")
    monkeypatch.setattr("app.guide_calc.get_provider", lambda: seer)
    s = _section(client, owner).json()
    r = client.post(f"/api/instructions/sections/{s['id']}/videos", headers=owner, files={"file": ("a.mp4", video, "video/mp4")})
    assert r.status_code == 201, r.text
    v = client.get(f"/api/instructions/sections/{s['id']}/videos", headers=owner).json()[0]
    assert v["status"] == "ok" and v["text"].startswith("1. Открыть")
    system, messages = seer.seen[0]
    assert "1С" in system and "данные для инструкции" in system
    first = messages[0]["content"][0]["text"]
    assert "Открываю журнал документов" in first  # описание владельца попало в запрос
    assert client.patch(f"/api/instructions/videos/{v['id']}", headers=owner, json={"text": "Правка"}).json()["text"] == "Правка"
    assert client.post(f"/api/instructions/videos/{v['id']}/reanalyze", headers=owner).status_code == 202
    assert client.delete(f"/api/instructions/videos/{v['id']}", headers=owner).status_code == 204
    assert client.post(f"/api/instructions/sections/{s['id']}/videos", headers=owner, files={"file": ("a.pdf", b"x", "application/pdf")}).status_code == 422
