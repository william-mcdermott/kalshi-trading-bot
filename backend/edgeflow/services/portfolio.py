from sqlalchemy.orm import Session
from datetime import datetime, timezone
from edgeflow.kalshi.client import KalshiClient
from edgeflow.db.models import Trade, User


async def sync_fills(user: User, db: Session) -> dict:
    client = KalshiClient(key_id=user.kalshi_key_id, private_key_pem=user.kalshi_private_key)
    fills_data = await client.get_fills(limit=100)
    fills = fills_data.get("fills", [])
    new_count = 0

    for fill in fills:
        trade_id = fill.get("trade_id")
        if db.query(Trade).filter_by(kalshi_trade_id=trade_id, user_id=user.id).first():
            continue
        trade = Trade(
            user_id=user.id,
            kalshi_trade_id=trade_id,
            ticker=fill.get("ticker", ""),
            side=fill.get("side", ""),
            count=fill.get("count", 0),
            price=fill.get("yes_price", 0),
            traded_at=datetime.fromisoformat(fill["created_time"].replace("Z", "+00:00"))
            if fill.get("created_time") else datetime.now(timezone.utc),
        )
        db.add(trade)
        new_count += 1

    db.commit()
    return {"synced": new_count, "total_fills": len(fills)}


async def sync_settlements(user: User, db: Session) -> dict:
    """
    Pull settlements from Kalshi and update matching trades with P&L and win/loss.

    Settlement shape:
      ticker, market_result (yes/no), value (cents per contract, 0 or 100),
      yes_count_fp, no_count_fp, yes_total_cost_dollars, no_total_cost_dollars,
      fee_cost, settled_time
    """
    client = KalshiClient(key_id=user.kalshi_key_id, private_key_pem=user.kalshi_private_key)
    settlements_data = await client.get_settlements(limit=100)
    settlements = settlements_data.get("settlements", [])
    updated = 0

    for s in settlements:
        ticker = s.get("ticker", "")
        market_result = s.get("market_result", "")   # "yes" or "no"
        value = s.get("value", 0)                     # 100 if winning side, 0 if losing
        yes_cost = float(s.get("yes_total_cost_dollars", 0))
        no_cost = float(s.get("no_total_cost_dollars", 0))
        fee = float(s.get("fee_cost", 0))
        settled_time = s.get("settled_time")

        settled_at = datetime.fromisoformat(settled_time.replace("Z", "+00:00")) if settled_time else None

        # Find all trades for this ticker for this user
        trades = db.query(Trade).filter_by(user_id=user.id, ticker=ticker).all()
        if not trades:
            continue

        for trade in trades:
            if trade.settled_at is not None:
                continue  # already processed

            side = trade.side  # "yes" or "no"
            is_win = (side == market_result)

            # P&L in cents:
            # If you held yes and yes won: payout = yes_count * 100 - yes_cost_cents - fee_cents
            # If you held yes and yes lost: payout = -yes_cost_cents - fee_cents
            # Same logic inverted for no
            if side == "yes":
                cost_dollars = yes_cost
                count = float(s.get("yes_count_fp", trade.count))
            else:
                cost_dollars = no_cost
                count = float(s.get("no_count_fp", trade.count))

            if is_win:
                payout_dollars = (count * value / 100)  # value is cents per contract
                pnl_dollars = payout_dollars - cost_dollars - fee
            else:
                pnl_dollars = -cost_dollars - fee

            # Store P&L in cents to match existing price convention
            trade.profit_loss = round(pnl_dollars * 100, 2)
            trade.is_win = is_win
            trade.settled_at = settled_at

            # Tag category from ticker
            if not trade.category:
                trade.category = _infer_category(ticker)

            updated += 1

    db.commit()
    return {"settlements_processed": len(settlements), "trades_updated": updated}


def _infer_category(ticker: str) -> str:
    ticker = ticker.upper()
    if "KXBTC" in ticker or "BTC" in ticker:
        return "BTC"
    if "WTI" in ticker or "OIL" in ticker:
        return "WTI"
    if "GOLD" in ticker or "XAU" in ticker:
        return "GOLD"
    if "SPX" in ticker or "SP500" in ticker or "INXD" in ticker:
        return "SPX"
    if "MLB" in ticker or "KXMLB" in ticker:
        return "MLB"
    if "CPI" in ticker or "HICP" in ticker:
        return "ECON"
    return "OTHER"


async def get_portfolio_stats(user: User, db: Session) -> dict:
    trades = db.query(Trade).filter_by(user_id=user.id).all()
    settled = [t for t in trades if t.profit_loss is not None]
    total_pnl = sum(t.profit_loss for t in settled)
    wins = [t for t in settled if t.is_win]
    win_rate = len(wins) / len(settled) if settled else 0.0
    # price is in cents (0-100), convert to dollars
    total_wagered = sum((t.price / 100) * t.count for t in settled) or 1
    # total_pnl is in cents, convert to dollars for comparison
    roi = (total_pnl / 100) / total_wagered
    open_trades = [t for t in trades if t.settled_at is None]
    return {
        "total_trades": len(trades),
        "settled_trades": len(settled),
        "open_positions": len(open_trades),
        "total_pnl": round(total_pnl / 100, 2),   # convert cents → dollars
        "win_rate": round(win_rate * 100, 1),
        "roi": round(roi * 100, 2),
    }

async def get_balance(user: User) -> dict:
    client = KalshiClient(key_id=user.kalshi_key_id, private_key_pem=user.kalshi_private_key)
    return await client.get_balance()
