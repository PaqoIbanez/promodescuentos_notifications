from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.repositories.subscribers import SubscribersRepository
from app.repositories.deals import DealsRepository
from app.services.telegram import TelegramService
from app.services.scraper import ScraperService

async def get_subscribers_repo(session: AsyncSession = Depends(get_db)) -> SubscribersRepository:
    return SubscribersRepository(session)

async def get_deals_repo(session: AsyncSession = Depends(get_db)) -> DealsRepository:
    return DealsRepository(session)

def get_telegram_service(request: Request) -> TelegramService:
    return request.app.state.telegram_service

def get_scraper_service(request: Request) -> ScraperService:
    return request.app.state.scraper_service
