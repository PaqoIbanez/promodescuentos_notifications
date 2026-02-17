
import asyncio
import logging
import random
import time
from typing import List
from sqlalchemy import select, func, text
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.session import async_session_factory
from app.repositories.deals import DealsRepository
from app.repositories.subscribers import SubscribersRepository
from app.services.scraper import ScraperService
from app.services.analyzer import AnalyzerService
from app.services.deals import DealsService
from app.services.telegram import TelegramService
from app.models.deals import Deal, DealOutcome

logger = logging.getLogger(__name__)

class SchedulerService:
    def __init__(self, scraper_service: ScraperService, telegram_service: TelegramService):
        self.scraper = scraper_service
        self.telegram = telegram_service
        self.shutdown_event = asyncio.Event()
        self.tasks = []
        
        # Analyzer (global instance, config updated periodically)
        self.analyzer = AnalyzerService({})

    async def start(self):
        """Starts all background loops."""
        logger.info("SchedulerService starting...")
        
        # Load initial config
        try:
            async with async_session_factory() as session:
                deals_repo = DealsRepository(session)
                config = await deals_repo.get_system_config()
                self.analyzer.update_config(config)
        except Exception as e:
            logger.error(f"Failed to load initial config: {e}")

        # Launch tasks
        self.tasks.append(asyncio.create_task(self.run_hunter()))
        self.tasks.append(asyncio.create_task(self.run_tracker()))
        self.tasks.append(asyncio.create_task(self.run_historian()))
        logger.info(f"SchedulerService started with {len(self.tasks)} loops.")

    async def stop(self):
        """Signals all loops to stop and waits for them."""
        logger.info("SchedulerService stopping...")
        self.shutdown_event.set()
        
        for t in self.tasks:
            t.cancel()
        
        await asyncio.gather(*self.tasks, return_exceptions=True)
        logger.info("SchedulerService stopped.")

    # --- 1. THE HUNTER (Finds new deals) ---
    async def run_hunter(self):
        """Scrapes /nuevas every 5-10 minutes."""
        logger.info("🏹 Hunter loop started.")
        while not self.shutdown_event.is_set():
            try:
                logger.info("🏹 Hunter: Scanning /nuevas ...")
                async with async_session_factory() as session:
                    deals_repo = DealsRepository(session)
                    sub_repo = SubscribersRepository(session)
                    deals_service = DealsService(deals_repo)
                    
                    html = await self.scraper.fetch_page("https://www.promodescuentos.com/nuevas")
                    if html:
                        deals = await asyncio.to_thread(self.scraper.parse_deals, html)
                        
                        # Batch get snapshots
                        urls = [d["url"] for d in deals if d.get("url")]
                        snapshots = await deals_repo.get_latest_snapshots_batch(urls)
                        
                        count = 0
                        for deal_data in deals:
                            url = deal_data.get("url")
                            if not url: continue
                            
                            # Analyze
                            prev_snap = snapshots.get(url)
                            analysis = self.analyzer.analyze_deal(deal_data, prev_snap)
                            
                            # Process & Save
                            deal_id = await deals_service.process_new_deal(deal_data, viral_score=analysis["final_score"])
                            
                            # Notify if Viral
                            if analysis["is_hot"] and deal_id:
                                await self._handle_viral_deal(deal_data, analysis, deals_repo, sub_repo)
                                count += 1
                        
                        logger.info(f"🏹 Hunter: Processed {len(deals)} items. {count} viral.")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"🏹 Hunter Error: {e}")

            # Wait 5-10 mins
            if not self.shutdown_event.is_set():
                await self._sleep(random.randint(300, 600))

    # --- 2. THE TRACKER (Updates active deals) ---
    async def run_tracker(self):
        """Updates active deals every 15-30 minutes."""
        logger.info("👀 Tracker loop started.")
        while not self.shutdown_event.is_set():
            try:
                async with async_session_factory() as session:
                    # Select deals: Active, Created < 24h ago
                    query = select(Deal).options(selectinload(Deal.history)).where(
                        Deal.is_active == 1,
                        Deal.created_at >= func.now() - text("INTERVAL '24 HOURS'")
                    ).order_by(Deal.last_tracked_at.asc()).limit(10) # Process batch of 10
                    
                    result = await session.execute(query)
                    active_deals = result.scalars().all()
                    
                    if not active_deals:
                         logger.debug("👀 Tracker: No active deals to track.")
                         await self._sleep(300)
                         continue

                    logger.info(f"👀 Tracker: Tracking {len(active_deals)} active deals...")
                    
                    deals_repo = DealsRepository(session)
                    deals_service = DealsService(deals_repo)
                    
                    for deal in active_deals:
                         if self.shutdown_event.is_set(): break
                         
                         html = await self.scraper.fetch_page(deal.url)
                         if html:
                             details = await asyncio.to_thread(self.scraper.parse_deal_detail, html)
                             if details:
                                 # Update logic
                                 if details.get("is_expired") or details.get("status") != "Activated":
                                     deal.is_active = 0
                                     deal.activity_status = "expired"
                                     logger.info(f"👀 Tracker: Deal {deal.id} expired.")
                                 
                                 # Update temperature/price in history
                                 # Calculate hours_since_posted
                                 hours_since_posted = deal.history[-1].hours_since_posted if deal.history else 0 # Fallback
                                 if details.get("published_at"):
                                     hours_since_posted = (time.time() - float(details["published_at"])) / 3600
                                 elif deal.created_at: 
                                      # Rough estimate if published_at missing (though Deal usually parses it)
                                      # Note: created_at is naive or timezone aware? SQLAlchemy usually returns datetime
                                      # Let's trust scraper data mostly.
                                      pass

                                 details["hours_since_posted"] = hours_since_posted

                                 await deals_repo.save_history(
                                     deal.id, 
                                     details,
                                     source="tracker"
                                 )
                                 
                                 deal.last_tracked_at = func.now()
                                 await session.commit()
                             else:
                                 logger.warning(f"👀 Tracker: Could not parse {deal.url}")
                         
                         # Short sleep between items to be nice
                         await asyncio.sleep(random.uniform(2, 5))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"👀 Tracker Error: {e}")

            # Wait 15-30 mins between BATCHES (or less if we have many deals? For now keep it simple)
            # Better: run continuously but sleep if empty.
            if not self.shutdown_event.is_set():
                await self._sleep(random.randint(60, 120)) # check relatively often for next batch

    # --- 3. THE HISTORIAN (Long-term trends) ---
    async def run_historian(self):
        """Scrapes /las-mas-hot every 2-4 hours."""
        logger.info("📜 Historian loop started.")
        while not self.shutdown_event.is_set():
            try:
                logger.info("📜 Historian: Archiving /las-mas-hot ...")
                async with async_session_factory() as session:
                     deals_repo = DealsRepository(session)
                     
                     html = await self.scraper.fetch_page("https://www.promodescuentos.com/las-mas-hot")
                     if html:
                         deals = await asyncio.to_thread(self.scraper.parse_hot_page, html)
                         
                         for d in deals:
                             url = d.get("url")
                             if not url: continue
                             
                             deal = await deals_repo.get_by_url(url)
                             if deal:
                                 # Update Outcome
                                 outcome = await deals_repo.get_outcome(deal.id)
                                 if not outcome:
                                     outcome = DealOutcome(deal_id=deal.id)
                                     session.add(outcome)
                                 
                                 temp = float(d.get("temperature", 0))
                                 current_max = outcome.final_max_temp if outcome.final_max_temp is not None else 0.0
                                 
                                 if temp > current_max:
                                     outcome.final_max_temp = temp
                                 
                                 if temp >= 200: outcome.reached_200 = 1
                                 if temp >= 500: outcome.reached_500 = 1
                                 if temp >= 1000: outcome.reached_1000 = 1
                                 
                                 await session.commit()
                         
                         logger.info(f"📜 Historian: Analyzed {len(deals)} hot deals.")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"📜 Historian Error: {e}")

            if not self.shutdown_event.is_set():
                await self._sleep(random.randint(7200, 14400)) # 2-4 hours

    # --- Helpers ---
    async def _handle_viral_deal(self, deal: dict, analysis: dict, deals_repo: DealsRepository, sub_repo: SubscribersRepository):
        curr_rating = analysis["rating"]
        url = deal.get("url")
        title = deal.get("title")
        
        if not url: return

        max_rating = await deals_repo.get_max_rating(url)
        
        if curr_rating > max_rating:
            logger.info(
                f"🔥 {title} is HOT ({curr_rating})! "
                f"Score: {analysis['final_score']:.1f}"
            )
            
            subs = await sub_repo.get_all()
            targets = set(subs)
            if settings.ADMIN_CHAT_IDS: targets.update(settings.ADMIN_CHAT_IDS)
            
            await deals_repo.update_max_rating(url, curr_rating)
            
            # Prepare notification data
            notification_data = {
                "title": title,
                "url": url,
                "price_display": deal.get("price") if deal.get("price") else "N/D",
                "rating": curr_rating,
                "image_url": deal.get("image_url"),
                "merchant": deal.get("merchant"),
                "description": deal.get("description"),
                "posted_or_updated": "Publicado", 
                "hours_since_posted": deal.get("hours_since_posted", 0.1),
                "temperature": deal.get("temperature", 0)
            }
            
            await self.telegram.send_bulk_notifications(targets, notification_data)

    async def _sleep(self, seconds: int):
        try:
             await asyncio.wait_for(self.shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
