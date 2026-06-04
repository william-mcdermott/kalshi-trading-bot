#!/usr/bin/env python3
"""
MLB Filter Comparison Backtest

Replays the existing mlb_backtest_results.csv through alternative filter
configurations to see what the strategy looks like with tightened rules.
This avoids re-fetching outcomes from the MLB API — it just slices the
already-resolved signals by different filter logic.

Usage:
    python mlb_filter_comparison.py
    python mlb_filter_comparison.py --results /path/to/mlb_backtest_results.csv

The script defines several filter configurations and runs each one against
the same dataset, reporting accuracy, ROI, signal volume, and a side-by-side
comparison so you can see the tradeoffs.

Filter logic adapts gracefully to whatever columns are present in the CSV.
Run with --show-columns first if anything fails.
"""

import argparse
import csv
import sys
from pathlib import Path
from collections import defaultdict

DEFAULT_CSV = Path.home() / "Developer/code/kalshi-trading-bot/backend/scripts/mlb_backtest_results.csv"


# ---------------------------------------------------------------------------
# Filter definitions
# ---------------------------------------------------------------------------
# Each filter is a function (signal_dict) -> bool. Returns True if signal
# should be KEPT (i.e., would have been traded under this config).

def filter_current_live(s):
    """Approximation of what the live bot is doing today: 5-12¢ edge, innings 3-6, skip ±1."""
    edge = s["edge"]
    inning = s["inning"]
    run_diff = s["run_diff_abs"]
    return (
        0.05 <= edge <= 0.12
        and 3 <= inning <= 6
        and run_diff != 1
    )


def filter_baseline_all(s):
    """No filtering — every signal in the dataset, for reference."""
    return True


def filter_proposed_tight(s):
    """
    Proposed tighter filter from the analysis:
    - 8-12¢ edge only (drop 5-8¢ marginal, drop 12-20¢ broken bucket)
    - innings 4-6 only (drop inning 7)
    - tied OR ±2 runs (drop ±1 anti-alpha, drop ±3+ small sample)
    """
    edge = s["edge"]
    inning = s["inning"]
    run_diff = s["run_diff_abs"]
    return (
        0.08 <= edge <= 0.12
        and 4 <= inning <= 6
        and run_diff in (0, 2)
    )


def filter_proposed_relaxed(s):
    """
    Less aggressive cleanup — keeps 5-8¢ but excludes the toxic combinations:
    - 5-12¢ edge (drop 12-20¢ broken bucket)
    - innings 4-6 only
    - skip ±1 run only
    """
    edge = s["edge"]
    inning = s["inning"]
    run_diff = s["run_diff_abs"]
    return (
        0.05 <= edge <= 0.12
        and 4 <= inning <= 6
        and run_diff != 1
    )


def filter_sweet_spot_only(s):
    """The ultra-narrow filter: 8-12¢ × tied × innings 4-6. Highest edge per trade."""
    edge = s["edge"]
    inning = s["inning"]
    run_diff = s["run_diff_abs"]
    return (
        0.08 <= edge <= 0.12
        and 4 <= inning <= 6
        and run_diff == 0
    )


def filter_sell_only_proposed(s):
    """Same as proposed_tight but SELL signals only — tests the BUY/SELL asymmetry hypothesis."""
    return filter_proposed_tight(s) and s["side"].upper() == "SELL"


FILTERS = [
    ("Baseline (all signals)", filter_baseline_all),
    ("Current live (approx)", filter_current_live),
    ("Proposed tight", filter_proposed_tight),
    ("Proposed relaxed", filter_proposed_relaxed),
    ("Sweet spot only (tied + 8-12¢)", filter_sweet_spot_only),
    ("SELL-only (proposed tight)", filter_sell_only_proposed),
]


# ---------------------------------------------------------------------------
# CSV parsing — adapts to whatever columns the backtest writes
# ---------------------------------------------------------------------------

def safe_float(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def safe_int(v, default=None):
    f = safe_float(v, None)
    return int(f) if f is not None else default


def normalize_signal(row):
    """
    Convert a raw CSV row into a normalized signal dict.

    P&L is computed from half-Kelly stake + execution price + correct flag
    using standard Kalshi-style payoff:
      BUY at ask  a, stake s, correct → profit = s * (1 - a) / a   (lose s if wrong)
      SELL at bid b, stake s, correct → profit = s * b / (1 - b)   (lose s if wrong)
    """

    def get(*candidates, default=None):
        for c in candidates:
            if c in row and row[c] not in (None, ""):
                return row[c]
        return default

    # Edge — prefer exec_edge (post-spread) over edge_mid
    edge_raw = safe_float(get("exec_edge", "edge", "executable_edge", "edge_mid", "edge_pct"))
    if edge_raw is None:
        return None
    if edge_raw > 1.0:
        edge_raw = edge_raw / 100.0

    inning = safe_int(get("inning", "inn"))
    if inning is None:
        return None

    run_diff_raw = safe_int(get("run_diff", "run_differential", "score_diff"))
    if run_diff_raw is None:
        return None
    run_diff_abs = abs(run_diff_raw)

    side = (get("signal", "side", "action") or "").upper()

    correct_raw = str(get("correct", "outcome", "result") or "").lower()
    if correct_raw in ("correct", "win", "true", "1", "yes"):
        correct = True
    elif correct_raw in ("wrong", "loss", "false", "0", "no"):
        correct = False
    else:
        return None

    # Kelly stake — live bot uses half-Kelly
    kelly_stake = safe_float(get("half_kelly", "kelly_stake", "stake", "size"), default=None)

    # Execution prices for payoff computation
    ask = safe_float(get("kalshi_ask"))
    bid = safe_float(get("kalshi_bid"))

    kelly_pnl = None
    if kelly_stake is not None and kelly_stake > 0:
        if side == "BUY" and ask is not None and 0 < ask < 1:
            kelly_pnl = kelly_stake * (1 - ask) / ask if correct else -kelly_stake
        elif side == "SELL" and bid is not None and 0 < bid < 1:
            kelly_pnl = kelly_stake * bid / (1 - bid) if correct else -kelly_stake

    return {
        "edge": edge_raw,
        "inning": inning,
        "run_diff_abs": run_diff_abs,
        "side": side,
        "correct": correct,
        "kelly_pnl": kelly_pnl,
        "kelly_stake": kelly_stake,
    }


def load_signals(csv_path):
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            sig = normalize_signal(raw)
            if sig:
                rows.append(sig)
    return rows


# ---------------------------------------------------------------------------
# Stat computation per filter
# ---------------------------------------------------------------------------

def compute_filter_stats(signals, filter_fn):
    kept = [s for s in signals if filter_fn(s)]
    n = len(kept)
    if n == 0:
        return {"n": 0}

    correct = sum(1 for s in kept if s["correct"])
    wrong = n - correct
    accuracy = correct / n

    pnls = [s["kelly_pnl"] for s in kept if s["kelly_pnl"] is not None]
    stakes = [s["kelly_stake"] for s in kept if s["kelly_stake"] is not None]

    total_pnl = sum(pnls) if pnls else 0
    total_staked = sum(stakes) if stakes else 0
    roi = (total_pnl / total_staked) if total_staked else 0
    avg_pnl_per_trade = total_pnl / len(pnls) if pnls else 0

    # Edge bucket and run diff breakdowns within this filter
    bucket_breakdown = defaultdict(lambda: {"n": 0, "correct": 0})
    for s in kept:
        e = s["edge"]
        if e < 0.08:
            b = "5-8¢"
        elif e < 0.12:
            b = "8-12¢"
        elif e < 0.20:
            b = "12-20¢"
        else:
            b = ">20¢"
        bucket_breakdown[b]["n"] += 1
        if s["correct"]:
            bucket_breakdown[b]["correct"] += 1

    return {
        "n": n,
        "correct": correct,
        "wrong": wrong,
        "accuracy": accuracy,
        "total_pnl": total_pnl,
        "total_staked": total_staked,
        "roi": roi,
        "avg_pnl_per_trade": avg_pnl_per_trade,
        "buckets": dict(bucket_breakdown),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_filter_report(name, stats):
    if stats["n"] == 0:
        print(f"\n{name}\n  (no signals matched)")
        return

    print(f"\n{name}")
    print("-" * len(name))
    print(f"  Signals:          {stats['n']} ({stats['correct']}W / {stats['wrong']}L)")
    print(f"  Accuracy:         {stats['accuracy']*100:.1f}%")
    if stats["total_staked"] > 0:
        print(f"  Total staked:     ${stats['total_staked']:.2f}")
        print(f"  Total P&L:        ${stats['total_pnl']:+.2f}")
        print(f"  ROI:              {stats['roi']*100:+.1f}%")
        print(f"  Avg P&L/trade:    ${stats['avg_pnl_per_trade']:+.2f}")
    if stats["buckets"]:
        print(f"  Edge bucket breakdown:")
        for bucket in ["5-8¢", "8-12¢", "12-20¢", ">20¢"]:
            if bucket in stats["buckets"]:
                b = stats["buckets"][bucket]
                acc = (b["correct"] / b["n"]) * 100
                print(f"    {bucket:8s}  n={b['n']:3d}  acc={acc:5.1f}%")


def print_summary_table(results):
    print()
    print("=" * 80)
    print("SIDE-BY-SIDE COMPARISON")
    print("=" * 80)
    print(f"{'Filter':<35} {'N':>5} {'Acc':>7} {'P&L':>10} {'ROI':>8} {'Avg/trade':>10}")
    print("-" * 80)
    for name, stats in results:
        if stats["n"] == 0:
            print(f"{name:<35} {'0':>5} {'-':>7} {'-':>10} {'-':>8} {'-':>10}")
            continue
        acc_str = f"{stats['accuracy']*100:.1f}%"
        pnl_str = f"${stats['total_pnl']:+.2f}" if stats["total_staked"] else "-"
        roi_str = f"{stats['roi']*100:+.1f}%" if stats["total_staked"] else "-"
        avg_str = f"${stats['avg_pnl_per_trade']:+.2f}" if stats["total_staked"] else "-"
        print(f"{name:<35} {stats['n']:>5} {acc_str:>7} {pnl_str:>10} {roi_str:>8} {avg_str:>10}")
    print("=" * 80)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compare MLB filter configurations against existing backtest results")
    parser.add_argument("--results", default=str(DEFAULT_CSV), help="Path to mlb_backtest_results.csv")
    parser.add_argument("--show-columns", action="store_true", help="Print CSV columns and exit")
    args = parser.parse_args()

    csv_path = Path(args.results)
    if not csv_path.exists():
        print(f"ERROR: results CSV not found at {csv_path}", file=sys.stderr)
        print("Run mlb_backtest.py first to generate it.", file=sys.stderr)
        sys.exit(1)

    if args.show_columns:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            print("Columns in CSV:")
            for c in (reader.fieldnames or []):
                print(f"  {c}")
            print(f"\nFirst row sample:")
            for row in reader:
                for k, v in row.items():
                    print(f"  {k}: {v}")
                break
        return

    signals = load_signals(csv_path)
    if not signals:
        print("No usable signals found in CSV. Run with --show-columns to inspect schema.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(signals)} resolved signals from {csv_path.name}")

    results = []
    for name, fn in FILTERS:
        stats = compute_filter_stats(signals, fn)
        results.append((name, stats))
        print_filter_report(name, stats)

    print_summary_table(results)

    print()
    print("How to read this:")
    print("  - 'Baseline (all signals)' shows what the backtest covered.")
    print("  - 'Current live (approx)' approximates your existing filter.")
    print("  - 'Proposed tight' applies the analysis recommendation: 8-12¢, inn 4-6, tied/±2.")
    print("  - 'Proposed relaxed' is a smaller change: drop 12-20¢ bucket and ±1 run only.")
    print("  - 'Sweet spot only' is the highest-conviction subset (small sample warning).")
    print("  - 'SELL-only' tests whether the BUY/SELL asymmetry persists under the tight filter.")
    print()
    print("Look for: filters that improve avg P&L/trade AND keep enough signal volume to matter.")
    print("A high ROI on n=10 signals is much weaker evidence than a moderate ROI on n=80.")


if __name__ == "__main__":
    main()