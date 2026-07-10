import aiohttp
import re
import logging
import struct

logger = logging.getLogger(__name__)

SC_API = "https://api-v2.soundcloud.com"
SC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": "https://soundcloud.com",
    "Referer": "https://soundcloud.com/"
}

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
        if not self.dynamic_cid:
            try:
                async with session.get("https://soundcloud.com", headers=SC_HEADERS) as resp:
                    text = await resp.text()
                    urls = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', text)
                    for js_url in reversed(urls):
                        async with session.get(js_url, headers=SC_HEADERS) as js_resp:
                            js_text = await js_resp.text()
                            m = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{32})"', js_text)
                            if m:
                                self.dynamic_cid = m.group(1)
                                return self.dynamic_cid
            except: pass
        return self.dynamic_cid or SC_CLIENT_IDS[self.current_cid_idx]

    def invalidate_cid(self):
        self.dynamic_cid = None

    async def search_sc(self, query: str, limit: int = 5, offset: int = 0):
        async with aiohttp.ClientSession() as session:
            cid = await self.get_valid_cid(session)
            # Добавили offset для пагинации
            params = {"q": query, "limit": limit, "offset": offset, "client_id": cid, "app_version": "1735820463"}
            try:
                async with session.get(f"{SC_API}/search/tracks", params=params, headers=SC_HEADERS) as resp:
                    if resp.status != 200: return []
                    data = await resp.json()
                    results = []
                    for t in data.get("collection", []):
                        if not t.get("streamable"): continue
                        dur = t.get("duration", 0)
                        results.append({
                            "id": t.get("id"),
                            "title": t.get("title", "Unknown"),
                            "artist": t.get("user", {}).get("username", "Unknown"),
                            "duration": f"{dur//60000}:{(dur%60000)//1000:02d}",
                        })
                    return results
            except: return []

    async def get_track_details(self, track_id: str) -> dict | None:
        """Получает детали трека по track_id через SoundCloud API."""
        async with aiohttp.ClientSession() as session:
            cid = await self.get_valid_cid(session)
            url = f"https://api-v2.soundcloud.com/tracks/{track_id}?client_id={cid}"
            try:
                async with session.get(url, headers=SC_HEADERS) as r:
                    if r.status != 200: return None
                    data = await r.json()
                    
                    stream_url = None
                    for tr in data.get("media", {}).get("transcodings", []):
                        if tr.get("format", {}).get("mime_type") == "audio/mpeg":
                            stream_url = tr.get("url")
                            break
                    
                    real_url = None
                    if stream_url:
                        async with session.get(f"{stream_url}?client_id={cid}", headers=SC_HEADERS) as sr:
                            if sr.status == 200: real_url = (await sr.json()).get("url")
                            
                    user = data.get("user", {})
                    artwork = (data.get("artwork_url") or "").replace("large", "t500x500")
                    
                    return {
                        "id":          str(data.get("id", "")),
                        "title":       data.get("title", "Unknown"),
                        "artist":      user.get("username", "Unknown"),
                        "stream_url":  real_url,
                        "artwork_url": artwork,
                        "genre":       data.get("genre") or "Неизвестен"
                    }
            except Exception as e:
                logger.error(f"get_track_details error: {e}")
                return None

    async def download_file(self, url: str | None) -> bytes | None:
        if not url: return None
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=SC_HEADERS) as r:
                    return await r.read() if r.status == 200 else None
            except: return None

music_engine = MusicEngine()

def add_id3_tags(audio_bytes: bytes, title: str = "", artist: str = "", cover_bytes: bytes | None = None) -> bytes:
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

        if title: _frame("TIT2", b"\x03" + title.encode("utf-8"))
        if artist: _frame("TPE1", b"\x03" + artist.encode("utf-8"))
        if cover_bytes: _frame("APIC", b"\x00" + b"image/jpeg" + b"\x00\x03\x00" + cover_bytes)

        if not frames: return audio_bytes
        header = b"ID3\x03\x00\x00" + _sync_safe(len(frames))
        payload = audio_bytes
        if payload[:3] == b"ID3":
            sb = payload[6:10]
            sz = (((sb[0]&0x7F)<<21)|((sb[1]&0x7F)<<14)|((sb[2]&0x7F)<<7)|(sb[3]&0x7F))
            payload = payload[10 + sz:]
        return header + bytes(frames) + payload
    except: return audio_bytes
