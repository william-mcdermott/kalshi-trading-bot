from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional
import csv, io

from edgeflow.db.session import get_db
from edgeflow.db.models import User, Trade
from edgeflow.core.security import get_current_user_id

router = APIRouter()


def get_user(user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)) -> User:
    return db.query(User).filter_by(id=user_id).first()


@router.get("/")
def list_trades(
    category: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    user: User = Depends(get_user),
    db: Session = Depends(get_db),
):
    q = db.query(Trade).filter_by(user_id=user.id)
    if category:
        q = q.filter(Trade.category == category.upper())
    if tag:
        q = q.filter(Trade.tag == tag)
    trades = q.order_by(Trade.traded_at.desc()).limit(limit).all()
    return {
        "trades": [
            {
                "id": t.id, "ticker": t.ticker, "market_title": t.market_title,
                "category": t.category, "side": t.side, "count": t.count,
                "price": t.price, "profit_loss": t.profit_loss, "is_win": t.is_win,
                "tag": t.tag, "notes": t.notes,
                "traded_at": t.traded_at.isoformat() if t.traded_at else None,
            }
            for t in trades
        ],
        "total": len(trades),
    }


@router.patch("/{trade_id}/tag")
def update_tag(trade_id: str, tag: str, user: User = Depends(get_user), db: Session = Depends(get_db)):
    trade = db.query(Trade).filter_by(id=trade_id, user_id=user.id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    trade.tag = tag
    db.commit()
    return {"id": trade_id, "tag": tag}


@router.get("/export/csv")
def export_csv(user: User = Depends(get_user), db: Session = Depends(get_db)):
    trades = db.query(Trade).filter_by(user_id=user.id).order_by(Trade.traded_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "ticker", "category", "side", "count", "price", "pnl", "win", "tag"])
    for t in trades:
        writer.writerow([
            t.traded_at.date() if t.traded_at else "", t.ticker, t.category or "",
            t.side, t.count, t.price, t.profit_loss or "", t.is_win or "", t.tag or "",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=edgeflow_trades.csv"},
    )
