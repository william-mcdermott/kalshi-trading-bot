# Architecture

A deep dive into how the system is designed and why.

## System overview

```
┌─────────────────┐     REST API      ┌─────────────────────────────────────┐
│   Dashboard     │ ◄────────────────► │           FastAPI Backend           │
│  (Vanilla JS)   │                   │                                     │
└─────────────────┘                   │  ┌──────────┐  ┌────────────────┐  │
                                      │  │ Scheduler│  │  Fill Tracker  │  │
                                      │  │  (async) │  │    (async)     │  │
                                      │  └────┬─────┘  └───────┬────────┘  │
                                      │       │                 │           │
                                      │  ┌────▼─────────────────▼────────┐  │
                                      │  │        Position Manager        │  │
                                      │  └────────────────┬──────────────┘  │
                                      │                   │                 │
                                      │  ┌────────────────▼──────────────┐  │
                                      │  │         SQLite Database        │  │
                                      │  └───────────────────────────────┘  │
                                      └──────────┬──────────────────────────┘
                                                 │
                          ┌──────────────────────┼──────────────────┐
                          │                      │                  │
                   ┌──────▼──────┐      ┌────────▼──────┐  ┌───────▼──────┐
                   │   Kalshi    │      │    Kraken     │  │   Kalshi     │
                   │  CLOB API   │      │  Market Data  │  │ Market Data  │
                   │  (trading)  │      │  (BTC price)  │  │  (markets)   │
                   └─────────────┘      └───────────────┘  └──────────────┘
```

## Core design decisions

### Why FastAPI over Flask or Django?

FastAPI is async-native, which matters here because the bot needs to:
- Poll Kalshi every 60 seconds
- Poll Kraken for BTC prices
- Serve the dashboard API concurrently
- Track order fills every 2 minutes

With Flask (sync), each of these would block the others. FastAPI with asyncio lets all of them run concurrently on a single thread without blocking.

FastAPI also generates interactive API docs automatically — useful for debugging and demonstrating the system.

### Why SQLite over PostgreSQL?

SQLite is zero-config and file-based. For a single-machine trading bot that writes a few hundred rows per day, PostgreSQL would be over-engineering. SQLite with aiosqlite handles the async requirements cleanly.

If the system scaled to multiple machines or strategies, migrating to PostgreSQL would be straightforward — SQLAlchemy abstracts the difference.

### Why vanilla JS for the dashboard?

No build step. No npm. No webpack config. The dashboard is a static folder served by `python -m http.server` — it can be opened directly in a browser. This keeps the development loop fast and the deployment simple.

For a production system with multiple engineers, React or Angular would make sense. For a solo project where the backend is the interesting part, vanilla JS gets out of the way.

## The trading loop

The scheduler runs one `bot_loop` coroutine per strategy. Each loop:

```
while True:
    1. Sync positions with Kalshi (every 2 min)
    2. Re-pick best market (every 15 min)
    3. Check for existing open position → skip if exists
    4. Fetch BTC price history from Kraken (12 x 5-min candles)
    5. Calculate momentum via linear regression
    6. Fetch contract price from Kalshi candlesticks
    7. Generate signal (BUY / SELL / HOLD)
    8. If signal: place limit order via Kalshi API
    9. Record position in PositionManager
    10. Sleep 60 seconds
```

Multiple strategies run concurrently via `asyncio.gather()` — the equivalent of `Promise.all()` in JavaScript.

## The strategy

### Market selection

Kalshi Bitcoin daily markets price the probability that BTC will be above a threshold at 5pm EDT. For example, `KXBTCD-26MAR2517-T71399.99` prices the probability that BTC is above $71,399.99 at 5pm on March 25.

The bot selects a market $800 above (if bullish) or below (if bearish) the current BTC price. This targets contracts with genuine uncertainty — priced around 20-50¢ — rather than contracts that have already effectively resolved.

Markets are refreshed every 15 minutes as BTC price moves.

### Signal generation

Momentum is calculated using linear regression on 12 x 5-minute BTC candles from Kraken (1 hour of data):

```python
slope_per_candle = Σ((i - mean_x)(price_i - mean_y)) / Σ((i - mean_x)²)
momentum_pct_per_hour = (slope_per_candle * 12 / first_price) * 100
```

Linear regression is more robust than simple percentage change because it uses all 12 data points rather than just the first and last, reducing sensitivity to noise.

Signal rules:
- `momentum > 0.2%/hr` and contract `< 0.40` → BUY YES (bullish)
- `momentum < -0.2%/hr` and contract `> 0.15` → SELL YES (bearish)
- Contract `>= 0.95` or `<= 0.05` → HOLD (already resolved)
- Otherwise → HOLD

### Position management

The `PositionManager` singleton prevents duplicate orders:
- Maintains an in-memory dict of `market_ticker → Trade`
- Loaded from database on startup (survives restarts)
- Checked before every order placement
- Stale orders (unfilled after 30 min) are automatically cancelled via the Kalshi API

### P&L tracking

Kalshi doesn't push fill notifications — the system polls. The `FillTracker` runs every 2 minutes:

1. Queries the database for unfilled trades with real order IDs
2. Calls `client.get_order()` for each
3. If `fill_count_fp > 0`: marks as filled, records cost
4. Calls `client.get_fills()` for settlement payout data
5. Calculates: `pnl = payout - cost`

## Data flow: placing an order

```
Scheduler tick
  → fetch_btc_history()     [Kraken API]
  → calculate_momentum()    [pure Python]
  → find_best_market()      [Kalshi REST API]
  → get_contract_price()    [Kalshi candlesticks API]
  → generate_signal()       [pure Python]
  → position_manager.has_open_position()  [in-memory]
  → place_order()           [Kalshi CLOB API]
  → db.add(Trade)           [SQLite via SQLAlchemy]
  → position_manager.record_order()  [in-memory]
```

## Data flow: a request from the dashboard

```
GET /api/trades?strategy=macd
  → FastAPI router (trades.py)
  → Depends(get_db)         [injects async DB session]
  → SELECT * FROM trades WHERE strategy = 'macd'
  → serialize to JSON
  → response
```

The `Depends(get_db)` pattern is FastAPI's dependency injection — equivalent to Angular's `@Injectable()` services. Each request gets its own database session, automatically closed when the response is sent.

## What I'd do differently at scale

- **Message queue** — replace the polling fill tracker with a webhook or WebSocket subscription if Kalshi adds support
- **PostgreSQL** — for multiple machines or strategies sharing state
- **Redis** — for the position manager cache instead of in-memory dict (survives crashes)
- **Docker** — containerize the backend for consistent deployment
- **Backtesting framework** — test strategies against historical data before live trading
- **Multiple strategies** — RSI, CVD, and mean-reversion strategies are scaffolded but not yet implemented
- **Alerting** — email or SMS when large trades fire or drawdown exceeds threshold
