#!/usr/bin/env python3
"""
mlb_backtest.py

Reads mlb_live_scanner_log.csv, fetches final game outcomes from the
MLB Stats API, and computes EV / accuracy stats for each signal type.

Usage:
    python scripts/mlb_backtest.py
    python scripts/mlb_backtest.py --log path/to/custom_log.csv
    python scripts/mlb_backtest.py --min-edge 0.08  # only analyze signals >= 8¢ edge
    python scripts/mlb_backtest.py --signal BUY     # only BUY signals
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

DEFAULT_LOG = Path(__file__).parent / "mlb_live_scanner_log.csv"


# ── MLB Stats API outcome fetch ─────────────────────────
def fetch_game_outcome(game_pk: int) -> dict | None:
    """
    Returns final score for a completed game.
    {
        "game_pk": 12345,
        "status": "Final",
        "away_team": "Seattle",
        "home_team": "Los Angeles A",
        "away_runs": 3,
        "home_runs": 5,
        "winner": "Los Angeles A",   # home_short or away_short
    }
    Returns None if game not found or not yet final.
    """
    try:
        r = httpx.get(
            f"https://statsapi.mlb.com/api/v1/game/{game_pk}/linescore",
            timeout=10.0,
        )
        r.raise_for_status()
        ls = r.json()

        # Check game status
        r2 = httpx.get(
            f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
            timeout=10.0,
        )
        r2.raise_for_status()
        box = r2.json()

        away_runs = ls.get("teams", {}).get("away", {}).get("runs", None)
        home_runs = ls.get("teams", {}).get("home", {}).get("runs", None)
        status    = box.get("info", [{}])

        # Get status from schedule endpoint (more reliable)
        r3 = httpx.get(
            f"https://statsapi.mlb.com/api/v1/schedule",
            params={"gamePk": game_pk, "sportId": 1},
            timeout=10.0,
        )
        sched   = r3.json()
        game_st = None
        away_name = home_name = ""

        for date in sched.get("dates", []):
            for g in date.get("games", []):
                if g["gamePk"] == game_pk:
                    game_st   = g["status"]["detailedState"]
                    away_name = g["teams"]["away"]["team"]["name"]
                    home_name = g["teams"]["home"]["team"]["name"]
                    break

        if game_st not in ("Final", "Game Over", "Completed Early"):
            return None  # not finished yet

        if away_runs is None or home_runs is None:
            return None

        winner = "home" if home_runs > away_runs else "away"

        return {
            "game_pk":    game_pk,
            "status":     game_st,
            "away_name":  away_name,
            "home_name":  home_name,
            "away_runs":  away_runs,
            "home_runs":  home_runs,
            "winner":     winner,  # "home" or "away"
        }

    except Exception as e:
        print(f"  Warning: could not fetch outcome for game_pk={game_pk}: {e}")
        return None


# ── Load log ───────────────────────────────────────────
OLD_COLS = [
    "scan_time","event_ticker","team","inning","half","outs","run_diff",
    "model_prob","kalshi_mid","edge","vol24","signal"
]
NEW_COLS = [
    "scan_time","game_pk","event_ticker","away_short","home_short","team",
    "inning","half","outs","run_diff","pregame_prob","prob_source",
    "pitcher_adj",
    "model_prob","kalshi_mid","kalshi_bid","kalshi_ask",
    "edge_mid","edge_buy","edge_sell","vol24","signal",
    "kelly_pct","half_kelly","full_kelly","kelly_contracts",
]

def load_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        print(f"Log file not found: {log_path}")
        sys.exit(1)

    old_rows, new_rows, kelly_rows = [], [], []
    with open(log_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            if len(row) == len(OLD_COLS):
                old_rows.append(dict(zip(OLD_COLS, row)))
            elif len(row) == len(NEW_COLS):
                # New schema with Kelly columns
                kelly_rows.append(dict(zip(NEW_COLS, row)))
            elif len(row) == len(NEW_COLS) - 4:
                # New schema without Kelly columns (pre-Kelly scanner runs)
                base_cols = NEW_COLS[:-4]
                d = dict(zip(base_cols, row))
                # Backfill Kelly columns as empty
                d.update({"kelly_pct": "", "half_kelly": "", "full_kelly": "", "kelly_contracts": ""})
                new_rows.append(d)
            # rows with other column counts skipped silently

    total = len(old_rows) + len(new_rows) + len(kelly_rows)
    print(f"Loaded {total} rows from {log_path}")
    print(f"  Old schema (no game_pk):       {len(old_rows)}")
    print(f"  New schema (no Kelly):         {len(new_rows)}")
    print(f"  New schema (with Kelly):       {len(kelly_rows)}")
    return new_rows + kelly_rows


# ── Check if a signal was correct ─────────────────────
def signal_correct(row: dict, outcome: dict) -> bool | None:
    """
    Returns True if signal was correct, False if wrong, None if indeterminate.

    BUY signal = betting YES on `team` to win
    SELL signal = betting NO on `team` to win (i.e. betting opponent wins)
    """
    signal    = row["signal"]
    team      = row["team"]
    home_short = row.get("home_short", "")
    away_short = row.get("away_short", "")

    if not signal or signal == "WATCH":
        return None

    is_home   = (team == home_short)
    team_won  = (outcome["winner"] == "home") if is_home else (outcome["winner"] == "away")

    if signal == "BUY":
        return team_won
    elif signal == "SELL":
        return not team_won  # SELL = bet team loses

    return None


# ── EV calculation ─────────────────────────────────────
def calc_ev(model_prob: float, exec_edge: float, signal: str) -> float:
    """
    Simple EV per $1 wagered.
    BUY:  EV = model_prob * (1 - ask) - (1 - model_prob) * ask
    SELL: EV = model_prob_opponent * (1 - bid_opp) - ...
    
    Simplified: EV ≈ exec_edge (edge already accounts for price paid)
    """
    return exec_edge


# ── Main ───────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Backtest MLB live scanner signals")
    parser.add_argument("--log",        type=Path, default=DEFAULT_LOG)
    parser.add_argument("--min-edge",   type=float, default=0.0,  help="Min |edge_mid| to include")
    parser.add_argument("--max-edge",   type=float, default=None, help="Max |edge_mid| to include (e.g. 0.12 to cap at 12¢)")
    parser.add_argument("--signal",     type=str, default=None, choices=["BUY", "SELL", "WATCH"])
    parser.add_argument("--max-inning", type=int,   default=None, help="Max inning to include (e.g. 5 to only analyze innings 4-5)")
    parser.add_argument("--tied-only",  action="store_true",      help="Only include signals where run_diff == 0 (tied game)")
    parser.add_argument("--reverse",    action="store_true",      help="Flip signal correctness — models what P&L would be if you traded the opposite of every signal")
    parser.add_argument("--min-kelly",  type=float, default=0.0,
                        help="Min half_kelly dollar size to include (filters pre-Kelly rows if 0)")
    args = parser.parse_args()

    rows = load_log(args.log)

    if not rows:
        print("\n⚠️  No new-schema rows found with game_pk.")
        print("   Run mlb_live_scanner.py for a few games to build up data.")
        sys.exit(1)

    # Filter
    signal_rows = [r for r in rows if r.get("signal") in ("BUY", "SELL")]
    if args.signal:
        signal_rows = [r for r in signal_rows if r["signal"] == args.signal]
    if args.min_edge:
        signal_rows = [r for r in signal_rows if abs(float(r.get("edge_mid", 0))) >= args.min_edge]
    if args.max_edge is not None:
        signal_rows = [r for r in signal_rows if abs(float(r.get("edge_mid", 0))) <= args.max_edge]
    if args.max_inning is not None:
        signal_rows = [r for r in signal_rows if int(r.get("inning", 99)) <= args.max_inning]
    if args.tied_only:
        signal_rows = [r for r in signal_rows if int(r.get("run_diff", 99)) == 0]
    if args.min_kelly:
        signal_rows = [
            r for r in signal_rows
            if r.get("half_kelly") and float(r["half_kelly"]) >= args.min_kelly
        ]

    # ── Deduplication ──────────────────────────────────
    # The scanner fires every 2 minutes — same signal can appear many times
    # for the same game/team/inning. Keep only the FIRST occurrence of each
    # unique (game_pk, signal, team, inning) combination so P&L isn't inflated.
    seen = set()
    deduped = []
    dupes   = 0
    for r in signal_rows:
        key = (r.get("game_pk",""), r.get("signal",""), r.get("team",""), r.get("inning",""))
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        deduped.append(r)

    if dupes > 0:
        print(f"Deduplicated {dupes} duplicate signals ({len(deduped)} unique remain)")
    signal_rows = deduped

    if not signal_rows:
        print("No matching signal rows to analyze.")
        return

    print(f"\nAnalyzing {len(signal_rows)} signal rows...")

    # Fetch outcomes — deduplicate game_pk lookups
    unique_pks = set(int(r["game_pk"]) for r in signal_rows)
    print(f"Fetching outcomes for {len(unique_pks)} unique games...\n")

    outcomes = {}
    for pk in sorted(unique_pks):
        print(f"  game_pk={pk} ... ", end="", flush=True)
        outcome = fetch_game_outcome(pk)
        if outcome:
            print(f"{outcome['away_name']} @ {outcome['home_name']} → {outcome['away_runs']}-{outcome['home_runs']} ({outcome['status']})")
            outcomes[pk] = outcome
        else:
            print("not final or not found")

    print()

    # Analyze signals
    stats = defaultdict(lambda: {
        "total": 0, "correct": 0, "wrong": 0, "pending": 0,
        "total_edge": 0.0, "correct_edge": 0.0,
    })

    detailed = []

    for row in signal_rows:
        pk     = int(row["game_pk"])
        signal = row["signal"]
        team   = row["team"]

        outcome = outcomes.get(pk)
        if not outcome:
            stats[signal]["pending"] += 1
            stats["ALL"]["pending"] += 1
            continue

        correct = signal_correct(row, outcome)
        if correct is None:
            continue

        if args.reverse:
            correct = not correct

        exec_edge_col = "edge_buy" if signal == "BUY" else "edge_sell"
        exec_edge     = float(row.get(exec_edge_col, row.get("edge_mid", 0)))
        mid_edge      = float(row.get("edge_mid", 0))

        for key in (signal, "ALL"):
            stats[key]["total"]      += 1
            stats[key]["total_edge"] += exec_edge
            if correct:
                stats[key]["correct"]      += 1
                stats[key]["correct_edge"] += exec_edge
            else:
                stats[key]["wrong"] += 1

        detailed.append({
            "game_pk":        pk,
            "signal":         signal,
            "team":           team,
            "inning":         row["inning"],
            "half":           row["half"],
            "run_diff":       int(row.get("run_diff", 0)),
            "is_home":        row.get("team") == row.get("home_short"),
            "away_short":     row.get("away_short", ""),
            "home_short":     row.get("home_short", ""),
            "pregame_prob":   float(row["pregame_prob"]) if row.get("pregame_prob") else None,
            "edge_mid":       mid_edge,
            "exec_edge":      exec_edge,
            "model_prob":     float(row.get("model_prob", 0)),
            "kalshi_ask":     float(row.get("kalshi_ask", 0)),
            "kalshi_bid":     float(row.get("kalshi_bid", 0)),
            "half_kelly":     float(row["half_kelly"]) if row.get("half_kelly") else None,
            "full_kelly":     float(row["full_kelly"]) if row.get("full_kelly") else None,
            "kelly_contracts": int(row["kelly_contracts"]) if row.get("kelly_contracts") else None,
            "correct":        correct,
            "score":          f"{outcome['away_runs']}-{outcome['home_runs']}",
            "winner":         outcome["winner"],
        })

    # ── Print results ──────────────────────────────────
    print("=" * 60)
    print("SIGNAL ACCURACY SUMMARY")
    if args.reverse:
        print("*** REVERSE MODE: correctness flipped — modeling opposite trades ***")
    print("=" * 60)

    for key in ("BUY", "SELL", "ALL"):
        s = stats[key]
        if s["total"] == 0 and s["pending"] == 0:
            continue

        total   = s["total"]
        correct = s["correct"]
        wrong   = s["wrong"]
        pending = s["pending"]
        acc     = correct / total if total > 0 else 0
        avg_edge = s["total_edge"] / total if total > 0 else 0

        print(f"\n{key} Signals:")
        print(f"  Total resolved : {total}")
        print(f"  Correct        : {correct} ({acc:.1%})")
        print(f"  Wrong          : {wrong}")
        print(f"  Pending        : {pending}")
        print(f"  Avg exec edge  : {avg_edge:+.3f}")

        if total > 0:
            # Brier-style: is accuracy above what model predicted?
            avg_model = sum(float(r.get("model_prob", 0.5)) for r in signal_rows
                           if r.get("signal") == key or key == "ALL") / max(total + pending, 1)
            print(f"  Avg model prob : {avg_model:.3f}")

    # ── Edge bucket breakdown ──────────────────────────
    print("\n" + "=" * 60)
    print("EDGE BUCKET BREAKDOWN (executable edge)")
    print("=" * 60)
    print(f"  {'Edge Range':<16} {'N':>4} {'Correct':>8} {'Accuracy':>10}")
    print(f"  {'-'*44}")

    buckets = [
        (0.05, 0.08, "5–8¢"),
        (0.08, 0.12, "8–12¢"),
        (0.12, 0.20, "12–20¢"),
        (0.20, 1.00, ">20¢"),
    ]

    for lo, hi, label in buckets:
        bucket = [d for d in detailed if lo <= abs(d["exec_edge"]) < hi]
        n      = len(bucket)
        corr   = sum(1 for d in bucket if d["correct"])
        acc    = corr / n if n > 0 else 0
        print(f"  {label:<16} {n:>4} {corr:>8} {acc:>10.1%}")

    # ── Inning breakdown ──────────────────────────────
    print("\n" + "=" * 60)
    print("ACCURACY BY INNING")
    print("=" * 60)
    print(f"  {'Inning':<10} {'N':>4} {'Correct':>8} {'Accuracy':>10}")
    print(f"  {'-'*36}")

    by_inning = defaultdict(list)
    for d in detailed:
        by_inning[int(d["inning"])].append(d)

    for inn in sorted(by_inning.keys()):
        items = by_inning[inn]
        n     = len(items)
        corr  = sum(1 for d in items if d["correct"])
        acc   = corr / n if n > 0 else 0
        print(f"  Inning {inn:<5} {n:>4} {corr:>8} {acc:>10.1%}")

    print()

    # ── Home vs Away breakdown ─────────────────────────
    print("\n" + "=" * 60)
    print("ACCURACY BY HOME/AWAY")
    print("=" * 60)
    print(f"  {'Context':<12} {'N':>4} {'Correct':>8} {'Accuracy':>10}")
    print(f"  {'-'*36}")
    for is_home, label in [(True, "Home"), (False, "Away")]:
        items = [d for d in detailed if d["is_home"] == is_home]
        n     = len(items)
        corr  = sum(1 for d in items if d["correct"])
        acc   = corr / n if n > 0 else 0
        print(f"  {label:<12} {n:>4} {corr:>8} {acc:>10.1%}")

    # ── Score differential breakdown ──────────────────
    print("\n" + "=" * 60)
    print("ACCURACY BY SCORE DIFFERENTIAL (at signal time)")
    print("=" * 60)
    print(f"  {'Run Diff':<14} {'N':>4} {'Correct':>8} {'Accuracy':>10}")
    print(f"  {'-'*40}")
    diff_buckets = [
        (0,  0,  "Tied"),
        (1,  1,  "±1 run"),
        (2,  2,  "±2 runs"),
        (3,  99, "±3+ runs"),
    ]
    for lo, hi, label in diff_buckets:
        items = [d for d in detailed if lo <= abs(d["run_diff"]) <= hi]
        n     = len(items)
        corr  = sum(1 for d in items if d["correct"])
        acc   = corr / n if n > 0 else 0
        print(f"  {label:<14} {n:>4} {corr:>8} {acc:>10.1%}")

    print()
    kelly_rows = [d for d in detailed if d["half_kelly"] is not None]
    if kelly_rows:
        print("\n" + "=" * 60)
        print("KELLY ROI SIMULATION")
        print("=" * 60)
        print("(Half-Kelly sizing — what P&L would have been if you followed the bot)")
        print()

        total_staked = 0.0
        total_pnl    = 0.0
        wins         = 0
        losses       = 0

        for d in kelly_rows:
            stake = d["half_kelly"]
            if d["signal"] == "BUY":
                price = d["kalshi_ask"]
            else:
                price = 1 - d["kalshi_bid"]

            if price <= 0 or price >= 1:
                continue

            payout    = stake / price          # gross return if win
            profit    = payout - stake         # net profit if win
            loss      = -stake                 # net loss if wrong

            total_staked += stake
            if d["correct"]:
                total_pnl += profit
                wins      += 1
            else:
                total_pnl += loss
                losses    += 1

        n        = wins + losses
        roi      = total_pnl / total_staked if total_staked > 0 else 0
        win_rate = wins / n if n > 0 else 0

        print(f"  Signals with Kelly data : {n}")
        print(f"  Win rate                : {win_rate:.1%}  ({wins}W / {losses}L)")
        print(f"  Total staked            : ${total_staked:.2f}")
        print(f"  Total P&L               : ${total_pnl:+.2f}")
        print(f"  ROI                     : {roi:+.1%}")

        # Kelly ROI by edge bucket
        print()
        print(f"  {'Edge Range':<16} {'N':>4} {'Staked':>8} {'P&L':>10} {'ROI':>8}")
        print(f"  {'-'*50}")

        buckets = [
            (0.05, 0.08, "5–8¢"),
            (0.08, 0.12, "8–12¢"),
            (0.12, 0.20, "12–20¢"),
            (0.20, 1.00, ">20¢"),
        ]
        for lo, hi, label in buckets:
            bucket = [d for d in kelly_rows if lo <= abs(d["exec_edge"]) < hi]
            b_staked = b_pnl = 0.0
            b_n = 0
            for d in bucket:
                stake = d["half_kelly"]
                price = d["kalshi_ask"] if d["signal"] == "BUY" else 1 - d["kalshi_bid"]
                if price <= 0 or price >= 1:
                    continue
                payout = stake / price
                b_staked += stake
                b_pnl    += (payout - stake) if d["correct"] else -stake
                b_n      += 1
            b_roi = b_pnl / b_staked if b_staked > 0 else 0
            if b_n > 0:
                print(f"  {label:<16} {b_n:>4} ${b_staked:>7.2f} ${b_pnl:>+9.2f} {b_roi:>+7.1%}")
    else:
        print("\n  (No Kelly data yet — run scanner for a few games to populate)")

    print()

    # ── Cross-tab: edge bucket × inning ───────────────
    print("\n" + "=" * 60)
    print("CROSS-TAB: EDGE BUCKET × INNING (accuracy %)")
    print("=" * 60)

    innings   = sorted(set(int(d["inning"]) for d in detailed))
    buckets_xt = [
        (0.05, 0.08, "5–8¢"),
        (0.08, 0.12, "8–12¢"),
        (0.12, 0.20, "12–20¢"),
        (0.20, 1.00, ">20¢"),
    ]

    # header row
    inn_labels = [f"Inn {i}" for i in innings]
    col_w = 10
    print(f"  {'':16}" + "".join(f"{h:>{col_w}}" for h in inn_labels))
    print(f"  {'-' * (16 + col_w * len(innings))}")

    for lo, hi, label in buckets_xt:
        row_parts = []
        for inn in innings:
            items = [d for d in detailed if lo <= abs(d["exec_edge"]) < hi and int(d["inning"]) == inn]
            n    = len(items)
            corr = sum(1 for d in items if d["correct"])
            if n == 0:
                row_parts.append(f"{'—':>{col_w}}")
            else:
                acc = corr / n
                row_parts.append(f"{acc:>{col_w - 4}.0%} n={n}")
        print(f"  {label:<16}" + "".join(row_parts))

    # ── Cross-tab: edge bucket × run differential ─────
    print("\n" + "=" * 60)
    print("CROSS-TAB: EDGE BUCKET × RUN DIFFERENTIAL (accuracy %)")
    print("=" * 60)

    diff_buckets_xt = [
        (0,  0,  "Tied"),
        (1,  1,  "±1 run"),
        (2,  2,  "±2 runs"),
        (3,  99, "±3+ runs"),
    ]
    diff_labels = [b[2] for b in diff_buckets_xt]
    col_w2 = 12
    print(f"  {'':16}" + "".join(f"{h:>{col_w2}}" for h in diff_labels))
    print(f"  {'-' * (16 + col_w2 * len(diff_labels))}")

    for lo, hi, label in buckets_xt:
        row_parts = []
        for dlo, dhi, dlabel in diff_buckets_xt:
            items = [
                d for d in detailed
                if lo <= abs(d["exec_edge"]) < hi and dlo <= abs(d["run_diff"]) <= dhi
            ]
            n    = len(items)
            corr = sum(1 for d in items if d["correct"])
            if n == 0:
                row_parts.append(f"{'—':>{col_w2}}")
            else:
                acc = corr / n
                row_parts.append(f"{acc:>{col_w2 - 4}.0%} n={n}")
        print(f"  {label:<16}" + "".join(row_parts))

    print()

    # ── Export enriched CSV ────────────────────────────
    out_path = args.log.parent / "mlb_backtest_results.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=detailed[0].keys() if detailed else [])
        writer.writeheader()
        writer.writerows(detailed)

    print(f"Detailed results saved to: {out_path}")


if __name__ == "__main__":
    main()