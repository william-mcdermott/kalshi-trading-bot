import asyncio
import logging
import time
from datetime import datetime, timezone, date
from sqlalchemy import select, func

from app.models.db import SessionLocal, init_db
from app.models.database import BotStatus, Trade
from app.services.trader import place_order
from app.services.position_manager import position_manager
from app.bots.btc_threshold_strategy import find_best_market
from app.bots.settlement_arb_strategy import find_best_opportunity

TICK_INTERVAL    = 60
SERIES_TICKER    = "KXBTCD"
MARKET_REFRESH   = 900
SYNC_INTERVAL    = 120
ARB_WINDOW_HOURS = 6.0

# ── Guardrails ─────────────────────────────────────────
MAX_TRADES_PER_DAY   = 3    # hard cap on daily trade count
COOLDOWN_AFTER_TRADE = 600  # seconds (10 min) before next trade after settlement

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

STRATEGIES = ["macd"]

# In-memory cooldown tracker — resets on restart (intentional)
_last_trade_time: float = 0.0


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


def hours_to_settlement() -> float:
    now        = datetime.now(timezone.utc)
    settlement = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if now >= settlement:
        settlement = settlement.replace(day=settlement.day + 1)
    return (settlement - now).total_seconds() / 3600


async def trades_today() -> int:
    """Returns number of trades placed today (UTC)."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    async with SessionLocal() as db:
        result = await db.execute(
            select(func.count(Trade.id)).where(
                Trade.created_at >= today_start,
                Trade.order_id != None,
                Trade.order_id != "dry_run_order",
            )
        )
        return result.scalar() or 0


async def get_contract_price(client, ticker: str) -> float:
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
            return 0.5
        return float(candles[-1].yes_ask.close_dollars)
    except Exception as e:
        log.error(f"Failed to fetch contract price: {e}")
        return 0.5


async def run_bot(strategy_name: str, client, market_ticker: str, hours_left: float):
    global _last_trade_time

    async with SessionLocal() as db:
        result = await db.execute(
            select(BotStatus).where(BotStatus.strategy == strategy_name)
        )
        bot = result.scalar_one_or_none()

        if not bot or not bot.is_running:
            log.debug(f"{strategy_name}: not running, skipping")
            return

        # ── One position at a time ─────────────────────────────────────────
        if position_manager.has_any_open_position():
            open_tickers = list(position_manager._open_positions.keys())
            log.info(f"{strategy_name}: open position(s) on {open_tickers} — skipping")
            return

        # ── Post-settlement cooldown ───────────────────────────────────────
        seconds_since_last = time.time() - _last_trade_time
        if _last_trade_time > 0 and seconds_since_last < COOLDOWN_AFTER_TRADE:
            remaining = int(COOLDOWN_AFTER_TRADE - seconds_since_last)
            log.info(f"{strategy_name}: cooldown — {remaining}s remaining before next trade")
            return

        # ── Daily trade limit ──────────────────────────────────────────────
        count_today = await trades_today()
        if count_today >= MAX_TRADES_PER_DAY:
            log.info(f"{strategy_name}: daily limit reached ({count_today}/{MAX_TRADES_PER_DAY}) — done for today")
            return

        # ── Choose strategy based on time to settlement ────────────────────
        if hours_left <= ARB_WINDOW_HOURS:
            log.info(f"{strategy_name}: using settlement arb ({hours_left:.1f}hrs to settlement)")
            signal        = await find_best_opportunity(hours_left)
            active_ticker = signal.market_ticker if signal.market_ticker else market_ticker
        else:
            log.info(f"{strategy_name}: using momentum strategy ({hours_left:.1f}hrs to settlement)")
            contract_price = await get_contract_price(client, market_ticker)
            from app.bots.btc_threshold_strategy import generate_signal
            raw_signal     = generate_signal(market_ticker, contract_price)
            from app.bots.settlement_arb_strategy import Signal as ArbSignal
            signal = ArbSignal(
                action        = raw_signal.action,
                price         = raw_signal.price,
                fair_value    = raw_signal.price,
                edge          = 0,
                confidence    = raw_signal.confidence,
                reason        = raw_signal.reason,
                market_ticker = market_ticker,
            )
            active_ticker = market_ticker

        log.info(f"{strategy_name}: {signal.action}  confidence={signal.confidence:.2f}  reason={signal.reason}")

        if signal.action == "HOLD" or not active_ticker:
            return

        order_result = await place_order(signal, active_ticker, bot.position_size)

        if not order_result.success or not order_result.order_id:
            log.warning(f"{strategy_name}: order failed — {order_result.message}")
            return

        if order_result.order_id == "dry_run_order":
            log.info(f"{strategy_name}: [DRY RUN] {signal.action} on {active_ticker} @ {signal.price:.3f}")
            return

        trade = Trade(
            strategy   = strategy_name,
            market_id  = active_ticker,
            side       = signal.action,
            price      = signal.price,
            size       = bot.position_size,
            filled     = False,
            pnl        = 0.0,
            order_id   = order_result.order_id,
            created_at = datetime.now(timezone.utc),
        )
        db.add(trade)
        bot.total_trades += 1
        bot.updated_at    = datetime.now(timezone.utc)
        await db.commit()

        position_manager.record_order(trade)
        _last_trade_time = time.time()

        log.info(f"{strategy_name}: logged {signal.action} ({count_today + 1}/{MAX_TRADES_PER_DAY} today) — order_id={order_result.order_id}")

        from app.services.alerter import alert_trade_placed
        await alert_trade_placed(signal.action, active_ticker, signal.price, bot.position_size)


async def bot_loop(strategy_name: str, client):
    log.info(f"{strategy_name}: loop started (every {TICK_INTERVAL}s) — max {MAX_TRADES_PER_DAY}/day, {COOLDOWN_AFTER_TRADE}s cooldown")

    market_ticker    = None
    last_market_pick = 0
    last_sync        = 0

    while True:
        try:
            if time.time() - last_sync > SYNC_INTERVAL:
                await position_manager.sync_with_kalshi(client)
                last_sync = time.time()

            hours_left = hours_to_settlement()

            if time.time() - last_market_pick > MARKET_REFRESH or market_ticker is None:
                try:
                    market_ticker, _ = await find_best_market(client)
                    last_market_pick = time.time()
                except ValueError as e:
                    log.warning(f"{strategy_name}: no valid market — {e}. Waiting 5 minutes.")
                    await asyncio.sleep(300)
                    continue

            await run_bot(strategy_name, client, market_ticker, hours_left)

        except Exception as e:
            log.error(f"{strategy_name}: error — {e}")

        await asyncio.sleep(TICK_INTERVAL)


async def run_scheduler():
    await init_db()
    client = get_client()
    await position_manager.load_from_db()
    log.info("Scheduler started — momentum + settlement arb strategy")
    await asyncio.gather(*[bot_loop(name, client) for name in STRATEGIES])