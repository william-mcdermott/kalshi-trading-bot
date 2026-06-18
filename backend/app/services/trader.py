import logging
import os
from dataclasses import dataclass
from dotenv import load_dotenv

from app.bots.macd_strategy import Signal

load_dotenv()

log      = logging.getLogger(__name__)
DRY_RUN  = os.getenv("DRY_RUN", "true").lower() != "false"
HOST     = os.getenv("KALSHI_HOST", "https://api.elections.kalshi.com/trade-api/v2")
KEY_ID   = os.getenv("KALSHI_API_KEY_ID")
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")


@dataclass
class OrderResult:
    success:  bool
    order_id: str | None
    message:  str


def get_client():
    from kalshi_python_async import Configuration, KalshiClient
    with open(KEY_PATH, "r") as f:
        private_key = f.read()
    config = Configuration(host=HOST)
    config.api_key_id    = KEY_ID
    config.private_key_pem = private_key
    return KalshiClient(config)


async def get_balance() -> float | None:
    """
    Fetch current Kalshi cash balance via authenticated API call.
    Returns balance in dollars, or None on failure.
    """
    try:
        import httpx
        from kalshi_python_async.auth import KalshiAuth

        with open(KEY_PATH, "r") as f:
            private_key = f.read()

        auth    = KalshiAuth(key_id=KEY_ID, private_key_pem=private_key)
        url     = f"{HOST}/portfolio/balance"
        headers = auth.create_auth_headers("GET", url)

        async with httpx.AsyncClient(timeout=10.0) as http:
            r    = await http.get(url, headers=headers)
            data = r.json()

        balance = data.get("balance")
        if balance is None:
            log.warning(f"Balance endpoint returned unexpected response: {data}")
            return None

        return float(balance) / 100  # Kalshi returns cents

    except Exception as e:
        log.warning(f"Failed to fetch Kalshi balance: {e}")
        return None


async def place_order(signal: Signal, market_ticker: str, size: float, count: int = 1) -> OrderResult:
    """
    Places a limit order on Kalshi.
    market_ticker is the Kalshi market ticker e.g. "HIGHNY-23DEC-T70"
    size is in dollars (kept for logging/compat).
    count is the number of contracts to trade. Defaults to 1 so existing
    callers (e.g. the BTC scheduler) are unchanged; pass an explicit count
    to size the order off Kelly.
    """
    if signal.action == "HOLD":
        return OrderResult(success=False, order_id=None, message="No action on HOLD")

    count = max(1, int(count))

    if DRY_RUN:
        log.info(f"[DRY RUN] {signal.action} {market_ticker} @ {signal.price:.3f} x{count} (size=${size})")
        return OrderResult(success=True, order_id="dry_run_order", message="Dry run")

    try:
        from kalshi_python_async.models import CreateOrderRequest
        client = get_client()

        order = await client.create_order(
            ticker=market_ticker,
            side="yes",
            action=signal.action.lower(),
            count=count,
            yes_price=int(signal.price * 100),
        )
        
        log.info(f"Order placed — id={order.order.order_id} x{count}")
        return OrderResult(success=True, order_id=order.order.order_id, message="Order placed")

    except Exception as e:
        log.error(f"Order failed: {e}")
        return OrderResult(success=False, order_id=None, message=str(e))