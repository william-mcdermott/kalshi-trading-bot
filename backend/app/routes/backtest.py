# app/routes/backtest.py
import logging
import pandas as pd
import ccxt

from fastapi import APIRouter

from backtesting.backtest import run_backtest

router = APIRouter()
log    = logging.getLogger(__name__)


@router.get("/run")
async def run_backtest_endpoint(days: int = 30):
    """
    Fetches fresh BTC candles from Kraken and runs the full parameter sweep.
    Returns results for all param combos + the best config details.
    """
    try:
        exchange = ccxt.kraken()
        since    = exchange.parse8601(
            (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days))
            .strftime('%Y-%m-%dT%H:%M:%SZ')
        )
        candles = exchange.fetch_ohlcv('BTC/USDT', '1h', since=since, limit=days * 24)
        log.info(f"Fetched {len(candles)} candles for backtest")
    except Exception as e:
        return {"error": f"Failed to fetch candles: {e}"}

    sweep   = []
    best    = None
    best_pnl = float('-inf')

    for min_range in [0, 500, 1000, 1500, 2000]:
        for min_mom in [0.2, 0.3, 0.5]:
            r  = run_backtest(
                days=days, min_edge=0.08,
                min_momentum=min_mom,
                min_daily_range=min_range,
                candles=candles,
            )
            pf = round(r.profit_factor, 2) if r.profit_factor != float('inf') else None
            row = {
                "min_range":    min_range,
                "min_momentum": min_mom,
                "trades":       r.total_trades,
                "win_rate":     round(r.win_rate, 1),
                "pnl":          round(r.total_pnl, 4),
                "profit_factor": pf,
                "max_drawdown": round(r.max_drawdown, 4),
            }
            sweep.append(row)

            if r.total_trades >= 5 and r.total_pnl > best_pnl:
                best_pnl = r.total_pnl
                best     = {
                    "params": row,
                    "trades": [
                        {
                            "timestamp":    t.timestamp.strftime("%m-%d %H:%M"),
                            "strategy":     t.strategy,
                            "side":         t.side,
                            "entry_price":  t.entry_price,
                            "fair_value":   t.fair_val,
                            "edge":         round(t.edge, 4),
                            "momentum":     round(t.momentum, 3),
                            "daily_range":  round(t.daily_range, 0),
                            "pnl":          t.pnl,
                            "resolved_yes": t.resolved_yes,
                        }
                        for t in r.trades
                    ],
                    "arb_trades":      len(r.arb_trades),
                    "momentum_trades": len(r.momentum_trades),
                    "arb_pnl":        round(sum(t.pnl for t in r.arb_trades), 4),
                    "momentum_pnl":   round(sum(t.pnl for t in r.momentum_trades), 4),
                }

    return {
        "days":   days,
        "candles": len(candles),
        "sweep":  sweep,
        "best":   best,
    }