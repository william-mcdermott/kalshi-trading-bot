#!/usr/bin/env python3
"""
mlb_live_scanner.py

Scans IN-PROGRESS MLB games for edge between Kalshi prices and a
win probability model based on score differential and innings remaining.

Data sources:
  - MLB Stats API (free) — live score, inning, outs
  - Kalshi API — current market prices for KXMLBGAME markets

Win probability model:
  Uses a logistic regression approximation of the well-known
  run expectancy / win probability tables published by Tom Tango.
  P(home wins) = logistic(a * run_diff + b * innings_remaining + c * home_advantage)

Calibrated constants from historical MLB data:
  - Each run ~ 15% win probability swing late in game
  - Home field advantage ~ 4%
  - Each inning remaining reduces certainty

Usage:
    python scripts/mlb_live_scanner.py

Run every 2 minutes during game hours for best results.
"""

import asyncio
import csv
import math
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────
IMESSAGE_NUMBER = "5129928658"
LOG_FILE        = Path(__file__).parent / "mlb_live_scanner_log.csv"
MIN_EDGE        = 0.05   # 5¢ minimum to flag
MIN_VOL_24H     = 1_000  # lower threshold — late games have less volume

# ── Team name mapping — MLB Stats API → Kalshi ─────────
TEAM_MAP = {
    "Arizona Diamondbacks":  "Arizona",
    "Atlanta Braves":        "Atlanta",
    "Baltimore Orioles":     "Baltimore",
    "Boston Red Sox":        "Boston",
    "Chicago Cubs":          "Chicago C",
    "Chicago White Sox":     "Chicago WS",
    "Cincinnati Reds":       "Cincinnati",
    "Cleveland Guardians":   "Cleveland",
    "Colorado Rockies":      "Colorado",
    "Detroit Tigers":        "Detroit",
    "Houston Astros":        "Houston",
    "Kansas City Royals":    "Kansas City",
    "Los Angeles Angels":    "Los Angeles A",
    "Los Angeles Dodgers":   "Los Angeles D",
    "Miami Marlins":         "Miami",
    "Milwaukee Brewers":     "Milwaukee",
    "Minnesota Twins":       "Minnesota",
    "New York Mets":         "New York M",
    "New York Yankees":      "New York Y",
    "Oakland Athletics":     "A's",
    "Athletics":             "A's",
    "Philadelphia Phillies": "Philadelphia",
    "Pittsburgh Pirates":    "Pittsburgh",
    "San Diego Padres":      "San Diego",
    "San Francisco Giants":  "San Francisco",
    "Seattle Mariners":      "Seattle",
    "St. Louis Cardinals":   "St. Louis",
    "Tampa Bay Rays":        "Tampa Bay",
    "Texas Rangers":         "Texas",
    "Toronto Blue Jays":     "Toronto",
    "Washington Nationals":  "Washington",
}


# ── Win probability model ───────────────────────────────
def logistic(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def win_probability(
    run_diff: int,        # positive = home team leading
    inning: int,          # current inning (1-9+)
    inning_half: str,     # "Top" or "Bottom"
    outs: int,            # 0, 1, or 2
    is_home: bool,        # are we calculating for home team?
) -> float:
    """
    Estimate win probability based on score differential and game state.

    Uses a logistic model calibrated from Tom Tango's win probability tables.
    Key parameters:
      - run_diff: each run worth ~0.45 log-odds
      - innings_remaining: more innings = more uncertainty
      - home advantage: ~0.12 log-odds baseline
    """
    # Calculate innings remaining (including partial)
    if inning_half == "Top":
        # Away team batting — home has more at-bats if tied
        half_innings_done = (inning - 1) * 2
    else:
        # Home team batting
        half_innings_done = (inning - 1) * 2 + 1

    # Adjust for outs within the half-inning
    out_fraction    = outs / 3.0
    effective_done  = half_innings_done + out_fraction
    total_half_inn  = 18  # 9 innings * 2 halves
    half_inn_left   = max(0, total_half_inn - effective_done)
    innings_left    = half_inn_left / 2

    # Home field advantage (home wins ~54% of MLB games)
    home_advantage = 0.16

    # Uncertainty factor — more innings = flatter probability curve
    # Each run worth less earlier in game
    if innings_left > 0:
        run_weight = 0.45 / math.sqrt(innings_left + 0.5)
    else:
        # Game essentially over
        if run_diff > 0:
            return 1.0 if is_home else 0.0
        elif run_diff < 0:
            return 0.0 if is_home else 1.0
        else:
            return 0.5

    # From home team perspective
    log_odds = run_diff * run_weight + home_advantage

    home_prob = logistic(log_odds)

    return round(home_prob if is_home else 1 - home_prob, 4)


# ── Date helper ────────────────────────────────────────
def get_today_date() -> str:
    """
    MLB games are scheduled in local US time.
    Use ET date — if UTC is past midnight but before 6am,
    use yesterday's date since late games are still going.
    """
    now_utc = datetime.now(timezone.utc)
    # ET is UTC-4 (EDT)
    now_et  = now_utc - timedelta(hours=4)
    return now_et.strftime("%m/%d/%Y")


# ── MLB Stats API ───────────────────────────────────────
def get_live_games() -> list[dict]:
    """Fetch all in-progress MLB games with linescore from MLB Stats API."""
    today = get_today_date()
    r     = httpx.get(
        "https://statsapi.mlb.com/api/v1/schedule",
        params={"sportId": 1, "date": today, "hydrate": "linescore"},
        timeout=10.0,
    )
    r.raise_for_status()
    data  = r.json()
    games = []

    for date in data.get("dates", []):
        for g in date.get("games", []):
            if g["status"]["abstractGameState"] != "Live":
                continue

            ls         = g.get("linescore", {})
            away_full  = g["teams"]["away"]["team"]["name"]
            home_full  = g["teams"]["home"]["team"]["name"]
            away_short = TEAM_MAP.get(away_full, away_full)
            home_short = TEAM_MAP.get(home_full, home_full)

            games.append({
                "game_pk":    g["gamePk"],
                "away_full":  away_full,
                "home_full":  home_full,
                "away_short": away_short,
                "home_short": home_short,
                "inning":     ls.get("currentInning", 1),
                "half":       ls.get("inningHalf", "Top"),
                "outs":       ls.get("outs", 0),
                "away_runs":  ls.get("teams", {}).get("away", {}).get("runs", 0),
                "home_runs":  ls.get("teams", {}).get("home", {}).get("runs", 0),
            })

    return games


# ── Kalshi MLB markets ─────────────────────────────────
async def get_kalshi_game(away_short: str, home_short: str) -> dict | None:
    """Find and return Kalshi market prices for a specific in-progress game."""
    now_et    = datetime.now(timezone.utc) - timedelta(hours=4)
    date_str  = now_et.strftime("%y%b%d").upper()  # e.g. 26APR03

    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/events",
            params={"limit": 100, "status": "open", "series_ticker": "KXMLBGAME"},
        )
        events = r.json().get("events", [])

        matched_event = None
        for e in events:
            ticker = e.get("event_ticker", "")
            title  = e.get("title", "")
            # Must match today's date AND both teams
            if date_str in ticker and away_short in title and home_short in title:
                matched_event = e
                break

        if not matched_event:
            return None

        # Get markets for this event
        r2 = await http.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            params={"limit": 5, "status": "open", "event_ticker": matched_event["event_ticker"]},
        )
        markets = r2.json().get("markets", [])

        result = {"event_ticker": matched_event["event_ticker"], "teams": {}}
        for m in markets:
            bid   = float(m.get("yes_bid_dollars") or 0)
            ask   = float(m.get("yes_ask_dollars") or 0)
            vol24 = float(m.get("volume_24h_fp") or 0)
            team  = m.get("yes_sub_title", "")
            if (bid > 0 or ask > 0) and team:
                result["teams"][team] = {
                    "bid":   bid,
                    "ask":   ask,
                    "mid":   round((bid + ask) / 2, 3),
                    "vol24": vol24,
                }

        return result if result["teams"] else None


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
                "inning", "half", "outs", "run_diff",
                "model_prob", "kalshi_mid", "edge",
                "vol24", "signal",
            ])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for r in results:
            writer.writerow([
                now, r["event_ticker"], r["team"],
                r["inning"], r["half"], r["outs"], r["run_diff"],
                r["model_prob"], r["kalshi_mid"], r["edge"],
                r["vol24"], r["signal"],
            ])


# ── Main ───────────────────────────────────────────────
async def main():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"MLB Live Scanner — {now_str}")
    print()

    # Get live games from MLB Stats API
    try:
        live_games = get_live_games()
    except Exception as e:
        print(f"Failed to get live games: {e}")
        return

    if not live_games:
        print("No games currently in progress.")
        return

    print(f"Live games: {len(live_games)}")
    for g in live_games:
        run_diff = g["home_runs"] - g["away_runs"]
        leader   = "tied" if run_diff == 0 else f"{g['home_short'] if run_diff > 0 else g['away_short']} +{abs(run_diff)}"
        print(f"  {g['away_short']} @ {g['home_short']} — Inning {g['inning']} {g['half']} ({g['outs']} outs) — {leader}")
    print()

    # Scan each live game
    all_results = []

    for g in live_games:
        kalshi = await get_kalshi_game(g["away_short"], g["home_short"])
        if not kalshi:
            print(f"  No Kalshi market found for {g['away_short']} @ {g['home_short']}")
            continue

        run_diff = g["home_runs"] - g["away_runs"]

        print(f"{g['away_short']} @ {g['home_short']} — Inn {g['inning']} {g['half']} {g['outs']}out — {g['away_runs']}-{g['home_runs']}")
        print(f"  Kalshi: {kalshi['event_ticker']}")
        print(f"  {'Team':<18} {'Kalshi':<8} {'Model':<8} {'Edge':<8} {'Vol24h':<10} Signal")
        print(f"  {'-'*60}")

        for team, data in kalshi["teams"].items():
            if data["vol24"] < MIN_VOL_24H:
                continue

            # Calculate model probability for this team
            is_home    = (team == g["home_short"])
            model_prob = win_probability(
                run_diff   = run_diff,
                inning     = g["inning"],
                inning_half= g["half"],
                outs       = g["outs"],
                is_home    = is_home,
            )

            kalshi_mid = data["mid"]
            edge       = model_prob - kalshi_mid
            signal     = ""

            if edge > MIN_EDGE:
                signal = "BUY"
            elif edge < -MIN_EDGE:
                signal = "SELL"
            elif abs(edge) > 0.02:
                signal = "WATCH"

            icon = "✅" if signal == "BUY" else "🔴" if signal == "SELL" else "👀" if signal == "WATCH" else ""
            print(f"  {team:<18} {kalshi_mid:<8.3f} {model_prob:<8.3f} {edge:+.3f}    {data['vol24']:<10,.0f} {icon} {signal}")

            all_results.append({
                "event_ticker": kalshi["event_ticker"],
                "team":         team,
                "inning":       g["inning"],
                "half":         g["half"],
                "outs":         g["outs"],
                "run_diff":     run_diff,
                "model_prob":   model_prob,
                "kalshi_mid":   kalshi_mid,
                "edge":         round(edge, 4),
                "vol24":        data["vol24"],
                "signal":       signal,
            })

        print()

    # Summary
    strong = [r for r in all_results if r["signal"] in ("BUY", "SELL")]
    print(f"Strong signals: {len(strong)}")

    # iMessage if strong signals
    if strong:
        now_short = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
        lines = [f"⚾ MLB Live — {now_short}"]
        for r in strong[:4]:
            inn_str = f"Inn {r['inning']} {r['half']}"
            lines.append(
                f"  {r['signal']} {r['team']} "
                f"kalshi={r['kalshi_mid']:.2f} model={r['model_prob']:.2f} "
                f"edge={r['edge']:+.2f} ({inn_str})"
            )
        message = "\n".join(lines)
        print()
        print("--- iMessage ---")
        print(message)
        send_imessage(message)
        print("iMessage sent.")

    # Log
    if all_results:
        log_results(all_results)
        print(f"Logged {len(all_results)} rows to {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())