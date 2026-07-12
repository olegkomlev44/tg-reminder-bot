import os, json, hmac, hashlib, aiohttp
from urllib.parse import parse_qsl
from aiohttp import web
from music_engine import music_engine
from db import (get_music_favs, get_user_history, get_playlists,
                get_user_queue, save_music_fav)
import logging

logger = logging.getLogger(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHUNK = 32 * 1024


def verify(init_data: str):
    try:
        d = dict(parse_qsl(init_data))
        h = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        sk = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if hmac.new(sk, s.encode(), hashlib.sha256).hexdigest() == h:
            return json.loads(d["user"])
    except Exception:
        pass
    return None


def cors(r):
    r.headers.update({
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization, Range, Content-Type",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    })
    return r


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


async def api_wave(request):
    """Главный экран — бесконечная волна: чарты + перемешка"""
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
    app.router.add_post("/api/fav", api_fav_add)
    app.router.add_get("/api/stream/{track_id}", api_stream_track)
    app.router.add_get("/api/track/{track_id}", api_track_info)
    app.router.add_options("/{path_info:.*}", lambda r: cors(web.Response()))

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 Web server on :{port}")
