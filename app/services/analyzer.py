import os
import math
import logging
import numpy as np
import joblib
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Mexico City timezone offsets for traffic shaping
TRAFFIC_MULTIPLIERS = {
    range(0, 7): 1.5,
    range(7, 9): 1.2,
    range(9, 22): 1.0,
    range(22, 24): 1.3,
}

def _get_traffic_multiplier(hour: int) -> float:
    for hour_range, multiplier in TRAFFIC_MULTIPLIERS.items():
        if hour in hour_range:
            return multiplier
    return 1.0

class AnalyzerService:
    def __init__(self, system_config: Dict[str, float]):
        self.config = system_config
        self.model = self._load_model()

    def _load_model(self):
        """Carga el modelo XGBoost desde el disco si existe."""
        model_path = os.path.join(os.getcwd(), "xgb_model.joblib")
        if os.path.exists(model_path):
            try:
                logger.info(f"Cargando modelo ML predictivo desde {model_path}...")
                return joblib.load(model_path)
            except Exception as e:
                logger.error(f"Error cargando el modelo ML: {e}")
        else:
            logger.warning(f"No se encontró {model_path}. Funcionando en modo Heurístico tradicional.")
        return None

    def update_config(self, new_config: Dict[str, float]):
        self.config = new_config
        # Si queremos recargar el modelo en caliente en el futuro, se podría hacer aquí

    def is_deal_invalid(self, deal: Dict[str, Any]) -> bool:
        posted_text = deal.get("posted_text", "")
        if "Expiró" in posted_text:
            return True
        return False

    def calculate_viral_score(self, deal: Dict[str, Any]) -> float:
        temp = float(deal.get("temperature", 0))
        min_seed = self.config.get("min_seed_temp", 15.0)
        
        if temp < min_seed:
            return 0.0

        hours = float(deal.get("hours_since_posted", 0))
        
        # 1. Suavizado de Laplace (Laplace Smoothing)
        # Sumamos 0.5 horas (30 mins) al denominador para evitar que 
        # las ofertas de 1 minuto tengan velocidades infinitas absurdas.
        smoothed_velocity = temp / (hours + 0.5)
        
        # 2. Decaimiento Logarítmico
        # math.log2(hours + 2) penaliza naturalmente el envejecimiento de la oferta
        # sin necesidad de reglas if-else destructivas.
        score = smoothed_velocity / math.log2(hours + 2)
        
        return round(score, 2)

    def calculate_acceleration(
        self, current_temp: float, current_hours: float, 
        prev_temp: Optional[float], prev_hours: Optional[float]
    ) -> float:
        if prev_temp is None or prev_hours is None:
            return 1.0
        
        delta_hours = current_hours - prev_hours
        if delta_hours <= 0.05: # Prevenir divisiones por cero (micro-ruido)
            return 1.0
        
        delta_temp = current_temp - prev_temp
        if delta_temp <= 0:
            return 0.5 # La oferta se congeló o perdió grados
        
        # Calculamos velocidad reciente vs velocidad histórica
        current_velocity = delta_temp / delta_hours
        historical_velocity = prev_temp / max(0.1, prev_hours)
        
        if historical_velocity <= 0:
            return 1.0
            
        ratio = current_velocity / historical_velocity
        
        # 3. Factor de Confianza (Volumen Real)
        # Amortigua el "ruido" matemáticamente. Si solo subió 2 grados, 
        # la confianza es bajísima (0.13), matando el multiplicador automáticamente.
        # Si subió > 15 grados de golpe, la confianza es plena (1.0).
        confidence = min(1.0, delta_temp / 15.0)
        
        # 4. Tangente Hiperbólica (Límites suaves y continuos)
        # En lugar de usar if/else rígidos, tanh aplana la curva suavemente
        # entre -1 y 1. Esto nos garantiza matemáticamente que el multiplicador
        # jamás se saldrá de control.
        raw_accel = 1.0 + (math.tanh(ratio - 1.0) * confidence)
        
        # Acotamos los límites finales de seguridad (entre 0.5x y 2.0x)
        return round(max(0.5, min(2.0, raw_accel)), 2)

    def get_current_mexico_hour(self) -> int:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("America/Mexico_City"))
            return now.hour
        except Exception:
            from datetime import timezone, timedelta
            utc_now = datetime.now(timezone.utc)
            mx_now = utc_now + timedelta(hours=-6)
            return mx_now.hour

    def analyze_deal(
        self, deal: Dict[str, Any], prev_snapshot: Optional[Tuple[float, float]] = None
    ) -> Dict[str, Any]:
        
        if self.is_deal_invalid(deal):
            return {
                "viral_score": 0.0, "acceleration": 1.0, "traffic_mult": 1.0,
                "final_score": 0.0, "is_hot": False, "rating": 0, "ml_probability": 0.0
            }

        temp = float(deal.get("temperature", 0))
        hours = float(deal.get("hours_since_posted", 0))

        # 1. Base viral score (Heurística)
        viral_score = self.calculate_viral_score(deal)
        prev_temp = prev_snapshot[0] if prev_snapshot else None
        prev_hours = prev_snapshot[1] if prev_snapshot else None
        acceleration = self.calculate_acceleration(temp, hours, prev_temp, prev_hours)
        mexico_hour = self.get_current_mexico_hour()
        traffic_mult = _get_traffic_multiplier(mexico_hour)
        
        final_score = round(viral_score * traffic_mult * acceleration, 2)
        
        # Heurística Old & Cold
        if hours >= 2.0 and temp < 100:
            final_score = round(final_score * 0.2, 2)
        elif hours >= 1.0 and temp < 50:
            final_score = round(final_score * 0.2, 2)
            
        threshold = self.config.get("viral_threshold", 50.0)
        is_hot_heuristic = final_score >= threshold
        rating = self._score_to_rating(final_score)

        # --- 2. PREDICCIÓN MACHINE LEARNING (REGRESIÓN LOGARÍTMICA) ---
        predicted_max_temp = 0.0
        ml_probability = 0.0
        is_hot_ml = False
        rating_ml = 0

        if self.model is not None and hours >= 0.16:
            try:
                velocity = temp / max(1, hours * 60)
                hour_sin = np.sin(2 * np.pi * mexico_hour / 24)
                hour_cos = np.cos(2 * np.pi * mexico_hour / 24)
                dow = datetime.now().weekday() + 1 
                
                features = np.array([[temp, velocity, hour_sin, hour_cos, dow]])
                
                # 1. El modelo devuelve la predicción comprimida (ej. 6.2)
                predicted_log = float(self.model.predict(features)[0])
                
                # 2. Descomprimimos usando la exponencial para obtener los grados reales
                predicted_max_temp = float(np.expm1(predicted_log))
                
                threshold = self.config.get("viral_threshold", 50.0)
                if predicted_max_temp >= threshold:
                    is_hot_ml = True
                    if predicted_max_temp >= 500.0:
                        rating_ml = 4  
                    elif predicted_max_temp >= 200.0:
                        rating_ml = 3  
                    elif predicted_max_temp >= 100.0:
                        rating_ml = 2  
                    else:
                        rating_ml = 1  
                    
            except Exception as e:
                logger.error(f"Error procesando predicción ML para {deal.get('url')}: {e}")

        # --- DECISIÓN FINAL UNIFICADA (ML EN MODO SOMBRA) ---
        # 1. La heurística recupera el control absoluto de Telegram
        final_is_hot = is_hot_heuristic
        final_rating = rating
        
        # 2. El ML solo se usa como observador en los logs
        if self.model is not None and hours >= 0.16:
            if is_hot_ml:
                # Usamos una etiqueta [SHADOW ML] para monitorearlo sin que dispare mensajes
                logger.info(f"🤖 [SHADOW ML] Proyecta {predicted_max_temp:.1f}° -> {deal.get('title')}")

        return {
            "viral_score": viral_score,
            "acceleration": round(acceleration, 2),
            "traffic_mult": traffic_mult,
            "final_score": final_score,
            "is_hot": final_is_hot,
            "rating": final_rating,
            "ml_probability": round(predicted_max_temp, 2)
        }

    def _score_to_rating(self, score: float) -> int:
        tier4 = self.config.get("score_tier_4", 500.0)
        tier3 = self.config.get("score_tier_3", 200.0)
        tier2 = self.config.get("score_tier_2", 100.0)
        
        if score >= tier4: return 4
        elif score >= tier3: return 3
        elif score >= tier2: return 2
        elif score > 0: return 1
        return 0

    def is_deal_hot(self, deal: Dict[str, Any], prev_snapshot: Optional[Tuple[float, float]] = None) -> bool:
        result = self.analyze_deal(deal, prev_snapshot)
        return result["is_hot"]

    def calculate_rating(self, deal: Dict[str, Any], prev_snapshot: Optional[Tuple[float, float]] = None) -> int:
        result = self.analyze_deal(deal, prev_snapshot)
        return result["rating"]