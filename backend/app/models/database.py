# app/models/database.py
#
# This defines your database tables using SQLAlchemy.
# It's similar to a Mongoose schema, but for SQLite (a local file-based database).
# SQLAlchemy is the most common Python ORM — think of it as Mongoose for SQL.

from datetime import datetime
from sqlalchemy import String, Float, DateTime, Boolean, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# Base class — all models inherit from this (like extending a base Mongoose model)
class Base(DeclarativeBase):
    pass


class Trade(Base):
    """Represents a single trade executed by a bot."""
    __tablename__ = "trades"

    id:           Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy:     Mapped[str]      = mapped_column(String, nullable=False)   # "macd", "rsi", "cvd"
    market_id:    Mapped[str]      = mapped_column(String, nullable=False)   # Polymarket market ID
    side:         Mapped[str]      = mapped_column(String, nullable=False)   # "BUY" or "SELL"
    price:        Mapped[float]    = mapped_column(Float,  nullable=False)   # 0.01 to 0.99
    size:         Mapped[float]    = mapped_column(Float,  nullable=False)   # dollars
    filled:       Mapped[bool]     = mapped_column(Boolean, default=False)
    pnl:          Mapped[float]    = mapped_column(Float,  default=0.0)      # profit/loss in dollars
    edge:         Mapped[float]    = mapped_column(Float,  default=0.0)      # arb edge at time of trade
    settled:      Mapped[bool]     = mapped_column(Boolean, default=False)
    order_id:     Mapped[str]      = mapped_column(String, nullable=True)    # Polymarket order ID
    created_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at:    Mapped[datetime] = mapped_column(DateTime, nullable=True)


class BotStatus(Base):
    """Tracks the current state of each running bot."""
    __tablename__ = "bot_status"

    id:           Mapped[int]   = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy:     Mapped[str]   = mapped_column(String, unique=True, nullable=False)
    is_running:   Mapped[bool]  = mapped_column(Boolean, default=False)
    position_size:Mapped[float] = mapped_column(Float, default=1.0)
    total_trades: Mapped[int]   = mapped_column(Integer, default=0)
    total_pnl:    Mapped[float] = mapped_column(Float, default=0.0)
    updated_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
