"""
app/routes/mlb.py

Endpoints for the MLB live scanner dashboard.
Serves signal log and backtest results as JSON so the dashboard
doesn't need direct filesystem access.

Registered in main.py:
    from app.routes import mlb
    app.include_router(mlb.router, prefix="/api/mlb", tags=["mlb"])
"""

import csv
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

log    = logging.getLogger(__name__)
router = APIRouter()

# ── Paths ──────────────────────────────────────────────
SCANNER_LOG  = Path("scripts/mlb_live_scanner_log.csv")
BACKTEST_LOG = Path("scripts/mlb_backtest_results.csv")

# ── Column schemas ─────────────────────────────────────
# Must match exactly what the scanner writes — column count is the discriminator
OLD_COLS = [
    "scan_time","event_ticker","team","inning","half","outs","run_diff",
    "model_prob","kalshi_mid","edge","vol24","signal",
]
# Intermediate schema — no prob_source or pitcher_adj
MID_COLS_19 = [
    "scan_time","game_pk","event_ticker","away_short","home_short","team",
    "inning","half","outs","run_diff",
    "model_prob","kalshi_mid","kalshi_bid","kalshi_ask",
    "edge_mid","edge_buy","edge_sell","vol24","signal",
]
# Intermediate schema — no pregame_prob
MID_COLS_21 = [
    "scan_time","game_pk","event_ticker","away_short","home_short","team",
    "inning","half","outs","run_diff","prob_source","pitcher_adj",
    "model_prob","kalshi_mid","kalshi_bid","kalshi_ask",
    "edge_mid","edge_buy","edge_sell","vol24","signal",
]
NEW_COLS = [
    "scan_time","game_pk","event_ticker","away_short","home_short","team",
    "inning","half","outs","run_diff","pregame_prob","prob_source",
    "pitcher_adj","model_prob","kalshi_mid","kalshi_bid","kalshi_ask",
    "edge_mid","edge_buy","edge_sell","vol24","signal",
]
KELLY_COLS = NEW_COLS + ["kelly_pct","half_kelly","full_kelly","kelly_contracts"]

ALL_SCHEMAS = {
    len(OLD_COLS):      OLD_COLS,
    len(MID_COLS_19):   MID_COLS_19,
    len(MID_COLS_21):   MID_COLS_21,
    len(NEW_COLS):      NEW_COLS,
    len(KELLY_COLS):    KELLY_COLS,
}

# Canonical output keys — what the dashboard expects regardless of source schema
CANONICAL = {
    "scan_time","game_pk","event_ticker","away_short","home_short","team",
    "inning","half","outs","run_diff","model_prob","kalshi_mid",
    "kalshi_bid","kalshi_ask","edge_mid","edge_buy","edge_sell",
    "vol24","signal","kelly_pct","half_kelly","full_kelly","kelly_contracts",
}

def _normalize(row: dict, source_cols: list[str]) -> dict:
    """Map a raw CSV row to the canonical schema, filling missing keys with ''."""
    out = {k: "" for k in CANONICAL}
    for k, v in row.items():
        if k in CANONICAL:
            out[k] = v
    # For old-schema rows, edge_mid = edge and there's no game_pk
    if source_cols is OLD_COLS:
        out["edge_mid"] = row.get("edge", "")
        # edge_buy/sell not available — approximate from edge_mid
        out["edge_buy"]  = row.get("edge", "")
        out["edge_sell"] = row.get("edge", "")
    return out


def _read_scanner_log(path: Path) -> list[dict]:
    """
    Read mlb_live_scanner_log.csv handling all schema versions.
    Discriminates by column count per row, normalizes to canonical output.
    """
    if not path.exists():
        return []
    rows = []
    try:
        with open(path, newline="") as f:
            reader = csv.reader(f)
            next(reader)  # skip header — may be wrong schema
            for raw in reader:
                schema = ALL_SCHEMAS.get(len(raw))
                if schema is None:
                    continue  # unknown schema, skip
                d = dict(zip(schema, raw))
                rows.append(_normalize(d, schema))
    except Exception as e:
        log.warning(f"Failed to read {path}: {e}")
    return rows


def _read_csv(path: Path) -> list[dict[str, Any]]:
    """Generic CSV reader for backtest results (stable schema)."""
    if not path.exists():
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        log.warning(f"Failed to read {path}: {e}")
        return []


def _dedup_signals(rows: list[dict]) -> list[dict]:
    """
    Keep only the first occurrence of each (game_pk, signal, team, inning).
    Prevents duplicate scanner runs from inflating signal counts and P&L.
    """
    seen = set()
    out  = []
    for r in rows:
        key = (r.get("game_pk",""), r.get("signal",""), r.get("team",""), r.get("inning",""))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


@router.get("/signals")
def get_signals(
    signal: str | None = None,
    limit:  int        = 500,
):
    rows = _read_scanner_log(SCANNER_LOG)
    rows = _dedup_signals(rows)
    if signal:
        rows = [r for r in rows if r.get("signal") == signal.upper()]
    rows = list(reversed(rows))
    return rows[:limit]


@router.get("/signals/live")
def get_live_signals():
    rows = _read_scanner_log(SCANNER_LOG)
    if not rows:
        return []
    last_time = rows[-1].get("scan_time", "")
    return [
        r for r in rows
        if r.get("scan_time") == last_time
        and r.get("signal") in ("BUY", "SELL")
    ]


# ── GET /api/mlb/backtest ──────────────────────────────
@router.get("/backtest")
def get_backtest(limit: int = 500):
    """
    Return rows from mlb_backtest_results.csv.
    These have outcome (correct/wrong) and Kelly P&L data.
    """
    rows = _read_csv(BACKTEST_LOG)
    return list(reversed(rows))[:limit]


# ── GET /api/mlb/stats ─────────────────────────────────
@router.get("/stats")
def get_stats():
    signals = _read_scanner_log(SCANNER_LOG)
    bt_rows = _read_csv(BACKTEST_LOG)

    # Index backtest outcomes by game_pk
    outcomes: dict[str, dict] = {}
    for r in bt_rows:
        pk = r.get("game_pk")
        if pk and pk not in outcomes:
            outcomes[pk] = {
                "correct": r.get("correct") == "True",
                "score":   r.get("score", ""),
                "winner":  r.get("winner", ""),
            }

    # Filter to actionable signals only
    actionable = [r for r in signals if r.get("signal") in ("BUY", "SELL")]
    actionable = _dedup_signals(actionable)

    total    = len(actionable)
    resolved = [r for r in actionable if r.get("game_pk") in outcomes]
    correct  = [r for r in resolved if outcomes[r["game_pk"]]["correct"]]
    pending  = total - len(resolved)

    win_rate = len(correct) / len(resolved) if resolved else None

    # Kelly P&L
    kelly_pnl   = 0.0
    kelly_staked = 0.0
    kelly_n     = 0

    for r in resolved:
        hk = _safe_float(r.get("half_kelly"))
        if hk is None or hk <= 0:
            continue
        price = (
            _safe_float(r.get("kalshi_ask"))
            if r["signal"] == "BUY"
            else 1 - (_safe_float(r.get("kalshi_bid")) or 0)
        )
        if price is None or price <= 0 or price >= 1:
            continue
        payout = hk / price
        won    = outcomes[r["game_pk"]]["correct"]
        kelly_pnl    += (payout - hk) if won else -hk
        kelly_staked += hk
        kelly_n      += 1

    kelly_roi = kelly_pnl / kelly_staked if kelly_staked > 0 else None

    # Average edge
    edges = []
    for r in actionable:
        e = (
            _safe_float(r.get("edge_buy"))
            if r["signal"] == "BUY"
            else _safe_float(r.get("edge_sell"))
        )
        if e is not None:
            edges.append(e)
    avg_edge = sum(edges) / len(edges) if edges else None

    # Inning breakdown
    inning_buckets = {
        "3-4": ([3, 4], 0, 0),
        "5-6": ([5, 6], 0, 0),
        "7":   ([7],    0, 0),
        "8-9": ([8, 9], 0, 0),
    }
    inn_stats: dict[str, dict] = {}
    for label, (innings, _, __) in inning_buckets.items():
        bucket  = [r for r in resolved if _safe_int(r.get("inning")) in innings]
        corr    = [r for r in bucket if outcomes[r["game_pk"]]["correct"]]
        inn_stats[label] = {
            "n":        len(bucket),
            "correct":  len(corr),
            "win_rate": len(corr) / len(bucket) if bucket else None,
        }

    return {
        "total":      total,
        "resolved":   len(resolved),
        "correct":    len(correct),
        "pending":    pending,
        "win_rate":   win_rate,
        "kelly_pnl":  round(kelly_pnl, 4),
        "kelly_roi":  round(kelly_roi, 4) if kelly_roi is not None else None,
        "kelly_n":    kelly_n,
        "avg_edge":   round(avg_edge, 4) if avg_edge is not None else None,
        "by_inning":  inn_stats,
    }


# ── Helpers ────────────────────────────────────────────
def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _safe_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
