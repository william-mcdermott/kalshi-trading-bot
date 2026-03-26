from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging

from app.routes import trades, bots, market
from app.services.scheduler import run_scheduler, get_client
from app.services.fill_tracker import run_fill_tracker
from app.routes import backtest, settings

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting scheduler...")
    client = get_client()
    asyncio.create_task(run_scheduler())
    asyncio.create_task(run_fill_tracker(client))
    yield

app = FastAPI(title="Polymarket Bot API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trades.router,   prefix="/api/trades",   tags=["trades"])
app.include_router(bots.router,     prefix="/api/bots",     tags=["bots"])
app.include_router(market.router,   prefix="/api/market",   tags=["market"])
app.include_router(backtest.router, prefix="/api/backtest", tags=["backtest"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])

@app.get("/health")
def health():
    return {"status": "ok"}