# app/bots/settlement_arb_strategy.py
import math
import logging
import statistics
from dataclasses import dataclass
from typing import Optional

import ccxt
import httpx

log = logging.getLogger(__name__)

from app.config import config
from app.bots.btc_threshold_strategy import fetch_btc_history, calculate_momentum, get_todays_series
from app.utils.market_regime import get_regime
MIN_CONTRACT_PRICE      = 0.05
MAX_CONTRACT_PRICE      = 0.95
MAX_SELL_PRICE          = 0.65   # never SELL a YES contract above this price
                                  # selling at 0.70+ means risking 70¢ to win 30¢ —
                                  # backtest shows these are near-certain losers

# Volatility bounds — dynamic realized vol is clamped to this range.
# Floor prevents underpricing on quiet days (a single news event can still move price).
# Ceiling prevents overpricing during extreme events (VIX block handles those anyway).
VOL_FLOOR_PCT           = 0.30   # %/hr minimum
VOL_CEILING_PCT         = 0.80   # %/hr maximum
VOL_LOOKBACK_HOURS      = 24     # how many 1hr candles to use for realized vol


@dataclass
class Signal:
    action:        str
    price:         float
    fair_value:    float
    edge:          float
    confidence:    float
    reason:        str
    market_ticker: str


def normal_cdf(x: float) -> float:
    t    = 1 / (1 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
           + t * (-1.821255978 + t * 1.330274429))))
    p    = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-x * x / 2) * poly
    return p if x >= 0 else 1 - p


def fetch_realized_vol() -> float:
    """
    Compute realized BTC volatility from the last 24 one-hour candles.

    Method:
      - Fetch 24 hourly closes from Kraken
      - Compute log returns: ln(close[i] / close[i-1])
      - Std dev of log returns = realized vol per hour (as a fraction)
      - Convert to percentage and clamp to [VOL_FLOOR_PCT, VOL_CEILING_PCT]

    Returns %/hr as a float (e.g. 0.43 means 0.43%/hr).
    """
    try:
        exchange = ccxt.kraken()
        # Fetch one extra candle so we get 24 complete returns
        ohlcv    = exchange.fetch_ohlcv("BTC/USDT", "1h", limit=VOL_LOOKBACK_HOURS + 1)
        closes   = [candle[4] for candle in ohlcv]

        if len(closes) < 2:
            log.warning(f"Not enough candles for realized vol — using floor {VOL_FLOOR_PCT}%/hr")
            return VOL_FLOOR_PCT

        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
        ]

        # Std dev of log returns gives vol per hour as a fraction
        raw_vol_pct = statistics.stdev(log_returns) * 100

        # Clamp to safe range
        clamped = max(VOL_FLOOR_PCT, min(VOL_CEILING_PCT, raw_vol_pct))

        if clamped != raw_vol_pct:
            log.info(
                f"Realized vol {raw_vol_pct:.3f}%/hr clamped to {clamped:.3f}%/hr "
                f"({'floor' if clamped == VOL_FLOOR_PCT else 'ceiling'})"
            )

        return round(clamped, 4)

    except Exception as e:
        log.warning(f"Realized vol fetch failed ({e}) — using floor {VOL_FLOOR_PCT}%/hr")
        return VOL_FLOOR_PCT


def fair_value(
    btc_price: float,
    threshold: float,
    hours_to_settlement: float,
    volatility_pct: float,
) -> float:
    if hours_to_settlement <= 0:
        return 1.0 if btc_price >= threshold else 0.0
    if hours_to_settlement > 24:
        return 0.5
    distance_pct = (btc_price - threshold) / threshold * 100
    total_vol    = volatility_pct * math.sqrt(hours_to_settlement)
    z            = distance_pct / total_vol
    return round(normal_cdf(z), 4)


def parse_threshold(ticker: str) -> float:
    try:
        return float(ticker.split("-T")[-1])
    except Exception:
        raise ValueError(f"Could not parse threshold from ticker: {ticker}")


def fetch_daily_range() -> float:
    exchange = ccxt.kraken()
    ohlcv    = exchange.fetch_ohlcv("BTC/USDT", "1d", limit=2)
    if not ohlcv:
        return 0.0
    today = ohlcv[-1]
    return float(today[2]) - float(today[3])


async def scan_markets(hours_to_settlement: float) -> list[dict]:
    all_markets = []
    cursor = None

    async with httpx.AsyncClient(timeout=10.0) as http:
        while True:
            params = {
                "limit": 100,
                "status": "open",
                "event_ticker": get_todays_series(),
            }
            if cursor:
                params["cursor"] = cursor

            r    = await http.get(
                "https://api.elections.kalshi.com/trade-api/v2/markets",
                params=params,
            )
            data    = r.json()
            markets = data.get("markets", [])
            cursor  = data.get("cursor", "")
            all_markets.extend(markets)

            if not cursor or not markets:
                break

    result = []
    for m in all_markets:
        ticker  = m.get("ticker", "")
        yes_bid = m.get("yes_bid_dollars")
        yes_ask = m.get("yes_ask_dollars")

        if yes_bid is None or yes_ask is None:
            continue

        mid = (float(yes_bid) + float(yes_ask)) / 2
        if mid <= MIN_CONTRACT_PRICE or mid >= MAX_CONTRACT_PRICE:
            continue

        try:
            threshold = parse_threshold(ticker)
        except ValueError:
            continue

        result.append({
            "ticker":    ticker,
            "threshold": threshold,
            "mid":       mid,
            "yes_bid":   float(yes_bid),
            "yes_ask":   float(yes_ask),
        })

    return result


async def find_best_opportunity(hours_to_settlement: float) -> Signal:
    if hours_to_settlement > config.max_hours_to_settlement:
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"Too early — {hours_to_settlement:.1f}hrs to settlement",
            market_ticker="",
        )

    regime, vix, regime_msg = get_regime()
    if regime == "HIGH":
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"Regime block — {regime_msg}",
            market_ticker="",
        )
    if regime == "ELEVATED":
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"Regime block — {regime_msg}",
            market_ticker="",
        )
    if regime == "UNKNOWN":
        log.warning("VIX unavailable — proceeding with caution")

    daily_range = fetch_daily_range()
    if daily_range < config.min_daily_range:
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"Low volatility (range=${daily_range:,.0f} — need >${config.min_daily_range:,.0f})",
            market_ticker="",
        )

    prices    = fetch_btc_history(12)
    btc_price = prices[-1]
    momentum  = calculate_momentum(prices)

    # Dynamic realized vol — replaces hardcoded BTC_VOLATILITY_PCT
    realized_vol = fetch_realized_vol()

    log.info(
        f"Arb scan — BTC=${btc_price:,.2f} momentum={momentum:+.2f}%/hr "
        f"realized_vol={realized_vol:.3f}%/hr range=${daily_range:,.0f}"
    )

    markets = await scan_markets(hours_to_settlement)

    best_signal   = None
    best_abs_edge = 0.0

    for m in markets:
        fv = fair_value(btc_price, m["threshold"], hours_to_settlement, realized_vol)

        # Compute executable edge against actual execution price, not mid.
        # BUY: we pay yes_ask, so edge = fv - yes_ask
        # SELL: we receive yes_bid, so edge = yes_bid - fv (positive = good)
        edge_buy  = fv - m["yes_ask"]
        edge_sell = m["yes_bid"] - fv

        # Pick the better side — whichever has positive executable edge
        if edge_buy >= edge_sell and edge_buy > 0:
            action    = "BUY"
            exec_edge = edge_buy
        elif edge_sell > edge_buy and edge_sell > 0:
            action    = "SELL"
            exec_edge = edge_sell
        else:
            continue  # no positive executable edge on either side

        if exec_edge < config.min_edge:
            continue

        # Directional filter — only block strong opposing momentum
        if action == "BUY" and momentum < -config.momentum_block:
            continue
        if action == "SELL" and momentum > config.momentum_block:
            continue

        price = m["yes_ask"] if action == "BUY" else m["yes_bid"]

        # Never SELL a contract priced above MAX_SELL_PRICE —
        # e.g. selling YES at 0.81 risks 81¢ to win 19¢ on a near-certain outcome
        if action == "SELL" and m["yes_bid"] > MAX_SELL_PRICE:
            log.debug(
                f"Skipping SELL {m['ticker']} — bid={m['yes_bid']:.2f} > MAX_SELL_PRICE={MAX_SELL_PRICE}"
            )
            continue

        confidence = min(exec_edge / 0.30, 1.0)

        signal = Signal(
            action        = action,
            price         = price,
            fair_value    = fv,
            edge          = exec_edge,
            confidence    = confidence,
            reason        = (
                f"{action} {m['ticker']} — "
                f"ask={m['yes_ask']:.3f} bid={m['yes_bid']:.3f} fair={fv:.3f} "
                f"edge_buy={edge_buy:+.3f} edge_sell={edge_sell:+.3f} exec={exec_edge:+.3f} "
                f"momentum={momentum:+.2f}%/hr vol={realized_vol:.3f}%/hr range=${daily_range:,.0f}"
            ),
            market_ticker = m["ticker"],
        )

        if exec_edge > best_abs_edge:
            best_abs_edge = exec_edge
            best_signal   = signal

    if best_signal is None:
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=(
                f"No opportunity (edge>{config.min_edge:.0%}, "
                f"vol={realized_vol:.3f}%/hr, range=${daily_range:,.0f}, mom={momentum:+.2f}%/hr)"
            ),
            market_ticker="",
        )

    log.info(f"Best arb: {best_signal.reason}")
    return best_signal