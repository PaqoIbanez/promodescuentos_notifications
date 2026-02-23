import asyncio
import sys
import os
import logging
import pandas as pd
import numpy as np
import joblib
from xgboost import XGBRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

sys.path.append(os.getcwd())

from app.core.logging_config import setup_logging
from app.db.session import async_session_factory
from app.repositories.deals import DealsRepository

setup_logging()
logger = logging.getLogger(__name__)

async def extract_and_train():
    logger.info("Iniciando extracción de features desde la Base de Datos para REGRESIÓN LOGARÍTMICA...")
    
    async with async_session_factory() as session:
        deals_repo = DealsRepository(session)
        raw_data = await deals_repo.get_training_dataset(checkpoint_mins=30)
    
    if not raw_data:
        logger.error("No hay suficientes datos.")
        return

    df = pd.DataFrame(raw_data)
    
    df['hour_of_day'] = df['hour_of_day'].astype(float)
    df['dow'] = df['dow'].astype(float)

    df['hour_sin'] = np.sin(2 * np.pi * df['hour_of_day'] / 24)
    df['hour_cos'] = np.cos(2 * np.pi * df['hour_of_day'] / 24)
    
    features = [
        'temp_at_checkpoint', 
        'velocity_at_checkpoint', 
        'hour_sin', 
        'hour_cos', 
        'dow'
    ]
    
    target = 'final_max_temp' 

    if target not in df.columns:
        logger.error(f"Falta la columna objetivo: {target}. Columnas: {df.columns.tolist()}")
        return

    X = df[features]
    
    # --- LA MAGIA: Transformación Logarítmica ---
    # Comprime los valores atípicos para que no vuelvan loco al modelo
    y_log = np.log1p(df[target].astype(float))

    X_train, X_test, y_train_log, y_test_log = train_test_split(X, y_log, test_size=0.2, random_state=42)

    logger.info("Entrenando modelo XGBoost Regressor...")
    
    model = XGBRegressor(
        n_estimators=150,
        max_depth=4,           
        learning_rate=0.05,    
        random_state=42,
        subsample=0.8,         # Ayuda a evitar que el modelo memorice
        colsample_bytree=0.8   # Fuerzas al modelo a no depender de una sola variable
    )
    
    model.fit(X_train, y_train_log)

    # 3. Evaluar el modelo (Revirtiendo el logaritmo)
    predictions_log = model.predict(X_test)
    
    # Revertimos a grados normales para entender el error real
    predictions_real = np.expm1(predictions_log)
    y_test_real = np.expm1(y_test_log)
    
    mae = mean_absolute_error(y_test_real, predictions_real)
    r2_log = r2_score(y_test_log, predictions_log)
    
    logger.info(f"MAE (Error Absoluto Medio Real): +- {mae:.2f} grados")
    logger.info(f"R2 Score (En mundo comprimido): {r2_log:.2f} (Entre más cerca de 1.0, mejor)")

    model_path = os.path.join(os.getcwd(), "xgb_model.joblib")
    joblib.dump(model, model_path)
    logger.info(f"Modelo LOGARÍTMICO entrenado y guardado en: {model_path}")

if __name__ == "__main__":
    asyncio.run(extract_and_train())