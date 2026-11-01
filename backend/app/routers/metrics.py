from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import Metric, MetricValue, User, utcnow

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

Direction = Literal["gen", "tech", "ops", "fin"]
SPARK = 12


def _check_thresholds(better: str, warn: float | None, bad: float | None) -> None:
    if warn is None or bad is None:
        return
    if better == "up" and bad > warn:
        raise ValueError("Для показателя «больше — лучше» красный порог не может быть выше жёлтого")
    if better == "down" and bad < warn:
        raise ValueError("Для показателя «меньше — лучше» красный порог не может быть ниже жёлтого")


class MetricIn(BaseModel):
    direction: Direction
    name: str
    unit: str = ""
    description: str = ""
    better: Literal["up", "down"] = "up"
    warn_threshold: float | None = None
    bad_threshold: float | None = None
    expected_days: int = 7

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 200:
            raise ValueError("Название: от 1 до 200 символов")
        return v

    @field_validator("expected_days")
    @classmethod
    def _days(cls, v: int) -> int:
        if not 1 <= v <= 366:
            raise ValueError("Срок обновления: от 1 до 366 дней")
        return v

    @model_validator(mode="after")
    def _thresholds(self):
        _check_thresholds(self.better, self.warn_threshold, self.bad_threshold)
        return self


class MetricPatch(BaseModel):
    name: str | None = None
    unit: str | None = None
    description: str | None = None
    better: Literal["up", "down"] | None = None
    warn_threshold: float | None = None
    bad_threshold: float | None = None
    expected_days: int | None = None
    clear_thresholds: bool = False


class ValueIn(BaseModel):
    period: date
    value: float
    note: str = ""

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("Некорректное число")
        return v


class MetricOut(BaseModel):
    id: int
    direction: str
    name: str
    unit: str
    description: str
    better: str
    warn_threshold: float | None
    bad_threshold: float | None
    expected_days: int
    latest: float | None
    latest_period: date | None
    previous: float | None
    change: float | None
    status: str  # ok | warning | bad | no_data
    stale: bool
    history: list[float]


class ValueOut(BaseModel):
    period: date
    value: float
    note: str
    created_at: datetime


def evaluate(m: Metric, value: float | None) -> str:
    if value is None:
        return "no_data"
    if m.bad_threshold is not None and ((m.better == "up" and value <= m.bad_threshold) or (m.better == "down" and value >= m.bad_threshold)):
        return "bad"
    if m.warn_threshold is not None and ((m.better == "up" and value <= m.warn_threshold) or (m.better == "down" and value >= m.warn_threshold)):
        return "warning"
    return "ok"


def _out(db: Session, m: Metric) -> MetricOut:
    rows = db.scalars(
        select(MetricValue).where(MetricValue.metric_id == m.id).order_by(MetricValue.period.desc()).limit(SPARK)
    ).all()
    latest = rows[0] if rows else None
    prev = rows[1] if len(rows) > 1 else None
    stale = False
    if latest is not None:
        stale = (utcnow().date() - latest.period).days > m.expected_days
    return MetricOut(
        id=m.id, direction=m.direction, name=m.name, unit=m.unit, description=m.description, better=m.better,
        warn_threshold=m.warn_threshold, bad_threshold=m.bad_threshold, expected_days=m.expected_days,
        latest=latest.value if latest else None,
        latest_period=latest.period if latest else None,
        previous=prev.value if prev else None,
        change=(latest.value - prev.value) if latest and prev else None,
        status=evaluate(m, latest.value if latest else None),
        stale=stale,
        history=[r.value for r in reversed(rows)],
    )


def _get(db: Session, metric_id: int) -> Metric:
    m = db.get(Metric, metric_id)
    if m is None or not m.is_active:
        raise HTTPException(status_code=404, detail="Показатель не найден")
    return m


@router.get("", response_model=list[MetricOut])
def list_metrics(direction: Direction | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(Metric).where(Metric.is_active.is_(True)).order_by(Metric.id)
    if direction:
        q = q.where(Metric.direction == direction)
    return [_out(db, m) for m in db.scalars(q)]


@router.post("", response_model=MetricOut, status_code=201)
def create(body: MetricIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    m = Metric(**body.model_dump(), created_by=me.id)
    db.add(m)
    db.commit()
    return _out(db, m)


@router.patch("/{metric_id}", response_model=MetricOut)
def update(metric_id: int, body: MetricPatch, db: Session = Depends(get_db), _: User = Depends(current_user)):
    m = _get(db, metric_id)
    data = body.model_dump(exclude_unset=True)
    clear = data.pop("clear_thresholds", False)
    for key, value in data.items():
        if key == "name":
            value = (value or "").strip()
            if not value:
                raise HTTPException(status_code=422, detail="Название не может быть пустым")
        if key == "expected_days" and not (value and 1 <= value <= 366):
            raise HTTPException(status_code=422, detail="Срок обновления: от 1 до 366 дней")
        if key in ("warn_threshold", "bad_threshold") and clear:
            continue
        setattr(m, key, value)
    if clear:
        m.warn_threshold = m.bad_threshold = None
    try:
        _check_thresholds(m.better, m.warn_threshold, m.bad_threshold)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db.commit()
    return _out(db, m)


@router.delete("/{metric_id}", status_code=204)
def archive(metric_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Показатель скрывается, значения остаются в базе."""
    _get(db, metric_id).is_active = False
    db.commit()


@router.get("/{metric_id}/values", response_model=list[ValueOut])
def values(metric_id: int, limit: int = 60, db: Session = Depends(get_db), _: User = Depends(current_user)):
    _get(db, metric_id)
    limit = max(1, min(limit, 500))
    return db.scalars(
        select(MetricValue).where(MetricValue.metric_id == metric_id).order_by(MetricValue.period.desc()).limit(limit)
    ).all()


@router.post("/{metric_id}/values", response_model=MetricOut, status_code=201)
def add_value(metric_id: int, body: ValueIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    m = _get(db, metric_id)
    # +1 день запаса: в Москве после полуночи дата уже на день впереди UTC
    if body.period > utcnow().date() + timedelta(days=1):
        raise HTTPException(status_code=422, detail="Дата в будущем")
    row = db.scalar(select(MetricValue).where(MetricValue.metric_id == m.id, MetricValue.period == body.period))
    if row is None:
        db.add(MetricValue(metric_id=m.id, period=body.period, value=body.value, note=body.note.strip(), created_by=me.id))
    else:  # повторный ввод за тот же день заменяет значение
        row.value, row.note, row.created_by = body.value, body.note.strip(), me.id
    db.commit()
    return _out(db, m)
