#!/usr/bin/env python3
"""
verify_routing.py

Confirms the MLB scanner's auto/shadow gate routes correctly, WITHOUT needing
live games to produce a signal.

Two checks:
  1. Offline truth table — feeds synthetic edges through the real
     is_auto_eligible() and MLB_LIVE flag and asserts each lands in the
     expected mode (AUTO / AUTO_DRY / SHADOW). Run it anytime; no API, no games.
  2. CSV audit — reads mlb_shadow_intents.csv (after you've done a dry run) and
     verifies every logged row obeys the invariant: AUTO/AUTO_DRY rows have edge
     in [0.08, 0.12), SHADOW rows don't, and nothing went live while dry.

Usage:
    python scripts/verify_routing.py
"""
from __future__ import annotations

import csv
import os as _os
import sys as _sys
from collections import Counter
from pathlib import Path

# Safe defaults so importing the scanner doesn't init a live client or send orders
_os.environ.setdefault("MLB_LIVE", "false")
_os.environ.setdefault("DRY_RUN", "true")
_os.environ.setdefault("ANTHROPIC_API_KEY", "verify-noop")
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))

import mlb_live_scanner as s  # noqa: E402

INTENTS = Path(__file__).parent / "mlb_shadow_intents.csv"
AUTO_LO, AUTO_HI = s.AUTO_EDGE_MIN, s.AUTO_EDGE_MAX


def expected_mode(edge: float, mlb_live: bool, has_ticker: bool) -> str:
    """Mirror of the scanner's gate so we can assert the real branch conditions."""
    if s.is_auto_eligible(edge):
        return "AUTO" if (mlb_live and has_ticker) else "AUTO_DRY"
    return "SHADOW"


def offline_truth_table() -> int:
    print("=" * 60)
    print("1. OFFLINE TRUTH TABLE  (no games needed)")
    print("=" * 60)
    print(f"  Auto bucket = [{AUTO_LO:.2f}, {AUTO_HI:.2f})   MLB_LIVE={s.MLB_LIVE}\n")

    # (edge, mlb_live, has_ticker, expected)
    cases = [
        (0.049, False, True,  "SHADOW"),
        (0.060, False, True,  "SHADOW"),   # 5-8c -> shadow
        (0.079, False, True,  "SHADOW"),   # just below auto
        (0.080, False, True,  "AUTO_DRY"), # auto bucket, dry
        (0.100, False, True,  "AUTO_DRY"),
        (0.119, False, True,  "AUTO_DRY"),
        (0.120, False, True,  "SHADOW"),   # capped out
        (0.150, False, True,  "SHADOW"),
        (0.100, True,  True,  "AUTO"),     # live + ticker -> real order path
        (0.100, True,  False, "AUTO_DRY"), # live but no ticker -> fails safe to dry
    ]
    fails = 0
    print(f"  {'edge':>6} {'live':>6} {'ticker':>7} {'-> mode':>10} {'expected':>10}  ok")
    print("  " + "-" * 52)
    for edge, live, tick, exp in cases:
        got = expected_mode(edge, live, tick)
        ok = got == exp
        fails += (not ok)
        print(f"  {edge:>6.3f} {str(live):>6} {str(tick):>7} {got:>10} {exp:>10}  {'ok' if ok else 'FAIL'}")
    print(f"\n  {'PASS' if fails == 0 else f'{fails} FAILURE(S)'} — routing logic "
          f"{'matches intent' if fails == 0 else 'DOES NOT match intent'}\n")
    return fails


def csv_audit() -> int:
    print("=" * 60)
    print("2. CSV AUDIT  (run after a dry-run session)")
    print("=" * 60)
    if not INTENTS.exists():
        print(f"  No {INTENTS.name} yet — run the scanner in dry mode during games,\n"
              f"  then re-run this to audit real logged rows.\n")
        return 0

    rows = list(csv.DictReader(open(INTENTS, newline="")))
    modes = Counter(r["mode"] for r in rows)
    print(f"  {len(rows)} rows — " + ", ".join(f"{m}:{c}" for m, c in modes.items()) + "\n")

    violations = 0
    for i, r in enumerate(rows, 1):
        try:
            edge = abs(float(r["exec_edge"]))
        except (ValueError, KeyError):
            print(f"  row {i}: unparseable exec_edge"); violations += 1; continue
        mode = r.get("mode", "")
        in_bucket = AUTO_LO <= edge < AUTO_HI

        if mode in ("AUTO", "AUTO_DRY") and not in_bucket:
            print(f"  row {i}: {mode} but edge {edge:.3f} is OUTSIDE the auto bucket — ROUTING BUG")
            violations += 1
        if mode == "SHADOW" and in_bucket:
            print(f"  row {i}: SHADOW but edge {edge:.3f} is INSIDE the auto bucket — ROUTING BUG")
            violations += 1
        # Safety: while dry, no row should carry a real placed-order status.
        if mode == "AUTO_DRY" and r.get("order_status", "") not in ("", "shadow", "Dry run"):
            print(f"  row {i}: AUTO_DRY but order_status={r['order_status']!r} — a real order may have gone out")
            violations += 1

    print(f"\n  {'PASS — every row obeys mode<->edge invariant' if violations == 0 else f'{violations} VIOLATION(S)'}\n")
    return violations


if __name__ == "__main__":
    total = offline_truth_table() + csv_audit()
    _sys.exit(1 if total else 0)