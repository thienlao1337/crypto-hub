# Crypto Hub architecture

This document describes how the system is built and why the decisions were made. The
database schema is in section 3, the module map in section 4.

## 1. What the system is

A personal terminal for a crypto investor: it aggregates the portfolio from Bybit and
Binance, shows market charts with indicators, computes technical signals, sends alerts to
the web panel and Telegram, and optionally trades according to a strategy.

Four processes on top of one database:

| Process | Role |
|---|---|
| `web` | The web panel and its WebSocket channels. The core value of the product. |
| `bot` | Telegram bot - a mirror of the web features and a notification delivery channel. |
| `worker` | Background jobs: exchange sync, candles, signals, alerts, auto-trading. |
| `db` | PostgreSQL - the only shared state. |

Business logic lives in `app/services/` and is called from all three application
processes. Routers and handlers are thin: they take input, validate it and call a
service.

## 2. Key decisions

### 2.1 Real-time without a message broker

Data streams are split by frequency, and each has its own delivery method.

**High-frequency** - the order book and trade feed. A WebSocket subscription multiplexer
inside `web`: one subscription to the exchange is kept per pair and channel regardless of
the number of open tabs, and clients connect to our WS and receive a broadcast.
WebSocket is unavoidable here - updates come several times a second.

**Rare** - a triggered alert or a new signal. They originate in `worker` and are stored in
`notifications`. From there, two independent paths: an open tab picks them up with
regular polling every thirty seconds, and a closed browser gets a web push. There's no
point keeping another inter-process channel for a few events a day: polling is cheaper
and easier to maintain, and push goes outside anyway, to the browser's push service.

Redis was deliberately not introduced. It would be needed to share state between several
`web` instances, but at this product's load profile (dozens of users) one process copes
fine, and an extra container is one more thing that can break in the client's
production.

If events become frequent or there are more `web` instances, the extension point is
known: counter polling is replaced with PostgreSQL `LISTEN/NOTIFY` or pub/sub, and the
services don't change - they already only write to `notifications`.

### 2.2 One client for two exchanges

Bybit and Binance are connected via `ccxt`: unified symbols, one interface for balances,
orders and candles, a ready-made sandbox mode for testnet. The alternative - each
exchange's native SDK - would mean two different data models and double work in every
feature.

The same pair on different exchanges is **two rows** in `markets`. That's how the
exchange comparison from the spec works: price and spread differences are computed
between two rows, not within one.

### 2.3 Money only in Numeric

All quantities, prices and valuations are `Numeric`, never `float`. Accumulated rounding
error on balances and PnL produces a mismatch with the exchange report that can't be
explained to the user. Precisions are defined in one place, `app/models/types.py`.

### 2.4 History instead of overwriting

Significant events are written as separate rows: `login_events`, `audit_log`,
`alert_triggers`, `signal_outcomes`, `bot_journal`, `portfolio_snapshots`. Only "now"
snapshots are overwritten - `balances`, `positions`, `market_tickers` - and each of them
has its history kept in its own table.

A portfolio snapshot stores the whole `breakdown`, so last month's chart isn't
recomputed retroactively at today's prices.

### 2.5 Reference data in the database

Edited from the admin panel rather than hard-coded as enums: `exchanges`, `timeframes`,
`alert_types`. User roles (`owner` / `user`) are deliberately left as a string in code -
they are access levels tied to checks, not business reference data.

Indicator parameters in `signal_rules.config` are stored as a JSON dict: per the spec,
the indicator set must grow without schema migrations.

## 3. Database schema

### Users and access

| Table | Purpose |
|---|---|
| `users` | Account, role, 2FA secret, Telegram link, preferences |
| `user_recovery_codes` | Single-use codes in case the 2FA device is lost (stored as hashes) |
| `invites` | Invites: registration is closed, codes are issued by the owner |
| `login_events` | Login history, including failed attempts |
| `audit_log` | Actions on significant entities: keys, permissions, strategy modes |

### Exchanges

| Table | Purpose |
|---|---|
| `exchanges` | Exchange reference table |
| `exchange_accounts` | A connected key: encrypted key/secret, mask, check status, permissions |

`exchange_accounts` has two permission flags: `requested_trading` - what the user asked
for, `allow_trading` - what the exchange confirmed when the key was checked. Trading is
possible only when both are true and the key status is `ok`.

### Market

| Table | Purpose |
|---|---|
| `assets` | A coin on its own, outside any pair or exchange |
| `markets` | A trading pair on a specific exchange |
| `timeframes` | Timeframe reference table |
| `candles` | OHLCV; only for pairs from watchlists and signal rules |
| `market_tickers` | "Now" snapshot per pair: last/bid/ask, 24h change |
| `global_stats` | Snapshots of market cap, dominance and the Fear & Greed index |

`market_tickers` exists so the bot and the alert engine don't hit the exchange on every
request. `global_stats` is kept as history because the index source only returns the
current value, while the widget needs the trend.

### Portfolio

| Table | Purpose |
|---|---|
| `balances` | Current balance of a coin on an exchange account |
| `portfolio_snapshots` | Portfolio value chart points over time |
| `trades` | Executed trades pulled from the exchange history |
| `positions` | Open positions and unrealized PnL |
| `watchlist_items` | Personal watchlist |

Uniqueness of `trades` on the pair (account, external trade id) makes re-syncing
idempotent.

The average entry price for spot is built from `trades` - walking from the oldest trades
to the newest with cost averaging. The same pass fills `trades.realized_pnl` for sales.
The recalculation is always full, never incremental: a catch-up sync brings in
backdated trades, and a single one changes the average price of the whole chain after
it.

Exchanges return a limited history period, so a position has a `cost_basis_complete`
flag. It is cleared in two cases: a sale appeared without a purchase, or the balance
holds noticeably more of the coin than the trades explain. In both cases the UI shows the
number with a caveat instead of passing off an approximation as exact.

Revaluation (`mark_price`, `unrealized_pnl`) happens together with the quote refresh,
not as a separate job: the entry price and the "now" price must come from the same
snapshot, otherwise PnL shows the difference between different moments in time.

### Signals

| Table | Purpose |
|---|---|
| `signal_rules` | A rule: pair, timeframe, indicator parameters, scoring horizon |
| `signals` | A fired signal: direction, price, justification, indicator values |
| `signal_outcomes` | What happened to the price after the horizon - material for accuracy statistics |

`signals.reason` stores a human-readable explanation, `indicators` the values at the
moment it fired. Per the spec, a signal card must explain why it appeared, not just say
"buy".

`candle_time` protects against issuing the same signal again on the engine's next pass.

### Alerts and notifications

| Table | Purpose |
|---|---|
| `alert_types` | Reference table: price above/below, % change, RSI |
| `alerts` | Condition, delivery channels, cooldown, trigger limits |
| `alert_triggers` | Trigger events and delivery results |
| `notifications` | The panel feed and the delivery queue for other channels |
| `notification_settings` | What to send and where |
| `push_subscriptions` | Browser web-push subscriptions, one per device |

`cooldown_seconds` is mandatory: without it a "price above X" alert would fire on every
check while the price stays above the level. `trigger_limit` and `expires_at` limit the
alert's lifetime: the limit is counted over all time, not from the last edit, because
`trigger_count` matches the rows in `alert_triggers`.

Time is stored in UTC everywhere and shown in the user's time zone (`users.timezone`).
The conversion lives in `services/localtime.py`, shared by the panel and the bot: two
separate implementations would drift apart, and the time in the chat would stop matching
the time on the site.

`notifications` is both a feed and a queue: `show_web` says whether to show the row in
the panel, `delivered_telegram` and `delivered_push` whether it went out to the
corresponding channel. The channel decision is made once, at recording time: if every
sender re-checked the settings on its own, "disabled" would mean different things in
different places. Web push is tied to `show_web` - it isn't a separate event, but a way
to bring to the browser what would have landed in the feed anyway.

### Auto-trading

| Table | Purpose |
|---|---|
| `strategies` | Signal -> position size -> SL/TP, mode, risk limits |
| `bot_orders` | Bot positions: entry, exit levels, exit price, result |
| `bot_journal` | Log of all bot actions |
| `risk_state` | Daily result and the stop flag for the loss limit |

One `bot_orders` row describes the whole position: opening creates the row, closing
fills in its `close_price`, `realized_pnl` and `closed_at`. Entry and exit as two rows
would have to be stitched back together every time they're shown.

A strategy holds at most one position - it trades spot, where selling with no open
position means selling the user's own coins. Exits happen on stop-loss, take-profit or an
opposite signal; levels are checked on every background pass, not only on a new signal.

A trade result goes into `risk_state` as a share of the deposit, not of the position: the
daily loss limit in the spec is a share of the deposit, and a 2% stop on a position worth
10% costs 0.2%.

The mode (`paper` / `testnet` / `live`) is recorded on each order separately: the
strategy's mode may change later, and the log must show what the trade was at the moment
of execution.

Switching a strategy to `live` requires a recorded `live_confirmed_at` - the time of
explicit confirmation. A single checkbox isn't enough for real money.

### P2P

| Table | Purpose |
|---|---|
| `p2p_ads` | Mirror of our ads: price, amount, limits, state |
| `p2p_price_rules` | A rule: position in the list, outbid step, corridor, neighbour filters |
| `p2p_price_events` | What the rule decided and why - including the decision not to move |
| `p2p_orders` | Orders on ads (for now only a mirror of their state) |

P2P permission is a separate pair of flags on the exchange key (`requested_p2p`,
`allow_p2p`), on the same principle as trading permission: what the user asked for and
what the marketplace confirmed. Separate because P2P endpoints are closed until
advertiser or merchant status is granted, and a key with spot trading permission gives
no access to ads.

Price calculation lives in `services/p2p_pricing.py` and knows nothing about the
database or the network - just like the indicators. The corridor's anchor is the board
median: there's no USDT/RUB spot pair, but a median always exists and is resistant to
someone else's bot pulling down the tail of the board.

`ccxt` doesn't fit P2P: it's an ad board with its own endpoints, its own signing and its
own data model, so `exchanges/p2p/` has its own protocol and one implementation per
marketplace. Response parsing is deliberately strict - if the price field is missing, an
error naming it is raised: the bot moves the price based on these numbers, and a silent
zero is worse than a failure.

## 4. Module map

```
app/
  config.py            settings from the environment (pydantic-settings)
  db.py                lazy engine, session factory, Base
  models/              ORM by domain + types.py with shared column types
  exchanges/
    base.py            common adapter interface and data types
    ccxt_client.py     REST on top of ccxt.async_support
    ws_hub.py          WebSocket subscription multiplexer
  providers/
    coingecko.py       market cap and dominance
    fear_greed.py      Fear & Greed index
  services/
    security.py        Fernet, bcrypt, TOTP
    user_service.py    accounts, login, 2FA, Telegram linking
    invite_service.py  invites
    audit_service.py   action and login log
    exchange_keys_service.py   exchange keys
    market_service.py  pairs, quotes, dollar valuation
    candle_service.py  candles and indicator series for the chart
    portfolio_service.py       balances, valuation, snapshots, trades
    position_service.py        average entry price, unrealized PnL
    indicators.py      EMA, SMA, RSI, MACD, Bollinger - pure functions
    signal_service.py  rules, signals, accuracy statistics
    alert_service.py   conditions, triggers, delivery to the feed
    notification_service.py    notification feed and channel selection
    webpush.py         browser subscriptions and sending web push
    watchlist_service.py       watchlist
    dashboard_service.py       home screen data
    tools_service.py   converter and trade calculator
    autotrade_service.py       strategies, execution, risk limits
    p2p_pricing.py     ad price calculation - pure functions
    p2p_service.py     P2P ads, rules, log
  web/
    main.py            application, middleware, error handlers
    auth.py            session, CSRF, access checks
    flash.py           messages between requests
    templates_env.py   Jinja2, formatting filters, static version
    routers/           panel screens
  bot/
    main.py            startup and dispatcher
    middlewares.py     DB session, user, error handling
    formatting.py      command parsing and reply formatting
    handlers/          commands
  worker/
    main.py            APScheduler scheduler
    tasks.py           sync, candles, signals, alerts, auto-trading
    delivery.py        sending notifications to Telegram
```

Indicators are implemented as our own pandas functions instead of a ready-made library:
it's about a hundred lines, but without a dependency that regularly lags behind new
pandas versions, and with unit tests on reference values.

## 5. Background jobs

| Job | Default interval | What it does |
|---|---|---|
| `sync_balances` | 60 s | Balances for all active keys |
| `sync_trades` | 5 min | Backfill of new trade history |
| `poll_candles` | 30 s | Candles for pairs from watchlists and rules |
| `evaluate_alerts` | 15 s | Condition checks, delivery, recording triggers |
| `evaluate_signals` | 60 s | Indicator calculation, issuing signals |
| `snapshot_portfolio` | 15 min | A point on the value chart |
| `refresh_global_stats` | 5 min | CoinGecko and the Fear & Greed index |
| `evaluate_outcomes` | 15 min | Scoring signals once their horizon has passed |
| `autotrade_loop` | 60 s | Running strategies (only with the kill switch on) |

Intervals are configured via the environment. External sources are called with caching
and exponential backoff on a 429 response - the public tiers of CoinGecko and the
exchanges have strict limits.

## 6. Security model

- **Exchange keys** are encrypted with the Fernet key from `ENCRYPTION_KEY`. They are
  never logged or passed to templates in plain text: the UI only gets `api_key_masked`.
  A database dump without `ENCRYPTION_KEY` is useless for accessing exchange accounts -
  which is why the key is stored separately from backups.
- **Key permissions** are verified with the exchange, not taken from the user's word.
  Read-only mode is the recommended default.
- **Passwords** - bcrypt directly, without passlib (rationale in the README).
- **2FA** - TOTP; the secret is encrypted the same way as exchange keys; recovery codes
  are stored as hashes and invalidated on use.
- **Data isolation** between users is done at the service level, not in routers: a
  `user_id` filter in the query, not a check in the template.
- **Sessions** - a signed cookie (Starlette SessionMiddleware), `https_only` outside
  debug mode.
- **Auto-trading** is disabled by a global kill switch and starts in `paper`; live trades
  require both a consent checkbox and trading permission confirmed by the exchange
  itself; a testnet key isn't allowed into live. The daily loss limit stops the strategy
  automatically, and only a person can lift the stop.

## 7. Lessons learned in practice

A few things came up during development and cost some debugging - they're written down
here so we don't step on them again.

**Don't touch loaded objects after `session.rollback()`.** A rollback marks them as
expired, and accessing any field - down to the `id` in a log line - triggers a SELECT
from synchronous code, which fails with `MissingGreenlet` in async SQLAlchemy. That's why
forms redirect after an error instead of re-rendering, and background jobs take their
own session for each unit of work.

**Tests run on PostgreSQL, not SQLite.** SQLite stores the `Numeric` type as float, and
`1234.56` is read back as `1234.559999999999945430`. For a product that handles money,
such a test bed is useless.

**`now()` in PostgreSQL is the transaction start time.** Events from one request get the
same timestamp, so time cutoffs within a transaction are ambiguous; where order matters,
the id is used.

**Type precision is checked on live data.** The total market cap didn't fit into
`Numeric(20, 8)` - the overflow only surfaced on the first real request to CoinGecko.

## 8. Implementation order

It followed the priorities of the spec: useful and safe things first.

1. Skeleton: config, database, migrations, Docker - **done**
2. Authentication, invites, 2FA, reference data - **done**
3. Exchange connections, sync, portfolio - **done**
4. Market: candles, chart, indicators, order book, trade feed, comparison - **done**
5. Signals, alerts, worker - **done**
6. Telegram bot - **done**
7. Tools and dashboard - **done**
8. Auto-trading - **done**
9. Production deployment and acceptance - **deployment ready, acceptance is up to the client**
