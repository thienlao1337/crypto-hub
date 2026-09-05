"""ORM-модели.

Импортируются здесь целиком, чтобы Alembic видел все таблицы при
автогенерации миграций.
"""

from app.db import Base
from app.models.alert import (
    Alert,
    AlertTrigger,
    AlertType,
    Notification,
    NotificationSetting,
    PushSubscription,
)
from app.models.audit import AuditLog
from app.models.exchange import Exchange, ExchangeAccount
from app.models.market import (
    Asset,
    Candle,
    GlobalStats,
    Market,
    MarketTicker,
    Timeframe,
)
from app.models.portfolio import (
    Balance,
    PortfolioSnapshot,
    Position,
    Trade,
    WatchlistItem,
)
from app.models.signal import Signal, SignalOutcome, SignalRule
from app.models.trading import BotJournalEntry, BotOrder, RiskState, Strategy
from app.models.user import Invite, LoginEvent, User, UserRecoveryCode

__all__ = [
    "Base",
    # user
    "User",
    "UserRecoveryCode",
    "Invite",
    "LoginEvent",
    # audit
    "AuditLog",
    # exchange
    "Exchange",
    "ExchangeAccount",
    # market
    "Asset",
    "Market",
    "Timeframe",
    "Candle",
    "MarketTicker",
    "GlobalStats",
    # portfolio
    "Balance",
    "PortfolioSnapshot",
    "Trade",
    "Position",
    "WatchlistItem",
    # signal
    "SignalRule",
    "Signal",
    "SignalOutcome",
    # alert
    "AlertType",
    "Alert",
    "AlertTrigger",
    "Notification",
    "NotificationSetting",
    "PushSubscription",
    # trading
    "Strategy",
    "BotOrder",
    "BotJournalEntry",
    "RiskState",
]
