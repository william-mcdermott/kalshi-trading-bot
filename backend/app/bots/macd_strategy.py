# app/bots/macd_strategy.py
#
# MACD strategy — generates BUY/SELL signals based on MACD crossovers.
# This file is pure logic: given price data, return a signal.
# It doesn't place orders — that's the trader's job (keeps things testable).

import pandas as pd
from dataclasses import dataclass
from app.bots.indicators import macd as calc_macd
from typing import Optional


@dataclass
class Signal:
    """
    A trading signal produced by a strategy.
    Dataclasses in Python are like TypeScript interfaces with default values.
    """
    action:     str            # "BUY", "SELL", or "HOLD"
    price:      float          # suggested entry price
    confidence: float          # 0.0 to 1.0 — how strong the signal is
    reason:     str            # human-readable explanation (useful for logs)


class MACDStrategy:
    """
    MACD Histogram strategy.

    Logic:
      - BUY  when MACD line crosses ABOVE the signal line (momentum turning positive)
      - SELL when MACD line crosses BELOW the signal line (momentum turning negative)
      - HOLD otherwise

    Parameters:
      fast=3, slow=15, signal=3  (shorter windows than classic 12/26/9
      because Polymarket markets move on shorter timeframes)
    """

    def __init__(self, fast: int = 3, slow: int = 15, signal: int = 3):
        self.fast   = fast
        self.slow   = slow
        self.signal = signal

    def generate_signal(self, prices: pd.DataFrame) -> Signal:
        """
        Takes a DataFrame of OHLCV price data and returns a Signal.

        Args:
            prices: DataFrame with columns: open, high, low, close, volume
                    Each row is one time period (e.g. 5 minutes)
                    Must have at least `slow + signal` rows to calculate MACD

        Returns:
            Signal with action BUY, SELL, or HOLD
        """
        if len(prices) < self.slow + self.signal + 2:
            return Signal(action="HOLD", price=0, confidence=0, reason="Not enough data")

        # Calculate MACD using our own indicators module
        macd_line, signal_line, _ = calc_macd(prices["close"], fast=self.fast, slow=self.slow, signal=self.signal)

        if macd_line.empty:
            return Signal(action="HOLD", price=0, confidence=0, reason="MACD calculation failed")

        # Get the last two values to detect a crossover
        prev_macd   = macd_line.iloc[-2]
        curr_macd   = macd_line.iloc[-1]
        prev_signal = signal_line.iloc[-2]
        curr_signal = signal_line.iloc[-1]
        curr_price  = prices["close"].iloc[-1]

        # Bullish crossover: MACD was below signal, now above
        if prev_macd < prev_signal and curr_macd > curr_signal:
            gap = abs(curr_macd - curr_signal)
            return Signal(
                action="BUY",
                price=curr_price,
                confidence=min(gap * 10, 1.0),   # larger gap = more confident
                reason=f"MACD crossed above signal line (gap: {gap:.4f})",
            )

        # Bearish crossover: MACD was above signal, now below
        if prev_macd > prev_signal and curr_macd < curr_signal:
            gap = abs(curr_macd - curr_signal)
            return Signal(
                action="SELL",
                price=curr_price,
                confidence=min(gap * 10, 1.0),
                reason=f"MACD crossed below signal line (gap: {gap:.4f})",
            )

        return Signal(action="HOLD", price=curr_price, confidence=0, reason="No crossover")
