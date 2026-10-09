# Crypto Hub

A crypto investor's dashboard: your Bybit and Binance portfolio in one place, market charts
with indicators, technical signals, alerts and a Telegram bot that mirrors the web panel.

The user interface is in Russian; code, comments and documentation are in English.

> **This is not financial advice.** Signals and indicators are technical analysis on
> historical data. Auto-trading is off by default and runs in paper-trading mode until
> live trading is enabled by a separate confirmation.

## Status

Work in progress.

Done: database schema and migrations, login with two-factor authentication, closed
invite-only registration, connecting Bybit and Binance keys, balance and trade sync, a
portfolio screen with valuation, allocation, value chart, open positions and unrealized
PnL, a market section with a candlestick chart, indicators, a real-time order book and
trade feed, exchange comparison, signals with justification and accuracy statistics,
alerts with editable conditions and delivery to the panel, web push and Telegram,
notification settings, the Telegram bot, a dashboard with the Fear & Greed index and
market-wide metrics, a converter and trade calculator, auto-trading with paper mode and
risk limits, and the background sync process.

Not yet verified on live data and requires your credentials: the bot against the Telegram
API (needs a token from @BotFather), auto-trading against an exchange (needs testnet
keys), certificate issuance when deploying to a real domain.

Next up: production deployment and acceptance.

## Stack

| Layer | Technology |
|---|---|
| Web panel | FastAPI + Jinja2, Alpine.js, TradingView Lightweight Charts |
| Bot | aiogram 3 |
| Database | PostgreSQL 17, SQLAlchemy 2.0 (async), Alembic |
| Exchanges | ccxt (REST + WebSocket, sandbox for testnet) |
| Indicators | pandas / numpy, own implementations of EMA, SMA, RSI, MACD, Bollinger |
| Background jobs | APScheduler in a separate process |
| P2P | Own Bybit and Binance adapters (ccxt doesn't cover P2P) |
| Notifications | Web push (VAPID + pywebpush), Telegram |
| External data | CoinGecko (market cap, dominance), alternative.me (Fear & Greed index) |
| Deployment | Docker + docker-compose |

Real-time updates work without Redis. The order book and trade feed go through a
WebSocket subscription multiplexer inside the web process - one subscription to the
exchange regardless of the number of viewers. Rare events (triggered alerts and signals
from the background process) are stored in the notifications table: an open tab picks
them up with regular polling, and web push delivers them to a closed browser. There's no
point keeping yet another inter-process channel for a few events a day.

## Running

You need Docker with the Compose plugin.

```bash
cp .env.example .env
```

Fill in at least three values in `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"          # SESSION_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
```

and `SEED_OWNER_EMAIL` / `SEED_OWNER_PASSWORD` - the first, owner account. Other users
register with an invite that the owner issues from the admin section.

```bash
docker compose up -d --build
```

The panel comes up at http://localhost:8000, the liveness check is
http://localhost:8000/healthz. Migrations are applied automatically by a separate
`migrate` service before the web app starts, and the `worker` starts polling the
exchanges at the same time.

If port 8000 is taken on your machine, change `WEB_PUBLIC_PORT` in `.env` - inside the
container the port is always 8000.

When running over plain http (no certificate), set `SESSION_SECURE_COOKIE=false`,
otherwise the browser won't send the session cookie and you won't be able to log in.

The bot only starts with a `BOT_TOKEN` from [@BotFather](https://t.me/BotFather). Without
a token the `bot` container starts, logs that fact and does nothing - the panel works
without it. After setting the token, restart it: `docker compose restart bot`.

Log in as `SEED_OWNER_EMAIL` - that's the panel owner. Other users register by invite:
the invites section is available only to the owner, who issues a link of the form
`/register?code=...`. The code is single-use, with a configurable expiry and an optional
binding to a specific email address.

Stop:

```bash
docker compose down
```

Completely, including the database data:

```bash
docker compose down -v
```

## Development without Docker

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

You need a running PostgreSQL; set `POSTGRES_HOST=localhost` in `.env`.

```bash
alembic upgrade head
uvicorn app.web.main:app --reload
pytest -q
```

Tests can also run in a container - in the same environment as the runtime, without a
local venv:

```bash
docker compose --profile test run --rm tests
```

## Deploying to a server

You need a domain pointed at the server with an A record, and ports 80 and 443 open.

```bash
git clone <repository> crypto-hub && cd crypto-hub
cp .env.example .env
```

In `.env`, in addition to the above, fill in:

```
DOMAIN=panel.example.com
PUBLIC_URL=https://panel.example.com
SESSION_SECURE_COOKIE=true
DEBUG=false
```

Change the database password and `SESSION_SECRET` to random values, and generate a new
`ENCRYPTION_KEY`.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Caddy obtains the Let's Encrypt certificate and renews it by itself - no separate certbot
or scheduled jobs needed. Only Caddy is exposed: in this configuration the panel and
database ports aren't published on the host.

Updating:

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Migrations are applied automatically by the `migrate` service before the web app starts.

Database backup:

```bash
docker compose exec -T db pg_dump -U cryptohub cryptohub > backup.sql
```

Keep `ENCRYPTION_KEY` **separately** from this backup - see below.

## Portfolio and PnL

A spot exchange doesn't return "positions" - only a balance and a trade history. So the
average entry price is built by walking the trades with cost averaging; the same pass
computes the result of every sale.

Exchanges return a limited history period, and the app says so plainly: if the balance
holds more of a coin than the trades explain, or a sale appears without a purchase, the
position is marked "partial" - its average entry price doesn't describe the whole
balance. Where money is concerned, an incomplete calculation must not be passed off as
exact.

## Time and time zone

Everything is stored in UTC and shown in the user's time zone. The zone is selected in
the Security section and applies to both the panel and the bot: a time mismatch between
the chat and the site would read as a data error.

The default is UTC. An alert's "valid until" field is also interpreted as the user's local
time, not the server's.

## Backup and restore

Three different things need saving, and they don't substitute for one another.

**Code with history.** A full snapshot of the repository in one file:

```bash
git bundle create crypto-hub.bundle --all
```

Restoring is a regular clone from the file:

```bash
git clone crypto-hub.bundle crypto-hub
```

**Secrets (`.env`).** Deliberately kept out of the repository, so they aren't in the
snapshot. Store them separately, especially `ENCRYPTION_KEY`: with a database dump but
without the key, exchange keys can't be decrypted - that's the point. The flip side is
that if the key is lost, the exchange keys have to be added again.

**Database.**

```bash
docker compose exec -T db pg_dump -U cryptohub -d cryptohub --clean --if-exists > dump.sql
```

Restoring into a running database:

```bash
docker compose exec -T db psql -U cryptohub -d cryptohub < dump.sql
```

Keep the dump and `ENCRYPTION_KEY` in different places: together they give full access
to exchange keys, separately they don't.

## Data retention

The app writes continuously, so once a day the excess is cleaned up. Candles are the most
noticeable: six timeframes per watched pair is about two thousand rows a day per pair,
i.e. several gigabytes a year across thirty pairs. Yet only the latest few hundred are
ever read.

Candles are capped by **count** per pair and timeframe (`CANDLES_KEEP_PER_SERIES`), not
by age: a week produces ten thousand one-minute candles but only seven daily ones, so a
single age limit would mean either junk in one series or an empty chart in another.

The login log, the notification feed and market-wide statistics are cleaned by age. Zero
in any of these settings means never delete. Trade history, bot orders, the bot log,
signals and portfolio snapshots are left alone: they grow slowly and are needed in full.

## Settings check on startup

The values in `.env.example` are placeholders, and all of them work: with them the panel
opens and nothing complains. That's why every process checks them on startup and
**refuses to start** if any remain:

- `SESSION_SECRET` from the example - anyone with the source code can forge a cookie
  signed with it;
- an empty `ENCRYPTION_KEY` - there's nothing to store exchange keys with;
- `SEED_OWNER_PASSWORD` from the example - the panel owner's password is public.

The refusal message says exactly what to generate. With `DEBUG=true` the same cases are
only logged: a developer shouldn't have to set up real secrets every time.

Separately, as a warning (without refusing): the example database password, and
`SESSION_SECURE_COOKIE=false` with a public address.

## Exchange keys and security

Exchange API keys are stored in the database only in encrypted form (Fernet, key from
`ENCRYPTION_KEY`) and never reach logs or templates - the UI shows only a mask. When a
key is added, its permissions are checked with a request to the exchange: trading is
allowed only if the exchange itself confirmed it, not the user in a form.

The recommended mode is **read-only keys**. Trading permission is needed only for
auto-trading and is enabled as a separate, deliberate step.

Two-factor authentication is enabled in the Security section. It turns on only after a
code from the authenticator app has been confirmed - so you can't lock yourself out by
scanning the QR code wrong. Ten single-use recovery codes are issued along with it; only
their hashes stay in the database, so they're shown exactly once.

Password guessing is limited: after ten failures in a row the address is temporarily
locked. The count starts from the last successful login, so old typos don't accumulate
and one day lock out the account owner.

Keep `ENCRYPTION_KEY` separate from database backups: with a dump but without the key,
exchange keys can't be decrypted - that's the point. The flip side is that if
`ENCRYPTION_KEY` is lost, they have to be added again.

## Telegram bot

A mirror of the web features that works through the same services - numbers in the chat
and on the site never diverge.

| Command | What it does |
|---|---|
| `/portfolio` | Balance summary broken down by exchange |
| `/price BTC` | Quote on both exchanges and the difference between them |
| `/signals` | Fresh signals for watched pairs |
| `/alert BTC > 70000` | Create a price alert |
| `/alerts` | List your alerts |
| `/unlink` | Unlink the chat from the account |

A chat is linked with a code: it's issued in the panel's Security section and sent to the
bot as a single message. The code is single-use and lives for 15 minutes.

Delivery is separate from recording the event: a triggered alert lands in the panel feed
immediately and goes to the chat on the next background pass. If the bot is unavailable
or the user blocked it, the event isn't lost and doesn't hold up the alert engine.

## P2P

The bot holds the price of an ad on the marketplace: it gets one step ahead of the
neighbour at the chosen position, but never leaves the corridor. The bot doesn't create
ads - they're created in the marketplace account - it only reprices them.

**The corridor is mandatory**, and it's the key decision in the whole feature. P2P is an
ad board, and a bot that only follows its neighbours, when it meets someone else's similar
bot, falls into an endless exchange of steps: each outbids the other until the price
becomes ruinous. The corridor is the only thing that stops this loop.

The corridor's anchor is the **middle of the board**, not the spot quote: there's no
USDT/RUB spot pair on the exchange, but any pair has a median. It's also stable - two
bots outbidding each other pull down the tail of the board but barely move the middle.

Neighbours not worth following are filtered out separately: our own ad (otherwise the bot
outbids itself), ads below a size limit and accounts with a low share of completed
trades.

Protection works like in auto-trading:

1. **Global kill switch** `P2P_ENABLED=false`. When off, nothing touches the price.
2. **Rule mode.** `observe` computes and logs but never touches the ad; `live` changes the
   price. A rule is created stopped and in observe mode.
3. **Switching to live** requires a consent checkbox in the form. Changing the mode and
   any edit to the rule always stop it: shifting the corridor of a running rule means
   immediately repricing to bounds nobody has checked.

The log covers both action and inaction: "why isn't the price moving" is asked just as
often as "why did it move". Every entry shows the neighbour's price and the middle of the
board at that moment.

The marketplace's P2P endpoints are only open to accounts with **advertiser** status
(Bybit, General Advertiser or higher) or **verified merchant** status (Binance). The
status is obtained in the marketplace account; in the panel the key sends a request with
the "Request P2P" button, and the permission is confirmed by the marketplace itself, not
by a checkbox in a form.

Orders on ads are mirrored into the panel - you can see what's going on and with whom -
but read-only.

Automatic release of crypto after the payment mark **deliberately doesn't work**. The
mark is set by the buyer, the marketplace doesn't verify it, and only the source of the
money - a bank statement or payment gateway - can confirm the funds arrived. The code
has an interface for this (`PaymentVerifier`) and a placeholder that by design can't
answer "confirmed": until a bank or gateway is named and configured, funds must be
released by a person. Plugging in a provider means replacing one function in
`services/payment_verification.py`.

## Notifications and web push

An event is recorded once and then fans out to channels: the panel feed, web push to the
browser, a Telegram message. Which ones is decided at recording time, according to the
user's settings (notifications in the Security section, or the settings button above the
feed). A single alert can't enable a channel disabled there: otherwise "don't post to
Telegram" would mean nothing.

Web push needs a VAPID key pair. Generate it:

```bash
docker compose run --rm web python -m app.services.webpush
```

Put the three lines of output into `.env`. Without them web push is simply disabled; the
feed and Telegram work as usual.

After that each user turns on the subscription - on every device separately, with a
button on the notification settings page. Browsers only allow push in a secure context:
over https or on `localhost`. Subscriptions the push service declared invalid are
removed automatically.

A push lives at the push service for five minutes: a phone locked at the moment of
sending will still get the notification, but a price that hit its level an hour ago is
no longer news.

## Auto-trading

**Off by default and does nothing.** It's enabled with the `AUTOTRADE_ENABLED=true` flag
in the environment - until then not even paper trades are executed.

Three independent layers of protection:

1. **Global kill switch** in the environment. When off, nothing runs.
2. **Strategy mode.** `paper` - trades only in the database, the exchange isn't touched at
   all; `testnet` - the exchange's test network; `live` - real money. A strategy is
   created in `paper` and stopped.
3. **Switching to live** requires both a consent checkbox in the form and a key whose
   trading permission *the exchange itself* confirmed. A testnet key isn't allowed into
   live. The confirmation time is recorded, and leaving the mode clears it.

A strategy trades spot and holds **at most one position**: a buy opens it, a sell closes
it. Selling with no open position is rejected - on spot that would be selling the user's
own coins, not closing the bot's position.

A position is closed by stop-loss, take-profit or an opposite signal. Levels are checked
on every background pass, not only on a new signal: a stop is a stop precisely because it
fires on its own. If within one interval the price touched both levels, the stop counts -
we don't know the order of events, so we assume the worst.

Plus risk limits: a share of the deposit per trade with a hard cap, and a daily loss
limit. The loss is measured as a share of the deposit, not of the position: a 2% stop on
a position worth 10% of the deposit costs 0.2%, and mixing these up would stop the bot
many times too early. When the limit is reached, the strategy stops itself, logs the
reason, sends a notification and doesn't restart automatically - a person has to lift
the stop.

The log is complete: placed orders, rejections and their reasons, stops at the limit.
The bot does nothing unexplained.

Changing the mode always stops the strategy: starting it is a separate, deliberate
action.

The order amount is rounded to the exchange's lot step (it won't accept the result of a
division with twenty decimal places), and a trade below the minimum lot is rejected in
all modes, including paper: it wouldn't have happened on the exchange, and recording it
in the paper result would promise profit that won't materialize. When closing, the
amount is capped at the free balance - the exchange often takes the fee in the coin
itself, and an order for the full amount would return "insufficient funds", leaving the
position open.

## Structure

```
app/
  config.py            settings from the environment
  db.py                SQLAlchemy engine and sessions
  models/              ORM models by domain
  exchanges/           Bybit and Binance adapters on top of ccxt, WS multiplexer
  providers/           CoinGecko, Fear & Greed index
  services/            business logic shared by the web panel and the bot
  web/                 web panel: routers, templates, static files
                       (the owner section - invites - lives here too,
                       under /admin)
  bot/                 Telegram bot
  worker/              background jobs (sync, signals, alerts, delivery)
alembic/               migrations
docs/ARCHITECTURE.md   database schema and architecture decisions
tests/                 tests
```

## Deviations from the typical stack

**bcrypt is used directly, without passlib.** passlib hasn't had a release since 2020 and
is incompatible with bcrypt 4.1+, so it would drag in a noticeably outdated password
hashing version. For a product that stores access to exchange accounts, that's an
unjustified compromise.

## License

MIT - see [LICENSE](LICENSE).
