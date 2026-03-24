# scripts/seed_fake_data.py
#
# Populates the database with fake trades so you can build and test
# the frontend dashboard without needing real Polymarket data.
#
# Run from the project root with:
#   python scripts/seed_fake_data.py

import asyncio
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Resolve the project root and add it to sys.path before any app imports.
# This works regardless of where Python is invoked from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.models.db import init_db, SessionLocal
from app.models.database import Trade, BotStatus
from sqlalchemy import select


STRATEGIES = ["macd", "rsi", "cvd"]
MARKET_IDS = ["market_abc123", "market_def456", "market_ghi789"]


async def seed():
    print(f"Project root: {PROJECT_ROOT}")
    print("Initialising database...")
    await init_db()

    async with SessionLocal() as db:

        for strategy in STRATEGIES:
            bot = BotStatus(
                strategy=strategy,
                is_running=False,
                position_size=1.0,
                total_trades=0,
                total_pnl=0.0,
            )
            db.add(bot)

        now = datetime.utcnow()
        total_pnl    = {s: 0.0 for s in STRATEGIES}
        total_trades = {s: 0    for s in STRATEGIES}

        for i in range(60):
            strategy = random.choice(STRATEGIES)
            side     = random.choice(["BUY", "SELL"])
            price    = round(random.uniform(0.3, 0.75), 3)
            size     = 1.0
            filled   = random.random() > 0.15
            pnl      = round(random.uniform(-0.15, 0.25), 4) if filled else 0.0
            created  = now - timedelta(hours=random.randint(0, 168))

            trade = Trade(
                strategy=strategy,
                market_id=random.choice(MARKET_IDS),
                side=side,
                price=price,
                size=size,
                filled=filled,
                pnl=pnl,
                order_id=f"order_{i:04d}",
                created_at=created,
            )
            db.add(trade)

            total_pnl[strategy]    += pnl
            total_trades[strategy] += 1

        result = await db.execute(select(BotStatus))
        bots = result.scalars().all()
        for bot in bots:
            bot.total_trades = total_trades[bot.strategy]
            bot.total_pnl    = round(total_pnl[bot.strategy], 4)

        await db.commit()
        print(f"Seeded 60 trades across {len(STRATEGIES)} strategies.")
        print("Start the server with: uvicorn main:app --reload")


if __name__ == "__main__":
    asyncio.run(seed())
