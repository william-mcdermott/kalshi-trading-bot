import asyncio
import logging
import time
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.db import SessionLocal, init_db
from app.models.database import BotStatus, Trade
from app.services.trader import place_order
from app.bots.btc_threshold_strategy import generate_signal as btc_signal

TICK_INTERVAL  = 60
MARKET_TICKER  = "KXBTCD-26MAR2517-T70649.99"
SERIES_TICKER  = "KXBTCD"

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


async def get_contract_price(client) -> float:
    """Fetches current YES ask price for the market from Kalshi."""
    try:
        now    = int(time.time())
        result = await client.get_market_candlesticks(
            series_ticker=SERIES_TICKER,
            ticker=MARKET_TICKER,
            start_ts=now - 3600,  # look back 1 hour instead of 5 minutes
            end_ts=now,
            period_interval=1,
        )
        candles = [c for c in (result.candlesticks or [])
                   if c.yes_ask.close_dollars and c.yes_ask.close_dollars != '0.0000']
        if not candles:
            log.warning("No candle data with prices, using fallback")
            return 0.5
        latest = candles[-1]
        price  = float(latest.yes_ask.close_dollars)
        log.info(f"Contract price: {price:.4f}")
        return price
    except Exception as e:
        log.error(f"Failed to fetch contract price: {e}")
        return 0.5


async def run_bot(strategy_name: str, client):
    async with SessionLocal() as db:
        result = await db.execute(
            select(BotStatus).where(BotStatus.strategy == strategy_name)
        )
        bot = result.scalar_one_or_none()

        if not bot or not bot.is_running:
            log.debug(f"{strategy_name}: not running, skipping")
            return

        # Get current contract price from Kalshi
        contract_price = await get_contract_price(client)

        # Generate signal using BTC price vs threshold strategy
        signal = btc_signal(MARKET_TICKER, contract_price)
        log.info(f"{strategy_name}: {signal.action}  confidence={signal.confidence:.2f}  reason={signal.reason}")

        if signal.action == "HOLD":
            return

        # Place the order
        order_result = await place_order(signal, MARKET_TICKER, bot.position_size)

        # Log the trade
        trade = Trade(
            strategy=strategy_name,
            market_id=MARKET_TICKER,
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
        log.info(f"{strategy_name}: logged {signal.action} — order_id={order_result.order_id}")


async def bot_loop(strategy_name: str, client):
    log.info(f"{strategy_name}: loop started (every {TICK_INTERVAL}s)")
    while True:
        try:
            await run_bot(strategy_name, client)
        except Exception as e:
            log.error(f"{strategy_name}: error — {e}")
        await asyncio.sleep(TICK_INTERVAL)


async def run_scheduler():
    await init_db()
    client = get_client()
    log.info("Scheduler started with BTC threshold strategy")
    await asyncio.gather(*[bot_loop(name, client) for name in STRATEGIES])