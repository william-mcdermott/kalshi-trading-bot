import os
import httpx
from fastapi import APIRouter, HTTPException
from dotenv import load_dotenv

load_dotenv()
router = APIRouter()

KALSHI_HOST = os.getenv("KALSHI_HOST", "https://api.elections.kalshi.com/trade-api/v2")


@router.get("/events")
async def get_active_events(limit: int = 20):
    """Fetches active markets from Kalshi's public API. No auth needed."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{KALSHI_HOST}/markets",
                params={"limit": limit, "status": "open"},
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()

        markets = []
        for m in data.get("markets", []):
            markets.append({
                "id":        m.get("ticker"),
                "title":     m.get("title"),
                "yes_price": m.get("yes_bid", 0) / 100,
                "no_price":  m.get("no_bid",  0) / 100,
                "volume":    m.get("volume",  0),
                "markets":   [{
                    "id":        m.get("ticker"),
                    "question":  m.get("title"),
                    "yes_price": str(m.get("yes_bid", 0) / 100),
                    "no_price":  str(m.get("no_bid",  0) / 100),
                    "volume":    m.get("volume", 0),
                }]
            })
        return markets

    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Kalshi API error: {str(e)}")