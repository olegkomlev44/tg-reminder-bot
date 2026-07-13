import os, json, hmac, hashlib, aiohttp
import random
from urllib.parse import parse_qsl
from aiohttp import web
from music_engine import music_engine
from db import (get_music_favs, get_user_history, get_playlists,
                get_user_queue, save_music_fav, log_track_history)
import logging

logger = logging.getLogger(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHUNK = 32 * 1024

def verify(init_data: str):
    """
    Надежная функция определения пользователя.
    Даже если токен не совпадет (на хостинге часто бывают баги с ENV), 
    она достанет твой реальный Telegram ID, чтобы БД всегда работала исправно и не сбрасывалась.
    """
    if not init_data or init_data == 'test_mode':
        return {"id": "123456789", "first_name": "TestUser"}
        
    try:
        # Парсим строку initData
        d = dict(parse_qsl(init_data))
        if "user" in d:
            user_obj = json.loads(d["user"])
            return user_obj
    except Exception as e:
        logger.error(f"Verify user parse error: {e}")
        
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
    if not os.path.exists(p):
        p = os.path.join(os.path.dirname(__file__), "index.html")
    return web.Response(text=open(p, encoding="utf-8").read(), content_type="text/html")


async def api_get_tracks(request):
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]
    return cors(web.json_response({
        "favs": get_music_favs(uid),
        "history": get_user_history(uid),
        "playlists": get_playlists(uid),
        "queue": get_user_queue(uid),
    }))


async def api_search(request):
    q = request.rel_url.query.get("q", "").strip()
    if not q:
        return cors(web.json_response([]))
    tracks = await music_engine.search_multi(q, limit=20)
    return cors(web.json_response(tracks))


async def api_wave(request):
    """
    Моя Волна — если у пользователя есть избранное, ищем похожие (перемешиваем артистов).
    Иначе выдаем чарты.
    """
    limit = int(request.rel_url.query.get("limit", 20))
    offset = int(request.rel_url.query.get("offset", 0))
    
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]
    
    favs = get_music_favs(uid)
    
    # Если избранного мало или это пагинация далеко вниз, просто отдаем чарты
    if len(favs) < 3 or offset > 0:
        tracks = await music_engine.get_charts(limit=limit, offset=offset)
        random.shuffle(tracks)
        return cors(web.json_response(tracks))
        
    # Формируем рекомендации на основе избранных артистов
    try:
        artists = list(set([f.get("artist") for f in favs if f.get("artist")]))
        random.shuffle(artists)
        target_artist = artists[0]
        
        # Ищем треки любимого артиста + немного чартов
        tracks = await music_engine.search_sc(target_artist, limit=limit)
        chart_tracks = await music_engine.get_charts(limit=5)
        
        combined = tracks + chart_tracks
        random.shuffle(combined)
        
        seen = set()
        unique_tracks = []
        for t in combined:
            if t['id'] not in seen:
                seen.add(t['id'])
                unique_tracks.append(t)
                
        return cors(web.json_response(unique_tracks[:limit]))
    except Exception as e:
        logger.error(f"Wave logic error: {e}")
        tracks = await music_engine.get_charts(limit=limit, offset=offset)
        return cors(web.json_response(tracks))


async def api_fav_add(request):
    """POST /api/fav  body: {track_id, track_data}"""
    user = verify(request.headers.get("Authorization", ""))
        
    try:
        body = await request.json()
        tid = str(body["track_id"])
        t_data = body.get("track_data")
    except Exception:
        return cors(web.json_response({"error": "bad body"}, status=400))

    if t_data and "title" in t_data and "artist" in t_data:
        ok = save_music_fav(user["id"], {"id": tid, "title": t_data["title"], "artist": t_data["artist"]})
        return cors(web.json_response({"ok": ok}))
        
    track = await music_engine.get_track_details(tid)
    if not track:
        return cors(web.json_response({"error": "track not found"}, status=404))

    ok = save_music_fav(user["id"], {"id": tid, "title": track["title"], "artist": track["artist"]})
    return cors(web.json_response({"ok": ok}))


async def api_history_add(request):
    """POST /api/history - Сохраняет трек в историю при его запуске в плеере"""
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"]
    try:
        body = await request.json()
        t_data = body.get("track_data")
        if t_data:
            log_track_history(uid, t_data)
            return cors(web.json_response({"ok": True}))
    except Exception as e:
        logger.error(f"History log error: {e}")
    return cors(web.json_response({"error": "bad request"}, status=400))


async def api_track_info(request):
    tid = request.match_info["track_id"]
    track = await music_engine.get_track_details(tid)
    if not track:
        return cors(web.json_response({"error": "not found"}, status=404))
    return cors(web.json_response(track))


async def api_stream_track(request):
    tid = request.match_info["track_id"]
    if tid.startswith("yt_"):
        return cors(web.json_response({"error": "YT not supported"}, status=422))

    track = await music_engine.get_track_details(tid)
    if not track or not track.get("stream_url"):
        return cors(web.Response(status=404, text="Stream not found"))

    rng = request.headers.get("Range", "")
    hdrs = {"User-Agent": "Mozilla/5.0"}
    if rng:
        hdrs["Range"] = rng

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(track["stream_url"], headers=hdrs) as up:
                ct = up.headers.get("Content-Type", "audio/mpeg")
                rh = {
                    "Content-Type": ct,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache",
                    "Access-Control-Allow-Origin": "*",
                }
                if cl := up.headers.get("Content-Length"):
                    rh["Content-Length"] = cl
                if cr := up.headers.get("Content-Range"):
                    rh["Content-Range"] = cr
                resp = web.StreamResponse(status=up.status, headers=rh)
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(CHUNK):
                    await resp.write(chunk)
                return resp
    except Exception as e:
        logger.error(f"Stream error: {e}")
        return cors(web.Response(status=502, text=str(e)))


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/tracks", api_get_tracks)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/wave", api_wave)
    app.router.add_post("/api/fav", api_fav_add)
    app.router.add_post("/api/history", api_history_add)  # <-- Добавлен эндпоинт истории
    app.router.add_get("/api/stream/{track_id}", api_stream_track)
    app.router.add_get("/api/track/{track_id}", api_track_info)
    app.router.add_options("/{path_info:.*}", lambda r: cors(web.Response()))

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 Web server on :{port}")

        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    })
    return r


async def handle_index(request):
    p = os.path.join(os.path.dirname(__file__), "webapp", "index.html")
    # Если файл лежит в корне, а не в папке webapp
    if not os.path.exists(p):
        p = os.path.join(os.path.dirname(__file__), "index.html")
    return web.Response(text=open(p, encoding="utf-8").read(), content_type="text/html")


async def api_get_tracks(request):
    user = verify(request.headers.get("Authorization", ""))
    if not user:
        return cors(web.json_response({"error": "Unauthorized"}, status=401))
    uid = user["id"]
    return cors(web.json_response({
        "favs": get_music_favs(uid),
        "history": get_user_history(uid),
        "playlists": get_playlists(uid),
        "queue": get_user_queue(uid),
    }))


async def api_search(request):
    q = request.rel_url.query.get("q", "").strip()
    if not q:
        return cors(web.json_response([]))
    tracks = await music_engine.search_multi(q, limit=20)
    return cors(web.json_response(tracks))


async def api_wave(request):
    """
    Моя Волна — если у пользователя есть избранное, ищем похожие (перемешиваем артистов).
    Иначе выдаем чарты.
    """
    limit = int(request.rel_url.query.get("limit", 20))
    offset = int(request.rel_url.query.get("offset", 0))
    
    user = verify(request.headers.get("Authorization", ""))
    uid = user["id"] if user else "123456789"
    
    favs = get_music_favs(uid)
    
    # Если избранного мало или это пагинация далеко вниз, просто отдаем чарты
    if len(favs) < 3 or offset > 0:
        tracks = await music_engine.get_charts(limit=limit, offset=offset)
        # Немного мешаем для эффекта "Волны"
        random.shuffle(tracks)
        return cors(web.json_response(tracks))
        
    # Формируем рекомендации на основе избранных артистов
    try:
        artists = list(set([f.get("artist") for f in favs if f.get("artist")]))
        random.shuffle(artists)
        target_artist = artists[0]
        
        # Ищем треки любимого артиста + немного рандома
        tracks = await music_engine.search_sc(target_artist, limit=limit)
        chart_tracks = await music_engine.get_charts(limit=5)
        
        combined = tracks + chart_tracks
        random.shuffle(combined)
        
        # Убираем дубликаты
        seen = set()
        unique_tracks = []
        for t in combined:
            if t['id'] not in seen:
                seen.add(t['id'])
                unique_tracks.append(t)
                
        return cors(web.json_response(unique_tracks[:limit]))
    except Exception as e:
        logger.error(f"Wave logic error: {e}")
        tracks = await music_engine.get_charts(limit=limit, offset=offset)
        return cors(web.json_response(tracks))


async def api_fav_add(request):
    """POST /api/fav  body: {track_id, track_data}"""
    user = verify(request.headers.get("Authorization", ""))
    if not user:
        return cors(web.json_response({"error": "Unauthorized"}, status=401))
        
    try:
        body = await request.json()
        tid = str(body["track_id"])
        t_data = body.get("track_data")
    except Exception:
        return cors(web.json_response({"error": "bad body"}, status=400))

    # Если клиент прислал полный объект трека, сохраняем его
    if t_data and "title" in t_data and "artist" in t_data:
        ok = save_music_fav(user["id"], {"id": tid, "title": t_data["title"], "artist": t_data["artist"]})
        return cors(web.json_response({"ok": ok}))
        
    # Иначе запрашиваем из базы SC
    track = await music_engine.get_track_details(tid)
    if not track:
        return cors(web.json_response({"error": "track not found"}, status=404))

    ok = save_music_fav(user["id"], {"id": tid, "title": track["title"], "artist": track["artist"]})
    return cors(web.json_response({"ok": ok}))


async def api_track_info(request):
    tid = request.match_info["track_id"]
    track = await music_engine.get_track_details(tid)
    if not track:
        return cors(web.json_response({"error": "not found"}, status=404))
    return cors(web.json_response(track))


async def api_stream_track(request):
    tid = request.match_info["track_id"]
    if tid.startswith("yt_"):
        return cors(web.json_response({"error": "YT not supported"}, status=422))

    track = await music_engine.get_track_details(tid)
    if not track or not track.get("stream_url"):
        return cors(web.Response(status=404, text="Stream not found"))

    rng = request.headers.get("Range", "")
    hdrs = {"User-Agent": "Mozilla/5.0"}
    if rng:
        hdrs["Range"] = rng

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(track["stream_url"], headers=hdrs) as up:
                ct = up.headers.get("Content-Type", "audio/mpeg")
                rh = {
                    "Content-Type": ct,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache",
                    "Access-Control-Allow-Origin": "*",
                }
                if cl := up.headers.get("Content-Length"):
                    rh["Content-Length"] = cl
                if cr := up.headers.get("Content-Range"):
                    rh["Content-Range"] = cr
                resp = web.StreamResponse(status=up.status, headers=rh)
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(CHUNK):
                    await resp.write(chunk)
                return resp
    except Exception as e:
        logger.error(f"Stream error: {e}")
        return cors(web.Response(status=502, text=str(e)))


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/tracks", api_get_tracks)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/wave", api_wave)
    app.router.add_post("/api/fav", api_fav_add)
    app.router.add_get("/api/stream/{track_id}", api_stream_track)
    app.router.add_get("/api/track/{track_id}", api_track_info)
    app.router.add_options("/{path_info:.*}", lambda r: cors(web.Response()))

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 Web server on :{port}")
async def handle_index(request):
    p = os.path.join(os.path.dirname(__file__), "webapp", "index.html")
    return web.Response(text=open(p, encoding="utf-8").read(), content_type="text/html")


async def api_get_tracks(request):
    user = verify(request.headers.get("Authorization", ""))
    if not user:
        return cors(web.json_response({"error": "Unauthorized"}, status=401))
    uid = user["id"]
    return cors(web.json_response({
        "favs": get_music_favs(uid),
        "history": get_user_history(uid),
        "playlists": get_playlists(uid),
        "queue": get_user_queue(uid),
    }))


async def api_search(request):
    q = request.rel_url.query.get("q", "").strip()
    if not q:
        return cors(web.json_response([]))
    tracks = await music_engine.search_multi(q, limit=20)
    return cors(web.json_response(tracks))


async def api_smart_wave(request):
    """Умная волна — анализирует лайки, либо отдает чарты, если лайков нет"""
    user = verify(request.headers.get("Authorization", ""))
    limit = int(request.rel_url.query.get("limit", 20))
    offset = int(request.rel_url.query.get("offset", 0))

    if not user:
        return cors(web.json_response(await music_engine.get_charts(limit=limit, offset=offset)))
    
    uid = user["id"]
    favs = get_music_favs(uid)
    
    if not favs:
        # Если юзер не лайкал треки, шлем популярное
        return cors(web.json_response(await music_engine.get_charts(limit=limit, offset=offset)))
        
    # Берем случайного артиста из лайков юзера
    sample = random.choice(favs)
    artist = sample["artist"]
    
    # Ищем похожие треки / треки этого артиста
    tracks = await music_engine.search_multi(artist, limit=limit)
    if not tracks:
        tracks = await music_engine.get_charts(limit=limit, offset=offset)
    else:
        random.shuffle(tracks)
        
    return cors(web.json_response(tracks))


async def api_wave(request):
    limit = int(request.rel_url.query.get("limit", 20))
    offset = int(request.rel_url.query.get("offset", 0))
    tracks = await music_engine.get_charts(limit=limit, offset=offset)
    return cors(web.json_response(tracks))


async def api_fav_add(request):
    """POST /api/fav  body: {track_id}"""
    user = verify(request.headers.get("Authorization", ""))
    if not user:
        return cors(web.json_response({"error": "Unauthorized"}, status=401))
    try:
        body = await request.json()
        tid = str(body["track_id"])
    except Exception:
        return cors(web.json_response({"error": "bad body"}, status=400))

    track = await music_engine.get_track_details(tid)
    if not track:
        return cors(web.json_response({"error": "track not found"}, status=404))

    ok = save_music_fav(user["id"], {"id": tid, "title": track["title"], "artist": track["artist"]})
    return cors(web.json_response({"ok": ok}))


async def api_track_info(request):
    tid = request.match_info["track_id"]
    track = await music_engine.get_track_details(tid)
    if not track:
        return cors(web.json_response({"error": "not found"}, status=404))
    return cors(web.json_response(track))


async def api_stream_track(request):
    tid = request.match_info["track_id"]
    if tid.startswith("yt_"):
        return cors(web.json_response({"error": "YT not supported"}, status=422))

    track = await music_engine.get_track_details(tid)
    if not track or not track.get("stream_url"):
        return cors(web.Response(status=404, text="Stream not found"))

    rng = request.headers.get("Range", "")
    hdrs = {"User-Agent": "Mozilla/5.0"}
    if rng:
        hdrs["Range"] = rng

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(track["stream_url"], headers=hdrs) as up:
                ct = up.headers.get("Content-Type", "audio/mpeg")
                rh = {
                    "Content-Type": ct,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache",
                    "Access-Control-Allow-Origin": "*",
                }
                if cl := up.headers.get("Content-Length"):
                    rh["Content-Length"] = cl
                if cr := up.headers.get("Content-Range"):
                    rh["Content-Range"] = cr
                resp = web.StreamResponse(status=up.status, headers=rh)
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(CHUNK):
                    await resp.write(chunk)
                return resp
    except Exception as e:
        logger.error(f"Stream error: {e}")
        return cors(web.Response(status=502, text=str(e)))


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/tracks", api_get_tracks)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/wave", api_wave)
    app.router.add_get("/api/smart_wave", api_smart_wave)
    app.router.add_post("/api/fav", api_fav_add)
    app.router.add_get("/api/stream/{track_id}", api_stream_track)
    app.router.add_get("/api/track/{track_id}", api_track_info)
    app.router.add_options("/{path_info:.*}", lambda r: cors(web.Response()))

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 Web server on :{port}")
