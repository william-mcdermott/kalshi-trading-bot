#!/usr/bin/env python3
"""
wti_scanner.py

Scans Kalshi WTI oil (KXWTI) markets for edge opportunities using a
fair value model calibrated to WTI's actual hourly volatility (0.92%/hr).

Settles at 2:30pm EDT (18:30 UTC) daily — different from BTC/gold.
Results logged to wti_scanner_log.csv for post-settlement validation.

Usage:
    python scripts/wti_scanner.py

WARNING: WTI has high geopolitical tail risk (Iran war).
Max single-hour move observed: 17%. Model assumes normal vol.
Use small position sizes until strategy is validated.
"""

import asyncio
import csv
import math
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import yfinance as yf

from app.utils.market_regime import get_regime

# ── Config ─────────────────────────────────────────────
WTI_SERIES      = "KXWTI"
WTI_VOL         = 0.92          # fallback only — replaced by implied vol each scan
MIN_EDGE        = 0.08
MIN_VOL_24H     = 1000          # higher bar than BTC/gold — only liquid markets
MONEYNESS_BAND  = 0.020         # only score thresholds within ±2.0% of WTI price
MIN_HOURS       = 0.5           # don't generate signals within 30 min of settlement
IMESSAGE_NUMBER = "5129928658"
LOG_FILE        = Path(__file__).parent / "wti_scanner_log.csv"
SETTLEMENT_HOUR = 18            # 2:30pm EDT = 18:30 UTC
SETTLEMENT_MIN  = 30


# ── Math ───────────────────────────────────────────────
def normal_cdf(x: float) -> float:
    t    = 1 / (1 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
           + t * (-1.821255978 + t * 1.330274429))))
    p    = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-x * x / 2) * poly
    return p if x >= 0 else 1 - p


def fair_value(price: float, threshold: float, hours: float, vol: float = WTI_VOL) -> float:
    if hours <= 0:
        return 1.0 if price >= threshold else 0.0
    dist      = (price - threshold) / threshold * 100
    total_vol = vol * math.sqrt(hours)
    z         = dist / total_vol
    return round(normal_cdf(z), 4)


def get_implied_vol(wti_price: float, markets: list[dict], hours: float) -> tuple[float, str]:
    """
    Back out implied vol from above-price strikes in the 0.5%–1.5% OTM range.
    WTI has high geopolitical tail risk so the vol smile is steep —
    anchoring at band-edge OTM strikes gives a more realistic calibration
    than using the nearest-ATM strike.
    """
    if hours <= 0:
        return WTI_VOL, "fallback"

    candidates = []
    for m in markets:
        bid = float(m.get("yes_bid_dollars") or 0)
        ask = float(m.get("yes_ask_dollars") or 0)
        if bid == 0 and ask == 0:
            continue
        mid = (bid + ask) / 2
        try:
            threshold = float(m["ticker"].split("-T")[-1])
        except Exception:
            continue

        moneyness = (threshold - wti_price) / wti_price
        if not (0.005 <= moneyness <= 0.015):
            continue
        if mid < 0.05 or mid > 0.60:
            continue

        spread = ask - bid
        candidates.append((abs(mid - 0.30), spread, mid, threshold, m["ticker"]))

    if not candidates:
        return WTI_VOL, "fallback"

    candidates.sort(key=lambda x: (x[0], x[1]))
    _, _, market_mid, threshold, ticker = candidates[0]

    # Bisection — threshold always above price here
    lo, hi = 0.01, 10.0
    for _ in range(60):
        mid_vol = (lo + hi) / 2
        fv      = fair_value(wti_price, threshold, hours, mid_vol)
        if fv < market_mid:
            lo = mid_vol
        else:
            hi = mid_vol
        if hi - lo < 1e-6:
            break

    implied = round((lo + hi) / 2, 4)

    if implied < 0.02 or implied > 9.9:
        return WTI_VOL, "fallback"

    return implied, ticker


def get_implied_wti_price(markets: list[dict]) -> float | None:
    """
    Back out implied WTI price from Kalshi market prices.
    Interpolates where yes_mid crosses 0.50 between bracketing strikes.
    Used to detect stale Yahoo Finance data — critical given WTI's volatility.
    """
    points = []
    for m in markets:
        bid = float(m.get("yes_bid_dollars") or 0)
        ask = float(m.get("yes_ask_dollars") or 0)
        if bid == 0 and ask == 0:
            continue
        mid = (bid + ask) / 2
        try:
            threshold = float(m["ticker"].split("-T")[-1])
        except Exception:
            continue
        points.append((threshold, mid))

    points.sort(key=lambda x: x[0])

    if len(points) < 2:
        return None

    for i in range(len(points) - 1):
        t_lo, mid_lo = points[i]
        t_hi, mid_hi = points[i + 1]
        if mid_lo >= 0.50 >= mid_hi:
            if mid_lo == mid_hi:
                return (t_lo + t_hi) / 2
            frac    = (mid_lo - 0.50) / (mid_lo - mid_hi)
            implied = t_lo + frac * (t_hi - t_lo)
            return round(implied, 2)

    return None


# ── Price feed ─────────────────────────────────────────
def get_wti_price() -> float:
    """Fetch current WTI crude oil futures price from Yahoo Finance (CL=F)."""
    data   = yf.download("CL=F", period="1d", interval="1m", progress=False)
    closes = data["Close"].squeeze().dropna()
    if closes.empty:
        raise ValueError("No WTI price data available")
    return float(closes.iloc[-1])


# ── Kalshi API ─────────────────────────────────────────
async def get_next_wti_event() -> str | None:
    """Returns the ticker for the nearest open WTI event."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        r      = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/events",
            params={"limit": 5, "status": "open", "series_ticker": WTI_SERIES},
        )
        events = r.json().get("events", [])
        if not events:
            return None
        events.sort(key=lambda e: e.get("strike_date", ""))
        return events[0]["event_ticker"]


async def get_wti_markets(event_ticker: str) -> list[dict]:
    """Fetch all open markets for a given WTI event."""
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


# ── Settlement time ─────────────────────────────────────
def hours_to_settlement() -> float:
    """WTI settles at 2:30pm EDT = 18:30 UTC."""
    now        = datetime.now(timezone.utc)
    settlement = now.replace(
        hour=SETTLEMENT_HOUR, minute=SETTLEMENT_MIN, second=0, microsecond=0
    )
    if now >= settlement:
        settlement += timedelta(days=1)
    # Skip weekends
    while settlement.weekday() >= 5:
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
    event_ticker: str,
    wti_price:    float,
    price_source: str,
    hours:        float,
    implied_vol:  float,
    vol_source:   str,
    opportunities: list[dict],
):
    write_header = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "scan_time", "event_ticker", "wti_price", "price_source", "hours_left",
                "implied_vol", "vol_source",
                "threshold", "moneyness_pct", "in_band",
                "bid", "ask", "fair_value", "buy_edge", "sell_edge",
                "vol_24h", "signal",
            ])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for o in opportunities:
            writer.writerow([
                now, event_ticker, f"{wti_price:.2f}", price_source, f"{hours:.1f}",
                f"{implied_vol:.4f}", vol_source,
                o["threshold"], f"{o['moneyness']:.3f}", o["in_band"],
                o["bid"], o["ask"], o["fv"],
                f"{o['buy_edge']:.4f}", f"{o['sell_edge']:.4f}",
                o["vol24"], o["signal"],
            ])


# ── Main ───────────────────────────────────────────────
async def main():
    print(f"WTI Oil Scanner — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    # ── Regime check ───────────────────────────────────
    regime, vix, regime_msg = get_regime()
    print(f"Regime:      {regime_msg}")
    if regime == "HIGH":
        print("  Skipping signals — fat tail risk invalidates normal-vol model")
        send_imessage(
            f"🛢️ WTI Scanner — {datetime.now(timezone.utc).strftime('%b %d %H:%M UTC')}\n"
            f"  ⛔ {regime_msg}\n"
            f"  No signals generated — resume when VIX < 30"
        )
        return
    print()

    # Get next event + markets first (needed for implied price)
    event_ticker = await get_next_wti_event()
    if not event_ticker:
        print("No open WTI events found")
        return
    print(f"Event:       {event_ticker}")

    markets = await get_wti_markets(event_ticker)
    print(f"Markets:     {len(markets)}")

    # Hours to settlement
    hours = hours_to_settlement()
    print(f"Hours left:  {hours:.1f}  (settles 2:30pm EDT)")

    if hours > 20:
        print("⚠️  >20hrs to settlement — high overnight geopolitical risk")

    # Price: Yahoo Finance + Kalshi-implied cross-check
    try:
        yahoo_price = get_wti_price()
    except Exception as e:
        print(f"  Yahoo Finance failed: {e}")
        yahoo_price = None

    kalshi_implied_price = get_implied_wti_price(markets)

    if yahoo_price and kalshi_implied_price:
        discrepancy = abs(yahoo_price - kalshi_implied_price)
        if discrepancy > 1.50:  # WTI $1.50 threshold — tighter than SPX
            wti_price    = kalshi_implied_price
            price_source = f"Kalshi-implied (Yahoo=${yahoo_price:.2f} stale, Δ=${discrepancy:.2f})"
        else:
            wti_price    = yahoo_price
            price_source = f"Yahoo (Kalshi-implied=${kalshi_implied_price:.2f}, Δ=${discrepancy:.2f})"
    elif kalshi_implied_price:
        wti_price    = kalshi_implied_price
        price_source = "Kalshi-implied (Yahoo unavailable)"
    elif yahoo_price:
        wti_price    = yahoo_price
        price_source = "Yahoo (Kalshi implied unavailable)"
    else:
        print("Failed to get WTI price from any source")
        return

    print(f"WTI price:   ${wti_price:.2f}  [{price_source}]")

    # Too close to settlement — implied vol unreliable, signals meaningless
    if hours < MIN_HOURS:
        print(f"⚠️  {hours:.2f}hrs to settlement — within MIN_HOURS ({MIN_HOURS}), skipping signals")
        send_imessage(f"🛢️ WTI Scanner — {datetime.now(timezone.utc).strftime('%b %d %H:%M UTC')}\n  {hours:.2f}hrs to settle — too close, no signals")
        return

    # Implied vol from band-edge OTM strikes
    implied_vol, vol_source = get_implied_vol(wti_price, markets, hours)
    if vol_source == "fallback":
        print(f"Implied vol: {implied_vol:.4f}%/hr  ⚠️  (fallback)")
    else:
        print(f"Implied vol: {implied_vol:.4f}%/hr  (from {vol_source})")
    print()

    # Calculate edge
    is_fallback = vol_source == "fallback"
    opportunities = []
    for m in markets:
        bid   = float(m.get("yes_bid_dollars") or 0)
        ask   = float(m.get("yes_ask_dollars") or 0)
        if bid == 0 and ask == 0:
            continue
        mid   = (bid + ask) / 2
        if mid < 0.03 or mid > 0.97:
            continue
        vol24 = float(m.get("volume_24h_fp") or 0)

        try:
            threshold = float(m["ticker"].split("-T")[-1])
        except Exception:
            continue

        fv        = fair_value(wti_price, threshold, hours, implied_vol)
        buy_edge  = fv - ask
        sell_edge = bid - fv

        moneyness = abs(threshold - wti_price) / wti_price
        in_band   = moneyness <= MONEYNESS_BAND

        signal = ""
        if in_band and vol24 >= MIN_VOL_24H:
            if buy_edge >= MIN_EDGE:
                signal = "WEAK_BUY" if is_fallback else "BUY"
            elif sell_edge >= MIN_EDGE:
                signal = "WEAK_SELL" if is_fallback else "SELL"
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
            "moneyness": round(moneyness * 100, 3),
            "in_band":   in_band,
            "vol24":     vol24,
            "signal":    signal,
        })

    opportunities.sort(key=lambda x: max(x["buy_edge"], x["sell_edge"]), reverse=True)

    # Print
    band_pct = MONEYNESS_BAND * 100
    lo_band  = wti_price * (1 - MONEYNESS_BAND)
    hi_band  = wti_price * (1 + MONEYNESS_BAND)
    print(f"Moneyness band: ±{band_pct:.1f}%  (${lo_band:.2f} – ${hi_band:.2f})")
    print()
    print(f"{'Threshold':<12} {'Bid':<6} {'Ask':<6} {'FV':<8} {'BuyEdge':<10} {'SellEdge':<10} {'Vol24h':<8} {'Mness%':<8} Signal")
    print("-" * 88)

    strong = [o for o in opportunities if o["signal"] in ("BUY", "SELL")]
    weak   = [o for o in opportunities if o["signal"] in ("WEAK_BUY", "WEAK_SELL")]

    for o in opportunities[:20]:
        icon     = "✅" if o["signal"] == "BUY" else "🔴" if o["signal"] == "SELL" else "⚠️" if o["signal"].startswith("WEAK") else ""
        vol_flag = " ⚠️ low vol" if o["vol24"] < MIN_VOL_24H else ""
        oob_flag = "" if o["in_band"] else " [OOB]"
        print(
            f"${o['threshold']:<10,.2f} "
            f"{o['bid']:<6.3f} "
            f"{o['ask']:<6.3f} "
            f"{o['fv']:<8.3f} "
            f"{o['buy_edge']:+.3f}     "
            f"{o['sell_edge']:+.3f}     "
            f"{o['vol24']:<8.0f} "
            f"{o['moneyness']:<8.2f} "
            f"{icon} {o['signal']}{oob_flag}{vol_flag}"
        )

    print()
    print(f"Strong: {len(strong)}  Weak: {len(weak)}  No edge: {len(opportunities) - len(strong) - len(weak)}")

    # iMessage
    now_str = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
    lines   = [f"🛢️ WTI Scanner — {now_str}"]
    vol_tag = " ⚠️ fallback" if vol_source == "fallback" else f" ({vol_source.split('-T')[-1]})"
    lines.append(f"  WTI: ${wti_price:.2f}  |  {hours:.1f}hrs  |  vol={implied_vol:.3f}%/hr{vol_tag}")
    lines.append(f"  Event: {event_ticker}")

    if strong:
        lines.append(f"  Strong signals ({len(strong)}):")
        for o in strong[:3]:
            direction = "BUY" if o["signal"] == "BUY" else "SELL"
            edge      = o["buy_edge"] if direction == "BUY" else o["sell_edge"]
            lines.append(f"    {direction} ${o['threshold']:,.2f} edge={edge:.0%} vol={o['vol24']:.0f}")
        if regime == "ELEVATED":
            lines.append(f"  ⚠️  {regime_msg} — reduce size")
        lines.append(f"  🔗 https://kalshi.com/markets/{event_ticker}")
    else:
        lines.append("  No strong signals")

    message = "\n".join(lines)
    print()
    print("--- iMessage ---")
    print(message)
    send_imessage(message)
    print("iMessage sent.")

    # Log
    log_scan(event_ticker, wti_price, price_source, hours, implied_vol, vol_source, opportunities)
    print(f"Logged to {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())