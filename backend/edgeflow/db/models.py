from sqlalchemy import Column, String, Float, Integer, Boolean, DateTime, ForeignKey, Text, Enum
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
import enum
import uuid

Base = declarative_base()


def new_uuid():
    return str(uuid.uuid4())


class SubscriptionTier(str, enum.Enum):
    free = "free"
    basic = "basic"
    pro = "pro"


class User(Base):
    __tablename__ = "ef_users"

    id = Column(String, primary_key=True, default=new_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    subscription_tier = Column(Enum(SubscriptionTier), default=SubscriptionTier.free)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    kalshi_key_id = Column(String, nullable=True)
    kalshi_private_key = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    trades = relationship("Trade", back_populates="user")
    snapshots = relationship("PortfolioSnapshot", back_populates="user")


class Trade(Base):
    __tablename__ = "ef_trades"

    id = Column(String, primary_key=True, default=new_uuid)
    user_id = Column(String, ForeignKey("ef_users.id"), nullable=False)
    kalshi_trade_id = Column(String, nullable=True, index=True)
    ticker = Column(String, nullable=False)
    market_title = Column(String, nullable=True)
    category = Column(String, nullable=True)
    side = Column(String, nullable=False)
    count = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    profit_loss = Column(Float, nullable=True)
    is_win = Column(Boolean, nullable=True)
    tag = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    traded_at = Column(DateTime(timezone=True), nullable=False)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="trades")


class PortfolioSnapshot(Base):
    __tablename__ = "ef_portfolio_snapshots"

    id = Column(String, primary_key=True, default=new_uuid)
    user_id = Column(String, ForeignKey("ef_users.id"), nullable=False)
    balance = Column(Float, nullable=False)
    total_pnl = Column(Float, nullable=False)
    win_rate = Column(Float, nullable=True)
    roi = Column(Float, nullable=True)
    open_positions = Column(Integer, default=0)
    snapped_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="snapshots")


class MarketEdge(Base):
    __tablename__ = "ef_market_edges"

    id = Column(String, primary_key=True, default=new_uuid)
    ticker = Column(String, nullable=False, index=True)
    title = Column(String, nullable=True)
    category = Column(String, nullable=True)
    market_yes_price = Column(Float, nullable=True)
    model_yes_prob = Column(Float, nullable=True)
    edge_score = Column(Float, nullable=True)
    kelly_fraction = Column(Float, nullable=True)
    regime = Column(String, nullable=True)
    close_time = Column(DateTime(timezone=True), nullable=True)
    scanned_at = Column(DateTime(timezone=True), server_default=func.now())
