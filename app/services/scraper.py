import httpx
import logging
import json
import time
import re
import os
import random
import asyncio
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

class ScraperService:
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None

    async def startup(self):
        """Initializes the persistent HTTP client."""
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=20.0, 
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
            )
            logger.info("ScraperService HTTP client initialized.")

    async def close(self):
        """Closes the persistent HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None
            logger.info("ScraperService HTTP client closed.")

    def _get_random_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    async def fetch_page(self, url: str) -> Optional[str]:
        if self.client is None:
            await self.startup()
            
        max_retries = 3
        backoff_factor = 2
        
        for attempt in range(max_retries):
            try:
                headers = self._get_random_headers() # Rotate on each request
                logger.info(f"Fetching {url} (Attempt {attempt+1}/{max_retries})...")
                
                response = await self.client.get(url, headers=headers)
                
                if response.status_code == 200:
                    return response.text
                
                elif 400 <= response.status_code < 500:
                        logger.error(f"Client error {response.status_code} fetching {url}. Not retrying.")
                        return None
                else:
                    logger.warning(f"Server error {response.status_code} fetching {url}.")
            
            except httpx.RequestError as e:
                logger.warning(f"Network error fetching {url}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error fetching {url}: {e}")
                return None

            # Exponential backoff if not last attempt
            if attempt < max_retries - 1:
                sleep_time = backoff_factor ** attempt + random.uniform(0, 1)
                logger.info(f"Retrying in {sleep_time:.2f}s...")
                await asyncio.sleep(sleep_time)

        logger.error(f"Max retries reached for {url}.")
        return None

    def _save_debug_html(self, content: str, suffix: str):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{settings.DEBUG_DIR}/debug_html_{suffix}_{timestamp}.html"
        os.makedirs(settings.DEBUG_DIR, exist_ok=True)
        try:
             with open(filename, "w", encoding="utf-8") as f:
                 f.write(content)
        except Exception as e:
             logger.error(f"Could not save debug file: {e}")

    def parse_deals(self, html_content: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html_content, "html.parser")
        articles = soup.select("article.thread")
        logger.info(f"Found {len(articles)} articles.")
        
        deals = []
        processed_urls = set()

        for art in articles:
            deal = self._extract_deal_info(art)
            if deal and deal.get("url") and deal["url"] not in processed_urls:
                processed_urls.add(deal["url"])
                deals.append(deal)
        
        return deals

    def _extract_deal_info(self, art: BeautifulSoup) -> Dict[str, Any]:
        deal_info = {}
        
        # --- 1. Vue Data Extraction Strategy ---
        vue_data = {}
        try:
            vue_elems = art.select("div.js-vue3[data-vue3]")
            for el in vue_elems:
                try:
                    data = json.loads(el.get("data-vue3", "{}"))
                    if data.get("name") == "ThreadMainListItemNormalizer":
                        vue_data = data.get("props", {}).get("thread", {})
                        break
                except: pass
        except Exception as e:
            logger.error(f"Error extracting Vue data: {e}")

        # --- 2. Title & URL ---
        try:
            if vue_data:
                deal_info["title"] = vue_data.get("title")
                if vue_data.get("titleSlug") and vue_data.get("threadId"):
                    deal_info["url"] = f"https://www.promodescuentos.com/ofertas/{vue_data['titleSlug']}-{vue_data['threadId']}"
                else:
                     deal_info["url"] = vue_data.get("shareableLink") or vue_data.get("link")
            else:
                # Fallback HTML
                title_el = art.select_one("strong.thread-title a, a.thread-link")
                if not title_el: return {}
                deal_info["title"] = title_el.get_text(strip=True)
                link = title_el.get("href", "")
                if link.startswith("/"): link = "https://www.promodescuentos.com" + link
                deal_info["url"] = link
        except Exception as e:
            logger.error(f"Error extracting Title/URL: {e}")
            return {}

        # --- 3. Merchant ---
        deal_info["merchant"] = "N/D"
        try:
            if vue_data and vue_data.get("merchant"):
                 m = vue_data["merchant"]
                 if isinstance(m, dict):
                     # Fix: JSON uses 'merchantName', not 'name'
                     deal_info["merchant"] = m.get("merchantName") or m.get("name") or "N/D"
                 else:
                     deal_info["merchant"] = str(m)
            elif vue_data and vue_data.get("merchantName"):
                 deal_info["merchant"] = vue_data.get("merchantName")
            else:
                # HTML Fallback
                merchant_el = art.select_one('a[data-t="merchantLink"], span.thread-merchant')
                if merchant_el:
                    deal_info["merchant"] = merchant_el.get_text(strip=True).replace("Disponible en", "").strip()
            
            # Final Fallback: Extract from title (e.g. "Amazon: Product")
            if deal_info["merchant"] == "N/D" and deal_info.get("title"):
                parts = deal_info["title"].split(":", 1)
                if len(parts) > 1 and len(parts[0]) < 20: # Heuristic for merchant name length
                    deal_info["merchant"] = parts[0].strip()
        except Exception as e:
            logger.debug(f"Error extracting Merchant: {e}")

        # --- 4. Price ---
        deal_info["price_display"] = None
        try:
            if vue_data and "price" in vue_data:
                 try:
                     price_val = float(vue_data["price"])
                     deal_info["price_display"] = f"${price_val:,.2f}" if price_val > 0 else "Gratis"
                 except: 
                     deal_info["price_display"] = vue_data.get("priceDisplay")
            
            if not deal_info["price_display"]:
                 # HTML Fallback
                 price_el = art.select_one(".thread-price")
                 if price_el:
                     deal_info["price_display"] = price_el.get_text(strip=True)
        except Exception as e:
            logger.debug(f"Error extracting Price: {e}")

        # --- 5. Discount ---
        deal_info["discount_percentage"] = vue_data.get("discountPercentage")
        try:
            if not deal_info["discount_percentage"]:
                 discount_el = art.select_one(".thread-discount, .textBadge--green")
                 if discount_el:
                     txt = discount_el.get_text(strip=True)
                     if "%" in txt: deal_info["discount_percentage"] = txt
        except Exception as e:
            logger.debug(f"Error extracting Discount: {e}")

        # --- 6. Image ---
        deal_info["image_url"] = None
        try:
            if vue_data:
                main_image = vue_data.get("mainImage", {})
                if isinstance(main_image, dict):
                    path = main_image.get("path")
                    name = main_image.get("name")
                    if path and name:
                        deal_info["image_url"] = f"https://static.promodescuentos.com/{path}/{name}.jpg"
            
            if not deal_info["image_url"]:
                 img_el = art.select_one("img.thread-image")
                 if img_el:
                     deal_info["image_url"] = img_el.get("data-src") or img_el.get("src")
                     if deal_info["image_url"] and deal_info["image_url"].startswith("//"):
                         deal_info["image_url"] = "https:" + deal_info["image_url"]
        except Exception as e:
            logger.debug(f"Error extracting Image: {e}")
        
        # --- 7. Coupon ---
        deal_info["coupon_code"] = vue_data.get("voucherCode")
        try:
            if not deal_info["coupon_code"]:
                 coupon_el = art.select_one(".voucher .buttonWithCode-code")
                 if coupon_el: deal_info["coupon_code"] = coupon_el.get_text(strip=True)
        except Exception as e:
            logger.debug(f"Error extracting Coupon: {e}")

        # --- 8. Description ---
        try:
            # Try to get from HTML usually best for summary
            desc_el = art.select_one(".thread-description .userHtml-content, .userHtml.userHtml-content div")
            if desc_el:
                desc = desc_el.get_text(strip=True, separator=' ')
                deal_info["description"] = desc[:280].strip() + "..." if len(desc) > 280 else desc
            else:
                 deal_info["description"] = "No disponible"
        except Exception as e:
             deal_info["description"] = "No disponible"

        # --- 9. Temperature ---
        deal_info["temperature"] = 0
        try:
            if vue_data:
                deal_info["temperature"] = float(vue_data.get("temperature", 0))
            else:
                temp_el = art.select_one(".vote-temp")
                if temp_el:
                    txt = temp_el.get_text(strip=True).replace("°", "").strip()
                    deal_info["temperature"] = float(txt)
        except Exception as e:
            logger.debug(f"Error extracting Temperature: {e}")

        # --- 10. Time ---
        deal_info["hours_since_posted"] = 999.0
        deal_info["posted_or_updated"] = "Publicado"
        try:
             if vue_data and vue_data.get("publishedAt"):
                 pub_at = int(vue_data["publishedAt"])
                 if vue_data.get("threadUpdates"):
                     deal_info["posted_or_updated"] = "Actualizado"
                 
                 diff = time.time() - pub_at
                 deal_info["hours_since_posted"] = diff / 3600
             else:
                 # HTML Fallback for time
                 time_el = art.select_one("span.chip span.size--all-s")
                 if time_el:
                     posted_txt = time_el.get_text(strip=True).lower()
                     if "actualizado" in posted_txt: deal_info["posted_or_updated"] = "Actualizado"
                     
                     # Simple regex parsing
                     if "min" in posted_txt or "m" in posted_txt.split():
                         m = re.search(r"(\d+)", posted_txt)
                         if m: deal_info["hours_since_posted"] = int(m.group(1)) / 60.0
                     elif "h" in posted_txt:
                         m = re.search(r"(\d+)", posted_txt)
                         if m: deal_info["hours_since_posted"] = float(m.group(1))
        except Exception as e:
             logger.debug(f"Error extracting Time: {e}")
        
        # Posted Text (for 'Expiró' check) -> HTML always
        try:
            meta_div = art.select_one(".thread-meta")
            if meta_div:
                 deal_info["posted_text"] = meta_div.get_text(strip=True)
        except Exception as e: pass

        return deal_info
