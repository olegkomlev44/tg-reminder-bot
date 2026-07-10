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

        async def get_track_details(self, track_id: str) -> dict | None:
        """Получает детали трека по track_id через SoundCloud API."""
        async with aiohttp.ClientSession() as session:
            cid = await self.get_valid_cid(session)
            url = f"https://api-v2.soundcloud.com/tracks/{track_id}?client_id={cid}"
            try:
                async with session.get(url, headers=SC_HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 401:
                        self.invalidate_cid()
                        return None
                    if r.status != 200:
                        return None
                    data = await r.json()
                    
                    # Пытаемся достать stream_url
                    media = data.get("media", {})
                    transcodings = media.get("transcodings", [])
                    stream_url = None
                    for tr in transcodings:
                        fmt = tr.get("format", {})
                        if fmt.get("mime_type") == "audio/mpeg":
                            stream_url = tr.get("url")
                            break
                    if not stream_url and transcodings:
                        stream_url = transcodings[0].get("url")
                    
                    real_url = None
                    if stream_url:
                        try:
                            async with session.get(
                                f"{stream_url}?client_id={cid}",
                                headers=SC_HEADERS,
                                timeout=aiohttp.ClientTimeout(total=10)
                            ) as sr:
                                if sr.status == 200:
                                    sdata = await sr.json()
                                    real_url = sdata.get("url")
                        except Exception:
                            pass
                            
                    user = data.get("user", {})
                    artwork = (data.get("artwork_url") or "").replace("large", "t500x500")
                    
                    # ВОТ ЭТА ЧАСТЬ САМАЯ ВАЖНАЯ:
                    return {
                        "id":          str(data.get("id", "")),
                        "title":       data.get("title", "Unknown"),
                        "artist":      user.get("username", "Unknown"),
                        "duration":    data.get("duration", 0) // 1000,
                        "stream_url":  real_url,
                        "artwork_url": artwork,
                        "permalink":   data.get("permalink_url", ""),
                        "genre":       data.get("genre") or "Неизвестен" # Добавили жанр!
                    }
            except Exception as e:
                logger.error(f"get_track_details error: {e}")
                return None


    async def download_file(self, url: str | None) -> bytes | None:
        """Скачивает файл по URL и возвращает байты. None при ошибке."""
        if not url:
            return None
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=SC_HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        return await r.read()
                    return None
            except Exception as e:
                logger.error(f"download_file error: {e}")
                return None


music_engine = MusicEngine()

def add_id3_tags(
    audio_bytes: bytes,
    title: str = "",
    artist: str = "",
    cover_bytes: bytes | None = None,
) -> bytes:
    """
    Вшивает ID3v2.3-теги (TIT2, TPE1, APIC) в байты MP3 без внешних зависимостей.
    При любой ошибке возвращает оригинальные байты.
    """
    import struct

    def _sync_safe(n: int) -> bytes:
        r = bytearray(4)
        for i in range(3, -1, -1):
            r[i] = n & 0x7F
            n >>= 7
        return bytes(r)

    try:
        frames = bytearray()

        def _frame(fid: str, data: bytes):
            frames.extend(fid.encode("ascii"))
            frames.extend(struct.pack(">I", len(data)))
            frames.extend(b"\x00\x00")
            frames.extend(data)

        if title:
            _frame("TIT2", b"\x03" + title.encode("utf-8"))
        if artist:
            _frame("TPE1", b"\x03" + artist.encode("utf-8"))
        if cover_bytes:
            apic = b"\x00" + b"image/jpeg" + b"\x00\x03\x00" + cover_bytes
            _frame("APIC", apic)

        if not frames:
            return audio_bytes

        header = b"ID3\x03\x00\x00" + _sync_safe(len(frames))
        payload = audio_bytes
        if payload[:3] == b"ID3":
            sb = payload[6:10]
            sz = (((sb[0]&0x7F)<<21)|((sb[1]&0x7F)<<14)|((sb[2]&0x7F)<<7)|(sb[3]&0x7F))
            payload = payload[10 + sz:]
        return header + bytes(frames) + payload
    except Exception:
        return audio_bytes

