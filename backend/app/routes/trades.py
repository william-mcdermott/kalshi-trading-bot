# app/routes/trades.py
#
# Trade history endpoints.
# These feed the dashboard's trade table and P&L chart.
#
# Pattern: router = APIRouter() is like express.Router()
# Then @router.get("/") is like router.get("/", handler)

from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.models.db import get_db
from app.models.database import Trade

router = APIRouter()


@router.get("/")
async def get_trades(
    strategy: Optional[str] = Query(None, description="Filter by strategy: macd, rsi, cvd"),
    limit:    int            = Query(50,   description="Max number of trades to return"),
    db:       AsyncSession   = Depends(get_db),
):
    """
    Returns recent trades, optionally filtered by strategy.

    Example calls from your frontend:
      GET /api/trades              → all trades
      GET /api/trades?strategy=macd → only MACD trades
      GET /api/trades?limit=10     → last 10 trades
    """
    query = select(Trade).order_by(desc(Trade.created_at)).limit(limit)

    if strategy:
        query = query.where(Trade.strategy == strategy)

    result = await db.execute(query)
    trades = result.scalars().all()

    # Convert SQLAlchemy objects to dicts for JSON serialization
    # (FastAPI can handle this automatically with Pydantic schemas,
    # but keeping it simple while you're learning)
    return [
        {
            "id":         t.id,
            "strategy":   t.strategy,
            "market_id":  t.market_id,
            "side":       t.side,
            "price":      t.price,
            "size":       t.size,
            "filled":     t.filled,
            "pnl":        t.pnl,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in trades
    ]


@router.get("/summary")
async def get_summary(db: AsyncSession = Depends(get_db)):
    """
    Returns overall P&L summary across all strategies.
    Powers the summary cards at the top of the dashboard.
    """
    result = await db.execute(select(Trade))
    trades = result.scalars().all()

    total_pnl    = sum(t.pnl for t in trades)
    total_trades = len(trades)
    wins         = sum(1 for t in trades if t.pnl > 0)
    win_rate     = (wins / total_trades * 100) if total_trades > 0 else 0

    # Break down by strategy
    strategies = {}
    for t in trades:
        if t.strategy not in strategies:
            strategies[t.strategy] = {"trades": 0, "pnl": 0.0}
        strategies[t.strategy]["trades"] += 1
        strategies[t.strategy]["pnl"]    += t.pnl

    return {
        "total_pnl":    round(total_pnl, 4),
        "total_trades": total_trades,
        "win_rate":     round(win_rate, 1),
        "by_strategy":  strategies,
    }

@router.get("/chart")
async def get_chart_data(db: AsyncSession = Depends(get_db)):
    """
    Returns cumulative P&L over time for the chart.
    Only includes filled trades with non-zero P&L.
    """
    result = await db.execute(
        select(Trade)
        .where(Trade.filled == True)
        .order_by(Trade.created_at)
    )
    trades = result.scalars().all()

    cumulative = 0.0
    points     = []

    for t in trades:
        if t.pnl is None:
            continue
        cumulative += t.pnl
        points.append({
            "time": t.created_at.isoformat() if t.created_at else None,
            "pnl":  round(cumulative, 4),
            "trade_pnl": round(t.pnl, 4),
            "side": t.side,
            "market": t.market_id,
        })

    return {
        "points":   points,
        "final_pnl": round(cumulative, 4),
    }