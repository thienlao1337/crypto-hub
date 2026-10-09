"""Background jobs.

A rule shared by all jobs below: a unit of work (one connection, one exchange, one user)
is processed in its own session.

This isn't done for tidiness but out of necessity. A rollback marks all loaded ORM
objects as expired, and the next access to any of their fields - even an id in a log
line - triggers a SELECT from synchronous code, which fails with MissingGreenlet in
async SQLAlchemy. With a shared session a failure on the first connection would break
processing of all the others. So lists are collected as plain values up front, and
objects are loaded inside their own transaction.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.exchanges.ccxt_client import CcxtAdapter
from app.models import (
    Alert,
    P2PAd,
    P2PPriceRule,
    Exchange,
    ExchangeAccount,
    Market,
    Signal,
    SignalRule,
    Strategy,
    Timeframe,
    User,
    WatchlistItem,
)
from app.models.exchange import KEY_STATUS_ERROR, KEY_STATUS_INVALID
from app.models.signal import DIRECTION_NEUTRAL
from app.services import exchange_keys_service as keys_service
from app.services import (
    alert_service,
    autotrade_service,
    dashboard_service,
    notification_service,
    candle_service,
    market_service,
    p2p_service,
    portfolio_service,
    position_service,
    signal_service,
)

logger = logging.getLogger(__name__)

# Pairs against stablecoins come first: the portfolio valuation is computed
# from them. The rest will be pulled in when charts need them.
QUOTE_FILTER = {"USDT", "USDC"}


async def refresh_markets() -> None:
    """Refresh the trading pair reference table. Once a day is enough."""
    for code in await _active_exchange_codes():
        try:
            async with session_scope() as session:
                exchange = await _exchange_by_code(session, code)
                async with CcxtAdapter(code) as adapter:
                    count = await market_service.sync_markets(
                        session, exchange, adapter, only_quotes=QUOTE_FILTER
                    )
                await session.commit()
            logger.info("Pairs for %s updated: %s", code, count)
        except Exception:
            logger.exception("Could not update pairs for %s", code)


async def refresh_tickers() -> None:
    """Refresh the quote snapshot. The portfolio valuation relies on it."""
    for code in await _active_exchange_codes():
        try:
            async with session_scope() as session:
                exchange = await _exchange_by_code(session, code)
                async with CcxtAdapter(code) as adapter:
                    count = await market_service.update_tickers(session, exchange, adapter)
                await session.commit()
            logger.debug("Quotes for %s updated: %s", code, count)
        except Exception:
            logger.exception("Could not update quotes for %s", code)

    # Positions are revalued here too, not in a separate job: the entry price
    # and the "now" price must come from the same quote snapshot, otherwise
    # unrealized PnL shows the difference between different moments in time.
    try:
        async with session_scope() as session:
            marked = await position_service.mark_positions(session)
            await session.commit()
        if marked:
            logger.debug("Positions revalued: %s", marked)
    except Exception:
        logger.exception("Could not revalue positions")


async def sync_balances() -> None:
    """Refresh balances for all active connections."""
    for account_id in await _syncable_account_ids():
        try:
            async with session_scope() as session:
                account = await session.get(ExchangeAccount, account_id)
                if account is None:
                    continue

                adapter = await keys_service.build_adapter(session, account)
                try:
                    count = await portfolio_service.sync_balances(session, account, adapter)
                    await keys_service.mark_synced(session, account)
                    await session.commit()
                finally:
                    await adapter.close()
            logger.debug("Balances of connection %s: %s coins", account_id, count)
        except Exception as exc:
            logger.warning("Balances of connection %s not updated: %s", account_id, exc)
            await _remember_error(account_id, exc)


async def sync_trades() -> None:
    """Backfill trade history for pairs in the watchlist.

    Exchanges don't return history "for all pairs at once", so we only ask for what the
    user is watching: otherwise every sync would cost hundreds of requests.
    """
    for account_id in await _syncable_account_ids():
        try:
            async with session_scope() as session:
                account = await session.get(ExchangeAccount, account_id)
                if account is None:
                    continue

                symbols = await _watchlist_symbols(session, account)
                if not symbols:
                    continue

                adapter = await keys_service.build_adapter(session, account)
                try:
                    count = await portfolio_service.sync_trades(
                        session, account, adapter, symbols=symbols
                    )
                    # A new trade changes the average entry price, so positions
                    # are rebuilt right away: a PnL that's off until the next
                    # cycle is a wrong number on screen.
                    await position_service.rebuild_positions(session, account)
                    await session.commit()
                finally:
                    await adapter.close()

            if count:
                logger.info("Connection %s: %s new trades", account_id, count)
        except Exception as exc:
            logger.warning("Trades of connection %s not updated: %s", account_id, exc)


async def snapshot_portfolios() -> None:
    """Record a value chart point for every user."""
    for user_id in await _active_user_ids():
        try:
            async with session_scope() as session:
                user = await session.get(User, user_id)
                if user is None:
                    continue
                snapshot = await portfolio_service.take_snapshot(session, user)
                await session.commit()
                total = snapshot.total_usd if snapshot is not None else None
            if total is not None:
                logger.debug("Portfolio snapshot %s: %s", user_id, total)
        except Exception:
            logger.exception("Could not snapshot portfolio of user %s", user_id)


# --- P2P ---


async def sync_p2p_ads() -> None:
    """Refresh the ad mirror for connections with P2P access."""
    if not get_settings().p2p_enabled:
        return

    for account_id in await _p2p_account_ids():
        try:
            async with session_scope() as session:
                account = await session.get(ExchangeAccount, account_id)
                if account is None:
                    continue

                adapter = await p2p_service.build_adapter(session, account)
                try:
                    count = await p2p_service.sync_ads(session, account, adapter)
                    # Orders are pulled in the same pass: a separate job for
                    # one request to the same marketplace would be redundant.
                    orders = await p2p_service.sync_orders(session, account, adapter)
                    await session.commit()
                finally:
                    await adapter.close()
            logger.debug(
                "Connection %s: %s ads, %s orders", account_id, count, orders
            )
        except Exception as exc:
            logger.warning("Ads of connection %s not updated: %s", account_id, exc)


async def reprice_p2p() -> None:
    """Recompute the price for every running rule."""
    if not get_settings().p2p_enabled:
        return

    for ad_id in await _active_rule_ad_ids():
        try:
            await _reprice_one(ad_id)
        except Exception:
            logger.exception("Rule for ad %s failed", ad_id)


async def _reprice_one(ad_id: int) -> None:
    async with session_scope() as session:
        ad = await session.get(P2PAd, ad_id)
        if ad is None or ad.status == "closed":
            return

        rule = await session.scalar(
            select(P2PPriceRule).where(P2PPriceRule.ad_id == ad.id)
        )
        if rule is None or not rule.is_active:
            return

        account = await session.get(ExchangeAccount, ad.exchange_account_id)
        if account is None or not account.can_p2p:
            return

        adapter = await p2p_service.build_adapter(session, account)
        try:
            event = await p2p_service.apply_rule(session, ad, rule, adapter)
            await session.commit()
            if event is not None:
                logger.debug("Ad %s: %s", ad_id, event.event_type)
        finally:
            await adapter.close()


async def _p2p_account_ids() -> list[int]:
    async with session_scope() as session:
        result = await session.execute(
            select(ExchangeAccount.id).where(
                ExchangeAccount.requested_p2p.is_(True),
                ExchangeAccount.allow_p2p.is_(True),
                ExchangeAccount.status != KEY_STATUS_INVALID,
            )
        )
        return [account_id for (account_id,) in result]


async def _active_rule_ad_ids() -> list[int]:
    async with session_scope() as session:
        result = await session.execute(
            select(P2PPriceRule.ad_id)
            .where(P2PPriceRule.is_active.is_(True))
            .order_by(P2PPriceRule.ad_id)
        )
        return [ad_id for (ad_id,) in result]


# --- Lists as plain values ---


async def _active_exchange_codes() -> list[str]:
    async with session_scope() as session:
        result = await session.execute(
            select(Exchange.code)
            .where(Exchange.is_active.is_(True))
            .order_by(Exchange.sort_order)
        )
        return [code for (code,) in result]


async def _syncable_account_ids() -> list[int]:
    """Connections worth polling.

    Keys deemed invalid are skipped: the exchange would refuse anyway, and the request
    limit would be spent.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(ExchangeAccount.id)
            .where(ExchangeAccount.status != KEY_STATUS_INVALID)
            .order_by(ExchangeAccount.id)
        )
        return [account_id for (account_id,) in result]


async def _active_user_ids() -> list[int]:
    async with session_scope() as session:
        result = await session.execute(
            select(User.id).where(User.is_active.is_(True)).order_by(User.id)
        )
        return [user_id for (user_id,) in result]


# --- Helpers ---


async def _exchange_by_code(session, code: str) -> Exchange:
    result = await session.execute(select(Exchange).where(Exchange.code == code))
    return result.scalar_one()


async def _watchlist_symbols(session, account: ExchangeAccount) -> list[str]:
    result = await session.execute(
        select(Market.symbol)
        .join(WatchlistItem, WatchlistItem.market_id == Market.id)
        .where(
            WatchlistItem.user_id == account.user_id,
            Market.exchange_id == account.exchange_id,
        )
    )
    return [symbol for (symbol,) in result]


async def _remember_error(account_id: int, exc: Exception) -> None:
    """Record the failure reason in a separate session.

    A dedicated session because the previous one has already been closed by the failure,
    and the message must not be lost: without it the user won't understand why the
    portfolio stopped updating.
    """
    try:
        async with session_scope() as session:
            account = await session.get(ExchangeAccount, account_id)
            if account is None:
                return

            was_working = account.status != KEY_STATUS_ERROR
            await keys_service.mark_sync_error(session, account, str(exc))

            # Notify only on the transition to failure. Otherwise every sync
            # cycle would send the same thing until the key is fixed - and the
            # feed would turn into noise.
            if was_working:
                await notification_service.dispatch(
                    session,
                    user_id=account.user_id,
                    kind=notification_service.KIND_SYSTEM,
                    title="Биржа перестала отвечать",
                    body=(
                        f"Подключение «{account.label}» не синхронизируется: "
                        f"{account.last_error}"
                    ),
                    payload={"exchange_account_id": account.id},
                )

            await session.commit()
    except Exception:
        logger.exception("Could not record the sync error for connection %s", account_id)


# --- Candles, signals and alerts ---


async def poll_candles() -> None:
    """Keep candles fresh for pairs someone needs.

    Downloading everything isn't an option: there are over a thousand pairs, and the
    candles table grows fast. We take only what signal rules, alerts and watchlists rely
    on.
    """
    targets = await _candle_targets()

    for market_id, timeframe_id in targets:
        try:
            async with session_scope() as session:
                market = await session.get(Market, market_id)
                timeframe = await session.get(Timeframe, timeframe_id)
                if market is None or timeframe is None:
                    continue
                if await candle_service.is_fresh(session, market, timeframe):
                    continue

                exchange = await session.get(Exchange, market.exchange_id)
                async with CcxtAdapter(exchange.code) as adapter:
                    await candle_service.sync_candles(session, market, timeframe, adapter)
                await session.commit()
        except Exception:
            logger.exception("Could not update candles for pair %s", market_id)


async def evaluate_signals() -> None:
    """Evaluate signal rules on fresh candles."""
    async with session_scope() as session:
        rule_ids = [rule.id for rule in await signal_service.active_rules(session)]

    for rule_id in rule_ids:
        try:
            async with session_scope() as session:
                rule = await session.get(SignalRule, rule_id)
                if rule is None:
                    continue

                timeframe = await session.get(Timeframe, rule.timeframe_id)
                for market_id in await _rule_markets(session, rule):
                    market = await session.get(Market, market_id)
                    if market is None:
                        continue
                    signal = await signal_service.evaluate_rule(
                        session, rule, market, timeframe
                    )
                    if signal is not None:
                        logger.info(
                            "Signal %s for %s: %s",
                            signal.direction, market.symbol, signal.reason,
                        )
                        await _notify_signal(session, rule, signal, market.symbol)
                await session.commit()
        except Exception:
            logger.exception("Signal rule %s failed", rule_id)


async def evaluate_signal_outcomes() -> None:
    """Check whether signals whose horizon has passed played out."""
    try:
        async with session_scope() as session:
            count = await signal_service.evaluate_outcomes(session)
            await session.commit()
        if count:
            logger.info("Signals scored: %s", count)
    except Exception:
        logger.exception("Could not score signal results")


async def evaluate_alerts() -> None:
    """Check alert conditions and send the triggered ones."""
    try:
        async with session_scope() as session:
            fired = await alert_service.evaluate_all(session)
            await session.commit()
        for trigger in fired:
            logger.info("Alert triggered: %s", trigger.message)
    except Exception:
        logger.exception("Could not check alerts")


async def _notify_signal(session, rule: SignalRule, signal, symbol: str) -> None:
    """Put the signal in the feed of those who watch this pair.

    For a shared rule (user_id empty) recipients are determined by watchlists: there's
    no point sending everyone a signal on someone else's pair.

    A neutral verdict stays on the signals screen and doesn't produce a notification:
    "we looked and decided not to enter" isn't worth ringing Telegram in the middle of
    the night.
    """
    if signal.direction == DIRECTION_NEUTRAL:
        return

    if rule.user_id is not None:
        recipients = [rule.user_id]
    else:
        result = await session.execute(
            select(WatchlistItem.user_id)
            .where(WatchlistItem.market_id == signal.market_id)
            .distinct()
        )
        recipients = [user_id for (user_id,) in result]

    word = "покупка" if signal.direction == "buy" else "продажа"
    for user_id in recipients:
        await notification_service.dispatch(
            session,
            user_id=user_id,
            kind=notification_service.KIND_SIGNAL,
            title=f"Сигнал: {symbol} — {word}",
            body=signal.reason,
            payload={"signal_id": signal.id, "symbol": symbol},
        )


async def refresh_global_stats() -> None:
    """Fetch market-wide metrics for the dashboard."""
    try:
        async with session_scope() as session:
            snapshot = await dashboard_service.refresh_global_stats(session)
            await session.commit()
        if snapshot is not None:
            logger.info(
                "Market metrics updated: market cap %s, index %s",
                snapshot.total_market_cap_usd,
                snapshot.fng_value,
            )
    except Exception:
        logger.exception("Could not update market-wide metrics")


# A signal older than this isn't acted on by the strategy: after downtime the
# bot shouldn't suddenly open positions for yesterday's reasons.
SIGNAL_MAX_AGE = timedelta(minutes=30)


async def run_autotrade() -> None:
    """Act on fresh signals with active strategies.

    The global kill switch is checked both here and in the service: the job simply
    shouldn't do anything when auto-trading is off.
    """
    if not get_settings().autotrade_enabled:
        return

    async with session_scope() as session:
        result = await session.execute(
            select(Strategy.id).where(Strategy.is_active.is_(True)).order_by(Strategy.id)
        )
        strategy_ids = [strategy_id for (strategy_id,) in result]

    for strategy_id in strategy_ids:
        try:
            await _run_strategy(strategy_id)
        except Exception:
            logger.exception("Strategy %s failed", strategy_id)


async def _run_strategy(strategy_id: int) -> None:
    async with session_scope() as session:
        strategy = await session.get(Strategy, strategy_id)
        if strategy is None or not strategy.is_active:
            return

        since = datetime.now(timezone.utc) - SIGNAL_MAX_AGE
        query = select(Signal).where(
            Signal.rule_id == strategy.signal_rule_id,
            Signal.market_id == strategy.market_id,
            Signal.created_at >= since,
        )
        # Take only what the strategy hasn't reviewed yet. Previously the
        # selection was based on the absence of an order, and a signal the
        # strategy declined to act on was picked up again on every pass.
        if strategy.last_signal_id is not None:
            query = query.where(Signal.id > strategy.last_signal_id)

        pending = await session.execute(query.order_by(Signal.id))
        signals = list(pending.scalars())

        # Exits are checked regardless, even when there are no new signals: a
        # stop-loss is a stop precisely because it fires on its own, not on a
        # signal.
        position = await autotrade_service.open_position(session, strategy)
        if not signals and position is None:
            return

        adapter = None
        if strategy.mode != "paper":
            account = await session.get(ExchangeAccount, strategy.exchange_account_id)
            adapter = await keys_service.build_adapter(session, account)

        try:
            closed = await autotrade_service.check_exits(session, strategy, adapter=adapter)
            if closed is not None:
                logger.info(
                    "Strategy %s: position closed at exit level, result %s",
                    strategy.id, closed.realized_pnl,
                )

            for signal in signals:
                order = await autotrade_service.execute(
                    session, strategy, signal, adapter=adapter
                )
                if order is not None:
                    logger.info(
                        "Strategy %s: order %s %s on signal %s",
                        strategy.id, order.side, order.amount, signal.id,
                    )
            await session.commit()
        finally:
            if adapter is not None:
                await adapter.close()


async def _candle_targets() -> list[tuple[int, int]]:
    """Pairs and timeframes that need candles."""
    async with session_scope() as session:
        targets: set[tuple[int, int]] = set()

        rules = await session.execute(
            select(SignalRule.market_id, SignalRule.timeframe_id, SignalRule.user_id)
            .where(SignalRule.is_active.is_(True))
        )
        for market_id, timeframe_id, user_id in rules:
            if market_id is not None:
                targets.add((market_id, timeframe_id))
                continue
            # A rule without a specific pair is evaluated against the watchlist.
            watched = await session.execute(
                select(WatchlistItem.market_id).where(
                    WatchlistItem.user_id == user_id
                    if user_id is not None
                    else WatchlistItem.user_id.is_not(None)
                )
            )
            for (watched_market_id,) in watched:
                targets.add((watched_market_id, timeframe_id))

        # Alerts need their own timeframe for RSI and period change.
        alert_timeframe = await session.execute(
            select(Timeframe.id).where(Timeframe.code == alert_service.ALERT_TIMEFRAME)
        )
        alert_timeframe_id = alert_timeframe.scalar_one_or_none()
        if alert_timeframe_id is not None:
            markets = await session.execute(
                select(Alert.market_id).where(Alert.is_active.is_(True)).distinct()
            )
            for (market_id,) in markets:
                targets.add((market_id, alert_timeframe_id))

        return sorted(targets)


async def _rule_markets(session, rule: SignalRule) -> list[int]:
    if rule.market_id is not None:
        return [rule.market_id]

    query = select(WatchlistItem.market_id).distinct()
    if rule.user_id is not None:
        query = query.where(WatchlistItem.user_id == rule.user_id)
    result = await session.execute(query)
    return [market_id for (market_id,) in result]
