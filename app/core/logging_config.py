import logging
import sys
from app.core.config import settings

def setup_logging():
    """Confirms logging configuration for the application."""
    log_level = logging.DEBUG if settings.DEBUG else logging.INFO
    
    # Define format
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Basic config
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.FileHandler("app.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

    # Silence noisy libraries
    noisy_loggers = [
        "httpcore",
        "httpx",
        "urllib3",
        "asyncio",
        "aiosqlite",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "multipart.multipart",
    ]

    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
