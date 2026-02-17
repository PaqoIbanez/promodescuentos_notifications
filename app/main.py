import logging
import asyncio
import os
import json
import random
import httpx
from contextlib import asynccontextmanager
from typing import Dict, Any, Set
from fastapi import FastAPI, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db.session import engine, async_session_factory, get_db
from app.models.base import Base
from app.repositories.subscribers import SubscribersRepository
from app.repositories.deals import DealsRepository
from app.services.telegram import TelegramService
from app.services.scraper import ScraperService
from app.services.analyzer import AnalyzerService
from app.services.optimizer import AutoTunerService
from app.services.deals import DealsService
from app.dependencies import get_subscribers_repo, get_telegram_service

# Initialize logging
setup_logging()
logger = logging.getLogger(__name__)

# Global state
shutdown_event = asyncio.Event()

async def setup_webhook():
    if settings.APP_BASE_URL and settings.TELEGRAM_BOT_TOKEN:
        webhook_url = f"{settings.APP_BASE_URL.rstrip('/')}/webhook/{settings.TELEGRAM_BOT_TOKEN}"
        try:
            url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook"
            async with httpx.AsyncClient() as client:
                await client.post(url, params={"url": webhook_url}, timeout=10.0)
            logger.info(f"Webhook set to {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")

async def init_db_content():
    """Initializes database with default config and indexes."""
    try:
        async with async_session_factory() as session:
            # 0. Schema Migrations (idempotent)
            logger.info("Running schema migrations...")
            await session.execute(text("ALTER TABLE deal_history ADD COLUMN IF NOT EXISTS viral_score FLOAT DEFAULT 0.0;"))
            
            # 1. Create Indexes (idempotent)
            logger.info("Verifying indexes...")
            await session.execute(text("CREATE INDEX IF NOT EXISTS idx_deal_history_deal_hours ON deal_history(deal_id, hours_since_posted);"))
            await session.execute(text("CREATE INDEX IF NOT EXISTS idx_deals_url ON deals(url);"))
            await session.execute(text("CREATE INDEX IF NOT EXISTS idx_deals_created ON deals(created_at);"))
            
            # 2. Seed Default Config
            logger.info("Seeding default configuration...")
            defaults = [
                # Legacy velocity thresholds
                ('velocity_instant_kill', '4.0'),
                ('velocity_fast_rising', '3.0'),
                ('min_temp_instant_kill', '15'),
                ('min_temp_fast_rising', '30'),
                # Advanced Scoring Engine
                ('viral_threshold', '50.0'),
                ('min_seed_temp', '15.0'),
                ('gravity', '1.2'),
                ('score_tier_4', '500.0'),
                ('score_tier_3', '200.0'),
                ('score_tier_2', '100.0'),
            ]
            for key, val in defaults:
                await session.execute(
                    text("INSERT INTO system_config (key, value) VALUES (:key, :val) ON CONFLICT (key) DO NOTHING"),
                    {"key": key, "val": val}
                )
            await session.commit()
            logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")

async def run_migration():
    """Migrates subscribers.json to PostgreSQL if it exists."""
    json_path = "subscribers.json"
    if os.path.exists(json_path):
        logger.info(f"Detectado archivo legado {json_path}. Iniciando migración...")
        try:
            async with async_session_factory() as session:
                sub_repo = SubscribersRepository(session)
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        count = 0
                        for chat_id in data:
                            if await sub_repo.add(str(chat_id)):
                                count += 1
                        logger.info(f"Migrados {count} suscriptores a la BD.")
                    else:
                        logger.warning("Formato de subscribers.json inválido (no es lista).")
                
                os.rename(json_path, json_path + ".bak")
                logger.info(f"Archivo {json_path} renombrado a .bak")
        except Exception as e:
            logger.error(f"Error durante migración: {e}")

async def scraper_loop(scraper_service: ScraperService, telegram_service: TelegramService):
    logger.info("Starting scraper loop...")
    iteration_count = 0
    consecutive_failures = 0
    max_consecutive_failures = 3
    
    # Run optimizer once on startup
    try:
        async with async_session_factory() as session:
            deals_repo = DealsRepository(session)
            optimizer = AutoTunerService(deals_repo)
            await optimizer.optimize()
            await session.commit() # Ensure commit if needed by AutoTuner (it handles its own commits now, but good practice)
    except Exception as e:
        logger.error(f"Startup optimizer failed: {e}")

    # Initial Analyzer config
    analyzer = AnalyzerService({})
    try:
        async with async_session_factory() as session:
             deals_repo = DealsRepository(session)
             initial_config = await deals_repo.get_system_config()
             analyzer.update_config(initial_config)
    except Exception as e:
        logger.error(f"Error loading initial config: {e}")

    while not shutdown_event.is_set():
        iteration_count += 1
        logger.info(f"=== Iteration #{iteration_count} ===")
        
        # New session for each iteration to ensure fresh state and prevent long-lived internal transaction state
        async with async_session_factory() as session:
            deals_repo = DealsRepository(session)
            sub_repo = SubscribersRepository(session)
            
            # Reload config every ~6 iterations
            if iteration_count % 6 == 0:
                new_config = await deals_repo.get_system_config()
                analyzer.update_config(new_config)

            # --- Hunter Mode ---
            html = await scraper_service.fetch_page("https://www.promodescuentos.com/nuevas")
            
            if html:
                consecutive_failures = 0
                deals = await asyncio.to_thread(scraper_service.parse_deals, html)
                
                # 1. Fetch previous snapshots for acceleration detection (batch)
                deal_urls = [d.get("url") for d in deals if d.get("url")]
                prev_snapshots = await deals_repo.get_latest_snapshots_batch(deal_urls)

                # 2. Analyze all deals first, then harvest + notify hot ones
                new_deals_count = 0
                for deal in deals:
                    url = deal.get("url")
                    if not url:
                        continue
                    
                    # Full viral analysis with acceleration
                    prev_snapshot = prev_snapshots.get(url)
                    analysis = analyzer.analyze_deal(deal, prev_snapshot)
                    viral_score = analysis["final_score"]

                    # Atomic "Unit of Work" save (with viral_score)
                    deals_service = DealsService(deals_repo)
                    await deals_service.process_new_deal(deal, viral_score=viral_score)

                    # Hot deal detection
                    if analysis["is_hot"]:
                        curr_rating = analysis["rating"]
                        max_rating = await deals_repo.get_max_rating(url)
                        
                        if curr_rating > max_rating:
                            deal['rating'] = curr_rating
                            logger.info(
                                f"🔥 VIRAL DEAL: {deal.get('title')} "
                                f"({'🔥' * curr_rating} score={viral_score:.1f} "
                                f"accel={analysis['acceleration']:.2f} "
                                f"traffic={analysis['traffic_mult']:.1f})"
                            )
                            
                            subs = await sub_repo.get_all()
                            admins = settings.ADMIN_CHAT_IDS
                            targets = set(subs)
                            if admins: targets.update(admins)

                            # 1. Update DB FIRST (Persistence)
                            await deals_repo.update_max_rating(url, curr_rating)
                            await session.commit()
                            new_deals_count += 1

                            # 2. Fire & Forget Notifications (Parallel)
                            await telegram_service.send_bulk_notifications(targets, deal)
                
                logger.info(f"Found {new_deals_count} new/upgraded viral deals.")

            else:
                consecutive_failures += 1
                logger.warning(f"Failed to fetch deals. Failures: {consecutive_failures}")
            
            if consecutive_failures >= max_consecutive_failures:
                logger.error("Max failures reached. Exiting loop.")
                break

        # Wait
        if not shutdown_event.is_set():
            wait_time = random.randint(300, 720)
            logger.info(f"Sleeping for {wait_time}s...")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=wait_time)
            except asyncio.TimeoutError:
                pass # Timeout reached, continue loop

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing services...")
    
    # Initialize services
    scraper_service = ScraperService()
    telegram_service = TelegramService()
    
    # Attach to app state for dependency injection
    app.state.scraper_service = scraper_service
    app.state.telegram_service = telegram_service
    
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    await run_migration()
    await init_db_content()
    await setup_webhook()
    await scraper_service.startup()

    # Pass services explicitly to the background loop
    loop_task = asyncio.create_task(scraper_loop(scraper_service, telegram_service))

    yield
    
    # Shutdown
    logger.info("Shutting down services...")
    shutdown_event.set()
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
        
    await telegram_service.close()
    await scraper_service.close()
    await engine.dispose()
    logger.info("Shutdown complete.")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "running", "service": "promodescuentos-bot"}

@app.get("/health")
async def health_check(session: AsyncSession = Depends(get_db)):
    # Verify DB connection
    try:
        await session.execute(text("SELECT 1"))
        return {"status": "healthy", "db": "connected"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail="Database connectivity failed")

@app.post(f"/webhook/{settings.TELEGRAM_BOT_TOKEN}")
async def webhook(
    request: Request, 
    sub_repo: SubscribersRepository = Depends(get_subscribers_repo),
    telegram_service: TelegramService = Depends(get_telegram_service)
):
    try:
        update = await request.json()
        if 'message' in update:
            msg = update['message']
            chat_id = str(msg['chat']['id'])
            text = msg.get('text', '').lower()
            
            if text in ['/start', '/subscribe']:
                if await sub_repo.add(chat_id):
                    await telegram_service.send_message(chat_id, text="¡Suscrito! 🎉 Recibirás ofertas calientes.")
                else:
                    await telegram_service.send_message(chat_id, text="Ya estás suscrito.")
            elif text in ['/stop', '/unsubscribe']:
                await sub_repo.remove(chat_id)
                await telegram_service.send_message(chat_id, text="Suscripción cancelada.")
            else:
                 await telegram_service.send_message(chat_id, text="Usa /start para suscribirte o /stop para cancelar.")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Internal Error")
