import os, json, hmac, hashlib, aiohttp, asyncio, random, urllib.parse
from urllib.parse import parse_qsl
from aiohttp import web
from music_engine import music_engine
from db import (get_music_favs, get_user_history, get_playlists,
                save_music_fav, remove_music_fav, log_track_history, clear_history,
                save_playlist_track,
                rename_playlist, remove_track_from_playlist, delete_playlist_db,
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
            user = json.loads(d["user"])
            # Сохраняем профиль при каждом запросе (кеш актуальных данных)
            try:
                uid = str(user.get("id", ""))
                if uid:
                    first = user.get("first_name", "")
                    last = user.get("last_name", "")
                    name = (first + " " + last).strip() or user.get("username") or "Пользователь"
                    upsert_user_profile(uid, name, user.get("username", ""), user.get("photo_url", ""))
            except Exception:
                pass
            return user
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
    }))

async def api_search(request):
    q = request.rel_url.query.get("q", "").strip()
    if not q: return cors(web.json_response([]))
    limit = int(request.rel_url.query.get("limit", 120))
    tracks = await music_engine.search_multi(q, limit=limit)
    return cors(web.json_response(tracks))

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
        import sqlite3
        from db import DB_PATH
        user_track_ids = {t["id"] for t in base_pool}
        cf_tracks = []
        if len(user_track_ids) >= 1:
            conn = sqlite3.connect(DB_PATH)
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
    except Exception: pass
    return cors(web.json_response({"error": "bad req"}, status=400))

async def api_fav_remove(request):
    """Удалить трек из избранного по track_id."""
    user = verify(request.headers.get("Authorization", ""))
    try:
        body = await request.json()
        track_id = body.get("track_id")
        if track_id:
            remove_music_fav(user["id"], track_id)
            return cors(web.json_response({"ok": True}))
    except Exception: pass
    return cors(web.json_response({"error": "bad req"}, status=400))

async def api_history_clear(request):
    """Полная очистка истории прослушиваний."""
    user = verify(request.headers.get("Authorization", ""))
    clear_history(user["id"])
    return cors(web.json_response({"ok": True}))

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
    if tid.startswith("yt_"): return cors(web.json_response({"error": "YT not supported"}, status=422))
    track = await music_engine.get_track_details(tid)
    if not track or not track.get("stream_url"): return cors(web.Response(status=404, text="Stream not found"))

    # ReplayGain / EBU R128 через ffmpeg — только для первичных запросов (без Range)
    normalize = request.rel_url.query.get("norm", "1") != "0"
    rng = request.headers.get("Range", "")

    if normalize and not rng:
        try:
            result = await _stream_normalized(request, track["stream_url"])
            if result is not None:
                return result
        except Exception as e:
            logger.warning(f"ffmpeg loudnorm failed ({e}), passthrough")

    hdrs = {"User-Agent": "Mozilla/5.0"}
    if rng: hdrs["Range"] = rng
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(track["stream_url"], headers=hdrs) as up:
                ct = up.headers.get("Content-Type", "audio/mpeg")
                rh = {"Content-Type": ct, "Accept-Ranges": "bytes", "Cache-Control": "max-age=3600",
                      "Access-Control-Allow-Origin": "*", "X-Audio-Norm": "passthrough"}
                if cl := up.headers.get("Content-Length"): rh["Content-Length"] = cl
                if cr := up.headers.get("Content-Range"): rh["Content-Range"] = cr
                resp = web.StreamResponse(status=up.status, headers=rh)
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(CHUNK): await resp.write(chunk)
                return resp
    except Exception as e:
        return cors(web.Response(status=502, text=str(e)))


async def _stream_normalized(request, source_url: str):
    """Проксирует аудио через ffmpeg loudnorm EBU R128 (-14 LUFS)."""
    import shutil
    if not shutil.which("ffmpeg"):
        return None  # ffmpeg не установлен — тихий fallback

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", source_url,
        "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=none",
        "-vn",
        "-c:a", "libmp3lame", "-q:a", "2",
        "-f", "mp3", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "audio/mpeg",
        "Cache-Control": "max-age=3600",
        "Access-Control-Allow-Origin": "*",
        "X-Audio-Norm": "loudnorm-r128",
        "Transfer-Encoding": "chunked",
    })
    await resp.prepare(request)
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK)
            if not chunk:
                break
            await resp.write(chunk)
    finally:
        try: proc.kill()
        except Exception: pass
        await proc.wait()
    return resp

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
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/sw.js", handle_sw)
    app.router.add_get("/api/tracks", api_get_tracks)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/wave", api_wave)
    app.router.add_get("/api/lyrics", api_lyrics)
    app.router.add_post("/api/dislike", api_dislike)
    app.router.add_post("/api/fav", api_fav_add)
    app.router.add_post("/api/fav/remove", api_fav_remove)
    app.router.add_post("/api/history", api_history_add)
    app.router.add_post("/api/history/clear", api_history_clear)
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
