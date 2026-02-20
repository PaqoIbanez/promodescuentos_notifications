import logging
from typing import Dict, Any, Optional, List, Tuple
from sqlalchemy import select, update, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from app.models.deals import Deal, DealHistory, DealOutcome
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
            ).on_conflict_do_update(
                index_elements=['url'],
                set_={
                    'title': deal_data.get("title"),
                    'merchant': deal_data.get("merchant", ""),
                    'image_url': deal_data.get("image_url", "")
                }
            ).returning(Deal.id)

            result = await self.session.execute(stmt)
            return result.scalar_one()

        except Exception as e:
            logger.error(f"Error saving deal {deal_data.get('url')}: {e}")
            raise

    async def save_history(self, deal_id: int, deal_data: Dict[str, Any], source: str, viral_score: float = 0.0) -> bool:
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
                viral_score=viral_score,
                hours_since_posted=hours,
                source=source
            )
            self.session.add(new_history)
            return True
        except Exception as e:
            logger.error(f"Error saving history for deal {deal_id}: {e}")
            raise

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
            return True
        except Exception as e:
            logger.error(f"Error updating max rating for {url}: {e}")
            raise 

    async def get_latest_snapshot(self, deal_url: str) -> Optional[Tuple[float, float]]:
        """
        Returns the most recent (temperature, hours_since_posted) from deal_history
        for a given deal URL. Used for acceleration detection.
        """
        try:
            query = text("""
                SELECT dh.temperature, dh.hours_since_posted
                FROM deal_history dh
                JOIN deals d ON d.id = dh.deal_id
                WHERE d.url = :url
                ORDER BY dh.recorded_at DESC
                LIMIT 1;
            """)
            result = await self.session.execute(query, {"url": deal_url})
            row = result.first()
            if row:
                return (float(row[0]), float(row[1]))
            return None
        except Exception as e:
            logger.error(f"Error getting latest snapshot for {deal_url}: {e}")
            return None

    async def get_latest_snapshots_batch(self, deal_urls: List[str]) -> Dict[str, Tuple[float, float]]:
        """
        Batch version: returns latest (temperature, hours_since_posted) for multiple deal URLs.
        Returns a dict keyed by URL.
        """
        if not deal_urls:
            return {}
        try:
            query = text("""
                SELECT DISTINCT ON (d.url) d.url, dh.temperature, dh.hours_since_posted
                FROM deal_history dh
                JOIN deals d ON d.id = dh.deal_id
                WHERE d.url = ANY(:urls)
                ORDER BY d.url, dh.recorded_at DESC;
            """)
            result = await self.session.execute(query, {"urls": deal_urls})
            rows = result.fetchall()
            return {row[0]: (float(row[1]), float(row[2])) for row in rows}
        except Exception as e:
            logger.error(f"Error getting batch snapshots: {e}")
            return {}

    async def get_golden_ratio_stats(self, checkpoint_hours: float, min_temp_at_checkpoint: float, success_temp: float) -> Dict[str, Any]:
        """
        Golden Ratio Analysis: Of deals that had >= min_temp at checkpoint_hours,
        what percentage reached success_temp?
        
        Returns: {probability: float, sample_size: int, successes: int}
        """
        try:
            query = text("""
                WITH candidates AS (
                    SELECT DISTINCT dh.deal_id
                    FROM deal_history dh
                    WHERE dh.hours_since_posted <= :checkpoint
                      AND dh.temperature >= :min_temp
                ),
                outcomes AS (
                    SELECT c.deal_id,
                           MAX(dh2.temperature) as max_temp
                    FROM candidates c
                    JOIN deal_history dh2 ON dh2.deal_id = c.deal_id
                    GROUP BY c.deal_id
                )
                SELECT 
                    COUNT(*) as sample_size,
                    COUNT(*) FILTER (WHERE max_temp >= :success_temp) as successes
                FROM outcomes;
            """)
            result = await self.session.execute(query, {
                "checkpoint": checkpoint_hours,
                "min_temp": min_temp_at_checkpoint,
                "success_temp": success_temp
            })
            row = result.first()
            if row and row[0] > 0:
                return {
                    "sample_size": int(row[0]),
                    "successes": int(row[1]),
                    "probability": round(int(row[1]) / int(row[0]) * 100, 1)
                }
            return {"sample_size": 0, "successes": 0, "probability": 0.0}
        except Exception as e:
            logger.error(f"Error calculating golden ratio: {e}")
            return {"sample_size": 0, "successes": 0, "probability": 0.0}

    async def get_viral_score_percentile(self, min_final_temp: float, hours_window: float, percentile: float) -> float:
        """
        Calculates the viral_score percentile from historical winners.
        Used by AutoTuner to dynamically set viral_threshold.
        """
        try:
            query = text("""
                WITH Winners AS (
                    SELECT deal_id FROM deal_history GROUP BY deal_id HAVING MAX(temperature) >= :min_temp
                )
                SELECT PERCENTILE_CONT(:percentile) WITHIN GROUP (ORDER BY viral_score)
                FROM deal_history 
                WHERE deal_id IN (SELECT deal_id FROM Winners)
                  AND hours_since_posted <= :hours_window
                  AND viral_score > 0;
            """)
            result = await self.session.execute(query, {
                "min_temp": min_final_temp,
                "percentile": percentile,
                "hours_window": hours_window
            })
            val = result.scalar_one_or_none()
            return float(val) if val is not None else 0.0
        except Exception as e:
            logger.error(f"Error calculating viral score percentile: {e}")
            return 0.0

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
        Legacy method kept for backwards compatibility.
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
        """Updates multiple system config values in bulk."""
        if not config:
            return False
            
        try:
            for key, val in config.items():
                stmt = insert(SystemConfig).values(
                    key=key, 
                    value=str(val)
                ).on_conflict_do_update(
                    index_elements=['key'],
                    set_={'value': str(val), 'updated_at': func.now()}
                )
                await self.session.execute(stmt)
            
            return True
        except Exception as e:
            logger.error(f"Error bulk updating system config: {e}")
            raise

    async def get_by_url(self, url: str) -> Optional[Deal]:
        """Retrieves a Deal by its URL."""
        try:
            stmt = select(Deal).where(Deal.url == url)
            result = await self.session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting deal by url {url}: {e}")
            return None

    async def get_outcome(self, deal_id: int) -> Optional[DealOutcome]:
        """Retrieves the DealOutcome for a given deal_id."""
        try:
            stmt = select(DealOutcome).where(DealOutcome.deal_id == deal_id)
            result = await self.session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting outcome for deal {deal_id}: {e}")
            return None

    async def get_training_dataset(self, checkpoint_mins: int = 30) -> List[Dict[str, Any]]:
        """
        Fetches a dataset for training/validation.
        Features: derived from DealHistory at approx 'checkpoint_mins' after posting.
        Labels: derived from DealOutcome (final_max_temp, reached_X).
        """
        try:
            # We want the snapshot closest to checkpoint_mins, but not BEFORE it (to avoid lookahead bias? 
            # actually we want the state AT that time). 
            # Let's simple pick the first history record where hours_since_posted * 60 >= checkpoint_mins
            
            query = text("""
                WITH TargetSnapshots AS (
                    SELECT DISTINCT ON (dh.deal_id) 
                        dh.deal_id,
                        dh.temperature as temp_at_checkpoint,
                        dh.velocity as velocity_at_checkpoint,
                        dh.viral_score as score_at_checkpoint,
                        dh.hours_since_posted
                    FROM deal_history dh
                    WHERE dh.hours_since_posted * 60 >= :mins
                    ORDER BY dh.deal_id, dh.hours_since_posted ASC
                )
                SELECT 
                    ts.deal_id,
                    ts.temp_at_checkpoint,
                    ts.velocity_at_checkpoint,
                    ts.score_at_checkpoint,
                    doc.final_max_temp,
                    doc.reached_500,
                    extract(ISODOW from d.created_at) as dow,
                    extract(HOUR from d.created_at) as hour_of_day
                FROM TargetSnapshots ts
                JOIN deal_outcomes doc ON doc.deal_id = ts.deal_id
                JOIN deals d ON d.id = ts.deal_id
                WHERE doc.final_max_temp > 0;
            """)
            
            result = await self.session.execute(query, {"mins": checkpoint_mins})
            rows = result.mappings().all()
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"Error fetching training dataset: {e}")
            return []
