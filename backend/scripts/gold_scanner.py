#!/usr/bin/env python3
"""
gold_scanner.py

Scans Kalshi gold (KXGOLDD) markets for edge opportunities using a
fair value model calibrated to gold's actual hourly volatility (0.341%/hr).

Run daily to build a validation dataset before committing real capital.
Results are logged to gold_scanner_log.csv for post-settlement analysis.

Usage:
    python scripts/gold_scanner.py

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
GOLD_SERIES      = "KXGOLDD"
GOLD_VOL_FLOOR   = 0.90         # minimum vol allowed — backtest showed 0.75 is too low,
                                 # generates fake ITM BUY edges. Set at realized vol lower bound.
GOLD_VOL_FALLBACK = 1.10        # fallback when implied vol unavailable — use recent realized
                                 # (April 2026 realized ~1.09%/hr per scanner output)
MIN_EDGE         = 0.08         # minimum edge to flag as opportunity
MIN_VOL_24H      = 50           # minimum 24h volume — backtest showed vol<50 signals win only 37%
                                 # vs 60% for vol>=50. Zero-vol signals are unfillable noise.
MONEYNESS_BAND   = 0.015        # only score thresholds within ±1.5% of gold price
MIN_HOURS        = 0.5          # don't generate signals within 30 min of settlement
KELLY_FRACTION   = 0.5          # half-Kelly to reduce variance
HEARTBEAT_FILE   = Path(__file__).parent / "gold_heartbeat.txt"
                                 # tracks last heartbeat send — prevents spam on no-signal scans
HEARTBEAT_HOURS  = 24           # send a "no signals" heartbeat at most once per day
IMESSAGE_NUMBER  = "5129928658"
LOG_FILE         = Path(__file__).parent / "gold_scanner_log.csv"
SETTLEMENT_HOUR  = 21           # 5pm EDT = 21:00 UTC


# ── Math ───────────────────────────────────────────────
def normal_cdf(x: float) -> float:
    t    = 1 / (1 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
           + t * (-1.821255978 + t * 1.330274429))))
    p    = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-x * x / 2) * poly
    return p if x >= 0 else 1 - p


def fair_value(price: float, threshold: float, hours: float, vol: float = GOLD_VOL_FALLBACK) -> float:
    if hours <= 0:
        return 1.0 if price >= threshold else 0.0
    dist     = (price - threshold) / threshold * 100
    total_vol = vol * math.sqrt(hours)
    z        = dist / total_vol
    return round(normal_cdf(z), 4)


def get_implied_vol(gold_price: float, markets: list[dict], hours: float) -> tuple[float, str, bool]:
    """
    Back out implied vol from liquid near-ATM strikes.

    Backtest finding: using only above-price strikes caused fake ITM BUY signals
    when those strikes were unavailable and the model fell back to 0.75%/hr.
    Fix: look at BOTH above and below-price strikes, pick best candidate from either,
    enforce a vol floor, and return is_fallback flag for downstream filtering.

    Strategy:
      - Above-price (OTM calls): moneyness 0.5%–1.5%, mid 0.10–0.60
      - Below-price (OTM puts):  moneyness −1.5%–−0.5%, mid 0.40–0.90
        (put mid is close to 1 when ITM, so we look at the complement)
      - Pick candidate closest to mid=0.30 for calls, mid=0.70 for puts
      - Run bisection to back out implied vol
      - Enforce GOLD_VOL_FLOOR — never let vol drop below proven lower bound

    Returns (implied_vol, source_ticker, is_fallback)
    """
    if hours <= 0:
        return GOLD_VOL_FALLBACK, "fallback", True

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

        moneyness = (threshold - gold_price) / gold_price  # + = above price
        spread    = ask - bid

        # Above-price OTM calls: 0.5%–1.5% OTM, mid 0.10–0.60
        if 0.005 <= moneyness <= 0.015 and 0.10 <= mid <= 0.60:
            candidates.append((abs(mid - 0.30), spread, mid, threshold, m["ticker"], "call"))

        # Below-price OTM puts: 0.5%–1.5% below spot, mid 0.40–0.90
        # (threshold below spot → high yes_bid since it's ITM)
        # We want the put-equivalent: a below-price strike where mid is ~0.70
        elif -0.015 <= moneyness <= -0.005 and 0.40 <= mid <= 0.90:
            candidates.append((abs(mid - 0.70), spread, mid, threshold, m["ticker"], "put"))

    if not candidates:
        return GOLD_VOL_FALLBACK, "fallback", True

    candidates.sort(key=lambda x: (x[0], x[1]))
    _, _, market_mid, threshold, ticker, strike_type = candidates[0]

    # Bisection to back out implied vol
    # For above-price: higher vol → FV increases toward 0.5
    # For below-price: higher vol → FV decreases toward 0.5
    lo, hi = 0.01, 5.0
    for _ in range(60):
        mid_vol = (lo + hi) / 2
        fv      = fair_value(gold_price, threshold, hours, mid_vol)
        if strike_type == "call":
            if fv < market_mid:
                lo = mid_vol
            else:
                hi = mid_vol
        else:  # put
            if fv > market_mid:
                lo = mid_vol
            else:
                hi = mid_vol
        if hi - lo < 1e-6:
            break

    implied = round((lo + hi) / 2, 4)

    # Sanity check — reject extreme values
    if implied < 0.02 or implied > 4.9:
        return GOLD_VOL_FALLBACK, "fallback", True

    # Enforce vol floor — backtest showed 0.75 generates fake edges
    if implied < GOLD_VOL_FLOOR:
        print(f"  ⚠️  Implied vol {implied:.4f} below floor {GOLD_VOL_FLOOR} — clamping")
        implied = GOLD_VOL_FLOOR

    return implied, f"{ticker}({strike_type})", False


# ── Price feed ─────────────────────────────────────────
def get_gold_price() -> float:
    """Fetch current gold futures price from Yahoo Finance (GC=F)."""
    data   = yf.download("GC=F", period="1d", interval="1m", progress=False)
    closes = data["Close"].squeeze().dropna()
    if closes.empty:
        raise ValueError("No gold price data available")
    return float(closes.iloc[-1])


def get_implied_gold_price(markets: list[dict]) -> float | None:
    """
    Back out the implied gold spot price from Kalshi market prices.

    Finds the threshold where the yes_bid crosses below 50¢ — that's
    the market's best estimate of where gold will settle. Interpolates
    between the two bracketing strikes for precision.

    Returns None if markets are too illiquid to give a reliable read.
    """
    # Build list of (threshold, mid) sorted ascending by threshold
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
        points.append((threshold, mid, bid, ask))

    points.sort(key=lambda x: x[0])

    if len(points) < 2:
        return None

    # Find where mid crosses 0.50 from above (higher threshold = lower probability)
    for i in range(len(points) - 1):
        t_lo, mid_lo = points[i][0],   points[i][1]
        t_hi, mid_hi = points[i+1][0], points[i+1][1]

        # mid decreases as threshold increases — look for crossover at 0.50
        if mid_lo >= 0.50 >= mid_hi:
            # Linear interpolation
            if mid_lo == mid_hi:
                return (t_lo + t_hi) / 2
            frac    = (mid_lo - 0.50) / (mid_lo - mid_hi)
            implied = t_lo + frac * (t_hi - t_lo)
            return round(implied, 2)

    return None


# ── Kalshi API ─────────────────────────────────────────
async def get_next_gold_event() -> str | None:
    """Returns the ticker for the nearest open gold event."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        r      = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/events",
            params={"limit": 5, "status": "open", "series_ticker": GOLD_SERIES},
        )
        events = r.json().get("events", [])
        if not events:
            return None
        # Sort by strike_date ascending, return nearest
        events.sort(key=lambda e: e.get("strike_date", ""))
        return events[0]["event_ticker"]


async def get_gold_markets(event_ticker: str) -> list[dict]:
    """Fetch all open markets for a given gold event."""
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
    now        = datetime.now(timezone.utc)
    settlement = now.replace(hour=SETTLEMENT_HOUR, minute=0, second=0, microsecond=0)
    if now >= settlement:
        settlement += timedelta(days=1)
    return (settlement - now).total_seconds() / 3600


# ── Kelly position sizing ──────────────────────────────
def kelly_size(
    edge:      float,   # executable edge (buy_edge for BUY, sell_edge for SELL)
    price:     float,   # execution price (ask for BUY, 1-bid for SELL)
    bankroll:  float,
    fraction:  float = KELLY_FRACTION,
) -> dict:
    """
    Compute Kelly-optimal position size for a Kalshi binary contract.

    For a binary paying $1 on win:
      Kelly % = edge / (1 - price)

    BUY:  price = ask  (you pay ask, win $1 if gold closes above threshold)
    SELL: price = 1 - bid  (you sell Yes at bid, effectively paying 1-bid for the No)

    Returns dict with full_kelly, fractional, and recommended contract count.
    """
    if price <= 0 or price >= 1 or edge <= 0:
        return {"pct": 0.0, "full_dollar": 0.0, "frac_dollar": 0.0, "contracts": 0}

    kelly_pct   = edge / (1 - price)
    kelly_pct   = max(0.0, min(kelly_pct, 0.25))   # cap at 25% of bankroll
    full_dollar = round(bankroll * kelly_pct, 2)
    frac_dollar = round(bankroll * kelly_pct * fraction, 2)
    contracts   = max(1, round(frac_dollar / price))

    return {
        "pct":         round(kelly_pct * 100, 1),
        "full_dollar": full_dollar,
        "frac_dollar": frac_dollar,
        "contracts":   contracts,
    }


# ── Heartbeat helper ───────────────────────────────────
def should_send_heartbeat() -> bool:
    """
    Returns True if enough time has passed since the last heartbeat send.
    Prevents "no signals" spam on every scan cycle.
    """
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
    event_ticker: str,
    gold_price:   float,
    price_source: str,
    hours:        float,
    implied_vol:  float,
    vol_source:   str,
    opportunities: list[dict],
):
    """Append scan results to CSV for post-settlement validation."""
    write_header = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "scan_time", "event_ticker", "gold_price", "price_source", "hours_left",
                "implied_vol", "vol_source", "is_fallback",
                "threshold", "moneyness_pct", "in_band",
                "bid", "ask", "fair_value", "buy_edge", "sell_edge",
                "vol_24h", "signal",
            ])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for o in opportunities:
            writer.writerow([
                now, event_ticker, f"{gold_price:.2f}", price_source, f"{hours:.1f}",
                f"{implied_vol:.4f}", vol_source, o.get("is_fallback", True),
                o["threshold"], f"{o['moneyness']:.3f}", o["in_band"],
                o["bid"], o["ask"], o["fv"],
                f"{o['buy_edge']:.4f}", f"{o['sell_edge']:.4f}",
                o["vol24"], o["signal"],
            ])


# ── Main ───────────────────────────────────────────────
async def main():
    print(f"Gold Scanner — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    # ── Regime check ───────────────────────────────────
    regime, vix, regime_msg = get_regime()
    print(f"Regime:      {regime_msg}")
    if regime == "HIGH":
        print("  Skipping signals — fat tail risk invalidates normal-vol model")
        send_imessage(
            f"🥇 Gold Scanner — {datetime.now(timezone.utc).strftime('%b %d %H:%M UTC')}\n"
            f"  ⛔ {regime_msg}\n"
            f"  No signals generated — resume when VIX < 30"
        )
        return
    print()

    # Gold futures trade nearly 24/7 on CME Globex:
    #   Open: Sunday 6pm ET → Friday 5pm ET
    #   Closed: Friday 5pm ET → Sunday 6pm ET
    # During the closed window Yahoo Finance returns Friday's close — stale.
    now = datetime.now(timezone.utc)
    et_hour    = (now.hour - 4) % 24
    et_weekday = (now.weekday() + (1 if et_hour >= 20 else 0)) % 7  # approximate
    friday_close  = now.weekday() == 4 and et_hour >= 17   # Fri after 5pm ET
    saturday      = now.weekday() == 5
    sunday_before = now.weekday() == 6 and et_hour < 18    # Sun before 6pm ET
    gold_market_closed = friday_close or saturday or sunday_before
    if gold_market_closed:
        print("⚠️  Gold futures closed (Fri 5pm–Sun 6pm ET) — using Kalshi-implied as primary source")

    # Get next event
    event_ticker = await get_next_gold_event()
    if not event_ticker:
        print("No open gold events found")
        return
    print(f"Event:       {event_ticker}")

    # Fetch live bankroll
    bankroll = await get_balance()
    if bankroll is None:
        bankroll = 50.0  # conservative fallback
        print(f"Bankroll:    ${bankroll:.2f}  ⚠️  (API failed — using fallback)")
    else:
        print(f"Bankroll:    ${bankroll:.2f}  (live)")

    # Get markets first — needed for implied price
    markets = await get_gold_markets(event_ticker)
    print(f"Markets:     {len(markets)}")

    # Get gold price — strategy depends on futures market status
    try:
        yahoo_price = get_gold_price()
    except Exception as e:
        print(f"  Yahoo Finance failed: {e}")
        yahoo_price = None

    kalshi_implied = get_implied_gold_price(markets)

    # Assess Kalshi liquidity — implied price only reliable with active order book
    kalshi_liquid = any(
        float(m.get("volume_24h_fp") or 0) >= 100
        for m in markets
        if m.get("yes_bid_dollars") and m.get("yes_ask_dollars")
    )

    if gold_market_closed:
        # Yahoo is stale — trust Kalshi-implied if liquid
        if kalshi_implied and kalshi_liquid:
            gold_price   = kalshi_implied
            price_source = (
                f"Kalshi-implied (gold futures closed · Yahoo=${yahoo_price:,.2f} stale)"
                if yahoo_price else "Kalshi-implied (gold futures closed)"
            )
        elif yahoo_price:
            gold_price   = yahoo_price
            price_source = "Yahoo STALE — gold futures closed and Kalshi illiquid · signals unreliable"
            print(f"  ⚠️  Gold futures closed and Kalshi is thin — price is Friday's close")
            print(f"  ⚠️  Any signals generated may not reflect actual gold price")
        elif kalshi_implied:
            gold_price   = kalshi_implied
            price_source = "Kalshi-implied (gold futures closed · Yahoo unavailable · low liquidity)"
        else:
            print("Failed to get gold price from any source")
            return
    else:
        # Gold futures are live — Yahoo should be current
        if yahoo_price and kalshi_implied:
            discrepancy = abs(yahoo_price - kalshi_implied)
            if discrepancy > 10:
                # Large gap during open hours — trust Kalshi (continuously traded)
                gold_price   = kalshi_implied
                price_source = f"Kalshi-implied (Yahoo=${yahoo_price:,.2f} suspect, Δ=${discrepancy:.0f})"
                print(f"  ⚠️  Large discrepancy ${discrepancy:.0f} during market hours — trusting Kalshi")
            else:
                gold_price   = yahoo_price
                price_source = f"Yahoo (Kalshi-implied=${kalshi_implied:,.2f}, Δ=${discrepancy:.0f})"
        elif kalshi_implied:
            gold_price   = kalshi_implied
            price_source = "Kalshi-implied (Yahoo unavailable)"
        elif yahoo_price:
            gold_price   = yahoo_price
            price_source = "Yahoo (Kalshi implied unavailable)"
        else:
            print("Failed to get gold price from any source")
            return

    print(f"Gold price:  ${gold_price:,.2f}  [{price_source}]")

    # Get hours to settlement
    hours = hours_to_settlement()
    print(f"Hours left:  {hours:.1f}")

    # Too close to settlement — implied vol unreliable, signals meaningless
    if hours < MIN_HOURS:
        print(f"⚠️  {hours:.2f}hrs to settlement — within MIN_HOURS ({MIN_HOURS}), skipping signals")
        send_imessage(f"🥇 Gold Scanner — {datetime.now(timezone.utc).strftime('%b %d %H:%M UTC')}\n  {hours:.2f}hrs to settle — too close, no signals")
        return
    print()

    # Back out implied vol from ATM market price
    implied_vol, vol_source, is_fallback = get_implied_vol(gold_price, markets, hours)
    if is_fallback:
        print(f"Implied vol: {implied_vol:.4f}%/hr  ⚠️  (fallback — no suitable calibration strike found)")
        print(f"  Signals logged with is_fallback=True — exclude from backtest validation")
    else:
        print(f"Implied vol: {implied_vol:.4f}%/hr  (from {vol_source})")
    print()

    # Calculate edge for each market
    opportunities = []
    for m in markets:
        bid  = float(m.get("yes_bid_dollars") or 0)
        ask  = float(m.get("yes_ask_dollars") or 0)
        if not bid and not ask:
            continue
        mid  = (bid + ask) / 2
        if mid < 0.03 or mid > 0.97:
            continue
        vol24 = float(m.get("volume_24h_fp") or 0)

        try:
            threshold = float(m["ticker"].split("-T")[-1])
        except Exception:
            continue

        fv       = fair_value(gold_price, threshold, hours, implied_vol)
        buy_edge = fv - ask
        sell_edge = bid - fv

        # Moneyness filter — skip thresholds outside ±MONEYNESS_BAND of spot
        moneyness = abs(threshold - gold_price) / gold_price
        in_band   = moneyness <= MONEYNESS_BAND

        signal = ""
        if in_band:
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
            "implied_vol": implied_vol,
            "is_fallback": is_fallback,
        })

    # Sort by best edge
    opportunities.sort(
        key=lambda x: max(x["buy_edge"], x["sell_edge"]),
        reverse=True,
    )

    # Print results
    band_pct = MONEYNESS_BAND * 100
    lo_band  = gold_price * (1 - MONEYNESS_BAND)
    hi_band  = gold_price * (1 + MONEYNESS_BAND)
    print(f"Moneyness band: ±{band_pct:.1f}%  (${lo_band:,.0f} – ${hi_band:,.0f})")
    print()
    print(f"{'Threshold':<12} {'Bid':<6} {'Ask':<6} {'FV':<8} {'BuyEdge':<10} {'SellEdge':<10} {'Vol24h':<8} {'Mness%':<8} Signal")
    print("-" * 85)

    strong = [o for o in opportunities if o["signal"] in ("BUY", "SELL")]
    weak   = [o for o in opportunities if o["signal"] in ("WEAK_BUY", "WEAK_SELL")]
    none_  = [o for o in opportunities if not o["signal"]]

    for o in opportunities[:20]:
        icon     = "✅" if o["signal"] == "BUY" else "🔴" if o["signal"] == "SELL" else "⚠️" if o["signal"].startswith("WEAK") else ""
        vol_flag = " ⚠️ low vol" if o["vol24"] < 50 else ""
        oob_flag = "" if o["in_band"] else " [OOB]"
        print(
            f"${o['threshold']:<11,.0f} "
            f"{o['bid']:<6.3f} "
            f"{o['ask']:<6.3f} "
            f"{o['fv']:<8.3f} "
            f"{o['buy_edge']:+.3f}     "
            f"{o['sell_edge']:+.3f}     "
            f"{o['vol24']:<8.0f} "
            f"{o['moneyness']:<8.2f} "
            f"{icon} {o['signal']}{oob_flag}{vol_flag}"
        )
        # Kelly sizing for actionable signals
        if o["signal"] == "BUY":
            sizing = kelly_size(o["buy_edge"], o["ask"], bankroll)
        elif o["signal"] == "SELL":
            sizing = kelly_size(o["sell_edge"], 1 - o["bid"], bankroll)
        else:
            sizing = None
        if sizing and sizing["frac_dollar"] > 0:
            print(
                f"  {'':12} 💰 Half-Kelly ${sizing['frac_dollar']:.2f}"
                f" ({sizing['contracts']} contracts)"
                f" | Full-Kelly ${sizing['full_dollar']:.2f}"
                f" | {sizing['pct']:.1f}% of bankroll"
            )

    print()
    print(f"Strong signals: {len(strong)}  Weak: {len(weak)}  No edge: {len(none_)}")

    # ── iMessage — only alert on actionable signals ────
    # Suppress "no signal" messages except for once-daily heartbeat
    now_str = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")

    if strong:
        # Always alert on strong signals — this is what we're here for
        lines = [f"🥇 Gold Scanner — {now_str}"]
        lines.append(f"  Price: ${gold_price:,.2f}  |  {hours:.1f}hrs to settle  |  vol={implied_vol:.3f}%/hr")
        lines.append(f"  Event: {event_ticker}")
        if is_fallback:
            lines.append(f"  ⚠️  Vol is fallback estimate — treat signals with caution")
        if gold_market_closed:
            lines.append(f"  ⚠️  Gold futures closed — price from Kalshi order book · verify before trading")
        if regime == "ELEVATED":
            lines.append(f"  ⚠️  {regime_msg} — reduce size")
        lines.append(f"  Strong signals ({len(strong)}):")
        for o in strong[:3]:
            direction = "BUY" if o["signal"] == "BUY" else "SELL"
            edge      = o["buy_edge"] if direction == "BUY" else o["sell_edge"]
            exec_price = o["ask"] if direction == "BUY" else 1 - o["bid"]
            sizing    = kelly_size(edge, exec_price, bankroll)
            lines.append(
                f"    {direction} ${o['threshold']:,.0f} "
                f"edge={edge:.0%} vol={o['vol24']:.0f}"
            )
            if sizing["frac_dollar"] > 0:
                lines.append(
                    f"    💰 Half-Kelly ${sizing['frac_dollar']:.2f}"
                    f" ({sizing['contracts']} contracts)"
                    f" | Full ${sizing['full_dollar']:.2f}"
                )
        message = "\n".join(lines)
        print()
        print("--- iMessage (signal alert) ---")
        print(message)
        send_imessage(message)
        print("iMessage sent.")
        record_heartbeat()

    elif weak:
        # Weak signals — alert only if daily heartbeat is due
        if should_send_heartbeat():
            best = weak[0]
            direction = "BUY" if "BUY" in best["signal"] else "SELL"
            edge = best["buy_edge"] if direction == "BUY" else best["sell_edge"]
            message = (
                f"🥇 Gold Scanner — {now_str}\n"
                f"  Price: ${gold_price:,.2f}  |  {hours:.1f}hrs  |  vol={implied_vol:.3f}%/hr\n"
                f"  No strong signals — best: {best['signal']} ${best['threshold']:,.0f} "
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
        # No signals at all — heartbeat only
        if should_send_heartbeat():
            message = (
                f"🥇 Gold Scanner — {now_str}\n"
                f"  Price: ${gold_price:,.2f}  |  {hours:.1f}hrs  |  vol={implied_vol:.3f}%/hr\n"
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

    # Log to CSV
    log_scan(event_ticker, gold_price, price_source, hours, implied_vol, vol_source, opportunities)
    print(f"Logged to {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())