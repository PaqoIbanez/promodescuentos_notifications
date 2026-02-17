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
    
    # Tracking fields
    is_active = Column(Integer, default=1) # 1=True, 0=False (SQLite boolean compat)
    last_tracked_at = Column(DateTime(timezone=True), server_default=func.now())
    activity_status = Column(String, default="active") # active, expired, settled

    history = relationship("DealHistory", back_populates="deal")
    outcome = relationship("DealOutcome", back_populates="deal", uselist=False)


class DealHistory(Base):
    __tablename__ = "deal_history"

    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False)
    temperature = Column(Float)
    velocity = Column(Float)
    viral_score = Column(Float, default=0.0)
    hours_since_posted = Column(Float)
    source = Column(String)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now())

    deal = relationship("Deal", back_populates="history")


class DealOutcome(Base):
    __tablename__ = "deal_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), unique=True, nullable=False)
    
    final_max_temp = Column(Float, default=0.0)
    reached_200 = Column(Integer, default=0) # Boolean
    reached_500 = Column(Integer, default=0) # Boolean
    reached_1000 = Column(Integer, default=0) # Boolean
    time_to_200_mins = Column(Float, nullable=True)
    
    last_updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    deal = relationship("Deal", back_populates="outcome")
