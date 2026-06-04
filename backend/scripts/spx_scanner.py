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

from app.utils.market_regime import get_regime
from app.services.trader import get_balance

# ── Config ─────────────────────────────────────────────
SPX_SERIES       = "KXINXU"
SPX_VOL          = 0.70         # fallback only — replaced by implied vol each scan
MIN_EDGE         = 0.08         # minimum edge to flag as opportunity
MIN_VOL_24H      = 50           # minimum 24h volume to consider liquid
MONEYNESS_BAND   = 0.015        # only score thresholds within ±1.5% of SPX price
MIN_HOURS        = 0.25         # don't generate signals within 15 min of settlement
KELLY_FRACTION   = 0.5          # half-Kelly to reduce variance
HEARTBEAT_FILE   = Path(__file__).parent / "spx_heartbeat.txt"
HEARTBEAT_HOURS  = 24           # send "no signals" heartbeat at most once per day
IMESSAGE_NUMBER  = "5129928658"
LOG_FILE         = Path(__file__).parent / "spx_scanner_log.csv"
SETTLEMENT_HOUR  = 20           # 4pm EDT = 20:00 UTC


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


def get_implied_vol(spx_price: float, markets: list[dict], hours: float) -> tuple[float, str]:
    """
    Back out implied vol from above-price strikes in the 0.5%–1.5% OTM range.
    Same logic as gold scanner — anchors vol calibration at the moneyness
    level where we're actually looking for edge, avoiding the steep near-ATM
    smile that would wildly overestimate far-strike fair values.
    """
    if hours <= 0:
        return SPX_VOL, "fallback"

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

        moneyness = (threshold - spx_price) / spx_price
        if not (0.005 <= moneyness <= 0.015):
            continue
        if mid < 0.05 or mid > 0.60:
            continue

        spread = ask - bid
        candidates.append((abs(mid - 0.30), spread, mid, threshold, m["ticker"]))

    if not candidates:
        return SPX_VOL, "fallback"

    candidates.sort(key=lambda x: (x[0], x[1]))
    _, _, market_mid, threshold, ticker = candidates[0]

    # Bisection — threshold always above price here
    lo, hi = 0.01, 10.0
    for _ in range(60):
        mid_vol = (lo + hi) / 2
        fv      = fair_value(spx_price, threshold, hours, mid_vol)
        if fv < market_mid:
            lo = mid_vol
        else:
            hi = mid_vol
        if hi - lo < 1e-6:
            break

    implied = round((lo + hi) / 2, 4)

    if implied < 0.02 or implied > 9.9:
        return SPX_VOL, "fallback"

    return implied, ticker


def get_implied_spx_price(markets: list[dict]) -> float | None:
    """
    Back out implied SPX price from Kalshi market prices.
    Finds where yes_mid crosses 0.50 and interpolates between bracketing strikes.
    Used to detect stale Yahoo Finance data.
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


# ── Kelly position sizing ──────────────────────────────
def kelly_size(edge: float, price: float, bankroll: float, fraction: float = KELLY_FRACTION) -> dict:
    if price <= 0 or price >= 1 or edge <= 0:
        return {"pct": 0.0, "full_dollar": 0.0, "frac_dollar": 0.0, "contracts": 0}
    kelly_pct   = edge / (1 - price)
    kelly_pct   = max(0.0, min(kelly_pct, 0.25))
    full_dollar = round(bankroll * kelly_pct, 2)
    frac_dollar = round(bankroll * kelly_pct * fraction, 2)
    contracts   = max(1, round(frac_dollar / price))
    return {
        "pct":         round(kelly_pct * 100, 1),
        "full_dollar": full_dollar,
        "frac_dollar": frac_dollar,
        "contracts":   contracts,
    }


# ── Heartbeat helpers ──────────────────────────────────
def should_send_heartbeat() -> bool:
    if not HEARTBEAT_FILE.exists():
        return True
    try:
        last = datetime.fromisoformat(HEARTBEAT_FILE.read_text().strip())
        hours_since = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return hours_since >= HEARTBEAT_HOURS
    except Exception:
        return True


def record_heartbeat():
    HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())


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
    price_source:  str,
    hours:         float,
    implied_vol:   float,
    vol_source:    str,
    opportunities: list[dict],
):
    write_header = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "scan_time", "event_ticker", "spx_price", "price_source", "hours_left",
                "implied_vol", "vol_source",
                "threshold", "moneyness_pct", "in_band",
                "bid", "ask", "fair_value", "buy_edge", "sell_edge",
                "vol_24h", "signal",
            ])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for o in opportunities:
            writer.writerow([
                now, event_ticker, f"{spx_price:.2f}", price_source, f"{hours:.1f}",
                f"{implied_vol:.4f}", vol_source,
                o["threshold"], f"{o['moneyness']:.3f}", o["in_band"],
                o["bid"], o["ask"], o["fv"],
                f"{o['buy_edge']:.4f}", f"{o['sell_edge']:.4f}",
                o["vol24"], o["signal"],
            ])


# ── Main ───────────────────────────────────────────────
async def main():
    print(f"S&P 500 Scanner — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    # ── Regime check ───────────────────────────────────
    regime, vix, regime_msg = get_regime()
    print(f"Regime:        {regime_msg}")
    if regime == "HIGH":
        print("  Skipping signals — fat tail risk invalidates normal-vol model")
        send_imessage(
            f"📈 S&P Scanner — {datetime.now(timezone.utc).strftime('%b %d %H:%M UTC')}\n"
            f"  ⛔ {regime_msg}\n"
            f"  No signals generated — resume when VIX < 30"
        )
        return
    print()

    # Check if market is closed
    now = datetime.now(timezone.utc)
    edt_hour = (now.hour - 4) % 24
    is_weekend   = now.weekday() >= 5
    is_afterhours = not (9 <= edt_hour < 16)
    market_open  = not is_weekend and not is_afterhours
    if not market_open:
        print("⚠️  Market is closed right now — using Kalshi-implied price as primary source")

    # Get next event + markets first (needed for implied price)
    event_ticker = await get_next_spx_event()
    if not event_ticker:
        print("No open S&P 500 events found")
        return
    print(f"Event:         {event_ticker}")

    # Fetch live bankroll
    bankroll = await get_balance()
    if bankroll is None:
        bankroll = 50.0
        print(f"Bankroll:      ${bankroll:.2f}  ⚠️  (API failed — using fallback)")
    else:
        print(f"Bankroll:      ${bankroll:.2f}  (live)")

    markets = await get_spx_markets(event_ticker)
    print(f"Markets:       {len(markets)}")

    # Hours to settlement
    hours = hours_to_settlement()
    print(f"Hours left:    {hours:.1f}")

    # Price selection — strategy depends on market status
    # Market OPEN:  Yahoo is live → trust Yahoo, use Kalshi as sanity check
    # Market CLOSED: Yahoo is stale (last close) → trust Kalshi-implied instead
    #
    # Kalshi-implied is only trustworthy if the order book has enough liquidity.
    # We check that at least one contract near ATM has vol24h > 200 before trusting it.
    try:
        yahoo_price = get_spx_price()
    except Exception as e:
        print(f"  Yahoo Finance failed: {e}")
        yahoo_price = None

    kalshi_implied_price = get_implied_spx_price(markets)

    # Assess Kalshi liquidity near ATM — implied price is only reliable with liquid markets
    kalshi_liquid = any(
        float(m.get("volume_24h_fp") or 0) >= 200
        for m in markets
        if m.get("yes_bid_dollars") and m.get("yes_ask_dollars")
    )

    if market_open:
        # Yahoo is live — primary source during market hours
        if yahoo_price and kalshi_implied_price:
            discrepancy = abs(yahoo_price - kalshi_implied_price)
            if discrepancy > 30:
                # Large gap during market hours = one source is badly wrong
                # Trust Kalshi since it's continuously traded
                spx_price    = kalshi_implied_price
                price_source = f"Kalshi-implied (Yahoo={yahoo_price:,.2f} suspect, Δ={discrepancy:.0f})"
                print(f"  ⚠️  Large discrepancy ${discrepancy:.0f} during market hours — trusting Kalshi")
            else:
                spx_price    = yahoo_price
                price_source = f"Yahoo (Kalshi-implied={kalshi_implied_price:,.2f}, Δ={discrepancy:.0f})"
        elif yahoo_price:
            spx_price    = yahoo_price
            price_source = "Yahoo (Kalshi implied unavailable)"
        elif kalshi_implied_price:
            spx_price    = kalshi_implied_price
            price_source = "Kalshi-implied (Yahoo unavailable)"
        else:
            print("Failed to get SPX price from any source")
            return
    else:
        # Market closed — Yahoo is definitionally stale (last close price)
        # Kalshi-implied reflects current overnight sentiment if liquid
        if kalshi_implied_price and kalshi_liquid:
            spx_price    = kalshi_implied_price
            price_source = (
                f"Kalshi-implied (market closed · Yahoo={yahoo_price:,.2f} stale)"
                if yahoo_price else "Kalshi-implied (market closed)"
            )
        elif yahoo_price:
            # Kalshi too thin to trust — use Yahoo but warn loudly
            spx_price    = yahoo_price
            price_source = "Yahoo STALE — market closed and Kalshi illiquid · signals unreliable"
            print(f"  ⚠️  Market closed and Kalshi order book is thin — price is Friday's close")
            print(f"  ⚠️  Signals generated may not reflect current overnight conditions")
        elif kalshi_implied_price:
            spx_price    = kalshi_implied_price
            price_source = "Kalshi-implied (market closed · Yahoo unavailable · low liquidity)"
        else:
            print("Failed to get SPX price from any source")
            return

    print(f"S&P 500 price: {spx_price:,.2f}  [{price_source}]")

    # Too close to settlement — implied vol unreliable, signals meaningless
    if hours < MIN_HOURS:
        print(f"⚠️  {hours:.2f}hrs to settlement — within MIN_HOURS ({MIN_HOURS}), skipping signals")
        send_imessage(f"📈 S&P Scanner — {datetime.now(timezone.utc).strftime('%b %d %H:%M UTC')}\n  {hours:.2f}hrs to settle — too close, no signals")
        return

    # Implied vol from band-edge OTM strikes
    implied_vol, vol_source = get_implied_vol(spx_price, markets, hours)
    if vol_source == "fallback":
        print(f"Implied vol:   {implied_vol:.4f}%/hr  ⚠️  (fallback)")
    else:
        print(f"Implied vol:   {implied_vol:.4f}%/hr  (from {vol_source})")
    print()

    # Calculate edge for each market
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

        fv        = fair_value(spx_price, threshold, hours, implied_vol)
        buy_edge  = fv - ask
        sell_edge = bid - fv

        moneyness = abs(threshold - spx_price) / spx_price
        in_band   = moneyness <= MONEYNESS_BAND

        signal = ""
        if in_band and vol24 >= MIN_VOL_24H:
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
            "moneyness": round(moneyness * 100, 3),
            "in_band":   in_band,
            "vol24":     vol24,
            "signal":    signal,
        })

    opportunities.sort(key=lambda x: max(x["buy_edge"], x["sell_edge"]), reverse=True)

    # Print results
    band_pct = MONEYNESS_BAND * 100
    lo_band  = spx_price * (1 - MONEYNESS_BAND)
    hi_band  = spx_price * (1 + MONEYNESS_BAND)
    print(f"Moneyness band: ±{band_pct:.1f}%  ({lo_band:,.0f} – {hi_band:,.0f})")
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
            f"{o['threshold']:<12,.0f} "
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
    print(f"Strong signals: {len(strong)}  Weak: {len(weak)}  No edge: {len(opportunities) - len(strong) - len(weak)}")

    # ── Console Kelly sizing for strong signals ────────
    for o in strong:
        direction  = "BUY" if o["signal"] == "BUY" else "SELL"
        edge       = o["buy_edge"] if direction == "BUY" else o["sell_edge"]
        exec_price = o["ask"] if direction == "BUY" else 1 - o["bid"]
        sizing     = kelly_size(edge, exec_price, bankroll)
        if sizing["frac_dollar"] > 0:
            print(
                f"  {direction} {o['threshold']:,.0f} → "
                f"💰 Half-Kelly ${sizing['frac_dollar']:.2f}"
                f" ({sizing['contracts']} contracts)"
                f" | Full ${sizing['full_dollar']:.2f}"
            )

    # ── iMessage — signal-only with daily heartbeat ────
    now_str = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")

    if strong:
        lines = [f"📈 S&P Scanner — {now_str}"]
        lines.append(f"  SPX: {spx_price:,.0f}  |  {hours:.1f}hrs  |  vol={implied_vol:.3f}%/hr")
        lines.append(f"  Event: {event_ticker}")
        if not market_open:
            lines.append(f"  ⚠️  Market closed · price is Kalshi-implied · verify before trading")
        if regime == "ELEVATED":
            lines.append(f"  ⚠️  {regime_msg} — reduce size")
        lines.append(f"  Strong signals ({len(strong)}):")
        for o in strong[:3]:
            direction  = "BUY" if o["signal"] == "BUY" else "SELL"
            edge       = o["buy_edge"] if direction == "BUY" else o["sell_edge"]
            exec_price = o["ask"] if direction == "BUY" else 1 - o["bid"]
            sizing     = kelly_size(edge, exec_price, bankroll)
            lines.append(f"    {direction} {o['threshold']:,.0f} edge={edge:.0%} vol={o['vol24']:.0f}")
            if sizing["frac_dollar"] > 0:
                lines.append(
                    f"    💰 Half-Kelly ${sizing['frac_dollar']:.2f}"
                    f" ({sizing['contracts']} contracts) | Full ${sizing['full_dollar']:.2f}"
                )
        lines.append(f"  🔗 https://kalshi.com/markets/{event_ticker}")
        message = "\n".join(lines)
        print()
        print("--- iMessage (signal alert) ---")
        print(message)
        send_imessage(message)
        print("iMessage sent.")
        record_heartbeat()

    elif weak:
        if should_send_heartbeat():
            best = weak[0]
            direction = "BUY" if "BUY" in best["signal"] else "SELL"
            edge = best["buy_edge"] if direction == "BUY" else best["sell_edge"]
            message = (
                f"📈 S&P Scanner — {now_str}\n"
                f"  SPX: {spx_price:,.0f}  |  {hours:.1f}hrs  |  vol={implied_vol:.3f}%/hr\n"
                f"  No strong signals — best: {best['signal']} {best['threshold']:,.0f} "
                f"edge={edge:.0%} vol={best['vol24']:.0f}"
            )
            print()
            print("--- iMessage (daily heartbeat — weak signals) ---")
            print(message)
            send_imessage(message)
            print("iMessage sent.")
            record_heartbeat()
        else:
            print("  (heartbeat suppressed — sent within last 24hrs)")

    else:
        if should_send_heartbeat():
            message = (
                f"📈 S&P Scanner — {now_str}\n"
                f"  SPX: {spx_price:,.0f}  |  {hours:.1f}hrs  |  vol={implied_vol:.3f}%/hr\n"
                f"  No signals"
            )
            print()
            print("--- iMessage (daily heartbeat — no signals) ---")
            print(message)
            send_imessage(message)
            print("iMessage sent.")
            record_heartbeat()
        else:
            print("  (heartbeat suppressed — sent within last 24hrs)")

    # Log
    log_scan(event_ticker, spx_price, price_source, hours, implied_vol, vol_source, opportunities)
    print(f"Logged to {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())