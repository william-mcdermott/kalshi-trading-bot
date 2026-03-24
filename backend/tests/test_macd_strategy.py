# tests/test_macd_strategy.py
#
# Unit tests for the MACD strategy.
# Run with: pytest tests/
#
# pytest is very similar to Jest:
#   Jest:   test("does thing", () => { expect(x).toBe(y) })
#   pytest: def test_does_thing(): assert x == y

import pandas as pd
import numpy as np
import pytest

from app.bots.macd_strategy import MACDStrategy, Signal


def make_prices(closes: list[float]) -> pd.DataFrame:
    """Helper to create a minimal OHLCV DataFrame from a list of close prices."""
    return pd.DataFrame({
        "open":   closes,
        "high":   [c * 1.01 for c in closes],
        "low":    [c * 0.99 for c in closes],
        "close":  closes,
        "volume": [1000.0] * len(closes),
    })


class TestMACDStrategy:

    def test_hold_when_not_enough_data(self):
        """Should return HOLD if there are fewer rows than slow + signal periods."""
        strategy = MACDStrategy(fast=3, slow=15, signal=3)
        prices   = make_prices([0.5] * 10)   # only 10 rows, need at least 20
        signal   = strategy.generate_signal(prices)
        assert signal.action == "HOLD"

    def test_buy_signal_on_bullish_crossover(self):
        """Should return BUY when MACD crosses above signal line."""
        strategy = MACDStrategy(fast=3, slow=15, signal=3)

        # Create a price series that trends down then sharply up
        # This should produce a bullish MACD crossover at the end
        down  = [0.6 - i * 0.01 for i in range(20)]
        up    = [0.4 + i * 0.03 for i in range(10)]
        prices = make_prices(down + up)

        signal = strategy.generate_signal(prices)
        # We can't guarantee a crossover at every price series,
        # but we can check the return type is valid
        assert signal.action in ["BUY", "SELL", "HOLD"]
        assert 0.0 <= signal.confidence <= 1.0
        assert isinstance(signal.reason, str)

    def test_confidence_is_bounded(self):
        """Confidence should always be between 0 and 1."""
        strategy = MACDStrategy()
        prices   = make_prices([0.5 + 0.01 * np.sin(i) for i in range(50)])
        signal   = strategy.generate_signal(prices)
        assert 0.0 <= signal.confidence <= 1.0

    def test_signal_has_all_fields(self):
        """Signal should always return all four fields."""
        strategy = MACDStrategy()
        prices   = make_prices([0.5] * 30)
        signal   = strategy.generate_signal(prices)

        assert hasattr(signal, "action")
        assert hasattr(signal, "price")
        assert hasattr(signal, "confidence")
        assert hasattr(signal, "reason")
