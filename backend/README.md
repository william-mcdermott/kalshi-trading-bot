# Backend

FastAPI + SQLAlchemy async backend. Runs the bot, exposes the REST API, manages the database.

## Running
```bash
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Keep awake overnight:
```bash
caffeinate -i uvicorn main:app --reload
```

## Environment Variables
```env
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
KALSHI_HOST=https://api.elections.kalshi.com/trade-api/v2
DRY_RUN=true
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
```

Set `DRY_RUN=false` for live trading.

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | App entry point, router registration, lifespan |
| `app/config.py` | All tunable strategy params — edit at runtime via `/api/settings` |
| `app/bots/btc_threshold_strategy.py` | Momentum + RSI signal generation |
| `app/bots/settlement_arb_strategy.py` | Fair-value arb near settlement |
| `app/services/scheduler.py` | 60s tick loop, strategy dispatch |
| `app/services/fill_tracker.py` | Polls Kalshi for fills + settlement P&L |
| `app/services/position_manager.py` | Global mutex — one position at a time |
| `app/services/alerter.py` | iMessage alerts via osascript |
| `backtesting/backtest.py` | Full backtest engine + 15-config parameter sweep |

## Running the Backtest
```bash
python backtesting/backtest.py 30   # 30-day lookback
python backtesting/analyze.py       # Analyze trade outcomes
```

Or hit `GET /api/backtest/run?days=30` from the dashboard.

## Database

SQLite at `data/bot.db`. To reset trade history:
```bash
python -c "
import asyncio
from app.models.db import SessionLocal, init_db
from app.models.database import Trade
from sqlalchemy import delete

async def reset():
    await init_db()
    async with SessionLocal() as db:
        await db.execute(delete(Trade))
        await db.commit()
        print('Done')

asyncio.run(reset())
"
```

## API Docs

Interactive Swagger UI at `http://localhost:8000/docs`