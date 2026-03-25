import asyncio
import logging
import time
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.db import SessionLocal, init_db
from app.models.database import BotStatus, Trade
from app.services.trader import place_order
from app.services.position_manager import position_manager
from app.bots.btc_threshold_strategy import generate_signal as btc_signal, find_best_market

TICK_INTERVAL  = 60
SERIES_TICKER  = "KXBTCD"
MARKET_REFRESH = 900   # re-pick market every 15 minutes
SYNC_INTERVAL  = 120   # sync positions with Kalshi every 2 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

STRATEGIES = ["macd"]


def get_client():
    import os
    from dotenv import load_dotenv
    from kalshi_python_async import Configuration, KalshiClient
    load_dotenv()
    with open(os.getenv('KALSHI_PRIVATE_KEY_PATH')) as f:
        key = f.read()
    config = Configuration(host=os.getenv('KALSHI_HOST'))
    config.api_key_id      = os.getenv('KALSHI_API_KEY_ID')
    config.private_key_pem = key
    return KalshiClient(config)


async def get_contract_price(client, ticker: str) -> float:
    """Fetches current YES ask price for a market from Kalshi."""
    try:
        now    = int(time.time())
        result = await client.get_market_candlesticks(
            series_ticker=SERIES_TICKER,
            ticker=ticker,
            start_ts=now - 3600,
            end_ts=now,
            period_interval=1,
        )
        candles = [c for c in (result.candlesticks or [])
                   if c.yes_ask.close_dollars and c.yes_ask.close_dollars != '0.0000']
        if not candles:
            log.warning(f"No candle data for {ticker}, using fallback 0.5")
            return 0.5
        price = float(candles[-1].yes_ask.close_dollars)
        log.info(f"Contract price: {price:.4f}")
        return price
    except Exception as e:
        log.error(f"Failed to fetch contract price: {e}")
        return 0.5


async def run_bot(strategy_name: str, client, market_ticker: str):
    async with SessionLocal() as db:
        result = await db.execute(
            select(BotStatus).where(BotStatus.strategy == strategy_name)
        )
        bot = result.scalar_one_or_none()

        if not bot or not bot.is_running:
            log.debug(f"{strategy_name}: not running, skipping")
            return

        # Check if we already have an open position on this market
        if position_manager.has_open_position(market_ticker):
            existing = position_manager.get_open_position(market_ticker)
            log.info(f"{strategy_name}: already have open {existing.side} on {market_ticker} — skipping")
            return

        contract_price = await get_contract_price(client, market_ticker)
        signal         = btc_signal(market_ticker, contract_price)
        log.info(f"{strategy_name}: {signal.action}  confidence={signal.confidence:.2f}  reason={signal.reason}")

        if signal.action == "HOLD":
            return

        order_result = await place_order(signal, market_ticker, bot.position_size)

        trade = Trade(
            strategy=strategy_name,
            market_id=market_ticker,
            side=signal.action,
            price=signal.price,
            size=bot.position_size,
            filled=False,
            pnl=0.0,
            order_id=order_result.order_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(trade)
        bot.total_trades += 1
        bot.updated_at    = datetime.now(timezone.utc)
        await db.commit()

        # Register the position so we don't double-trade
        position_manager.record_order(trade)
        log.info(f"{strategy_name}: logged {signal.action} — order_id={order_result.order_id}")


async def bot_loop(strategy_name: str, client):
    log.info(f"{strategy_name}: loop started (every {TICK_INTERVAL}s)")

    market_ticker    = None
    last_market_pick = 0
    last_sync        = 0

    while True:
        try:
            # Sync positions with Kalshi every 2 minutes
            if time.time() - last_sync > SYNC_INTERVAL:
                await position_manager.sync_with_kalshi(client)
                last_sync = time.time()

            # Re-pick the best market every 15 minutes
            if time.time() - last_market_pick > MARKET_REFRESH or market_ticker is None:
                try:
                    market_ticker, _ = await find_best_market(client)
                    last_market_pick = time.time()
                except ValueError as e:
                    log.warning(f"{strategy_name}: no valid market — {e}. Waiting 5 minutes.")
                    await asyncio.sleep(300)
                    continue

            await run_bot(strategy_name, client, market_ticker)

        except Exception as e:
            log.error(f"{strategy_name}: error — {e}")

        await asyncio.sleep(TICK_INTERVAL)


async def run_scheduler():
    await init_db()
    client = get_client()

    # Load any existing open positions from DB on startup
    await position_manager.load_from_db()

    log.info("Scheduler started with position manager")
    await asyncio.gather(*[bot_loop(name, client) for name in STRATEGIES])