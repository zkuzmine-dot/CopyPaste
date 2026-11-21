import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db.connection import create_db_engine, create_session_factory
from app.jobs.cleanup import run_channels_cleanup, run_cleanup
from app.jobs.files import run_disk_sweep, run_files_gc
from app.settings import settings
from app.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)


async def main() -> None:
    engine = create_db_engine()
    session_factory = create_session_factory(engine)

    storage = LocalFileStorage(settings.FILES_DIR)

    # Задачи — корутины: AsyncIOScheduler запускает их в этом же цикле событий
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_cleanup,
        "interval",
        seconds=settings.CLEANUP_INTERVAL_SECONDS,
        args=[session_factory],
        id="cleanup_expired_notes",
        max_instances=1,  # не запускать новый проход, пока не закончился предыдущий
        coalesce=True,  # пропущенные запуски (например, после паузы) выполнить один раз
    )
    scheduler.add_job(
        run_channels_cleanup,
        "interval",
        seconds=settings.CHANNEL_CLEANUP_INTERVAL_SECONDS,
        args=[session_factory],
        id="cleanup_channels",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_files_gc,
        "interval",
        seconds=settings.FILES_GC_INTERVAL_SECONDS,
        args=[session_factory, storage],
        id="cleanup_files",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_disk_sweep,
        "interval",
        seconds=settings.FILES_DISK_SWEEP_INTERVAL_SECONDS,
        args=[session_factory, storage],
        id="sweep_files_disk",
        max_instances=1,
        coalesce=True,
    )

    # docker stop шлёт SIGTERM: останавливаемся сразу
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    scheduler.start()
    logger.info(
        "Scheduler started: notes cleanup every %d s, channels cleanup every %d s, "
        "files cleanup every %d s, disk sweep every %d s",
        settings.CLEANUP_INTERVAL_SECONDS,
        settings.CHANNEL_CLEANUP_INTERVAL_SECONDS,
        settings.FILES_GC_INTERVAL_SECONDS,
        settings.FILES_DISK_SWEEP_INTERVAL_SECONDS,
    )
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(main())
