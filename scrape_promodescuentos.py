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
import glob # Para buscar archivos con patrones
import sys # Para salir limpiamente

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
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ==== CONFIGURACIONES TELEGRAM ====
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# Archivo para guardar ofertas ya vistas
SEEN_FILE: str = "seen_hot_deals.json"

# ==== CONFIGURACIONES DE DEBUG ==== (Constantes globales)
DEBUG_DIR = "/app/debug"
DEBUG_FILE_PREFIX = "debug_html_"
KEEP_DEBUG_FILES = 5 # Número de archivos de debug a conservar

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

# ===== FUNCIONES DE DEBUG =====

def cleanup_debug_files(directory: str, prefix: str, keep_count: int):
    """
    Limpia los archivos de debug, manteniendo solo los 'keep_count' más recientes.
    """
    try:
        # Asegurarse de que el directorio exista
        if not os.path.isdir(directory):
            logging.warning(f"Directorio de debug {directory} no encontrado para limpieza.")
            return

        # Usar glob para encontrar archivos que coincidan con el patrón
        debug_files = glob.glob(os.path.join(directory, f"{prefix}*.html"))

        if not debug_files:
            logging.debug(f"No se encontraron archivos de debug con prefijo '{prefix}' en {directory}.")
            return

        # Obtener pares (ruta, tiempo_modificacion)
        files_with_mtime = []
        for f_path in debug_files:
            try:
                mtime = os.path.getmtime(f_path)
                files_with_mtime.append((f_path, mtime))
            except FileNotFoundError:
                logging.warning(f"Archivo {f_path} no encontrado durante la limpieza (posiblemente eliminado concurrentemente).")
            except OSError as e:
                logging.error(f"Error obteniendo mtime para {f_path}: {e}")

        # Ordenar por tiempo de modificación (más reciente primero)
        files_with_mtime.sort(key=lambda x: x[1], reverse=True)

        # Si hay más archivos de los que queremos mantener
        if len(files_with_mtime) > keep_count:
            files_to_delete = files_with_mtime[keep_count:]
            logging.info(f"Limpiando archivos de debug antiguos. Manteniendo {keep_count}, eliminando {len(files_to_delete)}.")
            deleted_count = 0
            for f_path_to_delete, _ in files_to_delete:
                try:
                    os.remove(f_path_to_delete)
                    logging.debug(f"Archivo de debug eliminado: {os.path.basename(f_path_to_delete)}")
                    deleted_count += 1
                except OSError as e:
                    logging.error(f"Error eliminando archivo de debug {f_path_to_delete}: {e}")
            if deleted_count > 0:
                logging.info(f"Limpieza completada. Se eliminaron {deleted_count} archivos antiguos.")
            else:
                 logging.info(f"Limpieza intentada, pero no se eliminaron archivos (quizás por errores previos).")

        else:
            logging.debug(f"No se necesita limpieza de debug. Archivos encontrados: {len(files_with_mtime)} (Límite: {keep_count}).")

    except Exception as e:
        logging.exception(f"Error inesperado durante la limpieza de archivos de debug en {directory}: {e}")

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
            json.dump(seen_deals, f, indent=4)
        os.replace(temp_filepath, filepath)
        logging.debug(f"Ofertas vistas guardadas correctamente en {filepath}")
    except Exception as e:
        logging.error("Error guardando las ofertas vistas en %s: %s", filepath, e)
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

        price_display: str = str(deal_data.get('price_display', "N/D"))
        price_text: str = f"<b>Precio:</b> {price_display}" if price_display != "N/D" else ""
        discount_percentage: str = str(deal_data.get('discount_percentage', ""))
        discount_text: str = f"<b>Descuento:</b> {discount_percentage}" if discount_percentage else ""
        coupon_code: str = str(deal_data.get('coupon_code', ""))
        coupon_code_safe = coupon_code.replace('<', '<').replace('>', '>').replace('&', '&')
        coupon_text: str = f"<b>Cupón:</b> <code>{coupon_code_safe}</code>" if coupon_code else ""

        opt_price = "\n" + price_text if price_text else ""
        opt_discount = "\n" + discount_text if discount_text else ""
        opt_coupon = "\n" + coupon_text if coupon_text else ""

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
            if len(message) > 1024:
                 payload["caption"] = message[:1020] + "..."
                 logging.warning(f"Caption truncated for photo message (URL: {deal_url})")
        else:
            url_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload["text"] = message
            if len(message) > 4096:
                payload["text"] = message[:4092] + "..."
                logging.warning(f"Text message truncated (URL: {deal_url})")
            if image_url and image_url != 'No Image':
                logging.warning(f"Invalid or missing image URL: '{image_url}'. Sending text message.")

        logging.debug(f"Sending Telegram {'photo' if use_photo else 'message'}. Payload keys: {list(payload.keys())}")
        resp = requests.post(url_api, json=payload, timeout=20)
        resp.raise_for_status()
        logging.info(f"Mensaje Telegram enviado correctamente para: {deal_url}")
        time.sleep(1)

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
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--disable-translate")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-component-update")
    chrome_options.add_argument("--disable-domain-reliability")
    chrome_options.add_argument("--disable-features=AudioServiceOutOfProcess")
    chrome_options.add_argument("--disable-ipc-flooding-protection")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("user-agent=Mozilla/5.0 ...")
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--safebrowsing-disable-auto-update")
    chrome_options.add_argument("--password-store=basic")
    chrome_options.add_argument("--use-mock-keychain")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.binary_location = "/usr/bin/google-chrome"

    try:
        logging.info("Instalando/Actualizando ChromeDriver con webdriver-manager...")
        service = Service(ChromeDriverManager().install())
        logging.info("ChromeDriver listo.")
        driver = webdriver.Chrome(service=service, options=chrome_options)
        logging.info("Instancia de WebDriver creada.")
        driver.set_page_load_timeout(120)
        logging.info("Timeout de carga de página establecido en 120s.")
        driver.implicitly_wait(10)
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
        logging.exception("Error capturado por el context manager del driver: %s", e)
        raise
    finally:
        if driver:
            logging.info("Iniciando cierre del WebDriver...")
            try:
                driver.quit()
                logging.info("WebDriver (driver.quit()) ejecutado correctamente.")
            except WebDriverException as e:
                 logging.error("WebDriverException al cerrar (driver.quit()) el WebDriver: %s. El navegador podría haber crasheado.", e.msg)
            except Exception as e:
                logging.error("Error inesperado al cerrar (driver.quit()) el WebDriver: %s", e)
            finally:
                time.sleep(2)
                logging.info("Pausa de 2s después de driver.quit() completada.")

# ===== FUNCIONES PARA EL SCRAPING =====

def scrape_promodescuentos_hot(driver: webdriver.Chrome) -> str:
    """
    Extrae el HTML de la página 'nuevas' de Promodescuentos usando Selenium.
    Incluye manejo de errores mejorado, guardado de HTML y limpieza de archivos de debug.
    """
    url = "https://www.promodescuentos.com/nuevas"
    html_content = ""
    debug_file_path = None # Ruta específica para el archivo en caso de error
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    # Asegurarse de que el directorio de debug exista
    os.makedirs(DEBUG_DIR, exist_ok=True)

    try:
        logging.info(f"Accediendo a la URL: {url}")
        driver.get(url)

        # Espera solo por el body
        logging.info("Esperando elemento 'body' (max 60s)...") # Aumentado timeout
        WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        logging.info("Elemento 'body' cargado.")

        # QUITAR/COMENTAR la espera específica por #listLayout ya que parece causar timeouts innecesarios
        # deals_container_selector = "div#listLayout" # O "div#content-list.listLayout"
        # try:
        #     logging.info(f"Esperando contenedor de ofertas '{deals_container_selector}' (max 45s)...")
        #     WebDriverWait(driver, 45).until(
        #         EC.presence_of_element_located((By.CSS_SELECTOR, deals_container_selector))
        #     )
        #     logging.info("Contenedor de ofertas encontrado.")
        # except TimeoutException:
        #     logging.warning(f"Contenedor de ofertas '{deals_container_selector}' no encontrado después de 45s. La página podría estar vacía, haber cambiado o tener problemas de carga. Se continuará intentando obtener el HTML.")

        # Opcional: Pausa estática si se sospecha de JS lento
        # time.sleep(5)

        logging.info("Obteniendo page source...")
        html_content = driver.page_source # Intentar obtenerlo siempre
        logging.info(f"HTML obtenido (longitud: {len(html_content)} caracteres).")

    except TimeoutException as e:
        logging.error(f"Error scraping (TimeoutException): La página o 'body' tardó demasiado. URL: {url}, Error: {e.msg}")
        debug_file_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}TIMEOUT_{timestamp}.html")
        # No retornar, intentar guardar HTML si es posible
    except WebDriverException as e:
        logging.error(f"Error scraping (WebDriverException): {e.msg}")
        debug_file_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}WD_EXCEPTION_{e.__class__.__name__}_{timestamp}.html")
        # No retornar
    except Exception as e:
        logging.exception(f"Error inesperado durante scraping (URL: {url}): {e}")
        debug_file_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}UNEXPECTED_ERROR_{timestamp}.html")
        # No retornar

    # --- Bloque de guardado de HTML (unificado para éxito o error) ---
    save_path = None
    html_to_save = html_content # Usar el HTML obtenido si existe
    log_level = logging.INFO
    log_msg = ""

    if html_content: # Éxito
         save_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}SUCCESS_{timestamp}.html")
         log_msg = f"HTML guardado en {save_path}"
    elif debug_file_path: # Error (la ruta ya contiene el tipo de error)
         save_path = debug_file_path
         log_level = logging.WARNING # Loguear como warning si guardamos HTML de error
         log_msg = f"HTML en error guardado en {save_path}"
         # Intentar obtener HTML si no se pudo antes y el driver sigue vivo
         if not html_to_save and driver:
             try:
                 html_to_save = driver.page_source
                 logging.info(f"Se obtuvo page source para HTML de error ({len(html_to_save)} chars).")
             except Exception as ps_err:
                 logging.error(f"No se pudo obtener page_source para guardar HTML de error en {save_path}: {ps_err}")
                 html_to_save = "<!-- No se pudo obtener page_source durante el error -->" # Placeholder
    else:
         logging.warning("No se generó HTML ni ruta de archivo de debug para guardar.")

    if save_path and html_to_save:
         try:
             with open(save_path, "w", encoding="utf-8") as f:
                 f.write(html_to_save)
             logging.log(log_level, log_msg)
             # --- Llamar a la limpieza DESPUÉS de guardar exitosamente ---
             cleanup_debug_files(DEBUG_DIR, DEBUG_FILE_PREFIX, KEEP_DEBUG_FILES)
         except Exception as save_err:
              logging.error(f"Fallo crítico al intentar guardar HTML en {save_path}: {save_err}")

    return html_to_save # Devolver el contenido (o vacío/placeholder si falló)


def parse_deals(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Parsea el HTML con BeautifulSoup y extrae la información de las ofertas.
    """
    logging.info("Iniciando parseo de ofertas desde HTML...")
    articles = soup.select("article.thread.thread--type-card")
    if not articles:
        articles = soup.select("article.thread")
    logging.info(f"Se encontraron {len(articles)} artículos candidatos.")

    deals_data: List[Dict[str, Any]] = []
    processed_urls = set()

    for i, art in enumerate(articles):
        deal_info = {}
        link = "N/A"
        try:
            title_element = art.select_one("strong.thread-title a.thread-link, a.cept-tt.thread-link")
            if not title_element:
                logging.debug(f"Artículo #{i+1}: Sin elemento de título/link. Saltando.")
                continue

            link = title_element.get("href", "").strip()
            if not link:
                logging.debug(f"Artículo #{i+1}: Elemento de título sin href. Saltando.")
                continue
            if link.startswith("/"):
                link = "https://www.promodescuentos.com" + link
            deal_info["url"] = link

            if link in processed_urls:
                logging.debug(f"Artículo #{i+1}: URL duplicada en esta página ({link}). Saltando.")
                continue
            processed_urls.add(link)

            title = title_element.get_text(strip=True)
            deal_info["title"] = title

            temp_element = art.select_one(".vote-box span.vote-temp, .cept-vote-temp")
            temperature = 0.0
            if temp_element:
                temp_text = temp_element.get_text(strip=True).replace("°", "").replace(",", "").replace("+", "")
                m_temp = re.match(r"^\s*(\d+)\s*$", temp_text)
                if not m_temp:
                    m_temp = re.search(r"(\d+(\.\d+)?)", temp_text)
                if m_temp:
                    try: temperature = float(m_temp.group(1))
                    except ValueError: logging.warning(f"Valor temp no numérico: '{m_temp.group(1)}' para {link}.")
                else: logging.warning(f"No se pudo extraer temp del texto: '{temp_text}' para {link}.")
            else: logging.debug(f"No se encontró elem. temp para {link}.")
            deal_info["temperature"] = temperature

            time_element = art.select_one("span.chip span.size--all-s")
            total_hours = 999.0 # Valor por defecto si no se encuentra o falla el parseo
            posted_or_updated = "Desconocido"
            if time_element:
                posted_text = time_element.get_text(strip=True)
                deal_info["posted_text"] = posted_text # Guardar el texto original puede ser útil para debug
                # La lógica existente para determinar 'posted_or_updated' y extraer horas/minutos debería seguir funcionando bien
                posted_or_updated = "Actualizado" if ("Actualizado" in posted_text or "Editado" in posted_text or "Expiró" in posted_text) else "Publicado" # Ajuste para incluir 'Expiró' como 'Actualizado' en términos de estado, aunque el tiempo se calcule igual.
                hours, minutes, days = 0, 0, 0
                m_days = re.search(r"(\d+)\s*d", posted_text, re.IGNORECASE)
                if m_days: days = int(m_days.group(1))
                m_hrs = re.search(r"(\d+)\s*h", posted_text, re.IGNORECASE)
                if m_hrs: hours = int(m_hrs.group(1))
                m_min = re.search(r"(\d+)\s*m(?:in)?", posted_text, re.IGNORECASE) # Añadido 'in' opcional por si acaso
                if m_min: minutes = int(m_min.group(1))

                # Si no se encuentran números, podría ser "Hace un momento" o similar
                if days == 0 and hours == 0 and minutes == 0 and not re.search(r'\d', posted_text):
                    total_hours = 0.0 # Considerarlo como recién publicado
                    logging.debug(f"Tiempo interpretado como 0.0 horas para '{posted_text}' en {link}")
                elif days > 0 or hours > 0 or minutes > 0: # Solo calcular si se encontró alguna unidad de tiempo
                    total_hours = (days * 24) + hours + (minutes / 60.0)
                    logging.debug(f"Tiempo calculado como {total_hours:.2f} horas para '{posted_text}' en {link}")
                else:
                    # Si no se encontró nada pero sí el elemento time_element (caso raro)
                    logging.warning(f"No se pudieron extraer unidades de tiempo (d/h/m) del texto: '{posted_text}' para {link}. Usando default 999.0")
                    # total_hours se queda en 999.0 (el default)
            else:
                logging.debug(f"No se encontró elem. tiempo (selector: 'span.chip span.size--all-s') para {link}. Usando default 999.0")
                # total_hours se queda en 999.0 (el default)

            deal_info["hours_since_posted"] = total_hours
            deal_info["posted_or_updated"] = posted_or_updated

            merchant_element = art.select_one('a[data-t="merchantLink"]') # Busca el enlace marcado específicamente como link de comerciante

            merchant = "N/D" # Valor por defecto si no se encuentra
            if merchant_element:
                merchant_text = merchant_element.get_text(strip=True)
                # A veces puede incluir texto extra, aunque en el ejemplo no parece ser el caso.
                # Una limpieza simple por si acaso:
                merchant = merchant_text.replace("Disponible en", "").strip()
                if not merchant: # Si después de limpiar queda vacío
                    merchant = "N/D"
                    logging.warning(f"Merchant element encontrado pero texto vacío o solo 'Disponible en' para {link}")
            else:
                # Añadimos un log específico para saber si no encuentra el elemento
                logging.debug(f"No se encontró el elemento del comerciante (selector: 'a[data-t=\"merchantLink\"]') para {link}")

            deal_info["merchant"] = merchant
            # --- FIN: Corrección para extraer Comercio ---


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

            image_element = art.select_one("img.thread-image")
            image_url = 'No Image'
            image_url_base = 'No Image'
            if image_element:
                image_url = image_element.get('data-src', image_element.get('src', 'No Image'))
            if image_url and image_url != 'No Image':
                image_url_base = image_url.split("?")[0]
                if "/re/" in image_url_base: image_url_base = image_url_base.split("/re/")[0]
                if image_url_base.startswith("//"): image_url_base = "https:" + image_url_base
                if not image_url_base.startswith(('http://', 'https://')):
                    logging.warning(f"URL de imagen inválida: '{image_url_base}' para {link}. Marcando 'No Image'.")
                    image_url_base = 'No Image'
            deal_info["image_url"] = image_url_base

            description_element = art.select_one(".thread-description .userHtml-content, .userHtml.userHtml-content div")
            description = "No disponible"
            if description_element:
                description = description_element.get_text(strip=True, separator=' ')
                max_desc_len = 250
                if len(description) > max_desc_len: description = description[:max_desc_len].strip() + "..."
            deal_info["description"] = description

            coupon_code = None
            coupon_element = art.select_one(".voucher .buttonWithCode-code")
            if coupon_element: coupon_code = coupon_element.get_text(strip=True)
            deal_info["coupon_code"] = coupon_code

            final_deal = {k: v for k, v in deal_info.items() if v is not None}
            deals_data.append(final_deal)

        except Exception as e:
            logging.exception(f"Error procesando artículo #{i+1} (URL: {link}): {e}. Datos parciales: {deal_info}")
            continue

    logging.info(f"Se parsearon {len(deals_data)} ofertas después de filtrar duplicados y errores internos.")
    return deals_data


def filter_new_hot_deals(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filtra las ofertas y retorna solo aquellas que cumplan las validaciones definidas en is_deal_valid.
    """
    valid_deals = []
    logging.info(f"--- Iniciando filtro de {len(deals)} ofertas ---") # Mensaje añadido
    for i, deal in enumerate(deals):
        # --- INICIO: Código añadido para DEBUG ---
        temp_raw = deal.get("temperature", "N/A")
        hours_raw = deal.get("hours_since_posted", "N/A")
        title_short = deal.get("title", "Sin Título")[:50] # Acortar título para log
        logging.info(f"Deal #{i+1}: Temp='{temp_raw}', Horas='{hours_raw}', Título='{title_short}...'")
        # --- FIN: Código añadido para DEBUG ---

        if is_deal_valid(deal):
            logging.info(f"  -> Deal #{i+1} ES VÁLIDO.") # Mensaje añadido
            valid_deals.append(deal)
        # else: # Opcional: puedes añadir un log si no es válido
        #     logging.info(f"  -> Deal #{i+1} NO es válido.")

    logging.info(f"--- Fin del filtro ---") # Mensaje añadido
    logging.info(f"De {len(deals)} ofertas parseadas, {len(valid_deals)} cumplen con los criterios de validación (temp/tiempo).")
    return valid_deals


# ===== HTTP SERVER & HEALTH CHECK =====

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Handler para el health check y para servir archivos de depuración."""
    # Usa la constante global DEBUG_DIR
    # DEBUG_DIR = "/app/debug" # Ya no es necesario definirla aquí

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
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(f.read())
        except IOError as e:
            self._send_error(500, f"Error al leer el archivo: {os.path.basename(file_path)} - {e}")
        except Exception as e:
            logging.exception(f"Error inesperado sirviendo archivo {file_path}")
            # Evitar doble envío de headers
            try:
                if not self.wfile.closed: # Check if connection is still open
                     # Attempt to send error only if headers not sent (best effort)
                     if hasattr(self, '_headers_buffer') and not self._headers_buffer:
                         self._send_error(500, "Error interno del servidor al servir el archivo.")
            except Exception: # Ignore errors during error handling itself
                 pass


    def _send_error(self, status_code, message):
        # FIX: Llamar a send_response primero para evitar AttributeError
        body = message.encode('utf-8')
        try:
            self.send_response(status_code)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            # Log if sending the error itself fails
            logging.error(f"Error enviando respuesta HTTP de error ({status_code} - {message}): {e}")


    def _serve_debug_index(self):
        if not os.path.isdir(DEBUG_DIR):
            self._send_error(404, "Directorio de depuración no encontrado")
            return
        try:
            files_with_mtime = []
            # Usar el prefijo global
            for f in os.listdir(DEBUG_DIR):
                if f.startswith(DEBUG_FILE_PREFIX) and f.endswith(".html"):
                    try:
                        mtime = os.path.getmtime(os.path.join(DEBUG_DIR, f))
                        files_with_mtime.append((f, mtime))
                    except OSError:
                         files_with_mtime.append((f, 0))

            files_with_mtime.sort(key=lambda x: x[1], reverse=True)
            list_items = ""
            for file, _ in files_with_mtime: # No necesitamos mostrar mtime aquí
                safe_file_url = requests.utils.quote(file)
                safe_file_html = file.replace('<', '<').replace('>', '>')
                list_items += f'<li><a href="/debug/{safe_file_url}">{safe_file_html}</a></li>'

            html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Archivos de Depuración</title></head><body><h1>Archivos HTML en <code>{DEBUG_DIR}</code> (más recientes primero)</h1><ul>{list_items if list_items else "<li>No hay archivos HTML.</li>"}</ul></body></html>"""
            self._send_html_response(html)
        except OSError as e:
            logging.error(f"Error al listar directorio de depuración: {e}")
            self._send_error(500, "Error al listar el directorio de depuración")
        except Exception as e:
            logging.exception("Error inesperado generando índice de depuración")
            self._send_error(500, "Error interno generando índice de depuración")

    def _serve_debug_file(self):
        try:
            rel_path = requests.utils.unquote(self.path[len('/debug/'):])
            if '..' in rel_path or rel_path.startswith('/'):
                self._send_error(400, "Acceso inválido.")
                return
            # Usar la constante global DEBUG_DIR
            full_path = os.path.abspath(os.path.join(DEBUG_DIR, rel_path))
            if not full_path.startswith(os.path.abspath(DEBUG_DIR)):
                 self._send_error(400, "Acceso inválido (fuera del directorio).")
                 return
            self._send_file_response(full_path)
        except Exception as e:
            logging.exception(f"Error decodificando o validando ruta de archivo de depuración: {self.path}")
            self._send_error(400, "URL de archivo inválida.")

    def log_message(self, format, *args):
        try:
            status_code = str(args[1])
            if status_code.startswith(('2', '3')):
                return
        except IndexError:
            pass
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
         os._exit(2) # Salida crítica si el servidor no puede arrancar
    except Exception as e:
        logging.exception(f"Error fatal en el servidor HTTP: {e}")
    finally:
        if httpd:
            httpd.server_close()
            logging.info("Servidor HTTP cerrado.")


# ===== FUNCION PRINCIPAL =====

# Global flag para señalar apagado
shutdown_flag = threading.Event()

def signal_handler(signum, frame):
    """Manejar señales de terminación."""
    logging.warning(f"Señal {signal.Signals(signum).name} recibida. Iniciando apagado...")
    shutdown_flag.set()

def main() -> None:
    """
    Función principal que ejecuta el scraper en un loop, con manejo de errores,
    limpieza de debug y reinicio programado.
    """
    # Registrar manejadores de señal
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Iniciar servidor de health check
    health_thread = threading.Thread(target=run_health_server, name="HealthCheckThread", daemon=True)
    health_thread.start()

    # --- Limpieza inicial de archivos de debug ---
    logging.info("Realizando limpieza inicial de archivos de debug antiguos...")
    # Usa las constantes globales definidas al inicio del archivo
    cleanup_debug_files(DEBUG_DIR, DEBUG_FILE_PREFIX, KEEP_DEBUG_FILES)
    # --- Fin limpieza inicial ---

    seen_deals: Dict[str, int] = load_seen_deals(SEEN_FILE)
    logging.info(f"Inicio del proceso de scraping. {len(seen_deals)} ofertas cargadas desde {SEEN_FILE}.")

    iteration_count = 0
    consecutive_failures = 0
    max_consecutive_failures = 3
    restart_interval_seconds = 12 * 60 * 60 # 12 horas
    start_time = time.time() # Registrar hora de inicio

    # --- Bucle Principal ---
    while not shutdown_flag.is_set():
        iteration_count += 1
        logging.info(f"\n===== INICIO Iteración #{iteration_count} =====")
        iteration_successful = False

        # --- Comprobar tiempo para reinicio programado ---
        elapsed_time = time.time() - start_time
        if elapsed_time >= restart_interval_seconds:
            logging.warning(f"Tiempo de ejecución ({elapsed_time:.0f}s) ha superado el intervalo de reinicio ({restart_interval_seconds}s). Iniciando apagado programado.")
            shutdown_flag.set()
            break # Salir del bucle while inmediatamente

        try:
            logging.info("Revisando Promodescuentos...")
            with get_driver() as driver:
                html = scrape_promodescuentos_hot(driver)

            if not html:
                logging.warning("No se pudo obtener el HTML de la página en esta iteración.")
            else:
                soup = BeautifulSoup(html, "html.parser")
                deals = parse_deals(soup)
                valid_deals = filter_new_hot_deals(deals)
                new_deals_found_count = 0

                if valid_deals:
                    current_seen_in_iteration = {}
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
                            send_telegram_message(deal)
                            current_seen_in_iteration[url] = current_rating
                            new_deals_found_count += 1

                    if current_seen_in_iteration:
                         logging.info(f"Se procesaron {new_deals_found_count} ofertas nuevas/mejoradas.")
                         seen_deals.update(current_seen_in_iteration)
                         save_seen_deals(SEEN_FILE, seen_deals)
                    else:
                         logging.info("No hay ofertas nuevas o mejoradas que cumplan las validaciones en esta iteración.")
                else:
                    logging.info("No se encontraron ofertas válidas después del parseo.")

                iteration_successful = True # Iteración exitosa si obtuvimos y parseamos HTML

        except (WebDriverException, TimeoutException) as driver_error:
            logging.error(f"Error de WebDriver/Timeout durante la iteración #{iteration_count}: {driver_error}")
        except Exception as loop_exception:
            logging.exception(f"Excepción inesperada en la iteración #{iteration_count}: {loop_exception}")

        # --- Manejar Resultado de la Iteración ---
        if iteration_successful:
            logging.info("Iteración completada exitosamente (HTML obtenido y parseado).")
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            logging.warning(f"Iteración #{iteration_count} fallida. Fallos consecutivos: {consecutive_failures}/{max_consecutive_failures}.")
            if consecutive_failures >= max_consecutive_failures:
                logging.error(f"Se alcanzó el máximo de {max_consecutive_failures} fallos consecutivos. Iniciando apagado para permitir reinicio.")
                shutdown_flag.set()
                break # Salir del bucle para apagado ordenado

        # --- Lógica de Espera ---
        if not shutdown_flag.is_set():
            min_wait = 5 * 60
            max_wait = 12 * 60
            wait_seconds = random.randint(min_wait, max_wait)
            minutes, seconds = divmod(wait_seconds, 60)
            logging.info(f"===== FIN Iteración #{iteration_count} =====")
            logging.info(f"Esperando {minutes} min {seconds} seg hasta la próxima revisión...")
            shutdown_flag.wait(timeout=wait_seconds) # Espera interrumpible

    # --- Fin del Bucle Principal (Apagado iniciado) ---
    logging.info("Bucle principal terminado. Realizando tareas finales antes de salir.")
    # Guardado final de ofertas vistas
    try:
        logging.info("Intentando guardado final de ofertas vistas...")
        save_seen_deals(SEEN_FILE, seen_deals)
        logging.info("Guardado final completado.")
    except Exception as final_save_err:
         logging.error(f"Error durante el guardado final: {final_save_err}")

    logging.info("Saliendo del proceso principal.")


if __name__ == "__main__":
    try:
        main()
        # Salir con código 0 para indicar salida normal/planificada
        logging.info("Proceso main() completado. Saliendo con código 0.")
        sys.exit(0)
    except Exception as e:
        # Capturar cualquier error no manejado en main()
        logging.exception("Excepción fatal no capturada en main(): %s", e)
        # Salir con código de error
        sys.exit(3)
    finally:
        # Este log podría no ejecutarse si el proceso es terminado abruptamente
        logging.info("Proceso principal finalizado (bloque finally).")