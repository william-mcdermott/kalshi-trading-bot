# app/services/fill_tracker.py
#
# Polls Kalshi every few minutes to check if our orders have filled
# or settled, then updates P&L in the database.
#
# Kalshi doesn't push fill notifications — you have to ask.
# This is like polling an API for job status instead of using webhooks.

import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.db import SessionLocal
from app.models.database import Trade

POLL_INTERVAL = 120  # check every 2 minutes

log = logging.getLogger(__name__)


async def check_fills(client):
    """
    Fetches all unfilled trades from the database,
    checks their status on Kalshi, and updates P&L.
    """
    async with SessionLocal() as db:
        # Get all trades that haven't been filled yet and have a real order ID
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
                # Fetch order status from Kalshi
                response = await client.get_order(order_id=trade.order_id)
                order    = response.order

                status      = str(order.status).lower()
                filled_count = float(order.fill_count_fp or 0)

                if filled_count > 0 or "filled" in status or "canceled" in status:
                    # Calculate P&L
                    # On Kalshi: if you bought YES at 30¢ and it resolves YES → payout $1 per contract
                    # P&L = payout - cost = $1.00 - $0.30 = $0.70
                    # If it resolves NO → payout $0, P&L = -$0.30
                    taker_cost = float(order.taker_fill_cost_dollars or 0)
                    maker_cost = float(order.maker_fill_cost_dollars or 0)
                    total_cost = taker_cost + maker_cost

                    # Mark as filled
                    trade.filled    = True
                    trade.closed_at = datetime.now(timezone.utc)

                    # P&L is unknown until market settles — store cost for now
                    # Will be updated when market resolves
                    trade.pnl = -total_cost  # negative cost = money out

                    log.info(f"Order {trade.order_id[:8]}... filled — cost=${total_cost:.4f}")

                elif "resting" in status:
                    log.debug(f"Order {trade.order_id[:8]}... still resting")

            except Exception as e:
                log.error(f"Error checking order {trade.order_id}: {e}")

        await db.commit()


async def check_settlements(client):
    """
    Checks filled trades to see if their markets have settled,
    and updates final P&L.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Trade).where(
                Trade.filled == True,
                Trade.pnl <= 0,  # hasn't received payout yet
                Trade.order_id != None,
                Trade.order_id != "dry_run_order",
            )
        )
        trades = result.scalars().all()

        if not trades:
            return

        for trade in trades:
            try:
                # Check fills for this order — fills contain payout info
                fills = await client.get_fills(order_id=trade.order_id)

                total_payout = sum(
                    float(getattr(f, 'yes_price_dollars', None) or 
                        getattr(f, 'yes_price', None) or 0)
                    * float(getattr(f, 'count', None) or 
                            getattr(f, 'count_fp', 1) or 1)
                    for f in (fills.fills or [])
                )
                total_cost = abs(trade.pnl)  # stored as negative cost earlier

                if total_payout > 0:
                    trade.pnl = total_payout - total_cost
                    log.info(
                        f"Settlement — order {trade.order_id[:8]}... "
                        f"payout=${total_payout:.4f} cost=${total_cost:.4f} "
                        f"pnl=${trade.pnl:.4f}"
                    )

            except Exception as e:
                log.error(f"Error checking settlement for {trade.order_id}: {e}")

        await db.commit()


async def run_fill_tracker(client):
    """
    Runs continuously, polling Kalshi for fill and settlement updates.
    Runs as a separate asyncio task alongside the bot loop.
    """
    log.info("Fill tracker started")
    while True:
        try:
            await check_fills(client)
            await check_settlements(client)
        except Exception as e:
            log.error(f"Fill tracker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)