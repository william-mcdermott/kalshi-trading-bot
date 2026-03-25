# app/services/position_manager.py
#
# Tracks open positions and prevents duplicate orders on the same market.
# Before placing any order, the scheduler checks here first.
#
# Think of this like a mutex — only one open position per market at a time.

import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select

from app.models.db import SessionLocal
from app.models.database import Trade

log = logging.getLogger(__name__)

# How long to wait before cancelling a stale unfilled order (minutes)
STALE_ORDER_MINUTES = 30


class PositionManager:
    """
    Manages open positions per market.
    Keeps an in-memory cache of open positions, backed by the database.
    """

    def __init__(self):
        # market_ticker -> Trade (the open position)
        self._open_positions: dict[str, Trade] = {}

    async def load_from_db(self):
        """Load any existing open positions from the database on startup."""
        async with SessionLocal() as db:
            result = await db.execute(
                select(Trade).where(
                    Trade.filled == False,
                    Trade.order_id != None,
                    Trade.order_id != "dry_run_order",
                )
            )
            trades = result.scalars().all()
            for trade in trades:
                self._open_positions[trade.market_id] = trade
            if trades:
                log.info(f"Loaded {len(trades)} open positions from database")

    def has_open_position(self, market_ticker: str) -> bool:
        """Returns True if we already have an unfilled order on this market."""
        return market_ticker in self._open_positions

    def get_open_position(self, market_ticker: str) -> Trade | None:
        """Returns the open Trade for this market, or None."""
        return self._open_positions.get(market_ticker)

    def record_order(self, trade: Trade):
        """Called after placing an order — records it as an open position."""
        self._open_positions[trade.market_id] = trade
        log.info(f"Position opened: {trade.side} on {trade.market_id}")

    def close_position(self, market_ticker: str):
        """Called when an order fills or is cancelled."""
        if market_ticker in self._open_positions:
            del self._open_positions[market_ticker]
            log.info(f"Position closed: {market_ticker}")

    async def sync_with_kalshi(self, client):
        """
        Checks all open positions against Kalshi.
        Closes positions that have filled or been cancelled.
        Cancels positions that have been open too long.
        """
        if not self._open_positions:
            return

        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_ORDER_MINUTES)
        to_close     = []

        for market_ticker, trade in self._open_positions.items():
            if not trade.order_id or trade.order_id == "dry_run_order":
                continue

            try:
                response = await client.get_order(order_id=trade.order_id)
                order    = response.order
                status   = str(order.status).lower()
                filled   = float(order.fill_count_fp or 0)

                if filled > 0 or "canceled" in status:
                    log.info(f"Position filled/cancelled: {market_ticker} — closing")
                    to_close.append(market_ticker)

                elif trade.created_at and trade.created_at.replace(tzinfo=timezone.utc) < stale_cutoff:                    # Order has been sitting too long — cancel it
                    log.warning(f"Stale order on {market_ticker} — cancelling")
                    try:
                        await client.cancel_order(order_id=trade.order_id)
                    except Exception as e:
                        log.error(f"Failed to cancel stale order: {e}")
                    to_close.append(market_ticker)

            except Exception as e:
                log.error(f"Error syncing position for {market_ticker}: {e}")

        for market_ticker in to_close:
            self.close_position(market_ticker)


# Global singleton — shared across the whole app
position_manager = PositionManager()