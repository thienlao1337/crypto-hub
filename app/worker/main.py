"""Планировщик фоновых задач.

Отдельный процесс: тяжёлые опросы бирж не должны конкурировать с
обработкой запросов веб-панели, а падение одного не должно ронять другое.
"""

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.worker import delivery, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("worker")
settings = get_settings()


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        job_defaults={
            # Если предыдущий запуск ещё идёт, следующий не запускаем:
            # две одновременные синхронизации одного ключа только выберут
            # лимит запросов биржи.
            "max_instances": 1,
            # Пропущенные из-за долгой работы запуски не копим.
            "coalesce": True,
            "misfire_grace_time": 60,
        }
    )

    scheduler.add_job(
        tasks.refresh_markets,
        "interval",
        hours=24,
        id="refresh_markets",
        next_run_time=None,
    )
    scheduler.add_job(
        tasks.refresh_tickers,
        "interval",
        seconds=settings.sync_tickers_interval,
        id="refresh_tickers",
    )
    scheduler.add_job(
        tasks.sync_balances,
        "interval",
        seconds=settings.sync_balances_interval,
        id="sync_balances",
    )
    scheduler.add_job(
        tasks.sync_trades,
        "interval",
        seconds=settings.sync_trades_interval,
        id="sync_trades",
    )
    scheduler.add_job(
        tasks.snapshot_portfolios,
        "interval",
        seconds=settings.portfolio_snapshot_interval,
        id="snapshot_portfolios",
    )
    scheduler.add_job(
        tasks.poll_candles,
        "interval",
        seconds=settings.poll_candles_interval,
        id="poll_candles",
    )
    scheduler.add_job(
        tasks.evaluate_signals,
        "interval",
        seconds=settings.evaluate_signals_interval,
        id="evaluate_signals",
    )
    scheduler.add_job(
        tasks.evaluate_alerts,
        "interval",
        seconds=settings.evaluate_alerts_interval,
        id="evaluate_alerts",
    )
    scheduler.add_job(
        tasks.refresh_global_stats,
        "interval",
        seconds=settings.global_stats_interval,
        id="refresh_global_stats",
    )
    scheduler.add_job(
        delivery.deliver_pending,
        "interval",
        seconds=15,
        id="deliver_telegram",
    )
    scheduler.add_job(
        tasks.evaluate_signal_outcomes,
        "interval",
        minutes=15,
        id="evaluate_signal_outcomes",
    )
    return scheduler


async def main() -> None:
    scheduler = build_scheduler()

    # Справочник пар нужен до первой синхронизации балансов: без него
    # оценка портфеля будет пустой на всё первое окно.
    logger.info("Первичная загрузка торговых пар")
    await tasks.refresh_markets()
    await tasks.refresh_tickers()
    await tasks.refresh_global_stats()

    scheduler.start()
    logger.info(
        "Планировщик запущен: %s",
        ", ".join(sorted(job.id for job in scheduler.get_jobs())),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: сигналы в цикле не поддерживаются, полагаемся на
            # KeyboardInterrupt.
            pass

    try:
        await stop.wait()
    finally:
        logger.info("Останавливаем планировщик")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
