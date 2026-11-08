from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from .agents.service import run_due_agents, stop_stale_runs
from .bitrix_board import run_scheduled_board
from .bitrix_watch import run_scheduled_watch
from .config import settings
from .integrations.service import run_scheduled_checks
from .metrika_sync import run_scheduled_metrika


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
    scheduler.add_job(
        run_scheduled_board,
        "interval",
        minutes=settings.bitrix_sync_minutes,
        id="bitrix_sync",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_scheduled_watch,
        "interval",
        minutes=settings.bitrix_watch_minutes,
        id="bitrix_watch",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_scheduled_metrika,
        "interval",
        hours=6,
        id="metrika_sync",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )
    stop_stale_runs()
    scheduler.add_job(run_due_agents, "interval", minutes=1, id="agents_due", max_instances=1, coalesce=True)
    from .return_jobs import recover_jobs
    scheduler.add_job(recover_jobs, "interval", seconds=15, id="returns_recovery", max_instances=1, coalesce=True, next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5))
    from .supplier_credentials import synchronize
    scheduler.add_job(synchronize, "interval", hours=1, id="supplier_credentials", max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5))
    scheduler.start()
    return scheduler
