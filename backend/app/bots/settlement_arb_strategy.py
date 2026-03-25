import math
import logging
from dataclasses import dataclass
from typing import Optional

import ccxt
import httpx

log = logging.getLogger(__name__)

MIN_EDGE                = 0.12
MAX_HOURS_TO_SETTLEMENT = 6.0
MIN_CONTRACT_PRICE      = 0.05
MAX_CONTRACT_PRICE      = 0.95
BTC_VOLATILITY_PCT      = 2.5
MIN_DAILY_RANGE         = 1000.0


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


def fair_value(
    btc_price: float,
    threshold: float,
    hours_to_settlement: float,
    volatility_pct: float = BTC_VOLATILITY_PCT,
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
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            params={"limit": 100, "status": "open", "series_ticker": "KXBTCD"},
        )
        markets = r.json().get("markets", [])

    result = []
    for m in markets:
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


async def find_best_opportunity(hours_to_settlement: float) -> Optional[Signal]:
    if hours_to_settlement > MAX_HOURS_TO_SETTLEMENT:
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"Too early — {hours_to_settlement:.1f}hrs to settlement",
            market_ticker="",
        )

    # Volatility filter
    daily_range = fetch_daily_range()
    if daily_range < MIN_DAILY_RANGE:
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"Low volatility day (range=${daily_range:,.0f} — need >${MIN_DAILY_RANGE:,.0f})",
            market_ticker="",
        )

    from app.bots.btc_threshold_strategy import fetch_btc_history, calculate_momentum
    prices    = fetch_btc_history(12)
    btc_price = prices[-1]
    momentum  = calculate_momentum(prices)

    log.info(f"Settlement arb scan — BTC=${btc_price:,.2f} momentum={momentum:+.2f}%/hr range=${daily_range:,.0f}")

    markets = await scan_markets(hours_to_settlement)

    best_signal   = None
    best_abs_edge = 0.0

    for m in markets:
        fv   = fair_value(btc_price, m["threshold"], hours_to_settlement)
        edge = fv - m["mid"]

        if abs(edge) < MIN_EDGE:
            continue

        # Directional filter
        if edge > 0:
            continue
        if edge < 0 and momentum > 0:
            continue

        confidence = min(abs(edge) / 0.30, 1.0)
        action     = "BUY" if edge > 0 else "SELL"
        price      = m["yes_ask"] if action == "BUY" else m["yes_bid"]

        signal = Signal(
            action        = action,
            price         = price,
            fair_value    = fv,
            edge          = edge,
            confidence    = confidence,
            reason        = (
                f"{action} {m['ticker']} — "
                f"market={m['mid']:.3f} fair={fv:.3f} edge={edge:+.3f} "
                f"momentum={momentum:+.2f}%/hr range=${daily_range:,.0f}"
            ),
            market_ticker = m["ticker"],
        )

        if abs(edge) > best_abs_edge:
            best_abs_edge = abs(edge)
            best_signal   = signal

    if best_signal is None:
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"No opportunity found (edge>{MIN_EDGE:.0%}, momentum aligned, range=${daily_range:,.0f})",
            market_ticker="",
        )

    log.info(f"Best opportunity: {best_signal.reason}")
    return best_signal