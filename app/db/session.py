from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from typing import AsyncGenerator
from app.core.config import settings

# Modify DB URL to use asyncpg
DATABASE_URL = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(
    DATABASE_URL, 
    echo=False, 
    future=True,
    connect_args={"statement_cache_size": 0}
)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async session."""
    async with async_session_factory() as session:
        yield session

# Re-export these for use in main.py
async def init_db_pool():
    # SQLAlchemy engine is lazy, no explicit init needed for connection pool
    pass

async def close_db_pool():
    await engine.dispose()
