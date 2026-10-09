"""Background job scheduler.

A separate process: heavy exchange polling mustn't compete with handling web panel
requests, and one crashing mustn't take down the other.
"""

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings, verify_deployment
from app.worker import delivery, retention, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# APScheduler writes two INFO lines per run, and we have over a dozen jobs with
# the most frequent running every fifteen seconds: that's about seventeen
# thousand lines a day, among which a real error can't be found. Our own
# messages stay at INFO.
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("worker")
settings = get_settings()


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        job_defaults={
            # If the previous run is still going, don't start the next one: two
            # simultaneous syncs of the same key would just burn through the
            # exchange's request limit.
            "max_instances": 1,
            # Runs missed because of long execution aren't queued up.
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
        delivery.deliver_web_push,
        "interval",
        seconds=15,
        id="deliver_web_push",
    )
    scheduler.add_job(
        tasks.run_autotrade,
        "interval",
        seconds=60,
        id="run_autotrade",
    )
    scheduler.add_job(
        tasks.evaluate_signal_outcomes,
        "interval",
        minutes=15,
        id="evaluate_signal_outcomes",
    )
    scheduler.add_job(
        tasks.sync_p2p_ads,
        "interval",
        seconds=settings.sync_p2p_ads_interval,
        id="sync_p2p_ads",
    )
    scheduler.add_job(
        tasks.reprice_p2p,
        "interval",
        seconds=settings.reprice_p2p_interval,
        id="reprice_p2p",
    )
    # Once a day, at a quiet hour: deletion touches large tables, and there's
    # no reason to do it at the same time as exchange syncing.
    scheduler.add_job(
        retention.cleanup,
        "cron",
        hour=3,
        minute=20,
        id="cleanup_old_data",
    )
    return scheduler


async def main() -> None:
    verify_deployment(settings)

    scheduler = build_scheduler()

    # The pair list is needed before the first balance sync: without it the
    # portfolio valuation would be empty for the whole first window.
    logger.info("Initial load of trading pairs")
    await tasks.refresh_markets()
    await tasks.refresh_tickers()
    await tasks.refresh_global_stats()

    scheduler.start()
    logger.info(
        "Scheduler started: %s",
        ", ".join(sorted(job.id for job in scheduler.get_jobs())),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: signals in the event loop aren't supported, rely on
            # KeyboardInterrupt.
            pass

    try:
        await stop.wait()
    finally:
        logger.info("Stopping the scheduler")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
