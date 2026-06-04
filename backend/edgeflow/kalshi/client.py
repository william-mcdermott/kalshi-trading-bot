import httpx
from typing import Optional
import os

KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
HOST = os.environ.get("KALSHI_HOST", "https://api.elections.kalshi.com/trade-api/v2")


class KalshiClient:
    def __init__(self, key_id: Optional[str] = None, private_key_pem: Optional[str] = None):
        self.key_id = key_id or KEY_ID
        self._private_key_pem = private_key_pem
        self.host = HOST

    def _get_pem(self) -> str:
        if self._private_key_pem:
            return self._private_key_pem
        if KEY_PATH:
            with open(KEY_PATH, "r") as f:
                return f.read()
        raise ValueError("No Kalshi private key available")

    def _auth_headers(self, method: str, url: str) -> dict:
        from kalshi_python_async.auth import KalshiAuth
        auth = KalshiAuth(key_id=self.key_id, private_key_pem=self._get_pem())
        return auth.create_auth_headers(method, url)

    async def get_balance(self) -> dict:
        url = f"{self.host}/portfolio/balance"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=self._auth_headers("GET", url))
            r.raise_for_status()
            return r.json()

    async def get_fills(self, limit: int = 100, cursor: Optional[str] = None) -> dict:
        url = f"{self.host}/portfolio/fills"
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=self._auth_headers("GET", url), params=params)
            r.raise_for_status()
            return r.json()

    async def get_settlements(self, limit: int = 100, cursor: Optional[str] = None) -> dict:
        url = f"{self.host}/portfolio/settlements"
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=self._auth_headers("GET", url), params=params)
            r.raise_for_status()
            return r.json()

    async def get_markets(self, event_ticker: Optional[str] = None, limit: int = 100) -> dict:
        url = f"{self.host}/markets"
        params = {"limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=self._auth_headers("GET", url), params=params)
            r.raise_for_status()
            return r.json()
