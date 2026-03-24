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


async def place_order(signal: Signal, market_ticker: str, size: float) -> OrderResult:
    """
    Places a limit order on Kalshi.
    market_ticker is the Kalshi market ticker e.g. "HIGHNY-23DEC-T70"
    size is in dollars — Kalshi uses cents internally so we convert
    """
    if signal.action == "HOLD":
        return OrderResult(success=False, order_id=None, message="No action on HOLD")

    if DRY_RUN:
        log.info(f"[DRY RUN] {signal.action} {market_ticker} @ {signal.price:.3f} size=${size}")
        return OrderResult(success=True, order_id="dry_run_order", message="Dry run")

    try:
        from kalshi_python_async.models import CreateOrderRequest
        client = get_client()

        order = await client.create_order(
            ticker=market_ticker,
            side="yes",
            action=signal.action.lower(),
            count=1,
            yes_price=int(signal.price * 100),
        )
        
        log.info(f"Order placed — id={order.order.order_id}")
        return OrderResult(success=True, order_id=order.order.order_id, message="Order placed")

    except Exception as e:
        log.error(f"Order failed: {e}")
        return OrderResult(success=False, order_id=None, message=str(e))