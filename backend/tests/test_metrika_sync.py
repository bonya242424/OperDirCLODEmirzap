from datetime import date

import pytest
from sqlalchemy import select

import app.metrika_sync as ms
from app.db import SessionLocal
from app.models import Metric, MetricValue


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body or {}

    def json(self):
        return self._b


class FakeClient:
    calls: list = []
    reply = Resp(200, {"totals": [10, 7, 30]})

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        FakeClient.calls.append((url, params, headers))
        return FakeClient.reply


@pytest.fixture()
def metrika(monkeypatch, owner):
    FakeClient.calls = []
    FakeClient.reply = Resp(200, {"totals": [10, 7, 30]})
    monkeypatch.setattr(ms.httpx, "Client", FakeClient)
    monkeypatch.setattr(ms, "_yandex_token", lambda kind: "tok")
    monkeypatch.setattr(ms, "company_cfg", lambda kind: {"counter_id": "555"})


def test_week_starts():
    w = ms.week_starts(date(2026, 11, 3), 2)  # вторник
    assert w == [date(2026, 10, 19), date(2026, 10, 26)]


def test_sync_creates_metrics_and_is_idempotent(metrika):
    with SessionLocal() as db:
        n = ms.sync_metrika(db, today=date(2026, 11, 3))
        assert n == 12 * 3
        ms.sync_metrika(db, today=date(2026, 11, 3))
        metrics = db.scalars(select(Metric).where(Metric.source_kind.like("yandex_metrika:%"))).all()
        assert len(metrics) == 3
        assert len(db.scalars(select(MetricValue).where(MetricValue.metric_id == metrics[0].id)).all()) == 12


def test_sync_errors_are_readable(metrika):
    FakeClient.reply = Resp(403)
    with SessionLocal() as db, pytest.raises(ms.MetrikaError, match="Токен"):
        ms.sync_metrika(db)


def test_query_any_period_and_dimensions(metrika):
    FakeClient.reply = Resp(200, {"data": [{"dimensions": [{"name": "Поиск"}], "metrics": [5, 4]}], "totals": [5, 4], "total_rows": 1})
    out = ms.query_stat({"date1": "2024-01-01", "date2": "2024-03-31", "metrics": ["ym:s:visits", "ym:s:users"], "dimensions": ["ym:s:lastTrafficSource"]})
    assert "Поиск | 5 | 4" in out and "ИТОГО: 5 | 4" in out
    _, params, headers = FakeClient.calls[-1]
    assert params["date1"] == "2024-01-01" and params["ids"] == "555" and headers["Authorization"] == "OAuth tok"


@pytest.mark.parametrize("bad", [{"date1": "вчера", "date2": "2024-01-01"}, {"date1": "2024-01-01", "date2": "2024-01-02", "metrics": ["x; drop"]}, {"date1": "2024-01-01", "date2": "2024-01-02", "sort": "visits"}])
def test_query_rejects_bad_input(metrika, bad):
    with pytest.raises(ms.MetrikaError):
        ms.query_stat(bad)


def test_tool_is_available_in_chat():
    from app.agents.tools import TOOLS
    from app.routers.chat import CHAT_TOOLS

    assert "metrika_query" in TOOLS and "metrika_query" in CHAT_TOOLS


def _ctx(db, owner_id):
    from app.agents.tools import Ctx

    return Ctx(db, None, lambda _t: None, user_id=owner_id, label="ИИ-чат")


def test_create_metric_from_metrika_with_filter(metrika, owner):
    from app.agents.tools import TOOLS, ToolError
    from app.models import User

    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.role == "owner"))
        out = TOOLS["create_metric"].handler(_ctx(db, uid), {
            "name": "Визиты, небренд", "direction": "gen", "unit": "визитов",
            "metrika": {"metric": "ym:s:visits", "filters": "ym:s:lastSearchEngine=='yandex'", "period": "week", "count": 4},
        })
        assert "id=" in out and "4 периодов" in out
        m = db.scalar(select(Metric).where(Metric.name == "Визиты, небренд"))
        assert m.source_kind == ms.QUERY_KIND and len(db.scalars(select(MetricValue).where(MetricValue.metric_id == m.id)).all()) == 4
        sent = [c[1] for c in FakeClient.calls]
        assert all(p["filters"] == "ym:s:lastSearchEngine=='yandex'" and p["metrics"] == "ym:s:visits" for p in sent)
        with pytest.raises(ToolError, match="уже есть"):
            TOOLS["create_metric"].handler(_ctx(db, uid), {"name": "Визиты, небренд", "direction": "gen"})
        # обновление по расписанию идемпотентно
        assert ms.sync_custom(db) >= 4
        assert len(db.scalars(select(MetricValue).where(MetricValue.metric_id == m.id)).all()) >= 4


def test_create_metric_rolls_back_on_metrika_error(metrika, owner):
    from app.agents.tools import TOOLS, ToolError
    from app.models import User

    FakeClient.reply = Resp(400)
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.role == "owner"))
        with pytest.raises(ToolError, match="не создан"):
            TOOLS["create_metric"].handler(_ctx(db, uid), {"name": "Плохой", "direction": "gen", "metrika": {"metric": "ym:s:visits"}})
        assert db.scalar(select(Metric).where(Metric.name == "Плохой")) is None


def test_chat_write_tools_only_for_owner():
    from app.routers.chat import CHAT_TOOLS, OWNER_CHAT_TOOLS

    assert "create_metric" in OWNER_CHAT_TOOLS and "create_metric" not in CHAT_TOOLS
