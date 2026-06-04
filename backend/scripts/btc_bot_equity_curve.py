#!/usr/bin/env python3
"""
BTC Bot Equity Curve & Drawdown Analysis

Generates a visual equity curve with drawdown shading, plus the worst
losing streaks in pre-fix and post-fix windows. The point: gut-check
whether you can sit through a bad run at higher size.

Usage:
    python btc_bot_equity_curve.py --cutoff 2026-04-15
    python btc_bot_equity_curve.py --cutoff 2026-04-15 --size-multiplier 2
    python btc_bot_equity_curve.py --cutoff 2026-04-15 --output equity.png

The --size-multiplier flag scales all P&L by that factor so you can
preview what the curve would have looked like at a different size.

Adjust DEFAULT_DB_PATH if your DB lives elsewhere.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except ImportError:
    print("ERROR: matplotlib not installed. Run: pip install matplotlib", file=sys.stderr)
    sys.exit(1)

DEFAULT_DB_PATH = Path.home() / "Developer/code/kalshi-trading-bot/backend/data/bot.db"


def parse_ts(ts):
    if isinstance(ts, (int, float)):
        if ts > 1e12:
            ts = ts / 1000
        return datetime.fromtimestamp(ts)
    if isinstance(ts, str):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(ts.split("+")[0].split("Z")[0], fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(ts.replace("Z", ""))
        except ValueError:
            pass
    raise ValueError(f"Could not parse timestamp: {ts!r}")


def fetch_trades(db_path, strategy):
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT created_at, pnl, side, price, edge
        FROM trades
        WHERE strategy = ? AND filled = 1
        ORDER BY created_at ASC
        """,
        (strategy,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def build_equity_series(trades, multiplier=1.0):
    """Build (timestamps, pnls, equity, peaks, drawdowns) from raw trades."""
    timestamps = []
    pnls = []
    equity = []
    peaks = []
    drawdowns = []

    running = 0.0
    peak = 0.0
    for created_at, pnl, *_ in trades:
        try:
            dt = parse_ts(created_at)
        except ValueError:
            continue
        if pnl is None:
            continue
        scaled = float(pnl) * multiplier
        running += scaled
        peak = max(peak, running)
        timestamps.append(dt)
        pnls.append(scaled)
        equity.append(running)
        peaks.append(peak)
        drawdowns.append(peak - running)

    return timestamps, pnls, equity, peaks, drawdowns


def find_worst_streak(timestamps, equity, peaks, drawdowns):
    """Identify the peak-to-trough drawdown and how long recovery took."""
    if not drawdowns:
        return None
    worst_idx = drawdowns.index(max(drawdowns))
    # Find the peak that started this drawdown (walk backward to where equity == peak)
    peak_idx = worst_idx
    while peak_idx > 0 and equity[peak_idx] < peaks[worst_idx]:
        peak_idx -= 1
    # Find recovery: first index after worst_idx where equity reaches peak again
    recovery_idx = None
    for i in range(worst_idx + 1, len(equity)):
        if equity[i] >= peaks[worst_idx]:
            recovery_idx = i
            break
    return {
        "peak_idx": peak_idx,
        "trough_idx": worst_idx,
        "recovery_idx": recovery_idx,
        "peak_time": timestamps[peak_idx],
        "trough_time": timestamps[worst_idx],
        "recovery_time": timestamps[recovery_idx] if recovery_idx else None,
        "drawdown": drawdowns[worst_idx],
        "trades_to_trough": worst_idx - peak_idx,
        "trades_to_recover": (recovery_idx - worst_idx) if recovery_idx else None,
    }


def find_longest_losing_streak(pnls):
    """Longest run of consecutive losing trades."""
    longest = 0
    current = 0
    longest_start = 0
    longest_end = 0
    cur_start = 0
    for i, p in enumerate(pnls):
        if p <= 0:
            if current == 0:
                cur_start = i
            current += 1
            if current > longest:
                longest = current
                longest_start = cur_start
                longest_end = i
        else:
            current = 0
    return {"length": longest, "start_idx": longest_start, "end_idx": longest_end}


def plot_equity(timestamps, equity, peaks, drawdowns, cutoff, multiplier, output_path):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]})

    # Equity curve
    ax1.plot(timestamps, equity, color="#2962FF", linewidth=1.5, label="Equity")
    ax1.fill_between(timestamps, equity, peaks, color="#FF5252", alpha=0.25, label="Drawdown")
    ax1.plot(timestamps, peaks, color="#888", linewidth=0.8, linestyle="--", alpha=0.6, label="Running peak")
    ax1.axhline(0, color="#000", linewidth=0.5, alpha=0.3)

    # Cutoff line
    cutoff_dt = datetime.fromisoformat(cutoff)
    ax1.axvline(cutoff_dt, color="#00C853", linewidth=1.5, linestyle=":", label=f"Fix cutoff ({cutoff})")

    title = "BTC Bot Equity Curve"
    if multiplier != 1.0:
        title += f" (size scaled {multiplier}x)"
    ax1.set_title(title, fontsize=13)
    ax1.set_ylabel("Cumulative P&L ($)")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(alpha=0.3)

    # Drawdown chart
    ax2.fill_between(timestamps, 0, [-d for d in drawdowns], color="#FF5252", alpha=0.5)
    ax2.axvline(cutoff_dt, color="#00C853", linewidth=1.5, linestyle=":")
    ax2.set_ylabel("Drawdown ($)")
    ax2.set_xlabel("Date")
    ax2.grid(alpha=0.3)

    # Date formatting
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    print(f"Chart saved to: {output_path}")


def report_window(label, timestamps, pnls, equity, peaks, drawdowns):
    if not pnls:
        print(f"\n{label}: no trades")
        return

    print(f"\n{label}")
    print("-" * len(label))
    print(f"  Trades:           {len(pnls)}")
    print(f"  Total P&L:        ${equity[-1]:+.2f}")
    print(f"  Peak equity:      ${max(peaks):+.2f}")
    print(f"  Max drawdown:     ${max(drawdowns):.2f}")

    streak = find_worst_streak(timestamps, equity, peaks, drawdowns)
    if streak and streak["drawdown"] > 0:
        print(f"\n  Worst drawdown stretch:")
        print(f"    Peak:      {streak['peak_time'].strftime('%Y-%m-%d %H:%M')}  (equity ${equity[streak['peak_idx']]:+.2f})")
        print(f"    Trough:    {streak['trough_time'].strftime('%Y-%m-%d %H:%M')}  (equity ${equity[streak['trough_idx']]:+.2f})")
        print(f"    Drawdown:  ${streak['drawdown']:.2f} over {streak['trades_to_trough']} trades")
        if streak["recovery_time"]:
            print(f"    Recovery:  {streak['recovery_time'].strftime('%Y-%m-%d %H:%M')}  ({streak['trades_to_recover']} trades to recover)")
        else:
            print(f"    Recovery:  not yet recovered")

    losing = find_longest_losing_streak(pnls)
    if losing["length"] > 0:
        print(f"\n  Longest losing streak: {losing['length']} consecutive losses")
        print(f"    From: {timestamps[losing['start_idx']].strftime('%Y-%m-%d %H:%M')}")
        print(f"    To:   {timestamps[losing['end_idx']].strftime('%Y-%m-%d %H:%M')}")
        streak_pnls = pnls[losing["start_idx"]:losing["end_idx"] + 1]
        print(f"    Total damage: ${sum(streak_pnls):.2f}")


def main():
    parser = argparse.ArgumentParser(description="BTC bot equity curve and drawdown analysis")
    parser.add_argument("--cutoff", required=True, help="ISO date for pre/post split, e.g. 2026-04-15")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to bot.db")
    parser.add_argument("--strategy", default="macd", help="Strategy name to filter on")
    parser.add_argument("--size-multiplier", type=float, default=1.0,
                        help="Scale all P&L by this factor (e.g. 2 to preview $2 size)")
    parser.add_argument("--output", default="btc_equity_curve.png", help="Chart output filename")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    rows = fetch_trades(db_path, args.strategy)
    if not rows:
        print(f"No trades found for strategy='{args.strategy}'")
        return

    timestamps, pnls, equity, peaks, drawdowns = build_equity_series(rows, args.size_multiplier)

    cutoff_dt = datetime.fromisoformat(args.cutoff)
    pre_idx = [i for i, t in enumerate(timestamps) if t < cutoff_dt]
    post_idx = [i for i, t in enumerate(timestamps) if t >= cutoff_dt]

    print("=" * 68)
    print(f"BTC BOT EQUITY ANALYSIS — strategy='{args.strategy}', cutoff={args.cutoff}")
    if args.size_multiplier != 1.0:
        print(f"  P&L scaled by {args.size_multiplier}x")
    print("=" * 68)

    if pre_idx:
        # Rebuild equity/peaks/drawdowns scoped to pre-fix only
        pre_pnls = [pnls[i] for i in pre_idx]
        pre_ts = [timestamps[i] for i in pre_idx]
        pre_eq = []
        pre_peak = []
        pre_dd = []
        running = 0.0
        peak = 0.0
        for p in pre_pnls:
            running += p
            peak = max(peak, running)
            pre_eq.append(running)
            pre_peak.append(peak)
            pre_dd.append(peak - running)
        report_window("PRE-FIX WINDOW", pre_ts, pre_pnls, pre_eq, pre_peak, pre_dd)

    if post_idx:
        post_pnls = [pnls[i] for i in post_idx]
        post_ts = [timestamps[i] for i in post_idx]
        post_eq = []
        post_peak = []
        post_dd = []
        running = 0.0
        peak = 0.0
        for p in post_pnls:
            running += p
            peak = max(peak, running)
            post_eq.append(running)
            post_peak.append(peak)
            post_dd.append(peak - running)
        report_window("POST-FIX WINDOW", post_ts, post_pnls, post_eq, post_peak, post_dd)

    print()
    plot_equity(timestamps, equity, peaks, drawdowns, args.cutoff, args.size_multiplier, args.output)


if __name__ == "__main__":
    main()
