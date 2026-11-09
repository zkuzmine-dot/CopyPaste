import logging
import signal

from apscheduler.schedulers.blocking import BlockingScheduler

from app.db.connection import create_db_engine, create_session_factory
from app.jobs.cleanup import run_cleanup
from app.settings import settings

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    engine = create_db_engine()
    session_factory = create_session_factory(engine)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_cleanup,
        "interval",
        seconds=settings.CLEANUP_INTERVAL_SECONDS,
        args=[session_factory],
        id="cleanup_expired_notes",
        max_instances=1,  # не запускать новый проход, пока не закончился предыдущий
        coalesce=True,  # пропущенные запуски (например, после паузы) выполнить один раз
    )

    # docker stop шлёт SIGTERM: останавливаемся сразу
    signal.signal(signal.SIGTERM, lambda *_: scheduler.shutdown(wait=False))

    logger.info("Scheduler started, cleanup every %d s", settings.CLEANUP_INTERVAL_SECONDS)
    try:
        scheduler.start()
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
