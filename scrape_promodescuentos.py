#!/usr/bin/env python3
import re
import time
import random
import os
import json
import logging
from contextlib import contextmanager
from typing import Dict, List, Any, Generator, Set
import signal # Import signal for graceful shutdown attempts
import glob # Para buscar archivos con patrones
import sys # Para salir limpiamente

import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup



import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import mimetypes

# Global set de suscriptores y lock para acceso concurrente
# Definido aquí para asegurar que existe antes de cualquier función o configuración que pueda usarlo.
subscribers: Set[str] = set()
subscribers_lock = threading.Lock()

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
# El TELEGRAM_CHAT_ID global puede usarse para admin o si el bot solo tiene un usuario principal
# Sin embargo, para múltiples usuarios, el chat_id vendrá del mensaje o de una lista de suscriptores.
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
# TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "") # Para admin o notificaciones especiales, puede ser redundante con ADMIN_CHAT_IDS
APP_BASE_URL: str = os.getenv("APP_BASE_URL", "") # Ej: https://tu-app.onrender.com

ADMIN_CHAT_IDS_STR: str = os.getenv("ADMIN_CHAT_IDS", "") # Lista de CHAT_IDs separados por coma
logging.info(f"Raw ADMIN_CHAT_IDS_STR from env: '{ADMIN_CHAT_IDS_STR}'") # DEBUG LINE
ADMIN_CHAT_IDS: Set[str] = set()
if ADMIN_CHAT_IDS_STR:
    ADMIN_CHAT_IDS = {chat_id.strip() for chat_id in ADMIN_CHAT_IDS_STR.split(',') if chat_id.strip()}
    logging.info(f"CHAT_IDs administrativos cargados para notificación siempre: {ADMIN_CHAT_IDS}")

# Archivo para guardar ofertas ya vistas
SEEN_FILE: str = "seen_hot_deals.json"
# Archivo para guardar suscriptores
SUBSCRIBERS_FILE: str = "subscribers.json"

# ==== CONFIGURACIONES DE DEBUG ==== (Constantes globales)
DEBUG_DIR = os.getenv("DEBUG_DIR", "debug")
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
    Adicionalmente, excluye ofertas ya expiradas.
    """
    # --- NUEVO: Excluir ofertas expiradas ---
    # Usamos el texto original extraído por parse_deals
    posted_text = deal.get("posted_text", "")
    if "Expiró" in posted_text:
        logging.debug(f"Oferta {deal.get('url', 'N/A')} ignorada por estar expirada ('{posted_text}').")
        return False
    # --- FIN NUEVO ---

    temp = deal.get("temperature", 0)
    hours = deal.get("hours_since_posted", 999) # Default alto si falta

    # Asegurarse de que temp y hours sean numéricos
    try:
        temp_float = float(temp)
        hours_float = float(hours)
    except (ValueError, TypeError):
        logging.warning(f"Valores no numéricos en is_deal_valid para {deal.get('url', 'URL desconocida')}: temp='{temp}', hours='{hours}'. Se considera inválida.")
        return False # No puede cumplir las condiciones numéricas

    # --- Condiciones originales (ahora con temp_float y hours_float) ---
    # Ahora las temperaturas negativas serán filtradas aquí automáticamente porque temp_float será < 150
    if temp_float >= 10 and hours_float < 1:
        logging.debug(f"Deal {deal.get('url', 'N/A')} validado por Regla 1 (Temp: {temp_float}, Horas: {hours_float})")
        return True
    if temp_float >= 300 and hours_float < 2:
        logging.debug(f"Deal {deal.get('url', 'N/A')} validado por Regla 2 (Temp: {temp_float}, Horas: {hours_float})")
        return True
    if temp_float >= 500 and hours_float < 5:
        logging.debug(f"Deal {deal.get('url', 'N/A')} validado por Regla 3 (Temp: {temp_float}, Horas: {hours_float})")
        return True
    if temp_float >= 1000 and hours_float < 8:
        logging.debug(f"Deal {deal.get('url', 'N/A')} validado por Regla 4 (Temp: {temp_float}, Horas: {hours_float})")
        return True
    # --- FIN Condiciones originales ---

    # Si no cumplió ninguna condición (o era expirada)
    logging.debug(f"Deal {deal.get('url', 'N/A')} NO validado (Temp: {temp_float}, Horas: {hours_float}, Texto Exp: '{posted_text}')")
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

def load_subscribers_global(filepath: str) -> None:
    """
    Carga los chat_id de los suscriptores desde un archivo JSON al set global 'subscribers'.
    """
    global subscribers
    if not os.path.isfile(filepath):
        logging.info(f"Archivo de suscriptores ({filepath}) no encontrado. Empezando con set vacío.")
        subscribers = set()
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                logging.warning(f"El contenido de {filepath} no es una lista JSON válida. Empezando con set vacío.")
                subscribers = set()
                return
            with subscribers_lock:
                subscribers = {str(chat_id) for chat_id in data if chat_id}
            logging.info(f"Cargados {len(subscribers)} suscriptores desde {filepath} al set global.")
    except json.JSONDecodeError:
        logging.error(f"Error decodificando JSON desde {filepath}. El archivo podría estar corrupto. Empezando con set vacío.")
        subscribers = set()
    except Exception as e:
        logging.exception(f"Error inesperado cargando los suscriptores: {e}")
        subscribers = set()

def save_subscribers_global(filepath: str) -> None:
    """
    Guarda los chat_id del set global 'subscribers' en un archivo JSON de forma segura.
    """
    global subscribers
    temp_filepath = filepath + ".tmp"
    try:
        with subscribers_lock:
            subscribers_list = sorted(list(subscribers))
        with open(temp_filepath, "w", encoding="utf-8") as f:
            json.dump(subscribers_list, f, indent=4)
        os.replace(temp_filepath, filepath)
        logging.info(f"Suscriptores ({len(subscribers_list)}) guardados correctamente en {filepath} desde el set global.")
    except Exception as e:
        logging.error(f"Error guardando los suscriptores en {filepath}: {e}")
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
            except OSError as remove_err:
                logging.error(f"Error eliminando archivo temporal {temp_filepath} de suscriptores: {remove_err}")

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

def send_telegram_message(deal_data: Dict[str, Any], target_chat_id: str, message_text_override: str = None) -> None:
    """
    Envía un mensaje a Telegram. Puede ser un mensaje de oferta (deal_data)
    o un mensaje de texto simple (message_text_override).
    """
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN no configurado, mensaje no enviado.")
        return
    if not target_chat_id:
        logging.warning("target_chat_id vacío, mensaje no enviado.")
        return

    try:
        payload: Dict[str, Any] = {
            "chat_id": target_chat_id,
            "parse_mode": "HTML",
            "disable_web_page_preview": True, # Por defecto para mensajes de oferta
        }
        url_api_path = "/sendMessage" # Por defecto

        if message_text_override:
            payload["text"] = message_text_override
            # Para mensajes de texto simples, no necesitamos reply_markup ni web page preview usualmente.
            payload.pop("disable_web_page_preview", None)
            payload.pop("reply_markup", None)
        
        elif deal_data:
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

            message_content = f"""
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

            reply_markup_data = {
                "inline_keyboard": [[{"text": "Ver Oferta", "url": deal_url}]]
            }
            payload["reply_markup"] = json.dumps(reply_markup_data)

            image_url: str = deal_data.get('image_url', '')
            use_photo = False
            if image_url and isinstance(image_url, str) and image_url != 'No Image' and image_url.startswith(('http://', 'https://')):
                use_photo = True
                url_api_path = "/sendPhoto"
                payload["photo"] = image_url
                payload["caption"] = message_content
                if len(message_content) > 1024:
                     payload["caption"] = message_content[:1020] + "..."
                     logging.warning(f"Caption truncated for photo message (URL: {deal_url})")
            else:
                payload["text"] = message_content
                if len(message_content) > 4096:
                    payload["text"] = message_content[:4092] + "..."
                    logging.warning(f"Text message truncated (URL: {deal_url})")
                if image_url and image_url != 'No Image':
                    logging.warning(f"Invalid or missing image URL: '{image_url}'. Sending text message.")
        else:
            logging.warning("send_telegram_message llamado sin deal_data ni message_text_override.")
            return

        url_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}{url_api_path}"
        
        logging.debug(f"Sending Telegram {('photo' if url_api_path == '/sendPhoto' and deal_data else 'message')}. Target: {target_chat_id}. Payload keys: {list(payload.keys())}")
        resp = requests.post(url_api, json=payload, timeout=20)
        resp.raise_for_status()
        logging.info(f"Mensaje Telegram enviado correctamente a: {target_chat_id} para {'oferta ' + deal_data.get('url', 'N/A') if deal_data else 'mensaje de texto'}")
        time.sleep(1) # Mantener un pequeño delay

    except requests.exceptions.RequestException as e:
        deal_url_log = deal_data.get('url', 'N/A') if deal_data else "N/A (mensaje de texto)"
        logging.error(f"Error en API de Telegram para {deal_url_log} (target: {target_chat_id}): {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Respuesta API Telegram: Status={e.response.status_code}, Body={e.response.text}")
    except Exception as e:
        deal_url_log = deal_data.get('url', 'N/A') if deal_data else "N/A (mensaje de texto)"
        logging.exception(f"Excepción inesperada enviando mensaje Telegram a {target_chat_id} para {deal_url_log}: {e}")

# ===== FUNCIONES PARA EL DRIVER =====

# Función init_driver y get_driver eliminadas (Selenium removido)

# ===== FUNCIONES PARA EL SCRAPING =====

def scrape_promodescuentos_hot() -> str:
    """
    Extrae el HTML de la página 'nuevas' de Promodescuentos usando requests.
    Incluye manejo de errores mejorado, guardado de HTML y limpieza de archivos de debug.
    """
    url = "https://www.promodescuentos.com/nuevas"
    html_content = ""
    debug_file_path = None # Ruta específica para el archivo en caso de error
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    
    # Headers para simular un navegador real (Chrome en macOS)
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    # Asegurarse de que el directorio de debug exista
    os.makedirs(DEBUG_DIR, exist_ok=True)

    try:
        logging.info(f"Accediendo a la URL: {url} con requests...")
        response = requests.get(url, headers=headers, timeout=20)

        # Verificar status code
        if response.status_code == 200:
            html_content = response.text
            logging.info(f"HTML obtenido (longitud: {len(html_content)} caracteres).")
        else:
            logging.error(f"Error scraping: Status Code {response.status_code}")
            debug_file_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}ERROR_{response.status_code}_{timestamp}.html")
            html_content = response.text # Guardar lo que nos devolvieron

    except requests.exceptions.Timeout:
        logging.error(f"Error scraping (Timeout): La solicitud a {url} excedió el tiempo límite.")
        debug_file_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}TIMEOUT_{timestamp}.html")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error scraping (RequestException): {e}")
        debug_file_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}REQ_EXCEPTION_{timestamp}.html")
    except Exception as e:
        logging.exception(f"Error inesperado durante scraping (URL: {url}): {e}")
        debug_file_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}UNEXPECTED_ERROR_{timestamp}.html")

    # --- Bloque de guardado de HTML (unificado para éxito o error) ---
    save_path = None
    html_to_save = html_content # Usar el HTML obtenido si existe
    log_level = logging.INFO
    log_msg = ""

    if html_content and not debug_file_path: # Éxito (status 200)
         save_path = os.path.join(DEBUG_DIR, f"{DEBUG_FILE_PREFIX}SUCCESS_{timestamp}.html")
         log_msg = f"HTML guardado en {save_path}"
    elif debug_file_path: # Error (la ruta ya contiene el tipo de error)
         save_path = debug_file_path
         log_level = logging.WARNING # Loguear como warning si guardamos HTML de error
         log_msg = f"HTML en error guardado en {save_path}"
    else:
         # Caso raro donde html_content está vacío pero no hubo excepción (ej. respuesta vacía 200)
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

    return html_to_save


def parse_deals(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Parses the HTML with BeautifulSoup and extracts deal information.
    Prioritizes extracting data from 'data-vue3' JSON attributes for reliability.
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
        
        # --- Strategy 1: Extract from Vue JSON (Preferred) ---
        vue_data = {}
        try:
            # Find the divs with Vue data. 
            # We look for any .js-vue3 div and check its content
            vue_elements = art.select("div.js-vue3[data-vue3]")
            for el in vue_elements:
                try:
                    data_attr = el.get("data-vue3")
                    if not data_attr: continue
                    json_data = json.loads(data_attr)
                    
                    # Target specific component
                    if json_data.get("name") == "ThreadMainListItemNormalizer":
                        vue_props = json_data.get("props", {}).get("thread", {})
                        if vue_props:
                            vue_data = vue_props
                            break
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logging.warning(f"Error extracting Vue JSON for article #{i+1}: {e}")

        # --- Extract Fields (JSON > HTML) ---

        # 1. Title & URL
        try:
            if vue_data:
                title = vue_data.get("title")
                # Construct URL if 'link' is empty or relative
                # The JSON often has 'titleSlug' and 'threadId'
                if vue_data.get("titleSlug") and vue_data.get("threadId"):
                     # Standard format: https://www.promodescuentos.com/ofertas/slug-id
                     link = f"https://www.promodescuentos.com/ofertas/{vue_data['titleSlug']}-{vue_data['threadId']}"
                else:
                     link = vue_data.get("shareableLink") or vue_data.get("link")
            else:
                # Fallback to HTML
                title_element = art.select_one("strong.thread-title a.thread-link, a.cept-tt.thread-link")
                if not title_element:
                    logging.debug(f"Artículo #{i+1}: Sin elemento de título/link. Saltando.")
                    continue
                title = title_element.get_text(strip=True)
                link = title_element.get("href", "").strip()

            if not link:
                logging.debug(f"Artículo #{i+1}: Link vacío. Saltando.")
                continue
            if link.startswith("/"):
                link = "https://www.promodescuentos.com" + link
            
            deal_info["url"] = link
            deal_info["title"] = title

            if link in processed_urls:
                logging.debug(f"Artículo #{i+1}: URL duplicada ({link}). Saltando.")
                continue
            processed_urls.add(link)

        except Exception as e:
             logging.error(f"Error extracting title/url for article #{i+1}: {e}")
             continue

        # 2. Temperature
        deal_info["temperature"] = 0
        try:
            if vue_data and "temperature" in vue_data:
                deal_info["temperature"] = float(vue_data["temperature"])
            else:
                # Fallback HTML
                temp_element = art.select_one(".vote-temp") 
                if temp_element:
                    temp_text = temp_element.get_text(strip=True).replace("°", "").strip()
                    # Try validation roughly
                    if re.match(r"^-?\d+(\.\d+)?$", temp_text):
                        deal_info["temperature"] = float(temp_text)
        except (ValueError, TypeError):
            deal_info["temperature"] = 0

        # 3. Time / Published At
        deal_info["hours_since_posted"] = 999.0
        deal_info["posted_or_updated"] = "Desconocido"
        
        try:
            if vue_data and "publishedAt" in vue_data:
                published_at = int(vue_data["publishedAt"])
                if published_at > 0:
                    current_ts = time.time()
                    # hours = seconds / 3600
                    hours_since = (current_ts - published_at) / 3600
                    deal_info["hours_since_posted"] = hours_since
                    
                    # Generate a human readable string for notification
                    if hours_since < 1:
                        deal_info["posted_or_updated"] = f"Hace {int(hours_since*60)} m"
                    elif hours_since < 24:
                         deal_info["posted_or_updated"] = f"Hace {int(hours_since)} h"
                    else:
                         deal_info["posted_or_updated"] = f"Hace {int(hours_since/24)} d"
                else:
                     deal_info["hours_since_posted"] = 999.0
            else:
                # Fallback HTML - often missing in static HTML for Requests
                time_element = art.select_one("span.chip span.size--all-s")
                if time_element:
                    posted_text = time_element.get_text(strip=True)
                    deal_info["posted_text"] = posted_text
                    deal_info["posted_or_updated"] = posted_text
                    
                    hours = 999.0
                    if "min" in posted_text or "m" in posted_text.split():
                         m = re.search(r"(\d+)", posted_text)
                         if m: hours = int(m.group(1)) / 60.0
                    elif "h" in posted_text:
                         m = re.search(r"(\d+)", posted_text)
                         if m: hours = float(m.group(1))
                    elif "d" in posted_text:
                         m = re.search(r"(\d+)", posted_text)
                         if m: hours = int(m.group(1)) * 24.0
                    
                    if hours != 999.0:
                        deal_info["hours_since_posted"] = hours

        except Exception:
             deal_info["hours_since_posted"] = 999.0

        # 4. Merchant
        if vue_data and "merchant" in vue_data and vue_data["merchant"]:
             deal_info["merchant"] = vue_data["merchant"].get("merchantName", "N/D")
        else:
            merchant_element = art.select_one('a[data-t="merchantLink"]')
            deal_info["merchant"] = merchant_element.get_text(strip=True).replace("Disponible en", "").strip() if merchant_element else "N/D"

        # 5. Price
        if vue_data:
             price_val = vue_data.get("price", 0)
             if price_val > 0:
                 deal_info["price_display"] = f"${price_val:,.2f}"
             else:
                 deal_info["price_display"] = "Gratis" if price_val == 0 else "N/D"
        else:
            price_element = art.select_one(".thread-price")
            deal_info["price_display"] = price_element.get_text(strip=True) if price_element else "N/D"

        # 6. Discount %
        deal_info["discount_percentage"] = None
        # Not always in JSON top level, but let's check HTML fallback primarily
        discount_badge = art.select_one(".thread-discount, .textBadge--green")
        if discount_badge:
            discount_text = discount_badge.get_text(strip=True)
            m_discount = re.search(r"-?(\d+)%", discount_text)
            if m_discount: deal_info["discount_percentage"] = f"{m_discount.group(1)}%"

        # 7. Image
        image_url = 'No Image'
        try:
            # Try to construct from Vue data first
            if vue_data:
                main_image = vue_data.get("mainImage")
                if main_image:
                    path = main_image.get("path")
                    name = main_image.get("name")
                    if path and name:
                        image_url = f"https://static.promodescuentos.com/{path}/{name}.jpg"
            
            # Fallback to HTML if Vue data didn't work
            if image_url == 'No Image':
                image_element = art.select_one("img.thread-image")
                if image_element:
                     image_url = image_element.get('data-src', image_element.get('src', 'No Image'))
                
                if image_url != 'No Image' and image_url.startswith("//"):
                    image_url = "https:" + image_url
        except Exception as e:
            logging.warning(f"Error extracting image for {link}: {e}")

        # Basic validation
        if not image_url.startswith(('http://', 'https://')):
             if image_url != 'No Image':
                  logging.warning(f"URL de imagen inválida/incompleta: '{image_url}' para {link}. Resetting to 'No Image'.")
             image_url = 'No Image'

        deal_info["image_url"] = image_url

        # 8. Description
        description = "No disponible"
        desc_element = art.select_one(".thread-description .userHtml-content, .userHtml.userHtml-content div")
        if desc_element:
            description = desc_element.get_text(strip=True, separator=' ')
            if len(description) > 250: description = description[:250].strip() + "..."
        deal_info["description"] = description

        # 9. Coupon
        coupon_code = None
        if vue_data and "voucherCode" in vue_data and vue_data["voucherCode"]:
            coupon_code = vue_data["voucherCode"]
        else:
            coupon_element = art.select_one(".voucher .buttonWithCode-code")
            if coupon_element: coupon_code = coupon_element.get_text(strip=True)
        deal_info["coupon_code"] = coupon_code

        final_deal = {k: v for k, v in deal_info.items() if v is not None}
        deals_data.append(final_deal)

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


# ===== HTTP SERVER & HEALTH CHECK & WEBHOOK =====

class RequestHandler(BaseHTTPRequestHandler): # Renombrado de HealthCheckHandler
    """
    Handler para el health check, servir archivos de depuración y procesar webhooks de Telegram.
    """
    # Usa la constante global DEBUG_DIR
    # DEBUG_DIR = "/app/debug" # Ya no es necesario definirla aquí

    def do_GET(self):
        if self.path == '/':
            self._send_json_response({'status': 'running', 'service': 'promodescuentos-scraper'})
        elif self.path == '/debug' or self.path == '/debug/':
            self._serve_debug_index()
        elif self.path.startswith('/debug/'):
            self._serve_debug_file()
        # Podrías añadir un endpoint GET para /webhook/<TOKEN> para verificar que está configurado,
        # pero Telegram usa POST para enviar actualizaciones.
        else:
            self._send_error(404, "Ruta no encontrada")

    def do_POST(self):
        # El path del webhook debe ser secreto, idealmente incluyendo el token del bot
        webhook_path = f"/webhook/{TELEGRAM_BOT_TOKEN}" 
        if self.path == webhook_path:
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length == 0:
                    logging.warning("Webhook recibió POST vacío.")
                    self._send_error(400, "POST vacío")
                    return

                post_data = self.rfile.read(content_length)
                logging.info(f"Webhook recibió datos: {post_data.decode('utf-8')[:200]}...") # Loguear solo una parte
                
                update = json.loads(post_data.decode('utf-8'))
                
                self._process_telegram_update(update)
                
                # Responder a Telegram que todo OK
                self._send_json_response({"status": "ok"}, status=200)

            except json.JSONDecodeError:
                logging.error("Error decodificando JSON del webhook de Telegram.")
                self._send_error(400, "JSON inválido")
            except Exception as e:
                logging.exception("Error procesando webhook de Telegram.")
                self._send_error(500, "Error interno del servidor procesando webhook")
        else:
            self._send_error(404, "Ruta POST no encontrada o token inválido en URL.")

    def _process_telegram_update(self, update: Dict[str, Any]):
        global subscribers # Necesario para modificar el set global
        global subscribers_lock # Asegurar que estamos usando el lock global
        
        if 'message' in update:
            message = update['message']
            chat_id = str(message['chat']['id'])
            text = message.get('text', '')

            logging.info(f"Mensaje recibido de chat_id {chat_id}: '{text}'")

            if text.lower() == '/start' or text.lower() == '/subscribe':
                added = False
                with subscribers_lock:
                    if chat_id not in subscribers:
                        subscribers.add(chat_id)
                        added = True
                
                if added:
                    save_subscribers_global(SUBSCRIBERS_FILE) # Guardar la lista actualizada
                    welcome_message = "¡Hola! 🎉 Te has suscrito a las notificaciones de ofertas de Promodescuentos. Te avisaré cuando encuentre nuevas ofertas calientes."
                    send_telegram_message(deal_data=None, target_chat_id=chat_id, message_text_override=welcome_message)
                    logging.info(f"Chat ID {chat_id} añadido a suscriptores.")
                else:
                    already_subscribed_message = "Ya estás suscrito. ¡Gracias por tu interés! 👍"
                    send_telegram_message(deal_data=None, target_chat_id=chat_id, message_text_override=already_subscribed_message)
                    logging.info(f"Chat ID {chat_id} ya estaba suscrito.")
            
            elif text.lower() == '/stop' or text.lower() == '/unsubscribe':
                removed = False
                with subscribers_lock:
                    if chat_id in subscribers:
                        subscribers.discard(chat_id)
                        removed = True
                
                if removed:
                    save_subscribers_global(SUBSCRIBERS_FILE)
                    goodbye_message = "Has cancelado tu suscripción. Ya no recibirás notificaciones de ofertas. Puedes volver a suscribirte con /start."
                    send_telegram_message(deal_data=None, target_chat_id=chat_id, message_text_override=goodbye_message)
                    logging.info(f"Chat ID {chat_id} eliminado de suscriptores.")
                else:
                    not_subscribed_message = "No estabas suscrito. Usa /start para recibir notificaciones."
                    send_telegram_message(deal_data=None, target_chat_id=chat_id, message_text_override=not_subscribed_message)
                    logging.info(f"Chat ID {chat_id} intentó desuscribirse pero no estaba en la lista.")

            else:
                # Respuesta por defecto para otros mensajes
                help_message = "Soy un bot que te notifica sobre ofertas de Promodescuentos. Usa /start para suscribirte o /stop para cancelar la suscripción."
                send_telegram_message(deal_data=None, target_chat_id=chat_id, message_text_override=help_message)
        
        elif 'callback_query' in update:
            # Manejar callback queries si añades botones inline en el futuro
            # Por ahora, solo logueamos.
            callback_query = update['callback_query']
            chat_id = str(callback_query['message']['chat']['id'])
            data = callback_query.get('data')
            logging.info(f"Callback query recibido de chat_id {chat_id} con data: {data}")
            # Podrías enviar una respuesta al callback query aquí con answerCallbackQuery

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
            if status_code.startswith(('2', '3')): # No loguear 2xx y 3xx
                # También podemos evitar loguear el POST del webhook si es muy verboso
                # path = args[0].split()[1] if len(args[0].split()) > 1 else ""
                # if path == f"/webhook/{TELEGRAM_BOT_TOKEN}" and args[0].startswith("POST"):
                #    return
                return 
        except IndexError:
            pass
        logging.info(f"HTTP Request: {args[0]}") # Loguear otras peticiones

def run_server(): # Renombrado de run_health_server
    server_address = ('0.0.0.0', int(os.getenv("PORT", 10000))) # Usar PORT de Render si está disponible
    httpd = None
    try:
        httpd = HTTPServer(server_address, RequestHandler) # Usar el handler renombrado
        logging.info(f"Servidor HTTP iniciado en {server_address[0]}:{server_address[1]} (para health checks y webhook)")
        httpd.serve_forever()
    except OSError as e:
         logging.error(f"No se pudo iniciar el servidor HTTP en {server_address} (quizás el puerto ya está en uso?): {e}")
         os._exit(2) 
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
    Función principal que ejecuta el scraper en un loop y gestiona el bot.
    """
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Cargar suscriptores al inicio
    load_subscribers_global(SUBSCRIBERS_FILE)
    logging.info(f"Suscriptores iniciales cargados: {len(subscribers)}")

    # Configurar Webhook si APP_BASE_URL y TELEGRAM_BOT_TOKEN están definidos
    if APP_BASE_URL and TELEGRAM_BOT_TOKEN:
        webhook_url = f"{APP_BASE_URL.rstrip('/')}/webhook/{TELEGRAM_BOT_TOKEN}"
        try:
            set_webhook_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
            params = {"url": webhook_url}
            # Puedes añadir allowed_updates aquí si solo quieres ciertos tipos de updates, ej: ["message", "callback_query"]
            # params["allowed_updates"] = json.dumps(["message"]) 
            response = requests.post(set_webhook_url, params=params, timeout=10)
            response.raise_for_status()
            result = response.json()
            if result.get("ok"):
                logging.info(f"Webhook configurado exitosamente en: {webhook_url}. Resultado: {result.get('description')}")
            else:
                logging.error(f"Fallo al configurar webhook en {webhook_url}. Respuesta: {result}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error de red configurando webhook {webhook_url}: {e}")
        except json.JSONDecodeError:
            logging.error(f"Error decodificando respuesta de setWebhook: {response.text}")
        except Exception as e:
            logging.exception(f"Excepción inesperada configurando webhook {webhook_url}: {e}")
    else:
        logging.warning("APP_BASE_URL o TELEGRAM_BOT_TOKEN no definidos. El webhook no será configurado. El bot no recibirá mensajes de usuarios.")

    # Iniciar servidor HTTP (Health Check y Webhook)
    server_thread = threading.Thread(target=run_server, name="HTTPServerThread", daemon=True) # Renombrado
    server_thread.start()

    # --- Limpieza inicial de archivos de debug ---
    logging.info("Realizando limpieza inicial de archivos de debug antiguos...")
    # Usa las constantes globales definidas al inicio del archivo
    cleanup_debug_files(DEBUG_DIR, DEBUG_FILE_PREFIX, KEEP_DEBUG_FILES)
    # --- Fin limpieza inicial ---

    seen_deals: Dict[str, int] = load_seen_deals(SEEN_FILE)
    logging.info(f"Inicio del proceso de scraping. {len(seen_deals)} ofertas cargadas desde {SEEN_FILE}. {len(subscribers)} suscriptores cargados.")

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
            logging.info("Revisando Promodescuentos con requests...")
            # Ya no usamos driver context manager
            html = scrape_promodescuentos_hot()

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
                            
                            # Combinar suscriptores y admins, evitando duplicados
                            recipients_to_notify = set()
                            global subscribers_lock # Asegurar que estamos usando el lock global
                            with subscribers_lock:
                                recipients_to_notify.update(subscribers) # Añadir suscriptores
                            
                            recipients_to_notify.update(ADMIN_CHAT_IDS) # Añadir IDs de administrador (ya es un set)

                            if not recipients_to_notify:
                                logging.info("No hay destinatarios (ni suscriptores ni admin IDs) a los que notificar.")
                            else:
                                logging.info(f"Enviando oferta a {len(recipients_to_notify)} destinatario(s) (suscritos + admin).")
                                for chat_id_recipient in recipients_to_notify:
                                    try:
                                        send_telegram_message(deal, chat_id_recipient)
                                    except Exception as e_send:
                                        logging.error(f"Error enviando mensaje a destinatario {chat_id_recipient}: {e_send}")
                            
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

        except requests.exceptions.RequestException as req_error:
            logging.error(f"Error de solicitud (Requests) durante la iteración #{iteration_count}: {req_error}")
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