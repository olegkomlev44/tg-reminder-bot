import os
import json
import hmac
import hashlib
from urllib.parse import parse_qsl
from aiohttp import web
from music_engine import music_engine
from db import get_music_favs, get_user_history
import logging

logger = logging.getLogger(__name__)

# Токен твоего бота для проверки подлинности Web App
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

def verify_telegram_data(init_data: str):
    """Проверяет, что запрос реально пришел из Telegram от конкретного юзера"""
    try:
        parsed_data = dict(parse_qsl(init_data))
        hash_ = parsed_data.pop('hash')
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calc_hash == hash_:
            return json.loads(parsed_data['user'])
    except Exception:
        pass
    return None

async def handle_index(request):
    """Отдает страницу плеера (index.html)"""
    with open(os.path.join(os.path.dirname(__file__), 'webapp', 'index.html'), 'r', encoding='utf-8') as f:
        return web.Response(text=f.read(), content_type='text/html')

async def api_get_tracks(request):
    """Отдает Избранное и Историю в плеер"""
    init_data = request.headers.get('Authorization', '')
    user = verify_telegram_data(init_data)
    if not user:
        return web.json_response({'error': 'Unauthorized'}, status=401)
    
    user_id = user['id']
    favs = get_music_favs(user_id)
    history = get_user_history(user_id)
    return web.json_response({'favs': favs, 'history': history})

async def api_stream_track(request):
    """Редиректит плеер на прямой аудио-поток трека"""
    track_id = request.match_info['track_id']
    track = await music_engine.get_track_details(track_id)
    if track and track.get('stream_url'):
        # Возвращаем 302 редирект на прямой MP3-поток
        return web.HTTPFound(track['stream_url'])
    return web.Response(status=404, text="Track stream not found")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/api/tracks', api_get_tracks)
    app.router.add_get('/api/stream/{track_id}', api_stream_track)
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Web App сервер запущен на порту {port}")
