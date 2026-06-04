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
import json
import math
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv

load_dotenv()

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'))
from app.services.trader import get_balance

# ── Claude client ───────────────────────────────────────
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Config ─────────────────────────────────────────────
IMESSAGE_NUMBER = "5129928658"
LOG_FILE        = Path(__file__).parent / "mlb_live_scanner_log.csv"
LOCK_FILE       = Path(__file__).parent / "mlb_scanner.lock"
LAST_SIGNALS_FILE  = Path(__file__).parent / "mlb_last_signals.txt"
                                 # tracks last alerted signal set — prevents repeat iMessages
POSITION_LOCK_FILE = Path(__file__).parent / "mlb_positions.json"
                                 # persists active positions across scan cycles (event_ticker → side)

# ── Tiered Edge Thresholds Based on Backtest Performance ─
EDGE_THRESHOLDS = {
    'premium':  0.06,  # Tied games, innings 4-5 (70% hit rate in 8-12¢ bucket)
    'good':     0.08,  # ±2 runs, innings 4-6 (57% hit rate in 8-12¢ bucket)
    'standard': 0.10,  # All other contexts
}

# Updated configs based on performance analysis
MIN_EDGE        = 0.05   # Base minimum, but context-aware thresholds override this
MAX_EDGE        = 0.12   # Keep 12¢ cap — 12-20¢ bucket has 13% accuracy (anti-signal)
MIN_VOL_24H     = 500    # Lowered from 1000 for high-quality signals
MIN_INNING      = 4      # ignore signals before this inning — model too flat early
MAX_INNING      = 6      # Extended from 5 — inning 6 still has 42% accuracy
KELLY_FRACTION  = 0.5    # half-Kelly to reduce variance
ODDS_API_KEY    = os.getenv("ODDS_API_KEY", "")  # set in .env

# ── Quality Scoring System (based on backtest cross-tabs) ─
QUALITY_WEIGHTS = {
    'tied_game': 3,        # 52.8% accuracy vs 13.6% for ±1 run
    'two_run_diff': 1,     # 46.8% accuracy - decent
    'sweet_spot_inning': 2, # Innings 4-6 perform well
    'edge_bucket_8_12': 3, # 56.4% accuracy - the money zone
    'edge_bucket_5_8': 1,  # 44.2% accuracy - marginal
}

MAX_SIGNALS_PER_ALERT = 2  # Only send top ranked signals to prevent decision fatigue

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


# ── Odds API team name map ─────────────────────────────
# Maps Kalshi short names → The Odds API team names
ODDS_TEAM_MAP = {
    "Arizona":        "Arizona Diamondbacks",
    "Atlanta":        "Atlanta Braves",
    "Baltimore":      "Baltimore Orioles",
    "Boston":         "Boston Red Sox",
    "Chicago C":      "Chicago Cubs",
    "Chicago WS":     "Chicago White Sox",
    "Cincinnati":     "Cincinnati Reds",
    "Cleveland":      "Cleveland Guardians",
    "Colorado":       "Colorado Rockies",
    "Detroit":        "Detroit Tigers",
    "Houston":        "Houston Astros",
    "Kansas City":    "Kansas City Royals",
    "Los Angeles A":  "Los Angeles Angels",
    "Los Angeles D":  "Los Angeles Dodgers",
    "Miami":          "Miami Marlins",
    "Milwaukee":      "Milwaukee Brewers",
    "Minnesota":      "Minnesota Twins",
    "New York M":     "New York Mets",
    "New York Y":     "New York Yankees",
    "A's":            "Oakland Athletics",
    "Philadelphia":   "Philadelphia Phillies",
    "Pittsburgh":     "Pittsburgh Pirates",
    "San Diego":      "San Diego Padres",
    "San Francisco":  "San Francisco Giants",
    "Seattle":        "Seattle Mariners",
    "St. Louis":      "St. Louis Cardinals",
    "Tampa Bay":      "Tampa Bay Rays",
    "Texas":          "Texas Rangers",
    "Toronto":        "Toronto Blue Jays",
    "Washington":     "Washington Nationals",
}


def moneyline_to_prob(american_odds: int) -> float:
    """Convert American moneyline odds to implied probability."""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    else:
        return abs(american_odds) / (abs(american_odds) + 100)


# ── Odds cache ─────────────────────────────────────────
ODDS_CACHE_FILE = Path(__file__).parent / "mlb_odds_cache.json"


def load_odds_cache() -> dict[str, float] | None:
    """
    Load cached pregame odds if they were fetched today (ET date).
    Returns None if cache is missing or stale.
    """
    if not ODDS_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(ODDS_CACHE_FILE.read_text())
        cached_date = data.get("date")
        now_et  = datetime.now(timezone.utc) - timedelta(hours=4)
        today_et = now_et.strftime("%Y-%m-%d")
        if cached_date != today_et:
            return None
        print(f"  Using cached odds from {data.get('fetched_at', 'unknown')} ET")
        return data.get("probs", {})
    except Exception as e:
        print(f"  Odds cache read error: {e}")
        return None


def save_odds_cache(probs: dict[str, float]):
    """Save fetched odds to cache file with today's ET date."""
    try:
        now_et = datetime.now(timezone.utc) - timedelta(hours=4)
        ODDS_CACHE_FILE.write_text(json.dumps({
            "date":       now_et.strftime("%Y-%m-%d"),
            "fetched_at": now_et.strftime("%H:%M"),
            "probs":      probs,
        }, indent=2))
    except Exception as e:
        print(f"  Odds cache write error: {e}")


def fetch_pregame_probs() -> dict[str, float]:
    """
    Fetch today's MLB moneylines from The Odds API.
    Uses a per-day cache — only hits the API once per calendar day (ET).

    Filters out stale bookmaker lines (updated >30 min ago) and skips games
    where remaining books disagree by >10% implied probability — sign of
    mixed pre/in-game data or market maker error.
    """
    # Try cache first — avoids burning API calls on repeat scans
    cached = load_odds_cache()
    if cached is not None:
        return cached

    if not ODDS_API_KEY:
        return {}

    try:
        r = httpx.get(
            "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
            params={
                "apiKey":  ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
            },
            timeout=10.0,
        )
        r.raise_for_status()
        games = r.json()
    except Exception as e:
        print(f"  Odds API error: {e}")
        return {}

    reverse_map = {v: k for k, v in ODDS_TEAM_MAP.items()}
    now_utc     = datetime.now(timezone.utc)
    probs: dict[str, float] = {}

    for game in games:
        home_full  = game.get("home_team", "")
        away_full  = game.get("away_team", "")
        home_short = reverse_map.get(home_full)
        away_short = reverse_map.get(away_full)

        if not home_short or not away_short:
            continue

        # Collect per-bookmaker implied probs, filtering stale lines
        home_probs, away_probs = [], []
        for bm in game.get("bookmakers", []):
            # Filter bookmakers whose lines are >30 minutes old
            last_update_str = bm.get("last_update", "")
            if last_update_str:
                try:
                    last_update = datetime.fromisoformat(
                        last_update_str.replace("Z", "+00:00")
                    )
                    age_minutes = (now_utc - last_update).total_seconds() / 60
                    if age_minutes > 30:
                        continue  # skip stale bookmaker
                except Exception:
                    pass  # if we can't parse, include it

            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    odds = outcome.get("price", 0)
                    name = outcome.get("name", "")
                    if name == home_full:
                        home_probs.append(moneyline_to_prob(odds))
                    elif name == away_full:
                        away_probs.append(moneyline_to_prob(odds))

        if not home_probs or not away_probs:
            continue

        # Flag high disagreement across books — skip if spread >10%
        if max(home_probs) - min(home_probs) > 0.10:
            print(f"  ⚠️  {home_short} vs {away_short}: bookmaker disagreement "
                  f"({min(home_probs):.2f}–{max(home_probs):.2f}) — skipping pregame prob")
            continue

        # Normalize to remove vig
        raw_home  = sum(home_probs) / len(home_probs)
        raw_away  = sum(away_probs) / len(away_probs)
        total     = raw_home + raw_away
        norm_home = raw_home / total
        norm_away = raw_away / total

        probs[home_short] = round(norm_home, 4)
        probs[away_short] = round(norm_away, 4)

    if probs:
        save_odds_cache(probs)
        print(f"  Fetched fresh odds for {len(probs)} teams — cached for today")

    return probs


def blended_win_probability(
    pregame_prob:  float,   # from Odds API (team's pre-game win prob)
    run_diff:      int,     # positive = home leading
    inning:        int,
    inning_half:   str,
    outs:          int,
    is_home:       bool,
    pitcher_adj:   float = 0.0,  # log-odds adjustment from ERA differential
) -> float:
    """
    Blend pre-game Vegas probability with in-game run differential model,
    plus a pitcher quality adjustment based on current ERA differential.

    Weight shifts from pregame → in-game as game progresses:
      - Inning 1: 85% pregame, 15% in-game
      - Inning 5: 40% pregame, 60% in-game
      - Inning 8+: 10% pregame, 90% in-game

    Pitcher adjustment is applied to the in-game log-odds before blending.
    Positive pitcher_adj = home pitcher better = home team boosted.
    """
    pregame_weight = max(0.10, 0.85 - (inning - 1) * 0.083)
    ingame_weight  = 1 - pregame_weight

    # In-game probability with pitcher adjustment baked in
    # Compute base log-odds then add pitcher adjustment
    if inning_half == "Top":
        half_innings_done = (inning - 1) * 2
    else:
        half_innings_done = (inning - 1) * 2 + 1

    out_fraction   = outs / 3.0
    effective_done = half_innings_done + out_fraction
    half_inn_left  = max(0, 18 - effective_done)
    innings_left   = half_inn_left / 2

    home_advantage = 0.16

    if innings_left > 0:
        run_weight = 0.45 / math.sqrt(innings_left + 0.5)
        # Scale pitcher adjustment by innings remaining fraction
        # (pitcher matters less late in game when bullpens take over)
        innings_fraction = innings_left / 9.0
        scaled_pitcher_adj = pitcher_adj * innings_fraction
        log_odds   = run_diff * run_weight + home_advantage + scaled_pitcher_adj
        home_prob  = logistic(log_odds)
    else:
        if run_diff > 0:
            home_prob = 1.0
        elif run_diff < 0:
            home_prob = 0.0
        else:
            home_prob = 0.5

    ingame_prob = round(home_prob if is_home else 1 - home_prob, 4)
    blended = pregame_weight * pregame_prob + ingame_weight * ingame_prob
    return round(blended, 4)


# ── Stale data detection ───────────────────────────────
def implied_run_diff(kalshi_mid: float, inning: int, inning_half: str, outs: int) -> float:
    """
    Invert the win probability model to estimate what run differential
    the market is implying for the HOME team.
    Used to cross-check against MLB Stats API score.
    """
    # Calculate innings remaining (same logic as win_probability)
    if inning_half == "Top":
        half_innings_done = (inning - 1) * 2
    else:
        half_innings_done = (inning - 1) * 2 + 1

    out_fraction   = outs / 3.0
    effective_done = half_innings_done + out_fraction
    half_inn_left  = max(0, 18 - effective_done)
    innings_left   = half_inn_left / 2

    home_advantage = 0.16

    if innings_left <= 0:
        return 0.0

    run_weight = 0.45 / math.sqrt(innings_left + 0.5)

    # Invert: log_odds = logit(kalshi_mid) - home_advantage
    # run_diff = log_odds / run_weight
    if kalshi_mid <= 0 or kalshi_mid >= 1:
        return 0.0

    log_odds = math.log(kalshi_mid / (1 - kalshi_mid))
    return (log_odds - home_advantage) / run_weight


STALE_DATA_THRESHOLD = 0.20  # if |kalshi_mid - 0.5| > this, sanity check fires

def is_stale_data(
    run_diff: int,
    kalshi_mid: float,
    inning: int,
    inning_half: str,
    outs: int,
    is_home: bool,
) -> tuple[bool, str]:
    """
    Returns (is_stale, reason) if Kalshi price implies a very different
    game state than what MLB Stats API is reporting.

    Key heuristic: if Kalshi has a team at >70¢ but our score data
    shows a tied game (or close), our data is probably stale.
    """
    # Adjust kalshi_mid to always be from home team perspective
    home_mid = kalshi_mid if is_home else 1 - kalshi_mid

    market_conviction = abs(home_mid - 0.5)

    if market_conviction < STALE_DATA_THRESHOLD:
        return False, ""  # market is close enough to 50/50, no concern

    # Market is confident — check if our run_diff agrees
    implied = implied_run_diff(home_mid, inning, inning_half, outs)

    # If market implies a 2+ run lead but we see 0 runs scored, flag it
    run_diff_gap = abs(implied - run_diff)

    if run_diff_gap >= 2.0:
        direction = "home" if implied > 0 else "away"
        return True, (
            f"STALE DATA: market implies ~{implied:+.1f} run lead ({direction}), "
            f"MLB API shows {run_diff:+d} — skipping"
        )

    return False, ""


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


# ── Pitcher quality adjustment ─────────────────────────
def get_pitcher_adjustment(game_pk: int) -> tuple[float, str]:
    """
    Fetch current pitchers from MLB Stats API boxscore and compute
    a log-odds adjustment based on ERA differential.

    Logic:
      - Get current pitcher for each team from the boxscore
      - Use season ERA as proxy for quality
      - ERA differential → log-odds adjustment added to home team
      - Positive adjustment = home pitcher better → home team boosted
      - Capped at ±0.30 log-odds to prevent extreme swings

    Returns (adjustment_log_odds, description_string)
    Returns (0.0, "unavailable") if data can't be fetched.

    ERA calibration:
      - League average ERA ~4.20
      - Each 1.0 ERA difference ≈ 0.08 log-odds adjustment
        (estimated: 1 ERA point ≈ 0.5 runs/9inn ≈ ~8% win prob swing over full game)
      - Scaled by innings remaining fraction so early game has more impact
    """
    try:
        r = httpx.get(
            f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
            timeout=10.0,
        )
        r.raise_for_status()
        box = r.json()

        def get_current_era(team_data: dict) -> tuple[float, str]:
            """Extract current pitcher's season ERA from boxscore team data."""
            pitchers = team_data.get("pitchers", [])
            pitcher_info = team_data.get("players", {})

            if not pitchers:
                return 4.20, "unknown"

            # Current pitcher is the last in the list
            current_pid = f"ID{pitchers[-1]}"
            player = pitcher_info.get(current_pid, {})
            name   = player.get("person", {}).get("fullName", "unknown")
            stats  = player.get("seasonStats", {}).get("pitching", {})
            era    = stats.get("era", "4.20")

            try:
                return float(era), name
            except (ValueError, TypeError):
                return 4.20, name

        home_era, home_name = get_current_era(box.get("teams", {}).get("home", {}))
        away_era, away_name = get_current_era(box.get("teams", {}).get("away", {}))

        # Positive = home pitcher better (lower ERA)
        era_diff   = away_era - home_era
        adjustment = era_diff * 0.08
        adjustment = max(-0.30, min(0.30, adjustment))  # cap

        desc = f"home={home_name}({home_era:.2f}) away={away_name}({away_era:.2f}) adj={adjustment:+.3f}"
        return round(adjustment, 4), desc

    except Exception as e:
        return 0.0, f"unavailable ({e})"


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


# ── Kelly position sizing ──────────────────────────────
def kelly_size(
    edge: float,        # executable edge (edge_buy for BUY, edge_sell for SELL)
    price: float,       # kalshi_ask for BUY, (1 - kalshi_bid) for SELL
    bankroll: float,
    fraction: float = KELLY_FRACTION,
) -> dict:
    """
    Compute Kelly-optimal position size for a Kalshi binary contract.

    For a binary paying $1 on win:
      b = (1 - price) / price   (net odds per dollar risked)
      p = model probability of winning
      q = 1 - p
      Kelly % = (b*p - q) / b  =  edge / (1 - price)

    Returns dict with full_kelly, fractional, and recommended contract count.
    """
    if price <= 0 or price >= 1 or edge <= 0:
        return {"pct": 0.0, "full_dollar": 0.0, "frac_dollar": 0.0, "contracts": 0}

    # Net odds: win (1-price) per dollar risked
    b = (1 - price) / price
    kelly_pct = edge / (1 - price)
    kelly_pct = max(0.0, min(kelly_pct, 0.25))  # cap at 25% of bankroll

    full_dollar = round(bankroll * kelly_pct, 2)
    frac_dollar = round(bankroll * kelly_pct * fraction, 2)
    contracts   = max(1, round(frac_dollar / price))

    return {
        "pct":        round(kelly_pct * 100, 1),
        "full_dollar": full_dollar,
        "frac_dollar": frac_dollar,
        "contracts":   contracts,
    }



# ── Signal Quality Scoring (based on backtest performance) ─
def calculate_signal_quality(edge: float, inning: int, run_diff: int) -> tuple[int, str]:
    """
    Calculate quality score for a signal based on backtest performance data.
    Returns (score, reasoning_string)
    """
    score = 0
    reasons = []
    
    # Edge bucket scoring (most important factor)
    if 0.08 <= edge <= 0.12:
        score += QUALITY_WEIGHTS['edge_bucket_8_12']
        reasons.append("8-12¢ edge (56% hit rate)")
    elif 0.05 <= edge <= 0.08:
        score += QUALITY_WEIGHTS['edge_bucket_5_8'] 
        reasons.append("5-8¢ edge (44% hit rate)")
    
    # Game state scoring
    if run_diff == 0:
        score += QUALITY_WEIGHTS['tied_game']
        reasons.append("tied game (53% hit rate)")
    elif abs(run_diff) == 2:
        score += QUALITY_WEIGHTS['two_run_diff']
        reasons.append("±2 runs (47% hit rate)")
    
    # Inning scoring (4-6 are sweet spot)
    if 4 <= inning <= 6:
        score += QUALITY_WEIGHTS['sweet_spot_inning']
        reasons.append(f"inning {inning}")
        
    reasoning = ", ".join(reasons) if reasons else "standard signal"
    return score, reasoning


def get_edge_threshold(inning: int, run_diff: int) -> float:
    """
    Return appropriate edge threshold based on game context.
    Lower thresholds for higher-quality contexts based on backtest data.
    """
    # Premium tier: tied games in innings 4-5 (70% hit rate in 8-12¢ bucket)
    if run_diff == 0 and 4 <= inning <= 5:
        return EDGE_THRESHOLDS['premium']
    
    # Good tier: ±2 runs in innings 4-6 (57% hit rate in 8-12¢ bucket)
    if abs(run_diff) == 2 and 4 <= inning <= 6:
        return EDGE_THRESHOLDS['good']
        
    # Standard tier: everything else
    return EDGE_THRESHOLDS['standard']


def is_signal_worthy(edge: float, inning: int, run_diff: int, vol24: int, 
                    min_quality_score: int = 3) -> tuple[bool, str]:
    """
    Comprehensive signal evaluation based on backtest performance.
    Returns (is_worthy, explanation)
    """
    # Safety checks first (avoid known failure modes)
    if abs(run_diff) == 1:
        return False, "±1 run games have 13.6% hit rate"
        
    if edge > MAX_EDGE:
        return False, f"edge {edge:.3f} above {MAX_EDGE} (likely model error - 12-20¢ bucket has 13% accuracy)"
        
    if inning > MAX_INNING:
        return False, f"inning {inning} above {MAX_INNING} (accuracy drops after inning 6)"
    
    # Context-aware edge threshold
    edge_threshold = get_edge_threshold(inning, run_diff)
    if edge < edge_threshold:
        return False, f"edge {edge:.3f} below {edge_threshold:.3f} threshold for this context"
    
    # Quality scoring
    quality_score, reasoning = calculate_signal_quality(edge, inning, run_diff)
    if quality_score < min_quality_score:
        return False, f"quality score {quality_score} below minimum {min_quality_score}"
    
    # Dynamic volume threshold (lower for high-quality signals)
    min_vol = MIN_VOL_24H if quality_score < 5 else MIN_VOL_24H // 2
    if vol24 < min_vol:
        return False, f"volume {vol24:,.0f} below {min_vol:,.0f}"
        
    return True, f"QUALITY SIGNAL: {reasoning} (score: {quality_score})"


def rank_signals_by_priority(signal_rows: list) -> list:
    """
    Sort signal candidates by quality score + edge.
    Returns list of (priority_score, row) tuples sorted by priority descending.
    """
    ranked = []
    for row in signal_rows:
        edge = row['edge_buy'] if row['signal'] == 'BUY' else row['edge_sell']
        quality_score, _ = calculate_signal_quality(edge, row['inning'], row['run_diff'])
        
        # Priority = quality_score * 100 + edge (quality dominates, edge breaks ties)
        priority = quality_score * 100 + edge
        ranked.append((priority, row))
    
    return sorted(ranked, key=lambda x: x[0], reverse=True)


# ── Position lock & mirror deduplication ──────────────
def load_positions() -> dict[str, str]:
    """Load active positions from disk. Returns {event_ticker: side} e.g. {"KXMLBGAME-26APR141835AZBAL": "YES"}"""
    if not POSITION_LOCK_FILE.exists():
        return {}
    try:
        data = json.loads(POSITION_LOCK_FILE.read_text())
        # Prune positions older than 12 hours (game is definitely over)
        now_ts = datetime.now(timezone.utc).timestamp()
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and now_ts - v.get("ts", 0) < 43200}
    except Exception:
        return {}


def save_positions(positions: dict):
    """Persist active positions to disk."""
    try:
        POSITION_LOCK_FILE.write_text(json.dumps(positions, indent=2))
    except Exception as e:
        print(f"  Position lock write error: {e}")


def register_position(event_ticker: str, side: str, positions: dict):
    """Record that we entered a position on this market."""
    positions[event_ticker] = {"side": side, "ts": datetime.now(timezone.utc).timestamp()}
    save_positions(positions)


def is_already_positioned(event_ticker: str, positions: dict) -> tuple[bool, str]:
    """Return (True, side) if we already have any position on this market, else (False, '')."""
    entry = positions.get(event_ticker)
    if entry and isinstance(entry, dict):
        return True, entry.get("side", "?")
    return False, ""

# ── Claude trade rationale ─────────────────────────────
def generate_trade_rationale(r: dict, exec_edge: float) -> str:
    """
    Call Claude Haiku to generate a 2-sentence trade rationale for a signal.
    Returns the rationale string, or a fallback message on failure.
    """
    prompt = f"""
You are a prediction market trading analyst. In 2 sentences max, explain why this MLB signal is worth acting on or not.

Signal:
- Game: {r['away_short']} @ {r['home_short']}
- Inning: {r['inning']} {r['half']}, {r['outs']} outs
- Run differential: {r['run_diff']} (0=tied)
- Signal: {r['signal']}
- Executable edge: {exec_edge:+.3f}
- Model prob: {r['model_prob']:.3f}, Kalshi mid: {r['kalshi_mid']:.3f}
- Prob source: {r.get('prob_source', 'unknown')}

Backtest context: tied games innings 4-5 with 5-8¢ edge = 58.6% accuracy, +22.7% ROI.
Be direct. No fluff. End with BUY or SKIP.
"""
    response = _claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=120,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip().replace("\n", " ")


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
                "scan_time", "game_pk", "event_ticker",
                "away_short", "home_short",
                "team", "inning", "half", "outs", "run_diff", "run_diff_tag",
                "pregame_prob", "prob_source", "pitcher_adj",
                "model_prob", "kalshi_mid", "kalshi_bid", "kalshi_ask",
                "edge_mid", "edge_buy", "edge_sell",
                "vol24", "signal",
                "kelly_pct", "half_kelly", "full_kelly", "kelly_contracts",
            ])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for r in results:
            writer.writerow([
                now,
                r["game_pk"],
                r["event_ticker"],
                r["away_short"],
                r["home_short"],
                r["team"],
                r["inning"],
                r["half"],
                r["outs"],
                r["run_diff"],
                r.get("run_diff_tag", ""),
                r["pregame_prob"],
                r["prob_source"],
                r.get("pitcher_adj", 0.0),
                r["model_prob"],
                r["kalshi_mid"],
                r["kalshi_bid"],
                r["kalshi_ask"],
                r["edge_mid"],
                r["edge_buy"],
                r["edge_sell"],
                r["vol24"],
                r["signal"],
                r.get("kelly_pct", 0.0),
                r.get("half_kelly", 0.0),
                r.get("full_kelly", 0.0),
                r.get("kelly_contracts", 0),
            ])


# ── Main ───────────────────────────────────────────────
async def main():
    # ── Lockfile guard — prevent overlapping runs ──────
    if LOCK_FILE.exists():
        # Check if lockfile is stale (scanner crashed without cleanup)
        age_minutes = (datetime.now(timezone.utc).timestamp() - LOCK_FILE.stat().st_mtime) / 60
        if age_minutes < 10:
            print(f"Scanner already running (lock age: {age_minutes:.1f}min) — exiting")
            return
        else:
            print(f"Stale lockfile found ({age_minutes:.1f}min old) — removing and continuing")
            LOCK_FILE.unlink()

    LOCK_FILE.touch()
    try:
        await _main()
    finally:
        LOCK_FILE.unlink(missing_ok=True)


async def _main():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"🔄 MLB Live Scanner (OPTIMIZED) — {now_str}")
    print(f"⚙️  Tiered edges: Premium ≥{EDGE_THRESHOLDS['premium']:.2f}, Good ≥{EDGE_THRESHOLDS['good']:.2f}, Standard ≥{EDGE_THRESHOLDS['standard']:.2f}")
    print(f"⚙️  Max edge: {MAX_EDGE:.2f}, Volume: {MIN_VOL_24H:,}+, Innings: {MIN_INNING}-{MAX_INNING}")
    print(f"📊 Max {MAX_SIGNALS_PER_ALERT} signals per alert (quality-ranked)")
    print(f"🎯 Quality focus: 8-12¢ bucket (56% hit rate), tied games (53% hit rate)")
    print()

    # Fetch live bankroll
    bankroll = await get_balance()
    if bankroll is None:
        bankroll = 50.0
        print(f"Bankroll:    ${bankroll:.2f}  ⚠️  (API failed — using fallback)")
    else:
        print(f"Bankroll:    ${bankroll:.2f}  (live)")
    print()

    # Fetch pre-game Vegas probabilities once
    print("Fetching pre-game odds from The Odds API...")
    pregame_probs = fetch_pregame_probs()
    if pregame_probs:
        print(f"  Got odds for {len(pregame_probs)} teams")
    else:
        print("  No odds available — falling back to model-only")
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
    all_results     = []
    active_positions = load_positions()   # persisted across scan cycles
    signaled_games  = set()               # dedupe mirror signals within this scan cycle

    for g in live_games:
        kalshi = await get_kalshi_game(g["away_short"], g["home_short"])
        if not kalshi:
            print(f"  No Kalshi market found for {g['away_short']} @ {g['home_short']}")
            continue

        run_diff = g["home_runs"] - g["away_runs"]

        # Fetch pitcher adjustment once per game
        pitcher_adj, pitcher_desc = get_pitcher_adjustment(g["game_pk"])

        print(f"{g['away_short']} @ {g['home_short']} — Inn {g['inning']} {g['half']} {g['outs']}out — {g['away_runs']}-{g['home_runs']}")
        print(f"  Kalshi: {kalshi['event_ticker']}  game_pk: {g['game_pk']}")
        print(f"  Pitchers: {pitcher_desc}")
        # ── Pass 1: evaluate all teams, collect actionable candidates ──
        # We do this in two passes so we can pick the HIGHEST-EDGE signal
        # per game rather than whichever team happens to be iterated first.
        candidates = []   # (exec_edge, signal, team, data, computed fields...)

        for team, data in kalshi["teams"].items():
            if data["vol24"] < MIN_VOL_24H:
                continue

            if g["inning"] < MIN_INNING:
                print(f"  {team:<18} ⏭  Skipping — inning {g['inning']} < MIN_INNING ({MIN_INNING})")
                continue

            if g["inning"] > MAX_INNING:
                print(f"  {team:<18} ⏭  Skipping — inning {g['inning']} > MAX_INNING ({MAX_INNING})")
                continue

            # Skip ±1 run situations — backtest shows 13.6% accuracy here
            if abs(run_diff) == 1:
                print(f"  {team:<18} ⏭  Skipping — ±1 run diff (low model accuracy)")
                continue

            # Calculate model probability for this team
            is_home      = (team == g["home_short"])
            pregame_prob = pregame_probs.get(team)

            if pregame_prob is not None:
                model_prob = blended_win_probability(
                    pregame_prob = pregame_prob,
                    run_diff     = run_diff,
                    inning       = g["inning"],
                    inning_half  = g["half"],
                    outs         = g["outs"],
                    is_home      = is_home,
                    pitcher_adj  = pitcher_adj,
                )
                prob_source = f"blend(vegas={pregame_prob:.2f})"
            else:
                model_prob = win_probability(
                    run_diff    = run_diff,
                    inning      = g["inning"],
                    inning_half = g["half"],
                    outs        = g["outs"],
                    is_home     = is_home,
                )
                prob_source = "model-only"

            kalshi_mid  = data["mid"]
            kalshi_bid  = data["bid"]
            kalshi_ask  = data["ask"]

            edge_mid  = round(model_prob - kalshi_mid, 4)
            edge_buy  = round(model_prob - kalshi_ask, 4)
            edge_sell = round(kalshi_bid - model_prob, 4)

            # ── Stale data check ──────────────────────────
            stale, stale_reason = is_stale_data(
                run_diff    = run_diff,
                kalshi_mid  = kalshi_mid,
                inning      = g["inning"],
                inning_half = g["half"],
                outs        = g["outs"],
                is_home     = is_home,
            )
            if stale:
                print(f"  {team:<18} ⚠️  {stale_reason}")
                signal = "STALE"
            else:
                signal = ""
                
                # ── New Quality-Based Signal Evaluation ──
                # Check BUY signal with quality scoring system
                if edge_buy > 0:
                    is_worthy, reason = is_signal_worthy(
                        edge_buy, g["inning"], run_diff, data["vol24"]
                    )
                    if is_worthy:
                        signal = "BUY"
                        quality_score, quality_reasoning = calculate_signal_quality(
                            edge_buy, g["inning"], run_diff
                        )
                        print(f"  {team:<18} ✅ BUY edge={edge_buy:.3f} quality={quality_score} - {quality_reasoning}")
                    else:
                        print(f"  {team:<18} ⛔ BUY rejected - {reason}")
                
                # Check SELL signal with quality scoring system  
                elif edge_sell > 0:
                    is_worthy, reason = is_signal_worthy(
                        edge_sell, g["inning"], run_diff, data["vol24"]
                    )
                    if is_worthy:
                        signal = "SELL"
                        quality_score, quality_reasoning = calculate_signal_quality(
                            edge_sell, g["inning"], run_diff
                        )
                        print(f"  {team:<18} ✅ SELL edge={edge_sell:.3f} quality={quality_score} - {quality_reasoning}")
                    else:
                        print(f"  {team:<18} ⛔ SELL rejected - {reason}")
                        
                # Keep WATCH signals for market awareness
                elif abs(edge_mid) > 0.02:
                    signal = "WATCH"

            run_tag = "TIED" if run_diff == 0 else f"±{abs(run_diff)}run"

            row = {
                "game_pk":      g["game_pk"],
                "event_ticker": kalshi["event_ticker"],
                "away_short":   g["away_short"],
                "home_short":   g["home_short"],
                "team":         team,
                "inning":       g["inning"],
                "half":         g["half"],
                "outs":         g["outs"],
                "run_diff":     run_diff,
                "run_diff_tag": run_tag,
                "pregame_prob": pregame_prob if pregame_prob is not None else "",
                "prob_source":  prob_source,
                "pitcher_adj":  pitcher_adj,
                "model_prob":   model_prob,
                "kalshi_mid":   kalshi_mid,
                "kalshi_bid":   kalshi_bid,
                "kalshi_ask":   kalshi_ask,
                "edge_mid":     edge_mid,
                "edge_buy":     edge_buy,
                "edge_sell":    edge_sell,
                "vol24":        data["vol24"],
                "signal":       signal,  # may be overridden in pass 2
                "kelly_pct":    0.0,
                "half_kelly":   0.0,
                "full_kelly":   0.0,
                "kelly_contracts": 0,
            }

            if signal in ("BUY", "SELL"):
                exec_edge = edge_buy if signal == "BUY" else edge_sell
                candidates.append((exec_edge, row))
            else:
                # Non-actionable rows go straight to results (for logging)
                all_results.append(row)

        # ── Pass 2: Quality-based ranking and selection ──────────────
        # Check position lock once per game (same for all candidates)
        positioned, held_side = is_already_positioned(kalshi["event_ticker"], active_positions)

        if candidates:
            if positioned:
                # Already in this game — suppress everything
                for _, row in candidates:
                    print(f"  {row['team']:<18} ⛔ Already positioned ({held_side}) on {kalshi['event_ticker']} — skipping")
                    row["signal"] = ""
                    all_results.append(row)
            elif kalshi["event_ticker"] in signaled_games:
                # Another scan cycle already signaled this game
                for _, row in candidates:
                    print(f"  {row['team']:<18} ⛔ Already signaled this cycle — skipping")
                    row["signal"] = ""
                    all_results.append(row)
            else:
                # ── NEW: Quality-based ranking instead of simple edge sorting ──
                candidate_rows = [row for _, row in candidates]
                ranked_candidates = rank_signals_by_priority(candidate_rows)
                
                # Take top signals only (prevent decision fatigue)
                num_to_take = min(len(ranked_candidates), MAX_SIGNALS_PER_ALERT)
                top_signals = ranked_candidates[:num_to_take]
                suppressed_signals = ranked_candidates[num_to_take:]
                
                print(f"  📊 Ranked {len(ranked_candidates)} candidates, taking top {num_to_take}")
                
                # Process top-priority signals
                for priority, row in top_signals:
                    # Size the position
                    if row["signal"] == "BUY":
                        sizing = kelly_size(row["edge_buy"], row["kalshi_ask"], bankroll)
                    else:
                        sizing = kelly_size(row["edge_sell"], 1 - row["kalshi_bid"], bankroll)
                    
                    row["kelly_pct"] = sizing["pct"]
                    row["half_kelly"] = sizing["frac_dollar"]
                    row["full_kelly"] = sizing["full_dollar"]
                    row["kelly_contracts"] = sizing["contracts"]

                    signaled_games.add(kalshi["event_ticker"])
                    all_results.append(row)
                    
                    quality_score, _ = calculate_signal_quality(
                        row["edge_buy"] if row["signal"] == "BUY" else row["edge_sell"],
                        row["inning"], row["run_diff"]
                    )
                    print(f"  🎯 TOP SIGNAL: {row['signal']} {row['team']} - Priority: {priority:.1f} (Quality: {quality_score})")

                # Suppress lower-priority signals
                for priority, row in suppressed_signals:
                    quality_score, reasoning = calculate_signal_quality(
                        row["edge_buy"] if row["signal"] == "BUY" else row["edge_sell"],
                        row["inning"], row["run_diff"]
                    )
                    print(f"  ⛔ SUPPRESSED: {row['signal']} {row['team']} - Priority: {priority:.1f} (lower rank)")
                    row["signal"] = "SUPPRESSED"
                    row["kelly_pct"] = 0.0
                    row["half_kelly"] = 0.0
                    row["full_kelly"] = 0.0
                    row["kelly_contracts"] = 0
                    all_results.append(row)

        # ── Print table for this game ─────────────────────────────────────
        print(f"  {'Team':<18} {'Bid':<6} {'Ask':<6} {'Mid':<6} {'Model':<8} {'Edge(buy)':<10} {'Edge(sell)':<10} {'Vol24h':<10} Signal")
        print(f"  {'-'*80}")
        for r in sorted(all_results, key=lambda x: x["game_pk"] == g["game_pk"], reverse=True):
            if r["game_pk"] != g["game_pk"]:
                continue
            sig   = r["signal"]
            icon  = "✅" if sig == "BUY" else "🔴" if sig == "SELL" else "👀" if sig == "WATCH" else "⛔" if sig == "MIRROR" else "⚠️" if sig == "STALE" else ""
            print(
                f"  {r['team']:<18} {r['kalshi_bid']:<6.3f} {r['kalshi_ask']:<6.3f} {r['kalshi_mid']:<6.3f} "
                f"{r['model_prob']:<8.3f} {r['edge_buy']:+.3f}     {r['edge_sell']:+.3f}      "
                f"{r['vol24']:<10,.0f} {icon} {sig}  [{r['prob_source']}] [{r['run_diff_tag']}]"
            )
            if r.get("half_kelly", 0) > 0:
                print(
                    f"  {'':<18} 💰 Half-Kelly ${r['half_kelly']:.2f}"
                    f" ({r['kelly_contracts']} contracts @ {r['kalshi_ask']:.2f}¢)"
                    f" | Full-Kelly ${r['full_kelly']:.2f}"
                    f" | {r['kelly_pct']:.1f}% of bankroll"
                )

        print()

    # Summary
    strong = [r for r in all_results if r["signal"] in ("BUY", "SELL")]
    stale  = [r for r in all_results if r["signal"] == "STALE"]
    print(f"Strong signals: {len(strong)}  |  Stale/skipped: {len(stale)}")

    # iMessage if strong signals — with deduplication
    if strong:
        # Build a fingerprint of current signals: game_pk + signal + inning
        # If this matches what we sent last time, suppress the alert
        current_fingerprint = "|".join(sorted(
            f"{r['game_pk']}:{r['signal']}:{r['team']}:{r['inning']}"
            for r in strong
        ))

        last_fingerprint = ""
        if LAST_SIGNALS_FILE.exists():
            try:
                last_fingerprint = LAST_SIGNALS_FILE.read_text().strip()
            except Exception:
                pass

        if current_fingerprint == last_fingerprint:
            print("  (iMessage suppressed — same signals as last scan)")
        else:
            now_short = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
            lines = [f"⚾ MLB Live — {now_short}"]
            for r in strong[:4]:
                inn_str   = f"Inn {r['inning']} {r['half']}"
                exec_edge = r["edge_buy"] if r["signal"] == "BUY" else r["edge_sell"]
                lines.append(
                    f"  {r['signal']} {r['team']} "
                    f"kalshi={r['kalshi_mid']:.2f} model={r['model_prob']:.2f} "
                    f"edge={exec_edge:+.2f} ({inn_str}) [{r.get('run_diff_tag', '')}]"
                )
                if r.get("half_kelly", 0) > 0:
                    lines.append(
                        f"  💰 Half-Kelly ${r['half_kelly']:.2f}"
                        f" ({r['kelly_contracts']} contracts)"
                        f" | Full ${r['full_kelly']:.2f}"
                    )
                lines.append(f"  🔗 https://kalshi.com/markets/kxmlbgame/professional-baseball-game/{r['event_ticker'].lower()}")

                # ── Claude rationale ──────────────────────────
                try:
                    rationale = generate_trade_rationale(r, exec_edge)
                    lines.append(f"  🤖 {rationale}")
                except Exception as e:
                    print(f"  Claude rationale failed: {e}")

            message = "\n".join(lines)
            print()
            print("--- iMessage ---")
            print(message)
            send_imessage(message)
            print("iMessage sent.")
            try:
                LAST_SIGNALS_FILE.write_text(current_fingerprint)
            except Exception:
                pass
    else:
        # No signals — clear the last fingerprint so next signal fires fresh
        if LAST_SIGNALS_FILE.exists():
            LAST_SIGNALS_FILE.unlink(missing_ok=True)

    # Log
    if all_results:
        log_results(all_results)
        print(f"Logged {len(all_results)} rows to {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())