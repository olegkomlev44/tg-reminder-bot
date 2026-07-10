import aiohttp
import re
import logging

logger = logging.getLogger(__name__)

SC_API = "https://api-v2.soundcloud.com"
SC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": "https://soundcloud.com",
    "Referer": "https://soundcloud.com/"
}

# Резервные ключи из оригинального плагина на крайний случай
SC_CLIENT_IDS = [
    "KINHWRRKbKqSKzBWVyuKxKGtKSrCDPQR",
    "1OmWW731BOastLEDE5uI7is75NwkAg98",
    "iZIs9mchVcX5lhVRyQGGAYlNPa2Abel",
    "a3e059563d7fd3372b49b37f00a00bcf",
    "2t9loNQH90kzJcsFCODdigxfp325aq4z"
]

class MusicEngine:
    def __init__(self):
        self.dynamic_cid = None
        self.current_cid_idx = 0

    async def get_valid_cid(self, session):
        """Умный добытчик ключей: сначала пытается спарсить свежий ключ с сайта."""
        if not self.dynamic_cid:
            try:
                # Лезем на сайт SoundCloud и выдираем свежий токен из JS-плеера
                async with session.get("https://soundcloud.com", headers=SC_HEADERS) as resp:
                    text = await resp.text()
                    urls = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', text)
                    if not urls:
                        urls = re.findall(r'"(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', text)
                    
                    for js_url in reversed(urls):
                        async with session.get(js_url, headers=SC_HEADERS) as js_resp:
                            js_text = await js_resp.text()
                            m = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{32})"', js_text) or re.search(r'client_id:"([a-zA-Z0-9]{32})"', js_text)
                            if m:
                                self.dynamic_cid = m.group(1)
                                logger.info(f"🟢 Спарсил свежий SC ключ: {self.dynamic_cid}")
                                return self.dynamic_cid
            except Exception as e:
                logger.error(f"🔴 Не удалось спарсить ключ SC: {e}")
        
        # Возвращаем спарсенный или берем из хардкода, если парсинг сломался
        if self.dynamic_cid:
            return self.dynamic_cid
            
        cid = SC_CLIENT_IDS[self.current_cid_idx]
        self.current_cid_idx = (self.current_cid_idx + 1) % len(SC_CLIENT_IDS)
        return cid

    def invalidate_cid(self):
        """Сбрасывает текущий ключ, заставляя бота найти новый."""
        self.dynamic_cid = None

    async def search_sc(self, query: str, limit: int = 5):
        """Ищет треки в SoundCloud с автоматической заменой битых ключей."""
        async with aiohttp.ClientSession() as session:
            url = f"{SC_API}/search/tracks"
            
            for _ in range(3): # Даем боту 3 попытки с разными ключами
                cid = await self.get_valid_cid(session)
                params = {
                    "q": query, 
                    "limit": limit, 
                    "client_id": cid, 
                    "app_version": "1735820463" # Фейковая версия прилы
                }
                
                try:
                    async with session.get(url, params=params, headers=SC_HEADERS) as resp:
                        if resp.status == 401 or resp.status == 403:
                            logger.warning(f"🟡 Ключ {cid} сдох, ищем новый...")
                            self.invalidate_cid()
                            continue
                            
                        if resp.status != 200:
                            return []
                            
                        data = await resp.json()
                        results = []
                        for t in data.get("collection", []):
                            if not t.get("streamable") or t.get("policy") == "BLOCK": continue
                            dur = t.get("duration", 0)
                            results.append({
                                "id": t.get("id"),
                                "title": t.get("title", "Unknown"),
                                "artist": t.get("user", {}).get("username", "Unknown Artist"),
                                "duration": f"{dur//60000}:{(dur%60000)//1000:02d}",
                                "service": "SC"
                            })
                        return results
                except Exception as e:
                    logger.error(f"🔴 Ошибка поиска SC: {e}")
                    self.invalidate_cid()
            
            return []

    async def get_sc_stream_url(self, track_id: str):
        """Получает прямую ссылку на mp3 файл трека."""
        async with aiohttp.ClientSession() as session:
            url = f"{SC_API}/tracks/{track_id}"
            
            for _ in range(3):
                cid = await self.get_valid_cid(session)
                params = {"client_id": cid}
                try:
                    async with session.get(url, params=params, headers=SC_HEADERS) as resp:
                        if resp.status == 401 or resp.status == 403:
                            self.invalidate_cid()
                            continue
                            
                        data = await resp.json()
                        tcs = data.get("media", {}).get("transcodings", [])
                        chosen = None
                        
                        for tc in tcs:
                            if tc.get("format", {}).get("protocol") == "progressive":
                                chosen = tc.get("url")
                                break
                        
                        if not chosen: return None
                        
                        async with session.get(chosen, params=params, headers=SC_HEADERS) as m_resp:
                            m_data = await m_resp.json()
                            return m_data.get("url")
                except Exception as e:
                    logger.error(f"🔴 Ошибка стрима SC: {e}")
                    self.invalidate_cid()
            return None

    async def download_track(self, url: str):
        """Скачивает трек в память (bytes)."""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=SC_HEADERS) as resp:
                    if resp.status == 200:
                        return await resp.read()
            except Exception as e:
                logger.error(f"🔴 Ошибка скачивания трека: {e}")
        return None

music_engine = MusicEngine()
