import httpx
import logging
import json
import asyncio
from typing import Dict, Any, Optional, Set
from app.core.config import settings

logger = logging.getLogger(__name__)

class TelegramService:
    def __init__(self):
        self.base_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"
        self.client = httpx.AsyncClient(timeout=20.0)

    async def close(self):
        await self.client.aclose()

    async def send_message(self, chat_id: str, text: str = None, deal_data: Dict[str, Any] = None) -> bool:
        """
        Envía un mensaje a Telegram. Puede ser un mensaje de oferta (deal_data)
        o un mensaje de texto simple (text).
        """
        if not chat_id:
            logger.warning("target_chat_id vacío, mensaje no enviado.")
            return False

        try:
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            url_api_path = "/sendMessage"

            if text:
                payload["text"] = text
                payload.pop("disable_web_page_preview", None)
            
            elif deal_data:
                self._prepare_deal_payload(deal_data, payload)
                if "photo" in payload:
                    url_api_path = "/sendPhoto"
            else:
                logger.warning("send_message llamado sin deal_data ni text.")
                return False

            url_api = f"{self.base_url}{url_api_path}"
            
            response = await self.client.post(url_api, json=payload)
            response.raise_for_status()
            logger.info(f"Mensaje Telegram enviado a: {chat_id}")
            # await asyncio.sleep(1) # Removed for bulk optimization
            return True

        except httpx.HTTPStatusError as e:
            logger.error(f"Error en API de Telegram para {chat_id}: {e}")
            logger.error(f"Respuesta API Telegram: {e.response.text}")
            return False
        except Exception as e:
            logger.exception(f"Excepción envíando a {chat_id}: {e}")
            return False

    async def send_bulk_notifications(self, chat_ids: Set[str], deal_data: Dict[str, Any]):
        """
        Envía notificaciones a múltiples usuarios de forma concurrente pero controlada.
        """
        if not chat_ids:
            return

        semaphore = asyncio.Semaphore(10) # Limit concurrent requests to prevent 429s

        async def _bounded_send(chat_id):
            async with semaphore:
                try:
                    await self.send_message(chat_id, deal_data=deal_data)
                except Exception as e:
                    logger.error(f"Error enviando bulk a {chat_id}: {e}")

        logger.info(f"Iniciando envío masivo a {len(chat_ids)} usuarios...")
        start_time = asyncio.get_running_loop().time()
        
        tasks = [_bounded_send(chat_id) for chat_id in chat_ids]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        duration = asyncio.get_running_loop().time() - start_time
        logger.info(f"Envío masivo completado en {duration:.2f}s")

    def _prepare_deal_payload(self, deal_data: Dict[str, Any], payload: Dict[str, Any]):
        """Helper to formatting deal message."""
        rating = deal_data.get('rating', 0) # Calculated by AnalyzerService usually
        # If rating is not present, we might want to calculate or pass it. 
        # Assuming AnalyzerService enriched the dict or we calculate simply here?
        # Let's rely on enriched data or defaults.
        
        emoji = "🔥" * rating
        hours_posted = float(deal_data.get('hours_since_posted', 0))
        
        if hours_posted >= 1:
            time_ago_text = f"{round(hours_posted)} horas" if hours_posted >= 1.5 else "1 hora"
        else:
            minutes = round(hours_posted * 60)
            time_ago_text = f"{minutes} minutos" if minutes > 1 else "1 minuto"

        price = deal_data.get('price_display')
        price_text = f"<b>Precio:</b> {price}" if price and price != "N/D" else ""
        
        discount = deal_data.get('discount_percentage')
        discount_text = f"<b>Descuento:</b> {discount}" if discount else ""
        
        coupon = deal_data.get('coupon_code')
        if coupon:
            coupon_safe = coupon.replace('<', '&lt;').replace('>', '&gt;')
            coupon_text = f"<b>Cupón:</b> <code>{coupon_safe}</code>"
        else:
            coupon_text = ""

        opt_lines = "\n".join(filter(None, [price_text, discount_text, coupon_text]))
        if opt_lines: opt_lines = "\n" + opt_lines

        title = str(deal_data.get('title', '')).replace('<', '&lt;').replace('>', '&gt;')
        desc = str(deal_data.get('description', '')).replace('<', '&lt;').replace('>', '&gt;')
        merchant = str(deal_data.get('merchant') or 'N/D').replace('<', '&lt;').replace('>', '&gt;')
        temp = float(deal_data.get('temperature', 0))

        message_content = f"""
<b>{title}</b>

<b>Calificación:</b> {temp:.0f}° {emoji}
<b>{deal_data.get('posted_or_updated', 'Publicado')} hace:</b> {time_ago_text}
<b>Comercio:</b> {merchant}
{opt_lines}

<b>Descripción:</b>
{desc}
        """.strip()

        deal_url = deal_data.get('url', '')
        if deal_url:
            reply_markup = {
                "inline_keyboard": [[{"text": "Ver Oferta", "url": deal_url}]]
            }
            payload["reply_markup"] = json.dumps(reply_markup)

        image_url = deal_data.get('image_url', '')
        if image_url and isinstance(image_url, str) and image_url.startswith(('http', 'https')):
            payload["photo"] = image_url
            payload["caption"] = message_content
            if len(message_content) > 1024:
                payload["caption"] = message_content[:1020] + "..."
        else:
            payload["text"] = message_content
            if len(message_content) > 4096:
                payload["text"] = message_content[:4092] + "..."
