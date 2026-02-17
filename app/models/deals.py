from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import relationship
from .base import Base

class Deal(Base):
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, index=True, nullable=False)
    title = Column(String)
    merchant = Column(String)
    image_url = Column(String)
    max_seen_rating = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    history = relationship("DealHistory", back_populates="deal")


class DealHistory(Base):
    __tablename__ = "deal_history"

    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False)
    temperature = Column(Float)
    velocity = Column(Float)
    hours_since_posted = Column(Float)
    source = Column(String)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now())

    deal = relationship("Deal", back_populates="history")
