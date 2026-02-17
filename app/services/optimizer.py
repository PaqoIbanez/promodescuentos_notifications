import logging
from typing import Dict, Any
from app.repositories.deals import DealsRepository

logger = logging.getLogger(__name__)

class AutoTunerService:
    def __init__(self, deals_repository: DealsRepository):
        self.deals_repo = deals_repository

    async def _safe_query(self, coro, label: str, default=None):
        """Wraps a query in a SAVEPOINT so failures don't poison the transaction."""
        try:
            nested = await self.deals_repo.session.begin_nested()
            result = await coro
            await nested.commit()
            return result
        except Exception as e:
            await nested.rollback()
            logger.warning(f"⚠️ {label}: Query failed (non-fatal): {e}")
            return default

    async def optimize(self):
        logger.info("🧠 Iniciando ciclo de optimización (AutoTuner v2 — Viral Score Engine)...")
        
        try:
            new_config = {}

            # --- 1. LEGACY: Velocity Percentiles (backwards compatible) ---
            p20_15m = await self._safe_query(
                self.deals_repo.get_velocity_percentile(min_temp=200, hours_window=0.25, percentile=0.2),
                "Velocity P20 <15m", default=0.0
            )
            if p20_15m > 0:
                new_config["velocity_instant_kill"] = round(max(1.0, min(5.0, p20_15m)), 2)
                logger.info(f"📊 Legacy <15m: P20={p20_15m:.2f} -> velocity_instant_kill: {new_config['velocity_instant_kill']}")

            p20_30m = await self._safe_query(
                self.deals_repo.get_velocity_percentile(min_temp=100, hours_window=0.5, percentile=0.2),
                "Velocity P20 <30m", default=0.0
            )
            if p20_30m > 0:
                new_config["velocity_fast_rising"] = round(max(0.5, min(3.0, p20_30m)), 2)
                logger.info(f"📊 Legacy <30m: P20={p20_30m:.2f} -> velocity_fast_rising: {new_config['velocity_fast_rising']}")

            # --- 2. NEW: Viral Score Threshold ---
            viral_p20 = await self._safe_query(
                self.deals_repo.get_viral_score_percentile(
                    min_final_temp=200, hours_window=1.0, percentile=0.2
                ),
                "Viral Score P20", default=0.0
            )
            if viral_p20 > 0:
                suggested_threshold = round(max(10.0, min(500.0, viral_p20)), 2)
                new_config["viral_threshold"] = suggested_threshold
                logger.info(f"🧬 Viral Score P20 (winners 200°+, <1h): {viral_p20:.2f} -> viral_threshold: {suggested_threshold}")
            else:
                logger.info("🧬 Insufficient viral_score data for threshold tuning. Using defaults.")

            # --- 3. GOLDEN RATIO ANALYSIS (Observability) ---
            checkpoints = [
                (0.25, 20, 200, "15min/20°→200°"),
                (0.25, 30, 500, "15min/30°→500°"),
                (0.5, 30, 200, "30min/30°→200°"),
                (0.5, 50, 500, "30min/50°→500°"),
                (1.0, 50, 200, "1h/50°→200°"),
            ]
            
            for checkpoint_hours, min_temp, success_temp, label in checkpoints:
                stats = await self._safe_query(
                    self.deals_repo.get_golden_ratio_stats(
                        checkpoint_hours=checkpoint_hours,
                        min_temp_at_checkpoint=min_temp,
                        success_temp=success_temp
                    ),
                    f"Golden Ratio [{label}]",
                    default={"sample_size": 0, "successes": 0, "probability": 0.0}
                )
                if stats["sample_size"] >= 5:
                    logger.info(
                        f"🎯 Golden Ratio [{label}]: "
                        f"{stats['probability']:.1f}% success "
                        f"({stats['successes']}/{stats['sample_size']} deals)"
                    )
                else:
                    logger.debug(f"🎯 Golden Ratio [{label}]: Insufficient data ({stats['sample_size']} samples)")

            # --- 4. UPDATE DB ---
            if new_config:
                logger.info(f"💾 Actualizando configuración en BD: {new_config}")
                success = await self.deals_repo.update_system_config_bulk(new_config)
                if success:
                    await self.deals_repo.session.commit()
                    logger.info("✅ Configuración optimizada exitosamente.")
            else:
                logger.info("⏹ No hay cambios suficientes para aplicar.")

        except Exception as e:
            logger.error(f"Error en proceso de optimización: {e}")
