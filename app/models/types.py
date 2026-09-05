"""Общие типы колонок.

Вынесены отдельно, чтобы решения по точности и диалектам были в одном
месте, а не размазаны по моделям.
"""

from sqlalchemy import JSON, BigInteger, Integer, Numeric
from sqlalchemy.dialects.postgresql import JSONB

# JSONB на PostgreSQL (индексируемый), обычный JSON на SQLite — на нём
# гоняются юнит-тесты сервисного слоя.
JsonB = JSON().with_variant(JSONB, "postgresql")

# SQLite делает автоинкремент только для колонки типа INTEGER, поэтому
# большие первичные ключи объявляем с вариантом для тестового диалекта.
BigPk = BigInteger().with_variant(Integer, "sqlite")

# Деньги и количества — только Numeric. float здесь недопустим: накопленная
# ошибка округления на балансах и PnL даёт расхождение с отчётом биржи,
# и объяснить это клиенту будет нечем.
Amount = Numeric(36, 18)  # количество монет
Price = Numeric(36, 18)  # цена инструмента
Usd = Numeric(20, 8)  # оценка в долларах
Pct = Numeric(12, 4)  # проценты
