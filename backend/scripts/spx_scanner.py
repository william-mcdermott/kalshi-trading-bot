#!/usr/bin/env python3
"""
spx_scanner.py

Scans Kalshi S&P 500 (KXINXU) markets for edge opportunities using a
fair value model calibrated to S&P 500's actual hourly volatility (~0.7%/hr).

Run daily to build a validation dataset before committing real capital.
Results are logged to spx_scanner_log.csv for post-settlement analysis.

Usage:
    python scripts/spx_scanner.py

Schedule via launchd or run manually before market open.
"""

import asyncio
import csv
import math
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import yfinance as yf

# ── Config ─────────────────────────────────────────────
SPX_SERIES       = "KXINXU"
SPX_VOL          = 0.70         # S&P 500 hourly vol % (higher than gold, similar to BTC)
MIN_EDGE         = 0.08         # minimum edge to flag as opportunity
MIN_VOL_24H      = 50           # minimum 24h volume to consider liquid
IMESSAGE_NUMBER  = "5129928658"
LOG_FILE         = Path(__file__).parent / "spx_scanner_log.csv"
SETTLEMENT_HOUR  = 20           # 4pm EDT = 20:00 UTC (S&P closes at 4pm, not 5pm)


# ── Math ───────────────────────────────────────────────
def normal_cdf(x: float) -> float:
    t    = 1 / (1 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
           + t * (-1.821255978 + t * 1.330274429))))
    p    = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-x * x / 2) * poly
    return p if x >= 0 else 1 - p


def fair_value(price: float, threshold: float, hours: float, vol: float = SPX_VOL) -> float:
    if hours <= 0:
        return 1.0 if price >= threshold else 0.0
    dist      = (price - threshold) / threshold * 100
    total_vol = vol * math.sqrt(hours)
    z         = dist / total_vol
    return round(normal_cdf(z), 4)


# ── Price feed ─────────────────────────────────────────
def get_spx_price() -> float:
    """
    Fetch current S&P 500 price from Yahoo Finance.
    Uses SPY ETF during market hours, ^GSPC for close price after hours.
    """
    # Try SPY first (more liquid, real-time during market hours)
    try:
        data   = yf.download("SPY", period="1d", interval="1m", progress=False)
        closes = data["Close"].squeeze().dropna()
        if not closes.empty:
            # SPY ~= SPX / 10, convert to index level
            spy_price = float(closes.iloc[-1])
            # Use ^GSPC directly instead for accuracy
            data2   = yf.download("^GSPC", period="1d", interval="1m", progress=False)
            closes2 = data2["Close"].squeeze().dropna()
            if not closes2.empty:
                return float(closes2.iloc[-1])
            return spy_price * 10  # fallback approximation
    except Exception:
        pass

    # Fallback: use ^GSPC directly
    data   = yf.download("^GSPC", period="1d", interval="1m", progress=False)
    closes = data["Close"].squeeze().dropna()
    if closes.empty:
        raise ValueError("No S&P 500 price data available")
    return float(closes.iloc[-1])


# ── Kalshi API ─────────────────────────────────────────
async def get_next_spx_event() -> str | None:
    """Returns the ticker for the nearest open S&P 500 event."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        r      = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/events",
            params={"limit": 5, "status": "open", "series_ticker": SPX_SERIES},
        )
        events = r.json().get("events", [])
        if not events:
            return None
        events.sort(key=lambda e: e.get("strike_date", ""))
        return events[0]["event_ticker"]


async def get_spx_markets(event_ticker: str) -> list[dict]:
    """Fetch all open markets for a given S&P 500 event."""
    all_markets = []
    cursor      = None
    async with httpx.AsyncClient(timeout=10.0) as http:
        while True:
            params = {"limit": 100, "status": "open", "event_ticker": event_ticker}
            if cursor:
                params["cursor"] = cursor
            r       = await http.get(
                "https://api.elections.kalshi.com/trade-api/v2/markets",
                params=params,
            )
            data    = r.json()
            markets = data.get("markets", [])
            all_markets.extend(markets)
            cursor  = data.get("cursor", "")
            if not cursor or not markets:
                break
    return all_markets


# ── Settlement time ────────────────────────────────────
def hours_to_settlement() -> float:
    """S&P 500 settles at 4pm EDT = 20:00 UTC."""
    now        = datetime.now(timezone.utc)
    settlement = now.replace(hour=SETTLEMENT_HOUR, minute=0, second=0, microsecond=0)
    if now >= settlement:
        settlement += timedelta(days=1)
    # Skip weekends — S&P doesn't trade Saturday/Sunday
    while settlement.weekday() >= 5:  # 5=Saturday, 6=Sunday
        settlement += timedelta(days=1)
    return (settlement - now).total_seconds() / 3600


# ── iMessage ───────────────────────────────────────────
def send_imessage(message: str):
    safe   = message.replace('"', "'").replace("\\", "")
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{IMESSAGE_NUMBER}" of targetService
        send "{safe}" to targetBuddy
    end tell
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True)


# ── CSV logging ────────────────────────────────────────
def log_scan(
    event_ticker:  str,
    spx_price:     float,
    hours:         float,
    opportunities: list[dict],
):
    write_header = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "scan_time", "event_ticker", "spx_price", "hours_left",
                "threshold", "bid", "ask", "fair_value", "buy_edge", "sell_edge",
                "vol_24h", "signal",
            ])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for o in opportunities:
            writer.writerow([
                now, event_ticker, f"{spx_price:.2f}", f"{hours:.1f}",
                o["threshold"], o["bid"], o["ask"], o["fv"],
                f"{o['buy_edge']:.4f}", f"{o['sell_edge']:.4f}",
                o["vol24"], o["signal"],
            ])


# ── Main ───────────────────────────────────────────────
async def main():
    print(f"S&P 500 Scanner — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    # Get S&P price
    try:
        spx_price = get_spx_price()
        print(f"S&P 500 price: {spx_price:,.2f}")
    except Exception as e:
        print(f"Failed to get S&P 500 price: {e}")
        return

    # Hours to settlement
    hours = hours_to_settlement()
    print(f"Hours left:    {hours:.1f}")

    # Check if market is closed (weekend or after hours)
    now = datetime.now(timezone.utc)
    edt_hour = (now.hour - 4) % 24  # EDT offset
    market_open = now.weekday() < 5 and 9 <= edt_hour < 16
    if not market_open:
        print("⚠️  Market is closed right now — prices may be stale")

    # Get next event
    event_ticker = await get_next_spx_event()
    if not event_ticker:
        print("No open S&P 500 events found")
        return
    print(f"Event:         {event_ticker}")
    print()

    # Get markets
    markets = await get_spx_markets(event_ticker)
    print(f"Markets:       {len(markets)}")
    print()

    # Calculate edge for each market
    opportunities = []
    for m in markets:
        bid   = float(m.get("yes_bid_dollars") or 0)
        ask   = float(m.get("yes_ask_dollars") or 0)
        if not bid and not ask:
            continue
        mid   = (bid + ask) / 2
        if mid < 0.03 or mid > 0.97:
            continue
        vol24 = float(m.get("volume_24h_fp") or 0)

        try:
            threshold = float(m["ticker"].split("-T")[-1])
        except Exception:
            continue

        fv        = fair_value(spx_price, threshold, hours)
        buy_edge  = fv - ask
        sell_edge = bid - fv

        signal = ""
        if buy_edge >= MIN_EDGE:
            signal = "BUY"
        elif sell_edge >= MIN_EDGE:
            signal = "SELL"
        elif buy_edge >= 0.04:
            signal = "WEAK_BUY"
        elif sell_edge >= 0.04:
            signal = "WEAK_SELL"

        opportunities.append({
            "threshold": threshold,
            "bid":       bid,
            "ask":       ask,
            "fv":        fv,
            "buy_edge":  buy_edge,
            "sell_edge": sell_edge,
            "vol24":     vol24,
            "signal":    signal,
        })

    # Sort by best edge
    opportunities.sort(
        key=lambda x: max(x["buy_edge"], x["sell_edge"]),
        reverse=True,
    )

    # Print results
    print(f"{'Threshold':<12} {'Bid':<6} {'Ask':<6} {'FV':<8} {'BuyEdge':<10} {'SellEdge':<10} {'Vol24h':<8} Signal")
    print("-" * 78)

    strong = [o for o in opportunities if o["signal"] in ("BUY", "SELL")]
    weak   = [o for o in opportunities if o["signal"] in ("WEAK_BUY", "WEAK_SELL")]

    for o in opportunities[:15]:
        icon = "✅" if o["signal"] == "BUY" else "🔴" if o["signal"] == "SELL" else "⚠️" if o["signal"].startswith("WEAK") else ""
        print(
            f"{o['threshold']:<12,.0f} "
            f"{o['bid']:<6.3f} "
            f"{o['ask']:<6.3f} "
            f"{o['fv']:<8.3f} "
            f"{o['buy_edge']:+.3f}     "
            f"{o['sell_edge']:+.3f}     "
            f"{o['vol24']:<8.0f} "
            f"{icon} {o['signal']}"
        )

    print()
    print(f"Strong signals: {len(strong)}  Weak: {len(weak)}  Vol calibrated to {SPX_VOL}%/hr")

    # iMessage summary
    now_str = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
    lines   = [f"📈 S&P Scanner — {now_str}"]
    lines.append(f"  SPX: {spx_price:,.0f}  |  {hours:.1f}hrs to settle")
    lines.append(f"  Event: {event_ticker}")

    if strong:
        lines.append(f"  Strong signals ({len(strong)}):")
        for o in strong[:3]:
            direction = "BUY" if o["signal"] == "BUY" else "SELL"
            edge      = o["buy_edge"] if direction == "BUY" else o["sell_edge"]
            lines.append(f"    {direction} {o['threshold']:,.0f} edge={edge:.0%} vol={o['vol24']:.0f}")
    else:
        lines.append("  No strong signals")

    message = "\n".join(lines)
    print()
    print("--- iMessage ---")
    print(message)
    send_imessage(message)
    print("iMessage sent.")

    # Log to CSV
    log_scan(event_ticker, spx_price, hours, opportunities)
    print(f"Logged to {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())