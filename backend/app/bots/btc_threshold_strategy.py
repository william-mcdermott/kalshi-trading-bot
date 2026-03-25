import ccxt
import logging
import statistics
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

TARGET_DISTANCE  = 800.0
MIN_MOMENTUM_PCT = 0.2
MAX_BUY_PRICE    = 0.40
MIN_SELL_PRICE   = 0.15


@dataclass
class Signal:
    action:     str
    price:      float
    confidence: float
    reason:     str


def fetch_btc_history(n_candles: int = 12) -> list[float]:
    exchange = ccxt.kraken()
    ohlcv    = exchange.fetch_ohlcv("BTC/USDT", "5m", limit=n_candles)
    return [candle[4] for candle in ohlcv]


def fetch_btc_price() -> float:
    exchange = ccxt.kraken()
    ticker   = exchange.fetch_ticker("BTC/USDT")
    return float(ticker["last"])


def calculate_momentum(prices: list[float]) -> float:
    if len(prices) < 2:
        return 0.0
    n      = len(prices)
    mean_x = (n - 1) / 2
    mean_y = statistics.mean(prices)
    numerator   = sum((i - mean_x) * (prices[i] - mean_y) for i in range(n))
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    if denominator == 0:
        return 0.0
    slope_per_candle = numerator / denominator
    slope_per_hour   = slope_per_candle * 12
    pct_per_hour     = (slope_per_hour / prices[0]) * 100
    return pct_per_hour


def parse_threshold(ticker: str) -> float:
    try:
        return float(ticker.split("-T")[-1])
    except Exception:
        raise ValueError(f"Could not parse threshold from ticker: {ticker}")


async def find_directional_market(client, btc_price: float, bullish: bool) -> Optional[tuple[str, float]]:
    import httpx

    target = btc_price + TARGET_DISTANCE if bullish else btc_price - TARGET_DISTANCE

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

        if bullish and threshold <= btc_price:
            continue
        if not bullish and threshold >= btc_price:
            continue

        # Filter out contracts that have already effectively resolved
        yes_bid = m.get("yes_bid_dollars")
        yes_ask = m.get("yes_ask_dollars")

        if yes_bid is None or yes_ask is None:
            mid = 0.50  # brand new market, treat as uncertain
        else:
            mid = (float(yes_bid) + float(yes_ask)) / 2

        if mid >= 0.95 or mid <= 0.05:
            log.debug(f"Skipping {ticker} — already resolved (mid={mid:.2f})")
            continue

        distance = abs(target - threshold)
        if distance < best_distance:
            best_distance  = distance
            best_ticker    = ticker
            best_threshold = threshold

    if not best_ticker:
        return None

    log.info(f"Target=${target:,.2f}  best market: {best_ticker} (threshold=${best_threshold:,.2f})")
    return best_ticker, best_threshold


async def find_best_market(client) -> tuple[str, float]:
    prices    = fetch_btc_history(12)
    btc_price = prices[-1]
    momentum  = calculate_momentum(prices)

    log.info(f"BTC=${btc_price:,.2f}  momentum={momentum:+.2f}%/hr")

    bullish = momentum > 0
    result  = await find_directional_market(client, btc_price, bullish)

    if not result:
        log.warning("No market found in primary direction, trying opposite")
        result = await find_directional_market(client, btc_price, not bullish)

    if not result:
        raise ValueError("No valid markets available on either side — markets may be near settlement")

    return result


def generate_signal(market_ticker: str, contract_price: float) -> Signal:
    try:
        threshold = parse_threshold(market_ticker)
        prices    = fetch_btc_history(12)
        btc_price = prices[-1]
        momentum  = calculate_momentum(prices)
    except Exception as e:
        return Signal(action="HOLD", price=contract_price, confidence=0, reason=f"Data error: {e}")

    distance_pct = ((btc_price - threshold) / threshold) * 100
    abs_momentum = abs(momentum)

    log.info(
        f"BTC=${btc_price:,.2f}  threshold=${threshold:,.2f}  "
        f"distance={distance_pct:+.2f}%  momentum={momentum:+.2f}%/hr"
    )

    # Skip contracts that have already effectively resolved
    if contract_price >= 0.95 or contract_price <= 0.05:
        return Signal(
            action="HOLD",
            price=contract_price,
            confidence=0,
            reason=f"Contract already resolved ({contract_price:.2f}) — no edge available",
        )

    # Not enough momentum to trade
    if abs_momentum < MIN_MOMENTUM_PCT:
        return Signal(
            action="HOLD",
            price=contract_price,
            confidence=0,
            reason=f"Momentum too weak ({momentum:+.2f}%/hr — need >{MIN_MOMENTUM_PCT}%/hr)",
        )

    confidence = min(abs_momentum / 2.0, 1.0)

    if momentum > 0:
        if contract_price > MAX_BUY_PRICE:
            return Signal(
                action="HOLD",
                price=contract_price,
                confidence=0,
                reason=f"Bullish but contract too expensive at {contract_price:.2f} (max {MAX_BUY_PRICE})",
            )
        return Signal(
            action="BUY",
            price=contract_price,
            confidence=confidence,
            reason=f"Bullish {momentum:+.2f}%/hr — buying YES on ${threshold:,.2f} market at {contract_price:.2f}",
        )
    else:
        if contract_price < MIN_SELL_PRICE:
            return Signal(
                action="HOLD",
                price=contract_price,
                confidence=0,
                reason=f"Bearish but contract too cheap at {contract_price:.2f} (min {MIN_SELL_PRICE})",
            )
        return Signal(
            action="SELL",
            price=contract_price,
            confidence=confidence,
            reason=f"Bearish {momentum:+.2f}%/hr — selling YES on ${threshold:,.2f} market at {contract_price:.2f}",
        )