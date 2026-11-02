from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    position: Mapped[str] = mapped_column(String(255), default="")
    department: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    # owner | employee
    role: Mapped[str] = mapped_column(String(32), default="employee")
    # Право нажимать «Применить» и «Откатить» для доработок из чата.
    can_apply_changes: Mapped[bool] = mapped_column(Boolean, default=False)
    bitrix_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Integration(Base):
    __tablename__ = "integrations"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    # ok | warning | error | not_configured | unknown
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    message: Mapped[str] = mapped_column(Text, default="")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Когда данные из источника в последний раз успешно попали в систему.
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntegrationCheck(Base):
    __tablename__ = "integration_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    integration_key: Mapped[str] = mapped_column(ForeignKey("integrations.key"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Connection(Base):
    """Подключение пользователя или компании (личный Telegram, свои таблицы, общий FTP…).

    owner_id = NULL — подключение компании. Значения полей лежат в зашифрованном виде.
    """

    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(255))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    config_encrypted: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    message: Mapped[str] = mapped_column(Text, default="")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # Ключ интеграции: загрузка файла в папку отмечает свежую выгрузку этого источника.
    source_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StoredFile(Base):
    __tablename__ = "stored_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    disk_name: Mapped[str] = mapped_column(String(64), unique=True)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BitrixTask(Base):
    __tablename__ = "bitrix_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # id задачи в Битрикс24
    title: Mapped[str] = mapped_column(String(500))
    responsible_id: Mapped[int] = mapped_column(Integer, index=True)  # id пользователя в Битрикс24
    status: Mapped[int] = mapped_column(Integer, default=2)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    description: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # родительская задача
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # проект/группа в Битрикс24


class TaskMessage(Base):
    """Чат с ИИ-агентом внутри карточки задачи. task_ref: «l-12» своя задача, «b-345» из Битрикс24."""

    __tablename__ = "task_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_ref: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Metric(Base):
    """Показатель директора: название, единицы, пороги. Значения лежат в MetricValue."""

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)  # gen | tech | ops | fin
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # up — чем больше, тем лучше; down — чем меньше, тем лучше
    better: Mapped[str] = mapped_column(String(8), default="up")
    warn_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    bad_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Как часто ждём новое значение (в днях); позже этого срока показатель считается устаревшим
    expected_days: Mapped[int] = mapped_column(Integer, default=7)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricValue(Base):
    __tablename__ = "metric_values"
    __table_args__ = (UniqueConstraint("metric_id", "period", name="uq_metric_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), index=True)
    period: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    """Задача на доске. Задачи из Битрикс24 живут отдельно (BitrixTask) и показываются на доске только для чтения."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    # new | in_progress | review | done
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal")  # low | normal | high
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReturnBatch(Base):
    """Сборка возвратов одному поставщику, загруженная из таблицы."""

    __tablename__ = "return_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier: Mapped[str] = mapped_column(String(200), index=True)
    number: Mapped[str] = mapped_column(String(200), default="")  # «№000007007 от 02.11.2026»
    source: Mapped[str] = mapped_column(String(500), default="")  # имя файла или id таблицы
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReturnLine(Base):
    __tablename__ = "return_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("return_batches.id", ondelete="CASCADE"), index=True)
    article: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(500), default="")
    brand: Mapped[str] = mapped_column(String(200), default="")
    qty: Mapped[int] = mapped_column(Integer)
    warehouse: Mapped[str] = mapped_column(String(200), default="")
    invoice_date: Mapped[str] = mapped_column(String(32), default="")
    invoice_no: Mapped[str] = mapped_column(String(100), default="", index=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    # pending | submitted | skipped | failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    request_no: Mapped[str] = mapped_column(String(100), default="")
    note: Mapped[str] = mapped_column(Text, default="")
