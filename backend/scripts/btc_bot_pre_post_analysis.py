#!/usr/bin/env python3
"""
BTC Bot Pre/Post Fix Analysis

Splits MACD strategy trades by a cutoff date and compares performance metrics
to determine whether the recent bug fixes meaningfully improved edge.

Usage:
    python btc_bot_pre_post_analysis.py --cutoff 2026-04-15
    python btc_bot_pre_post_analysis.py --cutoff 2026-04-15 --db /path/to/bot.db
    python btc_bot_pre_post_analysis.py --cutoff 2026-04-15 --strategy macd

Adjust DEFAULT_DB_PATH if your DB lives elsewhere.
"""

import argparse
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / "Developer/code/kalshi-trading-bot/backend/data/bot.db"


def discover_schema(conn):
    """Print the trades table schema so we can sanity-check column names."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(trades)")
    cols = cur.fetchall()
    if not cols:
        print("ERROR: 'trades' table not found. Run with --list-tables to see available tables.", file=sys.stderr)
        sys.exit(1)
    return {row[1]: row[2] for row in cols}  # name -> type


def list_tables(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r[0] for r in cur.fetchall()]


def pick_column(schema, candidates, label):
    """Find the first column from candidates that exists in the schema."""
    for c in candidates:
        if c in schema:
            return c
    print(f"ERROR: Could not find a column for {label}. Tried: {candidates}", file=sys.stderr)
    print(f"Available columns: {list(schema.keys())}", file=sys.stderr)
    sys.exit(1)


def fetch_trades(conn, strategy, ts_col, pnl_col, strategy_col, filled_col=None):
    """Pull all trades for the given strategy."""
    where = [f"{strategy_col} = ?"]
    params = [strategy]
    if filled_col:
        # Treat both 1 and 'filled'/'✓' as filled for safety
        where.append(f"({filled_col} = 1 OR {filled_col} = 'filled' OR {filled_col} = '✓')")

    sql = f"""
        SELECT {ts_col}, {pnl_col}
        FROM trades
        WHERE {' AND '.join(where)}
        ORDER BY {ts_col} ASC
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def parse_ts(ts):
    """Best-effort timestamp parser — handles ISO strings and unix epochs."""
    if isinstance(ts, (int, float)):
        # Unix epoch (seconds or millis)
        if ts > 1e12:
            ts = ts / 1000
        return datetime.fromtimestamp(ts)
    if isinstance(ts, str):
        # Try a few common formats
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
            try:
                return datetime.strptime(ts.split("+")[0].split("Z")[0], fmt)
            except ValueError:
                continue
        # Last resort — fromisoformat
        try:
            return datetime.fromisoformat(ts.replace("Z", ""))
        except ValueError:
            pass
    raise ValueError(f"Could not parse timestamp: {ts!r}")


def compute_stats(trades, label):
    """Compute summary stats for a list of (timestamp, pnl) tuples."""
    if not trades:
        return {"label": label, "n": 0}

    pnls = [float(t[1]) for t in trades if t[1] is not None]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    win_rate = len(wins) / n if n else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    avg_pnl = total / n if n else 0

    # Sample standard deviation
    if n > 1:
        mean = avg_pnl
        variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        stdev = math.sqrt(variance)
    else:
        stdev = 0

    # Standard error of the mean and z-score vs zero
    sem = stdev / math.sqrt(n) if n else 0
    z_vs_zero = avg_pnl / sem if sem else 0

    # Sharpe-ish (per-trade, not annualized — pure signal quality metric)
    sharpe_per_trade = avg_pnl / stdev if stdev else 0

    # Expectancy decomposition
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    # Simulated equity curve max drawdown
    equity = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    return {
        "label": label,
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": total,
        "avg_pnl": avg_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "stdev": stdev,
        "sem": sem,
        "z_vs_zero": z_vs_zero,
        "sharpe_per_trade": sharpe_per_trade,
        "expectancy": expectancy,
        "max_dd": max_dd,
    }


def fmt_stats(s):
    if s["n"] == 0:
        return f"  {s['label']}: no trades\n"
    return (
        f"  {s['label']}\n"
        f"    Trades:           {s['n']} ({s['wins']}W / {s['losses']}L)\n"
        f"    Win rate:         {s['win_rate']*100:.1f}%\n"
        f"    Total P&L:        ${s['total_pnl']:+.2f}\n"
        f"    Avg P&L/trade:    ${s['avg_pnl']:+.4f}\n"
        f"    Avg win:          ${s['avg_win']:+.4f}\n"
        f"    Avg loss:         ${s['avg_loss']:+.4f}\n"
        f"    Std dev:          ${s['stdev']:.4f}\n"
        f"    SEM:              ${s['sem']:.4f}\n"
        f"    Z-score vs zero:  {s['z_vs_zero']:+.2f}  ({'real edge' if abs(s['z_vs_zero']) > 2 else 'noisy' if abs(s['z_vs_zero']) < 1 else 'suggestive'})\n"
        f"    Sharpe/trade:     {s['sharpe_per_trade']:+.4f}\n"
        f"    Expectancy:       ${s['expectancy']:+.4f}\n"
        f"    Max drawdown:     ${s['max_dd']:.2f}\n"
    )


def welch_t_compare(pre, post):
    """Welch's t-test on the per-trade P&L difference."""
    if pre["n"] < 2 or post["n"] < 2:
        return None
    diff = post["avg_pnl"] - pre["avg_pnl"]
    se_diff = math.sqrt((pre["stdev"] ** 2 / pre["n"]) + (post["stdev"] ** 2 / post["n"]))
    if se_diff == 0:
        return None
    t = diff / se_diff
    return {"diff": diff, "se_diff": se_diff, "t": t}


def sizing_recommendation(post):
    """Crude size recommendation based on post-fix sample confidence."""
    n = post["n"]
    z = post["z_vs_zero"]
    if n < 30:
        return "  Sample too small (<30). Stay at current size."
    if z < 1.0:
        return "  Edge not statistically distinguishable from zero. Stay at current size."
    if z < 2.0:
        return "  Suggestive edge but noisy. Consider 1.5–2x current size on highest-edge tier only."
    if z < 3.0:
        return "  Statistically meaningful edge. 2x flat or tier-weighted up to 3x on >20¢ edges is reasonable."
    return "  Strong evidence of edge. 2–3x flat or aggressive tier weighting justified."


def main():
    parser = argparse.ArgumentParser(description="Pre/post fix analysis for BTC bot")
    parser.add_argument("--cutoff", required=False, help="ISO date for pre/post split, e.g. 2026-04-15")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to bot.db")
    parser.add_argument("--strategy", default="macd", help="Strategy name to filter on")
    parser.add_argument("--list-tables", action="store_true", help="List tables and exit")
    parser.add_argument("--show-schema", action="store_true", help="Show trades schema and exit")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))

    if args.list_tables:
        print("Tables:", list_tables(conn))
        return

    schema = discover_schema(conn)
    if args.show_schema:
        print("trades schema:")
        for k, v in schema.items():
            print(f"  {k}: {v}")
        return

    if not args.cutoff:
        print("ERROR: --cutoff required (e.g. --cutoff 2026-04-15)", file=sys.stderr)
        print(f"\nAvailable columns in trades: {list(schema.keys())}", file=sys.stderr)
        sys.exit(1)

    ts_col = pick_column(schema, ["timestamp", "created_at", "executed_at", "filled_at", "time", "ts"], "timestamp")
    pnl_col = pick_column(schema, ["pnl", "profit_loss", "pnl_realized", "realized_pnl", "p_and_l"], "pnl")
    strategy_col = pick_column(schema, ["strategy", "strategy_name", "bot"], "strategy")
    filled_col = next((c for c in ["filled", "is_filled", "status"] if c in schema), None)

    print(f"Using columns: ts={ts_col}, pnl={pnl_col}, strategy={strategy_col}, filled={filled_col or '(none)'}")
    print()

    rows = fetch_trades(conn, args.strategy, ts_col, pnl_col, strategy_col, filled_col)
    if not rows:
        print(f"No trades found for strategy='{args.strategy}'")
        return

    cutoff = datetime.fromisoformat(args.cutoff)
    pre_trades, post_trades = [], []
    for ts, pnl in rows:
        try:
            dt = parse_ts(ts)
        except ValueError as e:
            print(f"WARN: skipping row with bad timestamp: {e}", file=sys.stderr)
            continue
        (post_trades if dt >= cutoff else pre_trades).append((dt, pnl))

    pre = compute_stats(pre_trades, f"PRE-FIX  (before {args.cutoff})")
    post = compute_stats(post_trades, f"POST-FIX (on/after {args.cutoff})")
    overall = compute_stats([(t, p) for t, p in (pre_trades + post_trades)], "OVERALL")

    print("=" * 68)
    print(f"BTC BOT ANALYSIS — strategy='{args.strategy}', cutoff={args.cutoff}")
    print("=" * 68)
    print()
    print(fmt_stats(pre))
    print(fmt_stats(post))
    print(fmt_stats(overall))

    test = welch_t_compare(pre, post)
    if test:
        print("Pre vs Post comparison:")
        print(f"  Δ avg P&L:        ${test['diff']:+.4f} per trade")
        print(f"  SE of diff:       ${test['se_diff']:.4f}")
        print(f"  t-statistic:      {test['t']:+.2f}")
        verdict = "no real change" if abs(test["t"]) < 1 else "suggestive change" if abs(test["t"]) < 2 else "meaningful change"
        print(f"  Verdict:          {verdict}")
        print()

    print("Sizing recommendation:")
    print(sizing_recommendation(post))


if __name__ == "__main__":
    main()
