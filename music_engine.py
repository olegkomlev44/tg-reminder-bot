import aiohttp
import re
import logging
import struct
import sys
import time
import urllib.parse
import asyncio
import importlib
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


    def pop(self, key: str):
        self._store.pop(key, None)


    def set(self, key: str, value):
        if len(self._store) >= self.maxsize:
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




def _ytdlp_version() -> str:
    if yt_dlp is None:
        return "not installed"
    return getattr(yt_dlp, "__version__", None) or getattr(getattr(yt_dlp, "version", None), "__version__", "unknown")




def _norm_words(text: str) -> set:
    """Слова названия без шума вроде (Official Video) — для сопоставления треков."""
    t = re.sub(r"[\(\[].*?[\)\]]", " ", text or "")
    words = re.findall(r"\w+", t.lower())
    noise = {"official", "video", "audio", "lyrics", "lyric", "hd", "hq", "clip", "клип", "topic", "feat", "ft", "prod"}
    return {w for w in words if w not in noise and len(w) > 1}




def _sec_to_dur(seconds) -> str:
    try:
        s = int(seconds)
        if s <= 0:
            return ""
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except (TypeError, ValueError):
        return ""




def _dur_to_sec(d) -> int:
    try:
        parts = [int(x) for x in str(d).split(":")]
        sec = 0
        for x in parts:
            sec = sec * 60 + x
        return sec
    except Exception:
        return 0




class MusicEngine:
    def __init__(self):
        self.dynamic_cid = None
        self.current_cid_idx = 0
        self._cid_ts = 0.0                    # когда получили текущий client_id
        self._cid_source = None               # откуда он: env / scrape / yt-dlp / fallback
        self._cid_lock = asyncio.Lock()
        self._bad_cids: dict = {}             # client_id → когда отвергнут (401/403); забываем через 30 мин
        self._cid_fail_until = 0.0            # пауза между попытками, если рабочий client_id не нашли
        self.search_cache = _TTLCache(ttl=300, maxsize=300)   # 5 минут
        self.charts_cache = _TTLCache(ttl=600, maxsize=20)    # 10 минут
        self.yt_status = {"available": True, "consecutive_failures": 0, "disabled_until": 0.0, "last_error": None}
        self.yt_details_cache = _TTLCache(ttl=1500, maxsize=200)
        self.sc_details_cache = _TTLCache(ttl=480, maxsize=300)
        self._details_inflight: dict = {}
        self.sc_status = {"ok": True, "last_error": None, "last_ok_ts": None, "last_fail_ts": None}
        self.ytdlp_state = {
            "version": _ytdlp_version(), "latest": None,
            "last_check_ts": None, "last_update_ts": None, "last_attempt_ts": 0.0,
            "last_error": None, "updating": False,
        }


    # ─────────────────────────────────────────────
    #  SoundCloud helpers
    # ─────────────────────────────────────────────


    SC_CID_TTL = 6 * 3600                       # раз в 6 часов обновляем client_id заранее

    _CID_PATTERNS = (
        r'client_id\s*:\s*"([a-zA-Z0-9]{32})"',
        r'client_id\s*[:=]\s*\\?["\']([a-zA-Z0-9]{32})',
        r'[?&]client_id=([a-zA-Z0-9]{32})',
    )

    async def _scrape_cid(self) -> str | None:
        """Достаёт client_id из JS-бандлов soundcloud.com."""
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("https://soundcloud.com", headers=SC_HEADERS) as resp:
                    text = await resp.text()
                urls = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', text)
                if not urls:
                    urls = re.findall(r'<script[^>]+src="([^"]*sndcdn[^"]+\.js)"', text)
                for js_url in list(reversed(urls))[:15]:
                    try:
                        async with session.get(js_url, headers=SC_HEADERS) as js_resp:
                            js_text = await js_resp.text()
                    except Exception:
                        continue
                    for pat in self._CID_PATTERNS:
                        m = re.search(pat, js_text)
                        if m:
                            return m.group(1)
        except Exception as e:
            logger.warning(f"SC: не удалось скачать client_id со страницы: {type(e).__name__}: {e}")
        return None

    async def _cid_from_ytdlp(self) -> str | None:
        """Запасной путь: yt-dlp сам умеет добывать client_id (и его правят при каждой поломке SC)."""
        if yt_dlp is None:
            return None

        def _run():
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                                   "socket_timeout": 15}) as ydl:
                ie = ydl.get_info_extractor("Soundcloud")
                upd = getattr(ie, "_update_client_id", None)
                if upd:
                    upd()
                return getattr(ie, "_CLIENT_ID", None)

        try:
            cid = await asyncio.wait_for(asyncio.get_event_loop().run_in_executor(None, _run), timeout=40)
            return cid if cid and re.fullmatch(r"[a-zA-Z0-9]{32}", str(cid)) else None
        except Exception as e:
            logger.info(f"SC: client_id через yt-dlp не вышел: {type(e).__name__}: {str(e)[:120]}")
            return None

    async def _cid_works(self, cid: str) -> bool | None:
        """True — SC принимает, False — отверг (401/403), None — не удалось проверить (сеть/лимиты)."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.get(f"{SC_API}/search/tracks",
                                       params={"q": "a", "limit": 1, "client_id": cid},
                                       headers=SC_HEADERS) as r:
                    if r.status == 200:
                        return True
                    if r.status in (401, 403):
                        return False
                    return None
        except Exception:
            return None

    def _is_bad(self, cid: str) -> bool:
        ts = self._bad_cids.get(cid)
        if ts is None:
            return False
        if time.time() - ts > 1800:          # 403 мог быть временной блокировкой IP — даём второй шанс
            self._bad_cids.pop(cid, None)
            return False
        return True

    async def _resolve_cid(self) -> tuple[str | None, str]:
        """Перебирает источники client_id и берёт первый реально рабочий."""
        candidates: list[tuple[str, str]] = []
        env_cid = os.getenv("SC_CLIENT_ID", "").strip()
        if env_cid:
            candidates.append((env_cid, "env"))
        scraped = await self._scrape_cid()
        if scraped:
            candidates.append((scraped, "scrape"))
        yd = await self._cid_from_ytdlp()
        if yd:
            candidates.append((yd, "yt-dlp"))
        candidates += [(c, "fallback") for c in SC_CLIENT_IDS]

        unsure = None
        seen = set()
        for cid, src in candidates:
            if cid in seen or self._is_bad(cid):
                continue
            seen.add(cid)
            ok = await self._cid_works(cid)
            if ok:
                return cid, src
            if ok is None and unsure is None and src != "fallback":
                unsure = (cid, src)          # проверить не смогли, но он свежий — запасной вариант
            elif ok is False:
                self._bad_cids[cid] = time.time()
        return unsure if unsure else (None, "none")

    async def get_valid_cid(self, session=None, force: bool = False):
        fresh = self.dynamic_cid and (time.time() - self._cid_ts) < self.SC_CID_TTL
        if fresh and not force:
            return self.dynamic_cid
        if not self.dynamic_cid and time.time() < self._cid_fail_until:
            return SC_CLIENT_IDS[self.current_cid_idx % len(SC_CLIENT_IDS)]   # недавно не нашли — не долбим SC
        async with self._cid_lock:
            if self.dynamic_cid and (time.time() - self._cid_ts) < self.SC_CID_TTL and not force:
                return self.dynamic_cid                 # пока ждали лок, другой запрос уже обновил
            cid, src = await self._resolve_cid()
            if cid:
                if cid != self.dynamic_cid:
                    logger.info(f"SC: client_id обновлён (источник: {src})")
                self.dynamic_cid, self._cid_ts, self._cid_source = cid, time.time(), src
            else:
                logger.error("SC: не нашли ни одного рабочего client_id")
                self._cid_fail_until = time.time() + 60            # повторная попытка через минуту
        return self.dynamic_cid or SC_CLIENT_IDS[self.current_cid_idx % len(SC_CLIENT_IDS)]

    def invalidate_cid(self, bad: str | None = None):
        """SC отверг client_id — запоминаем его как плохой и сбрасываем кеш."""
        bad = bad or self.dynamic_cid
        if bad:
            self._bad_cids[bad] = time.time()
        self.dynamic_cid = None
        self._cid_ts = 0.0

    async def _sc_get(self, url: str, params: dict | None = None, session=None):
        """
        GET к SoundCloud с client_id. На 401/403 меняет client_id и повторяет один раз.
        Возвращает (status, json|None).
        """
        own = session is None
        if own:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        try:
            status, data = 0, None
            prev_cid = None
            for attempt in (0, 1):
                cid = await self.get_valid_cid(force=(attempt == 1))
                if cid == prev_cid:
                    break                    # новый client_id достать не удалось — повторять бессмысленно
                prev_cid = cid
                q = dict(params or {})
                q["client_id"] = cid
                try:
                    async with session.get(url, params=q, headers=SC_HEADERS) as r:
                        status = r.status
                        if status in (401, 403) and attempt == 0:
                            logger.warning(f"SC: HTTP {status} с client_id={cid[:6]}…, пробуем обновить")
                            self.invalidate_cid(cid)
                            continue
                        if status == 200:
                            data = await r.json(content_type=None)
                        break
                except Exception as e:
                    logger.error(f"SC request error: {type(e).__name__}: {e}")
                    self._record_sc(False, f"{type(e).__name__}: {e}")
                    return 0, None
            self._record_sc(status == 200, f"HTTP {status}")
            return status, data
        finally:
            if own:
                await session.close()


    # ─────────────────────────────────────────────
    #  ПОИСК
    # ─────────────────────────────────────────────


    async def search_sc(self, query: str, limit: int = 5, offset: int = 0):
        """Поиск в SoundCloud."""
        status, data = await self._sc_get(
            f"{SC_API}/search/tracks",
            {"q": query, "limit": limit, "offset": offset, "app_version": "1735820463"})
        if status != 200 or not data:
            return []
        results = []
        for t in data.get("collection", []):
            if not t.get("streamable"):
                continue
            dur = t.get("duration", 0)
            user_obj = t.get("user", {})
            artwork = t.get("artwork_url") or user_obj.get("avatar_url") or ""
            artwork = artwork.replace("large", "t500x500") if artwork else ""
            avatar = (user_obj.get("avatar_url") or "").replace("large", "t500x500")
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


    def _record_sc(self, ok: bool, error: str = ""):
        st = self.sc_status
        if ok:
            st["ok"] = True; st["last_error"] = None; st["last_ok_ts"] = time.time()
        else:
            st["ok"] = False; st["last_error"] = error; st["last_fail_ts"] = time.time()


    YT_FAILURE_THRESHOLD = 3
    YT_COOLDOWN_SEC = 300


    def get_yt_status(self) -> dict:
        now = time.monotonic()
        st = self.yt_status
        if not st["available"] and now >= st["disabled_until"]:
            return {**st, "available": True, "half_open": True}
        return {**st, "half_open": False}


    def _record_yt_success(self):
        self.yt_status["available"] = True
        self.yt_status["consecutive_failures"] = 0
        self.yt_status["disabled_until"] = 0.0
        self.yt_status["last_error"] = None


    def _record_yt_failure(self, error: str):
        st = self.yt_status
        st["consecutive_failures"] += 1
        st["last_error"] = error
        if st["consecutive_failures"] >= self.YT_FAILURE_THRESHOLD:
            if st["available"]:
                logger.warning(
                    f"⚠️ YouTube Music помечен как временно недоступный после "
                    f"{st['consecutive_failures']} неудачных попыток подряд "
                    f"(последняя ошибка: {error}). Проверьте версию yt-dlp."
                )
            st["available"] = False
            st["disabled_until"] = time.monotonic() + self.YT_COOLDOWN_SEC
            try:
                asyncio.get_event_loop().create_task(self.update_yt_dlp(reason="circuit"))
            except RuntimeError:
                pass


    async def search_yt(self, query: str, limit: int = 5) -> list:
        """Поиск через yt-dlp (YouTube Music)."""
        if not yt_dlp:
            return []


        status = self.get_yt_status()
        if not status["available"]:
            logger.debug("search_yt: источник в cooldown, пропускаем запрос")
            return []


        def _search():
            ydl_opts = {
                'format': 'bestaudio/best', 'noplaylist': True,
                'quiet': True, 'extract_flat': True
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(f"ytsearch{max(1, min(int(limit), 50))}:{query}", download=False)
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _search)
            results = []
            for entry in data.get('entries', []):
                if not entry or not entry.get('id'):
                    continue
                dur = int(entry.get('duration') or 0)
                thumbs = entry.get('thumbnails', [])
                thumb = thumbs[-1].get('url', '') if thumbs else (entry.get('thumbnail') or '')
                results.append({
                    "id": f"yt_{entry['id']}",
                    "title": entry.get('title', 'Unknown'),
                    "artist": entry.get('uploader') or entry.get('channel') or 'YouTube Music',
                    "duration": f"{dur//60}:{dur%60:02d}" if dur else "",
                    "artwork_url": thumb,
                    "source": "YouTube Music"
                })
            self._record_yt_success()
            return results
        except Exception as e:
            logger.error(f"YT search error: {e}")
            self._record_yt_failure(str(e))
        return []


    async def suggest(self, query: str, limit: int = 6) -> list:
        q = query.strip()
        if len(q) < 2:
            return []
        key = f"sg::{q.lower()}::{limit}"
        hit = self.search_cache.get(key)
        if hit is not None:
            return hit
        res = await self.search_sc(q, limit=limit)
        if res:
            self.search_cache.set(key, res)
        return res


    async def search_multi(self, query: str, limit: int = 5, offset: int = 0, source: str = "all") -> list:
        """
        Мультиплатформенный поиск с поддержкой фильтра источника:
        - source="sc": только SoundCloud
        - source="yt": только YouTube Music
        - source="all":
            * при пользовательском поиске (offset == 0 и limit >= 10) опрашивает
              SoundCloud и YouTube Music параллельно и аккуратно чередует выдачу;
            * при быстрых фоновых резолвах (limit < 10) сначала ищет в SC, а при
              пустом ответе — в YT (чтобы не нагружать CPU на каждую рекомендацию).
        """
        src_mode = (source or "all").lower().strip()
        if src_mode not in ("all", "sc", "yt"):
            src_mode = "all"


        cache_key = f"{src_mode}::{query.strip().lower()}::{limit}::{offset}"
        cached = self.search_cache.get(cache_key)
        if cached is not None:
            return cached


        # 1. Явно выбран только SoundCloud
        if src_mode == "sc":
            sc = await self.search_sc(query, limit=limit, offset=offset)
            if sc:
                self.search_cache.set(cache_key, sc)
            return sc


        # 2. Явно выбран только YouTube Music
        if src_mode == "yt":
            if offset > 0:
                return []
            yt = await self.search_yt(query, limit=min(limit, 25))
            if yt:
                self.search_cache.set(cache_key, yt)
            return yt


        # 3. Режим "all": смешанная выдача для первой страницы поиска в UI
        if offset == 0 and limit >= 10 and self.get_yt_status()["available"]:
            yt_limit = min(max(4, limit // 4), 10)
            sc_task = asyncio.create_task(self.search_sc(query, limit=limit, offset=0))
            yt_task = asyncio.create_task(self.search_yt(query, limit=yt_limit))
            sc_res, yt_res = await asyncio.gather(sc_task, yt_task, return_exceptions=True)
            sc_list = sc_res if isinstance(sc_res, list) else []
            yt_list = yt_res if isinstance(yt_res, list) else []


            if sc_list and yt_list:
                merged = []
                yi = 0
                for idx, item in enumerate(sc_list):
                    merged.append(item)
                    # Вставляем по одному YouTube-треку после каждого 2-го трека SoundCloud
                    if (idx + 1) % 2 == 0 and yi < len(yt_list):
                        merged.append(yt_list[yi])
                        yi += 1
                while yi < len(yt_list) and len(merged) < limit:
                    merged.append(yt_list[yi])
                    yi += 1
                merged = merged[:limit]
                self.search_cache.set(cache_key, merged)
                return merged
            elif sc_list:
                self.search_cache.set(cache_key, sc_list)
                return sc_list
            elif yt_list:
                self.search_cache.set(cache_key, yt_list)
                return yt_list
            return []


        # Быстрый режим для внутренних резолвов (limit < 10) или пагинации (offset > 0)
        sc = await self.search_sc(query, limit=limit, offset=offset)
        if sc:
            self.search_cache.set(cache_key, sc)
            return sc


        if offset > 0:
            return []


        logger.info(f"SC дал 0 результатов для '{query}', пробуем YouTube Music...")
        yt = await self.search_yt(query, limit=limit)
        if yt:
            self.search_cache.set(cache_key, yt)
        return yt


    # ─────────────────────────────────────────────
    #  ЧАРТЫ
    # ─────────────────────────────────────────────


    async def get_charts(self, limit: int = 5, offset: int = 0):
        cache_key = f"{limit}::{offset}"
        cached = self.charts_cache.get(cache_key)
        if cached is not None:
            return cached


        status, data = await self._sc_get(f"{SC_API}/charts", {
            "kind": "top",
            "genre": "soundcloud:genres:all-music",
            "high_tier_only": "false",
            "limit": limit,
            "offset": offset,
        })
        if status != 200 or not data:
            return []
        results = []
        for item in data.get("collection", []):
            t = item.get("track", {})
            if not t.get("streamable"):
                continue
            dur = t.get("duration", 0)
            user_obj = t.get("user", {})
            artwork = t.get("artwork_url") or user_obj.get("avatar_url") or ""
            artwork = artwork.replace("large", "t500x500") if artwork else ""
            avatar = (user_obj.get("avatar_url") or "").replace("large", "t500x500")
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


    # ─────────────────────────────────────────────
    #  ДЕТАЛИ ТРЕКА + СТРИМ
    # ─────────────────────────────────────────────


    async def get_track_details(self, track_id: str, fresh: bool = False) -> dict | None:
        tid = str(track_id)
        is_yt = tid.startswith("yt_")
        cache = self.yt_details_cache if is_yt else self.sc_details_cache
        if fresh:
            cache.pop(tid)
        else:
            hit = cache.get(tid)
            if hit is not None:
                return hit


        waiting = self._details_inflight.get(tid)
        if waiting is not None and not fresh:
            return await asyncio.shield(waiting)


        fut = asyncio.get_event_loop().create_future()
        self._details_inflight[tid] = fut
        result = None
        try:
            result = await (self._get_yt_details(tid) if is_yt else self._get_sc_details(tid))
            playable = bool(result and (result.get("direct_stream_url") if is_yt else result.get("stream_url")))
            if playable:
                cache.set(tid, result)
            return result
        finally:
            if not fut.done():
                fut.set_result(result)
            if self._details_inflight.get(tid) is fut:
                self._details_inflight.pop(tid, None)


    def invalidate_details(self, track_id: str):
        tid = str(track_id)
        (self.yt_details_cache if tid.startswith("yt_") else self.sc_details_cache).pop(tid)


    async def _get_sc_details(self, track_id: str) -> dict | None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            status, data = await self._sc_get(f"{SC_API}/tracks/{track_id}", session=session)
            if status != 200 or not data:
                return None
            try:
                trans = data.get("media", {}).get("transcodings", [])
                stream_url = None
                for tr in trans:
                    if tr.get("format", {}).get("protocol") == "progressive":
                        stream_url = tr.get("url")
                        break
                if not stream_url:
                    for tr in trans:
                        if tr.get("format", {}).get("mime_type") == "audio/mpeg":
                            stream_url = tr.get("url")
                            break

                real_url = None
                if stream_url:
                    extra = {}
                    if data.get("track_authorization"):
                        extra["track_authorization"] = data["track_authorization"]
                    st2, js2 = await self._sc_get(stream_url, extra, session=session)
                    if st2 == 200 and js2:
                        real_url = js2.get("url")

                user = data.get("user", {})
                dur = data.get("duration", 0)
                artwork = (data.get("artwork_url") or user.get("avatar_url") or "").replace("large", "t500x500")
                avatar = (user.get("avatar_url") or "").replace("large", "t500x500")
                return {
                    "id": str(data.get("id", "")),
                    "title": data.get("title", "Unknown"),
                    "artist": user.get("username", "Unknown"),
                    "duration": f"{dur//60000}:{(dur%60000)//1000:02d}" if dur else "",
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
            dur = int(info.get("duration") or 0)
            return {
                "id": track_id,
                "title": info.get('title', 'Unknown'),
                "artist": info.get('uploader', 'Unknown'),
                "duration": f"{dur//60}:{dur%60:02d}" if dur else "",
                "stream_url": track_id,
                "direct_stream_url": info.get('url'),
                "stream_headers": info.get('http_headers') or {},
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
        if url.startswith("yt_"):
            return await self._download_yt(url.replace("yt_", ""))
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
    #  ИМПОРТ ПЛЕЙЛИСТА ПО ССЫЛКЕ
    # ─────────────────────────────────────────────


    PLAYLIST_URL_RE = re.compile(
        r"^https?://(?:[a-z0-9-]+\.)*"
        r"(youtube\.com|youtu\.be|soundcloud\.com)/",
        re.IGNORECASE,
    )


    async def extract_playlist(self, url: str, limit: int = 60) -> dict:
        url = (url or "").strip()
        if not url or not self.PLAYLIST_URL_RE.match(url):
            return {"error": "unsupported_url"}
        if not yt_dlp:
            return {"error": "yt_dlp_missing"}


        def _extract():
            ydl_opts = {
                "quiet": True, "extract_flat": "in_playlist",
                "playlistend": limit, "skip_download": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)


        try:
            loop = asyncio.get_event_loop()
            info = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=45)
        except asyncio.TimeoutError:
            return {"error": "timeout"}
        except Exception as e:
            logger.warning(f"extract_playlist('{url}'): {e}")
            return {"error": "extract_failed"}


        entries = info.get("entries") or ([info] if info.get("id") else [])
        if not entries:
            return {"error": "empty"}


        is_sc = "soundcloud.com" in url.lower()
        tracks = []
        seen = set()
        for e in entries[:limit]:
            if not e or not e.get("id"):
                continue
            tid = str(e["id"]) if is_sc else f"yt_{e['id']}"
            if tid in seen:
                continue
            seen.add(tid)
            thumb = e.get("thumbnail")
            if not thumb and e.get("thumbnails"):
                thumb = e["thumbnails"][-1].get("url")
            tracks.append({
                "id": tid,
                "title": e.get("title") or "Без названия",
                "artist": e.get("uploader") or e.get("channel") or (info.get("uploader") if not is_sc else "") or "",
                "artwork_url": thumb or "",
                "duration": _sec_to_dur(e.get("duration")),
                "source": "SoundCloud" if is_sc else "YouTube Music",
            })


        return {
            "title": info.get("title") or "Импортированный плейлист",
            "tracks": tracks,
            "truncated": len(entries) > limit,
        }


    # ─────────────────────────────────────────────
    #  ЗАПАСНОЙ ИСТОЧНИК (fallback при сбое трека)
    # ─────────────────────────────────────────────


    _BAD_VERSION_WORDS = {"live", "cover", "karaoke", "караоке", "instrumental", "remix", "slowed",
                          "reverb", "nightcore", "sped", "8d", "acoustic"}


    async def find_alternative(self, title: str, artist: str, prefer: str = "yt",
                               exclude_id: str = "", duration_sec: int = 0) -> dict | None:
        want = _norm_words(title)
        if not want:
            return None
        artist_words = _norm_words(artist)
        clean_title = re.sub(r"[\(\[].*?[\)\]]", " ", title or "").strip()
        query = f"{artist or ''} {clean_title}".strip()


        if prefer == "yt":
            if not self.get_yt_status()["available"]:
                return None
            cands = await self.search_yt(query, limit=8)
        else:
            cands = await self.search_sc(query, limit=10)


        best, best_score = None, 0.0
        for c in cands or []:
            if str(c.get("id")) == str(exclude_id):
                continue
            cw = _norm_words(c.get("title", ""))
            pool = cw | _norm_words(c.get("artist", ""))
            cov = len(want & pool) / len(want)
            extra = len(cw - want - artist_words) / max(1, len(cw))
            score = cov * (1 - 0.5 * extra)
            if (cw & self._BAD_VERSION_WORDS) - want:
                score *= 0.6
            cd = _dur_to_sec(c.get("duration"))
            if duration_sec and cd:
                diff = abs(cd - duration_sec)
                if diff > 30:
                    score *= 0.4
                elif diff > 12:
                    score *= 0.8
            if score > best_score:
                best, best_score = c, score
        if best and best_score >= 0.5:
            return {**best, "match_score": round(best_score, 2)}
        return None


    # ─────────────────────────────────────────────
    #  АВТООБНОВЛЕНИЕ yt-dlp
    # ─────────────────────────────────────────────


    async def _pypi_latest_ytdlp(self) -> str | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://pypi.org/pypi/yt-dlp/json",
                                       timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        return (await resp.json()).get("info", {}).get("version")
        except Exception as e:
            logger.debug(f"PyPI недоступен: {e}")
        return None


    async def _pip_upgrade_ytdlp(self) -> tuple[bool, str]:
        last = ""
        for extra in ([], ["--user"]):
            cmd = [sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir",
                   "--disable-pip-version-check", "--quiet", *extra, "yt-dlp"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(proc.communicate(), timeout=240)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return False, "pip: таймаут"
            except Exception as e:
                return False, str(e)
            if proc.returncode == 0:
                return True, ""
            last = (err or out or b"").decode(errors="ignore")[-300:]
        return False, last or "pip завершился с ошибкой"


    def _reload_yt_dlp(self) -> bool:
        global yt_dlp
        old_mods = {n: m for n, m in sys.modules.items() if n == "yt_dlp" or n.startswith("yt_dlp.")}
        for n in old_mods:
            sys.modules.pop(n, None)
        importlib.invalidate_caches()
        # Снимаем старую обёртку urllib3._PERCENT_RE: иначе повторный импорт
        # yt-dlp навесит вторую поверх первой, и requests начнёт падать
        # с "'Urllib3PercentREOverride' object has no attribute 'sub'".
        try:
            import urllib3.util.url as _u
            _r = _u._PERCENT_RE
            while type(_r).__name__ == "Urllib3PercentREOverride":
                _r = _r.re
            _u._PERCENT_RE = _r
        except Exception:
            pass
        try:
            yt_dlp = importlib.import_module("yt_dlp")
            return True
        except Exception as e:
            sys.modules.update(old_mods)
            logger.error(f"Не удалось подгрузить обновлённый yt-dlp: {e}")
            return False


    async def update_yt_dlp(self, reason: str = "scheduled", force: bool = False) -> dict:
        st = self.ytdlp_state
        if yt_dlp is None:
            return {"updated": False, "error": "yt-dlp не установлен"}
        if os.getenv("YTDLP_AUTOUPDATE", "1") == "0" and not force:
            return {"updated": False, "disabled": True}
        if st["updating"]:
            return {"updated": False, "busy": True}
        now = time.time()
        min_gap = 6 * 3600 if reason == "circuit" else 3600
        if not force and now - st["last_attempt_ts"] < min_gap:
            return {"updated": False, "throttled": True}


        st["updating"] = True
        st["last_attempt_ts"] = now
        try:
            before = _ytdlp_version()
            latest = await self._pypi_latest_ytdlp()
            st["latest"] = latest
            st["last_check_ts"] = now
            if latest and latest == before and not force:
                logger.info(f"yt-dlp актуален: {before}")
                return {"updated": False, "version": before, "latest": latest}
            if latest is None and reason != "circuit" and not force:
                return {"updated": False, "version": before, "error": "PyPI недоступен"}


            logger.info(f"yt-dlp: обновляю {before} → {latest or 'последняя'} (причина: {reason})")
            ok, err = await self._pip_upgrade_ytdlp()
            if not ok:
                st["last_error"] = err
                logger.warning(f"Автообновление yt-dlp не удалось: {err}")
                return {"updated": False, "error": err}
            if not self._reload_yt_dlp():
                st["last_error"] = "не удалось подгрузить новую версию (нужен перезапуск)"
                return {"updated": False, "error": st["last_error"]}


            after = _ytdlp_version()
            st.update(version=after, last_update_ts=time.time(), last_error=None)
            self.yt_status.update(available=True, consecutive_failures=0, disabled_until=0.0, last_error=None)
            logger.info(f"✅ yt-dlp обновлён: {before} → {after}")
            return {"updated": after != before, "version": after, "previous": before}
        except Exception as e:
            st["last_error"] = str(e)
            logger.warning(f"update_yt_dlp: {e}")
            return {"updated": False, "error": str(e)}
        finally:
            st["updating"] = False


    # ─────────────────────────────────────────────
    #  СТАТУС ИСТОЧНИКОВ
    # ─────────────────────────────────────────────


    def get_source_status(self) -> dict:
        yt = self.get_yt_status()
        if yt_dlp is None:
            yt_state = "missing"
        elif not yt["available"]:
            yt_state = "degraded"
        elif yt.get("half_open"):
            yt_state = "recovering"
        else:
            yt_state = "ok"
        retry = max(0, int(yt["disabled_until"] - time.monotonic())) if not yt["available"] else 0
        st = self.ytdlp_state
        return {
            "youtube": {"state": yt_state, "failures": yt["consecutive_failures"],
                        "retry_in_sec": retry, "last_error": yt.get("last_error")},
            "soundcloud": {"state": "ok" if self.sc_status["ok"] else "degraded",
                           "last_error": self.sc_status["last_error"],
                           "client_id_source": self._cid_source,
                           "client_id_age_sec": int(time.time() - self._cid_ts) if self._cid_ts else None},
            "yt_dlp": {"version": st["version"], "latest": st["latest"],
                       "last_update_ts": st["last_update_ts"], "updating": st["updating"],
                       "last_error": st["last_error"],
                       "autoupdate": os.getenv("YTDLP_AUTOUPDATE", "1") != "0"},
        }


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




async def yt_dlp_maintenance_loop():
    """Раз в сутки проверяет и обновляет yt-dlp (первый раз — через 30 секунд после старта)."""
    if yt_dlp is None:
        return
    await asyncio.sleep(30)
    while True:
        try:
            await music_engine.update_yt_dlp(reason="scheduled")
        except Exception as e:
            logger.warning(f"yt_dlp_maintenance_loop: {e}")
        await asyncio.sleep(24 * 3600)




async def check_yt_dlp_freshness():
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
                    return
                data = await resp.json()
                latest = data.get("info", {}).get("version")
                if not latest:
                    return
                if latest != installed:
                    logger.warning(
                        f"⚠️ yt-dlp устарел: установлена {installed}, в PyPI доступна {latest}. "
                        f"Обновите зависимость: pip install -U yt-dlp"
                    )
                else:
                    logger.info(f"yt-dlp актуален: версия {installed}")
    except Exception as e:
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