
import os
import psycopg2
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("Error: DATABASE_URL not found in .env")
    exit(1)

def init_db():
    print("Conectando a la base de datos...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        print("Creando tablas si no existen...")
        
        # 1. Deals Table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id SERIAL PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            merchant TEXT,
            image_url TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """)
        
        # 2. Deal History Table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS deal_history (
            id SERIAL PRIMARY KEY,
            deal_id INTEGER REFERENCES deals(id),
            temperature FLOAT,
            velocity FLOAT,
            hours_since_posted FLOAT,
            source TEXT,
            recorded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """)
        
        # 3. System Config Table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """)
        
        # 4. Insert Default Config
        print("Insertando configuración por defecto...")
        defaults = [
            ('velocity_instant_kill', '1.7'),
            ('velocity_fast_rising', '1.1'),
            ('min_temp_instant_kill', '15'),
            ('min_temp_fast_rising', '30')
        ]
        
        for key, val in defaults:
            cur.execute("""
                INSERT INTO system_config (key, value) 
                VALUES (%s, %s) 
                ON CONFLICT (key) DO NOTHING;
            """, (key, val))
            
        conn.commit()
        cur.close()
        conn.close()
        print("¡Base de datos inicializada exitosamente! 🚀")
        
    except Exception as e:
        print(f"Error inicializando DB: {e}")

if __name__ == "__main__":
    init_db()
