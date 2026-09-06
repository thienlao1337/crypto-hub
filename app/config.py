import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Значения из .env.example. Они рабочие — с ними всё запускается и ничего
# не жалуется, — и именно поэтому опасны: подписанную известным секретом
# cookie подделает любой, кто видел исходники.
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

    # --- Веб-панель ---
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    session_secret: str = "dev-secret-change-me"
    # Флаг Secure у сессионной cookie. По умолчанию включён: панель
    # должна работать по https. Для локального запуска по http его
    # приходится снимать, иначе браузер не отправит cookie и вход не
    # состоится.
    session_secure_cookie: bool = True
    # Публичный адрес панели — нужен боту для ссылок и привязки аккаунта.
    public_url: str = "http://localhost:8000"

    # --- Шифрование API-ключей бирж ---
    # Fernet-ключ (urlsafe base64, 32 байта). Сгенерировать:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Потеря этого ключа = потеря доступа ко всем сохранённым ключам бирж,
    # их придётся заводить заново. Хранить отдельно от бэкапа БД.
    encryption_key: str = ""

    # --- Первый аккаунт (владелец) ---
    # Создаётся сидом при первом запуске; регистрация остальных — по инвайту.
    seed_owner_email: str = "owner@example.com"
    seed_owner_password: str = "change-me"

    # --- Telegram-бот ---
    bot_token: str = ""

    # --- Веб-пуш ---
    # Пара ключей VAPID: ими push-сервис браузера отличает наш сервер от
    # чужого. Сгенерировать:
    #   docker compose run --rm web python -m app.services.webpush
    # Пусто — веб-пуш просто выключен, остальные каналы работают.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    # Контакт для push-сервиса: по нему он свяжется, если с отправкой
    # что-то не так. Требование спецификации, mailto: или https:.
    vapid_subject: str = "mailto:admin@example.com"

    # --- Внешние источники данных ---
    # CoinGecko без ключа работает на публичном тире с жёстким лимитом;
    # demo-ключ бесплатный и поднимает лимит. Пусто — работаем без ключа.
    coingecko_api_key: str = ""

    # --- Хранение данных ---
    # Свечей оставляем по столько на каждую пару и таймфрейм. Читаются
    # всегда последние несколько сотен: и индикаторам, и графику больше
    # не нужно, а пишутся они непрерывно.
    candles_keep_per_series: int = 1500
    # Ноль в любом из трёх — «не удалять».
    login_events_keep_days: int = 180
    notifications_keep_days: int = 90
    global_stats_keep_days: int = 365

    # --- Интервалы фоновых задач, секунды ---
    sync_balances_interval: int = 60
    sync_trades_interval: int = 300
    sync_tickers_interval: int = 60
    poll_candles_interval: int = 30
    evaluate_alerts_interval: int = 15
    evaluate_signals_interval: int = 60
    portfolio_snapshot_interval: int = 900
    global_stats_interval: int = 300

    # --- Автотрейдинг ---
    # Глобальный рубильник. Даже при включённом флаге каждая стратегия
    # стартует в режиме paper и переводится в live отдельным действием.
    autotrade_enabled: bool = False

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


def deployment_problems(settings: "Settings") -> tuple[list[str], list[str]]:
    """Что мешает выпускать это в прод: (запрещающее, предупреждающее).

    Проверка нужна потому, что все эти значения работают. Забытый секрет
    сессии не ломает ничего видимого — панель просто открывается, — а
    подделать вход по нему может любой, кто читал репозиторий. Такое
    должно падать громко, а не ждать инцидента.
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
    """Не дать процессу подняться с настройками из примера.

    В режиме DEBUG только предупреждаем: разработчику незачем каждый раз
    заводить настоящие секреты, и падение здесь мешало бы работать.
    """
    settings = settings or get_settings()
    blocking, warnings = deployment_problems(settings)

    for message in warnings:
        logger.warning("Настройки: %s", message)

    if not blocking:
        return

    for message in blocking:
        logger.error("Настройки: %s", message)

    if settings.debug:
        logger.warning(
            "DEBUG=true, поэтому запуск продолжается. В проде эти настройки "
            "остановят процесс."
        )
        return

    raise RuntimeError(
        "Небезопасные настройки, запуск остановлен:\n- " + "\n- ".join(blocking)
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
