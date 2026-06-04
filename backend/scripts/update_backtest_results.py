#!/usr/bin/env python3
"""
update_backtest_results.py

Fetches final scores for games in the backtest log and updates prediction accuracy.
Run this after games complete to see how accurate your run total predictions were.

Usage:
    python scripts/update_backtest_results.py --days 1  # Update games from last day
    python scripts/update_backtest_results.py --all     # Update all incomplete predictions
"""

import asyncio
import argparse
import csv
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

BACKTEST_LOG = Path(__file__).parent / "run_total_backtest.csv"


async def fetch_completed_games(start_date: str, end_date: str = None) -> dict:
    """Fetch completed MLB games and their final scores."""
    if end_date is None:
        end_date = start_date
    
    url = f"https://statsapi.mlb.com/api/v1/schedule/games?sportId=1&startDate={start_date}&endDate={end_date}&hydrate=linescore,decisions"
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        
        completed_games = {}
        
        for date_item in data.get("dates", []):
            for game in date_item.get("games", []):
                status = game.get("status", {}).get("abstractGameState", "")
                if status != "Final":
                    continue
                
                game_pk = game["gamePk"]
                linescore = game.get("linescore", {})
                
                away_runs = linescore.get("teams", {}).get("away", {}).get("runs", 0) or 0
                home_runs = linescore.get("teams", {}).get("home", {}).get("runs", 0) or 0
                total_runs = away_runs + home_runs
                
                completed_games[game_pk] = {
                    "away_runs": away_runs,
                    "home_runs": home_runs, 
                    "total_runs": total_runs,
                    "away_team": game["teams"]["away"]["team"]["name"],
                    "home_team": game["teams"]["home"]["team"]["name"],
                    "game_date": game.get("gameDate", ""),
                }
        
        return completed_games
        
    except Exception as e:
        print(f"Error fetching completed games: {e}")
        return {}


def update_backtest_predictions(completed_games: dict):
    """Update backtest log with final scores and calculate accuracy."""
    if not BACKTEST_LOG.exists():
        print("❌ No backtest log found")
        return
    
    # Load backtest data
    df = pd.read_csv(BACKTEST_LOG)
    updated_count = 0
    
    for idx, row in df.iterrows():
        game_pk = row['game_pk']
        
        # Skip if already has final score
        if pd.notna(row.get('actual_total', '')) and row.get('actual_total', '') != '':
            continue
            
        # Check if we have the final score
        if game_pk in completed_games:
            final_data = completed_games[game_pk]
            
            # Update final scores
            df.at[idx, 'final_score_away'] = final_data['away_runs']
            df.at[idx, 'final_score_home'] = final_data['home_runs']
            df.at[idx, 'actual_total'] = final_data['total_runs']
            
            # Calculate prediction accuracy
            projected_total = row['projected_total']
            actual_total = final_data['total_runs']
            market_threshold = row['market_threshold']
            market_type = row['market_type']
            signal = row['signal']
            
            # Determine if prediction was correct
            if market_type == "over":
                # Market bet: total will be OVER threshold
                model_prediction = projected_total > market_threshold
                actual_outcome = actual_total > market_threshold
                market_won = actual_outcome
                
                if signal == "BUY":
                    # We agreed with market (bet over)
                    prediction_correct = model_prediction and actual_outcome
                else:
                    # We disagreed with market (bet under) 
                    prediction_correct = not model_prediction and not actual_outcome
            else:
                # Market bet: total will be UNDER threshold  
                model_prediction = projected_total < market_threshold
                actual_outcome = actual_total < market_threshold
                market_won = actual_outcome
                
                if signal == "BUY":
                    # We agreed with market (bet under)
                    prediction_correct = model_prediction and actual_outcome
                else:
                    # We disagreed with market (bet over)
                    prediction_correct = not model_prediction and not actual_outcome
            
            df.at[idx, 'prediction_correct'] = prediction_correct
            
            # Calculate edge realized (simplified)
            exec_edge = row['exec_edge']
            if prediction_correct:
                edge_realized = abs(exec_edge)  # Won the edge
            else:
                edge_realized = -abs(exec_edge)  # Lost the edge
            
            df.at[idx, 'edge_realized'] = edge_realized
            
            updated_count += 1
            
            print(f"✅ {final_data['away_team']} @ {final_data['home_team']}: "
                  f"Projected {projected_total:.1f}, Actual {actual_total}, "
                  f"{'✓' if prediction_correct else '✗'}")
    
    # Save updated data
    if updated_count > 0:
        df.to_csv(BACKTEST_LOG, index=False)
        print(f"\n📊 Updated {updated_count} predictions in {BACKTEST_LOG}")
        
        # Show quick summary
        completed_predictions = df[df['prediction_correct'].notna()]
        if len(completed_predictions) > 0:
            accuracy = completed_predictions['prediction_correct'].mean() * 100
            total_edge = completed_predictions['edge_realized'].sum()
            print(f"🎯 Current accuracy: {accuracy:.1f}% ({len(completed_predictions)} games)")
            print(f"💰 Cumulative edge: {total_edge:+.3f}")
    else:
        print("📊 No new completions to update")


async def main():
    parser = argparse.ArgumentParser(description="Update backtest results with final scores")
    parser.add_argument("--days", type=int, default=1, help="Days back to check for completed games")
    parser.add_argument("--all", action="store_true", help="Update all incomplete predictions")
    
    args = parser.parse_args()
    
    if args.all:
        # Update all incomplete predictions (check last 30 days)
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)
        date_range = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") 
                     for i in range(31)]
    else:
        # Check specific number of days back
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=args.days)
        date_range = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") 
                     for i in range(args.days + 1)]
    
    print(f"🔍 Checking for completed games from {date_range[0]} to {date_range[-1]}")
    
    all_completed_games = {}
    
    # Fetch completed games for each date
    for date_str in date_range:
        completed_games = await fetch_completed_games(date_str)
        all_completed_games.update(completed_games)
        
        if completed_games:
            print(f"📅 {date_str}: {len(completed_games)} completed games")
    
    if all_completed_games:
        print(f"\n📊 Total completed games found: {len(all_completed_games)}")
        update_backtest_predictions(all_completed_games)
    else:
        print("❌ No completed games found for the specified date range")


if __name__ == "__main__":
    asyncio.run(main())
