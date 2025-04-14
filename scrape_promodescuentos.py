#!/usr/bin/env python3
import re
import time
import random
import os
import json
import logging
from contextlib import contextmanager
from typing import Dict, List, Any, Generator
import signal # Import signal for graceful shutdown attempts

import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler, SimpleHTTPRequestHandler
import mimetypes

# ===== CONFIGURACIÓN =====

# Cargar variables de entorno desde un archivo .env
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    # Added threadName and more specific format
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
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
def is_deal_valid(deal: Dict[str, Any]) -> bool:
    """
    Valida la oferta según las siguientes condiciones:
      - Temperatura ≥ 150 y publicada hace menos de 1 hora.
      - Temperatura ≥ 300 y publicada hace menos de 2 horas.
      - Temperatura ≥ 500 y publicada hace menos de 5 horas.
      - Temperatura ≥ 1000 y publicada hace menos de 8 horas.
    """
    temp = deal.get("temperature", 0)
    hours = deal.get("hours_since_posted", 0)
    if temp >= 150 and hours < 1: # Adjusted from 0.5h
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
        logging.info(f"Archivo de ofertas vistas ({filepath}) no encontrado. Empezando con diccionario vacío.")
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                logging.warning(f"El contenido de {filepath} no es un diccionario JSON válido. Empezando con diccionario vacío.")
                return {}
            # Ensure values are integers
            valid_data = {}
            for k, v in data.items():
                try:
                    valid_data[k] = int(v)
                except (ValueError, TypeError):
                    logging.warning(f"Valor no entero encontrado para la URL '{k}' en {filepath}. Omitiendo.")
            return valid_data
    except json.JSONDecodeError:
        logging.error(f"Error decodificando JSON desde {filepath}. El archivo podría estar corrupto. Empezando con diccionario vacío.")
        # Consider backing up the corrupted file here
        # os.rename(filepath, f"{filepath}.corrupted_{int(time.time())}")
        return {}
    except Exception as e:
        logging.exception("Error inesperado cargando las ofertas vistas: %s", e)
        return {}

def save_seen_deals(filepath: str, seen_deals: Dict[str, int]) -> None:
    """
    Guarda las ofertas vistas en un archivo JSON de forma segura (atomic write).
    """
    temp_filepath = filepath + ".tmp"
    try:
        with open(temp_filepath, "w", encoding="utf-8") as f:
            json.dump(seen_deals, f, indent=4) # Use indent for readability
        # Atomic rename (replaces the old file)
        os.replace(temp_filepath, filepath)
        logging.debug(f"Ofertas vistas guardadas correctamente en {filepath}")
    except Exception as e:
        logging.error("Error guardando las ofertas vistas en %s: %s", filepath, e)
        # Clean up temp file if it exists
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
            except OSError as remove_err:
                logging.error(f"Error eliminando archivo temporal {temp_filepath}: {remove_err}")

# ===== FUNCIONES DE RATING =====

def get_deal_rating(deal: Dict[str, Any]) -> int:
    """
    Calcula el "rating" (cantidad de 🔥) para la oferta.
    """
    temp = deal.get("temperature", 0)
    hours = deal.get("hours_since_posted", 0)

    # Asegurarse que los valores son numéricos
    try:
        temp = float(temp)
        hours = float(hours)
    except (ValueError, TypeError):
        logging.warning(f"Valores no numéricos para temp/horas en deal: {deal.get('url')}. Usando defaults.")
        temp = 0
        hours = 999 # Treat as very old if data is bad

    if temp < 300 and hours < 2:
        if hours < 0.5: return 4
        elif hours < 1: return 3
        elif hours < 1.5: return 2
        else: return 1
    else: # temp >= 300 OR hours >= 2
        if temp >= 1000: return 4
        elif temp >= 500: return 3
        elif temp >= 300: return 2
        else: return 1 # Default rating for older or cooler deals >= 300


# ===== FUNCIONES PARA TELEGRAM =====

def send_telegram_message(deal_data: Dict[str, Any]) -> None:
    """
    Envía un mensaje a Telegram con formato mejorado y manejo de errores.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram API no configurado, mensaje no enviado.")
        return

    try:
        rating = get_deal_rating(deal_data)
        emoji = "🔥" * rating

        hours_posted: float = deal_data.get('hours_since_posted', 0)
        if not isinstance(hours_posted, (int, float)) or hours_posted < 0:
             hours_posted = 0

        if hours_posted >= 1:
            time_ago_text = f"{int(hours_posted)} horas" if hours_posted >= 1.5 else "1 hora"
        else:
            minutes_ago = int(hours_posted * 60)
            time_ago_text = f"{minutes_ago} minutos" if minutes_ago > 1 else "1 minuto"

        price_display: str = str(deal_data.get('price_display', "N/D")) # Ensure string
        price_text: str = f"<b>Precio:</b> {price_display}" if price_display != "N/D" else ""
        discount_percentage: str = str(deal_data.get('discount_percentage', ""))
        discount_text: str = f"<b>Descuento:</b> {discount_percentage}" if discount_percentage else ""
        coupon_code: str = str(deal_data.get('coupon_code', ""))
        # Escape coupon code for HTML <code> tag
        coupon_code_safe = coupon_code.replace('<', '<').replace('>', '>').replace('&', '&')
        coupon_text: str = f"<b>Cupón:</b> <code>{coupon_code_safe}</code>" if coupon_code else ""

        opt_price = "\n" + price_text if price_text else ""
        opt_discount = "\n" + discount_text if discount_text else ""
        opt_coupon = "\n" + coupon_text if coupon_text else ""

        # Escape title and description for HTML
        title_safe = str(deal_data.get('title', '')).replace('<', '<').replace('>', '>').replace('&', '&')
        description_safe = str(deal_data.get('description', '')).replace('<', '<').replace('>', '>').replace('&', '&')
        merchant_safe = str(deal_data.get('merchant', 'N/D')).replace('<', '<').replace('>', '>').replace('&', '&')

        message = f"""
<b>{title_safe}</b>

<b>Calificación:</b> {deal_data.get('temperature', 0):.0f}° {emoji}
<b>{deal_data.get('posted_or_updated', 'Publicado')} hace:</b> {time_ago_text}
<b>Comercio:</b> {merchant_safe}
{opt_price}{opt_discount}{opt_coupon}

<b>Descripción:</b>
{description_safe}
        """.strip()

        deal_url = deal_data.get('url', '')
        if not deal_url:
            logging.error(f"No URL found for deal '{title_safe}', cannot send Telegram message.")
            return

        reply_markup = {
            "inline_keyboard": [[{"text": "Ver Oferta", "url": deal_url}]]
        }

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(reply_markup),
            "disable_web_page_preview": True,
        }

        image_url: str = deal_data.get('image_url', '')
        use_photo = False
        if image_url and isinstance(image_url, str) and image_url != 'No Image' and image_url.startswith(('http://', 'https://')):
            use_photo = True
            url_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            payload["photo"] = image_url
            payload["caption"] = message
            # Truncate caption if too long for photos (Telegram limit: 1024 chars)
            if len(message) > 1024:
                 payload["caption"] = message[:1020] + "..."
                 logging.warning(f"Caption truncated for photo message (URL: {deal_url})")
        else:
            url_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload["text"] = message
            # Truncate text message if too long (Telegram limit: 4096 chars)
            if len(message) > 4096:
                payload["text"] = message[:4092] + "..."
                logging.warning(f"Text message truncated (URL: {deal_url})")
            if image_url and image_url != 'No Image':
                logging.warning(f"Invalid or missing image URL: '{image_url}'. Sending text message.")

        logging.debug(f"Sending Telegram {'photo' if use_photo else 'message'}. Payload keys: {list(payload.keys())}")

        resp = requests.post(url_api, json=payload, timeout=20) # Increased timeout
        resp.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        logging.info(f"Mensaje Telegram enviado correctamente para: {deal_url}")
        time.sleep(1) # Small delay to avoid potential rate limiting

    except requests.exceptions.RequestException as e:
        logging.error(f"Error en API de Telegram para {deal_data.get('url', 'N/A')}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Respuesta API Telegram: Status={e.response.status_code}, Body={e.response.text}")
    except Exception as e:
        logging.exception(f"Excepción inesperada enviando mensaje Telegram para {deal_data.get('url', 'N/A')}: {e}")


# ===== FUNCIONES PARA EL DRIVER =====

def init_driver() -> webdriver.Chrome:
    """
    Inicializa y configura el WebDriver de Chrome con optimizaciones para Docker/Render.
    """
    logging.info("Inicializando WebDriver...")
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage") # ** CRUCIAL **
    chrome_options.add_argument("--disable-gpu") # ** RECOMENDADO **

    # --- Additional flags to potentially reduce resource usage ---
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--disable-translate")
    # chrome_options.add_argument("--disable-features=TranslateUI") # Alternative
    chrome_options.add_argument("--disable-background-timer-throttling") # May increase CPU slightly
    chrome_options.add_argument("--disable-component-update")
    chrome_options.add_argument("--disable-domain-reliability")
    chrome_options.add_argument("--disable-features=AudioServiceOutOfProcess")
    chrome_options.add_argument("--disable-ipc-flooding-protection")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-software-rasterizer") # Added back
    chrome_options.add_argument("user-agent=Mozilla/5.0 ...") # Quitar user-agent personalizado por ahora
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--safebrowsing-disable-auto-update")
    chrome_options.add_argument("--password-store=basic") # Avoid gnome-keyring or kwallet calls
    chrome_options.add_argument("--use-mock-keychain") # For macos/linux
    # --- End additional flags ---


    # Opciones para intentar parecer menos un bot
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"]) # Exclude logging too
    chrome_options.add_experimental_option('useAutomationExtension', False)

    chrome_options.binary_location = "/usr/bin/google-chrome"

    # Specify a specific port for chromedriver? Sometimes helps avoid conflicts.
    # webdriver_port = random.randint(9500, 9600) # Or a fixed one
    # logging.info(f"Using ChromeDriver port: {webdriver_port}")
    # service = Service(ChromeDriverManager().install(), port=webdriver_port)

    try:
        logging.info("Instalando/Actualizando ChromeDriver con webdriver-manager...")
        # Use default port for now unless issues persist
        service = Service(ChromeDriverManager().install())
        logging.info("ChromeDriver listo.")

        driver = webdriver.Chrome(service=service, options=chrome_options)
        logging.info("Instancia de WebDriver creada.")

        # Increased page load timeout further
        driver.set_page_load_timeout(120) # 120 segundos para carga de página
        logging.info("Timeout de carga de página establecido en 120s.")
        # Implicit wait (use cautiously, prefer explicit waits)
        driver.implicitly_wait(10) # Reduced implicit wait
        logging.info("Timeout implícito establecido en 10s.")

        return driver
    except Exception as e:
        logging.exception("FALLO al inicializar WebDriver: %s", e)
        raise

@contextmanager
def get_driver() -> Generator[webdriver.Chrome, None, None]:
    """
    Context manager para el WebDriver que se asegura de liberar los recursos al finalizar.
    """
    driver = None
    try:
        driver = init_driver()
        yield driver
    except Exception as e:
        # Log error occurred during driver usage or initialization
        logging.exception("Error capturado por el context manager del driver: %s", e)
        # Re-raise so the main loop knows something went wrong
        raise
    finally:
        if driver:
            logging.info("Iniciando cierre del WebDriver...")
            try:
                # Optionally clear cookies before quitting
                # driver.delete_all_cookies()
                driver.quit()
                logging.info("WebDriver (driver.quit()) ejecutado correctamente.")
            except WebDriverException as e:
                 # Handle cases where quit() itself fails (e.g., browser already crashed)
                 logging.error("WebDriverException al cerrar (driver.quit()) el WebDriver: %s. El navegador podría haber crasheado.", e.msg)
            except Exception as e:
                logging.error("Error inesperado al cerrar (driver.quit()) el WebDriver: %s", e)
            finally:
                # Add a small delay AFTER quit to allow processes to terminate
                time.sleep(2)
                logging.info("Pausa de 2s después de driver.quit() completada.")


# ===== FUNCIONES PARA EL SCRAPING =====

def scrape_promodescuentos_hot(driver: webdriver.Chrome) -> str:
    """
    Extrae el HTML de la página 'nuevas' de Promodescuentos usando Selenium.
    Incluye manejo de errores mejorado y guardado de HTML en error.
    """
    url = "https://www.promodescuentos.com/nuevas"
    html_content = ""
    debug_file_path = None
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    debug_dir = "/app/debug"
    os.makedirs(debug_dir, exist_ok=True)

    try:
        logging.info(f"Accediendo a la URL: {url}")
        driver.get(url) # Page load timeout set during init

        # Wait for body first (quick check)
        logging.info("Esperando elemento 'body' (max 30s)...")
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        logging.info("Elemento 'body' cargado.")

        # Wait specifically for the container holding the deals
        # Inspect the page to find a suitable container ID or class
        deals_container_selector = "div#listLayout" # Example selector, adjust if needed
        try:
            logging.info(f"Esperando contenedor de ofertas '{deals_container_selector}' (max 45s)...")
            WebDriverWait(driver, 45).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, deals_container_selector))
            )
            logging.info("Contenedor de ofertas encontrado.")
            # Optional: Add a small static wait AFTER the container is found, just in case
            # time.sleep(2)
        except TimeoutException:
            logging.warning(f"Contenedor de ofertas '{deals_container_selector}' no encontrado después de 45s. La página podría estar vacía, haber cambiado o tener problemas de carga.")
            # Continue anyway, maybe parsing can still find something or it's just empty

        logging.info("Obteniendo page source...")
        html_content = driver.page_source
        logging.info(f"HTML obtenido (longitud: {len(html_content)} caracteres).")

        # Save successful HTML
        debug_file_path = os.path.join(debug_dir, f"debug_html_SUCCESS_{timestamp}.html")
        with open(debug_file_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logging.info(f"HTML guardado en {debug_file_path}")
        return html_content # Return successfully obtained HTML

    except TimeoutException as e:
        logging.error(f"Error scraping (TimeoutException): La página o un elemento esperado tardó demasiado en cargar. URL: {url}, Error: {e.msg}")
        debug_file_path = os.path.join(debug_dir, f"debug_html_TIMEOUT_{timestamp}.html")
    except WebDriverException as e:
        # Handle common WebDriver errors like renderer timeouts or crashes
        logging.error(f"Error scraping (WebDriverException): {e.msg}") # e.msg often contains the specific error like "Timed out receiving message from renderer"
        debug_file_path = os.path.join(debug_dir, f"debug_html_WD_EXCEPTION_{e.__class__.__name__}_{timestamp}.html") # Include exception type in name
    except Exception as e:
        logging.exception(f"Error inesperado durante scraping (URL: {url}): {e}") # Use logging.exception to include traceback
        debug_file_path = os.path.join(debug_dir, f"debug_html_UNEXPECTED_ERROR_{timestamp}.html")

    # --- Attempt to save HTML on error ---
    # This block executes only if an exception occurred above
    logging.warning("Scraping fallido. Intentando guardar HTML de error...")
    if debug_file_path:
         try:
            # Important: Getting page source might also fail if the browser crashed badly
            error_html = driver.page_source
            with open(debug_file_path, "w", encoding="utf-8") as f:
                f.write(error_html)
            logging.info(f"HTML en error guardado exitosamente en {debug_file_path}")
         except Exception as save_err:
            # Log the failure to save the debug HTML
            logging.error(f"No se pudo guardar el HTML en error en {debug_file_path}: {save_err}. El driver/navegador podría estar inaccesible.")

    return "" # Return empty string indicating failure


def parse_deals(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Parsea el HTML con BeautifulSoup y extrae la información de las ofertas.
    Robustez mejorada y logging detallado.
    """
    logging.info("Iniciando parseo de ofertas desde HTML...")
    articles = soup.select("article.thread.thread--type-card") # Prioritize specific selector
    if not articles:
        articles = soup.select("article.thread") # Fallback
    logging.info(f"Se encontraron {len(articles)} artículos candidatos.")

    deals_data: List[Dict[str, Any]] = []
    processed_urls = set()

    for i, art in enumerate(articles):
        deal_info = {} # Store partial info for logging on error
        link = "N/A" # Default link for logging if extraction fails early
        try:
            # --- URL y Título (cruciales, extraer primero) ---
            title_element = art.select_one("strong.thread-title a.thread-link, a.cept-tt.thread-link") # Combine selectors
            if not title_element:
                logging.debug(f"Artículo #{i+1}: Sin elemento de título/link. Saltando.")
                continue

            link = title_element.get("href", "").strip()
            if not link:
                logging.debug(f"Artículo #{i+1}: Elemento de título sin href. Saltando.")
                continue
            if link.startswith("/"):
                link = "https://www.promodescuentos.com" + link
            deal_info["url"] = link # Store for potential error logging

            if link in processed_urls:
                logging.debug(f"Artículo #{i+1}: URL duplicada en esta página ({link}). Saltando.")
                continue
            processed_urls.add(link)

            title = title_element.get_text(strip=True)
            deal_info["title"] = title

            # --- Temperatura ---
            temp_element = art.select_one(".vote-box span.vote-temp, .cept-vote-temp") # Combine selectors
            temperature = 0.0 # Default
            if temp_element:
                temp_text = temp_element.get_text(strip=True).replace("°", "").replace(",", "").replace("+", "")
                m_temp = re.match(r"^\s*(\d+)\s*$", temp_text) # Prefer integer match
                if not m_temp:
                    m_temp = re.search(r"(\d+(\.\d+)?)", temp_text) # Fallback to float search
                if m_temp:
                    try: temperature = float(m_temp.group(1))
                    except ValueError: logging.warning(f"Valor temp no numérico: '{m_temp.group(1)}' para {link}.")
                else: logging.warning(f"No se pudo extraer temp del texto: '{temp_text}' para {link}.")
            else: logging.debug(f"No se encontró elem. temp para {link}.")
            deal_info["temperature"] = temperature

            # --- Tiempo ---
            time_element = art.select_one("span.thread-ago, .metaRibbon .chip--type-default span") # Combine selectors
            total_hours = 999.0 # Default to old if not found
            posted_or_updated = "Desconocido"
            if time_element:
                posted_text = time_element.get_text(strip=True)
                deal_info["posted_text"] = posted_text # Log raw text
                posted_or_updated = "Actualizado" if ("Actualizado" in posted_text or "Editado" in posted_text) else "Publicado"

                hours, minutes, days = 0, 0, 0
                m_days = re.search(r"(\d+)\s*d", posted_text, re.IGNORECASE)
                if m_days: days = int(m_days.group(1))
                m_hrs = re.search(r"(\d+)\s*h", posted_text, re.IGNORECASE)
                if m_hrs: hours = int(m_hrs.group(1))
                m_min = re.search(r"(\d+)\s*m", posted_text, re.IGNORECASE)
                if m_min: minutes = int(m_min.group(1))

                if days == 0 and hours == 0 and minutes == 0 and not re.search(r'\d', posted_text):
                    total_hours = 0.0 # Treat 'Ahora' etc. as 0
                else:
                    total_hours = (days * 24) + hours + (minutes / 60.0)
            else: logging.debug(f"No se encontró elem. tiempo para {link}.")
            deal_info["hours_since_posted"] = total_hours
            deal_info["posted_or_updated"] = posted_or_updated

            # --- Otros Campos (con defaults N/D) ---
            merchant_element = art.select_one(".thread-merchant-link a, .threadListCard-body a.link.color--text-NeutralSecondary")
            merchant = merchant_element.get_text(strip=True) if merchant_element else "N/D"
            deal_info["merchant"] = merchant

            price_element = art.select_one(".thread-price")
            price_display = price_element.get_text(strip=True) if price_element else "N/D"
            deal_info["price_display"] = price_display

            discount_percentage = None
            discount_badge = art.select_one(".thread-discount, .textBadge--green")
            if discount_badge:
                discount_text = discount_badge.get_text(strip=True)
                m_discount = re.search(r"-?(\d+)%", discount_text)
                if m_discount: discount_percentage = f"{m_discount.group(1)}%"
            deal_info["discount_percentage"] = discount_percentage

            # --- Imagen ---
            image_element = art.select_one("img.thread-image")
            image_url = 'No Image'
            image_url_base = 'No Image'
            if image_element:
                image_url = image_element.get('data-src', image_element.get('src', 'No Image')) # Prefer data-src

            if image_url and image_url != 'No Image':
                image_url_base = image_url.split("?")[0] # Remove query params
                if "/re/" in image_url_base: image_url_base = image_url_base.split("/re/")[0] # Remove resize part
                if image_url_base.startswith("//"): image_url_base = "https:" + image_url_base
                if not image_url_base.startswith(('http://', 'https://')):
                    logging.warning(f"URL de imagen inválida: '{image_url_base}' para {link}. Marcando 'No Image'.")
                    image_url_base = 'No Image'
            deal_info["image_url"] = image_url_base

            # --- Descripción ---
            description_element = art.select_one(".thread-description .userHtml-content, .userHtml.userHtml-content div")
            description = "No disponible"
            if description_element:
                description = description_element.get_text(strip=True, separator=' ')
                max_desc_len = 250
                if len(description) > max_desc_len: description = description[:max_desc_len].strip() + "..."
            deal_info["description"] = description

            # --- Cupón ---
            coupon_code = None
            coupon_element = art.select_one(".voucher .buttonWithCode-code")
            if coupon_element: coupon_code = coupon_element.get_text(strip=True)
            deal_info["coupon_code"] = coupon_code

            # --- Añadir a la lista ---
            # Create the final dict only with successfully extracted keys
            final_deal = {k: v for k, v in deal_info.items() if v is not None}
            deals_data.append(final_deal)

        except Exception as e:
            logging.exception(f"Error procesando artículo #{i+1} (URL: {link}): {e}. Datos parciales: {deal_info}")
            continue # Saltar al siguiente artículo

    logging.info(f"Se parsearon {len(deals_data)} ofertas después de filtrar duplicados y errores internos.")
    return deals_data


def filter_new_hot_deals(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filtra las ofertas y retorna solo aquellas que cumplan las validaciones definidas en is_deal_valid.
    """
    valid_deals = []
    for deal in deals:
        if is_deal_valid(deal):
            valid_deals.append(deal)
        # else: # Optional: Log why a deal was filtered out
        #     logging.debug(f"Filtrada oferta (inválida): Temp={deal.get('temperature', '?')}°, Horas={deal.get('hours_since_posted', '?')}h | {deal.get('title','?')}")

    logging.info(f"De {len(deals)} ofertas parseadas, {len(valid_deals)} cumplen con los criterios de validación (temp/tiempo).")
    return valid_deals


# ===== HTTP SERVER & HEALTH CHECK =====

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Handler para el health check y para servir archivos de depuración."""
    DEBUG_DIR = "/app/debug"

    def do_GET(self):
        if self.path == '/':
            self._send_json_response({'status': 'running', 'service': 'promodescuentos-scraper'})
        elif self.path == '/debug' or self.path == '/debug/':
            self._serve_debug_index()
        elif self.path.startswith('/debug/'):
            self._serve_debug_file()
        else:
            self._send_error(404, "Ruta no encontrada")

    def _send_response_util(self, status_code, content_type, body):
        self.send_response(status_code)
        self.send_header('Content-type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_response(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self._send_response_util(status, 'application/json; charset=utf-8', body)

    def _send_html_response(self, html_content, status=200):
        body = html_content.encode('utf-8')
        self._send_response_util(status, 'text/html; charset=utf-8', body)

    def _send_file_response(self, file_path):
        if not os.path.isfile(file_path):
            self._send_error(404, f"Archivo no encontrado: {os.path.basename(file_path)}")
            return

        try:
            mime_type, _ = mimetypes.guess_type(file_path)
            mime_type = mime_type or 'application/octet-stream'
            with open(file_path, 'rb') as f:
                fs = os.fstat(f.fileno())
                self.send_response(200)
                self.send_header('Content-type', mime_type)
                self.send_header("Content-Length", str(fs.st_size))
                # Add cache control header? Might help browser debugging.
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                # Stream the file? Useful for large files, maybe overkill here.
                self.wfile.write(f.read())
        except IOError as e:
            self._send_error(500, f"Error al leer el archivo: {os.path.basename(file_path)} - {e}")
        except Exception as e:
            logging.exception(f"Error inesperado sirviendo archivo {file_path}")
            # Avoid sending another response if headers already sent
            if not self.headers_sent:
                 self._send_error(500, "Error interno del servidor al servir el archivo.")


    def _send_error(self, status_code, message):
        body = message.encode('utf-8')
        # Check if headers already sent to avoid "Cannot send headers after they are sent to the client"
        if not self.headers_sent:
            self._send_response_util(status_code, 'text/plain; charset=utf-8', body)
        else:
            logging.error(f"Intento de enviar error '{message}' después de que las cabeceras ya fueron enviadas.")


    def _serve_debug_index(self):
        if not os.path.isdir(self.DEBUG_DIR):
            self._send_error(404, "Directorio de depuración no encontrado")
            return
        try:
            # List, filter HTML, sort by modification time (newest first)
            files_with_mtime = []
            for f in os.listdir(self.DEBUG_DIR):
                if f.endswith(".html"):
                    try:
                        mtime = os.path.getmtime(os.path.join(self.DEBUG_DIR, f))
                        files_with_mtime.append((f, mtime))
                    except OSError:
                         files_with_mtime.append((f, 0)) # Handle potential race condition if file deleted

            files_with_mtime.sort(key=lambda x: x[1], reverse=True)

            list_items = ""
            for file, mtime in files_with_mtime:
                safe_file_url = requests.utils.quote(file)
                safe_file_html = file.replace('<', '<').replace('>', '>')
                # Add timestamp?
                # mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
                # list_items += f'<li><a href="/debug/{safe_file_url}">{safe_file_html}</a> ({mtime_str})</li>'
                list_items += f'<li><a href="/debug/{safe_file_url}">{safe_file_html}</a></li>'


            html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Archivos de Depuración</title></head><body><h1>Archivos HTML en <code>{self.DEBUG_DIR}</code> (más recientes primero)</h1><ul>{list_items if list_items else "<li>No hay archivos HTML.</li>"}</ul></body></html>"""
            self._send_html_response(html)
        except OSError as e:
            logging.error(f"Error al listar directorio de depuración: {e}")
            self._send_error(500, "Error al listar el directorio de depuración")
        except Exception as e:
            logging.exception("Error inesperado generando índice de depuración")
            self._send_error(500, "Error interno generando índice de depuración")

    def _serve_debug_file(self):
        try:
            # Basic path traversal check and decode filename
            rel_path = requests.utils.unquote(self.path[len('/debug/'):])
            if '..' in rel_path or rel_path.startswith('/'):
                self._send_error(400, "Acceso inválido.")
                return
            full_path = os.path.abspath(os.path.join(self.DEBUG_DIR, rel_path))
            # Double check it's still inside DEBUG_DIR after normalization
            if not full_path.startswith(os.path.abspath(self.DEBUG_DIR)):
                 self._send_error(400, "Acceso inválido (fuera del directorio).")
                 return

            self._send_file_response(full_path)
        except Exception as e:
            logging.exception(f"Error decodificando o validando ruta de archivo de depuración: {self.path}")
            self._send_error(400, "URL de archivo inválida.")

    def log_message(self, format, *args):
        # Only log errors
        try:
            status_code = str(args[1])
            if status_code.startswith(('2', '3')): # 2xx Success, 3xx Redirect
                return
        except IndexError:
            pass # Ignore if format args don't match expected pattern
        super().log_message(format, *args)

def run_health_server():
    server_address = ('0.0.0.0', 10000)
    httpd = None
    try:
        httpd = HTTPServer(server_address, HealthCheckHandler)
        logging.info(f"Servidor HTTP de Health Check iniciado en {server_address[0]}:{server_address[1]}")
        httpd.serve_forever()
    except OSError as e:
         logging.error(f"No se pudo iniciar el servidor HTTP en {server_address} (quizás el puerto ya está en uso?): {e}")
         # If the server fails to start, we should probably exit the main script too.
         os._exit(2) # Exit with a specific code for server start failure
    except Exception as e:
        logging.exception(f"Error fatal en el servidor HTTP: {e}")
    finally:
        if httpd:
            httpd.server_close()
            logging.info("Servidor HTTP cerrado.")


# ===== FUNCION PRINCIPAL =====

# Global flag to signal shutdown
shutdown_flag = threading.Event()

def signal_handler(signum, frame):
    """Handle termination signals."""
    logging.warning(f"Señal {signal.Signals(signum).name} recibida. Iniciando apagado...")
    shutdown_flag.set()

def main() -> None:
    """
    Función principal que ejecuta el scraper en un loop, con manejo de errores y reinicio.
    """
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Start health check server in a separate thread
    health_thread = threading.Thread(target=run_health_server, name="HealthCheckThread", daemon=True)
    health_thread.start()

    seen_deals: Dict[str, int] = load_seen_deals(SEEN_FILE)
    logging.info(f"Inicio del proceso de scraping. {len(seen_deals)} ofertas cargadas desde {SEEN_FILE}.")

    iteration_count = 0
    consecutive_failures = 0
    # Lower max consecutive failures to trigger restart sooner if problems persist
    max_consecutive_failures = 3

    # --- Main Loop ---
    while not shutdown_flag.is_set():
        iteration_count += 1
        logging.info(f"\n===== INICIO Iteración #{iteration_count} =====")
        iteration_successful = False # Flag to track success within the iteration

        try:
            logging.info("Revisando Promodescuentos...")
            with get_driver() as driver: # Driver is initialized and quit within this block
                html = scrape_promodescuentos_hot(driver)

            if not html:
                logging.warning("No se pudo obtener el HTML de la página en esta iteración.")
                # Failure is counted below in the except block or if html is empty
            else:
                soup = BeautifulSoup(html, "html.parser")
                deals = parse_deals(soup)
                valid_deals = filter_new_hot_deals(deals)
                new_deals_found_count = 0

                # Process valid deals
                if valid_deals:
                    current_seen_in_iteration = {} # Track updates within this iteration
                    for deal in valid_deals:
                        url = deal.get("url")
                        if not url:
                            logging.warning(f"Oferta válida encontrada sin URL: {deal.get('title')}")
                            continue

                        current_rating = get_deal_rating(deal)
                        previous_rating = seen_deals.get(url, 0)

                        if url not in seen_deals or current_rating > previous_rating:
                            log_prefix = "[NUEVA]" if url not in seen_deals else f"[MEJORA RATING ({previous_rating}->{current_rating})]"
                            logging.info(f"{log_prefix} {deal.get('temperature'):.0f}°|{deal.get('hours_since_posted'):.1f}h| {deal.get('title')} | {url}")
                            # Send to Telegram immediately or collect? Send immediately for now.
                            send_telegram_message(deal)
                            current_seen_in_iteration[url] = current_rating
                            new_deals_found_count += 1

                    if current_seen_in_iteration:
                         logging.info(f"Se procesaron {new_deals_found_count} ofertas nuevas/mejoradas.")
                         # Update the main seen_deals dict and save
                         seen_deals.update(current_seen_in_iteration)
                         save_seen_deals(SEEN_FILE, seen_deals)
                    else:
                         logging.info("No hay ofertas nuevas o mejoradas que cumplan las validaciones en esta iteración.")

                else: # No valid deals found from parsing
                    logging.info("No se encontraron ofertas válidas después del parseo.")

                # Mark iteration as successful if we got HTML and parsed it (even if no new deals)
                iteration_successful = True

        except (WebDriverException, TimeoutException) as driver_error:
            # Catch specific errors likely from init_driver or get_driver context manager
            logging.error(f"Error de WebDriver/Timeout durante la iteración #{iteration_count}: {driver_error}")
            # Failure is handled below (iteration_successful remains False)
        except Exception as loop_exception:
            # Catch unexpected errors in the main processing logic
            logging.exception(f"Excepción inesperada en la iteración #{iteration_count}: {loop_exception}")
            # Failure is handled below

        # --- Handle Iteration Outcome ---
        if iteration_successful:
            logging.info("Iteración completada exitosamente (HTML obtenido y parseado).")
            consecutive_failures = 0 # Reset counter on success
        else:
            consecutive_failures += 1
            logging.warning(f"Iteración #{iteration_count} fallida. Fallos consecutivos: {consecutive_failures}/{max_consecutive_failures}.")
            if consecutive_failures >= max_consecutive_failures:
                logging.error(f"Se alcanzó el máximo de {max_consecutive_failures} fallos consecutivos. Saliendo para permitir reinicio automático.")
                # Optionally send admin notification
                # send_admin_alert("Scraper fallando repetidamente, reiniciando...")
                shutdown_flag.set() # Signal threads to stop
                os._exit(1) # Exit with error code

        # --- Wait logic ---
        if not shutdown_flag.is_set():
            min_wait = 5 * 60 # 5 minutes
            max_wait = 12 * 60 # 12 minutes (reduced max slightly)
            wait_seconds = random.randint(min_wait, max_wait)
            minutes, seconds = divmod(wait_seconds, 60)
            logging.info(f"===== FIN Iteración #{iteration_count} =====")
            logging.info(f"Esperando {minutes} min {seconds} seg hasta la próxima revisión...")
            # Use shutdown_flag.wait for interruptible sleep
            shutdown_flag.wait(timeout=wait_seconds)

    # --- End of Main Loop (Shutdown initiated) ---
    logging.info("Bucle principal terminado debido a señal de apagado.")
    # Final save attempt? Might be redundant if already saved after last successful iter.
    # logging.info("Intentando guardado final de ofertas vistas...")
    # save_seen_deals(SEEN_FILE, seen_deals)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Catch any unexpected error that might occur outside the main loop in main()
        logging.exception("Excepción fatal no capturada en main(): %s", e)
        os._exit(3) # Exit with a different error code for uncaught exceptions
    finally:
        logging.info("Proceso principal finalizado.")