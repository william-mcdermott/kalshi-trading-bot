# backtesting/analyze.py
#
# Analyzes what actually predicts trade outcomes in historical data.
# Run with: python -m backtesting.analyze

import sys
import math
import statistics
from dataclasses import dataclass, field

import ccxt
import pandas as pd

# Reuse the backtest engine
from backtesting.backtest import (
    run_backtest,
    SimulatedTrade,
    calculate_momentum,
    fair_value,
    LOOKBACK,
    BTC_VOLATILITY,
)


def fetch_candles(days: int = 30) -> list:
    print(f"Fetching {days} days of BTC/USDT 1-hour candles from Kraken...")
    exchange = ccxt.kraken()
    since    = exchange.parse8601(
        (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days))
        .strftime('%Y-%m-%dT%H:%M:%SZ')
    )
    candles = exchange.fetch_ohlcv('BTC/USDT', '1h', since=since, limit=720)
    print(f"Got {len(candles)} candles ({len(candles)/24:.1f} days)\n")
    return candles


def analyze_trades(trades: list[SimulatedTrade]):
    """Breaks down what separates winning trades from losing trades."""

    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    print(f"{'='*60}")
    print(f"TRADE ANALYSIS — {len(trades)} trades ({len(wins)} wins, {len(losses)} losses)")
    print(f"{'='*60}\n")

    def mean(vals):
        return statistics.mean(vals) if vals else 0

    def pct(n, total):
        return f"{n/total*100:.0f}%" if total else "0%"

    # ── Momentum analysis ──────────────────────────────────────────────
    print("MOMENTUM STRENGTH")
    print("-" * 40)
    buckets = [
        ("weak   (0.2-0.4%)", lambda t: 0.2 <= abs(t.momentum) < 0.4),
        ("medium (0.4-0.6%)", lambda t: 0.4 <= abs(t.momentum) < 0.6),
        ("strong (0.6-1.0%)", lambda t: 0.6 <= abs(t.momentum) < 1.0),
        ("strong (>1.0%)",    lambda t: abs(t.momentum) >= 1.0),
    ]
    for label, fn in buckets:
        group = [t for t in trades if fn(t)]
        if not group: continue
        w = sum(1 for t in group if t.pnl > 0)
        pnl = sum(t.pnl for t in group)
        print(f"  {label:<25} {len(group):>3} trades  win={pct(w,len(group)):>5}  P&L=${pnl:+.4f}")

    # ── Hours to settlement ────────────────────────────────────────────
    print("\nHOURS TO SETTLEMENT")
    print("-" * 40)
    hour_buckets = [
        (">8hrs  (early)",   lambda t: t.hours_left > 8),
        ("4-8hrs (mid)",     lambda t: 4 < t.hours_left <= 8),
        ("2-4hrs (late)",    lambda t: 2 < t.hours_left <= 4),
        ("<2hrs  (final)",   lambda t: t.hours_left <= 2),
    ]
    for label, fn in hour_buckets:
        group = [t for t in trades if fn(t)]
        if not group: continue
        w   = sum(1 for t in group if t.pnl > 0)
        pnl = sum(t.pnl for t in group)
        print(f"  {label:<25} {len(group):>3} trades  win={pct(w,len(group)):>5}  P&L=${pnl:+.4f}")

    # ── Distance from threshold ────────────────────────────────────────
    print("\nDISTANCE FROM THRESHOLD")
    print("-" * 40)
    dist_buckets = [
        ("<$500",    lambda t: abs(t.btc_at_entry - t.threshold) < 500),
        ("$500-1k",  lambda t: 500 <= abs(t.btc_at_entry - t.threshold) < 1000),
        ("$1k-2k",   lambda t: 1000 <= abs(t.btc_at_entry - t.threshold) < 2000),
        (">$2k",     lambda t: abs(t.btc_at_entry - t.threshold) >= 2000),
    ]
    for label, fn in dist_buckets:
        group = [t for t in trades if fn(t)]
        if not group: continue
        w   = sum(1 for t in group if t.pnl > 0)
        pnl = sum(t.pnl for t in group)
        print(f"  {label:<25} {len(group):>3} trades  win={pct(w,len(group)):>5}  P&L=${pnl:+.4f}")

    # ── Time of day ────────────────────────────────────────────────────
    print("\nTIME OF DAY (UTC)")
    print("-" * 40)
    time_buckets = [
        ("00-06 UTC (night)",  lambda t: 0  <= t.timestamp.hour < 6),
        ("06-12 UTC (morning)",lambda t: 6  <= t.timestamp.hour < 12),
        ("12-18 UTC (midday)", lambda t: 12 <= t.timestamp.hour < 18),
        ("18-24 UTC (evening)",lambda t: 18 <= t.timestamp.hour < 24),
    ]
    for label, fn in time_buckets:
        group = [t for t in trades if fn(t)]
        if not group: continue
        w   = sum(1 for t in group if t.pnl > 0)
        pnl = sum(t.pnl for t in group)
        print(f"  {label:<25} {len(group):>3} trades  win={pct(w,len(group)):>5}  P&L=${pnl:+.4f}")

    # ── BUY vs SELL ────────────────────────────────────────────────────
    print("\nBUY vs SELL")
    print("-" * 40)
    for side in ["BUY", "SELL"]:
        group = [t for t in trades if t.side == side]
        if not group: continue
        w   = sum(1 for t in group if t.pnl > 0)
        pnl = sum(t.pnl for t in group)
        print(f"  {side:<25} {len(group):>3} trades  win={pct(w,len(group)):>5}  P&L=${pnl:+.4f}")

    # ── Volatility regime ──────────────────────────────────────────────
    print("\nAVG HOURLY RANGE (volatility proxy)")
    print("-" * 40)
    # Use btc_at_entry vs btc_at_settlement as a proxy
    for t in trades:
        t._btc_move = abs(t.btc_at_settlement - t.btc_at_entry)

    vol_buckets = [
        ("<$500 move",   lambda t: t._btc_move < 500),
        ("$500-1k move", lambda t: 500 <= t._btc_move < 1000),
        ("$1k-2k move",  lambda t: 1000 <= t._btc_move < 2000),
        (">$2k move",    lambda t: t._btc_move >= 2000),
    ]
    for label, fn in vol_buckets:
        group = [t for t in trades if fn(t)]
        if not group: continue
        w   = sum(1 for t in group if t.pnl > 0)
        pnl = sum(t.pnl for t in group)
        print(f"  {label:<25} {len(group):>3} trades  win={pct(w,len(group)):>5}  P&L=${pnl:+.4f}")

    # ── Best combinations ──────────────────────────────────────────────
    print("\nBEST COMBINATIONS (win rate)")
    print("-" * 40)
    combos = []
    for side in ["BUY", "SELL"]:
        for hr_label, hr_fn in hour_buckets:
            for mom_label, mom_fn in buckets:
                group = [t for t in trades if t.side == side and hr_fn(t) and mom_fn(t)]
                if len(group) < 2: continue
                w   = sum(1 for t in group if t.pnl > 0)
                pnl = sum(t.pnl for t in group)
                combos.append((w/len(group), len(group), pnl, side, hr_label, mom_label))

    combos.sort(reverse=True)
    for win_rate, n, pnl, side, hr_label, mom_label in combos[:8]:
        print(f"  {side} {hr_label} {mom_label:<20} "
              f"{n:>2} trades  win={win_rate*100:.0f}%  P&L=${pnl:+.4f}")

    print(f"\n{'='*60}")


if __name__ == "__main__":
    days    = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    candles = fetch_candles(days)

    # Run with loose parameters to get maximum trades for analysis
    results = run_backtest(days=days, min_edge=0.05, min_momentum=0.2, candles=candles)

    print(f"Analyzing {results.total_trades} trades...\n")
    analyze_trades(results.trades)