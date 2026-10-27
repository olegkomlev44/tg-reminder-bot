import os, io, json, hmac, hashlib, aiohttp, asyncio, random, re, urllib.parse, uuid, shutil
from datetime import datetime
from urllib.parse import parse_qsl
from aiohttp import web, WSMsgType
from ig_webapp import setup_ig_webapp_routes
from PIL import Image, ImageDraw, ImageFont
from music_engine import music_engine, add_id3_tags, check_yt_dlp_freshness, yt_dlp_maintenance_loop
from db import (
    init_db, get_cached_file_id, save_cached_file_id,
    save_music_fav, get_music_favs, log_track_history, get_user_history,
    get_total_listen_seconds, save_playlist_track, get_playlists,
    rename_playlist, remove_track_from_playlist, delete_playlist_db,
    remove_music_fav, sync_music_favs, clear_history, DB_PATH,
    save_playback_state, get_playback_state,
    add_dislike, get_blacklist,
    collab_create, collab_get_meta, collab_get_tracks,
    collab_add_track, collab_remove_track, collab_delete,
    upsert_user_profile, get_user_profile, search_users,
    subscribe_artist, unsubscribe_artist, get_subscribed_artists, is_subscribed_artist,
    get_all_subscribed_artists, get_artist_subscribers,
    get_artist_seen_track_ids, mark_artist_tracks_seen,
    send_friend_request, accept_friend_request, get_friends,
    get_friend_requests_incoming, get_friend_status,
    get_friend_favs, get_friend_history,
    add_notification, get_notifications, mark_notifications_read,
    get_unread_notifications_count,
    get_cached_lyrics, save_cached_lyrics, save_cached_lyrics_translation,
)
import logging
import time
from collections import defaultdict, deque


logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
CHUNK = 32 * 1024
INITDATA_MAX_AGE = 24 * 3600


init_db()


if not BOT_TOKEN and not DEV_MODE:
    logger.error(
        "BOT_TOKEN не задан! Проверка initData Telegram невозможна — "
        "все запросы будут отклонены с 401. Установите BOT_TOKEN или DEV_MODE=1 для локальной разработки."
    )




# ═══════════════════════════════════════════════════════════════
# МЕТРИКИ И /health
# ═══════════════════════════════════════════════════════════════
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
    checks["ffmpeg"] = shutil.which("ffmpeg") is not None


    healthy = checks["db"]
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
    verify(request.headers.get("Authorization", ""))
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
    if DEV_MODE and (not init_data or init_data == "test_mode"):
        return {"id": "123456789", "first_name": "DevUser"}


    user = _check_telegram_auth(init_data)
    if not user:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "invalid or missing Telegram auth"}),
                                    content_type="application/json")


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
RATE_LIMIT_RULES = [
    ("/api/stream/", 60, 60),
    ("/api/suggest", 90, 60),
    ("/api/search", 60, 60),
    ("/api/track/", 60, 60),
    ("/api/resolve", 20, 60),
    ("/api/prefetch", 40, 60),
    ("/api/status", 20, 60),
    ("/api/fav/sync", 40, 60),
    ("/api/playlist/import", 6, 60),
    ("/api/send_to_chat", 10, 60),
    ("/api/story_card", 15, 60),
    ("/api/story_upload", 10, 60),
    ("/api/cover_proxy", 90, 60),
    ("/api/wave", 20, 60),
    ("/api/radio", 20, 60),
    ("/api/lyrics/translate", 15, 60),
    ("/api/lyrics", 30, 60),
    ("/api/users/search", 20, 60),
    ("/ig-app/api/", 60, 60),
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
    source = request.rel_url.query.get("source", "all").strip().lower()
    try:
        limit = max(1, min(int(request.rel_url.query.get("limit", 120)), 120))
        offset = max(0, min(int(request.rel_url.query.get("offset", 0)), 500))
    except ValueError:
        limit, offset = 120, 0
    tracks = await music_engine.search_multi(q, limit=limit, offset=offset, source=source)
    resp = web.json_response(tracks)
    yt_status = music_engine.get_yt_status()
    if not tracks and not yt_status["available"] and offset == 0 and source in ("all", "yt"):
        resp.headers["X-Source-Degraded"] = "youtube"
    return cors(resp)


async def api_track_get(request):
    """Мгновенное получение метаданных одного трека по его ID (для ?startapp=play_track_<id>)."""
    track_id = request.match_info.get("track_id", "").strip()
    if not track_id:
        return cors(web.json_response({"error": "missing track_id"}, status=400))
    track = await music_engine.get_track_details(track_id)
    if not track:
        return cors(web.json_response({"error": "not found"}, status=404))
    return cors(web.json_response({
        "id": str(track.get("id", track_id)),
        "title": track.get("title", "Unknown"),
        "artist": track.get("artist", "Unknown"),
        "duration": track.get("duration", ""),
        "artwork_url": track.get("artwork_url", ""),
        "artist_avatar": track.get("artist_avatar", ""),
        "genre": track.get("genre", ""),
        "source": track.get("source", "SoundCloud"),
    }))


async def api_suggest(request):
    q = request.rel_url.query.get("q", "").strip()
    if len(q) < 2: return cors(web.json_response([]))
    return cors(web.json_response(await music_engine.suggest(q, limit=6)))


async def api_resolve(request):
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




# ── Vibe Tuner: пресеты настроения для «Моей Волны» ────────────────────────
_WAVE_VIBE_QUERIES = {
    "energetic": ["energetic workout hits", "upbeat dance pop", "phonk drift", "hype rap bangers",
                  "edm festival", "rock energy", "high tempo electronic", "gym motivation"],
    "calm": ["lofi chill beats", "calm acoustic", "ambient relaxing", "soft indie chill",
             "piano relax", "sleep lofi", "slow jazz night", "dream pop calm"],
}


async def _wave_vibe_search(vibe: str, limit: int, offset: int, bl, known_ids: set):
    pool = _WAVE_VIBE_QUERIES[vibe]
    page = offset // max(limit, 1)
    queries = [pool[(page * 3 + k) % len(pool)] for k in range(3)]
    results = await asyncio.gather(
        *[music_engine.search_multi(q, limit=max(8, limit // 2)) for q in queries],
        return_exceptions=True,
    )
    out, seen = [], set()
    for res in results:
        if not isinstance(res, list):
            continue
        for t in res:
            tid = str(t.get("id", ""))
            if tid and tid not in seen and tid not in bl:
                seen.add(tid)
                out.append(t)
    random.shuffle(out)
    return out[:limit]


def _wave_familiar(uid, favs: list, history: list, limit: int, offset: int, bl):
    pool, seen = [], set()
    for t in list(favs) + list(history):
        tid = str(t.get("id", ""))
        if tid and tid not in seen and tid not in bl:
            seen.add(tid)
            pool.append(t)
    # стабильный порядок в течение дня, чтобы пагинация по offset не повторяла треки
    random.Random(f"{uid}-{datetime.now().strftime('%Y%m%d')}").shuffle(pool)
    return pool[offset:offset + limit]


async def api_wave(request):
    limit = int(request.rel_url.query.get("limit", 120))
    offset = int(request.rel_url.query.get("offset", 0))
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]


    bl = get_blacklist(uid)
    favs = get_music_favs(uid)
    history = get_user_history(uid)

    vibe = (request.rel_url.query.get("vibe") or "").strip().lower()
    known_ids = {str(t.get("id")) for t in (list(favs) + list(history)) if t.get("id")}
    if vibe in _WAVE_VIBE_QUERIES:
        tracks = await _wave_vibe_search(vibe, limit, offset, bl, known_ids)
        return cors(web.json_response(tracks))
    if vibe == "familiar":
        return cors(web.json_response(_wave_familiar(uid, favs, history, limit, offset, bl)))


    unique_tracks = []
    seen = set()


    base_pool = favs + history
    if base_pool and offset == 0:
        seed_pool = favs if favs else history
        seeds = random.sample(seed_pool, min(3, len(seed_pool)))
        lastfm_suggestions = []


        async def lastfm_similar_tracks(seed):
            return await music_engine.get_similar_lastfm(seed["artist"], seed["title"], limit=6)


        async def lastfm_similar_artists(artist: str):
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


        sim_track_tasks = [lastfm_similar_tracks(s) for s in seeds]
        sim_artist_tasks = [lastfm_similar_artists(s["artist"]) for s in seeds]
        all_results = await asyncio.gather(*(sim_track_tasks + sim_artist_tasks), return_exceptions=True)


        for res in all_results:
            if isinstance(res, list):
                lastfm_suggestions.extend(res)


        seen_suggestions = set()
        unique_suggestions = []
        for s in lastfm_suggestions:
            key = f"{s['artist'].lower()}::{s['title'].lower()}"
            if key not in seen_suggestions:
                seen_suggestions.add(key)
                unique_suggestions.append(s)


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


        async def resolve_track(sugg):
            try:
                q = f"{sugg['artist']} {sugg['title']}"
                res = await music_engine.search_multi(q, limit=1)
                return res[0] if res else None
            except Exception:
                return None


        resolve_tasks = [resolve_track(s) for s in unique_suggestions[:15]]
        resolved = await asyncio.gather(*resolve_tasks, return_exceptions=True)


        for t in cf_tracks:
            if t["id"] not in seen and t["id"] not in bl:
                seen.add(t["id"])
                unique_tracks.append(t)


        for t in resolved:
            if isinstance(t, dict) and t.get("id"):
                tid = str(t["id"])
                if tid not in seen and tid not in bl:
                    seen.add(tid)
                    unique_tracks.append(t)


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


    if vibe == "new":
        unique_tracks = [t for t in unique_tracks if str(t.get("id", "")) not in known_ids]
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
    verify(request.headers.get("Authorization", ""))
    result = await music_engine.update_yt_dlp(reason="manual", force=True)
    return cors(web.json_response(result))


async def api_playlist_import(request):
    verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        url = str(body.get("url", ""))[:2000]
    except Exception:
        return cors(web.json_response({"error": "bad req"}, status=400))
    if not url:
        return cors(web.json_response({"error": "no url"}, status=400))
    result = await music_engine.extract_playlist(url)
    status = 200 if "tracks" in result else 422
    return cors(web.json_response(result, status=status))


async def api_fav_sync(request):
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
    user = verify(request.headers.get("Authorization", ""))
    clear_history(user["id"])
    return cors(web.json_response({"ok": True}))




# ── СИНХРОНИЗАЦИЯ ВОСПРОИЗВЕДЕНИЯ МЕЖДУ УСТРОЙСТВАМИ ────────────────────
async def api_playback_sync(request):
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        track_data = body.get("track_data")
        device_id = str(body.get("device_id", "unknown"))[:64]
        position_sec = float(body.get("position_sec", 0))
        queue_data = body.get("queue")
        if not track_data or not track_data.get("id"):
            return cors(web.json_response({"error": "track_data required"}, status=400))
        if isinstance(queue_data, list) and len(queue_data) > 200:
            queue_data = queue_data[:200]
        save_playback_state(user["id"], device_id, track_data, position_sec, queue_data)
        return cors(web.json_response({"ok": True}))
    except Exception as e:
        logger.warning(f"api_playback_sync: {e}")
        return cors(web.json_response({"error": "bad req"}, status=400))




async def api_playback_state(request):
    user = verify(request.headers.get("Authorization", ""))
    device_id = str(request.rel_url.query.get("device_id", ""))
    state = get_playback_state(user["id"])
    if not state:
        return cors(web.json_response({"state": None}))
    state["is_other_device"] = bool(device_id) and state.get("device_id") != device_id
    return cors(web.json_response({"state": state}))




# ── ДИЗЛАЙКИ, ТЕКСТЫ (С КЭШЕМ SQLITE) И ИИ-ПЕРЕВОД ──────────────────────
async def api_dislike(request):
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        add_dislike(user["id"], body["track_id"])
        return cors(web.json_response({"ok": True}))
    except Exception:
        return cors(web.json_response({"error": "bad req"}, status=400))


async def api_lyrics(request):
    title = request.rel_url.query.get("title", "").strip()
    artist = request.rel_url.query.get("artist", "").strip()
    clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    if not clean_title and not artist:
        return cors(web.json_response({"lyrics": None, "synced": None, "translated": None}))


    # 1. Проверяем локальный кэш SQLite
    cached = get_cached_lyrics(artist, clean_title)
    if cached and (cached.get("plain") or cached.get("synced")):
        return cors(web.json_response({
            "lyrics": cached["plain"] or None,
            "synced": cached["synced"] or None,
            "translated": cached.get("translated") or None,
            "cached": True,
        }))


    # 2. Ищем в LRCLIB
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
                        if isinstance(data, list):
                            data = data[0] if data else {}
                        synced = data.get("syncedLyrics") or ""
                        plain = data.get("plainLyrics") or ""
                        if synced or plain:
                            save_cached_lyrics(artist, clean_title, plain, synced)
                            return cors(web.json_response({
                                "lyrics": plain,
                                "synced": synced,
                                "translated": None,
                            }))
    except Exception as e:
        logger.error(f"Lyrics err: {e}")
    return cors(web.json_response({"lyrics": None, "synced": None, "translated": None}))




async def api_lyrics_translate(request):
    """
    Построчный перевод текста песни на русский язык (с сохранением количества
    строк для синхронного отображения под строками караоке) + кэш в SQLite.
    """
    verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        artist = str(body.get("artist", "")).strip()
        title = str(body.get("title", "")).strip()
        lines = body.get("lines") or []
    except Exception:
        return cors(web.json_response({"error": "bad req"}, status=400))


    clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    if not isinstance(lines, list) or not lines:
        return cors(web.json_response({"error": "no lines"}, status=400))


    cached = get_cached_lyrics(artist, clean_title)
    if cached and cached.get("translated"):
        try:
            tr_list = json.loads(cached["translated"])
            if isinstance(tr_list, list) and len(tr_list) == len(lines):
                return cors(web.json_response({"translated": tr_list, "cached": True}))
        except Exception:
            pass


    gemini_key = os.getenv("GEMINI_API_KEY", "")
    translated_lines = []


    if gemini_key:
        try:
            from google import genai
            client = genai.Client(api_key=gemini_key)
            numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(lines[:160]))
            prompt = (
                "Переведи построчно текст песни на русский язык. "
                "Верни ТОЛЬКО валидный JSON-массив строк ровно той же длины, что и входной список "
                "(пустые строки оставь пустыми строками \"\"). Без markdown-обёрток.\n\n"
                f"Входные строки ({len(lines[:160])} шт.):\n{numbered}"
            )
            resp = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            raw = (resp.text or "").strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                translated_lines = [str(x) for x in parsed]
        except Exception as e:
            logger.warning(f"Gemini lyrics translate failed, trying fallback: {e}")


    # Fallback через бесплатный публичный переводчик, если нет GEMINI_API_KEY
    if not translated_lines:
        try:
            joined = "\n".join(line if line.strip() else "---" for line in lines[:120])
            url = "https://translate.googleapis.com/translate_a/single"
            params = {"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": joined}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        full_tr = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
                        raw_lines = full_tr.split("\n")
                        translated_lines = ["" if l.strip() == "---" else l.strip() for l in raw_lines]
        except Exception as e:
            logger.warning(f"Fallback lyrics translate error: {e}")


    if not translated_lines:
        return cors(web.json_response({"error": "translation_unavailable"}, status=503))


    # Подгоняем длину под исходный массив строк
    while len(translated_lines) < len(lines):
        translated_lines.append("")
    translated_lines = translated_lines[:len(lines)]


    save_cached_lyrics_translation(artist, clean_title, json.dumps(translated_lines, ensure_ascii=False))
    return cors(web.json_response({"translated": translated_lines}))




# ── ОТПРАВКА MP3 В ЛИЧКУ БОТА ПРЯМО ИЗ MINI APP ─────────────────────────
async def api_send_to_chat(request):
    """
    Отправляет выбранный трек MP3-файлом пользователю в личный чат с ботом.
    Если file_id уже есть в таблице cache — отправляет мгновенно за 0.1 сек.
    """
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user.get("id", ""))
    if not BOT_TOKEN:
        return cors(web.json_response({"error": "bot_token_missing"}, status=503))


    try:
        body = await request.json()
        track_id = str(body.get("track_id", "")).strip()
    except Exception:
        return cors(web.json_response({"error": "bad req"}, status=400))


    if not track_id:
        return cors(web.json_response({"error": "track_id required"}, status=400))


    track = await music_engine.get_track_details(track_id)
    if not track or not track.get("stream_url"):
        return cors(web.json_response({"error": "stream_not_found"}, status=404))


    cached_fid = get_cached_file_id(track_id)
    tg_api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
    caption = f"🎧 {track.get('artist', '')} — {track.get('title', '')}"


    async with aiohttp.ClientSession() as session:
        # 1. Быстрый путь по закэшированному Telegram file_id
        if cached_fid:
            try:
                async with session.post(tg_api, json={
                    "chat_id": uid,
                    "audio": cached_fid,
                    "caption": caption,
                    "performer": track.get("artist", ""),
                    "title": track.get("title", ""),
                }, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        return cors(web.json_response({"ok": True, "cached": True}))
            except Exception as e:
                logger.warning(f"sendAudio cached file_id failed: {e}")


        # 2. Полная загрузка + вшивание ID3-тегов и обложки
        cover_url = await music_engine.fetch_itunes_cover(track.get("artist", ""), track.get("title", "")) or track.get("artwork_url")
        audio_bytes, cover_bytes = await asyncio.gather(
            music_engine.download_file(track["stream_url"]),
            music_engine.download_file(cover_url) if cover_url else asyncio.sleep(0, result=None),
        )
        if not audio_bytes:
            return cors(web.json_response({"error": "download_failed"}, status=502))


        tagged_bytes = add_id3_tags(audio_bytes, track.get("title", ""), track.get("artist", ""), cover_bytes)
        form = aiohttp.FormData()
        form.add_field("chat_id", uid)
        form.add_field("performer", track.get("artist", ""))
        form.add_field("title", track.get("title", ""))
        form.add_field("caption", caption)
        form.add_field("audio", tagged_bytes, filename=f"{track.get('title', 'track')}.mp3", content_type="audio/mpeg")
        if cover_bytes:
            form.add_field("thumbnail", cover_bytes, filename="cover.jpg", content_type="image/jpeg")


        try:
            async with session.post(tg_api, data=form, timeout=aiohttp.ClientTimeout(total=60)) as r:
                data = await r.json()
                if r.status == 200 and data.get("ok"):
                    fid = (data.get("result") or {}).get("audio", {}).get("file_id")
                    if fid:
                        save_cached_file_id(track_id, fid)
                    return cors(web.json_response({"ok": True, "cached": False}))
                return cors(web.json_response({"error": data.get("description", "tg_error")}, status=502))
        except Exception as e:
            logger.error(f"api_send_to_chat error: {e}")
            return cors(web.json_response({"error": str(e)}, status=502))




# ── ГЕНЕРАЦИЯ ВЕРТИКАЛЬНОЙ КАРТОЧКИ ДЛЯ TELEGRAM STORIES ─────────────────
def _render_story_image(mode: str, user_name: str, favs: list, hist: list, listen_sec: int, track: dict | None) -> bytes:
    W, H = 1080, 1920
    img = Image.new("RGB", (W, H), (12, 10, 24))
    draw = ImageDraw.Draw(img)


    for y in range(H):
        t = y / H
        r = int(48 * (1 - t) + 10 * t)
        g = int(18 * (1 - t) + 10 * t)
        b = int(88 * (1 - t) + 22 * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))


    font_dir = os.path.join(BASE_DIR, "fonts")
    try:
        f_hero = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 68)
        f_big = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 46)
        f_med = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 34)
        f_small = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans.ttf"), 28)
    except Exception:
        f_hero = f_big = f_med = f_small = ImageFont.load_default()


    if mode == "track" and track:
        draw.text((W // 2, 220), "СЕЙЧАС В ПЛЕЕРЕ", font=f_med, fill=(255, 204, 0), anchor="mm")
        draw.rounded_rectangle([(140, 340), (W - 140, 1140)], radius=48, fill=(28, 24, 46), outline=(255, 204, 0), width=3)
        draw.text((W // 2, 740), "🎵", font=f_hero, fill=(255, 204, 0), anchor="mm")
        title_s = (track.get("title") or "Трек")[:36]
        artist_s = (track.get("artist") or "Исполнитель")[:36]
        draw.text((W // 2, 1280), title_s, font=f_big, fill=(255, 255, 255), anchor="mm")
        draw.text((W // 2, 1360), artist_s, font=f_med, fill=(190, 180, 220), anchor="mm")
        draw.text((W // 2, 1460), f"Источник: {track.get('source', 'SoundCloud')}", font=f_small, fill=(140, 130, 170), anchor="mm")
    else:
        total_tracks = len(hist)
        mins = max(1, listen_sec // 60) if listen_sec else total_tracks * 3
        artists = {}
        for t in (hist + favs):
            a = (t.get("artist") or "").strip()
            if a:
                artists[a] = artists.get(a, 0) + 1
        top_artists = sorted(artists.items(), key=lambda x: x[1], reverse=True)[:5]


        draw.text((W // 2, 160), "МОЯ МУЗЫКАЛЬНАЯ АУРА", font=f_big, fill=(255, 204, 0), anchor="mm")
        draw.text((W // 2, 230), f"@{user_name}", font=f_med, fill=(190, 175, 230), anchor="mm")


        draw.rounded_rectangle([(90, 310), (W - 90, 620)], radius=36, fill=(26, 20, 44), outline=(90, 70, 140), width=2)
        draw.text((150, 370), "Прослушано треков", font=f_small, fill=(180, 175, 205))
        draw.text((150, 430), str(total_tracks), font=f_hero, fill=(255, 255, 255))
        draw.text((600, 370), "Время с музыкой", font=f_small, fill=(180, 175, 205))
        draw.text((600, 430), f"{mins} мин", font=f_hero, fill=(255, 204, 0))
        draw.text((150, 540), f"В избранном: {len(favs)} треков", font=f_small, fill=(200, 190, 230))


        draw.text((100, 710), "👑 Топ исполнителей", font=f_big, fill=(255, 255, 255))
        y = 800
        for idx, (art, cnt) in enumerate(top_artists):
            draw.rounded_rectangle([(90, y), (W - 90, y + 110)], radius=24, fill=(24, 20, 40))
            draw.text((135, y + 35), f"#{idx + 1}  {art[:28]}", font=f_med, fill=(255, 255, 255))
            draw.text((W - 135, y + 40), f"{cnt} тр.", font=f_small, fill=(255, 204, 0), anchor="ra")
            y += 135


    draw.text((W // 2, H - 110), f"Telegram Music Mini App • {datetime.now().strftime('%d.%m.%Y')}", font=f_small, fill=(140, 130, 175), anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()




async def api_story_card(request):
    """
    Отдаёт готовую вертикальную JPEG-карточку 1080x1920 для публикации
    в Telegram Stories (tg.shareToStory) или скачивания.
    """
    qs = request.rel_url.query
    mode = qs.get("mode", "wrapped")
    uid = qs.get("uid", "").strip()
    user_name = qs.get("name", "meloman").strip()
    auth_hdr = request.headers.get("Authorization", "")
    if auth_hdr:
        try:
            u = verify(auth_hdr)
            uid = str(u.get("id", uid))
            user_name = u.get("username") or u.get("first_name") or user_name
        except Exception:
            pass


    track = None
    if mode == "track":
        tid = qs.get("track_id", "").strip()
        if tid:
            track = await music_engine.get_track_details(tid)


    favs = get_music_favs(uid) if uid else []
    hist = get_user_history(uid) if uid else []
    listen_sec = get_total_listen_seconds(uid) if uid else 0


    jpg_bytes = await asyncio.to_thread(_render_story_image, mode, user_name, favs, hist, listen_sec, track)
    return cors(web.Response(body=jpg_bytes, content_type="image/jpeg", headers={"Cache-Control": "no-store"}))




# ── Клиентские карточки для Stories ─────────────────────────────────────────
# Браузер рисует карточку 1080×1920 на <canvas> и заливает JPEG сюда, чтобы
# Telegram мог скачать её по публичной ссылке (tg.shareToStory требует URL).
_story_media: dict = {}
_STORY_TTL = 30 * 60
_STORY_MAX_ITEMS = 60


def _story_media_gc():
    now = time.monotonic()
    for k in [k for k, (_, ts) in _story_media.items() if now - ts > _STORY_TTL]:
        _story_media.pop(k, None)
    while len(_story_media) > _STORY_MAX_ITEMS:
        _story_media.pop(next(iter(_story_media)), None)


async def api_story_upload(request):
    verify(request.headers.get("Authorization", ""))
    data = await request.read()
    if len(data) < 1000 or len(data) > 900_000 or data[:3] != b"\xff\xd8\xff":
        return cors(web.json_response({"error": "bad image"}, status=400))
    _story_media_gc()
    token = uuid.uuid4().hex
    _story_media[token] = (data, time.monotonic())
    return cors(web.json_response({"ok": True, "path": f"/api/story_media/{token}.jpg"}))


async def api_story_media(request):
    token = request.match_info.get("name", "").split(".")[0]
    item = _story_media.get(token)
    if not item:
        return cors(web.json_response({"error": "not found"}, status=404))
    return cors(web.Response(body=item[0], content_type="image/jpeg",
                             headers={"Cache-Control": "public, max-age=1800"}))


async def _is_public_host(host: str) -> bool:
    import ipaddress, socket
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        except ValueError:
            return False
    return True


async def api_cover_proxy(request):
    """Отдаёт обложку с CORS, чтобы canvas не «протухал» (taint) при рендере карточек."""
    verify(request.headers.get("Authorization", ""))
    from urllib.parse import urlparse
    url = request.rel_url.query.get("url", "").strip()
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or not await _is_public_host(u.hostname):
        return cors(web.json_response({"error": "bad url"}, status=400))
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if resp.status != 200 or not ctype.startswith("image/"):
                    return cors(web.json_response({"error": "upstream"}, status=502))
                body = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 4_000_000:
                        return cors(web.json_response({"error": "too large"}, status=413))
    except Exception:
        return cors(web.json_response({"error": "fetch failed"}, status=502))
    return cors(web.Response(body=bytes(body), content_type=ctype.split(";")[0],
                             headers={"Cache-Control": "public, max-age=86400"}))


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
    try:
        await request.json()
    except Exception:
        pass
    return cors(web.json_response({"ok": True}))


async def api_stream_track(request):
    tid = request.match_info["track_id"]
    norm_param = request.rel_url.query.get("norm", "auto")
    rng = request.headers.get("Range", "")


    # 1. Если трек УЖЕ нормализован и лежит на диске — отдаём его сразу через
    # FileResponse (он нативно поддерживает Range-запросы и перемотку с 0% CPU).
    cached_path = _normalized_cache_path(tid)
    if norm_param != "0" and os.path.exists(cached_path):
        resp = web.FileResponse(cached_path, headers={"X-Audio-Norm": "cached"})
        return cors(resp)


    # 2. Если файла ещё нет в norm_cache, запускаем фоновую нормализацию для
    # будущих прослушиваний, а прямо сейчас отдаём быстрый passthrough с поддержкой
    # Range, чтобы звук стартовал мгновенно и перемотка не ломала поток.
    if norm_param != "0" and tid not in _prefetch_state and len(_prefetch_state) < 6 and _ID_RE.match(tid):
        _prefetch_state[tid] = "queued"
        asyncio.create_task(_prefetch_track(tid))


    force_live_norm = (norm_param == "1" and not rng)


    for attempt in (0, 1):
        track = await music_engine.get_track_details(tid, fresh=(attempt == 1))
        if not track: return cors(web.Response(status=404, text="Stream not found"))


        if tid.startswith("yt_"):
            source_url = track.get("direct_stream_url")
            extra_headers = track.get("stream_headers") or {}
        else:
            source_url = track.get("stream_url")
            extra_headers = {}
        if not source_url: return cors(web.Response(status=404, text="Stream not found"))


        if force_live_norm:
            try:
                result = await _stream_normalized(request, source_url, tid)
            except Exception as e:
                logger.warning(f"ffmpeg loudnorm failed ({e}), passthrough")
                result = None
            if result is False and attempt == 0:
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
                return resp
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
        entries.sort(key=lambda e: e[0])
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
    if not shutil.which("ffmpeg"):
        return None


    cache_path = _normalized_cache_path(track_id)
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
_prefetch_state: dict[str, str] = {}
_prefetch_sem = asyncio.Semaphore(1)
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")




async def _normalize_to_cache(source_url: str, track_id: str) -> bool:
    cache_path = _normalized_cache_path(track_id)
    tmp_path = f"{cache_path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    cmd = _ffmpeg_loudnorm_cmd(source_url, tmp_path)
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10", *cmd]
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
            track = await music_engine.get_track_details(tid)
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
# REAL-TIME PARTY MODE (WebSocket Jam)
# ═══════════════════════════════════════════════════════════════
_party_rooms: dict[str, dict] = {}
_PARTY_REACTIONS = {"🔥", "❤️", "🚀", "😭", "👏"}




def _get_party_room(room_id: str) -> dict:
    room = _party_rooms.get(room_id)
    if not room:
        room = {
            "id": room_id,
            "clients": set(),
            "members": {},          # uid -> {user_id, name, avatar}
            "host_id": None,
            "now_playing": None,
            "position_sec": 0.0,
            "is_playing": False,
            "updated_at": time.monotonic(),
            "skip_votes": set(),
        }
        _party_rooms[room_id] = room
    return room




def _party_snapshot(room: dict) -> dict:
    elapsed = (time.monotonic() - room["updated_at"]) if room["is_playing"] else 0.0
    pos = max(0.0, room["position_sec"] + elapsed)
    return {
        "type": "room_state",
        "room_id": room["id"],
        "host_id": room["host_id"],
        "now_playing": room["now_playing"],
        "position_sec": round(pos, 2),
        "is_playing": room["is_playing"],
        "members": list(room["members"].values()),
        "skip_votes": list(room["skip_votes"]),
        "skip_needed": max(1, (len(room["members"]) + 1) // 2),
    }




async def _party_broadcast(room: dict, payload: dict):
    dead = []
    msg_str = json.dumps(payload, ensure_ascii=False)
    for ws in list(room["clients"]):
        try:
            await ws.send_str(msg_str)
        except Exception:
            dead.append(ws)
    for ws in dead:
        room["clients"].discard(ws)




async def api_party_ws(request):
    """
    WebSocket-комната для синхронного совместного прослушивания (Party Mode).
    Поддерживает мгновенную синхронизацию play/pause/seek/смены трека и голосования за скип.
    """
    room_id = request.match_info.get("room_id", "").strip()
    auth_qs = request.rel_url.query.get("auth", "") or request.headers.get("Authorization", "")
    try:
        user = verify(auth_qs)
    except Exception:
        user = {"id": "guest_" + uuid.uuid4().hex[:6], "first_name": "Гость"}


    uid = str(user.get("id", "guest"))
    first = user.get("first_name", "")
    last = user.get("last_name", "")
    uname = (first + " " + last).strip() or user.get("username") or "Участник"
    uavatar = user.get("photo_url", "")


    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)


    room = _get_party_room(room_id)
    room["clients"].add(ws)
    room["members"][uid] = {"user_id": uid, "name": uname, "avatar": uavatar}
    if not room["host_id"]:
        room["host_id"] = uid


    await _party_broadcast(room, _party_snapshot(room))


    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            mtype = data.get("type")


            if mtype == "sync_playback":
                # Обновление текущего трека/позиции/паузы
                if data.get("track") is not None:
                    old_id = (room["now_playing"] or {}).get("id")
                    new_id = (data.get("track") or {}).get("id")
                    room["now_playing"] = data.get("track")
                    if old_id != new_id:
                        room["skip_votes"].clear()
                if "position_sec" in data:
                    room["position_sec"] = float(data.get("position_sec") or 0.0)
                if "is_playing" in data:
                    room["is_playing"] = bool(data.get("is_playing"))
                room["updated_at"] = time.monotonic()
                await _party_broadcast(room, {**_party_snapshot(room), "sender_id": uid})


            elif mtype == "vote_skip":
                if uid in room["skip_votes"]:
                    room["skip_votes"].discard(uid)
                else:
                    room["skip_votes"].add(uid)
                needed = max(1, (len(room["members"]) + 1) // 2)
                if len(room["skip_votes"]) >= needed:
                    room["skip_votes"].clear()
                    await _party_broadcast(room, {"type": "skip_triggered", "room_id": room_id})
                else:
                    await _party_broadcast(room, _party_snapshot(room))


            elif mtype == "queue_updated":
                await _party_broadcast(room, {"type": "queue_updated", "room_id": room_id, "sender_id": uid})


            elif mtype == "reaction":
                emoji = str(data.get("emoji", ""))
                if emoji in _PARTY_REACTIONS and not _is_rate_limited(f"react:{room_id}:{uid}", 12, 5):
                    await _party_broadcast(room, {
                        "type": "reaction", "emoji": emoji, "sender_id": uid, "name": uname,
                    })


    finally:
        room["clients"].discard(ws)
        room["members"].pop(uid, None)
        room["skip_votes"].discard(uid)
        if room["host_id"] == uid:
            room["host_id"] = next(iter(room["members"].keys()), None)
        if room["clients"]:
            await _party_broadcast(room, _party_snapshot(room))
        else:
            _party_rooms.pop(room_id, None)


    return ws




# ═══════════════════════════════════════════════════════════════
# ФОНОВЫЙ РАДАР РЕЛИЗОВ (уведомления о новых треках подписок)
# ═══════════════════════════════════════════════════════════════
async def release_radar_loop():
    """
    Каждые 6 часов проверяет свежие треки всех артистов из artist_subscriptions
    и рассылает уведомления 'new_release' подписчикам при появлении новых треков.
    """
    await asyncio.sleep(60)
    while True:
        try:
            artists = get_all_subscribed_artists()
            for artist in artists:
                try:
                    tracks = await music_engine.search_sc(artist, limit=5)
                    artist_l = artist.lower().strip()
                    matched = [t for t in tracks if artist_l in (t.get("artist") or "").lower()]
                    if not matched:
                        continue
                    seen = get_artist_seen_track_ids(artist)
                    current_ids = [str(t["id"]) for t in matched if t.get("id")]
                    if not seen:
                        # Первичная инициализация базы для артиста — запоминаем без спама
                        mark_artist_tracks_seen(artist, current_ids)
                        continue
                    new_tracks = [t for t in matched if str(t.get("id")) not in seen]
                    if new_tracks:
                        mark_artist_tracks_seen(artist, [str(t["id"]) for t in new_tracks])
                        subs = get_artist_subscribers(artist)
                        fresh = new_tracks[0]
                        for uid in subs:
                            add_notification(
                                uid,
                                "new_release",
                                f"🎵 Новый релиз — {artist}",
                                f"Вышел трек «{fresh.get('title', 'Без названия')}»",
                                {
                                    "artist": artist,
                                    "track_id": str(fresh["id"]),
                                    "track_title": fresh.get("title", ""),
                                    "artwork_url": fresh.get("artwork_url", ""),
                                },
                            )
                except Exception as e:
                    logger.debug(f"release_radar artist '{artist}' error: {e}")
                await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"release_radar_loop error: {e}")
        await asyncio.sleep(6 * 3600)




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
        action = body.get("action", "subscribe")
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
    user = verify(request.headers.get("Authorization", ""))
    uid = str(user["id"])
    friend_id = request.match_info["friend_id"]
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




# ═══════════════════════════════════════════════════════════
# КОЛЛАБОРАТИВНЫЕ ПЛЕЙЛИСТЫ — API
# ═══════════════════════════════════════════════════════════


def _tg_user_info(request) -> tuple[str, str, str]:
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
    verify(request.headers.get("Authorization", ""))
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




async def start_web_server():
    app = web.Application(middlewares=[metrics_middleware, cors_middleware, rate_limit_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", api_health)
    app.router.add_get("/api/metrics", api_metrics)
    app.router.add_get("/sw.js", handle_sw)
    app.router.add_get("/api/tracks", api_get_tracks)
    app.router.add_get("/api/track/{track_id}", api_track_get)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/suggest", api_suggest)
    app.router.add_get("/api/resolve", api_resolve)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/prefetch", api_prefetch)
    app.router.add_post("/api/fav/sync", api_fav_sync)
    app.router.add_post("/api/ytdlp/update", api_ytdlp_update)
    app.router.add_post("/api/playlist/import", api_playlist_import)
    app.router.add_post("/api/send_to_chat", api_send_to_chat)
    app.router.add_get("/api/story_card", api_story_card)
    app.router.add_post("/api/story_upload", api_story_upload)
    app.router.add_get("/api/story_media/{name}", api_story_media)
    app.router.add_get("/api/cover_proxy", api_cover_proxy)
    app.router.add_get("/api/wave", api_wave)
    app.router.add_get("/api/radio", api_radio)
    app.router.add_get("/api/lyrics", api_lyrics)
    app.router.add_post("/api/lyrics/translate", api_lyrics_translate)
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
    # Party Mode WebSocket
    app.router.add_get("/api/party/{room_id}/ws", api_party_ws)
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

    # Мини-апп «Лента» Instagram-бота (страница /ig-app, API /ig-app/api/*, превью /ig-app/thumb/*)
    setup_ig_webapp_routes(app)
    app.router.add_options("/{path_info:.*}", lambda r: cors(web.Response()))


    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 Web server on :{port}")


    asyncio.create_task(yt_dlp_maintenance_loop())
    asyncio.create_task(release_radar_loop())