# app/bots/btc_threshold_strategy.py
import ccxt
import logging
import statistics
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

TARGET_DISTANCE  = 800.0
MIN_MOMENTUM_PCT = 0.3
MAX_BUY_PRICE    = 0.40
MIN_SELL_PRICE   = 0.15
MIN_DAILY_RANGE  = 1000.0

# RSI filter — only SELL when RSI > 55 (not oversold)
RSI_PERIOD       = 14
RSI_SELL_MIN     = 55   # don't sell into oversold conditions
RSI_BUY_MAX      = 45   # don't buy into overbought conditions


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


def fetch_daily_range() -> float:
    exchange = ccxt.kraken()
    ohlcv    = exchange.fetch_ohlcv("BTC/USDT", "1d", limit=2)
    if not ohlcv:
        return 0.0
    today = ohlcv[-1]
    return float(today[2]) - float(today[3])


def calculate_momentum(prices: list[float]) -> float:
    if len(prices) < 2:
        return 0.0
    n      = len(prices)
    mean_x = (n - 1) / 2
    mean_y = statistics.mean(prices)
    num    = sum((i - mean_x) * (prices[i] - mean_y) for i in range(n))
    den    = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    slope_per_candle = num / den
    slope_per_hour   = slope_per_candle * 12
    return (slope_per_hour / prices[0]) * 100


def calculate_rsi(prices: list[float], period: int = RSI_PERIOD) -> float:
    """
    RSI via Wilder's smoothing method.
    Returns 0-100. Above 70 = overbought, below 30 = oversold.
    Needs at least period+1 prices.
    """
    if len(prices) < period + 1:
        return 50.0  # neutral if not enough data

    gains  = []
    losses = []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    # First average
    avg_gain = statistics.mean(gains[:period])
    avg_loss = statistics.mean(losses[:period])

    # Wilder smoothing for remaining
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs  = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def fetch_btc_history_long(n_candles: int = 30) -> list[float]:
    """Fetch more candles for RSI calculation."""
    exchange = ccxt.kraken()
    ohlcv    = exchange.fetch_ohlcv("BTC/USDT", "5m", limit=n_candles)
    return [candle[4] for candle in ohlcv]


def parse_threshold(ticker: str) -> float:
    try:
        return float(ticker.split("-T")[-1])
    except Exception:
        raise ValueError(f"Could not parse threshold from ticker: {ticker}")


async def find_directional_market(client, btc_price: float, bullish: bool) -> Optional[tuple[str, float]]:
    import httpx

    target = btc_price + TARGET_DISTANCE if bullish else btc_price - TARGET_DISTANCE

    async with httpx.AsyncClient(timeout=10.0) as http:
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

        yes_bid = m.get("yes_bid_dollars")
        yes_ask = m.get("yes_ask_dollars")

        if yes_bid is None or yes_ask is None:
            mid = 0.50
        else:
            mid = (float(yes_bid) + float(yes_ask)) / 2

        if mid >= 0.95 or mid <= 0.05:
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
        raise ValueError("No valid markets available — may be near settlement")

    return result


def generate_signal(market_ticker: str, contract_price: float) -> Signal:
    try:
        threshold  = parse_threshold(market_ticker)
        # Fetch enough candles for RSI (need RSI_PERIOD + momentum lookback)
        prices     = fetch_btc_history_long(RSI_PERIOD + 12)
        btc_price  = prices[-1]
        momentum   = calculate_momentum(prices[-12:])
        rsi        = calculate_rsi(prices)
    except Exception as e:
        return Signal(action="HOLD", price=contract_price, confidence=0, reason=f"Data error: {e}")

    distance_pct = ((btc_price - threshold) / threshold) * 100
    abs_momentum = abs(momentum)

    log.info(
        f"BTC=${btc_price:,.2f}  threshold=${threshold:,.2f}  "
        f"distance={distance_pct:+.2f}%  momentum={momentum:+.2f}%/hr  RSI={rsi:.1f}"
    )

    daily_range = fetch_daily_range()
    if daily_range < MIN_DAILY_RANGE:
        return Signal(
            action="HOLD", price=contract_price, confidence=0,
            reason=f"Low volatility (range=${daily_range:,.0f} — need >${MIN_DAILY_RANGE:,.0f})",
        )

    if contract_price >= 0.95 or contract_price <= 0.05:
        return Signal(
            action="HOLD", price=contract_price, confidence=0,
            reason=f"Contract already resolved ({contract_price:.2f})",
        )

    if abs_momentum < MIN_MOMENTUM_PCT:
        return Signal(
            action="HOLD", price=contract_price, confidence=0,
            reason=f"Momentum too weak ({momentum:+.2f}%/hr — need >{MIN_MOMENTUM_PCT}%/hr)",
        )

    confidence = min(abs_momentum / 2.0, 1.0)

    if momentum > 0:
        # BUY disabled — losing in backtest
        return Signal(
            action="HOLD", price=contract_price, confidence=0,
            reason=f"BUY disabled (insufficient edge in backtesting)",
        )
    else:
        # SELL signal — confirm with RSI
        if rsi < RSI_SELL_MIN:
            return Signal(
                action="HOLD", price=contract_price, confidence=0,
                reason=f"Bearish momentum but RSI={rsi:.1f} too low (need >{RSI_SELL_MIN}) — may be oversold",
            )
        if contract_price < MIN_SELL_PRICE:
            return Signal(
                action="HOLD", price=contract_price, confidence=0,
                reason=f"Bearish but contract too cheap at {contract_price:.2f}",
            )
        return Signal(
            action="SELL",
            price=contract_price,
            confidence=confidence,
            reason=(
                f"Bearish {momentum:+.2f}%/hr RSI={rsi:.1f} — "
                f"selling YES on ${threshold:,.2f} at {contract_price:.2f}"
            ),
        )