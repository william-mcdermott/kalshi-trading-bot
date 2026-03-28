#!/usr/bin/env python3
"""
econ_monitor.py

Fetches Kalshi pricing for upcoming CPI, payrolls, and Fed events
and prints a summary of what the market is pricing.

Run manually before major data releases to spot mispricings.
Usage: python scripts/econ_monitor.py
"""

import asyncio
import httpx
from datetime import datetime, timezone


EVENTS = {
    "CPI MoM":      ("KXECONSTATCPI",     "KXECONSTATCPI-26MAR",  "exact"),
    "Core CPI MoM": ("KXECONSTATCPICORE", "KXECONSTATCPICORE-26MAR", "exact"),
    "Payrolls":     ("KXPAYROLLS",         None,                   "threshold"),
    "Unemployment": ("KXECONSTATU3",       None,                   "exact"),
    "Fed Apr":      ("KXFED",              "KXFED-26APR",          "threshold"),
}


async def get_next_event(series_ticker: str) -> str | None:
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/events",
            params={"limit": 10, "status": "open", "series_ticker": series_ticker},
        )
        events = r.json().get("events", [])
        if not events:
            return None
        # Return last in list (nearest chronologically — API returns newest first)
        return events[-1]["event_ticker"]


async def get_markets(event_ticker: str) -> list[dict]:
    all_markets = []
    cursor = None
    async with httpx.AsyncClient(timeout=10.0) as http:
        while True:
            params = {"limit": 100, "status": "open", "event_ticker": event_ticker}
            if cursor:
                params["cursor"] = cursor
            r = await http.get(
                "https://api.elections.kalshi.com/trade-api/v2/markets",
                params=params,
            )
            data    = r.json()
            markets = data.get("markets", [])
            cursor  = data.get("cursor", "")
            all_markets.extend(markets)
            if not cursor or not markets:
                break
    return all_markets


def parse_strike(ticker: str) -> float | None:
    try:
        return float(ticker.split("-T")[-1])
    except Exception:
        return None


async def analyze_event(name: str, series: str, event_ticker: str | None, market_type: str = "exact"):
    if event_ticker is None:
        event_ticker = await get_next_event(series)
    if not event_ticker:
        print(f"\n{name}: no open events found")
        return

    markets = await get_markets(event_ticker)
    if not markets:
        print(f"\n{name} ({event_ticker}): no markets found")
        return

    # Build distribution
    distribution = []
    for m in markets:
        strike = parse_strike(m["ticker"])
        if strike is None:
            continue
        bid    = float(m.get("yes_bid_dollars") or 0)
        ask    = float(m.get("yes_ask_dollars") or 0)
        mid    = (bid + ask) / 2
        vol24  = float(m.get("volume_24h_fp") or 0)
        if mid > 0:
            distribution.append((strike, mid, bid, ask, vol24))

    distribution.sort(key=lambda x: x[1], reverse=True)

    close_time = markets[0].get("close_time", "unknown")

    print(f"\n{'='*60}")
    print(f"{name}  |  {event_ticker}")
    print(f"Closes: {close_time}")
    print(f"{'Strike':<12} {'Mid':>6} {'Bid':>6} {'Ask':>6} {'Vol24h':>8}")
    print("-" * 45)

    total_prob = 0
    for strike, mid, bid, ask, vol in distribution[:8]:
        total_prob += mid
        bar = "█" * int(mid * 30)
        print(f"  {strike:>6.1f}%   {mid:>5.2f}  {bid:>5.2f}  {ask:>5.2f}  {vol:>8.0f}  {bar}")

    if market_type == "exact":
        print(f"\n  Implied probs sum: {total_prob:.2f} (should be ~1.0 for mutually exclusive)")
    else:
        print(f"\n  Threshold market — prices are independent probabilities, not a distribution")

    # Most likely outcome
    if distribution:
        top = distribution[0]
        print(f"  Most likely: {top[0]:.1f}% at {top[1]:.0%} probability")


async def main():
    print(f"Economic Market Monitor")
    print(f"Run at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Next CPI release: April 10, 2026 at 8:30am ET (March data)")

    for name, (series, event, mtype) in EVENTS.items():
        try:
            await analyze_event(name, series, event, mtype)
        except Exception as e:
            print(f"\n{name}: error — {e}")


if __name__ == "__main__":
    asyncio.run(main())