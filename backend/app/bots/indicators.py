# app/bots/indicators.py
#
# MACD, RSI, and VWAP calculated with plain pandas.
# No external indicator library needed — this is just maths.
# Each function takes a pandas Series and returns a Series.

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average — the building block for MACD."""
    return series.ewm(span=period, adjust=False).mean()


def macd(close: pd.Series, fast=3, slow=15, signal=3):
    """
    Returns (macd_line, signal_line, histogram) as three Series.

    MACD line  = fast EMA - slow EMA
    Signal     = EMA of the MACD line
    Histogram  = MACD line - signal line
    """
    fast_ema   = ema(close, fast)
    slow_ema   = ema(close, slow)
    macd_line  = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


def rsi(close: pd.Series, period=14) -> pd.Series:
    """
    Relative Strength Index (0-100).
    Above 70 = overbought, below 30 = oversold.
    """
    delta  = close.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs     = avg_gain / avg_loss.replace(0, float('inf'))
    return 100 - (100 / (1 + rs))


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    Volume Weighted Average Price.
    Typical price weighted by volume — a fair-value reference.
    """
    typical_price = (high + low + close) / 3
    return (typical_price * volume).cumsum() / volume.cumsum()
