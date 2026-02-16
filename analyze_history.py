
import csv
import statistics
from datetime import datetime
from collections import defaultdict

HISTORY_FILE = "deals_history.csv"

def analyze_history():
    print(f"--- Analizando {HISTORY_FILE} ---")
    
    deals_by_url = defaultdict(list)
    
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                deals_by_url[row["url"]].append(row)
    except FileNotFoundError:
        print("Archivo no encontrado.")
        return

    print(f"Total de ofertas únicas rastreadas: {len(deals_by_url)}")

    # Categorías de éxito
    winners_100 = [] # Llegaron a > 100°
    winners_200 = [] # Llegaron a > 200°
    losers = []      # Nunca pasaron de 50° y tienen al menos 1 hora de datos o > 5 horas de antigüedad

    # Métricas para análisis temprano (0-15 min, 15-30 min)
    early_stats = {
        "winners_100": {"vel_15m": [], "vel_30m": []},
        "winners_200": {"vel_15m": [], "vel_30m": []},
        "losers":      {"vel_15m": [], "vel_30m": []}
    }

    for url, history in deals_by_url.items():
        # Calcular max temperatura alcanzada
        temps = [float(h["temperature"]) for h in history]
        max_temp = max(temps)
        min_hours = min([float(h["hours_since_posted"]) for h in history])
        max_hours = max([float(h["hours_since_posted"]) for h in history])
        
        category = None
        if max_temp >= 200:
            category = "winners_200"
            winners_200.append(url)
        elif max_temp >= 100:
            category = "winners_100"
            winners_100.append(url)
        elif max_temp < 50 and max_hours > 5.0: # Solo considerar losers confirmados (viejos y fríos)
             category = "losers"
             losers.append(url)
        
        if category:
            # Analizar puntos tempranos
            for h in history:
                hours = float(h["hours_since_posted"])
                velocity = float(h["velocity"])
                
                if hours <= 0.25: # 0-15 min
                    early_stats[category]["vel_15m"].append(velocity)
                if hours <= 0.50: # 0-30 min
                    early_stats[category]["vel_30m"].append(velocity)

    print(f"\n--- Resultados ---")
    print(f"Super Winners (>200°): {len(winners_200)}")
    print(f"Winners (>100°): {len(winners_100)}")
    print(f"Losers (<50° tras 5h): {len(losers)}")

    def print_stats(label, data):
        if not data:
            print(f"{label}: Sin datos suficientes.")
            return
        avg = statistics.mean(data)
        median = statistics.median(data)
        try:
            p90 = statistics.quantiles(data, n=10)[0] # 10th percentile (lo más bajo de los top) - error en python < 3.8, usar sorted
            p10 = sorted(data)[int(len(data)*0.1)]
        except:
             p10 = min(data)

        print(f"{label:<35} | Media: {avg:.4f} | Mediana: {median:.4f} | Min (Top 10%): {p10:.4f}")

    print("\n--- Velocidad en los primeros 15 minutos (< 0.25h) ---")
    print_stats("Winners > 200° (Super Hot)", early_stats["winners_200"]["vel_15m"])
    print_stats("Winners > 100° (Hot)", early_stats["winners_100"]["vel_15m"])
    print_stats("Losers (Cold)", early_stats["losers"]["vel_15m"])

    print("\n--- Velocidad en los primeros 30 minutos (< 0.50h) ---")
    print_stats("Winners > 200° (Super Hot)", early_stats["winners_200"]["vel_30m"])
    print_stats("Winners > 100° (Hot)", early_stats["winners_100"]["vel_30m"])
    print_stats("Losers (Cold)", early_stats["losers"]["vel_30m"])

    # Recomendación
    print("\n--- Recomendación para Umbrales ---")
    
    # Threshold sug. 15m
    w200_15m = early_stats["winners_200"]["vel_15m"]
    w100_15m = early_stats["winners_100"]["vel_15m"]
    l_15m = early_stats["losers"]["vel_15m"]

    if w200_15m:
        rec_15m = sorted(w200_15m)[int(len(w200_15m)*0.2)] # 20th percentile
        print(f"Umbral sugerido < 15min (Instant Kill): {rec_15m:.2f}°/min (Capturaría al 80% de las ofertas > 200°)")
    
    # Threshold sug. 30m
    w100_30m = early_stats["winners_100"]["vel_30m"]
    if w100_30m:
        rec_30m = sorted(w100_30m)[int(len(w100_30m)*0.2)] # 20th percentile
        print(f"Umbral sugerido < 30min (Fast Rising): {rec_30m:.2f}°/min (Capturaría al 80% de las ofertas > 100°)")

if __name__ == "__main__":
    analyze_history()
