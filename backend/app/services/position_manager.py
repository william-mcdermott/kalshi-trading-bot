# app/services/position_manager.py
#
# Tracks open positions and prevents duplicate orders.
# ONE open position at a time across ALL markets.
# Position stays open until settlement — not just until fill.

import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select

from app.models.db import SessionLocal
from app.models.database import Trade

log = logging.getLogger(__name__)

STALE_ORDER_MINUTES = 30


class PositionManager:

    def __init__(self):
        self._open_positions: dict[str, Trade] = {}

    async def load_from_db(self):
        """
        On startup, reload any positions that filled but haven't settled yet.
        Checks both unfilled orders AND filled trades with negative pnl (awaiting settlement).
        """
        async with SessionLocal() as db:
            result = await db.execute(
                select(Trade).where(
                    Trade.order_id != None,
                    Trade.order_id != "dry_run_order",
                ).where(
                    # Either not yet filled, or filled but not yet settled
                    (Trade.filled == False) | (Trade.pnl < 0)
                )
            )
            trades = result.scalars().all()
            for trade in trades:
                self._open_positions[trade.market_id] = trade
            if trades:
                log.info(f"Loaded {len(trades)} open/unsettled positions from database")

    def has_any_open_position(self) -> bool:
        """Returns True if we have ANY open or unsettled position."""
        return len(self._open_positions) > 0

    def has_open_position(self, market_ticker: str) -> bool:
        return market_ticker in self._open_positions

    def get_open_position(self, market_ticker: str) -> Trade | None:
        return self._open_positions.get(market_ticker)

    def all_open_positions(self) -> list[Trade]:
        return list(self._open_positions.values())

    def record_order(self, trade: Trade):
        self._open_positions[trade.market_id] = trade
        log.info(f"Position opened: {trade.side} on {trade.market_id}")

    def close_position(self, market_ticker: str):
        """Called after settlement is confirmed — not on fill."""
        if market_ticker in self._open_positions:
            del self._open_positions[market_ticker]
            log.info(f"Position closed after settlement: {market_ticker}")

    async def sync_with_kalshi(self, client):
        """
        Only cancels stale unfilled orders.
        Does NOT close positions on fill — that happens after settlement.
        """
        if not self._open_positions:
            return

        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_ORDER_MINUTES)

        for market_ticker, trade in list(self._open_positions.items()):
            if not trade.order_id or trade.order_id == "dry_run_order":
                continue

            # Skip already-filled trades — they stay until settlement
            if trade.filled:
                continue

            try:
                response = await client.get_order(order_id=trade.order_id)
                order    = response.order
                status   = str(order.status).lower()

                if "canceled" in status:
                    log.info(f"Order cancelled: {market_ticker} — closing position")
                    self.close_position(market_ticker)

                elif trade.created_at and trade.created_at.replace(tzinfo=timezone.utc) < stale_cutoff:
                    log.warning(f"Stale order on {market_ticker} — cancelling")
                    try:
                        await client.cancel_order(order_id=trade.order_id)
                    except Exception as e:
                        log.error(f"Failed to cancel stale order: {e}")
                    self.close_position(market_ticker)

            except Exception as e:
                log.error(f"Error syncing position for {market_ticker}: {e}")


position_manager = PositionManager()