import logging
from typing import Dict, Any, Optional, List
from sqlalchemy import select, update, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from app.models.deals import Deal, DealHistory
from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)

class DealsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def save_deal(self, deal_data: Dict[str, Any]) -> Optional[int]:
        """
        Saves or updates a deal in the database. Returns the deal ID.
        Does NOT commit.
        """
        try:
            stmt = insert(Deal).values(
                url=deal_data.get("url"),
                title=deal_data.get("title"),
                merchant=deal_data.get("merchant", ""),
                image_url=deal_data.get("image_url", ""),
                # created_at defaults to func.now()
            ).on_conflict_do_update(
                index_elements=['url'],
                set_={
                    'title': deal_data.get("title"),
                    'merchant': deal_data.get("merchant", ""),
                    'image_url': deal_data.get("image_url", "")
                }
            ).returning(Deal.id)

            result = await self.session.execute(stmt)
            # await self.session.commit() # Removed for Unit of Work
            return result.scalar_one()

        except Exception as e:
            logger.error(f"Error saving deal {deal_data.get('url')}: {e}")
            raise # Propagate exception to Service

    async def save_history(self, deal_id: int, deal_data: Dict[str, Any], source: str) -> bool:
        """
        Saves a history record for a deal.
        Does NOT commit.
        """
        try:
            temp = float(deal_data.get("temperature", 0))
            hours = float(deal_data.get("hours_since_posted", 0))
            minutes = max(1, hours * 60)
            velocity = temp / minutes

            new_history = DealHistory(
                deal_id=deal_id,
                temperature=temp,
                velocity=velocity,
                hours_since_posted=hours,
                source=source
            )
            self.session.add(new_history)
            # await self.session.commit() # Removed for Unit of Work
            return True
        except Exception as e:
            logger.error(f"Error saving history for deal {deal_id}: {e}")
            raise # Propagate exception

    async def get_max_rating(self, url: str) -> int:
        """Gets the max_seen_rating for a deal URL."""
        try:
            stmt = select(Deal.max_seen_rating).where(Deal.url == url)
            result = await self.session.execute(stmt)
            rating = result.scalar_one_or_none()
            return rating if rating is not None else 0
        except Exception as e:
            logger.error(f"Error getting max rating for {url}: {e}")
            return 0

    async def update_max_rating(self, url: str, new_rating: int) -> bool:
        """Updates the max_seen_rating."""
        try:
            stmt = update(Deal).where(Deal.url == url).values(max_seen_rating=new_rating)
            await self.session.execute(stmt)
            # await self.session.commit() # Caller handles commit if needed, or we keep it here if isolated? 
            # For simplicity, let's keep it here for standalone updates, or remove to be consistent?
            # The prompt specificially asked about save_deal + save_history.
            # update_max_rating is used in Analyzer logic, usually separate. 
            # But to be safe and consistent with "Atomic Transactions" instruction 
            # "Refactor DealsRepository to remove internal commits", let's remove it and let Service commit.
            return True
        except Exception as e:
            logger.error(f"Error updating max rating for {url}: {e}")
            raise 

    async def get_system_config(self) -> Dict[str, float]:
        """Loads dynamic system config from DB."""
        config = {}
        try:
            result = await self.session.execute(select(SystemConfig))
            rows = result.scalars().all()
            for row in rows:
                try:
                    config[row.key] = float(row.value)
                except ValueError:
                    pass
        except Exception as e:
            logger.error(f"Error loading system config: {e}")
        return config

    async def get_velocity_percentile(self, min_temp: float, hours_window: float, percentile: float) -> float:
        """
        Calculates the velocity percentile directly in the database.
        """
        try:
            query = text("""
                WITH Winners AS (
                    SELECT deal_id FROM deal_history GROUP BY deal_id HAVING MAX(temperature) >= :min_temp
                )
                SELECT PERCENTILE_CONT(:percentile) WITHIN GROUP (ORDER BY velocity)
                FROM deal_history 
                WHERE deal_id IN (SELECT deal_id FROM Winners)
                  AND hours_since_posted <= :hours_window
                  AND velocity > 0;
            """)
            
            result = await self.session.execute(query, {
                "min_temp": min_temp, 
                "percentile": percentile, 
                "hours_window": hours_window
            })
            val = result.scalar_one_or_none()
            return float(val) if val is not None else 0.0
        except Exception as e:
            logger.error(f"Error calculating velocity percentile: {e}")
            return 0.0

    async def update_system_config_bulk(self, config: Dict[str, float]) -> bool:
        """
        Updates multiple system config values in bulk.
        """
        if not config:
            return False
            
        try:
            for key, val in config.items():
                stmt = insert(SystemConfig).values(
                    key=key, 
                    value=str(val)
                ).on_conflict_do_update(
                    index_elements=['key'],
                    set_={'value': str(val)}
                )
                await self.session.execute(stmt)
            
            # await self.session.commit() # Removed
            return True
        except Exception as e:
            logger.error(f"Error bulk updating system config: {e}")
            raise
