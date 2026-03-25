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
BTC_VOLATILITY   = 2.5
ARB_WINDOW_HOURS = 6.0


def normal_cdf(x: float) -> float:
    t    = 1 / (1 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
           + t * (-1.821255978 + t * 1.330274429))))
    p    = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-x * x / 2) * poly
    return p if x >= 0 else 1 - p


def fair_value(btc_price: float, threshold: float, hours_left: float) -> float:
    if hours_left <= 0:
        return 1.0 if btc_price >= threshold else 0.0
    if hours_left > 24:
        return 0.5
    distance_pct = (btc_price - threshold) / threshold * 100
    total_vol    = BTC_VOLATILITY * math.sqrt(hours_left)
    z            = distance_pct / total_vol
    return round(normal_cdf(z), 4)


def calculate_momentum(prices: list[float]) -> float:
    if len(prices) < 2:
        return 0.0
    n      = len(prices)
    mean_x = (n - 1) / 2
    mean_y = statistics.mean(prices)
    num    = sum((i - mean_x) * (prices[i] - mean_y) for i in range(n))
    den    = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return (num / den * CANDLES_PER_HOUR / prices[0]) * 100


def daily_range_at(df: pd.DataFrame, idx: int) -> float:
    """Calculates the high-low range for the current trading day up to candle idx."""
    current_day = df.iloc[idx]['dt'].date()
    day_candles = df[(df['dt'].dt.date == current_day) & (df.index <= idx)]
    if day_candles.empty:
        return 0.0
    return float(day_candles['high'].max()) - float(day_candles['low'].min())


def simulate_markets(btc_price: float, hours_left: float, spacing: float = 250.0) -> list[dict]:
    markets = []
    for offset in range(-10, 11):
        threshold      = round((btc_price + offset * spacing) / spacing) * spacing
        contract_price = fair_value(btc_price, threshold, hours_left)
        distance_pct   = abs(btc_price - threshold) / btc_price * 100
        spread         = 0.04 + distance_pct * 0.02
        mispricing     = (distance_pct / 100) * 0.15

        if threshold > btc_price:
            market_price = max(0.05, contract_price - mispricing)
        else:
            market_price = min(0.95, contract_price + mispricing)

        if market_price <= 0.05 or market_price >= 0.95:
            continue

        markets.append({
            "threshold":    threshold,
            "fair_value":   contract_price,
            "market_price": market_price,
            "bid":          max(0.01, market_price - spread / 2),
            "ask":          min(0.99, market_price + spread / 2),
        })

    return markets


@dataclass
class SimulatedTrade:
    timestamp:         datetime
    side:              str
    entry_price:       float
    threshold:         float
    fair_val:          float
    edge:              float
    btc_at_entry:      float
    btc_at_settlement: float
    resolved_yes:      bool
    pnl:               float
    momentum:          float
    hours_left:        float
    strategy:          str
    daily_range:       float = 0.0


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

    @property
    def arb_trades(self): return [t for t in self.trades if t.strategy == "arb"]

    @property
    def momentum_trades(self): return [t for t in self.trades if t.strategy == "momentum"]


def run_backtest(
    days: int = 30,
    min_edge: float = 0.08,
    min_momentum: float = MIN_MOMENTUM_PCT,
    min_daily_range: float = 0.0,
    candles: list = None,
) -> BacktestResults:
    df       = pd.DataFrame(candles, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['dt'] = pd.to_datetime(df['ts'], unit='ms', utc=True)

    results       = BacktestResults()
    open_position = None

    for i in range(LOOKBACK, len(df)):
        row       = df.iloc[i]
        btc_price = float(row['close'])
        dt        = row['dt']

        settlement = dt.replace(hour=21, minute=0, second=0, microsecond=0)
        if dt >= settlement:
            settlement += pd.Timedelta(days=1)
        hours_left = (settlement - dt).total_seconds() / 3600

        prices    = df['close'].iloc[i - LOOKBACK:i].tolist()
        momentum  = calculate_momentum(prices)
        d_range   = daily_range_at(df, i)

        # Close open position at start of new settlement day
        if open_position and hours_left > 23.5:
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
                fair_val          = open_position['fair_val'],
                edge              = open_position['edge'],
                btc_at_entry      = open_position['btc_entry'],
                btc_at_settlement = open_position['btc_settlement'],
                resolved_yes      = resolved_yes,
                pnl               = round(pnl * POSITION_SIZE, 4),
                momentum          = open_position['momentum'],
                hours_left        = open_position['hours_left'],
                strategy          = open_position['strategy'],
                daily_range       = open_position['daily_range'],
            )
            results.trades.append(trade)
            results.total_pnl += trade.pnl
            if trade.pnl > 0:
                results.win_count  += 1
            else:
                results.loss_count += 1
            open_position = None

        if open_position:
            open_position['btc_settlement'] = btc_price
            continue

        # Volatility filter
        if min_daily_range > 0 and d_range < min_daily_range:
            continue

        signal_side   = None
        signal_price  = None
        signal_thresh = None
        signal_fv     = None
        signal_edge   = None
        signal_strat  = None

        if hours_left <= ARB_WINDOW_HOURS:
            markets   = simulate_markets(btc_price, hours_left)
            best_edge = 0.0

            for m in markets:
                edge = m['fair_value'] - m['market_price']
                if abs(edge) < min_edge:
                    continue
                if edge > 0 and momentum < 0:
                    continue
                if edge < 0 and momentum > 0:
                    continue
                if abs(edge) > best_edge:
                    best_edge     = abs(edge)
                    signal_side   = 'BUY' if edge > 0 else 'SELL'
                    signal_price  = m['ask'] if signal_side == 'BUY' else m['bid']
                    signal_thresh = m['threshold']
                    signal_fv     = m['fair_value']
                    signal_edge   = edge
                    signal_strat  = 'arb'
        else:
            if momentum > min_momentum:
                threshold    = round((btc_price + 800) / 250) * 250
                fv           = fair_value(btc_price, threshold, hours_left)
                market_price = max(0.05, fv - 0.05)
                if market_price < MAX_BUY_PRICE:
                    signal_side   = 'BUY'
                    signal_price  = market_price
                    signal_thresh = threshold
                    signal_fv     = fv
                    signal_edge   = fv - market_price
                    signal_strat  = 'momentum'
            elif momentum < -min_momentum:
                threshold    = round((btc_price - 800) / 250) * 250
                fv           = fair_value(btc_price, threshold, hours_left)
                market_price = min(0.95, fv + 0.05)
                if market_price > MIN_SELL_PRICE:
                    signal_side   = 'SELL'
                    signal_price  = market_price
                    signal_thresh = threshold
                    signal_fv     = fv
                    signal_edge   = fv - market_price
                    signal_strat  = 'momentum'

        if signal_side is None:
            continue

        open_position = {
            'timestamp':      dt,
            'side':           signal_side,
            'entry_price':    signal_price,
            'threshold':      signal_thresh,
            'fair_val':       signal_fv,
            'edge':           signal_edge,
            'btc_entry':      btc_price,
            'btc_settlement': btc_price,
            'momentum':       momentum,
            'hours_left':     hours_left,
            'strategy':       signal_strat,
            'daily_range':    d_range,
        }

    return results


def print_results(results: BacktestResults, label: str = ""):
    print(f"\n{'='*55}")
    print(f"BACKTEST RESULTS {label}")
    print(f"{'='*55}")
    print(f"Total trades:    {results.total_trades}  "
          f"(arb={len(results.arb_trades)} momentum={len(results.momentum_trades)})")
    print(f"Win rate:        {results.win_rate:.1f}%")
    print(f"Total P&L:       ${results.total_pnl:.4f}")
    pf = f"{results.profit_factor:.2f}" if results.profit_factor != float('inf') else "∞"
    print(f"Profit factor:   {pf}")
    print(f"Max drawdown:    ${results.max_drawdown:.4f}")
    if results.total_trades:
        print(f"Avg P&L/trade:   ${results.total_pnl / results.total_trades:.4f}")
    if results.arb_trades:
        arb_pnl  = sum(t.pnl for t in results.arb_trades)
        arb_wins = sum(1 for t in results.arb_trades if t.pnl > 0)
        print(f"\nArb trades:      {len(results.arb_trades)}  "
              f"win={arb_wins/len(results.arb_trades)*100:.0f}%  "
              f"P&L=${arb_pnl:.4f}")
    if results.momentum_trades:
        mom_pnl  = sum(t.pnl for t in results.momentum_trades)
        mom_wins = sum(1 for t in results.momentum_trades if t.pnl > 0)
        print(f"Momentum trades: {len(results.momentum_trades)}  "
              f"win={mom_wins/len(results.momentum_trades)*100:.0f}%  "
              f"P&L=${mom_pnl:.4f}")
    if results.trades:
        print()
        print(f"{'Time':<20} {'Strat':<10} {'Side':<6} {'Entry':<7} {'FV':<7} {'Edge':<8} {'Mom':<10} {'Range':<8} {'P&L'}")
        print("-" * 95)
        for t in results.trades[-15:]:
            print(
                f"{t.timestamp.strftime('%m-%d %H:%M'):<20} "
                f"{t.strategy:<10} "
                f"{t.side:<6} "
                f"{t.entry_price:<7.3f} "
                f"{t.fair_val:<7.3f} "
                f"{t.edge:+.3f}   "
                f"{t.momentum:+.2f}%/hr  "
                f"${t.daily_range:<7,.0f} "
                f"{'+'if t.pnl>0 else ''}{t.pnl:.4f}"
            )
    print("=" * 55)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    print(f"Fetching {days} days of BTC/USDT 1-hour candles from Kraken...")
    exchange    = ccxt.kraken()
    since       = exchange.parse8601(
        (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days))
        .strftime('%Y-%m-%dT%H:%M:%SZ')
    )
    all_candles = exchange.fetch_ohlcv('BTC/USDT', '1h', since=since, limit=720)
    print(f"Got {len(all_candles)} candles ({len(all_candles)/24:.1f} days)\n")

    print(f"{'Range':<12} {'Momentum':<10} {'Trades':<8} {'Win%':<8} {'P&L':<10} {'PF':<8} {'MaxDD'}")
    print("-" * 60)

    best_pnl    = float('-inf')
    best_params = None
    best_r      = None

    for min_range in [0, 500, 1000, 1500, 2000]:
        for min_mom in [0.2, 0.3, 0.5]:
            r  = run_backtest(
                days=days, min_edge=0.08, min_momentum=min_mom,
                min_daily_range=min_range, candles=all_candles
            )
            pf = f"{r.profit_factor:.2f}" if r.profit_factor != float('inf') else "∞"
            print(
                f">${min_range:<11} "
                f"{min_mom:<10.1f} "
                f"{r.total_trades:<8} "
                f"{r.win_rate:<8.1f} "
                f"${r.total_pnl:<9.4f} "
                f"{pf:<8} "
                f"${r.max_drawdown:.4f}"
            )
            if r.total_trades >= 3 and r.total_pnl > best_pnl:
                best_pnl    = r.total_pnl
                best_mom    = min_mom
                best_r      = r
                best_params = (min_range, min_mom)

    if best_r:
        print(f"\nBest: range>${best_params[0]} momentum>{best_params[1]}")
        print_results(best_r, f"range>${best_params[0]} momentum>{best_params[1]}")
    else:
        print("\nNo profitable configuration found.")