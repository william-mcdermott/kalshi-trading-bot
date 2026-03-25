# Kalshi Algorithmic Trading Bot

A full-stack algorithmic trading system that places real limit orders on [Kalshi](https://kalshi.com) — a CFTC-regulated US prediction market exchange. Built in Python and vanilla JavaScript over two days as a learning project to expand beyond a JavaScript/TypeScript background.

![Dashboard](dashboard/screenshot.png)

## Live results

- **+$3.86 profit** on first day of live trading
- **11 trades** placed and tracked automatically
- Running 24/7 on a MacBook, making real trades on a regulated exchange

## What it does

Every 60 seconds the bot:

1. Fetches Bitcoin's real-time price from Kraken
2. Calculates hourly momentum using linear regression on 5-minute candles
3. Auto-selects the best Kalshi BTC market based on current price and momentum direction
4. Places a limit order if momentum exceeds the minimum threshold
5. Tracks fills and calculates P&L automatically

The strategy: Bitcoin daily markets on Kalshi price the probability that BTC will be above a threshold at settlement. If BTC has strong downward momentum, the bot sells YES contracts on markets where BTC is above the threshold — collecting premium on contracts likely to resolve NO.

## Tech stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Backend | Python, FastAPI | Async-first, automatic API docs, clean DI system |
| Scheduler | asyncio | Concurrent bot loops without threading complexity |
| Database | SQLite + SQLAlchemy | Zero-config local persistence, async ORM |
| Trading | Kalshi Python async SDK | Official CFTC-regulated exchange API |
| Market data | ccxt (Kraken) | Real-time BTC price and OHLCV candles |
| Frontend | Vanilla JS, Chart.js | No build step, fast to iterate |

## Project structure

```
kalshi-trading-bot/
├── backend/
│   ├── main.py                          # FastAPI app + lifespan management
│   ├── app/
│   │   ├── bots/
│   │   │   ├── btc_threshold_strategy.py  # Core trading strategy
│   │   │   ├── macd_strategy.py           # MACD signal generation
│   │   │   └── indicators.py              # Pure pandas: MACD, RSI, VWAP
│   │   ├── models/
│   │   │   ├── database.py                # SQLAlchemy table definitions
│   │   │   └── db.py                      # Async engine + session management
│   │   ├── routes/
│   │   │   ├── trades.py                  # Trade history + P&L endpoints
│   │   │   ├── bots.py                    # Bot start/stop controls
│   │   │   └── market.py                  # Live Kalshi market data
│   │   └── services/
│   │       ├── scheduler.py               # Bot execution loops (asyncio)
│   │       ├── trader.py                  # Kalshi order placement
│   │       ├── fill_tracker.py            # Polls for fills + settlements
│   │       └── position_manager.py        # Prevents duplicate orders
│   └── tests/
│       └── test_macd_strategy.py
└── dashboard/
    ├── index.html
    └── src/
        ├── components/                    # Chart, trade table, bot cards
        └── services/api.js               # All backend API calls
```

## Setup

**Requirements:** Python 3.13+, a Kalshi account with API credentials

```bash
# Backend
cd backend
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install greenlet kalshi_python_async

# Configure
cp .env.example .env
# Add your Kalshi API key ID and private key path

# Run
uvicorn main:app --reload
```

```bash
# Dashboard (separate terminal)
cd dashboard
python3.13 -m http.server 5500
# Open http://localhost:5500
```

Interactive API docs at `http://localhost:8000/docs`

## Environment variables

```env
KALSHI_API_KEY_ID=your-api-key-id
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
KALSHI_HOST=https://api.elections.kalshi.com/trade-api/v2
DRY_RUN=true
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
DEFAULT_POSITION_SIZE=1.0
```

Set `DRY_RUN=false` to place real orders.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/trades` | Trade history, filterable by strategy |
| GET | `/api/trades/summary` | Aggregate P&L and win rate |
| GET | `/api/bots` | Bot status and statistics |
| POST | `/api/bots/start` | Start a bot with position size |
| POST | `/api/bots/stop/{strategy}` | Stop a running bot |
| GET | `/api/market/events` | Live Kalshi markets |

## What I learned

This project was built to expand beyond a JavaScript/TypeScript background. Key things learned:

- Python async/await patterns vs JavaScript (similar concepts, different ecosystem)
- SQLAlchemy async ORM (vs Mongoose in Node)
- FastAPI dependency injection (vs Express middleware)
- pandas for time-series data manipulation
- Real-world API integration with authentication, rate limiting, and error handling
- Running a live system with real money on the line

## Author

William McDermott · [github.com/william-mcdermott](https://github.com/william-mcdermott) · [w.e.mcdermott@gmail.com](mailto:w.e.mcdermott@gmail.com)
