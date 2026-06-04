#!/usr/bin/env python3
"""
run_total_backtest.py

Backtesting framework for the MLB run total scanner.
Analyzes historical predictions vs actual results to validate:
- Weather impact models
- Park factor accuracy 
- Game state adjustments
- Edge threshold effectiveness

Usage:
    python scripts/run_total_backtest.py --days 30
    python scripts/run_total_backtest.py --analyze-only  # Just analyze existing data
"""

import asyncio
import argparse
import csv
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import your scanner functions
from scripts.mlb_run_total_scanner import (
    fetch_live_games, fetch_kalshi_run_total_markets, match_games_to_run_markets,
    calculate_remaining_run_expectancy, fetch_weather_data, PARK_FACTORS,
    calculate_weather_adjustment, calculate_game_state_adjustment
)

BACKTEST_LOG = Path(__file__).parent / "run_total_backtest.csv"
ANALYSIS_OUTPUT = Path(__file__).parent / "run_total_analysis.json"


class RunTotalBacktester:
    def __init__(self):
        self.predictions = []
        self.results = []
        
    async def collect_prediction_data(self, days_back: int = 7):
        """
        Collect prediction data for recent games.
        Note: This is a simplified version - in reality you'd need historical 
        game state data from each inning to recreate the exact predictions.
        """
        print(f"🔍 Collecting prediction data for last {days_back} days...")
        
        # For now, we'll set up the logging structure
        # In practice, you'd run this during live games to capture real predictions
        fieldnames = [
            # Game identification
            "timestamp", "game_pk", "away_team", "home_team", "final_score_away", "final_score_home",
            
            # Market information
            "market_threshold", "market_type", "kalshi_mid", "kalshi_bid", "kalshi_ask",
            
            # Game state at prediction time
            "prediction_inning", "prediction_half", "runs_at_prediction", 
            "away_runs_at_pred", "home_runs_at_pred",
            
            # Model components
            "remaining_expectancy", "projected_total", "model_prob", 
            "weather_adj", "park_factor", "game_state_adj",
            
            # Weather conditions
            "temp_f", "humidity", "wind_mph", "wind_dir", "dome",
            
            # Edge and signal
            "edge_buy", "edge_sell", "signal", "exec_edge",
            
            # Actual outcome
            "actual_total", "prediction_correct", "edge_realized"
        ]
        
        # Create CSV with proper headers if it doesn't exist
        if not BACKTEST_LOG.exists():
            with BACKTEST_LOG.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
            print(f"📊 Created backtest log: {BACKTEST_LOG}")
            print("💡 Run your live scanner to start collecting prediction data!")
            return
        
        print(f"📊 Backtest log exists: {BACKTEST_LOG}")
        return fieldnames

    def analyze_results(self):
        """Analyze collected backtest data and generate insights."""
        if not BACKTEST_LOG.exists():
            print("❌ No backtest data found. Run collection first.")
            return
        
        print("📈 Analyzing backtest results...")
        
        # Load data
        df = pd.read_csv(BACKTEST_LOG)
        
        if len(df) == 0:
            print("❌ No data in backtest log yet.")
            return
            
        print(f"📊 Loaded {len(df)} prediction records")
        
        analysis = {}
        
        # Overall accuracy
        total_predictions = len(df[df['signal'].isin(['BUY', 'SELL'])])
        if total_predictions > 0:
            correct_predictions = len(df[df['prediction_correct'] == True])
            overall_accuracy = correct_predictions / total_predictions * 100
            analysis['overall'] = {
                'total_signals': total_predictions,
                'correct': correct_predictions,
                'accuracy_pct': round(overall_accuracy, 1)
            }
        
        # Weather impact analysis
        analysis['weather'] = self._analyze_weather_impact(df)
        
        # Park factor analysis  
        analysis['park_factors'] = self._analyze_park_factors(df)
        
        # Game state analysis
        analysis['game_states'] = self._analyze_game_states(df)
        
        # Edge bucket analysis
        analysis['edge_buckets'] = self._analyze_edge_buckets(df)
        
        # Model component accuracy
        analysis['model_components'] = self._analyze_model_components(df)
        
        # Save analysis
        with ANALYSIS_OUTPUT.open("w") as f:
            json.dump(analysis, f, indent=2)
        
        # Print summary
        self._print_analysis_summary(analysis)
        
        return analysis

    def _analyze_weather_impact(self, df):
        """Analyze how weather conditions affect prediction accuracy."""
        weather_analysis = {}
        
        # Temperature buckets
        if 'temp_f' in df.columns and len(df) > 10:
            df['temp_bucket'] = pd.cut(df['temp_f'], 
                                     bins=[0, 60, 75, 85, 100], 
                                     labels=['Cold (<60°F)', 'Cool (60-75°F)', 'Warm (75-85°F)', 'Hot (>85°F)'])
            
            temp_stats = df.groupby('temp_bucket').agg({
                'prediction_correct': ['count', 'sum', 'mean'],
                'actual_total': 'mean',
                'projected_total': 'mean'
            }).round(3)
            
            weather_analysis['temperature'] = temp_stats.to_dict()
        
        # Wind impact
        if 'wind_mph' in df.columns:
            df['wind_bucket'] = pd.cut(df['wind_mph'],
                                     bins=[0, 8, 15, 50],
                                     labels=['Calm (<8mph)', 'Moderate (8-15mph)', 'Strong (>15mph)'])
            
            wind_stats = df.groupby('wind_bucket').agg({
                'prediction_correct': ['count', 'sum', 'mean'],
                'actual_total': 'mean',
                'projected_total': 'mean'
            }).round(3)
            
            weather_analysis['wind'] = wind_stats.to_dict()
        
        return weather_analysis

    def _analyze_park_factors(self, df):
        """Analyze park factor accuracy."""
        park_analysis = {}
        
        if 'home_team' in df.columns and len(df) > 5:
            park_stats = df.groupby('home_team').agg({
                'prediction_correct': ['count', 'sum', 'mean'],
                'actual_total': 'mean',
                'projected_total': 'mean',
                'park_factor': 'first'  # Park factor is constant per team
            }).round(3)
            
            park_analysis['by_stadium'] = park_stats.to_dict()
            
            # Compare predicted vs actual park effects
            if len(df) > 20:
                for stadium in df['home_team'].unique()[:10]:  # Top 10 stadiums
                    stadium_games = df[df['home_team'] == stadium]
                    if len(stadium_games) >= 3:
                        avg_actual = stadium_games['actual_total'].mean()
                        avg_projected = stadium_games['projected_total'].mean()
                        park_factor_used = stadium_games['park_factor'].iloc[0]
                        
                        park_analysis[f'stadium_{stadium}'] = {
                            'games': len(stadium_games),
                            'avg_actual_total': round(avg_actual, 2),
                            'avg_projected_total': round(avg_projected, 2),
                            'park_factor_used': park_factor_used,
                            'projection_bias': round(avg_projected - avg_actual, 2)
                        }
        
        return park_analysis

    def _analyze_game_states(self, df):
        """Analyze game state adjustment accuracy."""
        game_state_analysis = {}
        
        if 'prediction_inning' in df.columns and 'runs_at_prediction' in df.columns:
            # Inning analysis
            inning_stats = df.groupby('prediction_inning').agg({
                'prediction_correct': ['count', 'sum', 'mean'],
                'actual_total': 'mean',
                'projected_total': 'mean'
            }).round(3)
            
            game_state_analysis['by_inning'] = inning_stats.to_dict()
            
            # Score differential at prediction time
            if 'away_runs_at_pred' in df.columns and 'home_runs_at_pred' in df.columns:
                df['score_diff_at_pred'] = abs(df['home_runs_at_pred'] - df['away_runs_at_pred'])
                df['score_diff_bucket'] = pd.cut(df['score_diff_at_pred'],
                                                bins=[0, 1, 3, 5, 20],
                                                labels=['Tied/Close (0-1)', 'Moderate (2-3)', 'Large (4-5)', 'Blowout (6+)'])
                
                diff_stats = df.groupby('score_diff_bucket').agg({
                    'prediction_correct': ['count', 'sum', 'mean'],
                    'actual_total': 'mean',
                    'projected_total': 'mean'
                }).round(3)
                
                game_state_analysis['by_score_diff'] = diff_stats.to_dict()
        
        return game_state_analysis

    def _analyze_edge_buckets(self, df):
        """Analyze accuracy by edge bucket (similar to your MLB backtest)."""
        edge_analysis = {}
        
        signals_only = df[df['signal'].isin(['BUY', 'SELL'])]
        
        if len(signals_only) > 0 and 'exec_edge' in df.columns:
            signals_only['edge_bucket'] = pd.cut(signals_only['exec_edge'].abs(),
                                                bins=[0, 0.08, 0.12, 0.16, 0.20, 1.0],
                                                labels=['<8¢', '8-12¢', '12-16¢', '16-20¢', '>20¢'])
            
            edge_stats = signals_only.groupby('edge_bucket').agg({
                'prediction_correct': ['count', 'sum', 'mean'],
                'exec_edge': 'mean'
            }).round(3)
            
            edge_analysis['by_edge_bucket'] = edge_stats.to_dict()
        
        return edge_analysis

    def _analyze_model_components(self, df):
        """Analyze individual model component accuracy."""
        component_analysis = {}
        
        if len(df) > 10:
            # Weather adjustment impact
            if 'weather_adj' in df.columns:
                df['weather_boost'] = df['weather_adj'] > 1.02  # 2%+ boost
                weather_boost_stats = df.groupby('weather_boost').agg({
                    'prediction_correct': ['count', 'sum', 'mean'],
                    'actual_total': 'mean',
                    'projected_total': 'mean'
                }).round(3)
                component_analysis['weather_boost'] = weather_boost_stats.to_dict()
            
            # Game state adjustment impact
            if 'game_state_adj' in df.columns:
                df['game_state_boost'] = df['game_state_adj'] > 1.02  # 2%+ boost
                game_state_stats = df.groupby('game_state_boost').agg({
                    'prediction_correct': ['count', 'sum', 'mean'],
                    'actual_total': 'mean',
                    'projected_total': 'mean'  
                }).round(3)
                component_analysis['game_state_boost'] = game_state_stats.to_dict()
        
        return component_analysis

    def _print_analysis_summary(self, analysis):
        """Print a readable summary of the analysis."""
        print("\n" + "="*60)
        print("📊 RUN TOTAL BACKTEST ANALYSIS SUMMARY")
        print("="*60)
        
        # Overall performance
        if 'overall' in analysis:
            overall = analysis['overall']
            print(f"\n🎯 OVERALL PERFORMANCE:")
            print(f"   Total signals: {overall['total_signals']}")
            print(f"   Accuracy: {overall['accuracy_pct']}%")
        
        # Edge bucket performance (most important)
        if 'edge_buckets' in analysis and 'by_edge_bucket' in analysis['edge_buckets']:
            print(f"\n📈 EDGE BUCKET PERFORMANCE:")
            edge_data = analysis['edge_buckets']['by_edge_bucket']
            for bucket in edge_data.get('prediction_correct', {}):
                count = edge_data['prediction_correct'][bucket]['count']
                correct = edge_data['prediction_correct'][bucket]['sum'] 
                accuracy = edge_data['prediction_correct'][bucket]['mean'] * 100
                print(f"   {bucket}: {correct}/{count} = {accuracy:.1f}%")
        
        # Weather insights
        if 'weather' in analysis and 'temperature' in analysis['weather']:
            print(f"\n🌡️  TEMPERATURE IMPACT:")
            temp_data = analysis['weather']['temperature']
            for bucket in temp_data.get('prediction_correct', {}):
                count = temp_data['prediction_correct'][bucket]['count']
                if count > 0:
                    accuracy = temp_data['prediction_correct'][bucket]['mean'] * 100
                    avg_total = temp_data['actual_total'][bucket]['mean']
                    print(f"   {bucket}: {accuracy:.1f}% accuracy, {avg_total:.1f} avg runs")
        
        # Park factor insights  
        if 'park_factors' in analysis:
            print(f"\n🏟️  PARK FACTOR INSIGHTS:")
            for stadium, data in analysis['park_factors'].items():
                if stadium.startswith('stadium_'):
                    stadium_name = stadium.replace('stadium_', '')
                    bias = data['projection_bias']
                    games = data['games']
                    if games >= 3:
                        if abs(bias) > 0.5:
                            direction = "over-estimates" if bias > 0 else "under-estimates"
                            print(f"   {stadium_name}: {direction} by {abs(bias):.1f} runs ({games} games)")
        
        print(f"\n📄 Full analysis saved to: {ANALYSIS_OUTPUT}")
        print("\n💡 NEXT STEPS:")
        print("   1. Review edge bucket accuracy (aim for >55% in 8-12¢ range)")
        print("   2. Calibrate park factors for biased stadiums")
        print("   3. Adjust weather impact coefficients if needed")
        print("   4. Consider filtering out low-accuracy edge buckets")


async def main():
    parser = argparse.ArgumentParser(description="Backtest the run total scanner")
    parser.add_argument("--days", type=int, default=7, help="Days of data to collect")
    parser.add_argument("--analyze-only", action="store_true", help="Only analyze existing data")
    parser.add_argument("--collect", action="store_true", help="Collect prediction data")
    
    args = parser.parse_args()
    
    backtester = RunTotalBacktester()
    
    if args.analyze_only:
        backtester.analyze_results()
    elif args.collect:
        await backtester.collect_prediction_data(args.days)
    else:
        # Default: try to analyze, show collection setup if no data
        if BACKTEST_LOG.exists() and BACKTEST_LOG.stat().st_size > 100:
            backtester.analyze_results()
        else:
            await backtester.collect_prediction_data(args.days)


if __name__ == "__main__":
    asyncio.run(main())
