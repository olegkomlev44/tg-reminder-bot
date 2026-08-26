import aiohttp
import re
import logging
import struct
import time
import urllib.parse
import asyncio
import os
import tempfile
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

logger = logging.getLogger(__name__)


class _TTLCache:
    """
    Простой in-memory TTL-кэш (без внешних зависимостей типа cachetools/redis).
    Достаточно для одного процесса на bothost — снижает число запросов
    к SoundCloud API (риск бана client_id) и ускоряет отклик на частые запросы.
    """
    def __init__(self, ttl: float = 300, maxsize: int = 300):
        self.ttl = ttl
        self.maxsize = maxsize
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        ts, value = item
        if time.monotonic() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value):
        if len(self._store) >= self.maxsize:
            # Вытесняем самую старую запись (простой LRU-подобный лимит)
            oldest_key = min(self._store, key=lambda k: self._store[k][0])
            self._store.pop(oldest_key, None)
        self._store[key] = (time.monotonic(), value)

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

SOURCE_EMOJI = {
    "SoundCloud": "🔊",
    "YouTube Music": "▶️",
}


class MusicEngine:
    def __init__(self):
        self.dynamic_cid = None
        self.current_cid_idx = 0
        # Кэш поиска и чартов — снижает нагрузку на SoundCloud API
        self.search_cache = _TTLCache(ttl=300, maxsize=300)   # 5 минут
        self.charts_cache = _TTLCache(ttl=600, maxsize=20)    # 10 минут

    # ─────────────────────────────────────────────
    #  SoundCloud helpers
    # ─────────────────────────────────────────────

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
            except Exception as e:
                logger.debug(f"Не удалось получить динамический client_id SoundCloud: {e}")
        return self.dynamic_cid or SC_CLIENT_IDS[self.current_cid_idx]

    def invalidate_cid(self):
        self.dynamic_cid = None

    # ─────────────────────────────────────────────
    #  ПОИСК
    # ─────────────────────────────────────────────

    async def search_sc(self, query: str, limit: int = 5, offset: int = 0):
        """Поиск в SoundCloud."""
        async with aiohttp.ClientSession() as session:
            cid = await self.get_valid_cid(session)
            params = {
                "q": query, "limit": limit, "offset": offset,
                "client_id": cid, "app_version": "1735820463"
            }
            try:
                async with session.get(f"{SC_API}/search/tracks", params=params, headers=SC_HEADERS) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = []
                        for t in data.get("collection", []):
                            if not t.get("streamable"):
                                continue
                            dur = t.get("duration", 0)
                            user_obj = t.get("user", {})
                            artwork = t.get("artwork_url") or user_obj.get("avatar_url") or ""
                            artwork = artwork.replace("large", "t500x500") if artwork else ""
                            avatar = user_obj.get("avatar_url", "").replace("large", "t500x500")
                            results.append({
                                "id": str(t.get("id")),
                                "title": t.get("title", "Unknown"),
                                "artist": user_obj.get("username", "Unknown"),
                                "duration": f"{dur//60000}:{(dur%60000)//1000:02d}",
                                "artwork_url": artwork,
                                "artist_avatar": avatar,
                                "source": "SoundCloud"
                            })
                        return results
            except Exception as e:
                logger.error(f"SC Search error: {e}")
        return []

    async def search_yt(self, query: str, limit: int = 5) -> list:
        """Поиск через yt-dlp (YouTube Music)."""
        if not yt_dlp:
            return []
        def _search():
            ydl_opts = {
                'format': 'bestaudio/best', 'noplaylist': True,
                'quiet': True, 'extract_flat': True
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _search)
            results = []
            for entry in data.get('entries', []):
                dur = int(entry.get('duration') or 0)
                thumbs = entry.get('thumbnails', [])
                thumb = thumbs[-1].get('url', '') if thumbs else ""
                results.append({
                    "id": f"yt_{entry['id']}",
                    "title": entry.get('title', 'Unknown'),
                    "artist": entry.get('uploader', 'YouTube Music'),
                    "duration": f"{dur//60}:{dur%60:02d}",
                    "artwork_url": thumb,
                    "source": "YouTube Music"
                })
            return results
        except Exception as e:
            logger.error(f"YT search error: {e}")
        return []

    async def search_multi(self, query: str, limit: int = 5, offset: int = 0) -> list:
        """
        Мультиплатформенный поиск: SoundCloud → YouTube Music.
        Результаты кэшируются на self.search_cache.ttl секунд, чтобы не
        дёргать SoundCloud API повторно на одинаковые запросы (риск бана
        client_id + лишняя нагрузка на слабом хостинге).
        """
        cache_key = f"{query.strip().lower()}::{limit}::{offset}"
        cached = self.search_cache.get(cache_key)
        if cached is not None:
            return cached

        # 1. SoundCloud (основной)
        sc = await self.search_sc(query, limit=limit, offset=offset)
        if sc:
            self.search_cache.set(cache_key, sc)
            return sc

        # Пагинация SC провалилась — YT не поддерживает offset
        if offset > 0:
            return []

        logger.info(f"SC дал 0 результатов для '{query}', пробуем YouTube Music...")

        # 2. YouTube Music
        yt = await self.search_yt(query, limit=limit)
        if yt:
            self.search_cache.set(cache_key, yt)
        return yt

    # ─────────────────────────────────────────────
    #  ЧАРТЫ
    # ─────────────────────────────────────────────

    async def get_charts(self, limit: int = 5, offset: int = 0):
        """Чарты SC. Кэшируются на self.charts_cache.ttl секунд — чарты не
        меняются от запроса к запросу, дёргать SC на каждый /api/wave не нужно."""
        cache_key = f"{limit}::{offset}"
        cached = self.charts_cache.get(cache_key)
        if cached is not None:
            return cached

        async with aiohttp.ClientSession() as session:
            cid = await self.get_valid_cid(session)
            params = {
                "kind": "top",
                "genre": "soundcloud:genres:all-music",
                "high_tier_only": "false",
                "limit": limit,
                "offset": offset,
                "client_id": cid
            }
            try:
                async with session.get(f"{SC_API}/charts", params=params, headers=SC_HEADERS) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = []
                        for item in data.get("collection", []):
                            t = item.get("track", {})
                            if not t.get("streamable"):
                                continue
                            dur = t.get("duration", 0)
                            user_obj = t.get("user", {})
                            artwork = t.get("artwork_url") or user_obj.get("avatar_url") or ""
                            artwork = artwork.replace("large", "t500x500") if artwork else ""
                            avatar = user_obj.get("avatar_url", "").replace("large", "t500x500")
                            results.append({
                                "id": str(t.get("id")),
                                "title": t.get("title", "Unknown"),
                                "artist": user_obj.get("username", "Unknown"),
                                "duration": f"{dur//60000}:{(dur%60000)//1000:02d}",
                                "artwork_url": artwork,
                                "artist_avatar": avatar,
                                "source": "SoundCloud"
                            })
                        if results:
                            self.charts_cache.set(cache_key, results)
                        return results
            except Exception as e:
                logger.error(f"Chart SC error: {e}")
        return []

    # ─────────────────────────────────────────────
    #  ДЕТАЛИ ТРЕКА + СТРИМ
    # ─────────────────────────────────────────────

    async def get_track_details(self, track_id: str) -> dict | None:
        if track_id.startswith("yt_"):
            return await self._get_yt_details(track_id)
        return await self._get_sc_details(track_id)

    async def _get_sc_details(self, track_id: str) -> dict | None:
        async with aiohttp.ClientSession() as session:
            cid = await self.get_valid_cid(session)
            url = f"https://api-v2.soundcloud.com/tracks/{track_id}?client_id={cid}"
            try:
                async with session.get(url, headers=SC_HEADERS) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()

                    stream_url = None
                    for tr in data.get("media", {}).get("transcodings", []):
                        if tr.get("format", {}).get("protocol") == "progressive":
                            stream_url = tr.get("url")
                            break
                    if not stream_url:
                        for tr in data.get("media", {}).get("transcodings", []):
                            if tr.get("format", {}).get("mime_type") == "audio/mpeg":
                                stream_url = tr.get("url")
                                break

                    real_url = None
                    if stream_url:
                        async with session.get(f"{stream_url}?client_id={cid}", headers=SC_HEADERS) as sr:
                            if sr.status == 200:
                                real_url = (await sr.json()).get("url")

                    user = data.get("user", {})
                    artwork = (data.get("artwork_url") or "").replace("large", "t500x500")
                    avatar = (user.get("avatar_url") or "").replace("large", "t500x500")
                    return {
                        "id": str(data.get("id", "")),
                        "title": data.get("title", "Unknown"),
                        "artist": user.get("username", "Unknown"),
                        "stream_url": real_url,
                        "artwork_url": artwork,
                        "artist_avatar": avatar,
                        "genre": data.get("genre") or "Неизвестен",
                        "source": "SoundCloud"
                    }
            except Exception as e:
                logger.error(f"get_sc_details error: {e}")
        return None

    async def _get_yt_details(self, track_id: str) -> dict | None:
        if not yt_dlp:
            return None
        real_id = track_id.replace("yt_", "")
        def _get_info():
            ydl_opts = {'format': 'bestaudio/best', 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(real_id, download=False)
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, _get_info)
            return {
                "id": track_id,
                "title": info.get('title', 'Unknown'),
                "artist": info.get('uploader', 'Unknown'),
                "stream_url": track_id,  # маркер для download_file
                "artwork_url": info.get('thumbnail', ''),
                "genre": "Мультиплатформа",
                "source": "YouTube Music"
            }
        except Exception as e:
            logger.error(f"YT Track details error: {e}")
        return None
    # ─────────────────────────────────────────────
    #  LAST.FM УМНЫЕ РЕКОМЕНДАЦИИ
    # ─────────────────────────────────────────────

    async def get_similar_lastfm(self, artist: str, track: str, limit: int = 10) -> list:
        """
        Отправляет запрос в Last.fm API и возвращает список похожих треков 
        в формате [{"artist": "...", "title": "..."}, ...]
        """
        # Вставь сюда свой ключ от Last.fm
        LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "ТВОЙ_КЛЮЧ_СЮДА") 
        
        url = "http://ws.audioscrobbler.com/2.0/"
        params = {
            "method": "track.getsimilar",
            "artist": artist,
            "track": track,
            "api_key": LASTFM_API_KEY,
            "format": "json",
            "limit": limit
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, params=params, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        similars = data.get("similartracks", {}).get("track", [])
                        
                        # Парсим ответ в удобный список
                        return [
                            {"title": t["name"], "artist": t["artist"]["name"]} 
                            for t in similars
                        ]
            except Exception as e:
                logger.error(f"Ошибка Last.fm API: {e}")
        
        return []

    # ─────────────────────────────────────────────
    #  СКАЧИВАНИЕ ФАЙЛА
    # ─────────────────────────────────────────────

    async def download_file(self, url: str | None) -> bytes | None:
        if not url:
            return None

        # YouTube Music
        if url.startswith("yt_"):
            return await self._download_yt(url.replace("yt_", ""))

        # Обычный HTTP (SC / картинки)
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    return await r.read() if r.status == 200 else None
            except Exception as e:
                logger.error(f"download_file HTTP error: {e}")
        return None

    async def _download_yt(self, real_id: str) -> bytes | None:
        if not yt_dlp:
            return None
        temp_path = os.path.join(tempfile.gettempdir(), f"yt_{real_id}.m4a")
        def _dl():
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': temp_path,
                'quiet': True,
                'nocheckcertificate': True
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([real_id])
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _dl)
            if os.path.exists(temp_path):
                with open(temp_path, "rb") as f:
                    data = f.read()
                os.remove(temp_path)
                return data
        except Exception as e:
            logger.error(f"YT download error: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)
        return None

    # ─────────────────────────────────────────────
    #  LYRICS / COVER
    # ─────────────────────────────────────────────

    async def fetch_lyrics(self, artist: str, title: str) -> str | None:
        clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
        url = f"https://lrclib.net/api/search?q={urllib.parse.quote(artist + ' ' + clean_title)}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data:
                            return data[0].get("syncedLyrics") or data[0].get("plainLyrics")
            except Exception as e:
                logger.error(f"Lyrics error: {e}")
        return None

    async def fetch_itunes_cover(self, artist: str, title: str) -> str | None:
        clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
        query = urllib.parse.quote(f"{artist} {clean_title}")
        url = f"https://itunes.apple.com/search?term={query}&entity=song&limit=1"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('resultCount', 0) > 0:
                            img = data['results'][0].get('artworkUrl100', '')
                            return img.replace('100x100bb', '1000x1000bb')
            except Exception as e:
                logger.error(f"iTunes cover error: {e}")
        return None


music_engine = MusicEngine()


async def check_yt_dlp_freshness():
    """
    yt-dlp — единственная точка доступа к YouTube в этом проекте, а YouTube
    регулярно меняет защиту от скрапинга. Устаревшая версия обычно не падает
    с ошибкой — она просто тихо перестаёт находить/скачивать треки, и
    пользователи решают, что "YouTube не работает", хотя дело в версии пакета.
    Логируем предупреждение при старте, чтобы это было видно в логах bothost
    сразу, а не через жалобы пользователей.
    Не блокирует запуск: сетевой запрос обёрнут в try/except с коротким таймаутом.
    """
    if yt_dlp is None:
        logger.warning("yt-dlp не установлен — поиск/стрим с YouTube Music недоступен")
        return

    installed = getattr(yt_dlp, "__version__", None) or getattr(yt_dlp.version, "__version__", "unknown")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://pypi.org/pypi/yt-dlp/json",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Не удалось проверить актуальность yt-dlp: PyPI вернул {resp.status}")
                    return
                data = await resp.json()
                latest = data.get("info", {}).get("version")
                if not latest:
                    return
                if latest != installed:
                    logger.warning(
                        f"⚠️ yt-dlp устарел: установлена {installed}, в PyPI доступна {latest}. "
                        f"YouTube Music может искать/играть нестабильно или вообще не находить треки. "
                        f"Обновите зависимость: pip install -U yt-dlp"
                    )
                else:
                    logger.info(f"yt-dlp актуален: версия {installed}")
    except asyncio.TimeoutError:
        logger.debug("Проверка актуальности yt-dlp: таймаут запроса к PyPI")
    except Exception as e:
        # Не критично — например, на bothost может не быть доступа к pypi.org.
        # Не должно мешать запуску бота, поэтому только debug-уровень.
        logger.debug(f"Не удалось проверить актуальность yt-dlp: {e}")


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

        if title:
            _frame("TIT2", b"\x03" + title.encode("utf-8"))
        if artist:
            _frame("TPE1", b"\x03" + artist.encode("utf-8"))
        if cover_bytes:
            _frame("APIC", b"\x00" + b"image/jpeg" + b"\x00\x03\x00" + cover_bytes)

        if not frames:
            return audio_bytes
        header = b"ID3\x03\x00\x00" + _sync_safe(len(frames))
        payload = audio_bytes
        if payload[:3] == b"ID3":
            sb = payload[6:10]
            sz = (((sb[0] & 0x7F) << 21) | ((sb[1] & 0x7F) << 14) | ((sb[2] & 0x7F) << 7) | (sb[3] & 0x7F))
            payload = payload[10 + sz:]
        return header + bytes(frames) + payload
    except Exception as e:
        logger.warning(f"add_id3_tags failed, возвращаю файл без тегов: {e}")
        return audio_bytes
