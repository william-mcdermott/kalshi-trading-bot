# app/config.py
#
# Central config store for all tunable strategy parameters.
# Held in memory — changes take effect immediately without restart.
# On restart, values reset to these defaults.

from dataclasses import dataclass, asdict


@dataclass
class Config:
    # ── Momentum strategy ─────────────────────────────
    min_momentum_pct: float = 0.3    # minimum momentum %/hr to trade
    min_sell_price:   float = 0.15   # minimum contract price to sell
    min_daily_range:  float = 1000.0 # minimum BTC daily range ($)
    rsi_sell_min:     float = 40.0   # minimum RSI to allow SELL signal
    rsi_buy_max:      float = 45.0   # maximum RSI to allow BUY signal

    # ── Arb strategy ──────────────────────────────────
    min_edge:                float = 0.08  # minimum mispricing to trade
    max_hours_to_settlement: float = 6.0   # arb window (hours before 5pm)
    momentum_block:          float = 0.3   # block trade if momentum > this against us
    btc_volatility_pct: float = 0.56

    # ── Scheduler ─────────────────────────────────────
    tick_interval:      int   = 60    # seconds between bot ticks
    stale_order_minutes: int  = 30    # cancel unfilled orders after this many minutes


# Global singleton
config = Config()


def get_config() -> dict:
    return asdict(config)


def update_config(updates: dict) -> dict:
    for key, value in updates.items():
        if hasattr(config, key):
            setattr(config, key, type(getattr(config, key))(value))
    return asdict(config)