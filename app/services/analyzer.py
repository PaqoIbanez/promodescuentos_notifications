import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class AnalyzerService:
    def __init__(self, system_config: Dict[str, float]):
        self.config = system_config

    def update_config(self, new_config: Dict[str, float]):
        self.config = new_config

    def is_deal_invalid(self, deal: Dict[str, Any]) -> bool:
        """
        Check if deal is clearly invalid/expired.
        Returns True if invalid.
        """
        posted_text = deal.get("posted_text", "")
        if "Expiró" in posted_text:
            return True
        return False

    def is_deal_hot(self, deal: Dict[str, Any]) -> bool:
        """
        Determines if a deal is worthy of notification based on temperature and time.
        """
        if self.is_deal_invalid(deal):
            return False
            
        temp = float(deal.get("temperature", 0))
        hours = float(deal.get("hours_since_posted", 999))
        minutes = max(1, hours * 60)
        velocity = temp / minutes

        # 1. Dynamic Checks (Configurable via DB -> system_config)
        # Instant Kill
        vel_instant = self.config.get("velocity_instant_kill", 1.7)
        min_temp_instant = self.config.get("min_temp_instant_kill", 15.0)
        if minutes <= 15 and velocity >= vel_instant and temp >= min_temp_instant:
            logger.info(f"HOT: Instant Kill! {deal.get('url')}")
            return True

        # Fast Rising
        vel_fast = self.config.get("velocity_fast_rising", 1.1)
        min_temp_fast = self.config.get("min_temp_fast_rising", 30.0)
        if minutes <= 30 and velocity >= vel_fast and temp >= min_temp_fast:
             logger.info(f"HOT: Fast Rising! {deal.get('url')}")
             return True

        # 2. Static Rules (Legacy)
        if temp >= 150 and hours < 1: return True
        if temp >= 300 and hours < 2: return True
        if temp >= 500 and hours < 5: return True
        if temp >= 1000 and hours < 8: return True

        return False

    def calculate_rating(self, deal: Dict[str, Any]) -> int:
        """Calculates fire rating (1-4)."""
        temp = float(deal.get("temperature", 0))
        hours = float(deal.get("hours_since_posted", 0))
        minutes = max(1, hours * 60)
        velocity = temp / minutes

        if minutes <= 30 and velocity >= 1.2:
            return 4

        if temp < 300 and hours < 2:
            if hours < 0.5: return 4
            if hours < 1: return 3
            if hours < 1.5: return 2
            return 1
        else:
            if temp >= 1000: return 4
            if temp >= 500: return 3
            if temp >= 300: return 2
            return 1
