import os, json, hmac, hashlib, aiohttp, random, urllib.parse
from urllib.parse import parse_qsl
from aiohttp import web
from music_engine import music_engine
from db import (get_music_favs, get_user_history, get_playlists,
                save_music_fav, log_track_history, save_playlist_track, 
                rename_playlist, remove_track_from_playlist, delete_playlist_db,
                init_db, add_dislike, get_blacklist)
import logging

logger = logging.getLogger(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHUNK = 32 * 1024

init_db()

def verify(init_data: str):
    if not init_data or init_data == 'test_mode':
        return {"id": "123456789", "first_name": "TestUser"}
    try:
        d = dict(parse_qsl(init_data))
        if "user" in d:
            return json.loads(d["user"])
    except Exception: pass
    return {"id": "123456789", "first_name": "DevUser"}

def cors(r):
    r.headers.update({
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization, Range, Content-Type",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    })
    return r

async def handle_index(request):
    p = os.path.join(os.path.dirname(__file__), "webapp", "index.html")
    if not os.path.exists(p): p = os.path.join(os.path.dirname(__file__), "index.html")
    return web.Response(text=open(p, encoding="utf-8").read(), content_type="text/html")

async def api_get_tracks(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]
    return cors(web.json_response({
        "favs": get_music_favs(uid),
        "history": get_user_history(uid),
        "playlists": get_playlists(uid),
    }))

async def api_search(request):
    q = request.rel_url.query.get("q", "").strip()
    if not q: return cors(web.json_response([]))
    tracks = await music_engine.search_multi(q, limit=20)
    return cors(web.json_response(tracks))

async def api_wave(request):
    limit = int(request.rel_url.query.get("limit", 20))
    offset = int(request.rel_url.query.get("offset", 0))
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]
    
    bl = get_blacklist(uid)
    favs = get_music_favs(uid)
    history = get_user_history(uid)
    
    # Объединяем вкусы юзера
    base_pool = favs + history
    unique_tracks = []
    seen = set()
    
    # Генерируем умную волну только для первой страницы (offset == 0)
    if base_pool and offset == 0:
        # 1. Выбираем 2 случайных трека из истории/избранного как "сиды"
        seeds = random.sample(base_pool, min(2, len(base_pool)))
        lastfm_suggestions = []
        
        # 2. Получаем похожие названия из Last.fm
        for seed in seeds:
            similars = await music_engine.get_similar_lastfm(seed["artist"], seed["title"], limit=5)
            lastfm_suggestions.extend(similars)
            
        # 3. Асинхронно пробиваем эти названия через наш SoundCloud/YT поисковик
        async def resolve_track(sugg):
            q = f"{sugg['artist']} {sugg['title']}"
            res = await music_engine.search_multi(q, limit=1)
            return res[0] if res else None

        # Запускаем все запросы параллельно!
        tasks = [resolve_track(s) for s in lastfm_suggestions]
        resolved_tracks = await asyncio.gather(*tasks)
        
        # 4. Фильтруем результаты (убираем дубликаты, дизлайки и пустые ответы)
        for t in resolved_tracks:
            if t and t['id'] not in seen and str(t['id']) not in bl:
                seen.add(t['id'])
                unique_tracks.append(t)

    # 5. ФОЛЛБЭК: Если Last.fm ничего не вернул или это подгрузка при скролле
    if len(unique_tracks) < limit:
        charts = await music_engine.get_charts(limit=limit, offset=offset)
        if not charts:
            charts = await music_engine.search_multi(random.choice(["rap hits", "phonk", "pop 2024", "lofi beats"]), limit=limit)
        
        for t in charts:
            if t['id'] not in seen and str(t['id']) not in bl:
                seen.add(t['id'])
                unique_tracks.append(t)
                
    # Перемешиваем, чтобы тренды и рекомендации Last.fm миксовались органично
    random.shuffle(unique_tracks)
    return cors(web.json_response(unique_tracks[:limit]))

async def api_fav_add(request):
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        if body.get("track_data"):
            ok = save_music_fav(user["id"], body["track_data"])
            return cors(web.json_response({"ok": ok}))
    except Exception: pass
    return cors(web.json_response({"error": "bad req"}, status=400))

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
        if body.get("track_data"): log_track_history(user["id"], body["track_data"])
        return cors(web.json_response({"ok": True}))
    except Exception: pass
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

async def api_stream_track(request):
    tid = request.match_info["track_id"]
    if tid.startswith("yt_"): return cors(web.json_response({"error": "YT not supported"}, status=422))
    track = await music_engine.get_track_details(tid)
    if not track or not track.get("stream_url"): return cors(web.Response(status=404, text="Stream not found"))
    rng = request.headers.get("Range", "")
    hdrs = {"User-Agent": "Mozilla/5.0"}
    if rng: hdrs["Range"] = rng
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(track["stream_url"], headers=hdrs) as up:
                ct = up.headers.get("Content-Type", "audio/mpeg")
                rh = {"Content-Type": ct, "Accept-Ranges": "bytes", "Cache-Control": "max-age=3600", "Access-Control-Allow-Origin": "*"}
                if cl := up.headers.get("Content-Length"): rh["Content-Length"] = cl
                if cr := up.headers.get("Content-Range"): rh["Content-Range"] = cr
                resp = web.StreamResponse(status=up.status, headers=rh)
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(CHUNK): await resp.write(chunk)
                return resp
    except Exception as e: return cors(web.Response(status=502, text=str(e)))

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/tracks", api_get_tracks)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/wave", api_wave)
    app.router.add_get("/api/lyrics", api_lyrics)
    app.router.add_post("/api/dislike", api_dislike)
    app.router.add_post("/api/fav", api_fav_add)
    app.router.add_post("/api/history", api_history_add)
    app.router.add_post("/api/playlist/add", api_pl_add)
    app.router.add_post("/api/playlist/rename", api_pl_rename)
    app.router.add_post("/api/playlist/remove_track", api_pl_remove_track)
    app.router.add_post("/api/playlist/delete", api_pl_delete)
    app.router.add_get("/api/stream/{track_id}", api_stream_track)
    app.router.add_options("/{path_info:.*}", lambda r: cors(web.Response()))

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 Web server on :{port}")
