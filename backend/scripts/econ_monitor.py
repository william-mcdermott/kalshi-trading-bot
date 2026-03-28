#!/usr/bin/env python3
"""
econ_monitor.py

Fetches Kalshi pricing for upcoming CPI, payrolls, unemployment, and Fed events.
Sends a summary iMessage and prints to stdout.

Run manually or via launchd before major data releases.
Usage: python scripts/econ_monitor.py

Schedule:
  - April 3  8:25am ET — Payrolls + Unemployment
  - April 10 8:25am ET — CPI (March data)
"""

import asyncio
import subprocess
from datetime import datetime, timezone

import httpx

IMESSAGE_NUMBER = "5129928658"

EVENTS = {
    "CPI MoM":      ("KXECONSTATCPI",     "KXECONSTATCPI-26MAR",     "exact",     "%"),
    "Core CPI MoM": ("KXECONSTATCPICORE", "KXECONSTATCPICORE-26MAR", "exact",     "%"),
    "Payrolls":     ("KXPAYROLLS",         None,                      "threshold", "k jobs"),
    "Unemployment": ("KXECONSTATU3",       None,                      "exact",     "%"),
    "Fed Apr":      ("KXFED",              "KXFED-26APR",             "threshold", "%"),
}


def fmt_strike(strike: float, unit: str) -> str:
    if unit == "k jobs":
        return f"{strike/1000:.0f}k"
    return f"{strike:.1f}%"


def send_imessage(message: str):
    # Escape quotes for AppleScript
    safe = message.replace('"', "'").replace("\\", "")
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{IMESSAGE_NUMBER}" of targetService
        send "{safe}" to targetBuddy
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True)
    if result.returncode != 0:
        print(f"iMessage failed: {result.stderr.decode().strip()}")


async def get_next_event(series_ticker: str) -> str | None:
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/events",
            params={"limit": 10, "status": "open", "series_ticker": series_ticker},
        )
        events = r.json().get("events", [])
        if not events:
            return None
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


async def analyze_event(
    name: str,
    series: str,
    event_ticker: str | None,
    market_type: str = "exact",
    unit: str = "%",
) -> tuple[str, str]:
    """
    Returns (summary_line, detail_block) for console + iMessage.
    summary_line: one-liner e.g. "CPI MoM: 0.9% most likely (32%)"
    detail_block: multi-line block for console output
    """
    if event_ticker is None:
        event_ticker = await get_next_event(series)
    if not event_ticker:
        return f"{name}: no open events", f"{name}: no open events found"

    markets = await get_markets(event_ticker)
    if not markets:
        return f"{name}: no markets", f"{name} ({event_ticker}): no markets found"

    close_time = markets[0].get("close_time", "unknown")

    # Build distribution
    distribution = []
    for m in markets:
        strike = parse_strike(m["ticker"])
        if strike is None:
            continue
        bid   = float(m.get("yes_bid_dollars") or 0)
        ask   = float(m.get("yes_ask_dollars") or 0)
        mid   = (bid + ask) / 2
        vol24 = float(m.get("volume_24h_fp") or 0)
        if mid > 0.01:
            distribution.append((strike, mid, bid, ask, vol24))

    distribution.sort(key=lambda x: x[1], reverse=True)

    # Console detail block
    lines = [
        f"\n{'='*60}",
        f"{name}  |  {event_ticker}",
        f"Closes: {close_time}",
        f"{'Strike':<12} {'Mid':>6} {'Bid':>6} {'Ask':>6} {'Vol24h':>8}",
        "-" * 45,
    ]

    total_prob = 0.0
    for strike, mid, bid, ask, vol in distribution[:8]:
        total_prob += mid
        bar = "█" * int(mid * 30)
        lines.append(
            f"  {fmt_strike(strike, unit):>8}   {mid:>5.2f}  {bid:>5.2f}  {ask:>5.2f}  {vol:>8.0f}  {bar}"
        )

    if market_type == "exact":
        lines.append(f"\n  Implied probs sum: {total_prob:.2f} (should be ~1.0)")
    else:
        lines.append(f"\n  Threshold market — prices are independent probabilities")

    if distribution:
        top = distribution[0]
        lines.append(f"  Most likely: {fmt_strike(top[0], unit)} at {top[1]:.0%} probability")

    detail_block = "\n".join(lines)

    # Short summary for iMessage
    if distribution:
        top    = distribution[0]
        second = distribution[1] if len(distribution) > 1 else None
        summary = f"{name}: {fmt_strike(top[0], unit)} ({top[1]:.0%})"
        if second:
            summary += f" · {fmt_strike(second[0], unit)} ({second[1]:.0%})"
    else:
        summary = f"{name}: no liquid markets"

    return summary, detail_block


async def main():
    now = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
    header = f"Econ Monitor — {now}"
    print(header)

    summaries = [header]
    details   = []

    for name, (series, event, mtype, unit) in EVENTS.items():
        try:
            summary, detail = await analyze_event(name, series, event, mtype, unit)
            summaries.append(f"  {summary}")
            details.append(detail)
            print(detail)
        except Exception as e:
            msg = f"{name}: error — {e}"
            summaries.append(f"  {msg}")
            print(f"\n{msg}")

    # Send compact iMessage
    imessage_text = "\n".join(summaries)
    print(f"\n--- iMessage ---\n{imessage_text}\n")
    send_imessage(imessage_text)
    print("iMessage sent.")


if __name__ == "__main__":
    asyncio.run(main())
