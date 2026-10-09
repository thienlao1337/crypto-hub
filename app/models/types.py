"""Shared column types.

Kept separately so decisions about precision and dialects live in one place instead of
being scattered across models.
"""

from sqlalchemy import JSON, BigInteger, Integer, Numeric
from sqlalchemy.dialects.postgresql import JSONB

# JSONB on PostgreSQL (indexable), plain JSON on other dialects.
JsonB = JSON().with_variant(JSONB, "postgresql")

# SQLite only auto-increments an INTEGER column. Tests run on PostgreSQL now,
# but the variant stays: it costs nothing and removes a pitfall if the database
# is ever run on SQLite.
BigPk = BigInteger().with_variant(Integer, "sqlite")

# Money and quantities - Numeric only. float is not acceptable here:
# accumulated rounding error on balances and PnL produces a mismatch with the
# exchange report, and there'd be no way to explain it to the client.
Amount = Numeric(36, 18)  # coin quantity
Price = Numeric(36, 18)  # instrument price
Usd = Numeric(20, 8)  # value in dollars
# Market-wide totals: the total market cap is measured in trillions, while
# Numeric(20, 8) holds less than 10^12 - live data overflows it immediately.
# Fractions of a cent aren't needed here, but headroom in digits is.
BigUsd = Numeric(30, 2)
Pct = Numeric(12, 4)  # percentages
