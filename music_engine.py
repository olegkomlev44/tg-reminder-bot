import aiohttp
import re
import urllib.parse
import logging

logger = logging.getLogger(__name__)

SC_API = "https://api-v2.soundcloud.com"
SC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Origin": "https://soundcloud.com",
    "Referer": "https://soundcloud.com/"
}
SC_CLIENT_IDS = [
    "iZIs9mchVcX5lhVRyQGGAYlNPa2Abel",
    "a3e059563d7fd3372b49b37f00a00bcf",
    "2t9loNQH90kzJcsFCODdigxfp325aq4z"
]

class MusicEngine:
    def __init__(self):
        self.sc_cid = SC_CLIENT_IDS[0]

    async def search_sc(self, query: str, limit: int = 5):
        """Ищет треки в SoundCloud и возвращает список словарей."""
        async with aiohttp.ClientSession() as session:
            url = f"{SC_API}/search/tracks"
            params = {"q": query, "limit": limit, "client_id": self.sc_cid}
            try:
                async with session.get(url, params=params, headers=SC_HEADERS) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    
                    results = []
                    for t in data.get("collection", []):
                        if not t.get("streamable"): continue
                        dur = t.get("duration", 0)
                        tid = t.get("id")
                        results.append({
                            "id": tid,
                            "title": t.get("title", "Unknown"),
                            "artist": t.get("user", {}).get("username", "Unknown Artist"),
                            "duration": f"{dur//60000}:{(dur%60000)//1000:02d}",
                            "service": "SC"
                        })
                    return results
            except Exception as e:
                logger.error(f"Ошибка поиска SC: {e}")
                return []

    async def get_sc_stream_url(self, track_id: str):
        """Получает прямую ссылку на mp3 файл трека."""
        async with aiohttp.ClientSession() as session:
            url = f"{SC_API}/tracks/{track_id}"
            params = {"client_id": self.sc_cid}
            try:
                async with session.get(url, params=params, headers=SC_HEADERS) as resp:
                    data = await resp.json()
                    tcs = data.get("media", {}).get("transcodings", [])
                    chosen = None
                    for tc in tcs:
                        if tc.get("format", {}).get("protocol") == "progressive":
                            chosen = tc.get("url")
                            break
                    
                    if not chosen: return None
                    
                    # Получаем сам mp3 url
                    async with session.get(chosen, params=params, headers=SC_HEADERS) as m_resp:
                        m_data = await m_resp.json()
                        return m_data.get("url")
            except Exception as e:
                logger.error(f"Ошибка получения стрима SC: {e}")
                return None

    async def download_track(self, url: str):
        """Скачивает трек в память (bytes), чтобы не засорять диск."""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=SC_HEADERS) as resp:
                    if resp.status == 200:
                        return await resp.read()
            except Exception as e:
                logger.error(f"Ошибка скачивания: {e}")
            return None

music_engine = MusicEngine()

