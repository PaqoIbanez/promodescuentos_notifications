
import os
import time
import psycopg2
import logging
import statistics
from dotenv import load_dotenv
from typing import List, Dict, Tuple, Optional

# Load environment variables
load_dotenv()

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [OPTIMIZER] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

DATABASE_URL = os.getenv("DATABASE_URL")

class AutoTuner:
    def __init__(self):
        self.conn = None
        if not DATABASE_URL:
            logging.error("DATABASE_URL no encontrada en .env")
            return

    def get_db_connection(self):
        try:
            return psycopg2.connect(DATABASE_URL)
        except Exception as e:
            logging.error(f"Error conectando a BD: {e}")
            return None

    def calculate_percentile(self, data: List[float], percentile: int) -> float:
        if not data:
            return 0.0
        try:
            # statistics.quantiles requiere Python 3.8+
            # n=100 significa percentiles 1..99
            quantiles = statistics.quantiles(data, n=100)
            return quantiles[percentile - 1]
        except AttributeError:
             # Fallback simple para versiones viejas
             data.sorted()
             idx = int(len(data) * (percentile / 100))
             return data[idx]

    def optimize(self):
        logging.info("🧠 Iniciando ciclo de optimización...")
        conn = self.get_db_connection()
        if not conn:
            return

        try:
            cur = conn.cursor()

            # 1. Identificar Ganadores (Winners > 100° y Super Winners > 200°)
            # Usamos una subquery para encontrar el max temp por deal
            logging.info("Analizando historial de ofertas...")
            
            # --- ANÁLISIS PARA INSTANT KILL (< 15 min) ---
            # Buscamos ofertas que eventualmente llegaron a > 200° (Super Winners)
            # Y vemos qué velocidad tenían en sus primeros 15 minutos (source='hunter' preferiblemente)
            query_15m = """
            WITH Winners AS (
                SELECT deal_id FROM deal_history GROUP BY deal_id HAVING MAX(temperature) >= 200
            )
            SELECT velocity 
            FROM deal_history 
            WHERE deal_id IN (SELECT deal_id FROM Winners)
              AND hours_since_posted <= 0.25
              AND velocity > 0
            """
            cur.execute(query_15m)
            velocities_15m = [row[0] for row in cur.fetchall()]
            
            # --- ANÁLISIS PARA FAST RISING (< 30 min) ---
            # Buscamos ofertas que llegaron a > 100° (Winners)
            # Y vemos su velocidad en los primeros 30 min
            query_30m = """
            WITH Winners AS (
                SELECT deal_id FROM deal_history GROUP BY deal_id HAVING MAX(temperature) >= 100
            )
            SELECT velocity 
            FROM deal_history 
            WHERE deal_id IN (SELECT deal_id FROM Winners)
              AND hours_since_posted <= 0.5
              AND velocity > 0
            """
            cur.execute(query_30m)
            velocities_30m = [row[0] for row in cur.fetchall()]

            new_config = {}

            # --- CALCULAR UMBRALES (Regla del 80%) ---
            # Queremos capturar al 80% de los ganadores, así que tomamos el percentil 20 de sus velocidades.
            
            if len(velocities_15m) > 10: # Mínimo datos requeridos
                p20_15m = self.calculate_percentile(velocities_15m, 20)
                # Safeguards: No bajar de 1.0 ni subir de 5.0
                suggested_kill = max(1.0, min(5.0, p20_15m))
                new_config["velocity_instant_kill"] = round(suggested_kill, 2)
                logging.info(f"📊 Análisis <15m (N={len(velocities_15m)}): P20={p20_15m:.2f} -> Nuevo 'Instant Kill': {new_config['velocity_instant_kill']}")
            else:
                logging.warning(f"Insuficientes datos para <15m (N={len(velocities_15m)}). Manteniendo config.")

            if len(velocities_30m) > 10:
                p20_30m = self.calculate_percentile(velocities_30m, 20)
                # Safeguards: No bajar de 0.5 ni subir de 3.0
                suggested_rise = max(0.5, min(3.0, p20_30m))
                new_config["velocity_fast_rising"] = round(suggested_rise, 2)
                logging.info(f"📊 Análisis <30m (N={len(velocities_30m)}): P20={p20_30m:.2f} -> Nuevo 'Fast Rising': {new_config['velocity_fast_rising']}")
            else:
                logging.warning(f"Insuficientes datos para <30m (N={len(velocities_30m)}). Manteniendo config.")

            # --- ACTUALIZAR DB ---
            if new_config:
                logging.info(f"💾 Actualizando configuración en BD: {new_config}")
                for key, val in new_config.items():
                    # Upsert config
                    cur.execute("""
                        INSERT INTO system_config (key, value, updated_at) 
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (key) DO UPDATE SET 
                            value = EXCLUDED.value,
                            updated_at = NOW();
                    """, (key, str(val)))
                conn.commit()
                logging.info("✅ Configuración optimizada exitosamente.")
            else:
                logging.info("⏹ No hay cambios suficientes para aplicar.")

            cur.close()
            conn.close()

        except Exception as e:
            logging.error(f"Error en proceso de optimización: {e}")
            if conn: conn.close()

if __name__ == "__main__":
    tuner = AutoTuner()
    tuner.optimize()
