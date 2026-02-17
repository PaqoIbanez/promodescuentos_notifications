import logging
import sys
from app.core.config import settings

def setup_logging():
    """Confirms logging configuration for the application."""
    log_level = logging.DEBUG if settings.DEBUG else logging.INFO
    
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("app.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Metter down chatter from requests/urllib3 if needed
    logging.getLogger("urllib3").setLevel(logging.WARNING)
