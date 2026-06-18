#!/usr/bin/env python3
"""
mlb_reconcile.py

Reconciles the MLB scanner's shadow-intent log against reality. Answers the one
question the backtest structurally cannot: when you actually try to trade these
signals, do your fills match the prices the backtest assumed?

Two populations in mlb_shadow_intents.csv:
  - AUTO   : real orders placed live (have an order_id). We pull the actual
             fill price and the Kalshi settlement, and report realized net P&L
             after real fees, plus slippage vs the price the signal saw.
  - SHADOW : signals we logged but never traded (thin/fragile buckets). We pull
             the market's objective result and compute HYPOTHETICAL P&L at the
             logged price — the same frictionless assumption the backtest makes.
             This keeps the held-back buckets scored without risking capital, but
             it is NOT a real fill, so treat its ROI the same way you treat the
             backtest's: an upper bound.

The comparison that matters: AUTO realized ROI (real fills + fees) vs the
backtest's predicted 8-12c net ROI. If realized lags and slippage is high,
fills are the leak — exactly what min-size live exists to measure.

Usage:
    python scripts/mlb_reconcile.py
    python scripts/mlb_reconcile.py --intents mlb_shadow_intents.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import sys as _sys
import os as _os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
from edgeflow.kalshi.client import KalshiClient

DEFAULT_INTENTS = Path(__file__).parent / "mlb_shadow_intents.csv"

FEE_TAKER = 0.07  # general/sports taker multiplier; confirm at kalshi.com/fee-schedule


# ── Pure helpers (unit-tested) ─────────────────────────
def taker_fee(price: float, contracts: float) -> float:
    """Kalshi taker entry fee in dollars, rounded up to the cent per order."""
    if price <= 0 or price >= 1 or contracts <= 0:
        return 0.0
    return math.ceil(FEE_TAKER * contracts * price * (1 - price) * 100) / 100


def edge_bucket(edge: float) -> str:
    e = abs(edge)
    if 0.05 <= e < 0.08:
        return "5-8c"
    if 0.08 <= e < 0.12:
        return "8-12c"
    if 0.12 <= e < 0.20:
        return "12-20c"
    if e >= 0.20:
        return ">20c"
    return "<5c"


def cost_price(signal: str, order_price: float) -> float:
    """
    Price actually risked per contract. BUY logs the ask (what you pay for YES).
    SELL logs the YES bid; the position you take is the NO side, costing 1 - bid.
    Mirrors the backtest's sizing convention so ROIs are comparable.
    """
    return order_price if signal == "BUY" else (1 - order_price)


def slippage_cents(signal: str, order_price: float, fill_price: float) -> float:
    """
    Signed slippage in cents, positive = filled WORSE than the signal's price.
    BUY: paying a higher YES price is worse. SELL: selling at a lower YES price
    is worse.
    """
    if signal == "BUY":
        return round((fill_price - order_price) * 100, 2)
    return round((order_price - fill_price) * 100, 2)


def win_from_result(signal: str, market_result: str) -> bool | None:
    """BUY wins if YES resolves; SELL (bet team loses) wins if NO resolves."""
    if market_result not in ("yes", "no"):
        return None
    return market_result == "yes" if signal == "BUY" else market_result == "no"


def hypothetical_pnl(signal: str, order_price: float, contracts: float,
                     market_result: str) -> tuple[bool | None, float]:
    """
    Counterfactual P&L at the logged price (backtest-equivalent, fee-adjusted).
    Returns (is_win, pnl_dollars). Used for SHADOW rows we never actually traded.
    """
    win = win_from_result(signal, market_result)
    if win is None:
        return None, 0.0
    cp  = cost_price(signal, order_price)
    fee = taker_fee(cp, contracts)
    if win:
        return True, round((1 - cp) * contracts - fee, 4)
    return False, round(-cp * contracts - fee, 4)


# ── Data loading ───────────────────────────────────────
def load_intents(path: Path) -> list[dict]:
    if not path.exists():
        print(f"No intent log at {path} — run the scanner first (dry run is fine).")
        _sys.exit(1)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("exec_edge", "order_price", "kalshi_bid", "kalshi_ask"):
            try:
                r[k] = float(r[k])
            except (ValueError, KeyError, TypeError):
                r[k] = 0.0
        try:
            r["contracts"] = int(float(r["contracts"]))
        except (ValueError, KeyError, TypeError):
            r["contracts"] = 0
    return rows


# ── Kalshi pulls (paginated) ───────────────────────────
async def fetch_all(client_method, key: str, max_pages: int = 20) -> list[dict]:
    out, cursor = [], None
    for _ in range(max_pages):
        data = await client_method(limit=100, cursor=cursor) if cursor else await client_method(limit=100)
        out.extend(data.get(key, []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


async def fetch_market_results(client: KalshiClient, event_tickers: set[str]) -> dict[str, str]:
    """Map market_ticker -> result ('yes'/'no'/'') by querying each event once."""
    results: dict[str, str] = {}
    for ev in event_tickers:
        if not ev:
            continue
        try:
            data = await client.get_markets(event_ticker=ev, limit=100)
            for m in data.get("markets", []):
                results[m.get("ticker", "")] = m.get("result", "") or ""
        except Exception as e:
            print(f"  market lookup failed for {ev}: {e}")
    return results


# ── Reconcile ──────────────────────────────────────────
def match_fill(intent: dict, fills_by_ticker: dict[str, list[dict]]) -> dict | None:
    """Nearest fill on the same market ticker at/after the intent timestamp."""
    cands = fills_by_ticker.get(intent.get("market_ticker", ""), [])
    if not cands:
        return None
    # Position lock means ~1 position per game, so ticker is near-unique. Take the
    # earliest fill on that ticker (the opening trade).
    return sorted(cands, key=lambda f: f.get("created_time", ""))[0]


async def main():
    ap = argparse.ArgumentParser(description="Reconcile MLB shadow intents vs real fills/settlements")
    ap.add_argument("--intents", type=Path, default=DEFAULT_INTENTS)
    args = ap.parse_args()

    intents = load_intents(args.intents)
    auto    = [r for r in intents if r.get("mode") == "AUTO"]
    shadow  = [r for r in intents if r.get("mode") in ("SHADOW", "AUTO_DRY")]
    print(f"Loaded {len(intents)} intents — {len(auto)} live AUTO, {len(shadow)} shadow/dry\n")

    client = KalshiClient()

    # Pull reality
    print("Fetching fills, settlements, and market results from Kalshi...")
    try:
        fills       = await fetch_all(client.get_fills, "fills")
        settlements = await fetch_all(client.get_settlements, "settlements")
    except Exception as e:
        print(f"Kalshi fetch failed: {e}")
        _sys.exit(1)

    fills_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        fills_by_ticker[f.get("ticker", "")].append(f)
    settle_by_ticker = {s.get("ticker", ""): s for s in settlements}

    event_tickers = {r.get("event_ticker", "") for r in intents}
    market_results = await fetch_market_results(client, event_tickers)
    print()

    # ── AUTO: realized, real fills + real fees ─────────
    print("=" * 64)
    print("LIVE / AUTO  (realized — real fills, real Kalshi fees)")
    print("=" * 64)
    filled = 0
    slips, a_staked, a_pnl, a_wins, a_losses = [], 0.0, 0.0, 0, 0
    for r in auto:
        fill = match_fill(r, fills_by_ticker)
        if not fill:
            print(f"  {r['team']:<14} {r['signal']}  — no fill found (limit may not have filled)")
            continue
        filled += 1
        fill_price = float(fill.get("yes_price", 0)) / 100
        slip = slippage_cents(r["signal"], r["order_price"], fill_price)
        slips.append(slip)

        s = settle_by_ticker.get(r.get("market_ticker", ""))
        if not s:
            print(f"  {r['team']:<14} {r['signal']}  filled @ {fill_price:.2f} (slip {slip:+.1f}c) — not settled yet")
            continue
        result = s.get("market_result", "")
        value  = float(s.get("value", 0))
        side   = "yes" if r["signal"] == "BUY" else "no"
        cost   = float(s.get("yes_total_cost_dollars", 0)) if side == "yes" else float(s.get("no_total_cost_dollars", 0))
        cnt    = float(s.get(f"{side}_count_fp", r["contracts"]))
        fee    = float(s.get("fee_cost", 0))
        win    = (side == result)
        pnl    = (cnt * value / 100 - cost - fee) if win else (-cost - fee)
        a_staked += cost
        a_pnl    += pnl
        a_wins   += int(win)
        a_losses += int(not win)
        print(f"  {r['team']:<14} {r['signal']}  filled @ {fill_price:.2f} (slip {slip:+.1f}c)  "
              f"{'WIN ' if win else 'LOSS'}  P&L ${pnl:+.2f}")

    n_auto = a_wins + a_losses
    if auto:
        fill_rate = filled / len(auto) * 100
        avg_slip  = sum(slips) / len(slips) if slips else 0.0
        roi       = a_pnl / a_staked * 100 if a_staked else 0.0
        wr        = a_wins / n_auto * 100 if n_auto else 0.0
        print(f"\n  Fill rate               : {filled}/{len(auto)}  ({fill_rate:.0f}%)")
        print(f"  Avg slippage vs logged  : {avg_slip:+.2f}c   (positive = filled worse than the signal price)")
        print(f"  Settled positions       : {n_auto}")
        print(f"  Win rate                : {wr:.1f}%  ({a_wins}W / {a_losses}L)")
        print(f"  Net P&L (real fees)     : ${a_pnl:+.2f}")
        print(f"  Net ROI                 : {roi:+.1f}%")
        print(f"  Backtest predicted 8-12c: +38.4% net, ~56% win   <-- compare")
        if n_auto and (roi < 20 or avg_slip > 1.0):
            print("  >> Realized lags backtest and/or slippage is high — fills are the leak.")
    else:
        print("  No live AUTO orders yet. Flip MLB_LIVE=true (cap=1) to start measuring fills.")

    # ── SHADOW: hypothetical at logged price ───────────
    print("\n" + "=" * 64)
    print("SHADOW  (hypothetical at logged price — backtest assumption, NOT real fills)")
    print("=" * 64)
    by_bucket = defaultdict(lambda: {"n": 0, "w": 0, "staked": 0.0, "pnl": 0.0, "pending": 0})
    for r in shadow:
        result = market_results.get(r.get("market_ticker", ""), "")
        win, pnl = hypothetical_pnl(r["signal"], r["order_price"], r["contracts"], result)
        b = by_bucket[edge_bucket(r["exec_edge"])]
        if win is None:
            b["pending"] += 1
            continue
        cp = cost_price(r["signal"], r["order_price"])
        b["n"] += 1
        b["w"] += int(win)
        b["staked"] += cp * r["contracts"]
        b["pnl"] += pnl
    print(f"  {'Bucket':<8}{'N':>5}{'Win%':>8}{'Staked':>10}{'HypNetROI':>12}{'Pending':>9}")
    print("  " + "-" * 50)
    for b in ("5-8c", "8-12c", "12-20c", ">20c"):
        d = by_bucket.get(b)
        if not d or (d["n"] == 0 and d["pending"] == 0):
            continue
        wr  = d["w"] / d["n"] * 100 if d["n"] else 0.0
        roi = d["pnl"] / d["staked"] * 100 if d["staked"] else 0.0
        print(f"  {b:<8}{d['n']:>5}{wr:>7.1f}%{d['staked']:>10.2f}{roi:>+11.1f}%{d['pending']:>9}")
    print("\n  (Shadow ROI assumes you filled at the logged price — same caveat as the")
    print("   backtest. The AUTO block above is the only fill-real number.)")


if __name__ == "__main__":
    asyncio.run(main())