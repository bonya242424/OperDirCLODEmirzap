from datetime import date, timedelta

import pytest

from app.db import SessionLocal
from app.models import Metric, MetricValue
from app.routers.metrics import evaluate


def _folder(client, h, name="Выгрузки 1С"):
    r = client.post("/api/folders", headers=h, json={"name": name})
    assert r.status_code in (201, 409), r.text
    return next(f["id"] for f in client.get("/api/folders", headers=h).json() if f["name"] == name)


def _create(client, h, **kw):
    body = {
        "direction": "ops", "name": "Конверсия в заказ", "unit": "%", "better": "up", "warn_threshold": 30, "bad_threshold": 20,
        "source_folder_id": _folder(client, h), "source_prompt": "Конверсия из колонки C",
    }
    body.update(kw)
    return client.post("/api/metrics", headers=h, json=body)


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _val(client, h, mid, days_ago, value):
    """Значения теперь пишет только расчёт по файлу; в тесте подставляем результат расчёта напрямую."""
    d = date.today() - timedelta(days=days_ago)
    with SessionLocal() as db:
        row = db.query(MetricValue).filter_by(metric_id=mid, period=d).first()
        if row:
            row.value = value
        else:
            db.add(MetricValue(metric_id=mid, period=d, value=value, created_by=1))
        db.commit()
    return _Resp(next(m for m in client.get("/api/metrics", headers=h).json() if m["id"] == mid))


def test_status_by_thresholds_up_and_down(client, owner):
    up = _create(client, owner).json()
    assert up["status"] == "no_data"
    assert _val(client, owner, up["id"], 0, 45).json()["status"] == "ok"
    assert _val(client, owner, up["id"], 0, 25).json()["status"] == "warning"
    assert _val(client, owner, up["id"], 0, 12).json()["status"] == "bad"

    down = _create(client, owner, name="Возвраты", unit="%", better="down", warn_threshold=5, bad_threshold=8).json()
    assert _val(client, owner, down["id"], 0, 3).json()["status"] == "ok"
    assert _val(client, owner, down["id"], 0, 6).json()["status"] == "warning"
    assert _val(client, owner, down["id"], 0, 9).json()["status"] == "bad"


def test_trend_history_and_overwrite_same_day(client, owner):
    m = _create(client, owner).json()
    _val(client, owner, m["id"], 14, 40)
    _val(client, owner, m["id"], 7, 50)
    out = _val(client, owner, m["id"], 0, 55).json()
    assert out["history"] == [40, 50, 55] and out["previous"] == 50 and out["change"] == 5
    out = _val(client, owner, m["id"], 0, 60).json()
    assert out["history"] == [40, 50, 60]
    assert len(client.get(f"/api/metrics/{m['id']}/values", headers=owner).json()) == 3


def test_stale_when_not_updated(client, owner):
    m = _create(client, owner, expected_days=7).json()
    assert _val(client, owner, m["id"], 3, 40).json()["stale"] is False
    m2 = _create(client, owner, name="Старый", expected_days=7).json()
    assert _val(client, owner, m2["id"], 10, 40).json()["stale"] is True


def test_validation(client, owner):
    assert _create(client, owner, direction="xxx").status_code == 422
    assert _create(client, owner, name="  ").status_code == 422
    assert _create(client, owner, warn_threshold=10, bad_threshold=20).status_code == 422
    assert _create(client, owner, better="down", warn_threshold=10, bad_threshold=5).status_code == 422
    m = _create(client, owner).json()
    assert client.post(f"/api/metrics/{m['id']}/values", headers=owner, json={"period": "2026-01-01", "value": 1}).status_code in (404, 405)  # руками значения не вносятся
    assert client.patch(f"/api/metrics/{m['id']}", headers=owner, json={"warn_threshold": 35}).status_code == 200
    assert client.patch(f"/api/metrics/{m['id']}", headers=owner, json={"bad_threshold": 50}).status_code == 422
    assert client.get("/api/metrics").status_code == 401


def test_filter_archive_and_clear_thresholds(client, owner):
    a = _create(client, owner, direction="fin", name="Выручка").json()
    _create(client, owner, direction="ops")
    assert len(client.get("/api/metrics?direction=fin", headers=owner).json()) == 1
    r = client.patch(f"/api/metrics/{a['id']}", headers=owner, json={"clear_thresholds": True})
    assert r.json()["warn_threshold"] is None and r.json()["bad_threshold"] is None
    assert client.delete(f"/api/metrics/{a['id']}", headers=owner).status_code == 204
    assert client.get("/api/metrics?direction=fin", headers=owner).json() == []
    assert client.post(f"/api/metrics/{a['id']}/recalculate", headers=owner).status_code == 404


@pytest.mark.parametrize("value,expected", [(10, "bad"), (20, "bad"), (21, "warning"), (30, "warning"), (31, "ok")])
def test_threshold_edges(value, expected):
    m = Metric(better="up", warn_threshold=30, bad_threshold=20)
    assert evaluate(m, value) == expected
