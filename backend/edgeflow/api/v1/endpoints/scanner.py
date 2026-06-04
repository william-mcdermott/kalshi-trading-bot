from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from edgeflow.db.session import get_db
from edgeflow.db.models import User
from edgeflow.core.security import get_current_user_id
from edgeflow.scanners.registry import ScannerRegistry

router = APIRouter()


def get_user(user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)) -> User:
    return db.query(User).filter_by(id=user_id).first()


@router.get("/regime")
async def regime():
    try:
        from app.utils.market_regime import get_current_regime
        return await get_current_regime()
    except ImportError:
        return {"vix": None, "regime": "normal", "kelly_multiplier": 0.8}


@router.get("/edges")
async def edges(user: User = Depends(get_user)):
    registry = ScannerRegistry()
    results = await registry.run_all()
    return {"edges": results, "count": len(results)}
