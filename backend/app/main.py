import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import func, select

from .config import settings
from .db import Base, SessionLocal, add_missing_columns, engine
from .integrations.service import ensure_rows
from .models import User
from . import search as search_index
from .guide_calc import reset_stale as reset_stale_guides
from .metric_calc import reset_stale
from .routers import search as search_router
from . import changelog as changelog_data
from .routers import agents, changelog, proxies, auth, board, returns, chat, connections, files, integrations, metrics, tasks, users
from .scheduler import start_scheduler
from .security import hash_password

log = logging.getLogger("app")


def _check_secret_key() -> None:
    if settings.environment == "prod" and settings.secret_key in ("", "change-me"):
        raise RuntimeError("В prod нужно задать надёжный SECRET_KEY (например: openssl rand -hex 32)")


def _bootstrap_owner() -> None:
    with SessionLocal() as db:
        if (db.scalar(select(func.count()).select_from(User)) or 0) > 0:
            return
        if not settings.admin_email or not settings.admin_password:
            log.warning("В базе нет пользователей, а ADMIN_EMAIL/ADMIN_PASSWORD не заданы: войти будет нельзя")
            return
        db.add(
            User(
                email=settings.admin_email.strip().lower(),
                password_hash=hash_password(settings.admin_password),
                full_name="Владелец",
                role="owner",
                can_apply_changes=True,
            )
        )
        db.commit()
        log.info("Создан владелец %s", settings.admin_email)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _check_secret_key()
    # Для старта таблицы создаются автоматически; миграции (Alembic) добавим, когда схема устоится.
    Base.metadata.create_all(engine)
    for col in add_missing_columns():
        log.warning("В базу добавлена колонка %s", col)
    _bootstrap_owner()
    with SessionLocal() as db:
        ensure_rows(db)
        search_index.index_missing(db)
        changelog_data.sync(db)
    reset_stale()
    reset_stale_guides()
    scheduler = start_scheduler() if settings.scheduler_enabled else None
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(title="OperDir", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(integrations.router)
app.include_router(chat.router)
app.include_router(connections.router)
app.include_router(files.router)
app.include_router(tasks.router)
app.include_router(metrics.router)
app.include_router(board.router)
app.include_router(returns.router)
app.include_router(agents.router)
app.include_router(proxies.router)
app.include_router(search_router.router)
app.include_router(changelog.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
