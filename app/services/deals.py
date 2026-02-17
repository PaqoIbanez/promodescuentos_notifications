import logging
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.repositories.deals import DealsRepository

logger = logging.getLogger(__name__)

class DealsService:
    def __init__(self, deals_repository: DealsRepository):
        self.deals_repo = deals_repository
        self.session = deals_repository.session

    async def process_new_deal(self, deal_data: Dict[str, Any], viral_score: float = 0.0) -> Optional[int]:
        """
        Atomically saves a deal and its initial history.
        Implements Unit of Work pattern: saves both or neither.
        Returns deal_id if successful, None otherwise.
        """
        if not deal_data.get("url"):
            return None

        try:
            # 1. Save Deal
            deal_id = await self.deals_repo.save_deal(deal_data)
            
            if not deal_id:
                raise Exception(f"Failed to get deal ID for {deal_data.get('url')}")

            # 2. Save Initial History (with viral_score)
            history_saved = await self.deals_repo.save_history(
                deal_id, deal_data, source="hunter", viral_score=viral_score
            )
            
            if not history_saved:
                 raise Exception(f"Failed to save history for deal {deal_id}")

            # 3. Atomic Commit
            await self.session.commit()
            return deal_id

        except Exception as e:
            logger.error(f"Transaction failed for deal {deal_data.get('url')}: {e}")
            await self.session.rollback()
            return None
