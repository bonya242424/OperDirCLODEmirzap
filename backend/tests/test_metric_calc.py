from datetime import date

from app.db import SessionLocal
from app.metric_calc import reset_stale
from app.models import Metric, MetricValue
from tests.test_agents import Script, call, say
from tests.test_metrics import _create, _folder


def _upload(client, h, folder_id, text="период,значение\n2026-11-01,42\n"):
    r = client.post(f"/api/folders/{folder_id}/files", headers=h, files={"file": ("vygruzka.csv", text.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()


def _use(monkeypatch, script):
    monkeypatch.setattr("app.metric_calc.get_provider", lambda: script)
    return script


def _metric(client, h, mid):
    return next(m for m in client.get("/api/metrics", headers=h).json() if m["id"] == mid)


def test_create_requires_folder_and_prompt(client, owner):
    fid = _folder(client, owner)
    assert _create(client, owner, source_prompt="  ").status_code == 422
    assert _create(client, owner, source_folder_id=9999).status_code == 422
    body = {"direction": "ops", "name": "X", "source_folder_id": fid}
    assert client.post("/api/metrics", headers=owner, json=body).status_code == 422


def test_no_file_yet_stays_idle(client, owner):
    m = _create(client, owner).json()
    assert m["calc_status"] == "none" and m["source_folder"] == "Выгрузки 1С" and m["latest"] is None


def test_upload_recomputes_metric_from_file(client, owner, monkeypatch):
    m = _create(client, owner).json()
    f = _upload(client, owner, m["source_folder_id"])
    mid = m["id"]
    # TestClient выполняет фоновые задачи сразу, но модели нет → ошибка с понятным текстом
    assert _metric(client, owner, mid)["calc_status"] == "error"
    assert "ключ" in _metric(client, owner, mid)["calc_message"]

    script = _use(monkeypatch, Script(
        call("read_file", file_id=f["id"]),
        call("set_metric_value", metric_id=mid, period="2026-11-01", value=42),
        say("Файл vygruzka.csv, колонка «значение»"),
    ))
    _upload(client, owner, m["source_folder_id"])
    got = _metric(client, owner, mid)
    assert got["calc_status"] == "ok" and got["latest"] == 42 and got["calc_file"] == "vygruzka.csv"
    assert "колонка" in got["calc_message"]


def test_model_without_write_is_error(client, owner, monkeypatch):
    m = _create(client, owner).json()
    _use(monkeypatch, Script(say("Нет нужной колонки")))
    _upload(client, owner, m["source_folder_id"])
    got = _metric(client, owner, m["id"])
    assert got["calc_status"] == "error" and "Нет нужной колонки" in got["calc_message"] and got["latest"] is None


def test_cannot_write_other_metric(client, owner, monkeypatch):
    a = _create(client, owner).json()
    b = _create(client, owner, name="Другой").json()
    _use(monkeypatch, Script(call("set_metric_value", metric_id=b["id"], period="2026-11-01", value=1), say("записал")))
    _upload(client, owner, a["source_folder_id"])
    assert _metric(client, owner, b["id"])["latest"] is None
    assert _metric(client, owner, a["id"])["calc_status"] == "error"


def test_recalculate_endpoint(client, owner, monkeypatch):
    m = _create(client, owner).json()
    _upload(client, owner, m["source_folder_id"])
    _use(monkeypatch, Script(call("set_metric_value", metric_id=m["id"], period=date.today().isoformat(), value=7), say("ок")))
    r = client.post(f"/api/metrics/{m['id']}/recalculate", headers=owner)
    assert r.status_code == 202
    assert _metric(client, owner, m["id"])["latest"] == 7
    assert client.post("/api/metrics/9999/recalculate", headers=owner).status_code == 404


def test_recalculate_conflict_and_reset_stale(client, owner):
    m = _create(client, owner).json()
    with SessionLocal() as db:
        db.get(Metric, m["id"]).calc_status = "running"
        db.commit()
    assert client.post(f"/api/metrics/{m['id']}/recalculate", headers=owner).status_code == 409
    reset_stale()
    got = _metric(client, owner, m["id"])
    assert got["calc_status"] == "error" and "Пересчитать" in got["calc_message"]


def test_patch_prompt_triggers_recompute(client, owner, monkeypatch):
    m = _create(client, owner).json()
    _upload(client, owner, m["source_folder_id"])
    _use(monkeypatch, Script(call("set_metric_value", metric_id=m["id"], period="2026-11-01", value=5), say("ок")))
    r = client.patch(f"/api/metrics/{m['id']}", headers=owner, json={"source_prompt": "Другое поручение"})
    assert r.status_code == 200
    assert _metric(client, owner, m["id"])["latest"] == 5
    assert client.patch(f"/api/metrics/{m['id']}", headers=owner, json={"source_prompt": " "}).status_code == 422
    with SessionLocal() as db:
        assert db.query(MetricValue).filter_by(metric_id=m["id"]).count() == 1
