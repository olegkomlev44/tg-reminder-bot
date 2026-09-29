import os, json, hmac, hashlib, aiohttp, asyncio, random, re, urllib.parse, uuid, shutil
from urllib.parse import parse_qsl
from aiohttp import web
from music_engine import music_engine, check_yt_dlp_freshness, yt_dlp_maintenance_loop
from db import (
                init_db, get_cached_file_id, save_cached_file_id,
                save_music_fav, get_music_favs, log_track_history, get_user_history, 
                get_total_listen_seconds, save_playlist_track, get_playlists,
                rename_playlist, remove_track_from_playlist, delete_playlist_db,
                remove_music_fav, sync_music_favs, clear_history, DB_PATH,
                save_playback_state, get_playback_state,
                init_db, add_dislike, get_blacklist,
                collab_create, collab_get_meta, collab_get_tracks,
                collab_add_track, collab_remove_track, collab_delete,
                # Новые функции
                upsert_user_profile, get_user_profile, search_users,
                subscribe_artist, unsubscribe_artist, get_subscribed_artists, is_subscribed_artist,
                send_friend_request, accept_friend_request, get_friends,
                get_friend_requests_incoming, get_friend_status,
                get_friend_favs, get_friend_history,
                add_notification, get_notifications, mark_notifications_read,
                get_unread_notifications_count)
import logging
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# DEV_MODE включает фейкового пользователя для локальной разработки без
# реального Telegram-клиента. НИКОГДА не должен быть включён в продакшене —
# при DEV_MODE=1 подпись initData вообще не проверяется.
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
CHUNK = 32 * 1024
INITDATA_MAX_AGE = 24 * 3600  # секунд — старше считаем протухшим (защита от replay)

init_db()

if not BOT_TOKEN and not DEV_MODE:
    logger.error(
        "BOT_TOKEN не задан! Проверка initData Telegram невозможна — "
        "все запросы будут отклонены с 401. Установите BOT_TOKEN или DEV_MODE=1 для локальной разработки."
    )


# ═══════════════════════════════════════════════════════════════
# МЕТРИКИ И /health
# ═══════════════════════════════════════════════════════════════
# Лёгкие in-memory метрики без внешних зависимостей (Prometheus и т.п.) —
# на bothost их некуда собирать снаружи, но /health хотя бы даёт быстрый
# ответ на вопрос "процесс жив и адекватен?" без ручного захода в контейнер.
START_TIME = time.monotonic()
_metrics = {
    "requests_total": 0,
    "requests_by_status": defaultdict(int),
    "errors_total": 0,
    "requests_by_path": defaultdict(int),
}


@web.middleware
async def metrics_middleware(request, handler):
    _metrics["requests_total"] += 1
    # Группируем по префиксу пути (без id), чтобы не разрастался словарь
    path_key = request.path.rsplit("/", 1)[0] if request.path.startswith("/api/") else request.path
    _metrics["requests_by_path"][path_key] += 1
    try:
        response = await handler(request)
        _metrics["requests_by_status"][response.status] += 1
        return response
    except web.HTTPException as ex:
        _metrics["requests_by_status"][ex.status] += 1
        if ex.status >= 500:
            _metrics["errors_total"] += 1
        raise
    except Exception:
        _metrics["errors_total"] += 1
        raise


async def api_health(request):
    """
    Health-check для мониторинга (UptimeRobot/Better Uptime/ручной curl).
    Специально не требует авторизации — сторонний монитор не знает и не
    должен знать initData. Отдаёт 200, только если БД реально отвечает.
    """
    checks = {"db": False, "yt_dlp": False, "ffmpeg": False}
    db_error = None
    try:
        from db import _db_connect
        conn = _db_connect()
        conn.execute("SELECT 1")
        conn.close()
        checks["db"] = True
    except Exception as e:
        db_error = str(e)

    try:
        import yt_dlp as _ydl
        checks["yt_dlp"] = _ydl is not None
    except ImportError:
        checks["yt_dlp"] = False

    yt_status = music_engine.get_yt_status()
    checks["yt_dlp_circuit"] = "available" if yt_status["available"] else "degraded"

    import shutil as _shutil
    checks["ffmpeg"] = _shutil.which("ffmpeg") is not None

    healthy = checks["db"]  # yt-dlp/ffmpeg — деградация допустима, БД — нет
    body = {
        "status": "ok" if healthy else "degraded",
        "uptime_sec": round(time.monotonic() - START_TIME, 1),
        "checks": checks,
        "requests_total": _metrics["requests_total"],
        "errors_total": _metrics["errors_total"],
    }
    if db_error:
        body["db_error"] = db_error
    return web.json_response(body, status=200 if healthy else 503)


async def api_metrics(request):
    """Чуть более подробные метрики — не для публичного доступа, поэтому
    защищена тем же Telegram-auth, что и остальной API."""
    user = verify(request.headers.get("Authorization", ""))
    norm_cache_files = 0
    norm_cache_bytes = 0
    try:
        for fname in os.listdir(NORM_CACHE_DIR):
            fpath = os.path.join(NORM_CACHE_DIR, fname)
            if os.path.isfile(fpath):
                norm_cache_files += 1
                norm_cache_bytes += os.path.getsize(fpath)
    except Exception:
        pass
    return cors(web.json_response({
        "uptime_sec": round(time.monotonic() - START_TIME, 1),
        "requests_total": _metrics["requests_total"],
        "requests_by_status": dict(_metrics["requests_by_status"]),
        "requests_by_path": dict(_metrics["requests_by_path"]),
        "errors_total": _metrics["errors_total"],
        "norm_cache_files": norm_cache_files,
        "norm_cache_mb": round(norm_cache_bytes / (1024 * 1024), 1),
        "rate_limit_active_keys": len(_rate_buckets),
        "youtube_status": music_engine.get_yt_status(),
    }))


def _check_telegram_auth(init_data: str) -> dict | None:
    """
    Проверяет подпись Telegram WebApp initData по алгоритму из документации:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Возвращает распарсенного пользователя при успешной проверке, иначе None.
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            logger.warning("initData: подпись не совпала (возможна подделка запроса)")
            return None

        auth_date = int(pairs.get("auth_date", 0) or 0)
        if auth_date and (time.time() - auth_date) > INITDATA_MAX_AGE:
            logger.warning("initData: подпись просрочена (auth_date старше суток)")
            return None

        user_raw = pairs.get("user")
        if not user_raw:
            return None
        return json.loads(user_raw)
    except Exception as e:
        logger.warning(f"initData: ошибка при проверке: {e}")
        return None


def verify(init_data: str):
    """
    Возвращает словарь пользователя Telegram, только если initData прошла
    криптографическую проверку. Бросает web.HTTPUnauthorized, если проверка
    не пройдена — aiohttp сам превратит это в корректный 401-ответ.
    """
    if DEV_MODE and (not init_data or init_data == "test_mode"):
        return {"id": "123456789", "first_name": "DevUser"}

    user = _check_telegram_auth(init_data)
    if not user:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "invalid or missing Telegram auth"}),
                                    content_type="application/json")

    # Сохраняем профиль при каждом успешно проверенном запросе (кеш актуальных данных)
    try:
        uid = str(user.get("id", ""))
        if uid:
            first = user.get("first_name", "")
            last = user.get("last_name", "")
            name = (first + " " + last).strip() or user.get("username") or "Пользователь"
            upsert_user_profile(uid, name, user.get("username", ""), user.get("photo_url", ""))
    except Exception as e:
        logger.warning(f"upsert_user_profile failed for user {user.get('id')}: {e}")

    return user

def cors(r):
    r.headers.update({
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization, Range, Content-Type",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, X-Source-Degraded, X-Audio-Norm",
    })
    return r


@web.middleware
async def cors_middleware(request, handler):
    """
    Гарантирует CORS-заголовки на ЛЮБОМ ответе, включая ошибки (например,
    401 из verify() при неверной initData) — без этого браузер показывал бы
    "CORS error" вместо реального кода ошибки, что маскирует настоящую причину.
    """
    if request.method == "OPTIONS":
        return cors(web.Response())
    try:
        response = await handler(request)
    except web.HTTPException as ex:
        cors(ex)
        raise
    return cors(response)


# ═══════════════════════════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════════════════════════
# Простой in-memory sliding-window лимитер на процесс. Для одного инстанса
# на bothost этого достаточно; при горизонтальном масштабировании нужно
# заменить на Redis (INCR + EXPIRE) — единый счётчик на все инстансы.

RATE_LIMIT_RULES = [
    # (префикс пути, лимит запросов, окно в секундах)
    ("/api/stream/", 60, 60),        # аудиостримы — разрешаем чаще
    ("/api/suggest", 90, 60),        # подсказки при наборе (с дебаунсом на клиенте)
    ("/api/search", 60, 60),         # первая страница + подгрузка при скролле
    ("/api/resolve", 20, 60),        # поиск запасного источника
    ("/api/prefetch", 40, 60),
    ("/api/status", 20, 60),
    ("/api/fav/sync", 40, 60),
    ("/api/wave", 20, 60),
    ("/api/radio", 20, 60),
    ("/api/lyrics", 30, 60),
    ("/api/users/search", 20, 60),
]

_rate_buckets: dict[str, deque] = defaultdict(deque)


def _is_rate_limited(key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    bucket = _rate_buckets[key]
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


@web.middleware
async def rate_limit_middleware(request, handler):
    if request.method == "OPTIONS":
        return await handler(request)
    for prefix, limit, window in RATE_LIMIT_RULES:
        if request.path.startswith(prefix):
            ip = request.headers.get("X-Forwarded-For", request.remote or "unknown").split(",")[0].strip()
            key = f"{ip}:{prefix}"
            if _is_rate_limited(key, limit, window):
                logger.warning(f"Rate limit hit: {key}")
                return cors(web.json_response(
                    {"error": "too_many_requests", "retry_after": window}, status=429
                ))
            break
    return await handler(request)

async def handle_index(request):
    p = os.path.join(os.path.dirname(__file__), "webapp", "index.html")
    if not os.path.exists(p): p = os.path.join(os.path.dirname(__file__), "index.html")
    return web.Response(text=open(p, encoding="utf-8").read(), content_type="text/html")

async def handle_sw(request):
    p = os.path.join(os.path.dirname(__file__), "webapp", "sw.js")
    if not os.path.exists(p): 
        p = os.path.join(os.path.dirname(__file__), "sw.js")
    return web.FileResponse(p)

async def api_get_tracks(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]
    return cors(web.json_response({
        "favs": get_music_favs(uid),
        "history": get_user_history(uid),
        "playlists": get_playlists(uid),
        "listen_seconds": get_total_listen_seconds(uid),
    }))

async def api_search(request):
    q = request.rel_url.query.get("q", "").strip()
    if not q: return cors(web.json_response([]))
    try:
        limit = max(1, min(int(request.rel_url.query.get("limit", 120)), 120))
        offset = max(0, min(int(request.rel_url.query.get("offset", 0)), 500))
    except ValueError:
        limit, offset = 120, 0
    tracks = await music_engine.search_multi(q, limit=limit, offset=offset)
    resp = web.json_response(tracks)
    # Пустой результат мог получиться не потому что "ничего не нашлось",
    # а потому что YouTube-источник сейчас в circuit-breaker cooldown —
    # явно сигнализируем об этом заголовком, а не молчим полным нулём.
    yt_status = music_engine.get_yt_status()
    if not tracks and not yt_status["available"] and offset == 0:
        resp.headers["X-Source-Degraded"] = "youtube"
    return cors(resp)

async def api_suggest(request):
    q = request.rel_url.query.get("q", "").strip()
    if len(q) < 2: return cors(web.json_response([]))
    return cors(web.json_response(await music_engine.suggest(q, limit=6)))

async def api_resolve(request):
    """Тот же трек в другом источнике — запасной вариант, когда основной не грузится."""
    qs = request.rel_url.query
    title = qs.get("title", "").strip()[:200]
    artist = qs.get("artist", "").strip()[:200]
    exclude = qs.get("exclude", "").strip()[:64]
    prefer = qs.get("prefer") or ("sc" if exclude.startswith("yt_") else "yt")
    try:
        dur = int(qs.get("dur", 0) or 0)
    except ValueError:
        dur = 0
    if not title:
        return cors(web.json_response({"track": None}))
    track = await music_engine.find_alternative(title, artist, prefer=prefer, exclude_id=exclude, duration_sec=dur)
    return cors(web.json_response({"track": track}))

async def api_status(request):
    """Статус источников и кэша — для карточки «Состояние» в профиле."""
    verify(request.headers.get("Authorization", ""))
    files = size = 0
    try:
        for fname in os.listdir(NORM_CACHE_DIR):
            fpath = os.path.join(NORM_CACHE_DIR, fname)
            if os.path.isfile(fpath) and not fname.endswith(".tmp"):
                files += 1
                size += os.path.getsize(fpath)
    except Exception:
        pass
    try:
        free = shutil.disk_usage(NORM_CACHE_DIR).free
    except Exception:
        free = None
    body = music_engine.get_source_status()
    body.update({
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "cache": {"files": files, "bytes": size, "limit_bytes": NORM_CACHE_MAX_BYTES, "disk_free_bytes": free},
        "uptime_sec": round(time.monotonic() - START_TIME, 1),
    })
    return cors(web.json_response(body))

async def api_radio(request):
    """
    'Радио' — бесконечный автоплей от ОДНОГО трека (в отличие от /api/wave,
    который строит подборку из всего избранного+истории пользователя).
    Фронтенд вызывает это при включении радио и затем повторно — с последним
    треком очереди как новым сидом — чтобы очередь пополнялась сама по себе.
    """
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]
    artist = request.rel_url.query.get("artist", "").strip()
    title = request.rel_url.query.get("title", "").strip()
    exclude_id = str(request.rel_url.query.get("exclude_id", ""))
    try:
        limit = max(1, min(int(request.rel_url.query.get("limit", 10)), 20))
    except ValueError:
        limit = 10

    if not artist:
        return cors(web.json_response({"error": "artist required"}, status=400))

    bl = get_blacklist(uid)
    seen = {exclude_id} if exclude_id else set()
    results = []

    try:
        similar = await music_engine.get_similar_lastfm(artist, title, limit=limit + 10)
    except Exception as e:
        logger.warning(f"api_radio: Last.fm error для '{artist} - {title}': {e}")
        similar = []

    async def resolve(sugg):
        try:
            q = f"{sugg['artist']} {sugg['title']}"
            res = await music_engine.search_multi(q, limit=1)
            return res[0] if res else None
        except Exception:
            return None

    if similar:
        resolved = await asyncio.gather(*[resolve(s) for s in similar[:limit + 10]], return_exceptions=True)
        for t in resolved:
            if isinstance(t, dict) and t.get("id"):
                tid = str(t["id"])
                if tid not in seen and tid not in bl:
                    seen.add(tid)
                    results.append(t)
                    if len(results) >= limit:
                        break

    # Last.fm недоступен / нет API-ключа / ничего не нашлось — не оставляем
    # радио молча пустым, подкидываем что-то по духу через чарты.
    if not results:
        charts = await music_engine.get_charts(limit=limit + 10)
        for t in charts:
            tid = str(t.get("id", ""))
            if tid and tid not in seen and tid not in bl:
                seen.add(tid)
                results.append(t)
                if len(results) >= limit:
                    break

    return cors(web.json_response(results))


async def api_wave(request):
    limit = int(request.rel_url.query.get("limit", 120))
    offset = int(request.rel_url.query.get("offset", 0))
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]

    bl = get_blacklist(uid)
    favs = get_music_favs(uid)
    history = get_user_history(uid)

    unique_tracks = []
    seen = set()

    # ── 1. УМНАЯ ВОЛНА (только первая страница) ──────────────────────────────
    base_pool = favs + history
    if base_pool and offset == 0:

        # Берём до 3 сидов: приоритет — избранному
        seed_pool = favs if favs else history
        seeds = random.sample(seed_pool, min(3, len(seed_pool)))
        lastfm_suggestions = []

        # Параллельно запрашиваем Last.fm: similar tracks + similar artists
        async def lastfm_similar_tracks(seed):
            return await music_engine.get_similar_lastfm(seed["artist"], seed["title"], limit=6)

        async def lastfm_similar_artists(artist: str):
            """Берём топ-треки артистов, похожих на seed-артиста."""
            LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
            if not LASTFM_API_KEY:
                return []
            url = "http://ws.audioscrobbler.com/2.0/"
            params = {
                "method": "artist.getsimilar",
                "artist": artist,
                "api_key": LASTFM_API_KEY,
                "format": "json",
                "limit": 4,
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            similar_artists = [
                                a["name"] for a in data.get("similarartists", {}).get("artist", [])
                            ]
                            # Для каждого похожего артиста берём его топ-трек
                            top_tracks = []
                            async def get_top_track(art):
                                p2 = {
                                    "method": "artist.gettoptracks",
                                    "artist": art,
                                    "api_key": LASTFM_API_KEY,
                                    "format": "json",
                                    "limit": 2,
                                }
                                async with session.get(url, params=p2, timeout=aiohttp.ClientTimeout(total=5)) as r2:
                                    if r2.status == 200:
                                        d2 = await r2.json()
                                        tracks = d2.get("toptracks", {}).get("track", [])
                                        return [{"title": t["name"], "artist": art} for t in tracks]
                                return []
                            results = await asyncio.gather(*[get_top_track(a) for a in similar_artists])
                            for r in results:
                                top_tracks.extend(r)
                            return top_tracks
            except Exception as e:
                logger.error(f"Last.fm similar artists error: {e}")
            return []

        # Запускаем все Last.fm запросы параллельно
        sim_track_tasks = [lastfm_similar_tracks(s) for s in seeds]
        sim_artist_tasks = [lastfm_similar_artists(s["artist"]) for s in seeds]
        all_results = await asyncio.gather(*(sim_track_tasks + sim_artist_tasks), return_exceptions=True)

        for res in all_results:
            if isinstance(res, list):
                lastfm_suggestions.extend(res)

        # Дедуп Last.fm подсказок
        seen_suggestions = set()
        unique_suggestions = []
        for s in lastfm_suggestions:
            key = f"{s['artist'].lower()}::{s['title'].lower()}"
            if key not in seen_suggestions:
                seen_suggestions.add(key)
                unique_suggestions.append(s)

        # ── 2. КОЛЛАБОРАТИВНАЯ ФИЛЬТРАЦИЯ из локальной БД ───────────────────
        from db import _db_connect
        user_track_ids = {t["id"] for t in base_pool}
        cf_tracks = []
        if len(user_track_ids) >= 1:
            conn = _db_connect()
            c = conn.cursor()
            placeholders = ",".join(["?"] * len(user_track_ids))
            try:
                c.execute(f"""
                    SELECT track_id, title, artist, artwork_url, source, COUNT(*) as score
                    FROM favorites
                    WHERE user_id IN (
                        SELECT DISTINCT user_id FROM favorites
                        WHERE track_id IN ({placeholders}) AND user_id != ?
                    )
                    AND track_id NOT IN ({placeholders})
                    GROUP BY track_id
                    ORDER BY score DESC
                    LIMIT 10
                """, list(user_track_ids) + [str(uid)] + list(user_track_ids))
                for r in c.fetchall():
                    if str(r[0]) not in bl:
                        cf_tracks.append({
                            "id": str(r[0]), "title": r[1], "artist": r[2],
                            "artwork_url": r[3], "source": r[4] or "SoundCloud"
                        })
            except Exception as e:
                logger.error(f"CF query error: {e}")
            conn.close()

        # ── 3. Резолвим Last.fm подсказки через SC/YT параллельно ───────────
        async def resolve_track(sugg):
            try:
                q = f"{sugg['artist']} {sugg['title']}"
                res = await music_engine.search_multi(q, limit=1)
                return res[0] if res else None
            except Exception:
                return None

        # Ограничиваем до 15 подсказок, чтобы не перегружать SC
        resolve_tasks = [resolve_track(s) for s in unique_suggestions[:15]]
        resolved = await asyncio.gather(*resolve_tasks, return_exceptions=True)

        # Сначала добавляем CF-треки (они уже в нужном формате)
        for t in cf_tracks:
            if t["id"] not in seen and t["id"] not in bl:
                seen.add(t["id"])
                unique_tracks.append(t)

        # Потом Last.fm resolved
        for t in resolved:
            if isinstance(t, dict) and t.get("id"):
                tid = str(t["id"])
                if tid not in seen and tid not in bl:
                    seen.add(tid)
                    unique_tracks.append(t)

    # ── 4. ФОЛЛБЭК: чарты / рандомные запросы ────────────────────────────────
    if len(unique_tracks) < limit:
        need = limit - len(unique_tracks)
        charts = await music_engine.get_charts(limit=need + 5, offset=offset)
        if not charts:
            fallback_q = random.choice(["rap hits", "phonk", "pop hits", "lofi beats", "indie rock"])
            charts = await music_engine.search_multi(fallback_q, limit=need + 5)
        for t in charts:
            tid = str(t.get("id", ""))
            if tid and tid not in seen and tid not in bl:
                seen.add(tid)
                unique_tracks.append(t)
                if len(unique_tracks) >= limit:
                    break

    random.shuffle(unique_tracks)
    return cors(web.json_response(unique_tracks[:limit]))

async def api_fav_add(request):
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        if body.get("track_data"):
            ok = save_music_fav(user["id"], body["track_data"])
            return cors(web.json_response({"ok": ok}))
    except Exception as e:
        logger.warning(f"api_fav_add: {e}")
    return cors(web.json_response({"error": "bad req"}, status=400))

async def api_ytdlp_update(request):
    """Кнопка «Обновить сейчас» в карточке статуса профиля."""
    verify(request.headers.get("Authorization", ""))
    result = await music_engine.update_yt_dlp(reason="manual", force=True)
    return cors(web.json_response(result))

async def api_fav_sync(request):
    """Слияние очереди офлайн-операций клиента с избранным на сервере."""
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        ops = body.get("ops") or []
        if not isinstance(ops, list):
            raise ValueError("ops must be a list")
    except Exception as e:
        logger.warning(f"api_fav_sync: {e}")
        return cors(web.json_response({"error": "bad req"}, status=400))
    favs = sync_music_favs(user["id"], ops)
    return cors(web.json_response({"ok": True, "favs": favs}))

async def api_fav_remove(request):
    """Удалить трек из избранного по track_id."""
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        track_id = body.get("track_id")
        if track_id:
            remove_music_fav(user["id"], track_id)
            return cors(web.json_response({"ok": True}))
    except Exception as e:
        logger.warning(f"api_fav_remove: {e}")
    return cors(web.json_response({"error": "bad req"}, status=400))

async def api_history_clear(request):
    """Полная очистка истории прослушиваний."""
    user = verify(request.headers.get("Authorization", ""))
    clear_history(user["id"])
    return cors(web.json_response({"ok": True}))


# ── СИНХРОНИЗАЦИЯ ВОСПРОИЗВЕДЕНИЯ МЕЖДУ УСТРОЙСТВАМИ ────────────────────
async def api_playback_sync(request):
    """
    Устройство периодически (раз в ~10-15 сек, на паузе/смене трека) шлёт сюда
    свою позицию. Хранится последнее состояние на пользователя — если он
    откроет приложение на другом устройстве, оно увидит: "продолжить с Y:XX?".
    """
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        track_data = body.get("track_data")
        device_id = str(body.get("device_id", "unknown"))[:64]
        position_sec = float(body.get("position_sec", 0))
        queue_data = body.get("queue")  # опционально — чтобы на другом устройстве не терять очередь
        if not track_data or not track_data.get("id"):
            return cors(web.json_response({"error": "track_data required"}, status=400))
        # Не тащим огромную очередь в БД бесконечно — разумный кап
        if isinstance(queue_data, list) and len(queue_data) > 200:
            queue_data = queue_data[:200]
        save_playback_state(user["id"], device_id, track_data, position_sec, queue_data)
        return cors(web.json_response({"ok": True}))
    except Exception as e:
        logger.warning(f"api_playback_sync: {e}")
        return cors(web.json_response({"error": "bad req"}, status=400))


async def api_playback_state(request):
    """
    Отдаёт последнее сохранённое состояние воспроизведения пользователя.
    Клиент передаёт свой device_id — если состояние пришло с ДРУГОГО
    устройства, показывает предложение продолжить; если это его же
    устройство (например, после перезагрузки страницы) — можно просто
    восстановиться молча.
    """
    user = verify(request.headers.get("Authorization", ""))
    device_id = str(request.rel_url.query.get("device_id", ""))
    state = get_playback_state(user["id"])
    if not state:
        return cors(web.json_response({"state": None}))
    state["is_other_device"] = bool(device_id) and state.get("device_id") != device_id
    return cors(web.json_response({"state": state}))


# --- НОВЫЕ API: ДИЗЛАЙКИ И ТЕКСТЫ ---
async def api_dislike(request):
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        add_dislike(user["id"], body["track_id"])
        return cors(web.json_response({"ok": True}))
    except Exception: return cors(web.json_response({"error": "bad req"}, status=400))

async def api_lyrics(request):
    title = request.rel_url.query.get("title", "")
    artist = request.rel_url.query.get("artist", "")
    import re as _re
    clean_title = _re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    
    # Try lrclib /api/get first (exact match), then /api/search
    endpoints = [
        f"https://lrclib.net/api/get?track_name={urllib.parse.quote(clean_title)}&artist_name={urllib.parse.quote(artist)}",
        f"https://lrclib.net/api/search?q={urllib.parse.quote(artist + ' ' + clean_title)}",
    ]
    try:
        async with aiohttp.ClientSession() as session:
            for url in endpoints:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # /api/search returns list, /api/get returns object
                        if isinstance(data, list):
                            data = data[0] if data else {}
                        synced = data.get("syncedLyrics") or ""
                        plain = data.get("plainLyrics") or ""
                        if synced or plain:
                            return cors(web.json_response({
                                "lyrics": plain,
                                "synced": synced,
                            }))
    except Exception as e:
        logger.error(f"Lyrics err: {e}")
    return cors(web.json_response({"lyrics": None, "synced": None}))

# -----------------------------------

async def api_history_add(request):
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        if body.get("track_data"):
            track_data = body["track_data"]
            if not track_data.get('duration_sec') and track_data.get('duration'):
                try:
                    parts = str(track_data['duration']).split(':')
                    track_data['duration_sec'] = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0
                except (ValueError, IndexError):
                    track_data['duration_sec'] = 0
            log_track_history(user["id"], track_data)
        return cors(web.json_response({"ok": True}))
    except Exception as e:
        logger.warning(f"api_history_add: {e}")
    return cors(web.json_response({"error": "bad"}, status=400))
  
async def api_pl_add(request):
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        save_playlist_track(user["id"], body["name"], body["track_data"])
        return cors(web.json_response({"ok": True}))
    except Exception: return cors(web.json_response({"error": "bad"}, status=400))

async def api_pl_rename(request):
    user = verify(request.headers.get("Authorization", ""))
    data = await request.json()
    rename_playlist(user["id"], data["old_name"], data["new_name"])
    return cors(web.json_response({"ok": True}))

async def api_pl_remove_track(request):
    user = verify(request.headers.get("Authorization", ""))
    data = await request.json()
    remove_track_from_playlist(user["id"], data["name"], data["track_id"])
    return cors(web.json_response({"ok": True}))

async def api_pl_delete(request):
    user = verify(request.headers.get("Authorization", ""))
    data = await request.json()
    delete_playlist_db(user["id"], data["name"])
    return cors(web.json_response({"ok": True}))

async def api_pl_create(request):
    # Пустой плейлист существует только в localStorage на клиенте.
    # В БД запись появится при первом api/playlist/add.
    # Этот эндпоинт нужен только чтобы фронт не получал 404.
    try:
        await request.json()
    except Exception:
        pass
    return cors(web.json_response({"ok": True}))

async def api_stream_track(request):
    tid = request.match_info["track_id"]

    # ReplayGain / EBU R128 через ffmpeg — только для первичных запросов (без Range)
    normalize = request.rel_url.query.get("norm", "1") != "0"
    rng = request.headers.get("Range", "")

    # Уже нормализовали этот трек раньше — отдаём готовый файл с диска и не
    # трогаем ffmpeg вообще. web.FileResponse сам умеет Range-запросы, так что
    # перемотка внутри уже играющего трека тоже пойдёт из кэша, а не с источника.
    if normalize:
        cached_path = _normalized_cache_path(tid)
        if not os.path.exists(cached_path) and _prefetch_state.get(tid) == "running":
            # Этот же трек прямо сейчас досчитывается в фоне (предзагрузка) —
            # подождём до 20 секунд вместо второго параллельного транскода.
            for _ in range(40):
                await asyncio.sleep(0.5)
                if os.path.exists(cached_path) or _prefetch_state.get(tid) != "running":
                    break
        if os.path.exists(cached_path):
            resp = web.FileResponse(cached_path, headers={"X-Audio-Norm": "cached"})
            return cors(resp)

    # Две попытки: если закэшированная ссылка на аудио протухла (403/404/410 от
    # источника или ffmpeg не смог её открыть) — сбрасываем кэш и берём свежую.
    for attempt in (0, 1):
        track = await music_engine.get_track_details(tid, fresh=(attempt == 1))
        if not track: return cors(web.Response(status=404, text="Stream not found"))

        # У YouTube-треков track["stream_url"] — служебный маркер вида "yt_...",
        # а не http-адрес (его понимает только download_file, см. music_engine.py).
        # Реальный проксируемый адрес аудио-CDN лежит в direct_stream_url.
        if tid.startswith("yt_"):
            source_url = track.get("direct_stream_url")
            extra_headers = track.get("stream_headers") or {}
        else:
            source_url = track.get("stream_url")
            extra_headers = {}
        if not source_url: return cors(web.Response(status=404, text="Stream not found"))

        if normalize and not rng:
            try:
                result = await _stream_normalized(request, source_url, tid)
            except Exception as e:
                logger.warning(f"ffmpeg loudnorm failed ({e}), passthrough")
                result = None
            if result is False and attempt == 0:   # источник не отдал ни байта
                music_engine.invalidate_details(tid)
                continue
            if result:
                return result

        hdrs = {"User-Agent": "Mozilla/5.0", **extra_headers}
        if rng: hdrs["Range"] = rng
        started = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source_url, headers=hdrs) as up:
                    if up.status in (401, 403, 404, 410) and attempt == 0:
                        music_engine.invalidate_details(tid)
                        continue
                    ct = up.headers.get("Content-Type", "audio/mpeg")
                    rh = {"Content-Type": ct, "Accept-Ranges": "bytes", "Cache-Control": "max-age=3600",
                          "Access-Control-Allow-Origin": "*", "X-Audio-Norm": "passthrough"}
                    if cl := up.headers.get("Content-Length"): rh["Content-Length"] = cl
                    if cr := up.headers.get("Content-Range"): rh["Content-Range"] = cr
                    resp = web.StreamResponse(status=up.status, headers=rh)
                    await resp.prepare(request)
                    started = True
                    async for chunk in up.content.iter_chunked(CHUNK): await resp.write(chunk)
                    return resp
        except Exception as e:
            if started:
                return resp  # клиент отвалился посреди трека — повторять нечего
            if attempt == 0:
                music_engine.invalidate_details(tid)
                continue
            return cors(web.Response(status=502, text=str(e)))
    return cors(web.Response(status=502, text="Stream unavailable"))

NORM_CACHE_DIR = os.path.join(os.path.dirname(DB_PATH), "norm_cache")
os.makedirs(NORM_CACHE_DIR, exist_ok=True)
NORM_CACHE_MAX_BYTES = int(os.getenv("NORM_CACHE_MAX_MB", "500")) * 1024 * 1024


def _normalized_cache_path(track_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(track_id))
    return os.path.join(NORM_CACHE_DIR, f"{safe_id}.mp3")


def _prune_norm_cache():
    """Простая эвикция по размеру: удаляем самые давно использованные файлы,
    пока суммарный размер кэша не впишется в лимит. Диск на bothost обычно
    ограничен, поэтому кэш не должен расти бесконечно."""
    try:
        entries = []
        total = 0
        for fname in os.listdir(NORM_CACHE_DIR):
            if fname.endswith(".tmp"):
                continue
            fpath = os.path.join(NORM_CACHE_DIR, fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            entries.append((st.st_atime, st.st_size, fpath))
            total += st.st_size
        if total <= NORM_CACHE_MAX_BYTES:
            return
        entries.sort(key=lambda e: e[0])  # старые по atime — первыми на удаление
        for atime, size, fpath in entries:
            if total <= NORM_CACHE_MAX_BYTES:
                break
            try:
                os.remove(fpath)
                total -= size
            except OSError:
                pass
    except Exception as e:
        logger.warning(f"_prune_norm_cache: {e}")


def _ffmpeg_loudnorm_cmd(source_url: str, output: str) -> list:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", source_url,
        "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=none",
        "-vn",
        "-c:a", "libmp3lame", "-q:a", "2",
        "-f", "mp3", output,
    ]
    return cmd


async def _stream_normalized(request, source_url: str, track_id: str):
    """
    Проксирует аудио через ffmpeg loudnorm EBU R128 (-14 LUFS), одновременно
    отдавая поток клиенту и записывая его во временный файл на диске.
    По завершении без ошибок временный файл атомарно переименовывается в
    постоянный кэш — следующий запрос этого трека уже не тронет ffmpeg.

    Возвращает: StreamResponse — успех; None — ffmpeg не установлен (играем
    напрямую); False — ffmpeg не получил от источника ни байта (ссылка мертва).
    Заголовки клиенту уходят только после первого куска данных, поэтому при
    мёртвой ссылке вызывающий код ещё может сделать повторную попытку.
    """
    if not shutil.which("ffmpeg"):
        return None  # ffmpeg не установлен — тихий fallback

    cache_path = _normalized_cache_path(track_id)
    # Уникальный суффикс: два одновременных первых запроса одного трека не
    # должны писать в один и тот же временный файл.
    tmp_path = f"{cache_path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"

    proc = await asyncio.create_subprocess_exec(
        *_ffmpeg_loudnorm_cmd(source_url, "pipe:1"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    resp = None
    tmp_file = None
    completed_ok = False
    try:
        try:
            first = await asyncio.wait_for(proc.stdout.read(CHUNK), timeout=30)
        except asyncio.TimeoutError:
            first = b""
        if not first:
            return False
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "max-age=3600",
            "Access-Control-Allow-Origin": "*",
            "X-Audio-Norm": "loudnorm-r128",
            "Transfer-Encoding": "chunked",
        })
        await resp.prepare(request)
        tmp_file = open(tmp_path, "wb")
        await resp.write(first)
        tmp_file.write(first)
        while True:
            chunk = await proc.stdout.read(CHUNK)
            if not chunk:
                break
            await resp.write(chunk)
            tmp_file.write(chunk)
        rc = await proc.wait()
        completed_ok = (rc == 0)
    finally:
        if tmp_file:
            tmp_file.close()
        if proc.returncode is None:
            try: proc.kill()
            except Exception: pass
            await proc.wait()
        # Кэшируем, только если ffmpeg реально доработал до конца (rc==0) —
        # иначе в кэше мог бы осесть оборванный/битый файл.
        if completed_ok and os.path.exists(tmp_path):
            try:
                os.replace(tmp_path, cache_path)
                _prune_norm_cache()
            except OSError as e:
                logger.warning(f"norm_cache rename failed: {e}")
                try: os.remove(tmp_path)
                except OSError: pass
        elif os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass
    return resp


# ═══════════════════════════════════════════════════════════════
# ПРЕДЗАГРУЗКА СЛЕДУЮЩИХ ТРЕКОВ
# ═══════════════════════════════════════════════════════════════
# Пока играет текущий трек, клиент просит подготовить следующие 1-2: сервер
# заранее получает ссылку на аудио и нормализует трек в кэш. Когда очередь
# доходит до него, файл отдаётся с диска мгновенно (и с нормой громкости).
_prefetch_state: dict[str, str] = {}      # track_id -> "queued" | "running"
_prefetch_sem = asyncio.Semaphore(1)      # один ffmpeg за раз — хостинг слабый
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


async def _normalize_to_cache(source_url: str, track_id: str) -> bool:
    cache_path = _normalized_cache_path(track_id)
    tmp_path = f"{cache_path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    cmd = _ffmpeg_loudnorm_cmd(source_url, tmp_path)
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10", *cmd]  # не отнимаем CPU у самого бота
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=300)
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        await proc.wait()
        rc = -1
    ok = rc == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 10_000
    if ok:
        try:
            os.replace(tmp_path, cache_path)
            _prune_norm_cache()
        except OSError as e:
            logger.warning(f"prefetch: не удалось сохранить кэш: {e}")
            ok = False
    if not ok and os.path.exists(tmp_path):
        try: os.remove(tmp_path)
        except OSError: pass
    return ok


async def _prefetch_track(tid: str):
    try:
        async with _prefetch_sem:
            if os.path.exists(_normalized_cache_path(tid)):
                return
            _prefetch_state[tid] = "running"
            track = await music_engine.get_track_details(tid)   # заодно прогревает кэш ссылок
            if not track:
                return
            src = track.get("direct_stream_url") if tid.startswith("yt_") else track.get("stream_url")
            if not src or not shutil.which("ffmpeg"):
                return
            await _normalize_to_cache(src, tid)
    except Exception as e:
        logger.debug(f"prefetch {tid}: {e}")
    finally:
        _prefetch_state.pop(tid, None)


async def api_prefetch(request):
    verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        ids = list(dict.fromkeys(str(i) for i in (body.get("ids") or [])))[:3]
    except Exception:
        return cors(web.json_response({"error": "bad req"}, status=400))
    queued, cached = [], []
    for tid in ids:
        if not _ID_RE.match(tid):
            continue
        if os.path.exists(_normalized_cache_path(tid)):
            cached.append(tid)
        elif tid not in _prefetch_state and len(_prefetch_state) < 6:
            _prefetch_state[tid] = "queued"
            asyncio.create_task(_prefetch_track(tid))
            queued.append(tid)
    return cors(web.json_response({"queued": queued, "cached": cached}))

# ═══════════════════════════════════════════════════════════════
# ARTIST SUBSCRIPTIONS
# ═══════════════════════════════════════════════════════════════

async def api_artist_subscribe(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    try:
        body = await request.json()
        artist = body.get("artist", "").strip()
        if not artist:
            return cors(web.json_response({"error": "no artist"}, status=400))
        action = body.get("action", "subscribe")  # subscribe | unsubscribe
        if action == "unsubscribe":
            unsubscribe_artist(uid, artist)
            return cors(web.json_response({"ok": True, "subscribed": False}))
        ok = subscribe_artist(uid, artist)
        return cors(web.json_response({"ok": ok, "subscribed": True}))
    except Exception as e:
        return cors(web.json_response({"error": str(e)}, status=400))

async def api_artist_status(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    artist = request.rel_url.query.get("artist", "").strip()
    if not artist:
        return cors(web.json_response({"subscribed": False}))
    subscribed = is_subscribed_artist(uid, artist)
    return cors(web.json_response({"subscribed": subscribed}))

async def api_subscribed_artists(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    artists = get_subscribed_artists(uid)
    return cors(web.json_response({"artists": artists}))

# ═══════════════════════════════════════════════════════════════
# FRIENDS
# ═══════════════════════════════════════════════════════════════

async def api_users_search(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    q = request.rel_url.query.get("q", "").strip()
    if len(q) < 2:
        return cors(web.json_response([]))
    results = search_users(q, uid)
    # Добавляем статус дружбы к каждому
    for r in results:
        r["friend_status"] = get_friend_status(uid, r["user_id"])
    return cors(web.json_response(results))

async def api_friend_request(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    first = user.get("first_name", "")
    last = user.get("last_name", "")
    my_name = (first + " " + last).strip() or user.get("username") or "Пользователь"
    my_avatar = user.get("photo_url", "")
    try:
        body = await request.json()
        to_id = str(body.get("to_user_id", "")).strip()
        if not to_id or to_id == uid:
            return cors(web.json_response({"error": "invalid"}, status=400))
        result = send_friend_request(uid, my_name, my_avatar, to_id)
        # Уведомление получателю
        if result in ('sent', 'accepted'):
            ntype = "friend_accepted" if result == 'accepted' else "friend_request"
            nbody = f"{my_name} принял(а) запрос" if result == 'accepted' else f"{my_name} хочет добавить тебя в друзья"
            add_notification(to_id, ntype, "👥 Друзья", nbody, {"from_user_id": uid, "from_user_name": my_name, "from_user_avatar": my_avatar})
            if result == 'accepted':
                add_notification(uid, "friend_accepted", "👥 Друзья", f"Вы теперь друзья с {my_name}", {"from_user_id": to_id})
        return cors(web.json_response({"ok": True, "result": result}))
    except Exception as e:
        return cors(web.json_response({"error": str(e)}, status=400))

async def api_friend_accept(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    first = user.get("first_name", "")
    last = user.get("last_name", "")
    my_name = (first + " " + last).strip() or user.get("username") or "Пользователь"
    my_avatar = user.get("photo_url", "")
    try:
        body = await request.json()
        from_id = str(body.get("from_user_id", "")).strip()
        accept_friend_request(from_id, uid, my_name, my_avatar)
        # Уведомление отправителю запроса
        add_notification(from_id, "friend_accepted", "👥 Друзья", f"{my_name} принял(а) твой запрос в друзья 🎉",
                         {"from_user_id": uid, "from_user_name": my_name, "from_user_avatar": my_avatar})
        return cors(web.json_response({"ok": True}))
    except Exception as e:
        return cors(web.json_response({"error": str(e)}, status=400))

async def api_friends_list(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    friends = get_friends(uid)
    incoming = get_friend_requests_incoming(uid)
    return cors(web.json_response({"friends": friends, "incoming": incoming}))

async def api_friend_tracks(request):
    """Треки друга (избранное + история)."""
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    friend_id = request.match_info["friend_id"]
    # Проверяем что реально друзья
    status = get_friend_status(uid, friend_id)
    if status != 'friends':
        return cors(web.json_response({"error": "not friends"}, status=403))
    profile = get_user_profile(friend_id)
    favs = get_friend_favs(friend_id)
    history = get_friend_history(friend_id)
    return cors(web.json_response({"profile": profile, "favs": favs, "history": history}))

# ═══════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

async def api_notifications_get(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    notifs = get_notifications(uid)
    unread = get_unread_notifications_count(uid)
    return cors(web.json_response({"notifications": notifs, "unread": unread}))

async def api_notifications_read(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    mark_notifications_read(uid)
    return cors(web.json_response({"ok": True}))


async def start_web_server():
    app = web.Application(middlewares=[metrics_middleware, cors_middleware, rate_limit_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", api_health)
    app.router.add_get("/api/metrics", api_metrics)
    app.router.add_get("/sw.js", handle_sw)
    app.router.add_get("/api/tracks", api_get_tracks)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/suggest", api_suggest)
    app.router.add_get("/api/resolve", api_resolve)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/prefetch", api_prefetch)
    app.router.add_post("/api/fav/sync", api_fav_sync)
    app.router.add_post("/api/ytdlp/update", api_ytdlp_update)
    app.router.add_get("/api/wave", api_wave)
    app.router.add_get("/api/radio", api_radio)
    app.router.add_get("/api/lyrics", api_lyrics)
    app.router.add_post("/api/dislike", api_dislike)
    app.router.add_post("/api/fav", api_fav_add)
    app.router.add_post("/api/fav/remove", api_fav_remove)
    app.router.add_post("/api/history", api_history_add)
    app.router.add_post("/api/history/clear", api_history_clear)
    app.router.add_post("/api/playback/sync", api_playback_sync)
    app.router.add_get("/api/playback/state", api_playback_state)
    app.router.add_post("/api/playlist/add", api_pl_add)
    app.router.add_post("/api/playlist/create", api_pl_create)
    app.router.add_post("/api/playlist/rename", api_pl_rename)
    app.router.add_post("/api/playlist/remove_track", api_pl_remove_track)
    app.router.add_post("/api/playlist/delete", api_pl_delete)
    app.router.add_get("/api/stream/{track_id}", api_stream_track)
    # Коллаборативные плейлисты
    app.router.add_post("/api/collab/create", api_collab_create)
    app.router.add_get("/api/collab/{cid}", api_collab_get)
    app.router.add_post("/api/collab/{cid}/add", api_collab_add_track)
    app.router.add_post("/api/collab/{cid}/remove", api_collab_remove_track)
    app.router.add_post("/api/collab/{cid}/delete", api_collab_delete)
    # Подписки на артистов
    app.router.add_post("/api/artist/subscribe", api_artist_subscribe)
    app.router.add_get("/api/artist/status", api_artist_status)
    app.router.add_get("/api/artist/subscriptions", api_subscribed_artists)
    # Друзья
    app.router.add_get("/api/users/search", api_users_search)
    app.router.add_post("/api/friends/request", api_friend_request)
    app.router.add_post("/api/friends/accept", api_friend_accept)
    app.router.add_get("/api/friends", api_friends_list)
    app.router.add_get("/api/friends/{friend_id}/tracks", api_friend_tracks)
    # Уведомления
    app.router.add_get("/api/notifications", api_notifications_get)
    app.router.add_post("/api/notifications/read", api_notifications_read)
    app.router.add_options("/{path_info:.*}", lambda r: cors(web.Response()))

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 Web server on :{port}")

    # Не блокирует старт: раз в сутки проверяет PyPI и обновляет yt-dlp сам
    # (отключается переменной YTDLP_AUTOUPDATE=0).
    asyncio.create_task(yt_dlp_maintenance_loop())

# ═══════════════════════════════════════════════════════════
# КОЛЛАБОРАТИВНЫЕ ПЛЕЙЛИСТЫ — API
# ═══════════════════════════════════════════════════════════

def _tg_user_info(request) -> tuple[str, str, str]:
    """Возвращает (user_id, display_name, photo_url) из initData."""
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user.get("id", "unknown"))
    first = user.get("first_name", "")
    last = user.get("last_name", "")
    name = (first + " " + last).strip() or user.get("username") or "Участник"
    avatar = user.get("photo_url", "")
    return uid, name, avatar

async def api_collab_create(request):
    uid, name, avatar = _tg_user_info(request)
    try:
        body = await request.json()
        pl_name = body.get("name", "Коллаб-плейлист")[:80]
    except Exception:
        pl_name = "Коллаб-плейлист"
    cid = collab_create(uid, name, avatar, pl_name)
    return cors(web.json_response({"ok": True, "id": cid}))

async def api_collab_get(request):
    cid = request.match_info["cid"]
    meta = collab_get_meta(cid)
    if not meta:
        return cors(web.json_response({"error": "not found"}, status=404))
    tracks = collab_get_tracks(cid)
    return cors(web.json_response({"meta": meta, "tracks": tracks}))

async def api_collab_add_track(request):
    uid, name, avatar = _tg_user_info(request)
    cid = request.match_info["cid"]
    if not collab_get_meta(cid):
        return cors(web.json_response({"error": "not found"}, status=404))
    try:
        body = await request.json()
        track = body["track_data"]
    except Exception:
        return cors(web.json_response({"error": "bad request"}, status=400))
    ok = collab_add_track(cid, track, uid, name, avatar)
    return cors(web.json_response({"ok": ok}))

async def api_collab_remove_track(request):
    uid, _, _ = _tg_user_info(request)
    cid = request.match_info["cid"]
    meta = collab_get_meta(cid)
    if not meta:
        return cors(web.json_response({"error": "not found"}, status=404))
    try:
        body = await request.json()
        track_id = body["track_id"]
    except Exception:
        return cors(web.json_response({"error": "bad request"}, status=400))
    collab_remove_track(cid, track_id, uid, meta["owner_id"])
    return cors(web.json_response({"ok": True}))

async def api_collab_delete(request):
    uid, _, _ = _tg_user_info(request)
    cid = request.match_info["cid"]
    meta = collab_get_meta(cid)
    if not meta or meta["owner_id"] != uid:
        return cors(web.json_response({"error": "forbidden"}, status=403))
    collab_delete(cid, uid)
    return cors(web.json_response({"ok": True}))
