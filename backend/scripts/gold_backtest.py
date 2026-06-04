#!/usr/bin/env python3
"""
gold_backtest.py

Validates gold_scanner_log.csv signals against actual settlement prices.

For each settled event it:
  1. Parses the settlement date from the event ticker (e.g. KXGOLDD-26APR0617 → Apr 06 2026)
  2. Fetches the gold futures close at 5pm EDT (21:00 UTC) via yfinance
  3. Scores every BUY/SELL signal: did gold close above/below the threshold?
  4. Prints accuracy by signal type, edge bucket, moneyness, and hours-to-settle
  5. Exports gold_backtest_results.csv with an 'outcome' column appended

Usage:
    python scripts/gold_backtest.py
    python scripts/gold_backtest.py --log path/to/gold_scanner_log.csv
    python scripts/gold_backtest.py --min-edge 0.08 --signal SELL
    python scripts/gold_backtest.py --min-vol 50   # only signals with vol_24h >= 50
"""

import argparse
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

DEFAULT_LOG = Path(__file__).parent / "gold_scanner_log.csv"
SETTLEMENT_HOUR_UTC = 21   # 5pm EDT = 21:00 UTC


# ── Ticker → settlement datetime ───────────────────────
def parse_settlement_date(event_ticker: str) -> datetime | None:
    """
    Parse settlement date from Kalshi gold event ticker.

    Format: KXGOLDD-26APR0617
                      ^^         year suffix  (26 → 2026)
                        ^^^      month abbrev (APR)
                           ^^    day          (06)
                             ^^  hour UTC     (17 → settlement hour, not used)

    Settlement is always at 5pm EDT = 21:00 UTC regardless of the ticker suffix.
    """
    try:
        suffix = event_ticker.split("-")[-1]   # e.g. "26APR0617"
        year   = int("20" + suffix[:2])         # 26 → 2026
        month_str = suffix[2:5]                  # "APR"
        day    = int(suffix[5:7])               # "06" → 6

        month_map = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
            "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
            "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        month = month_map.get(month_str.upper())
        if not month:
            return None

        return datetime(year, month, day, SETTLEMENT_HOUR_UTC, 0, 0, tzinfo=timezone.utc)
    except Exception:
        return None


# ── Gold settlement price fetch ────────────────────────
def fetch_settlement_price(settlement_dt: datetime) -> float | None:
    """
    Fetch gold futures (GC=F) price nearest to settlement_dt.

    Uses 1-hour bars — finds the bar that covers the settlement hour.
    Falls back to daily close if hourly unavailable.

    Returns None if price cannot be determined (e.g. holiday, weekend).
    """
    date_str   = settlement_dt.strftime("%Y-%m-%d")
    next_day   = (settlement_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    # Try hourly first
    try:
        data = yf.download("GC=F", start=date_str, end=next_day,
                           interval="1h", progress=False, auto_adjust=True)
        if not data.empty:
            closes = data["Close"].squeeze().dropna()
            # Find the bar at or just before settlement time
            target_ts = settlement_dt
            closes.index = closes.index.tz_convert("UTC")
            before = closes[closes.index <= target_ts]
            if not before.empty:
                price = float(before.iloc[-1])
                source = "1h bar"
                return price, source
    except Exception:
        pass

    # Fall back to daily close
    try:
        data = yf.download("GC=F", start=date_str, end=next_day,
                           interval="1d", progress=False, auto_adjust=True)
        if not data.empty:
            price = float(data["Close"].squeeze().dropna().iloc[-1])
            return price, "daily close"
    except Exception:
        pass

    return None, "unavailable"


# ── Signal scoring ──────────────────────────────────────
def score_signal(signal: str, threshold: float, settlement_price: float) -> bool | None:
    """
    BUY  = bet gold closes ABOVE threshold → win if settlement_price > threshold
    SELL = bet gold closes BELOW threshold → win if settlement_price <= threshold
    """
    if signal == "BUY":
        return settlement_price > threshold
    elif signal == "SELL":
        return settlement_price <= threshold
    return None


# ── Load log ───────────────────────────────────────────
def load_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        print(f"Log file not found: {log_path}")
        sys.exit(1)

    rows = []
    with open(log_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"Loaded {len(rows)} rows from {log_path}")
    return rows


# ── Main ───────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Backtest gold scanner signals")
    parser.add_argument("--log",       type=Path,  default=DEFAULT_LOG)
    parser.add_argument("--min-edge",  type=float, default=0.0,
                        help="Min buy_edge or sell_edge to include")
    parser.add_argument("--min-vol",   type=float, default=0.0,
                        help="Min vol_24h to include (filters out illiquid signals)")
    parser.add_argument("--signal",    type=str,   default=None,
                        choices=["BUY", "SELL"], help="Filter to one signal type")
    parser.add_argument("--max-hours", type=float, default=999,
                        help="Only include signals with hours_left <= this")
    args = parser.parse_args()

    rows = load_log(args.log)

    # Filter to strong signals only
    signal_rows = [r for r in rows if r.get("signal") in ("BUY", "SELL")]
    if args.signal:
        signal_rows = [r for r in signal_rows if r["signal"] == args.signal]
    if args.min_edge > 0:
        signal_rows = [
            r for r in signal_rows
            if max(float(r.get("buy_edge", 0)), float(r.get("sell_edge", 0))) >= args.min_edge
        ]
    if args.min_vol > 0:
        signal_rows = [
            r for r in signal_rows
            if float(r.get("vol_24h", 0)) >= args.min_vol
        ]
    if args.max_hours < 999:
        signal_rows = [
            r for r in signal_rows
            if float(r.get("hours_left", 999)) <= args.max_hours
        ]

    print(f"Analyzing {len(signal_rows)} signals after filters\n")

    if not signal_rows:
        print("No signals match filters.")
        return

    # Collect unique events and fetch settlement prices
    unique_events = set(r["event_ticker"] for r in signal_rows)
    print(f"Events to resolve: {len(unique_events)}")

    settlement_prices: dict[str, tuple[float | None, str]] = {}
    for ticker in sorted(unique_events):
        settle_dt = parse_settlement_date(ticker)
        if not settle_dt:
            print(f"  {ticker}: could not parse settlement date")
            settlement_prices[ticker] = (None, "parse error")
            continue

        now_utc = datetime.now(timezone.utc)
        if settle_dt > now_utc:
            print(f"  {ticker}: not yet settled (settles {settle_dt.strftime('%Y-%m-%d %H:%M UTC')})")
            settlement_prices[ticker] = (None, "pending")
            continue

        price, source = fetch_settlement_price(settle_dt)
        if price:
            print(f"  {ticker}: settled @ ${price:,.2f} [{source}]  ({settle_dt.strftime('%Y-%m-%d %H:%M UTC')})")
        else:
            print(f"  {ticker}: price unavailable")
        settlement_prices[ticker] = (price, source)

    print()

    # Score signals
    detailed = []
    stats = defaultdict(lambda: {"total": 0, "correct": 0, "wrong": 0, "pending": 0})

    for row in signal_rows:
        ticker    = row["event_ticker"]
        signal    = row["signal"]
        threshold = float(row["threshold"])
        hours     = float(row.get("hours_left", 0))
        buy_edge  = float(row.get("buy_edge", 0))
        sell_edge = float(row.get("sell_edge", 0))
        exec_edge = buy_edge if signal == "BUY" else sell_edge
        vol24     = float(row.get("vol_24h", 0))
        fv        = float(row.get("fair_value", 0))
        gold_px   = float(row.get("gold_price", 0))
        moneyness = (gold_px - threshold) / threshold * 100  # + = ITM for BUY

        settle_price, settle_source = settlement_prices.get(ticker, (None, "unknown"))

        if settle_price is None:
            for key in (signal, "ALL"):
                stats[key]["pending"] += 1
            outcome = "pending"
            correct = None
        else:
            correct = score_signal(signal, threshold, settle_price)
            outcome = "correct" if correct else "wrong"
            for key in (signal, "ALL"):
                stats[key]["total"] += 1
                if correct:
                    stats[key]["correct"] += 1
                else:
                    stats[key]["wrong"] += 1

        detailed.append({
            "scan_time":       row["scan_time"],
            "event_ticker":    ticker,
            "signal":          signal,
            "threshold":       threshold,
            "gold_price":      gold_px,
            "moneyness_pct":   round(moneyness, 2),
            "hours_left":      hours,
            "fair_value":      fv,
            "exec_edge":       round(exec_edge, 4),
            "vol_24h":         vol24,
            "bid":             row.get("bid", ""),
            "ask":             row.get("ask", ""),
            "settle_price":    settle_price,
            "settle_source":   settle_source,
            "outcome":         outcome,
        })

    # ── Print summary ──────────────────────────────────
    print("=" * 60)
    print("SIGNAL ACCURACY SUMMARY")
    print("=" * 60)

    for key in ("BUY", "SELL", "ALL"):
        s = stats[key]
        total   = s["total"]
        correct = s["correct"]
        pending = s["pending"]
        if total == 0 and pending == 0:
            continue
        acc = correct / total if total > 0 else 0
        print(f"\n{key} Signals:")
        print(f"  Resolved : {total}  (correct: {correct} = {acc:.1%})")
        print(f"  Pending  : {pending}")

    # ── Edge bucket breakdown ──────────────────────────
    resolved = [d for d in detailed if d["outcome"] in ("correct", "wrong")]
    if resolved:
        print("\n" + "=" * 60)
        print("EDGE BUCKET BREAKDOWN")
        print("=" * 60)
        print(f"  {'Edge Range':<16} {'N':>4} {'Correct':>8} {'Accuracy':>10}")
        print(f"  {'-'*42}")

        buckets = [
            (0.04, 0.08, "4–8¢"),
            (0.08, 0.12, "8–12¢"),
            (0.12, 0.20, "12–20¢"),
            (0.20, 1.00, ">20¢"),
        ]
        for lo, hi, label in buckets:
            bucket = [d for d in resolved if lo <= abs(d["exec_edge"]) < hi]
            n      = len(bucket)
            corr   = sum(1 for d in bucket if d["outcome"] == "correct")
            acc    = corr / n if n > 0 else 0
            print(f"  {label:<16} {n:>4} {corr:>8} {acc:>10.1%}")

        # ── Moneyness breakdown ────────────────────────
        print("\n" + "=" * 60)
        print("MONEYNESS BREAKDOWN  (+ = ITM for BUY, OTM for SELL)")
        print("=" * 60)
        print(f"  {'Moneyness':<16} {'N':>4} {'Correct':>8} {'Accuracy':>10}")
        print(f"  {'-'*42}")

        mon_buckets = [
            (-99, -2.0,  "< −2% OTM"),
            (-2.0, -1.0, "−2 to −1%"),
            (-1.0,  0.0, "−1 to  0%"),
            ( 0.0,  1.0, " 0 to +1%"),
            ( 1.0,  2.0, "+1 to +2%"),
            ( 2.0,  99,  "> +2% ITM"),
        ]
        for lo, hi, label in mon_buckets:
            bucket = [d for d in resolved if lo <= d["moneyness_pct"] < hi]
            n      = len(bucket)
            corr   = sum(1 for d in bucket if d["outcome"] == "correct")
            acc    = corr / n if n > 0 else 0
            if n > 0:
                print(f"  {label:<16} {n:>4} {corr:>8} {acc:>10.1%}")

        # ── Hours-to-settle breakdown ──────────────────
        print("\n" + "=" * 60)
        print("HOURS TO SETTLE BREAKDOWN")
        print("=" * 60)
        print(f"  {'Hours Left':<16} {'N':>4} {'Correct':>8} {'Accuracy':>10}")
        print(f"  {'-'*42}")

        hour_buckets = [
            (0,  3,  "0–3 hrs"),
            (3,  6,  "3–6 hrs"),
            (6,  12, "6–12 hrs"),
            (12, 99, ">12 hrs"),
        ]
        for lo, hi, label in hour_buckets:
            bucket = [d for d in resolved if lo <= d["hours_left"] < hi]
            n      = len(bucket)
            corr   = sum(1 for d in bucket if d["outcome"] == "correct")
            acc    = corr / n if n > 0 else 0
            if n > 0:
                print(f"  {label:<16} {n:>4} {corr:>8} {acc:>10.1%}")

        # ── Vol filter impact ──────────────────────────
        print("\n" + "=" * 60)
        print("VOLUME FILTER IMPACT")
        print("=" * 60)
        for vol_min in [0, 10, 50, 100]:
            bucket = [d for d in resolved if d["vol_24h"] >= vol_min]
            n      = len(bucket)
            corr   = sum(1 for d in bucket if d["outcome"] == "correct")
            acc    = corr / n if n > 0 else 0
            print(f"  vol >= {vol_min:<6} {n:>4} signals   {corr:>4} correct   {acc:>7.1%}")

        # ── Calibration check ──────────────────────────
        print("\n" + "=" * 60)
        print("MODEL CALIBRATION  (fair_value vs actual win rate)")
        print("=" * 60)
        print(f"  {'FV Range':<16} {'N':>4} {'Win Rate':>10} {'Avg FV':>10} {'Δ (bias)':>10}")
        print(f"  {'-'*52}")

        fv_buckets = [
            (0.0,  0.2,  "0–20%"),
            (0.2,  0.4,  "20–40%"),
            (0.4,  0.6,  "40–60%"),
            (0.6,  0.8,  "60–80%"),
            (0.8,  1.0,  "80–100%"),
        ]
        for lo, hi, label in fv_buckets:
            bucket  = [d for d in resolved if lo <= d["fair_value"] < hi]
            n       = len(bucket)
            wins    = sum(1 for d in bucket if d["outcome"] == "correct")
            win_rate = wins / n if n > 0 else 0
            avg_fv  = sum(d["fair_value"] for d in bucket) / n if n > 0 else 0
            bias    = win_rate - avg_fv
            if n > 0:
                print(f"  {label:<16} {n:>4} {win_rate:>10.1%} {avg_fv:>10.2f} {bias:>+10.2f}")

    print()

    # ── Export results ─────────────────────────────────
    if detailed:
        out_path = args.log.parent / "gold_backtest_results.csv"
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=detailed[0].keys())
            writer.writeheader()
            writer.writerows(detailed)
        print(f"Results saved to: {out_path}")
    else:
        print("No detailed results to export.")


if __name__ == "__main__":
    main()
