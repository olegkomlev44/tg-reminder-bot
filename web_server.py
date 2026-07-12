import os
import json
import hmac
import hashlib
from urllib.parse import parse_qsl
from aiohttp import web, ClientSession
from music_engine import music_engine
from db import get_music_favs, get_user_history, get_playlists, get_user_queue, save_music_fav
import logging

logger = logging.getLogger(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")


def verify_telegram_data(init_data: str):
    try:
        parsed_data = dict(parse_qsl(init_data))
        hash_ = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calc_hash == hash_:
            return json.loads(parsed_data["user"])
    except Exception:
        pass
    return None


def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


async def handle_index(request):
    path = os.path.join(os.path.dirname(__file__), "webapp", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")


async def api_get_tracks(request):
    init_data = request.headers.get("Authorization", "")
    user = verify_telegram_data(init_data)
    if not user:
        return _cors(web.json_response({"error": "Unauthorized"}, status=401))
    uid = user["id"]
    favs = get_music_favs(uid)
    history = get_user_history(uid)
    playlists = get_playlists(uid)       # dict {name: [track,...]}
    queue = get_user_queue(uid)
    return _cors(web.json_response({
        "favs": favs,
        "history": history,
        "playlists": playlists,
        "queue": queue,
    }))


async def api_search(request):
    """Поиск треков прямо из Web App"""
    q = request.rel_url.query.get("q", "").strip()
    if not q:
        return _cors(web.json_response([]))
    tracks = await music_engine.search_multi(q, limit=20)
    return _cors(web.json_response(tracks))


async def api_stream_track(request):
    track_id = request.match_info["track_id"]
    # YouTube треки — редиректим на yt
    if track_id.startswith("yt_"):
        return _cors(web.json_response({"error": "YT stream not supported in browser"}, status=422))
    track = await music_engine.get_track_details(track_id)
    if track and track.get("stream_url"):
        return web.HTTPFound(track["stream_url"])
    return web.Response(status=404, text="Track stream not found")
    
async def api_fav_add(request):
    init_data = request.headers.get("Authorization", "")
    user = verify_telegram_data(init_data)
    if not user:
        return _cors(web.json_response({"error": "Unauthorized"}, status=401))
    
    track_id = request.query.get("id")
    title = request.query.get("title", "Unknown")
    artist = request.query.get("artist", "Unknown")
    
    if track_id:
        save_music_fav(user["id"], {"id": track_id, "title": title, "artist": artist})
        
    return _cors(web.json_response({"status": "ok"}))

async def api_track_info(request):
    """Детали трека (обложка, жанр и т.д.)"""
    track_id = request.match_info["track_id"]
    track = await music_engine.get_track_details(track_id)
    if not track:
        return _cors(web.json_response({"error": "not found"}, status=404))
    return _cors(web.json_response(track))


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/tracks", api_get_tracks)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/stream/{track_id}", api_stream_track)
    app.router.add_get("/api/track/{track_id}", api_track_info)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Web App сервер запущен на порту {port}")
