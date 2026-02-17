import os
from typing import Set
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Application settings managed by Pydantic.
    Reads from environment variables and .env file.
    """
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # App
    APP_BASE_URL: str = Field(default="", description="Base URL of the application")
    DEBUG: bool = Field(default=False, description="Debug mode")
    
    # Database
    DATABASE_URL: str = Field(..., description="PostgreSQL Database URL")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = Field(..., description="Telegram Bot Token")
    ADMIN_CHAT_IDS_STR: str = Field(default="", alias="ADMIN_CHAT_IDS")

    # Scraping Defaults (Dynamic config overrides these from DB)
    DEFAULT_VELOCITY_INSTANT_KILL: float = 1.7
    DEFAULT_VELOCITY_FAST_RISING: float = 1.1
    DEFAULT_MIN_TEMP_INSTANT_KILL: float = 15.0
    DEFAULT_MIN_TEMP_FAST_RISING: float = 30.0

    # Paths
    DEBUG_DIR: str = Field(default="debug", description="Directory for debug files")
    HISTORY_FILE: str = Field(default="deals_history.csv", description="CSV file for storing history (Legacy)")

    @computed_field
    def ADMIN_CHAT_IDS(self) -> Set[str]:
        """Parses the comma-separated string of admin IDs into a set."""
        if not self.ADMIN_CHAT_IDS_STR:
            return set()
        return {chat_id.strip() for chat_id in self.ADMIN_CHAT_IDS_STR.split(',') if chat_id.strip()}

settings = Settings()
