import logging
from typing import Set
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.subscribers import Subscriber

logger = logging.getLogger(__name__)

class SubscribersRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
        # Table creation is handled by init_db (alembic or create_all)

    async def get_all(self) -> Set[str]:
        """Retrieves all subscriber chat_ids."""
        try:
            result = await self.session.execute(select(Subscriber.chat_id))
            return {row[0] for row in result.fetchall()}
        except Exception as e:
            logger.error(f"Error fetching subscribers: {e}")
            return set()

    async def add(self, chat_id: str) -> bool:
        """Adds a subscriber. Returns True if added, False if already exists or error."""
        try:
            # Check if exists first to return correct boolean
            # (or handling IntegrityError, but check is cleaner for boolean return)
            exists = await self.exists(chat_id)
            if exists:
                return False
            
            new_sub = Subscriber(chat_id=chat_id)
            self.session.add(new_sub)
            await self.session.commit()
            return True
        except Exception as e:
            logger.error(f"Error adding subscriber {chat_id}: {e}")
            await self.session.rollback()
            return False

    async def remove(self, chat_id: str) -> bool:
        """Removes a subscriber. Returns True if removed/not found, False on error."""
        try:
            await self.session.execute(delete(Subscriber).where(Subscriber.chat_id == chat_id))
            await self.session.commit()
            return True
        except Exception as e:
            logger.error(f"Error removing subscriber {chat_id}: {e}")
            await self.session.rollback()
            return False

    async def exists(self, chat_id: str) -> bool:
        try:
            result = await self.session.execute(select(Subscriber).where(Subscriber.chat_id == chat_id))
            return result.scalar_one_or_none() is not None
        except Exception as e:
            logger.error(f"Error checking subscriber {chat_id}: {e}")
            return False
