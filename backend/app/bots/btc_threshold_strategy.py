# app/bots/btc_threshold_strategy.py
#
# Strategy based on Bitcoin's actual price vs the Kalshi market threshold.
# Much more logical than MACD for prediction markets.
#
# Logic:
#   - Parse the threshold from the market ticker (e.g. KXBTCD-26MAR2517-T80649.99)
#   - Fetch Bitcoin's current price from Kraken
#   - Calculate distance from threshold as a percentage
#   - BUY YES if BTC is comfortably above threshold
#   - BUY NO  if BTC is comfortably below threshold
#   - HOLD    if BTC is too close to the threshold (uncertain zone)

import ccxt
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# How far Bitcoin needs to be from threshold to trigger a trade (percentage)
# 5% = only trade when BTC is at least 5% above or below the threshold
# Higher = fewer but more confident trades
EDGE_THRESHOLD_PCT = 5.0

# How strong the signal is relative to distance
# 10% away = confidence 1.0, 5% away = confidence 0.5
MAX_EDGE_PCT = 10.0


@dataclass
class Signal:
    action:     str    # "BUY", "SELL", or "HOLD"
    price:      float  # suggested contract price
    confidence: float  # 0.0 to 1.0
    reason:     str


def parse_threshold(ticker: str) -> float:
    """
    Extracts the price threshold from a Kalshi ticker.
    e.g. "KXBTCD-26MAR2517-T80649.99" → 80649.99
    """
    try:
        # The threshold is after the last "-T"
        part = ticker.split("-T")[-1]
        return float(part)
    except Exception:
        raise ValueError(f"Could not parse threshold from ticker: {ticker}")


def fetch_btc_price() -> float:
    """Fetches current Bitcoin price from Kraken."""
    exchange = ccxt.kraken()
    ticker   = exchange.fetch_ticker("BTC/USDT")
    return float(ticker["last"])


def generate_signal(market_ticker: str, contract_price: float) -> Signal:
    """
    Main strategy logic.

    Args:
        market_ticker:  Kalshi ticker e.g. "KXBTCD-26MAR2517-T80649.99"
        contract_price: current YES contract price (0.0 to 1.0)
    """
    try:
        threshold = parse_threshold(market_ticker)
        btc_price = fetch_btc_price()
    except Exception as e:
        return Signal(action="HOLD", price=contract_price, confidence=0, reason=f"Data error: {e}")

    distance_pct = ((btc_price - threshold) / threshold) * 100
    abs_distance  = abs(distance_pct)

    log.info(f"BTC=${btc_price:,.2f}  threshold=${threshold:,.2f}  distance={distance_pct:+.2f}%")

    # Too close to threshold — too uncertain to trade
    if abs_distance < EDGE_THRESHOLD_PCT:
        return Signal(
            action="HOLD",
            price=contract_price,
            confidence=0,
            reason=f"BTC too close to threshold ({distance_pct:+.2f}% — need >{EDGE_THRESHOLD_PCT}%)",
        )

    confidence = min(abs_distance / MAX_EDGE_PCT, 1.0)

    if distance_pct > 0:
        # BTC is above threshold — YES contract should resolve YES
        # Buy YES if it's underpriced (below 0.85)
        if contract_price < 0.85:
            return Signal(
                action="BUY",
                price=contract_price,
                confidence=confidence,
                reason=f"BTC {distance_pct:+.2f}% above threshold — YES likely, contract at {contract_price:.2f}",
            )
        return Signal(
            action="HOLD",
            price=contract_price,
            confidence=0,
            reason=f"BTC above threshold but contract already fairly priced at {contract_price:.2f}",
        )
    else:
        # BTC is below threshold — YES contract should resolve NO
        # Sell YES (or buy NO) if it's overpriced (above 0.15)
        if contract_price > 0.15:
            return Signal(
                action="SELL",
                price=contract_price,
                confidence=confidence,
                reason=f"BTC {distance_pct:+.2f}% below threshold — NO likely, contract at {contract_price:.2f}",
            )
        return Signal(
            action="HOLD",
            price=contract_price,
            confidence=0,
            reason=f"BTC below threshold but contract already fairly priced at {contract_price:.2f}",
        )


async def find_best_market(client) -> tuple[str, float]:
    """
    Finds the open KXBTCD market whose threshold is closest to
    current Bitcoin price. Returns (ticker, threshold).
    """
    import httpx

    btc_price = fetch_btc_price()
    log.info(f"Finding best market — BTC=${btc_price:,.2f}")

    async with httpx.AsyncClient() as http:
        r = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            params={"limit": 100, "status": "open", "series_ticker": "KXBTCD"},
        )
        markets = r.json().get("markets", [])

    best_ticker    = None
    best_threshold = None
    best_distance  = float("inf")

    for m in markets:
        ticker = m.get("ticker", "")
        try:
            threshold = parse_threshold(ticker)
        except ValueError:
            continue
        distance = abs(btc_price - threshold)
        if distance < best_distance:
            best_distance  = distance
            best_ticker    = ticker
            best_threshold = threshold

    if not best_ticker:
        raise ValueError("No suitable KXBTCD market found")

    log.info(f"Best market: {best_ticker} (threshold=${best_threshold:,.2f}, distance=${best_distance:,.2f})")
    return best_ticker, best_threshold