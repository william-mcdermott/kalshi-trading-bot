from pydantic_settings import BaseSettings
from typing import List

try:
    from app.config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
    import os
    KALSHI_BASE_URL = os.environ.get('KALSHI_HOST', 'https://api.elections.kalshi.com/trade-api/v2')
except ImportError:
    KALSHI_API_KEY_ID = ""
    KALSHI_PRIVATE_KEY_PATH = ""
    KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

class EdgeFlowSettings(BaseSettings):
    EF_SECRET_KEY: str = "change-me-in-production"
    EF_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24
    EF_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    EF_DATABASE_URL: str = "postgresql://edgeflow:edgeflow@localhost:5432/edgeflow"
    CORS_ORIGINS: List[str] = ["http://localhost:4200"]
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_ID_BASIC: str = ""
    STRIPE_PRICE_ID_PRO: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


ef_settings = EdgeFlowSettings()
