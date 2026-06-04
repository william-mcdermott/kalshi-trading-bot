# app/utils/market_regime.py
"""
Shared market regime filter used by all scanners and strategies.

Fetches VIX to assess current market volatility regime before generating
signals. High VIX = fat tails, model assumptions break down, skip or warn.

Thresholds (empirically grounded):
  NORMAL   VIX < 20  — model assumptions hold, proceed normally
  ELEVATED VIX 20–30 — proceed with caution, include warning in alerts
  HIGH     VIX >= 30 — skip trading, fat tails invalidate normal-vol models

Usage:
    from app.utils.market_regime import get_regime

    regime, vix, message = get_regime()
    if regime == "HIGH":
        # skip signals, send warning
        return
    if regime == "ELEVATED":
        # include warning in iMessage
        pass
"""

import requests


VIX_ELEVATED = 20.0   # caution threshold
VIX_HIGH     = 30.0   # skip-trading threshold

# CBOE delayed quote — updated every ~15 min during market hours,
# static after close (last close value). No auth required.
_CBOE_VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json"


def get_vix() -> float:
    """
    Fetch current VIX from CBOE delayed quotes API. Raises on failure.

    Response data is a list of dicts: {"date", "open", "high", "low", "close", "volume"}
    We take the close of the most recent row.
    """
    resp = requests.get(_CBOE_VIX_URL, timeout=10)
    resp.raise_for_status()

    payload = resp.json()
    rows = payload.get("data")
    if not rows:
        raise ValueError("No VIX rows in CBOE response")

    last_close = rows[-1].get("close")
    if last_close is None:
        raise ValueError("CBOE VIX close is None")

    return round(float(last_close), 2)


def get_regime() -> tuple[str, float | None, str]:
    """
    Returns (regime, vix, message).

    regime:  "NORMAL" | "ELEVATED" | "HIGH" | "UNKNOWN"
    vix:     float or None if fetch failed
    message: human-readable string for logging / iMessage

    UNKNOWN is returned when VIX is unavailable — callers should
    treat this as ELEVATED (proceed with caution, warn in alert).
    """
    try:
        vix = get_vix()
    except Exception as e:
        return "UNKNOWN", None, f"VIX unavailable ({e}) — proceeding with caution"

    if vix >= VIX_HIGH:
        return "HIGH", vix, f"VIX={vix:.1f} ⚠️ HIGH — fat tail risk, skipping signals"
    elif vix >= VIX_ELEVATED:
        return "ELEVATED", vix, f"VIX={vix:.1f} ⚠️ ELEVATED — caution, model assumptions stressed"
    else:
        return "NORMAL", vix, f"VIX={vix:.1f} — normal conditions"