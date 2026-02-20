import math
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Mexico City timezone offsets for traffic shaping
# We use hour-of-day (0-23) in America/Mexico_City (UTC-6)
TRAFFIC_MULTIPLIERS = {
    # Off-peak: impressive gains with low traffic
    range(0, 7): 1.5,
    # Morning ramp-up
    range(7, 9): 1.2,
    # Peak hours: standard difficulty
    range(9, 22): 1.0,
    # Late night wind-down
    range(22, 24): 1.3,
}


def _get_traffic_multiplier(hour: int) -> float:
    """Returns traffic multiplier based on hour of day (Mexico City time)."""
    for hour_range, multiplier in TRAFFIC_MULTIPLIERS.items():
        if hour in hour_range:
            return multiplier
    return 1.0


class AnalyzerService:
    def __init__(self, system_config: Dict[str, float]):
        self.config = system_config

    def update_config(self, new_config: Dict[str, float]):
        self.config = new_config

    def is_deal_invalid(self, deal: Dict[str, Any]) -> bool:
        """Check if deal is clearly invalid/expired."""
        posted_text = deal.get("posted_text", "")
        if "Expiró" in posted_text:
            return True
        return False

    def calculate_viral_score(self, deal: Dict[str, Any]) -> float:
        """
        Gravity-based viral score similar to HackerNews ranking.
        
        Formula: (temp - 1) / (hours + offset)^gravity
        
        A deal with 50° in 10 minutes scores exponentially higher than 
        one with 50° in 5 hours. The gravity parameter controls how 
        aggressively we penalize aging deals.
        """
        temp = float(deal.get("temperature", 0))
        
        # Anti-noise gate: require minimum "seed capital"
        min_seed = self.config.get("min_seed_temp", 15.0)
        if temp < min_seed:
            return 0.0

        hours = float(deal.get("hours_since_posted", 0))
        
        # Small offset prevents division by zero for brand-new deals
        # and gives ~6 min of grace period
        offset = 0.1
        
        # Gravity: how aggressively time penalizes the score
        # 1.2 = gentle (good for early detection)
        # 1.8 = harsh (HackerNews standard, better for ranking established posts)
        gravity = self.config.get("gravity", 1.2)
        
        score = (temp - 1) / pow(hours + offset, gravity)
        return round(score, 2)

    def calculate_acceleration(
        self, 
        current_temp: float, 
        current_hours: float, 
        prev_temp: Optional[float], 
        prev_hours: Optional[float]
    ) -> float:
        """
        Detects if the rate of temperature gain is increasing (2nd derivative).
        
        Compares velocity of the current snapshot vs the previous snapshot.
        Returns a multiplier:
          1.0 = steady growth
          >1.0 = accelerating (votes coming in faster)
          <1.0 = decelerating
        
        Capped between 0.5 and 3.0 to prevent outlier distortion.
        """
        if prev_temp is None or prev_hours is None:
            return 1.0  # No previous data, assume steady
        
        # Time delta between snapshots
        delta_hours = current_hours - prev_hours
        if delta_hours <= 0.01:  # Less than ~36 seconds apart
            return 1.0
        
        # Temperature gained in this interval
        delta_temp = current_temp - prev_temp
        if delta_temp <= 0:
            return 0.5  # Temperature dropped or flat = decelerating
        
        # Current interval velocity (degrees per hour)
        current_velocity = delta_temp / delta_hours
        
        # Overall average velocity up to previous snapshot
        prev_minutes = max(1, prev_hours * 60)
        prev_avg_velocity = prev_temp / prev_minutes  # deg/min for consistency
        
        if prev_avg_velocity <= 0:
            return 1.5  # No prior velocity but gaining now = mildly accelerating
        
        # Convert current to same units (deg/min)
        current_velocity_min = current_velocity / 60
        
        # Dampen historical velocity to avoid huge multipliers from noise
        # A tiny historical velocity (e.g. 0.1 deg/min) makes any newly gained
        # 3 degrees look like a massive acceleration.
        damped_prev_avg = max(prev_avg_velocity, 0.5)
        
        # Ratio: how much faster is the current interval vs historical average
        ratio = current_velocity_min / damped_prev_avg
        
        # Restrict maximum multiplier based on absolute temperature change
        # A tiny jump of 3 degrees shouldn't give a 3x multiplier.
        if delta_temp < 5:
            max_mult = 1.1
        elif delta_temp < 15:
            max_mult = 1.5
        else:
            max_mult = 3.0
            
        # Clamp to prevent noise from dominating
        return max(0.5, min(max_mult, ratio))

    def get_current_mexico_hour(self) -> int:
        """Returns current hour in Mexico City timezone (UTC-6)."""
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("America/Mexico_City"))
            return now.hour
        except Exception:
            # Fallback: assume UTC-6
            from datetime import timezone, timedelta
            utc_now = datetime.now(timezone.utc)
            mx_now = utc_now + timedelta(hours=-6)
            return mx_now.hour

    def analyze_deal(
        self, 
        deal: Dict[str, Any], 
        prev_snapshot: Optional[Tuple[float, float]] = None
    ) -> Dict[str, Any]:
        """
        Full analysis pipeline for a deal.
        
        Returns a dict with:
          - viral_score: raw gravity-based score
          - acceleration: velocity change multiplier  
          - traffic_mult: time-of-day bonus
          - final_score: viral_score × traffic_mult × acceleration
          - is_hot: whether final_score exceeds threshold
          - rating: fire rating (1-4)
        
        prev_snapshot: Optional (temperature, hours_since_posted) from deal_history
        """
        if self.is_deal_invalid(deal):
            return {
                "viral_score": 0.0, "acceleration": 1.0, "traffic_mult": 1.0,
                "final_score": 0.0, "is_hot": False, "rating": 0
            }

        temp = float(deal.get("temperature", 0))
        hours = float(deal.get("hours_since_posted", 0))

        # 1. Base viral score (gravity model)
        viral_score = self.calculate_viral_score(deal)
        
        # 2. Acceleration bonus
        prev_temp = prev_snapshot[0] if prev_snapshot else None
        prev_hours = prev_snapshot[1] if prev_snapshot else None
        acceleration = self.calculate_acceleration(temp, hours, prev_temp, prev_hours)
        
        # 3. Traffic shaping
        mexico_hour = self.get_current_mexico_hour()
        traffic_mult = _get_traffic_multiplier(mexico_hour)
        
        # 4. Final composite score
        final_score = round(viral_score * traffic_mult * acceleration, 2)
        
        # 4.b Old & Cold Penalty (Heuristic Hotfix)
        # Destroy the score of deals that have no statistical chance of reaching 500
        if hours >= 2.0 and temp < 100:
            final_score = round(final_score * 0.2, 2)
        elif hours >= 1.0 and temp < 50:
            final_score = round(final_score * 0.2, 2)
            
        
        # 5. Hot detection
        threshold = self.config.get("viral_threshold", 50.0)
        is_hot = final_score >= threshold
        
        # 6. Rating tiers (score-based)
        rating = self._score_to_rating(final_score)
        
        return {
            "viral_score": viral_score,
            "acceleration": round(acceleration, 2),
            "traffic_mult": traffic_mult,
            "final_score": final_score,
            "is_hot": is_hot,
            "rating": rating,
        }

    def _score_to_rating(self, score: float) -> int:
        """Converts final score to fire rating (1-4)."""
        tier4 = self.config.get("score_tier_4", 500.0)
        tier3 = self.config.get("score_tier_3", 200.0)
        tier2 = self.config.get("score_tier_2", 100.0)
        
        if score >= tier4:
            return 4
        elif score >= tier3:
            return 3
        elif score >= tier2:
            return 2
        elif score > 0:
            return 1
        return 0

    # --- Legacy compatibility methods ---

    def is_deal_hot(self, deal: Dict[str, Any], prev_snapshot: Optional[Tuple[float, float]] = None) -> bool:
        """Legacy-compatible hot check. Now delegates to analyze_deal."""
        result = self.analyze_deal(deal, prev_snapshot)
        return result["is_hot"]

    def calculate_rating(self, deal: Dict[str, Any], prev_snapshot: Optional[Tuple[float, float]] = None) -> int:
        """Legacy-compatible rating. Now delegates to analyze_deal."""
        result = self.analyze_deal(deal, prev_snapshot)
        return result["rating"]
