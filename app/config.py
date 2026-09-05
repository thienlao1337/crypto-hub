from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Внешние источники данных ---
    # CoinGecko без ключа работает на публичном тире с жёстким лимитом;
    # demo-ключ бесплатный и поднимает лимит. Пусто — работаем без ключа.
    coingecko_api_key: str = ""

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
