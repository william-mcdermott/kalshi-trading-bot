#!/usr/bin/env python3
"""
mlb_scanner.py

Compares Kalshi MLB game market prices to Vegas moneylines from The Odds API.
Flags games where Kalshi diverges from Vegas implied probability by >5¢.

The edge: if Kalshi is slow to reprice relative to Vegas live lines,
there's systematic arbitrage opportunity.

Usage:
    python scripts/mlb_scanner.py

Results logged to mlb_scanner_log.csv for validation.
"""

import asyncio
import csv
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────
ODDS_API_KEY    = os.getenv("ODDS_API_KEY")
IMESSAGE_NUMBER = "5129928658"
LOG_FILE        = Path(__file__).parent / "mlb_scanner_log.csv"
MIN_EDGE        = 0.05   # 5¢ minimum divergence to flag
MIN_VOL_24H     = 10_000 # only scan liquid markets

# ── Team name mapping — Odds API → Kalshi ─────────────
# Kalshi uses short names, Odds API uses full names
TEAM_MAP = {
    "Arizona Diamondbacks":      "Arizona",
    "Atlanta Braves":            "Atlanta",
    "Baltimore Orioles":         "Baltimore",
    "Boston Red Sox":            "Boston",
    "Chicago Cubs":              "Chicago C",
    "Chicago White Sox":         "Chicago WS",
    "Cincinnati Reds":           "Cincinnati",
    "Cleveland Guardians":       "Cleveland",
    "Colorado Rockies":          "Colorado",
    "Detroit Tigers":            "Detroit",
    "Houston Astros":            "Houston",
    "Kansas City Royals":        "Kansas City",
    "Los Angeles Angels":        "Los Angeles A",
    "Los Angeles Dodgers":       "Los Angeles D",
    "Miami Marlins":             "Miami",
    "Milwaukee Brewers":         "Milwaukee",
    "Minnesota Twins":           "Minnesota",
    "New York Mets":             "New York M",
    "New York Yankees":          "New York Y",
    "Oakland Athletics":         "A's",
    "Athletics":                 "A's",
    "Philadelphia Phillies":     "Philadelphia",
    "Pittsburgh Pirates":        "Pittsburgh",
    "San Diego Padres":          "San Diego",
    "San Francisco Giants":      "San Francisco",
    "Seattle Mariners":          "Seattle",
    "St. Louis Cardinals":       "St. Louis",
    "Tampa Bay Rays":            "Tampa Bay",
    "Texas Rangers":             "Texas",
    "Toronto Blue Jays":         "Toronto",
    "Washington Nationals":      "Washington",
}


# ── Math ───────────────────────────────────────────────
def american_to_prob(american: int) -> float:
    """Convert American moneyline odds to implied probability (no vig removal)."""
    if american > 0:
        return 100 / (american + 100)
    else:
        return abs(american) / (abs(american) + 100)


def remove_vig(prob_home: float, prob_away: float) -> tuple[float, float]:
    """Remove bookmaker vig to get true implied probabilities."""
    total = prob_home + prob_away
    return prob_home / total, prob_away / total


# ── Vegas odds ─────────────────────────────────────────
def get_vegas_odds() -> dict:
    """
    Fetch MLB moneylines from The Odds API.
    Returns dict keyed by (away_short, home_short) → (away_prob, home_prob)
    """
    r = httpx.get(
        "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
        params={
            "apiKey":     ODDS_API_KEY,
            "regions":    "us",
            "markets":    "h2h",
            "oddsFormat": "american",
        },
        timeout=10.0,
    )
    r.raise_for_status()
    games  = r.json()
    result = {}

    for g in games:
        home_full = g["home_team"]
        away_full = g["away_team"]
        home_short = TEAM_MAP.get(home_full, home_full)
        away_short = TEAM_MAP.get(away_full, away_full)

        if not g.get("bookmakers"):
            continue

        # Use first available bookmaker
        market = g["bookmakers"][0]["markets"][0]
        odds   = {o["name"]: o["price"] for o in market["outcomes"]}

        home_ml = odds.get(home_full)
        away_ml = odds.get(away_full)
        if not home_ml or not away_ml:
            continue

        home_raw = american_to_prob(home_ml)
        away_raw = american_to_prob(away_ml)
        home_prob, away_prob = remove_vig(home_raw, away_raw)

        result[(away_short, home_short)] = {
            "away_prob":    round(away_prob, 4),
            "home_prob":    round(home_prob, 4),
            "away_ml":      away_ml,
            "home_ml":      home_ml,
            "commence":     g["commence_time"],
        }

    return result


# ── Kalshi MLB markets ─────────────────────────────────
async def get_kalshi_mlb_games() -> list[dict]:
    """Fetch all open MLB game markets from Kalshi."""
    all_events = []
    cursor     = None
    async with httpx.AsyncClient(timeout=10.0) as http:
        while True:
            params = {"limit": 100, "status": "open", "series_ticker": "KXMLBGAME"}
            if cursor:
                params["cursor"] = cursor
            r      = await http.get(
                "https://api.elections.kalshi.com/trade-api/v2/events",
                params=params,
            )
            data   = r.json()
            events = data.get("events", [])
            all_events.extend(events)
            cursor = data.get("cursor", "")
            if not cursor or not events:
                break

    # Get markets for each event
    games = []
    async with httpx.AsyncClient(timeout=10.0) as http:
        for event in all_events:
            r = await http.get(
                "https://api.elections.kalshi.com/trade-api/v2/markets",
                params={"limit": 5, "status": "open", "event_ticker": event["event_ticker"]},
            )
            markets = r.json().get("markets", [])
            if not markets:
                continue

            game = {"event_ticker": event["event_ticker"], "teams": {}}
            for m in markets:
                bid   = float(m.get("yes_bid_dollars") or 0)
                ask   = float(m.get("yes_ask_dollars") or 0)
                vol24 = float(m.get("volume_24h_fp") or 0)
                team  = m.get("yes_sub_title", "")
                if bid > 0 or ask > 0:
                    game["teams"][team] = {
                        "bid":   bid,
                        "ask":   ask,
                        "mid":   round((bid + ask) / 2, 3),
                        "vol24": vol24,
                    }
            if game["teams"]:
                games.append(game)

    return games


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
def log_results(results: list[dict]):
    write_header = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "scan_time", "event_ticker", "team",
                "kalshi_mid", "vegas_prob", "edge",
                "vol24", "signal",
            ])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for r in results:
            writer.writerow([
                now, r["event_ticker"], r["team"],
                r["kalshi_mid"], r["vegas_prob"], r["edge"],
                r["vol24"], r["signal"],
            ])


# ── Main ───────────────────────────────────────────────
async def main():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"MLB Scanner — {now_str}")
    print()

    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY not set in .env")
        return

    # Get Vegas odds
    try:
        vegas = get_vegas_odds()
        print(f"Vegas games loaded: {len(vegas)}")
    except Exception as e:
        print(f"Failed to get Vegas odds: {e}")
        return

    # Get Kalshi markets
    try:
        kalshi_games = await get_kalshi_mlb_games()
        print(f"Kalshi games loaded: {len(kalshi_games)}")
    except Exception as e:
        print(f"Failed to get Kalshi markets: {e}")
        return

    print()

    # Match and compare
    results      = []
    matched      = 0
    skipped_live = 0
    unmatched    = []

    for game in kalshi_games:
        teams = list(game["teams"].keys())
        if len(teams) < 2:
            continue

        # Try to find matching Vegas game
        matched_vegas = None
        matched_key   = None
        for key in vegas:
            away_short, home_short = key
            if away_short in teams and home_short in teams:
                matched_vegas = vegas[key]
                matched_key   = key
                break
            # Also try reversed
            if home_short in teams and away_short in teams:
                matched_vegas = vegas[key]
                matched_key   = (home_short, away_short)
                break

        if not matched_vegas:
            unmatched.append(game["event_ticker"])
            continue

        # ── Skip in-progress games ───────────────────────
        now = datetime.now(timezone.utc)
        game_start = datetime.fromisoformat(
            matched_vegas["commence"].replace("Z", "+00:00")
        )
        if game_start < now:
            skipped_live += 1
            continue  # Kalshi reflects live score, Vegas may lag — skip
        # ─────────────────────────────────────────────────

        matched += 1
        away_short, home_short = matched_key

        for team_short, team_data in game["teams"].items():
            if team_data["vol24"] < MIN_VOL_24H:
                continue

            # Get Vegas probability for this team
            if team_short == away_short:
                vegas_prob = matched_vegas["away_prob"]
            elif team_short == home_short:
                vegas_prob = matched_vegas["home_prob"]
            else:
                continue

            kalshi_mid = team_data["mid"]
            edge       = vegas_prob - kalshi_mid  # positive = Kalshi underpricing
            signal     = ""

            if edge > MIN_EDGE:
                signal = "BUY"        # Kalshi underpriced vs Vegas
            elif edge < -MIN_EDGE:
                signal = "SELL"       # Kalshi overpriced vs Vegas
            elif abs(edge) > 0.02:
                signal = "WATCH"

            results.append({
                "event_ticker": game["event_ticker"],
                "team":         team_short,
                "kalshi_mid":   kalshi_mid,
                "kalshi_bid":   team_data["bid"],
                "kalshi_ask":   team_data["ask"],
                "vegas_prob":   vegas_prob,
                "edge":         round(edge, 4),
                "vol24":        team_data["vol24"],
                "signal":       signal,
            })

    # Sort by abs edge
    results.sort(key=lambda x: abs(x["edge"]), reverse=True)

    # Print
    print(f"Matched: {matched}/{len(kalshi_games)}  Skipped live: {skipped_live}  Unmatched: {len(unmatched)}")
    print()
    print(f"{'Team':<18} {'Kalshi':<8} {'Vegas':<8} {'Edge':<8} {'Vol24h':<10} Signal")
    print("-" * 65)

    strong = [r for r in results if r["signal"] in ("BUY", "SELL")]
    watch  = [r for r in results if r["signal"] == "WATCH"]

    for r in results[:15]:
        icon = "✅" if r["signal"] == "BUY" else "🔴" if r["signal"] == "SELL" else "👀" if r["signal"] == "WATCH" else ""
        print(
            f"{r['team']:<18} "
            f"{r['kalshi_mid']:<8.3f} "
            f"{r['vegas_prob']:<8.3f} "
            f"{r['edge']:+.3f}    "
            f"{r['vol24']:<10,.0f} "
            f"{icon} {r['signal']}"
        )

    print()
    print(f"Strong: {len(strong)}  Watch: {len(watch)}")

    if unmatched:
        print(f"\nUnmatched events: {unmatched[:5]}")

    # iMessage
    lines = [f"⚾ MLB Scanner — {now_str}"]
    lines.append(f"  {matched} games matched, {len(strong)} strong signals")
    if strong:
        for r in strong[:4]:
            lines.append(f"  {r['signal']} {r['team']} kalshi={r['kalshi_mid']:.2f} vegas={r['vegas_prob']:.2f} edge={r['edge']:+.2f}")
    else:
        lines.append("  No strong signals — markets aligned")

    message = "\n".join(lines)
    print()
    print("--- iMessage ---")
    print(message)
    send_imessage(message)
    print("iMessage sent.")

    # Log
    log_results(results)
    print(f"Logged to {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())