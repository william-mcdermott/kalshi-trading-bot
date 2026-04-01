# Kalshi Algorithmic Trading Bot

A live algorithmic trading bot for [Kalshi](https://kalshi.com) — a CFTC-regulated US prediction market exchange. Trades Bitcoin daily price contracts using a momentum + settlement arbitrage strategy. Built in Python as a learning project coming from a JavaScript/TypeScript background.

**Starting balance:** $100 · **Current balance:** ~$111 · **Live win rate:** 70%+ · **Trades:** 35+

---

## What It Does

Every 60 seconds the bot:

1. Fetches BTC/USDT price from Kraken via ccxt
2. Calculates momentum using linear regression on 12×5min candles
3. Confirms signal with RSI-14 to filter oversold conditions
4. Selects the best Kalshi KXBTCD market based on price and direction
5. Places a limit order if all filters pass
6. Tracks fills and P&L automatically via a background polling loop
7. Sends an iMessage alert on every trade and settlement

Two strategies run in rotation based on time to settlement:

**Momentum** (>6hrs before 5pm EDT settlement)
- SELL YES contracts when BTC has strong downward momentum
- Filters: momentum >0.3%/hr, RSI >55, daily range >$1,000, contract price >15¢
- BUY disabled — backtesting showed insufficient edge

**Settlement Arb** (<6hrs before settlement)
- Models fair value using a normal distribution of BTC price moves
- Finds mispriced contracts where market price diverges from fair value by >8¢
- Directional filter: only trades aligned with current momentum

One position at a time across all markets — the bot won't open a new trade until the current one settles at 5pm EDT.

---

## Risk Management

### Tiered Edge System
Replaces a hard daily trade cap. Each edge tier has its own quota — exceptional opportunities (>20¢ edge) are never blocked:
```python
EDGE_TIERS = [
    (0.20, float('inf'), float('inf')),  # unlimited
    (0.16, 0.20,         3),             # up to 3/day
    (0.12, 0.16,         3),             # up to 3/day
    (0.08, 0.12,         3),             # up to 3/day
]
```

### Position Sizing
All trades are $1. Small size is intentional — this is a strategy validation phase. The bot is designed to scale position size once edge is confirmed over 50+ trades.

---

## Stack

| Layer | Technology | JS Equivalent |
|-------|-----------|---------------|
| Web framework | FastAPI | Express |
| Server | Uvicorn | nodemon |
| Database | SQLite + SQLAlchemy async | Mongoose + MongoDB |
| Validation | Pydantic | Zod |
| HTTP client | httpx | axios |
| Market data | ccxt (Kraken) | — |
| Trading | kalshi-python-async | — |
| Alerts | osascript (AppleScript) | — |
| Frontend | Vanilla JS + Chart.js | — |

---

## Project Structure
```
kalshi-trading-bot/
├── backend/
│   ├── main.py                            # FastAPI app, lifespan, router registration
│   ├── app/
│   │   ├── config.py                      # Runtime-configurable strategy params
│   │   ├── bots/
│   │   │   ├── btc_threshold_strategy.py  # Momentum + RSI signal generation
│   │   │   └── settlement_arb_strategy.py # Fair-value arbitrage near settlement
│   │   ├── models/
│   │   │   ├── database.py                # SQLAlchemy table definitions
│   │   │   └── db.py                      # Async engine + session management
│   │   ├── routes/
│   │   │   ├── trades.py                  # Trade history + P&L + chart endpoints
│   │   │   ├── bots.py                    # Bot start/stop controls
│   │   │   ├── market.py                  # Live Kalshi market feed
│   │   │   ├── backtest.py                # Backtest runner + parameter sweep
│   │   │   └── settings.py                # Runtime config read/write
│   │   └── services/
│   │       ├── scheduler.py               # Bot tick loop, strategy dispatch
│   │       ├── trader.py                  # Order placement (live + dry-run mode)
│   │       ├── fill_tracker.py            # Polls fills and settlement P&L
│   │       ├── position_manager.py        # Global one-position-at-a-time mutex
│   │       └── alerter.py                 # iMessage alerts via osascript
│   ├── backtesting/
│   │   ├── backtest.py                    # Full backtest engine + parameter sweep
│   │   └── analyze.py                     # Trade outcome analysis
│   └── scripts/
│       ├── econ_monitor.py                # CPI/payrolls/Fed market monitor with iMessage alerts
│       ├── gold_scanner.py                # Gold (KXGOLDD) daily edge scanner + CSV logging
│       └── spx_scanner.py                 # S&P 500 (KXINXU) daily edge scanner + CSV logging
└── dashboard/
    ├── index.html                         # Live trading dashboard
    ├── backtest.html                      # Backtest results + parameter sweep
    ├── settings.html                      # Runtime strategy configuration
    └── src/
        ├── app.js                         # Init, clock, auto-refresh
        ├── services/api.js                # All backend HTTP calls
        ├── styles/main.css                # Dark terminal aesthetic
        └── components/
            ├── chart.js                   # Cumulative P&L chart (Chart.js)
            ├── trades.js                  # Trade log table
            ├── bots.js                    # Bot toggle controls
            ├── markets.js                 # Live Kalshi market feed
            └── toast.js                   # Toast notifications
```

---

## Setup

**Requirements:** Python 3.13+, Kalshi account with API credentials
```bash
# Backend
cd backend
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Fill in:
#   KALSHI_API_KEY_ID=your-key-id
#   KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
#   KALSHI_HOST=https://api.elections.kalshi.com/trade-api/v2
#   DRY_RUN=true   ← set to false for live trading

# Run (keep awake overnight)
caffeinate -i uvicorn main:app --reload
```
```bash
# Dashboard (separate terminal)
cd dashboard
python3.13 -m http.server 5500
# → http://localhost:5500
```

Interactive API docs at `http://localhost:8000/docs`

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/api/trades` | Trade history (`?strategy=macd&limit=50`) |
| GET | `/api/trades/summary` | Total P&L, win rate, by strategy |
| GET | `/api/trades/chart` | Cumulative P&L time series |
| GET | `/api/bots` | Bot status and statistics |
| POST | `/api/bots/start` | Start a bot (`{strategy, position_size}`) |
| POST | `/api/bots/stop/:name` | Stop a bot |
| GET | `/api/market/events` | Live Kalshi market feed |
| GET | `/api/backtest/run` | Run backtest (`?days=30`) |
| GET | `/api/settings` | Get current strategy config |
| POST | `/api/settings` | Update config at runtime — no restart needed |

---

## Backtesting

The engine fetches historical BTC/USDT candles from Kraken and simulates the full strategy across a 15-config parameter sweep (5 daily range filters × 3 momentum thresholds).
```bash
cd backend
python backtesting/backtest.py 30   # 30-day backtest
```

Or run it from the dashboard's Backtest page.

**Best config (30 days, March 2026):**

| Metric | Value |
|--------|-------|
| Momentum threshold | >0.3%/hr |
| Trades | 7 |
| Win rate | 57.1% |
| Total P&L | +$0.22 |
| Profit factor | 1.14 |
| Max drawdown | $1.24 |

Key finding: SELL trades win 4/5. BUY disabled in live bot — backtesting showed insufficient edge.

---

## Post-mortem

On day one, the bot placed 60 duplicate SELL trades in a single session. Root cause: the position manager checked for open positions per-market-ticker, but the bot was selecting a new ticker every 2 minutes as BTC moved. Each new ticker passed the duplicate check and fired another order.

Fix: changed `has_open_position(ticker)` → `has_any_open_position()`. The bot now holds one position globally until settlement at 5pm EDT, regardless of which market it's on.

Secondary bug: filled positions were being closed in the position manager the moment they filled, allowing a new trade to fire immediately. Fixed by keeping the position locked until `fill_tracker` confirms settlement — not just fill.

---

## What I Learned

Coming from a MEAN stack background, this project covered:

- Python async/await patterns — similar to JS but different event loop model
- SQLAlchemy async ORM — like Mongoose but with explicit session management
- FastAPI dependency injection — cleaner than Express middleware for typed APIs
- pandas + linear regression for time-series signal generation
- Running a live system with real money and real consequences for bugs
- The importance of position sizing and duplicate-order protection in trading systems

---

## Market Research

The bot is built to expand beyond BTC. Two scanner scripts run daily to identify edge opportunities in new markets:

### Gold (KXGOLDD)
- Settles at 5pm EDT daily, same as BTC
- Hourly vol: 0.341%/hr (calibrated from 30 days of GC=F data)
- 40 liquid markets, ~10,000 contracts/day volume
- Scanner: `python scripts/gold_scanner.py`

### S&P 500 (KXINXU)
- Settles at 4pm EDT daily (market close)
- Hourly vol: ~0.70%/hr
- 32 liquid markets, 6,000+ contracts/day and growing
- Scanner: `python scripts/spx_scanner.py`

### Economic Data Monitor
Tracks Kalshi pricing for upcoming CPI, payrolls, unemployment, and Fed decision events. Runs automatically before each release via launchd:
- `python scripts/econ_monitor.py`

---

## Author

William McDermott · [github.com/william-mcdermott](https://github.com/william-mcdermott)