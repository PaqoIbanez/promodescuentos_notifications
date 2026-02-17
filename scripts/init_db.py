
import asyncio
import sys
import os
import logging

# Add project root to path
sys.path.append(os.getcwd())

from app.core.logging_config import setup_logging
from app.main import init_db_content
from app.db.session import engine
from app.models.base import Base

setup_logging()
logger = logging.getLogger(__name__)

async def main():
    logger.info("Starting manual database initialization...")
    
    # 1. Create Tables (Base.metadata.create_all)
    # This handles tables defined in models (like Deal, DealHistory, DealOutcome)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Tables created (if not existed).")
    except Exception as e:
        logger.error(f"Error creating tables: {e}")

    # 2. Run specific SQL migrations/seeds from init_db_content
    await init_db_content()
    
    await engine.dispose()
    logger.info("DB Initialization complete.")

if __name__ == "__main__":
    asyncio.run(main())
