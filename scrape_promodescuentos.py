#!/usr/bin/env python3
import re
import time
import random
import os
import json
import logging
from contextlib import contextmanager
from typing import Dict, List, Any, Generator

import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# ===== CONFIGURACIÓN =====

# Cargar variables de entorno desde un archivo .env
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ==== CONFIGURACIONES TELEGRAM ====
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# Archivo para guardar ofertas ya vistas (almacena un diccionario { url: rating })
SEEN_FILE: str = "seen_hot_deals.json"

# ==== CONFIGURACIONES DE SCRAPING ====
# Estos valores de referencia (150, 300, 500, 1000 y tiempos) se usan en la validación.
# La función is_deal_valid determina si la oferta es "válida" para enviar.
def is_deal_valid(deal: Dict[str, Any]) -> bool:
    """
    Valida la oferta según las siguientes condiciones:
      - Temperatura ≥ 150 y publicada hace menos de 30 minutos (0.5 horas).
      - Temperatura ≥ 300 y publicada hace menos de 2 horas.
      - Temperatura ≥ 500 y publicada hace menos de 5 horas.
      - Temperatura ≥ 1000 y publicada hace menos de 8 horas.
    """
    temp = deal.get("temperature", 0)
    hours = deal.get("hours_since_posted", 0)
    if temp >= 150 and hours < 1:
        return True
    if temp >= 300 and hours < 2:
        return True
    if temp >= 500 and hours < 5:
        return True
    if temp >= 1000 and hours < 8:
        return True
    return False

# ===== FUNCIONES DE ALMACENAMIENTO =====

def load_seen_deals(filepath: str) -> Dict[str, int]:
    """
    Carga las ofertas ya vistas (como diccionario {url: rating}) desde un archivo JSON.
    """
    if not os.path.isfile(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error("Error cargando las ofertas vistas: %s", e)
        return {}

def save_seen_deals(filepath: str, seen_deals: Dict[str, int]) -> None:
    """
    Guarda las ofertas vistas en un archivo JSON.
    """
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(seen_deals, f)
    except Exception as e:
        logging.error("Error guardando las ofertas vistas: %s", e)

# ===== FUNCIONES DE RATING =====

def get_deal_rating(deal: Dict[str, Any]) -> int:
    """
    Calcula el "rating" (cantidad de 🔥) para la oferta.
    Para ofertas con temperatura < 300 y publicadas hace menos de 2 horas se asigna dinámicamente:
      - Menos de 30 min → 4
      - Menos de 1 hora → 3
      - Menos de 1.5 horas → 2
      - Menos de 2 horas → 1
    Para ofertas con temperatura ≥300 se asigna de forma "estática":
      - ≥1000 → 4
      - ≥500  → 3
      - ≥300  → 2
      - (caso contrario, 1)
    """
    temp = deal.get("temperature", 0)
    hours = deal.get("hours_since_posted", 0)
    if temp < 300 and hours < 2:
        if hours < 0.5:
            return 4
        elif hours < 1:
            return 3
        elif hours < 1.5:
            return 2
        else:
            return 1
    else:
        if temp >= 1000:
            return 4
        elif temp >= 500:
            return 3
        elif temp >= 300:
            return 2
        else:
            return 1

# ===== FUNCIONES PARA TELEGRAM =====

def send_telegram_message(deal_data: Dict[str, Any]) -> None:
    """
    Envía un mensaje a Telegram con un formato visual mejorado, mostrando la cantidad de 🔥
    según el rating calculado.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram API no configurado, mensaje no enviado.")
        return

    rating = get_deal_rating(deal_data)
    emoji = "🔥" * rating

    # Formateo del tiempo transcurrido
    hours_posted: float = deal_data.get('hours_since_posted', 0)
    if hours_posted >= 1:
        time_ago_text = f"{int(hours_posted)} horas"
    else:
        time_ago_text = f"{int(hours_posted * 60)} minutos"

    # Secciones opcionales: precio, descuento y cupón
    price_display: str = deal_data.get('price_display') or "Unknown"
    price_text: str = f"<b>Precio:</b> {price_display}" if price_display != "Unknown" else ""
    discount_percentage: str = deal_data.get('discount_percentage') or ""
    discount_text: str = f"<b>Descuento:</b> {discount_percentage}" if discount_percentage else ""
    coupon_code: str = deal_data.get('coupon_code') or ""
    coupon_text: str = f"<b>Cupón:</b> <code>{coupon_code}</code>" if coupon_code else ""

    # Calcular las líneas opcionales fuera de la f-string para evitar backslashes
    opt_discount: str = "\n" + discount_text if discount_text else ""
    opt_coupon: str = "\n" + coupon_text if coupon_text else ""

    # Construcción del mensaje HTML sin incluir backslashes en expresiones f-string
    message = f"""
<b>{deal_data.get('title', '')}</b>

<b>Calificación:</b> {deal_data.get('temperature', 0):.0f}° {emoji} 
<b>{deal_data.get('posted_or_updated', 'Publicado')} hace:</b> {time_ago_text}
<b>Comercio:</b> {deal_data.get('merchant', 'Unknown')}

{price_text}{opt_discount}{opt_coupon}

<b>Descripción:</b>
{deal_data.get('description', '')}
    """.strip()

    # Crear teclado inline para "Ver Oferta"
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Ver Oferta", "url": deal_data.get('url', '')}
            ]
        ]
    }

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup),
        "disable_web_page_preview": True,
    }

    image_url: str = deal_data.get('image_url', '')
    if image_url and image_url != 'No Image':
        url_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload["photo"] = image_url
        payload["caption"] = message
    else:
        url_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload["text"] = message

    logging.debug("Telegram Payload: %s", payload)
    try:
        resp = requests.post(url_api, json=payload, timeout=10)
        if resp.status_code == 200:
            logging.info("Mensaje Telegram enviado correctamente.")
        else:
            logging.error("Error en Telegram API: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logging.exception("Excepción al enviar mensaje Telegram: %s", e)

# ===== FUNCIONES PARA EL DRIVER =====

def init_driver() -> webdriver.Chrome:
    """
    Inicializa y configura el WebDriver de Chrome.
    """
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.binary_location = "/usr/bin/google-chrome"
    
    # Configurar el servicio de ChromeDriver
    service = Service(ChromeDriverManager().install())
    
    # Inicializar el driver
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

@contextmanager
def get_driver() -> Generator[webdriver.Chrome, None, None]:
    """
    Context manager para el WebDriver que se asegura de liberar los recursos al finalizar.
    """
    driver = init_driver()
    try:
        yield driver
    finally:
        driver.quit()

# ===== FUNCIONES PARA EL SCRAPING =====

def scrape_promodescuentos_hot(driver: webdriver.Chrome) -> str:
    """
    Extrae el HTML de la página 'hot' de Promodescuentos usando Selenium.
    """
    url = "https://www.promodescuentos.com/nuevas"
    html = ""
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)
        html = driver.page_source
    except Exception as e:
        logging.exception("Error scraping: %s", e)
    return html

def parse_deals(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Parsea el HTML con BeautifulSoup y extrae la información de las ofertas.
    """
    articles = soup.select("article.thread")
    deals_data: List[Dict[str, Any]] = []
    for art in articles:
        temp_element = art.select_one(".cept-vote-temp")
        if not temp_element:
            continue
        temp_text = temp_element.get_text(strip=True)
        m_temp = re.search(r"(\d+(\.\d+)?)", temp_text)
        if not m_temp:
            continue
        temperature = float(m_temp.group(1))

        time_ribbon = art.select_one(".chip--type-default span.size--all-s")
        if not time_ribbon:
            continue
        posted_text = time_ribbon.get_text(strip=True)
        posted_or_updated = "Publicado"
        if "Actualizado" in posted_text:
            posted_or_updated = "Actualizado"

        hours = 0
        minutes = 0
        days = 0
        m_days = re.search(r"hace\s*(\d+)\s*d", posted_text)
        if m_days:
            days = int(m_days.group(1))
        m_hrs = re.search(r"hace\s*(\d+)\s*h", posted_text)
        if m_hrs:
            hours = int(m_hrs.group(1))
        m_min = re.search(r"hace\s*(\d+)\s*m", posted_text)
        if m_min:
            minutes = int(m_min.group(1))
        total_hours = (days * 24) + hours + (minutes / 60.0)

        title_element = art.select_one(".cept-tt.thread-link")
        if not title_element:
            continue
        title = title_element.get_text(strip=True)
        link = title_element["href"] if title_element.has_attr("href") else ""
        if link.startswith("/"):
            link = "https://www.promodescuentos.com" + link

        merchant_element = art.select_one(".threadListCard-body a.link.color--text-NeutralSecondary")
        merchant = merchant_element.get_text(strip=True) if merchant_element else "Unknown"

        price_element = art.select_one(".thread-price")
        price_display = price_element.get_text(strip=True) if price_element else None
        if not price_display:
            price_display = "Unknown"

        discount_percentage = None
        discount_badge = art.select_one(".textBadge--green")
        if discount_badge:
            discount_text = discount_badge.get_text(strip=True)
            m_discount = re.search(r"-(\d+)%", discount_text)
            if m_discount:
                discount_percentage = f"{m_discount.group(1)}%"

        # Extraer URL de la imagen
        image_element = art.select_one(".threadListCard-image img.thread-image")
        image_url = image_element['src'] if image_element and image_element.has_attr('src') else 'No Image'
        logging.debug("Extracted image URL: %s", image_url)
        if image_url != 'No Image' and "/re/" in image_url:
            image_url_base = image_url.split("/re/")[0]
        else:
            image_url_base = image_url

        description_element = art.select_one(".userHtml.userHtml-content div")
        description = description_element.get_text(strip=True) if description_element else "No description available"

        coupon_code = None
        coupon_element = art.select_one(".voucher .buttonWithCode-code")
        if coupon_element:
            coupon_code = coupon_element.get_text(strip=True)

        deals_data.append({
            "title": title,
            "url": link,
            "temperature": temperature,
            "hours_since_posted": total_hours,
            "merchant": merchant,
            "price_display": price_display,
            "discount_percentage": discount_percentage,
            "image_url": image_url_base,
            "description": description,
            "coupon_code": coupon_code,
            "posted_or_updated": posted_or_updated
        })
    return deals_data

def filter_new_hot_deals(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filtra las ofertas y retorna solo aquellas que cumplan las validaciones definidas en is_deal_valid.
    """
    return [d for d in deals if is_deal_valid(d)]

# ===== FUNCION PRINCIPAL =====

def main() -> None:
    """
    Función principal que ejecuta el scraper en un loop, filtra y envía las ofertas válidas a Telegram.
    Se reenvía la oferta si su rating actual es mayor que el registrado anteriormente.
    """
    seen_deals: Dict[str, int] = load_seen_deals(SEEN_FILE)
    logging.info("Inicio del proceso de scraping de Promodescuentos Hot.")

    try:  # Mueve el bloque try exterior para capturar KeyboardInterrupt y otras excepciones
        while True:
            logging.info("Revisando 'Hot' Promodescuentos...")
            with get_driver() as driver:  # Inicializa el driver DENTRO del bucle
                html = scrape_promodescuentos_hot(driver)
                if not html:
                    logging.warning("No se pudieron obtener las ofertas. Se intentará nuevamente en la siguiente iteración.")
                else:
                    # Guardar el HTML para depuración (opcional)
                    with open("debug_degrees.html", "w", encoding="utf-8") as f:
                        f.write(html)

                    soup = BeautifulSoup(html, "html.parser")
                    deals = parse_deals(soup)
                    valid_deals = filter_new_hot_deals(deals)

                    new_deals = []
                    for deal in valid_deals:
                        current_rating = get_deal_rating(deal)
                        url = deal["url"]
                        # Si la oferta no ha sido enviada antes o si su rating ha mejorado
                        if (url not in seen_deals) or (current_rating > seen_deals.get(url, 0)):
                            new_deals.append(deal)
                            seen_deals[url] = current_rating

                    if new_deals:
                        logging.info("Se encontraron %d ofertas nuevas o mejoradas.", len(new_deals))
                        for d in new_deals:
                            logging.info("- %.0f° | %.1fh | %s\n%s",
                                         d['temperature'], d['hours_since_posted'], d['title'], d['url'])
                            send_telegram_message(d)
                    else:
                        logging.info("No hay ofertas nuevas o mejoradas que cumplan las validaciones.")

                    save_seen_deals(SEEN_FILE, seen_deals)

            # Espera aleatoria (entre 5 y 25 minutos)
            wait_seconds = random.randint(5 * 60, 9 * 60)
            minutes, seconds = divmod(wait_seconds, 60)
            logging.info("Esperando %d min %d seg...\n", minutes, seconds)
            time.sleep(wait_seconds)
    except KeyboardInterrupt:
        logging.info("Interrupción manual detectada. Saliendo...")
    except Exception as e: # Captura cualquier otra excepción en el loop principal
        logging.exception("Excepción en el loop principal: %s", e)

if __name__ == "__main__":
    main()