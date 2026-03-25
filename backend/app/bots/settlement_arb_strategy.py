# app/bots/settlement_arb_strategy.py
#
# Settlement arbitrage strategy.
#
# Finds Kalshi BTC markets where the contract price is significantly
# different from the fair value based on current BTC price and
# time to settlement.
#
# Edge sources:
#   - Low liquidity on outer strikes
#   - Wide bid/ask spreads
#   - Retail traders pricing in too much uncertainty
#
# Only trades in the last 6 hours before settlement when
# the probability model is most accurate.

import math
import logging
import statistics
from dataclasses import dataclass
from typing import Optional
from app.bots.btc_threshold_strategy import fetch_btc_history, calculate_momentum

import ccxt
import httpx

log = logging.getLogger(__name__)

# Minimum edge required to trade (fair value - market price)
MIN_EDGE          = 0.12   # 12¢ minimum edge

# Only trade within this many hours of settlement
MAX_HOURS_TO_SETTLEMENT = 6.0

# Don't trade contracts already near resolution
MIN_CONTRACT_PRICE = 0.05
MAX_CONTRACT_PRICE = 0.95

# BTC hourly volatility assumption (%)
BTC_VOLATILITY_PCT = 2.5


@dataclass
class Signal:
    action:          str    # "BUY", "SELL", or "HOLD"
    price:           float  # current contract price
    fair_value:      float  # estimated fair value
    edge:            float  # fair_value - price (positive = underpriced)
    confidence:      float  # 0.0 to 1.0
    reason:          str
    market_ticker:   str


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
    """
    Estimates fair value of a YES contract using normal distribution.
    The contract pays $1 if BTC >= threshold at settlement.
    """
    if hours_to_settlement <= 0:
        return 1.0 if btc_price >= threshold else 0.0
    if hours_to_settlement > 24:
        return 0.5  # too far out to price accurately

    distance_pct = (btc_price - threshold) / threshold * 100
    total_vol    = volatility_pct * math.sqrt(hours_to_settlement)
    z            = distance_pct / total_vol
    return round(normal_cdf(z), 4)


def parse_threshold(ticker: str) -> float:
    try:
        return float(ticker.split("-T")[-1])
    except Exception:
        raise ValueError(f"Could not parse threshold from ticker: {ticker}")


def fetch_btc_price() -> float:
    exchange = ccxt.kraken()
    ticker   = exchange.fetch_ticker("BTC/USDT")
    return float(ticker["last"])


async def scan_markets(hours_to_settlement: float) -> list[dict]:
    """
    Fetches all open KXBTCD markets and returns their current prices.
    """
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
    """
    Scans all markets and returns the best mispricing opportunity.
    Adds directional filter — only trades WITH momentum direction.
    """
    if hours_to_settlement > MAX_HOURS_TO_SETTLEMENT:
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"Too early — {hours_to_settlement:.1f}hrs to settlement",
            market_ticker="",
        )

    # Get BTC price and momentum
    prices    = fetch_btc_history(12)
    btc_price = prices[-1]
    momentum  = calculate_momentum(prices)

    log.info(f"Settlement arb scan — BTC=${btc_price:,.2f} momentum={momentum:+.2f}%/hr")

    markets = await scan_markets(hours_to_settlement)

    best_signal   = None
    best_abs_edge = 0.0

    for m in markets:
        fv   = fair_value(btc_price, m["threshold"], hours_to_settlement)
        edge = fv - m["mid"]

        if abs(edge) < MIN_EDGE:
            continue

        # Directional filter — only trade WITH momentum
        if edge > 0 and momentum < 0:
            # Contract is underpriced (BUY YES) but BTC is falling — skip
            log.debug(f"Skipping BUY on {m['ticker']} — bearish momentum ({momentum:+.2f}%/hr)")
            continue
        if edge < 0 and momentum > 0:
            # Contract is overpriced (SELL YES) but BTC is rising — skip
            log.debug(f"Skipping SELL on {m['ticker']} — bullish momentum ({momentum:+.2f}%/hr)")
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
                f"momentum={momentum:+.2f}%/hr"
            ),
            market_ticker = m["ticker"],
        )

        if abs(edge) > best_abs_edge:
            best_abs_edge = abs(edge)
            best_signal   = signal

    if best_signal is None:
        return Signal(
            action="HOLD", price=0, fair_value=0, edge=0, confidence=0,
            reason=f"No opportunity found (edge>{MIN_EDGE:.0%}, aligned with momentum={momentum:+.2f}%/hr)",
            market_ticker="",
        )

    return best_signal