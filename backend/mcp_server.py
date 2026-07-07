"""
PMBOT MCP server — exposes the live trading system's data as tools an agent can call.

Read-only by design: these tools let an LLM inspect trades, P&L, bot status, and
open positions. They deliberately do NOT expose start/stop/trade, so an agent can
observe the system but never move real money.

Run locally:
    pip install "mcp[cli]>=1.27,<2"
    python mcp_server.py            # stdio transport (default)

Test with the inspector:
    npx @modelcontextprotocol/inspector python mcp_server.py

DB location resolves to backend/data/bot.db by default; override with BOT_DB_PATH.
"""

import os
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pmbot")

# ── DB access (read-only) ────────────────────────────────────────────────────
# Resolve to the same SQLite file the bot writes to. Opened read-only so a tool
# call can never mutate trading state.
DEFAULT_DB = Path(__file__).resolve().parent / "data" / "bot.db"
DB_PATH = Path(os.getenv("BOT_DB_PATH", DEFAULT_DB))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(query: str, params: tuple = ()) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


# ── Tools ────────────────────────────────────────────────────────────────────
@mcp.tool()
def get_recent_trades(strategy: str | None = None, limit: int = 20) -> list[dict]:
    """Return the most recent trades, newest first.

    Args:
        strategy: Optional filter, e.g. "macd", "rsi", "cvd".
        limit: Max number of trades to return (default 20).
    """
    q = "SELECT strategy, market_id, side, price, size, filled, pnl, edge, created_at FROM trades"
    params: tuple = ()
    if strategy:
        q += " WHERE strategy = ?"
        params = (strategy,)
    q += " ORDER BY created_at DESC LIMIT ?"
    params = params + (limit,)
    return _rows(q, params)


@mcp.tool()
def get_pnl_summary() -> dict:
    """Return overall P&L: total, trade count, win rate, and a per-strategy breakdown."""
    trades = _rows("SELECT strategy, pnl FROM trades")
    total_pnl = sum(t["pnl"] or 0 for t in trades)
    total = len(trades)
    wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
    by_strategy: dict[str, dict] = {}
    for t in trades:
        s = by_strategy.setdefault(t["strategy"], {"trades": 0, "pnl": 0.0})
        s["trades"] += 1
        s["pnl"] = round(s["pnl"] + (t["pnl"] or 0), 4)
    return {
        "total_pnl": round(total_pnl, 4),
        "total_trades": total,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "by_strategy": by_strategy,
    }


@mcp.tool()
def get_bot_status() -> list[dict]:
    """Return each strategy's current state and its realized P&L from settled trades."""
    bots = _rows("SELECT strategy, is_running, position_size, total_trades FROM bot_status")
    for b in bots:
        settled = _rows(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades WHERE strategy = ? AND settled = 1",
            (b["strategy"],),
        )
        b["realized_pnl"] = round(settled[0]["pnl"], 4)
        b["is_running"] = bool(b["is_running"])
    return bots


@mcp.tool()
def get_open_positions() -> list[dict]:
    """Return currently open (unsettled) positions, best trading edge first."""
    return _rows(
        "SELECT strategy, market_id, side, price, size, edge, created_at "
        "FROM trades WHERE settled = 0 AND filled = 1 ORDER BY edge DESC"
    )


if __name__ == "__main__":
    mcp.run()  # stdio transport by default