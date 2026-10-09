import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Values from .env.example. They work - everything starts with them and nothing
# complains - and that's exactly why they're dangerous: anyone who has seen the
# source can forge a cookie signed with a known secret.
DEFAULT_SESSION_SECRET = "dev-secret-change-me"
DEFAULT_OWNER_PASSWORD = "change-me"
DEFAULT_POSTGRES_PASSWORD = "cryptohub"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Crypto Hub"
    debug: bool = False

    # --- PostgreSQL ---
    postgres_user: str = "cryptohub"
    postgres_password: str = "cryptohub"
    postgres_db: str = "cryptohub"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # --- Web panel ---
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    session_secret: str = "dev-secret-change-me"
    # Secure flag on the session cookie. On by default: the panel is meant to
    # run over https. For a local run over http it has to be turned off,
    # otherwise the browser won't send the cookie and login fails.
    session_secure_cookie: bool = True
    # Public address of the panel - the bot needs it for links and account linking.
    public_url: str = "http://localhost:8000"

    # --- Encryption of exchange API keys ---
    # Fernet key (urlsafe base64, 32 bytes). Generate one:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Losing this key = losing access to all stored exchange keys; they'd have to be added again.
    # Store it separately from DB backups.
    encryption_key: str = ""

    # --- First account (owner) ---
    # Created by the seed on first start; everyone else registers by invite.
    seed_owner_email: str = "owner@example.com"
    seed_owner_password: str = "change-me"

    # --- Telegram bot ---
    bot_token: str = ""

    # --- Web push ---
    # VAPID key pair: the browser's push service uses it to tell our server
    # apart from anyone else's. Generate one:
    #   docker compose run --rm web python -m app.services.webpush
    # Empty - web push is simply disabled; the other channels keep working.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    # Contact for the push service: it uses it to reach us if something is
    # wrong with sending. Required by the spec, mailto: or https:.
    vapid_subject: str = "mailto:admin@example.com"

    # --- External data sources ---
    # Without a key CoinGecko runs on the public tier with a strict limit; a
    # demo key is free and raises the limit. Empty - we work without a key.
    coingecko_api_key: str = ""

    # --- P2P ---
    # Global kill switch, same as for auto-trading. When off, rules can be
    # configured and observed, but the bot doesn't move the ad price.
    p2p_enabled: bool = False
    sync_p2p_ads_interval: int = 300
    reprice_p2p_interval: int = 60

    # --- Data retention ---
    # We keep this many candles per pair and timeframe. Only the latest few
    # hundred are ever read - neither indicators nor the chart need more -
    # while new ones are written continuously.
    candles_keep_per_series: int = 1500
    # Zero in any of the three means "never delete".
    login_events_keep_days: int = 180
    notifications_keep_days: int = 90
    global_stats_keep_days: int = 365
    p2p_events_keep_days: int = 180

    # --- Background job intervals, seconds ---
    sync_balances_interval: int = 60
    sync_trades_interval: int = 300
    sync_tickers_interval: int = 60
    poll_candles_interval: int = 30
    evaluate_alerts_interval: int = 15
    evaluate_signals_interval: int = 60
    portfolio_snapshot_interval: int = 900
    global_stats_interval: int = 300

    # --- Auto-trading ---
    # Global kill switch. Even with the flag on, every strategy starts in paper
    # mode and is switched to live by a separate action.
    autotrade_enabled: bool = False

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


def deployment_problems(settings: "Settings") -> tuple[list[str], list[str]]:
    """What blocks shipping this to production: (blocking, warning).

    The check exists because all of these values work. A forgotten session secret breaks
    nothing visible - the panel just opens - yet anyone who has read the repository can
    forge a login with it. That has to fail loudly instead of waiting for an incident.
    """
    blocking: list[str] = []
    warnings: list[str] = []

    if settings.session_secret in ("", DEFAULT_SESSION_SECRET):
        blocking.append(
            "SESSION_SECRET оставлен из примера. Подписанную им cookie подделает "
            "любой, у кого есть исходники. Сгенерировать: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )

    if not settings.encryption_key:
        blocking.append(
            "ENCRYPTION_KEY не задан — ключи бирж сохранить будет нечем. "
            'Сгенерировать: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )

    if settings.seed_owner_password == DEFAULT_OWNER_PASSWORD:
        blocking.append(
            "SEED_OWNER_PASSWORD оставлен из примера — пароль владельца панели "
            "известен всем."
        )

    if settings.postgres_password == DEFAULT_POSTGRES_PASSWORD:
        warnings.append(
            "POSTGRES_PASSWORD оставлен из примера. Наружу база не публикуется, "
            "но пароль стоит сменить."
        )

    if not settings.session_secure_cookie and not settings.public_url.startswith(
        ("http://localhost", "http://127.0.0.1")
    ):
        warnings.append(
            "SESSION_SECURE_COOKIE=false при публичном адресе: сессионная cookie "
            "будет уходить по незашифрованному соединению."
        )

    return blocking, warnings


def verify_deployment(settings: "Settings | None" = None) -> None:
    """Prevent the process from starting with the example settings.

    In DEBUG mode we only warn: a developer shouldn't have to set up real secrets every
    time, and failing here would get in the way of work.
    """
    settings = settings or get_settings()
    blocking, warnings = deployment_problems(settings)

    for message in warnings:
        logger.warning("Settings: %s", message)

    if not blocking:
        return

    for message in blocking:
        logger.error("Settings: %s", message)

    if settings.debug:
        logger.warning(
            "DEBUG=true, so startup continues. In production these settings "
            "will stop the process."
        )
        return

    raise RuntimeError(
        "Небезопасные настройки, запуск остановлен:\n- " + "\n- ".join(blocking)
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
