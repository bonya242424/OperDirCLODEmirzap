from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from .config import settings
from .integrations.service import run_scheduled_checks


def start_scheduler() -> BackgroundScheduler:
    """Проверки интеграций: сразу при старте, дальше каждые N минут (по умолчанию 60).

    Запускать в одном процессе (uvicorn без --workers): иначе проверки задвоятся.
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_scheduled_checks,
        "interval",
        minutes=settings.integration_check_minutes,
        id="integration_checks",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
