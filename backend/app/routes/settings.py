# app/routes/settings.py
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any

from app.config import get_config, update_config

router = APIRouter()


class SettingsUpdate(BaseModel):
    updates: dict[str, Any]


@router.get("/")
async def get_settings():
    return get_config()


@router.post("/")
async def post_settings(body: SettingsUpdate):
    updated = update_config(body.updates)
    return {"status": "ok", "config": updated}