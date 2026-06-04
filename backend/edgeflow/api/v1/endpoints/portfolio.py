from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from edgeflow.db.session import get_db
from edgeflow.db.models import User
from edgeflow.core.security import get_current_user_id
from edgeflow.services import portfolio as portfolio_service

router = APIRouter()


def get_user(user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)) -> User:
    return db.query(User).filter_by(id=user_id).first()


@router.get("/balance")
async def balance(user: User = Depends(get_user)):
    return await portfolio_service.get_balance(user)


@router.get("/stats")
async def stats(user: User = Depends(get_user), db: Session = Depends(get_db)):
    return await portfolio_service.get_portfolio_stats(user, db)


@router.post("/sync")
async def sync(user: User = Depends(get_user), db: Session = Depends(get_db)):
    return await portfolio_service.sync_fills(user, db)


@router.post("/sync-settlements")
async def sync_settlements(user: User = Depends(get_user), db: Session = Depends(get_db)):
    return await portfolio_service.sync_settlements(user, db)
