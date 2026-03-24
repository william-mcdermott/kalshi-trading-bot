import asyncio
import logging
import time
import pandas as pd
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.db import SessionLocal, init_db
from app.models.database import BotStatus, Trade
from app.bots.macd_strategy import MACDStrategy
from app.services.trader import place_order

TICK_INTERVAL = 60  # 1 minute — matches 1-min candles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Bitcoin daily market — stays open 24hrs, has continuous price movement
MARKET_TICKER  = "KXBTCD-26MAR2417-T77549.99"
SERIES_TICKER  = "KXBTCD"

STRATEGIES = {
    "macd": MACDStrategy(),
}


async def get_real_prices(client, ticker: str, series_ticker: str, n: int = 50) -> pd.DataFrame:
    """
    Fetches real candlestick data from Kalshi and returns a pandas DataFrame.
    Uses yes_ask close price as the 'close' — cost to buy YES contract.
    """
    now       = int(time.time())
    start_ts  = now - (n * 60 * 2)  # fetch 2x what we need to ensure enough candles

    result = await client.get_market_candlesticks(
        series_ticker=series_ticker,
        ticker=ticker,
        start_ts=start_ts,
        end_ts=now,
        period_interval=1,
    )

    candles = result.candlesticks or []

    rows = []
    for c in candles:
        ask = c.yes_ask
        # Skip candles with no data
        if not ask or ask.close_dollars == '0.0000':
            continue
        close = float(ask.close_dollars)
        rows.append({
            "open":   float(ask.open_dollars  or ask.close_dollars),
            "high":   float(ask.high_dollars  or ask.close_dollars),
            "low":    float(ask.low_dollars   or ask.close_dollars),
            "close":  close,
            "volume": float(c.volume_fp or 0),
        })

    if len(rows) < 20:
        log.warning(f"Only got {len(rows)} candles — not enough for reliable signal")
        return pd.DataFrame()

    return pd.DataFrame(rows[-n:])  # use the most recent n candles


def get_client():
    """Returns an authenticated Kalshi client."""
    import os
    from dotenv import load_dotenv
    from kalshi_python_async import Configuration, KalshiClient
    load_dotenv()
    with open(os.getenv('KALSHI_PRIVATE_KEY_PATH')) as f:
        key = f.read()
    config = Configuration(host=os.getenv('KALSHI_HOST'))
    config.api_key_id     = os.getenv('KALSHI_API_KEY_ID')
    config.private_key_pem = key
    return KalshiClient(config)


async def run_bot(strategy_name: str, client):
    strategy = STRATEGIES.get(strategy_name)
    if not strategy:
        return

    async with SessionLocal() as db:
        result = await db.execute(
            select(BotStatus).where(BotStatus.strategy == strategy_name)
        )
        bot = result.scalar_one_or_none()

        if not bot or not bot.is_running:
            log.debug(f"{strategy_name}: not running, skipping")
            return

        # Fetch real prices
        prices = await get_real_prices(client, MARKET_TICKER, SERIES_TICKER)
        if prices.empty:
            log.warning(f"{strategy_name}: no price data, skipping tick")
            return

        signal = strategy.generate_signal(prices)
        log.info(f"{strategy_name}: {signal.action}  price={signal.price:.3f}  reason={signal.reason}")

        if signal.action == "HOLD":
            return

        result = await place_order(signal, MARKET_TICKER, bot.position_size)

        trade = Trade(
            strategy=strategy_name,
            market_id=MARKET_TICKER,
            side=signal.action,
            price=signal.price,
            size=bot.position_size,
            filled=False,
            pnl=0.0,
            order_id=result.order_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(trade)
        bot.total_trades += 1
        bot.updated_at    = datetime.now(timezone.utc)
        await db.commit()
        log.info(f"{strategy_name}: logged {signal.action} — order_id={result.order_id}")


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
    log.info("Scheduler started with real Kalshi price data")
    await asyncio.gather(*[bot_loop(name, client) for name in STRATEGIES])