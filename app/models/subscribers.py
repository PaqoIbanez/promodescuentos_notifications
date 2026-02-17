from sqlalchemy import Column, String, DateTime, func
from .base import Base

class Subscriber(Base):
    __tablename__ = "subscribers"

    chat_id = Column(String, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
