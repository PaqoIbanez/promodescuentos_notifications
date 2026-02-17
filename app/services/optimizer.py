import logging
# import statistics # Removed as no longer needed
from typing import List, Dict, Any
from app.repositories.deals import DealsRepository

logger = logging.getLogger(__name__)

class AutoTunerService:
    def __init__(self, deals_repository: DealsRepository):
        self.deals_repo = deals_repository

    async def optimize(self):
        logger.info("🧠 Iniciando ciclo de optimización (AutoTuner - SQL Optimized)...")
        
        try:
            new_config = {}

            # --- ANALYSIS FOR INSTANT KILL (< 15 min) ---
            # Winners > 200 deg, < 15 min, 20th Percentile (0.2)
            p20_15m = await self.deals_repo.get_velocity_percentile(min_temp=200, hours_window=0.25, percentile=0.2)
            
            if p20_15m > 0:
                suggested_kill = max(1.0, min(5.0, p20_15m))
                new_config["velocity_instant_kill"] = round(suggested_kill, 2)
                logger.info(f"📊 Análisis <15m: P20={p20_15m:.2f} -> Nuevo 'Instant Kill': {new_config['velocity_instant_kill']}")
            else:
                logger.warning(f"Insuficientes datos para <15m o P20 es 0. Manteniendo config.")

            # --- ANALYSIS FOR FAST RISING (< 30 min) ---
            # Winners > 100 deg, < 30 min, 20th Percentile (0.2)
            p20_30m = await self.deals_repo.get_velocity_percentile(min_temp=100, hours_window=0.5, percentile=0.2)

            if p20_30m > 0:
                suggested_rise = max(0.5, min(3.0, p20_30m))
                new_config["velocity_fast_rising"] = round(suggested_rise, 2)
                logger.info(f"📊 Análisis <30m: P20={p20_30m:.2f} -> Nuevo 'Fast Rising': {new_config['velocity_fast_rising']}")
            else:
                logger.warning(f"Insuficientes datos para <30m o P20 es 0. Manteniendo config.")

            # --- UPDATE DB ---
            if new_config:
                logger.info(f"💾 Actualizando configuración en BD: {new_config}")
                success = await self.deals_repo.update_system_config_bulk(new_config)
                if success:
                    await self.deals_repo.session.commit() # Unit of Work: Commit explicitly
                    logger.info("✅ Configuración optimizada exitosamente.")
            else:
                logger.info("⏹ No hay cambios suficientes para aplicar.")

        except Exception as e:
            logger.error(f"Error en proceso de optimización: {e}")
