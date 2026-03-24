# app/routes/bots.py
#
# Bot control endpoints — start, stop, and get status.
# The frontend calls these when the user clicks "Start Bot" or "Stop Bot".

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import datetime

from app.models.db import get_db
from app.models.database import BotStatus

router = APIRouter()

VALID_STRATEGIES = ["macd", "rsi", "cvd"]


# Pydantic models define the shape of request/response bodies.
# Think of them like TypeScript interfaces.
class StartBotRequest(BaseModel):
    strategy:      str
    position_size: float = 1.0   # dollars per trade


@router.get("/")
async def get_all_bots(db: AsyncSession = Depends(get_db)):
    """Returns the current status of all bots."""
    result = await db.execute(select(BotStatus))
    bots = result.scalars().all()

    return [
        {
            "strategy":      b.strategy,
            "is_running":    b.is_running,
            "position_size": b.position_size,
            "total_trades":  b.total_trades,
            "total_pnl":     round(b.total_pnl, 4),
            "updated_at":    b.updated_at.isoformat() if b.updated_at else None,
        }
        for b in bots
    ]


@router.post("/start")
async def start_bot(body: StartBotRequest, db: AsyncSession = Depends(get_db)):
    """
    Starts a bot for the given strategy.

    In a real implementation this would launch a background task.
    For now it just updates the status in the DB so the dashboard can show it.
    """
    if body.strategy not in VALID_STRATEGIES:
        # HTTPException works just like res.status(400).json({error: ...}) in Express
        raise HTTPException(status_code=400, detail=f"Unknown strategy. Use one of: {VALID_STRATEGIES}")

    # Get existing record or create a new one
    result = await db.execute(
        select(BotStatus).where(BotStatus.strategy == body.strategy)
    )
    bot = result.scalar_one_or_none()

    if bot is None:
        bot = BotStatus(strategy=body.strategy)
        db.add(bot)

    bot.is_running    = True
    bot.position_size = body.position_size
    bot.updated_at    = datetime.utcnow()

    await db.commit()

    return {"message": f"{body.strategy} bot started", "position_size": body.position_size}


@router.post("/stop/{strategy}")
async def stop_bot(strategy: str, db: AsyncSession = Depends(get_db)):
    """Stops a running bot."""
    result = await db.execute(
        select(BotStatus).where(BotStatus.strategy == strategy)
    )
    bot = result.scalar_one_or_none()

    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")

    bot.is_running = False
    bot.updated_at = datetime.utcnow()
    await db.commit()

    return {"message": f"{strategy} bot stopped"}
