import sys
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime

import ccxt
import pandas as pd


TARGET_DISTANCE  = 800.0
MIN_MOMENTUM_PCT = 0.2
MAX_BUY_PRICE    = 0.40
MIN_SELL_PRICE   = 0.15
POSITION_SIZE    = 1.0
CANDLES_PER_HOUR = 1
LOOKBACK         = 12


def estimate_contract_price(
    btc_price: float,
    threshold: float,
    hours_to_settlement: float,
    btc_volatility_pct: float = 2.5,
) -> float:
    if hours_to_settlement <= 0:
        return 1.0 if btc_price >= threshold else 0.0
    distance_pct = (btc_price - threshold) / threshold * 100
    total_vol    = btc_volatility_pct * math.sqrt(hours_to_settlement)
    z            = distance_pct / total_vol

    def normal_cdf(x):
        t    = 1 / (1 + 0.2316419 * abs(x))
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
               + t * (-1.821255978 + t * 1.330274429))))
        p    = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-x * x / 2) * poly
        return p if x >= 0 else 1 - p

    return round(normal_cdf(z), 4)


def calculate_momentum(prices: list[float]) -> float:
    if len(prices) < 2:
        return 0.0
    n      = len(prices)
    mean_x = (n - 1) / 2
    mean_y = statistics.mean(prices)
    numerator   = sum((i - mean_x) * (prices[i] - mean_y) for i in range(n))
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    if denominator == 0:
        return 0.0
    return (numerator / denominator * CANDLES_PER_HOUR / prices[0]) * 100


@dataclass
class SimulatedTrade:
    timestamp:         datetime
    side:              str
    entry_price:       float
    threshold:         float
    btc_at_entry:      float
    btc_at_settlement: float
    resolved_yes:      bool
    pnl:               float
    momentum:          float


@dataclass
class BacktestResults:
    trades:     list[SimulatedTrade] = field(default_factory=list)
    total_pnl:  float = 0.0
    win_count:  int   = 0
    loss_count: int   = 0

    @property
    def total_trades(self): return len(self.trades)

    @property
    def win_rate(self):
        if not self.trades: return 0.0
        return self.win_count / len(self.trades) * 100

    @property
    def profit_factor(self):
        wins   = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = sum(abs(t.pnl) for t in self.trades if t.pnl < 0)
        return wins / losses if losses > 0 else float('inf')

    @property
    def max_drawdown(self):
        if not self.trades: return 0.0
        cumulative = peak = max_dd = 0.0
        for t in self.trades:
            cumulative += t.pnl
            peak        = max(peak, cumulative)
            max_dd      = max(max_dd, peak - cumulative)
        return max_dd


def run_backtest(
    days: int = 30,
    min_momentum: float = MIN_MOMENTUM_PCT,
    candles: list = None,
) -> BacktestResults:
    """
    Runs the backtest on pre-fetched candles.
    Always pass candles in — never fetches internally.
    """
    df       = pd.DataFrame(candles, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['dt'] = pd.to_datetime(df['ts'], unit='ms', utc=True)

    results       = BacktestResults()
    open_position = None

    for i in range(LOOKBACK, len(df)):
        row       = df.iloc[i]
        btc_price = float(row['close'])
        dt        = row['dt']

        settlement_today = dt.replace(hour=21, minute=0, second=0, microsecond=0)
        if dt >= settlement_today:
            settlement_today += pd.Timedelta(days=1)
        hours_to_settlement = (settlement_today - dt).total_seconds() / 3600

        prices   = df['close'].iloc[i - LOOKBACK:i].tolist()
        momentum = calculate_momentum(prices)

        bullish   = momentum > 0
        target    = btc_price + TARGET_DISTANCE if bullish else btc_price - TARGET_DISTANCE
        threshold = round(target / 250) * 250

        contract_price = estimate_contract_price(btc_price, threshold, hours_to_settlement)

        if contract_price >= 0.95 or contract_price <= 0.05:
            continue

        # Close open position at start of new settlement day
        if open_position and hours_to_settlement > 23.5:
            resolved_yes = open_position['btc_settlement'] >= open_position['threshold']
            if open_position['side'] == 'BUY':
                pnl = (1.0 - open_position['entry_price']) if resolved_yes else -open_position['entry_price']
            else:
                pnl = -open_position['entry_price'] if resolved_yes else (1.0 - open_position['entry_price'])

            trade = SimulatedTrade(
                timestamp         = open_position['timestamp'],
                side              = open_position['side'],
                entry_price       = open_position['entry_price'],
                threshold         = open_position['threshold'],
                btc_at_entry      = open_position['btc_entry'],
                btc_at_settlement = open_position['btc_settlement'],
                resolved_yes      = resolved_yes,
                pnl               = round(pnl * POSITION_SIZE, 4),
                momentum          = open_position['momentum'],
            )
            results.trades.append(trade)
            results.total_pnl += trade.pnl
            if trade.pnl > 0:
                results.win_count += 1
            else:
                results.loss_count += 1
            open_position = None

        if open_position:
            open_position['btc_settlement'] = btc_price
            continue

        if momentum > min_momentum and contract_price < MAX_BUY_PRICE:
            side = 'BUY'
        elif momentum < -min_momentum and contract_price > MIN_SELL_PRICE:
            side = 'SELL'
        else:
            continue

        open_position = {
            'timestamp':      dt,
            'side':           side,
            'entry_price':    contract_price,
            'threshold':      threshold,
            'btc_entry':      btc_price,
            'btc_settlement': btc_price,
            'momentum':       momentum,
        }

    return results


def print_results(results: BacktestResults, min_momentum: float = None):
    label = f" (momentum>{min_momentum}%/hr)" if min_momentum else ""
    print(f"\n{'='*50}")
    print(f"BACKTEST RESULTS{label}")
    print(f"{'='*50}")
    print(f"Total trades:    {results.total_trades}")
    print(f"Win rate:        {results.win_rate:.1f}%")
    print(f"Total P&L:       ${results.total_pnl:.4f}")
    pf = f"{results.profit_factor:.2f}" if results.profit_factor != float('inf') else "∞"
    print(f"Profit factor:   {pf}")
    print(f"Max drawdown:    ${results.max_drawdown:.4f}")
    if results.total_trades:
        print(f"Avg P&L/trade:   ${results.total_pnl / results.total_trades:.4f}")
    if results.trades:
        print()
        print("Last 10 trades:")
        print(f"{'Time':<22} {'Side':<6} {'Entry':<8} {'Threshold':<12} {'Momentum':<12} {'P&L'}")
        print("-" * 75)
        for t in results.trades[-10:]:
            print(
                f"{t.timestamp.strftime('%Y-%m-%d %H:%M'):<22} "
                f"{t.side:<6} "
                f"{t.entry_price:<8.3f} "
                f"${t.threshold:<11,.0f} "
                f"{t.momentum:+.2f}%/hr    "
                f"{'+'if t.pnl>0 else ''}{t.pnl:.4f}"
            )
    print("=" * 50)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    # Fetch candles ONCE — reuse for all parameter combinations
    print(f"Fetching {days} days of BTC/USDT 1-hour candles from Kraken...")
    exchange    = ccxt.kraken()
    since       = exchange.parse8601(
        (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days))
        .strftime('%Y-%m-%dT%H:%M:%SZ')
    )
    all_candles = exchange.fetch_ohlcv('BTC/USDT', '1h', since=since, limit=720)
    print(f"Got {len(all_candles)} candles ({len(all_candles)/24:.1f} days)\n")

    print(f"Parameter sweep — MIN_MOMENTUM_PCT ({days} days)\n")
    print(f"{'Momentum':<12} {'Trades':<8} {'Win%':<8} {'P&L':<10} {'PF':<8} {'MaxDD'}")
    print("-" * 55)

    best_pnl = float('-inf')
    best_mom = None
    best_r   = None

    for min_mom in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0, 1.5, 2.0]:
        r  = run_backtest(days=days, min_momentum=min_mom, candles=all_candles)
        pf = f"{r.profit_factor:.2f}" if r.profit_factor != float('inf') else "∞"
        print(
            f"{min_mom:<12.1f} "
            f"{r.total_trades:<8} "
            f"{r.win_rate:<8.1f} "
            f"${r.total_pnl:<9.4f} "
            f"{pf:<8} "
            f"${r.max_drawdown:.4f}"
        )
        if r.total_trades >= 3 and r.total_pnl > best_pnl:
            best_pnl = r.total_pnl
            best_mom = min_mom
            best_r   = r

    if best_r:
        print(f"\nBest parameter: MIN_MOMENTUM_PCT = {best_mom}")
        print_results(best_r, best_mom)
    else:
        print("\nNo profitable configuration found in this period.")