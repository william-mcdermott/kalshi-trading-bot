# app/services/fill_tracker.py
import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.db import SessionLocal
from app.models.database import Trade

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

                if filled_count > 0 or "filled" in status or "canceled" in status:
                    taker_cost = float(order.taker_fill_cost_dollars or 0)
                    maker_cost = float(order.maker_fill_cost_dollars or 0)
                    total_cost = taker_cost + maker_cost

                    trade.filled    = True
                    trade.closed_at = datetime.now(timezone.utc)
                    trade.pnl       = -total_cost  # always negative until settlement pays out

                    log.info(f"Order {trade.order_id[:8]}... filled — cost=${total_cost:.4f}")

                elif "resting" in status:
                    log.debug(f"Order {trade.order_id[:8]}... still resting")

            except Exception as e:
                log.error(f"Error checking order {trade.order_id}: {e}")

        await db.commit()


async def check_settlements(client):
    async with SessionLocal() as db:
        result = await db.execute(
            select(Trade).where(
                Trade.filled == True,
                Trade.pnl < 0,
                Trade.order_id != None,
                Trade.order_id != "dry_run_order",
            )
        )
        trades = result.scalars().all()

        if not trades:
            return

        for trade in trades:
            try:
                fills = await client.get_fills(order_id=trade.order_id)
                fill_list = fills.fills or []

                total_payout = 0.0
                for f in fill_list:
                    for attr in ['payout_dollars', 'settlement_payout_dollars',
                                 'yes_price_dollars', 'no_price_dollars']:
                        val = getattr(f, attr, None)
                        if val is not None:
                            count = float(getattr(f, 'count', None) or
                                          getattr(f, 'count_fp', 1) or 1)
                            total_payout += float(val) * count
                            break

                total_cost = abs(trade.pnl)

                if total_payout > 0:
                    trade.pnl = total_payout - total_cost
                    log.info(
                        f"Settlement — order {trade.order_id[:8]}... "
                        f"payout=${total_payout:.4f} cost=${total_cost:.4f} "
                        f"pnl=${trade.pnl:.4f}"
                    )
                    from app.services.alerter import alert_trade_settled
                    await alert_trade_settled(trade.side, trade.market_id, trade.pnl)

                else:
                    try:
                        response = await client.get_order(order_id=trade.order_id)
                        order    = response.order
                        status   = str(order.status).lower()

                        if "settled" in status or "expired" in status:
                            trade.pnl = -total_cost
                            log.info(
                                f"Settlement — order {trade.order_id[:8]}... "
                                f"payout=$0.0000 cost=${total_cost:.4f} "
                                f"pnl=${trade.pnl:.4f}"
                            )
                            from app.services.alerter import alert_trade_settled
                            await alert_trade_settled(trade.side, trade.market_id, trade.pnl)

                    except Exception:
                        pass

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