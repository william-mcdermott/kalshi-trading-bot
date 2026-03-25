# app/services/alerter.py
#
# Sends iMessage alerts via osascript (AppleScript).
# No API keys, no accounts — just Mac's built-in Messages app.

import asyncio
import logging

log = logging.getLogger(__name__)

ALERT_NUMBER = "5129928658"


async def send_imessage(message: str):
    """Send an iMessage via AppleScript. Fire-and-forget."""
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{ALERT_NUMBER}" of targetService
        send "{message}" to targetBuddy
    end tell
    '''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error(f"iMessage failed: {stderr.decode().strip()}")
        else:
            log.info(f"iMessage sent: {message[:60]}...")
    except Exception as e:
        log.error(f"iMessage error: {e}")


async def alert_trade_placed(side: str, market: str, price: float, size: float):
    emoji = "🔴" if side == "SELL" else "🟢"
    await send_imessage(
        f"{emoji} PMBOT trade\n"
        f"{side} {market[-16:]}\n"
        f"price={price:.2f}  size=${size:.0f}"
    )


async def alert_trade_settled(side: str, market: str, pnl: float):
    emoji = "✅" if pnl >= 0 else "❌"
    sign  = "+" if pnl >= 0 else ""
    await send_imessage(
        f"{emoji} PMBOT settled\n"
        f"{side} {market[-16:]}\n"
        f"pnl={sign}${pnl:.4f}"
    )