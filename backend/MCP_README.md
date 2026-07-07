# PMBOT MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes a live-capital
algorithmic trading system to AI agents as a set of read-only tools. An MCP client such as Claude
can connect to it and inspect real trading data — trades, P&L, bot status, and open positions —
by calling structured tools instead of being handed a static export.

PMBOT is a multi-strategy trading system running real capital on the [Kalshi](https://kalshi.com)
prediction-market exchange. This server is the interface that lets a model read that system's state
directly.

## Why this exists

Increasingly, the consumer of an application is a model, not a person. A REST API with a UI in front
of it is software built for humans; an MCP server is the same data shaped for an agent to reason over.
This server takes the read queries that already power PMBOT's dashboard and re-exposes them as tools an
LLM can call on its own, deciding which to reach for based on what's being asked.

## Read-only by design

The tools deliberately expose **observation only** — no start, stop, or trade. An agent can inspect the
system but can never move real money through it. The SQLite database is opened in read-only mode
(`mode=ro`), so a tool call physically cannot mutate trading state. Giving a model a window into a
live financial system is useful; giving it the steering wheel is not.

## Tools

| Tool | Returns |
| --- | --- |
| `get_recent_trades(strategy?, limit=20)` | Most recent trades, newest first, optionally filtered by strategy |
| `get_pnl_summary()` | Total P&L, trade count, win rate, and a per-strategy breakdown |
| `get_bot_status()` | Each strategy's running state, position size, and realized P&L from settled trades |
| `get_open_positions()` | Currently open (unsettled) positions, sorted best trading edge first |

Each tool maps directly to a query that already backs the trading dashboard, reading from the same
`trades` and `bot_status` tables the bot writes to.

## Setup

Requires Python 3.10+.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install "mcp[cli]>=1.27,<2"
```

The `<2` bound is intentional: the MCP Python SDK's v2 line renames the core server class, so pinning to
the stable v1.x avoids a breaking change on upgrade.

## Running

```bash
python mcp_server.py          # stdio transport (default)
```

The database resolves to `backend/data/bot.db` by default. Override with the `BOT_DB_PATH`
environment variable if it lives elsewhere.

> Note: `backend/data/` is gitignored (it holds live trading data), so a fresh clone won't include a
> database and the tools will return empty results until one exists. The server reads whatever
> `bot.db` the running system writes to.

## Testing

Inspect and call the tools interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python mcp_server.py
```

Connect, list the tools, and run any of them against live data.

## Connecting to Claude Desktop

Add the server to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pmbot": {
      "command": "/absolute/path/to/backend/.venv/bin/python3",
      "args": ["/absolute/path/to/backend/mcp_server.py"]
    }
  }
}
```

Use the absolute path to the virtualenv's Python so the SDK is on the path, then fully restart Claude
Desktop. Once connected, ask it something like *"what are my open positions and which has the best edge?"*
and it will call the tools and answer from live data.

## Implementation notes

- Single file, no framework beyond the official `mcp` SDK and the standard library.
- Tools are thin wrappers over parameterized SQL; the query logic mirrors the trading dashboard's own
  read endpoints.
- stdio transport, so no stray writes to stdout (which would corrupt the JSON-RPC stream).
