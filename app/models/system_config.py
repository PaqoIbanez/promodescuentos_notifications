from sqlalchemy import Column, String, Float, DateTime, func
from .base import Base

class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String, primary_key=True, index=True)
    value = Column(String) # Storing as string to be flexible, but we cast to float in logic usually
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
