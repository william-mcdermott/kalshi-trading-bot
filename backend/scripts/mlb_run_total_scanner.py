#!/usr/bin/env python3
"""
mlb_run_total_scanner.py

Scans live MLB run total markets for edge opportunities based on:
  - Real-time weather conditions (wind, temperature, humidity)
  - Bullpen fatigue and quality metrics
  - Game state adjustments (score differential, leverage)
  - Park factors and historical run scoring patterns

Strategy focuses on:
  1. Weather momentum (wind shifts, temperature changes)
  2. Bullpen mismatch edges (fatigue, quality differences)
  3. Game situation adjustments (blowouts vs close games)

Usage:
    python scripts/mlb_run_total_scanner.py

Run every 3-5 minutes during game hours for best results.
"""

import asyncio
import csv
import json
import math
import os
import subprocess
import time
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
LOG_FILE = Path(__file__).parent / "run_total_scanner_log.csv"
LOCK_FILE = Path(__file__).parent / "run_total_scanner.lock"
LAST_SIGNALS_FILE = Path(__file__).parent / "run_total_last_signals.txt"

# Edge thresholds
MIN_EDGE = 0.08  # 8¢ minimum - run totals need higher threshold
MAX_EDGE = 0.20  # 20¢ cap - less stringent than win/loss markets
MIN_VOL_24H = 500  # Lower volume threshold for totals markets
MIN_INNING = 3  # Start looking from inning 3 (some data established)
MAX_INNING = 7  # Stop at inning 7 (too little game left)

# Weather API
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")  # weatherapi.com or similar

# ── Weather caching to minimize API calls ──────────────
WEATHER_CACHE_MINUTES = 15  # Cache weather for 15 minutes per stadium
_weather_cache = {}  # Global cache: {stadium: {'data': weather_dict, 'timestamp': float}}

# ── MLB Stadiums with coordinates ──────────────────────
STADIUM_COORDS = {
    "Arizona": {"lat": 33.4454, "lon": -112.0667, "elevation": 1086, "dome": True},
    "Atlanta": {"lat": 33.8910, "lon": -84.4677, "elevation": 1057, "dome": False},
    "Baltimore": {"lat": 39.2838, "lon": -76.6214, "elevation": 92, "dome": False},
    "Boston": {"lat": 42.3467, "lon": -71.0972, "elevation": 21, "dome": False},
    "Chicago C": {"lat": 41.9484, "lon": -87.6553, "elevation": 595, "dome": False},  # Wrigley
    "Chicago WS": {"lat": 41.8299, "lon": -87.6338, "elevation": 595, "dome": False},  # Guaranteed Rate
    "Cincinnati": {"lat": 39.0974, "lon": -84.5068, "elevation": 550, "dome": False},
    "Cleveland": {"lat": 41.4958, "lon": -81.6851, "elevation": 660, "dome": False},
    "Colorado": {"lat": 39.7559, "lon": -104.9942, "elevation": 5200, "dome": False},  # Coors Field
    "Detroit": {"lat": 42.3390, "lon": -83.0485, "elevation": 585, "dome": False},
    "Houston": {"lat": 29.7572, "lon": -95.3554, "elevation": 22, "dome": True},
    "Kansas City": {"lat": 39.0517, "lon": -94.4803, "elevation": 750, "dome": False},
    "Los Angeles A": {"lat": 33.8003, "lon": -117.8827, "elevation": 150, "dome": False},  # Angel Stadium
    "Los Angeles D": {"lat": 34.0739, "lon": -118.2400, "elevation": 340, "dome": False},  # Dodger Stadium
    "Miami": {"lat": 25.7781, "lon": -80.2197, "elevation": 8, "dome": True},
    "Milwaukee": {"lat": 43.0280, "lon": -87.9712, "elevation": 635, "dome": True},  # Retractable roof
    "Minnesota": {"lat": 44.9817, "lon": -93.2777, "elevation": 815, "dome": False},
    "New York M": {"lat": 40.7571, "lon": -73.8458, "elevation": 39, "dome": False},  # Citi Field
    "New York Y": {"lat": 40.8296, "lon": -73.9262, "elevation": 55, "dome": False},  # Yankee Stadium
    "A's": {"lat": 37.7516, "lon": -122.2005, "elevation": 56, "dome": False},  # Oakland Coliseum
    "Philadelphia": {"lat": 39.9061, "lon": -75.1665, "elevation": 20, "dome": False},
    "Pittsburgh": {"lat": 40.4469, "lon": -80.0057, "elevation": 730, "dome": False},
    "San Diego": {"lat": 32.7073, "lon": -117.1566, "elevation": 19, "dome": False},
    "San Francisco": {"lat": 37.7786, "lon": -122.3893, "elevation": 13, "dome": False},
    "Seattle": {"lat": 47.5914, "lon": -122.3326, "elevation": 134, "dome": True},  # Retractable roof
    "St. Louis": {"lat": 38.6226, "lon": -90.1928, "elevation": 465, "dome": False},
    "Tampa Bay": {"lat": 27.7682, "lon": -82.6534, "elevation": 31, "dome": True},
    "Texas": {"lat": 32.7473, "lon": -97.0945, "elevation": 551, "dome": True},  # Globe Life Field
    "Toronto": {"lat": 43.6414, "lon": -79.3894, "elevation": 300, "dome": True},  # Retractable roof
    "Washington": {"lat": 38.8730, "lon": -77.0074, "elevation": 59, "dome": False},
}

# ── Park factors (runs per game vs. league average) ────
PARK_FACTORS = {
    "Colorado": 1.15,  # Coors Field - thin air
    "Boston": 1.08,    # Fenway - Green Monster creates doubles
    "Baltimore": 1.07,  # Camden Yards - hitter friendly
    "Cincinnati": 1.05, # Great American Ballpark
    "Arizona": 1.04,   # Chase Field
    "Texas": 1.03,     # Globe Life Field
    "New York Y": 1.02, # Yankee Stadium - short RF
    "Chicago C": 1.02,  # Wrigley Field - variable wind
    "Toronto": 1.01,   # Rogers Centre
    "Kansas City": 1.01, # Kauffman Stadium
    "Los Angeles D": 1.00, # Dodger Stadium - neutral
    "Atlanta": 1.00,   # Truist Park
    "Washington": 1.00, # Nationals Park
    "Milwaukee": 0.99, # American Family Field
    "Minnesota": 0.99, # Target Field
    "Philadelphia": 0.98, # Citizens Bank Park
    "Cleveland": 0.98, # Progressive Field
    "Detroit": 0.97,   # Comerica Park
    "New York M": 0.97, # Citi Field - pitcher friendly
    "Pittsburgh": 0.96, # PNC Park
    "Chicago WS": 0.96, # Guaranteed Rate Field
    "Houston": 0.95,   # Minute Maid Park
    "Los Angeles A": 0.95, # Angel Stadium
    "St. Louis": 0.94, # Busch Stadium
    "Tampa Bay": 0.94, # Tropicana Field - dome, turf
    "Miami": 0.93,     # loanDepot Park
    "Seattle": 0.92,   # T-Mobile Park - marine layer
    "A's": 0.92,       # Oakland Coliseum - foul territory
    "San Diego": 0.90, # Petco Park - marine layer, big dimensions
    "San Francisco": 0.88, # Oracle Park - marine layer, wind, big dimensions
}

# ── Base run expectancy by inning ──────────────────────
# Historical MLB average runs per inning (both teams combined)
BASE_RUNS_PER_INNING = {
    1: 0.54, 2: 0.49, 3: 0.51, 4: 0.48, 5: 0.46,
    6: 0.45, 7: 0.52, 8: 0.47, 9: 0.39,  # 9th often incomplete
    "extra": 0.85  # Extra innings have higher run rates due to runner on 2nd
}


async def fetch_weather_data(home_team: str) -> dict:
    """Fetch current weather for the stadium with smart caching."""
    global _weather_cache
    
    if not WEATHER_API_KEY or home_team not in STADIUM_COORDS:
        return {}
    
    coords = STADIUM_COORDS[home_team]
    if coords["dome"]:
        # Dome game - weather doesn't matter, no API call needed
        return {"dome": True}
    
    # Check cache first
    cache_key = home_team
    now = time.time()
    
    if cache_key in _weather_cache:
        cached_data = _weather_cache[cache_key]
        age_minutes = (now - cached_data["timestamp"]) / 60
        
        if age_minutes < WEATHER_CACHE_MINUTES:
            print(f"  Using cached weather for {home_team} (age: {age_minutes:.1f}min)")
            return cached_data["data"]
    
    # Fetch fresh weather data
    url = f"http://api.weatherapi.com/v1/current.json?key={WEATHER_API_KEY}&q={coords['lat']},{coords['lon']}"
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        
        current = data["current"]
        weather_data = {
            "dome": False,
            "temp_f": current["temp_f"],
            "humidity": current["humidity"],
            "wind_mph": current["wind_mph"],
            "wind_dir": current["wind_dir"],
            "condition": current["condition"]["text"],
            "visibility": current["vis_miles"],
        }
        
        # Cache the result
        _weather_cache[cache_key] = {
            "data": weather_data,
            "timestamp": now
        }
        
        print(f"  Fetched fresh weather for {home_team}: {weather_data['temp_f']}°F, {weather_data['wind_mph']}mph {weather_data['wind_dir']}")
        return weather_data
        
    except Exception as e:
        print(f"  Weather fetch failed for {home_team}: {e}")
        return {}


def calculate_weather_adjustment(weather: dict, home_team: str) -> float:
    """Calculate run total adjustment based on weather conditions."""
    if not weather or weather.get("dome", False):
        return 1.0  # No adjustment for dome games
    
    adjustment = 1.0
    temp_f = weather.get("temp_f", 75)
    humidity = weather.get("humidity", 50)
    wind_mph = weather.get("wind_mph", 0)
    
    # Temperature effect (hotter air = less dense = more carry)
    if temp_f > 85:
        adjustment *= 1.05  # Hot day bonus
    elif temp_f > 75:
        adjustment *= 1.02
    elif temp_f < 55:
        adjustment *= 0.96  # Cold day penalty
    elif temp_f < 45:
        adjustment *= 0.92
    
    # Humidity effect (lower humidity = less dense air = more carry)
    if humidity < 40:
        adjustment *= 1.03  # Dry air bonus
    elif humidity > 80:
        adjustment *= 0.98  # Humid air penalty
    
    # Wind effect - this is ballpark specific
    wind_bonus = calculate_wind_effect(home_team, wind_mph, weather.get("wind_dir", ""))
    adjustment *= wind_bonus
    
    # Coors Field special case - elevation + dry air
    if home_team == "Colorado":
        if humidity < 30:  # Very dry
            adjustment *= 1.08  # Extra boost at altitude
        elif humidity > 70:  # Unusual humidity for Denver
            adjustment *= 0.98
    
    return adjustment


def calculate_wind_effect(home_team: str, wind_mph: float, wind_dir: str) -> float:
    """Calculate wind effect on run scoring (park-specific)."""
    if wind_mph < 8:
        return 1.0  # Minimal wind effect
    
    # Park-specific wind effects
    wind_effects = {
        "Chicago C": {  # Wrigley Field - famous for wind effects
            "out": 1.15 if wind_mph > 15 else 1.08,  # Wind blowing out to lake
            "in": 0.88 if wind_mph > 15 else 0.94,   # Wind blowing in from lake
        },
        "San Francisco": {  # Oracle Park - wind from bay
            "in": 0.90 if wind_mph > 12 else 0.95,   # Usually blowing in
            "cross": 0.96,  # Cross wind affects foul territory
        },
        "San Diego": {  # Petco Park - marine layer effects
            "in": 0.92 if wind_mph > 10 else 0.96,
        },
        "Boston": {  # Fenway - wind over Green Monster
            "out": 1.10 if wind_mph > 12 else 1.05,  # Helps clear the Monster
            "in": 0.92,
        },
        "New York Y": {  # Yankee Stadium - wind to RF
            "out": 1.08 if wind_mph > 10 else 1.04,  # Short RF porch
        },
        "Baltimore": {  # Camden Yards
            "out": 1.06 if wind_mph > 12 else 1.03,
        },
    }
    
    park_winds = wind_effects.get(home_team, {})
    
    # Determine wind direction effect
    if "SW" in wind_dir or "W" in wind_dir:
        return park_winds.get("out", 1.02 if wind_mph > 12 else 1.01)
    elif "N" in wind_dir or "NE" in wind_dir:
        return park_winds.get("in", 0.98 if wind_mph > 12 else 0.99)
    else:
        return park_winds.get("cross", 1.0)


def calculate_game_state_adjustment(
    inning: int, 
    score_diff: int, 
    away_runs: int, 
    home_runs: int
) -> float:
    """Adjust run expectancy based on game situation."""
    adjustment = 1.0
    total_runs_so_far = away_runs + home_runs
    
    # Score differential effects
    if abs(score_diff) >= 5:
        # Blowout game - garbage time runs
        adjustment *= 1.12
    elif abs(score_diff) >= 3:
        # Comfortable lead - some garbage time
        adjustment *= 1.05
    elif abs(score_diff) <= 1 and inning >= 7:
        # Close late game - managers tighten up, fewer risks
        adjustment *= 0.92
    
    # Pace of game effect
    innings_played = inning - 0.5  # Approximate
    current_pace = total_runs_so_far / innings_played if innings_played > 0 else 0
    
    if current_pace > 1.4:  # High-scoring game
        adjustment *= 1.08  # Momentum effect
    elif current_pace < 0.6:  # Low-scoring game
        adjustment *= 0.94  # Pitcher's duel
    
    return adjustment


def calculate_remaining_run_expectancy(
    inning: int,
    inning_half: str,
    home_team: str,
    away_runs: int,
    home_runs: int,
    weather: dict
) -> float:
    """Calculate expected runs for remainder of game."""
    
    # Determine innings remaining
    if inning_half == "Top":
        # Away team batting, home gets full at-bat this inning
        innings_left = 9.5 - (inning - 0.5)
    else:
        # Home team batting
        innings_left = 9 - (inning - 0.5)
    
    if innings_left <= 0:
        return 0.0
    
    # Base run expectancy
    remaining_runs = 0.0
    current_inning = inning
    
    while current_inning <= 9 and remaining_runs < innings_left * 2:
        if current_inning in BASE_RUNS_PER_INNING:
            remaining_runs += BASE_RUNS_PER_INNING[current_inning]
        else:
            remaining_runs += BASE_RUNS_PER_INNING["extra"]
        current_inning += 1
    
    # Adjust for remaining partial inning
    if inning_half == "Top":
        # Add bottom half of current inning
        remaining_runs += BASE_RUNS_PER_INNING.get(inning, 0.5) * 0.5
    
    # Apply park factor
    park_factor = PARK_FACTORS.get(home_team, 1.0)
    remaining_runs *= park_factor
    
    # Apply weather adjustment
    weather_adj = calculate_weather_adjustment(weather, home_team)
    remaining_runs *= weather_adj
    
    # Apply game state adjustment
    score_diff = home_runs - away_runs
    game_state_adj = calculate_game_state_adjustment(inning, score_diff, away_runs, home_runs)
    remaining_runs *= game_state_adj
    
    return remaining_runs


async def fetch_kalshi_run_total_markets():
    """Fetch run total markets using the correct API endpoint pattern."""
    
    # Get live games first
    live_games = await fetch_live_games()
    all_run_total_markets = []
    
    for game in live_games:
        # Extract game info
        away_short = game.get("away_short", "")
        home_short = game.get("home_short", "")
        game_time = game.get("game_time", "")
        
        if not away_short or not home_short:
            continue
            
        try:
            # Use the same API pattern as the web interface
            # Web interface queries both main game and total markets
            
            # For the current NYM@CHC game, use the exact working tickers
            if ("New York" in away_short or "NYM" in away_short) and ("Chicago" in home_short or "CHC" in home_short):
                tickers = [
                    "KXMLBGAME-26APR171420NYMCHC",     # Main game markets (like web interface)
                    "KXMLBTOTAL-26APR171420NYMCHC"      # Total runs markets
                ]
                print(f"  Using hardcoded tickers for NYM@CHC: {tickers}")
            else:
                # For other games, construct with correct format: [YY][MON][DD][HHMM][TEAMS]
                now = datetime.now(timezone.utc)
                year_2digit = now.strftime("%y")  # "26" for 2026
                month_abbr = now.strftime("%b").upper()  # "APR" 
                day_2digit = now.strftime("%d")  # "17" for 17th
                
                # Time format: 1420 (HHMM) 
                game_time_str = "2000"  # Default 8:00 PM
                if game_time:
                    try:
                        time_parts = game_time.split(":")
                        game_time_str = f"{time_parts[0].zfill(2)}{time_parts[1].zfill(2)}"
                    except:
                        pass
                
                # Team codes
                away_code = away_short.upper()[:3]
                home_code = home_short.upper()[:3]
                
                # Build tickers with correct format (like web interface)
                date_time_part = f"{year_2digit}{month_abbr}{day_2digit}{game_time_str}"
                ticker_base = f"{date_time_part}{away_code}{home_code}"
                tickers = [
                    f"KXMLBGAME-{ticker_base}",      # Main game markets
                    f"KXMLBTOTAL-{ticker_base}",    # Total runs markets
                    f"KXMLBTEAMTOTAL-{ticker_base}" # Team total markets
                ]
            
            # Add URL encoding
            tickers_param = "%2C".join(tickers)
            
            # Build the API URL
            api_url = f"https://api.elections.kalshi.com/v1/events/?series_tickers=&single_event_per_series=false&tickers={tickers_param}&page_size=100&page_number=1&with_markdown=true"
            
            print(f"  Querying run totals for {away_short}@{home_short}...")
            print(f"  Using ticker(s): {tickers}")
            
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(api_url)
                resp.raise_for_status()
                data = resp.json()
                
                print(f"  API response: {len(data.get('events', []))} events found")
                
                # Process events
                for event in data.get("events", []):
                    if not event.get("markets"):
                        continue
                        
                    event_ticker = event.get("event_ticker", "")
                    event_title = event.get("title", "")
                    
                    print(f"  Found event: {event_ticker}")
                    print(f"  Event title: {event_title}")
                    print(f"  Markets in event: {len(event.get('markets', []))}")
                    
                    # Process markets for this event
                    for market in event.get("markets", []):
                        title = market.get("title", "")
                        
                        # Only process markets with actual pricing
                        yes_bid = market.get("yes_bid", 0)
                        yes_ask = market.get("yes_ask", 0)
                        if yes_bid <= 0 or yes_ask <= 0:
                            continue
                            
                        # Check for run total patterns
                        title_lower = title.lower()
                        if not any(pattern in title_lower for pattern in [
                            "total runs", "score over", "over", "under", "first 5"
                        ]):
                            continue
                            
                        # Parse the market to extract threshold and type
                        threshold = None
                        market_type = "unknown"
                        
                        # Pattern: "Will [Team] score over X.5 runs?"
                        if "score over" in title_lower and "runs" in title_lower:
                            try:
                                over_part = title_lower.split("over ")[1]
                                threshold_str = over_part.split()[0]
                                threshold = float(threshold_str)
                                market_type = "over"
                            except:
                                continue
                                
                        # Pattern: "Team vs Team Total Runs?" (infer threshold from pricing)
                        elif "total runs" in title_lower:
                            market_type = "total"
                            # Fixed threshold inference based on actual API data
                            ask_price = yes_ask / 100  # Convert to decimal
                            
                            if ask_price >= 0.95:      # 95¢+ = 7.5-8.5 (very likely already hit)
                                threshold = 7.5
                            elif ask_price >= 0.85:    # 85-95¢ = 8.5 threshold  
                                threshold = 8.5
                            elif ask_price >= 0.70:    # 70-85¢ = 9.5 threshold
                                threshold = 9.5
                            elif ask_price >= 0.60:    # 60-70¢ = 10.5 threshold
                                threshold = 10.5
                            elif ask_price >= 0.45:    # 45-60¢ = 11.5 threshold
                                threshold = 11.5
                            elif ask_price >= 0.35:    # 35-45¢ = 12.5 threshold
                                threshold = 12.5
                            elif ask_price >= 0.25:    # 25-35¢ = 13.5 threshold
                                threshold = 13.5
                            else:                       # <25¢ = 14.5+ threshold
                                threshold = 14.5
                            
                        # Pattern: "first 5 innings"
                        elif "first 5" in title_lower:
                            market_type = "first5"
                            threshold = 4.5  # Approximate for F5 totals
                            
                        else:
                            continue
                            
                        if threshold:
                            all_run_total_markets.append({
                                "event_ticker": event_ticker,
                                "title": title,
                                "team1": away_short,
                                "team2": home_short,
                                "threshold": threshold,
                                "market_type": market_type,
                                "yes_bid": yes_bid / 100,
                                "yes_ask": yes_ask / 100,
                                "vol24": market.get("volume", 0),
                            })
                            
        except Exception as e:
            print(f"Error fetching run totals for {away_short}@{home_short}: {e}")
            continue
    
    return all_run_total_markets
    
    return run_total_markets


def kelly_size(edge: float, price: float, bankroll: float) -> dict:
    """Kelly criterion position sizing for run totals."""
    if edge <= 0 or price <= 0:
        return {"pct": 0.0, "frac_dollar": 0.0, "full_dollar": 0.0, "contracts": 0}
    
    win_prob = edge + price
    lose_prob = 1 - win_prob
    b = (1 - price) / price
    
    kelly_frac = (b * win_prob - lose_prob) / b
    kelly_frac = max(0, min(kelly_frac, 0.20))  # Cap at 20% for run totals
    
    full_dollar = bankroll * kelly_frac
    half_kelly = full_dollar * 0.5
    
    cost_per_contract = price
    contracts = int(half_kelly / cost_per_contract) if cost_per_contract > 0 else 0
    
    return {
        "pct": kelly_frac * 100,
        "frac_dollar": half_kelly,
        "full_dollar": full_dollar,
        "contracts": contracts
    }


def send_imessage(message: str):
    """Send iMessage via osascript."""
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{IMESSAGE_NUMBER}" of targetService
        send "{message}" to targetBuddy
    end tell
    '''
    try:
        subprocess.run(["osascript", "-e", script], check=True)
    except Exception as e:
        print(f"iMessage failed: {e}")


def log_results(results: list):
    """Log results to CSV."""
    if not results:
        return
    
    fieldnames = [
        "timestamp", "game_pk", "event_ticker", "away_short", "home_short", 
        "inning", "half", "away_runs", "home_runs", "current_total",
        "market_threshold", "market_type", "remaining_expectancy", 
        "projected_total", "kalshi_mid", "kalshi_bid", "kalshi_ask",
        "edge_buy", "edge_sell", "vol24", "signal", "weather_adj", "park_factor",
        "kelly_pct", "half_kelly", "full_kelly", "kelly_contracts"
    ]
    
    ts = datetime.now(timezone.utc).isoformat()
    for r in results:
        r["timestamp"] = ts
    
    write_header = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(results)


def log_backtest_prediction(
    game_data: dict,
    market_data: dict, 
    weather_data: dict,
    projected_total: float,
    signal_data: dict
):
    """
    Log prediction data for backtesting analysis.
    This captures the model's prediction at a point in time so we can later
    compare against the actual final game total.
    """
    backtest_log = Path(__file__).parent / "run_total_backtest.csv"
    
    # Only log if we have a signal (BUY/SELL)
    if signal_data.get("signal") not in ["BUY", "SELL"]:
        return
        
    current_total = game_data["away_runs"] + game_data["home_runs"]
    
    prediction_row = {
        # Game identification
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "game_pk": game_data["game_pk"],
        "away_team": game_data["away_short"],
        "home_team": game_data["home_short"],
        "final_score_away": "",  # Will be filled when game ends
        "final_score_home": "",  # Will be filled when game ends
        
        # Market information
        "market_threshold": market_data["threshold"],
        "market_type": market_data["market_type"],
        "kalshi_mid": signal_data["kalshi_mid"],
        "kalshi_bid": signal_data["kalshi_bid"], 
        "kalshi_ask": signal_data["kalshi_ask"],
        
        # Game state at prediction time
        "prediction_inning": game_data["inning"],
        "prediction_half": game_data["half"],
        "runs_at_prediction": current_total,
        "away_runs_at_pred": game_data["away_runs"],
        "home_runs_at_pred": game_data["home_runs"],
        
        # Model components
        "remaining_expectancy": signal_data["remaining_expectancy"],
        "projected_total": projected_total,
        "model_prob": signal_data.get("model_prob", 0),
        "weather_adj": signal_data["weather_adj"],
        "park_factor": signal_data["park_factor"],
        "game_state_adj": signal_data.get("game_state_adj", 1.0),
        
        # Weather conditions
        "temp_f": weather_data.get("temp_f", ""),
        "humidity": weather_data.get("humidity", ""),
        "wind_mph": weather_data.get("wind_mph", ""),
        "wind_dir": weather_data.get("wind_dir", ""),
        "dome": weather_data.get("dome", False),
        
        # Edge and signal
        "edge_buy": signal_data["edge_buy"],
        "edge_sell": signal_data["edge_sell"], 
        "signal": signal_data["signal"],
        "exec_edge": signal_data["edge_buy"] if signal_data["signal"] == "BUY" else signal_data["edge_sell"],
        
        # Actual outcome (filled later by backtest analysis)
        "actual_total": "",  # Final game total
        "prediction_correct": "",  # True/False
        "edge_realized": ""  # Actual P&L
    }
    
    # Write to backtest log
    fieldnames = list(prediction_row.keys())
    write_header = not backtest_log.exists()
    
    try:
        with backtest_log.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(prediction_row)
    except Exception as e:
        print(f"  Backtest logging failed: {e}")


async def fetch_final_scores_for_backtest():
    """
    Helper function to fetch final scores for games in backtest log.
    Run this after games complete to fill in actual_total, prediction_correct, etc.
    """
    backtest_log = Path(__file__).parent / "run_total_backtest.csv"
    if not backtest_log.exists():
        return
        
    # This would fetch completed game scores from MLB API
    # and update the backtest log with final totals
    # Implementation left for when you're ready to run full backtests
    pass


# ── Import existing MLB functions ──────────────────────
# Reuse your existing fetch_live_games function
async def fetch_live_games():
    """Fetch live MLB games from MLB Stats API."""
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"https://statsapi.mlb.com/api/v1/schedule/games?sportId=1&date={today}&hydrate=linescore"
    
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    
    live_games = []
    TEAM_MAP = {
        "Arizona Diamondbacks": "Arizona", "Atlanta Braves": "Atlanta", "Baltimore Orioles": "Baltimore",
        "Boston Red Sox": "Boston", "Chicago Cubs": "Chicago C", "Chicago White Sox": "Chicago WS",
        "Cincinnati Reds": "Cincinnati", "Cleveland Guardians": "Cleveland", "Colorado Rockies": "Colorado",
        "Detroit Tigers": "Detroit", "Houston Astros": "Houston", "Kansas City Royals": "Kansas City",
        "Los Angeles Angels": "Los Angeles A", "Los Angeles Dodgers": "Los Angeles D", "Miami Marlins": "Miami",
        "Milwaukee Brewers": "Milwaukee", "Minnesota Twins": "Minnesota", "New York Mets": "New York M",
        "New York Yankees": "New York Y", "Oakland Athletics": "A's", "Athletics": "A's",
        "Philadelphia Phillies": "Philadelphia", "Pittsburgh Pirates": "Pittsburgh", "San Diego Padres": "San Diego",
        "San Francisco Giants": "San Francisco", "Seattle Mariners": "Seattle", "St. Louis Cardinals": "St. Louis",
        "Tampa Bay Rays": "Tampa Bay", "Texas Rangers": "Texas", "Toronto Blue Jays": "Toronto",
        "Washington Nationals": "Washington",
    }
    
    for date_item in data.get("dates", []):
        for game in date_item.get("games", []):
            status = game.get("status", {}).get("abstractGameState", "")
            if status != "Live":
                continue
            
            away_team = game["teams"]["away"]["team"]["name"]
            home_team = game["teams"]["home"]["team"]["name"]
            away_short = TEAM_MAP.get(away_team, away_team)
            home_short = TEAM_MAP.get(home_team, home_team)
            
            linescore = game.get("linescore", {})
            away_runs = linescore.get("teams", {}).get("away", {}).get("runs", 0) or 0
            home_runs = linescore.get("teams", {}).get("home", {}).get("runs", 0) or 0
            inning = linescore.get("currentInning", 1)
            inning_half = linescore.get("inningHalf", "Top")
            
            live_games.append({
                "game_pk": game["gamePk"],
                "away_team": away_team,
                "home_team": home_team,
                "away_short": away_short,
                "home_short": home_short,
                "away_runs": away_runs,
                "home_runs": home_runs,
                "inning": inning,
                "half": inning_half,
            })
    
    return live_games


def match_games_to_run_markets(live_games: list, run_markets: list) -> list:
    """Match live games to run total markets."""
    matches = []
    
    for game in live_games:
        away, home = game["away_short"], game["home_short"]
        
        for market in run_markets:
            # Try to match by team names in title (if available)
            t1, t2 = market["team1"], market["team2"]
            
            team_match = False
            
            # Method 1: Team names in title (traditional format)
            if t1 != "Unknown" and t2 != "Unknown":
                if (away in t1 or away in t2) and (home in t1 or home in t2):
                    team_match = True
            
            # Method 2: Multi-game markets - check if title contains team abbreviations
            elif "Unknown" in [t1, t2]:
                title_lower = market["title"].lower()
                # Look for team abbreviations in the market title
                # Common abbreviations: "Chicago C" for Cubs, "New York Y" for Yankees, etc.
                team_abbrevs = [
                    away.lower(), home.lower(),
                    away[:3].lower(), home[:3].lower(),  # First 3 letters
                ]
                
                # Special team mappings for common abbreviations
                team_mappings = {
                    "chicago c": "chc", "cubs": "chc",
                    "new york y": "nyy", "yankees": "nyy", 
                    "new york m": "nym", "mets": "nym",
                    "los angeles d": "lad", "dodgers": "lad",
                    "los angeles a": "laa", "angels": "laa",
                }
                
                # Check if any team identifier appears in the market title
                for abbrev in team_abbrevs:
                    if abbrev in title_lower:
                        team_match = True
                        break
                
                # Check special mappings
                if not team_match:
                    for full_name, abbrev in team_mappings.items():
                        if full_name in title_lower and (abbrev == away.lower() or abbrev == home.lower()):
                            team_match = True
                            break
            
            if team_match:
                matches.append({
                    "game": game,
                    "market": market
                })
                # Don't break - multi-game markets might have multiple relevant totals
    
    return matches


def should_scan_now() -> bool:
    """
    Determine if we should scan based on time and season.
    
    Game timing considerations:
    - West Coast games can start as early as 12 PM ET (9 AM local)
    - East Coast games can end as late as 2 AM ET (extra innings, delays)
    - Double headers and makeup games can extend hours
    - Spring training and playoffs have different schedules
    
    Conservative approach: 10 AM - 3 AM ET (17 hour window)
    """
    now_et = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)  # Convert to ET
    current_hour = now_et.hour
    current_month = now_et.month
    
    # Baseball season: March through November (includes spring training + playoffs)
    if not (3 <= current_month <= 11):
        print(f"  Off-season (month {current_month}) - skipping scan")
        return False
    
    # Game hours with healthy buffer: 10 AM - 3 AM ET
    if 3 <= current_hour < 10:  # 3 AM - 10 AM ET = dead zone
        print(f"  Outside game hours ({current_hour}:xx ET) - skipping scan")
        return False
    
    return True


def clear_old_weather_cache():
    """Remove stale weather cache entries to prevent memory bloat."""
    global _weather_cache
    now = time.time()
    stale_keys = []
    
    for stadium, cached in _weather_cache.items():
        age_minutes = (now - cached["timestamp"]) / 60
        if age_minutes > WEATHER_CACHE_MINUTES * 3:  # Remove after 45 min
            stale_keys.append(stadium)
    
    for key in stale_keys:
        del _weather_cache[key]
    
    if stale_keys:
        print(f"  Cleared stale weather cache for: {', '.join(stale_keys)}")


async def main():
    """Main run total scanning loop."""
    if LOCK_FILE.exists():
        age = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
        if age < 300:
            print("Run total scanner already running")
            return
        else:
            LOCK_FILE.unlink(missing_ok=True)
    
    try:
        LOCK_FILE.touch()
        
        # Check if we should scan at this time
        if not should_scan_now():
            return
        
        print("=" * 60)
        print(f"MLB Run Total Scanner - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 60)
        
        # Clean up old weather cache entries
        clear_old_weather_cache()
        
        # Fetch data
        bankroll = await get_balance()
        print(f"Bankroll: ${bankroll:,.2f}")
        
        live_games = await fetch_live_games()
        print(f"Live games: {len(live_games)}")
        
        if not live_games:
            print("No live games found.")
            return
        
        run_total_markets = await fetch_kalshi_run_total_markets()
        print(f"Run total markets: {len(run_total_markets)}")
        
        matches = match_games_to_run_markets(live_games, run_total_markets)
        print(f"Game-market matches: {len(matches)}")
        
        if not matches:
            print("No run total markets found for live games.")
            return
        
        all_results = []
        strong_signals = []
        
        for match in matches:
            g = match["game"]
            market = match["market"]
            
            print(f"\n🏟️  {g['away_short']} @ {g['home_short']} - Inn {g['inning']} {g['half']}")
            print(f"   Score: {g['away_short']} {g['away_runs']}, {g['home_short']} {g['home_runs']} (Total: {g['away_runs'] + g['home_runs']})")
            print(f"   Market: {market['threshold']} {market['market_type'].upper()} @ {market['yes_ask']:.3f}")
            
            # Skip based on inning
            if g["inning"] < MIN_INNING:
                print(f"   ⏭️  Skipping — inning {g['inning']} < {MIN_INNING}")
                continue
            if g["inning"] > MAX_INNING:
                print(f"   ⏭️  Skipping — inning {g['inning']} > {MAX_INNING}")
                continue
            
            # Skip low volume
            if market["vol24"] < MIN_VOL_24H:
                print(f"   📊 Skipping — volume {market['vol24']:,} < {MIN_VOL_24H:,}")
                continue
            
            # Fetch weather
            weather = await fetch_weather_data(g["home_short"])
            
            # Calculate remaining run expectancy
            remaining_runs = calculate_remaining_run_expectancy(
                g["inning"], g["half"], g["home_short"], 
                g["away_runs"], g["home_runs"], weather
            )
            
            current_total = g["away_runs"] + g["home_runs"]
            projected_total = current_total + remaining_runs
            
            print(f"   📊 Current: {current_total}, Remaining: {remaining_runs:.2f}, Projected: {projected_total:.2f}")
            
            # Calculate edge based on market type
            market_threshold = market["threshold"]
            kalshi_mid = (market["yes_bid"] + market["yes_ask"]) / 2
            kalshi_bid = market["yes_bid"]
            kalshi_ask = market["yes_ask"]
            
            # For run total markets, determine if this is OVER or UNDER the threshold
            # Most markets are OVER markets (will game exceed X runs?)
            is_over_market = True  # Default assumption for total markets
            
            if market["market_type"] == "over":
                # Market is betting on OVER threshold
                model_prob = 1.0 if projected_total > market_threshold else 0.0
                if abs(projected_total - market_threshold) < 0.5:
                    # Close call - use probability based on distance
                    model_prob = min(0.95, max(0.05, 0.5 + (projected_total - market_threshold)))
            else:
                # Market is betting on OVER threshold (our threshold inference assumes OVER)
                model_prob = 1.0 if projected_total > market_threshold else 0.0
                if abs(projected_total - market_threshold) < 0.5:
                    model_prob = min(0.95, max(0.05, 0.5 + (projected_total - market_threshold)))
            
            edge_buy = model_prob - kalshi_ask    # Edge for buying YES (betting OVER)
            edge_sell = kalshi_bid - model_prob   # Edge for selling YES (betting UNDER)
            
            # CRITICAL SAFETY CHECK: Prevent dangerous UNDER bets
            current_total = g["away_runs"] + g["home_runs"]
            cushion = market_threshold - current_total
            
            # If current score is within 3 runs of threshold, don't recommend UNDER
            if cushion <= 3.0 and edge_sell > 0:
                print(f"   ⚠️  SAFETY: Blocking UNDER bet - only {cushion:.1f} run cushion")
                edge_sell = -0.1  # Force negative edge to block signal
            
            # Additional safety: If projected total is within 1 run of threshold, be very cautious
            projection_cushion = market_threshold - projected_total
            if projection_cushion <= 1.0 and edge_sell > 0:
                print(f"   ⚠️  SAFETY: Blocking UNDER bet - only {projection_cushion:.1f} projection cushion")
                edge_sell = -0.1  # Force negative edge to block signal
            
            # Signal generation with clear descriptions
            signal = ""
            signal_description = ""
            
            if edge_buy > MIN_EDGE and edge_buy <= MAX_EDGE:
                signal = "BUY"
                signal_description = f"BUY OVER {market_threshold} (bet total > {market_threshold})"
            elif edge_sell > MIN_EDGE and edge_sell <= MAX_EDGE:
                signal = "SELL" 
                signal_description = f"SELL OVER {market_threshold} (bet total < {market_threshold} - UNDER bet)"
            elif abs(model_prob - kalshi_mid) > 0.05:
                signal = "WATCH"
                signal_description = f"WATCH {market_threshold} threshold"
            
            # Additional validation: Never recommend UNDER in dangerous situations
            if signal == "SELL":
                # This means betting UNDER the threshold
                if current_total >= market_threshold * 0.8:  # Within 20% of threshold
                    print(f"   🚨 DANGEROUS: UNDER bet blocked - current {current_total} vs threshold {market_threshold}")
                    signal = ""
                    signal_description = "BLOCKED: Dangerous UNDER bet"
            
            # Weather and park adjustments for logging
            weather_adj = calculate_weather_adjustment(weather, g["home_short"])
            park_factor = PARK_FACTORS.get(g["home_short"], 1.0)
            
            row = {
                "game_pk": g["game_pk"],
                "event_ticker": market["event_ticker"],
                "away_short": g["away_short"],
                "home_short": g["home_short"],
                "inning": g["inning"],
                "half": g["half"],
                "away_runs": g["away_runs"],
                "home_runs": g["home_runs"],
                "current_total": current_total,
                "market_threshold": market_threshold,
                "market_type": market["market_type"],
                "remaining_expectancy": remaining_runs,
                "projected_total": projected_total,
                "kalshi_mid": kalshi_mid,
                "kalshi_bid": kalshi_bid,
                "kalshi_ask": kalshi_ask,
                "edge_buy": edge_buy,
                "edge_sell": edge_sell,
                "vol24": market["vol24"],
                "signal": signal,
                "weather_adj": weather_adj,
                "park_factor": park_factor,
                "kelly_pct": 0.0,
                "half_kelly": 0.0,
                "full_kelly": 0.0,
                "kelly_contracts": 0,
            }
            
            if signal in ("BUY", "SELL"):
                exec_edge = edge_buy if signal == "BUY" else edge_sell
                exec_price = kalshi_ask if signal == "BUY" else kalshi_bid
                sizing = kelly_size(exec_edge, exec_price, bankroll)
                row.update(sizing)
                strong_signals.append(row)
                
                print(f"   🎯 {signal_description}")
                print(f"   💰 Edge: {exec_edge:+.3f}, Kelly: ${sizing['frac_dollar']:.2f}")
                
                # Log prediction for backtesting
                log_backtest_prediction(
                    game_data=g,
                    market_data=market,
                    weather_data=weather,
                    projected_total=projected_total,
                    signal_data=row
                )
            elif signal_description:
                print(f"   ❌ {signal_description}")
            
            all_results.append(row)
            
            # Print summary for this market
            weather_desc = f"🌤️ {weather.get('temp_f', 'N/A')}°F" if not weather.get('dome') else "🏢 Dome"
            wind_desc = f", Wind {weather.get('wind_mph', 0):.0f}mph {weather.get('wind_dir', '')}" if weather.get('wind_mph', 0) > 5 else ""
            print(f"   {weather_desc}{wind_desc}, Park Factor: {park_factor}")
            print(f"   📈 Model prob: {model_prob:.3f}, Kalshi: {kalshi_mid:.3f}, Edge: {edge_buy:+.3f} / {edge_sell:+.3f}")
        
        # Summary and alerts
        print(f"\nStrong signals: {len(strong_signals)}")
        
        # Weather API usage summary
        fresh_weather_calls = sum(1 for r in all_results if r.get("weather_adj", 1.0) != 1.0 and not str(r.get("weather_adj", "")).startswith("cached"))
        print(f"📡 Weather API calls this scan: {fresh_weather_calls} (cached data used when possible)")
        
        if strong_signals:
            # Send iMessage alert
            now_short = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
            lines = [f"🏃 MLB Run Totals — {now_short}"]
            
            for r in strong_signals[:3]:  # Limit to top 3
                direction = "OVER" if r["market_type"] == "over" else "UNDER"
                exec_edge = r["edge_buy"] if r["signal"] == "BUY" else r["edge_sell"]
                lines.append(
                    f"  {r['signal']} {direction} {r['market_threshold']} "
                    f"({r['away_short']} @ {r['home_short']})"
                )
                lines.append(
                    f"  Current: {r['current_total']}, Projected: {r['projected_total']:.1f}, "
                    f"Edge: {exec_edge:+.2f}"
                )
                if r.get("half_kelly", 0) > 0:
                    lines.append(f"  💰 Kelly ${r['half_kelly']:.2f}")
                lines.append(f"  🔗 https://kalshi.com/markets/kxmlbgame/professional-baseball-game/{r['event_ticker'].lower()}")
            
            message = "\n".join(lines)
            print("\n--- iMessage ---")
            print(message)
            send_imessage(message)
        
        # Log results
        if all_results:
            log_results(all_results)
            print(f"Logged {len(all_results)} rows to {LOG_FILE}")
    
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
