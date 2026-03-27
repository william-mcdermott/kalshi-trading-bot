# app/services/fill_tracker.py
import asyncio
import logging
import httpx
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.db import SessionLocal
from app.models.database import Trade
from app.services.position_manager import position_manager

POLL_INTERVAL = 120

log = logging.getLogger(__name__)


async def check_fills(client):
    async with SessionLocal() as db:
        result = await db.execute(
            select(Trade).where(
                Trade.filled == False,
                Trade.order_id != None,
                Trade.order_id != "dry_run_order",
            )
        )
        trades = result.scalars().all()

        if not trades:
            log.debug("No unfilled trades to check")
            return

        log.info(f"Checking {len(trades)} unfilled trades")

        for trade in trades:
            try:
                response     = await client.get_order(order_id=trade.order_id)
                order        = response.order
                status       = str(order.status).lower()
                filled_count = float(order.fill_count_fp or 0)

                if filled_count > 0 or "executed" in status or "filled" in status or "canceled" in status:
                    taker_cost = float(order.taker_fill_cost_dollars or 0)
                    maker_cost = float(order.maker_fill_cost_dollars or 0)
                    total_cost = taker_cost + maker_cost

                    trade.filled    = True
                    trade.closed_at = datetime.now(timezone.utc)
                    trade.pnl       = 0.0  # zero until settlement confirms result

                    log.info(f"Order {trade.order_id[:8]}... filled — cost=${total_cost:.4f}")

                elif "resting" in status:
                    log.debug(f"Order {trade.order_id[:8]}... still resting")

            except Exception as e:
                log.error(f"Error checking order {trade.order_id}: {e}")

        await db.commit()


async def get_market_result(market_id: str) -> tuple[str, float] | None:
    """
    Fetches market status from Kalshi.
    Returns (result, cost) where result is 'yes', 'no', or None if not settled.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                f"https://api.elections.kalshi.com/trade-api/v2/markets/{market_id}",
            )
            market = r.json().get("market", {})

        status     = market.get("status", "")
        result_val = market.get("result", "")

        if status == "finalized" and result_val in ("yes", "no"):
            return result_val
        return None

    except Exception as e:
        log.error(f"Error fetching market {market_id}: {e}")
        return None


async def check_settlements(client):
    async with SessionLocal() as db:
        # Catch both: pnl=0.0 (new style) and pnl<0 (old style, cost stored negative)
        result = await db.execute(
            select(Trade).where(
                Trade.filled == True,
                Trade.order_id != None,
                Trade.order_id != "dry_run_order",
                Trade.settled == False,
            )
        )
        trades = result.scalars().all()

        if not trades:
            return

        for trade in trades:
            try:
                result_val = await get_market_result(trade.market_id)

                if result_val is None:
                    continue  # not settled yet

                # Get cost from order
                order_resp = await client.get_order(order_id=trade.order_id)
                order      = order_resp.order
                taker_cost = float(order.taker_fill_cost_dollars or 0)
                maker_cost = float(order.maker_fill_cost_dollars or 0)
                total_cost = taker_cost + maker_cost

                if total_cost == 0:
                    # Fallback: use stored negative pnl as cost if order fetch fails
                    total_cost = abs(trade.pnl) if trade.pnl < 0 else trade.price

                # Calculate P&L from first principles
                # BUY YES: win $1 if yes, lose cost if no
                # SELL YES: keep cost if no, owe $1 if yes
                if trade.side == "BUY":
                    trade.pnl = round(1.0 - total_cost, 4) if result_val == "yes" else round(-total_cost, 4)
                else:
                    trade.pnl = round(total_cost, 4) if result_val == "no" else round(-(1.0 - total_cost), 4)

                trade.settled = True  # never process this trade again

                log.info(
                    f"Settlement — {trade.side} {trade.market_id[-16:]} "
                    f"result={result_val} cost=${total_cost:.4f} "
                    f"pnl=${trade.pnl:.4f}"
                )

                position_manager.close_position(trade.market_id)

                from app.services.alerter import alert_trade_settled
                await alert_trade_settled(trade.side, trade.market_id, trade.pnl)
            except Exception as e:
                log.error(f"Error checking settlement for {trade.order_id}: {e}")

        await db.commit()


async def run_fill_tracker(client):
    log.info("Fill tracker started")
    while True:
        try:
            await check_fills(client)
            await check_settlements(client)
        except Exception as e:
            log.error(f"Fill tracker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)