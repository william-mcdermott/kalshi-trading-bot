from fastapi import APIRouter
from edgeflow.api.v1.endpoints import auth, portfolio, scanner, trades

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(portfolio.router, prefix="/portfolio", tags=["portfolio"])
api_router.include_router(scanner.router, prefix="/scanner", tags=["scanner"])
api_router.include_router(trades.router, prefix="/trades", tags=["trades"])
