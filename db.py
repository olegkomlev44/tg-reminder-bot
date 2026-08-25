import sqlite3
import os
import logging

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── PERSISTENT STORAGE ────────────────────────────────────────────────────────
# BotHost монтирует /data как постоянный том (не сбрасывается при редеплое).
# Если /data недоступна (локальная разработка) — падаем обратно на BASE_DIR.
def _resolve_db_path() -> str:
    candidates = [
        os.getenv("DB_DIR"),          # явный override через переменную окружения
        "/data",                       # BotHost persistent volume
        "/app/data",                   # альтернативный BotHost путь
        BASE_DIR,                      # fallback — рядом со скриптами
    ]
    for path in candidates:
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            # Проверяем возможность записи
            test = os.path.join(path, ".write_test")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            logger.info(f"💾 DB будет храниться в: {path}")
            return os.path.join(path, "music.db")
        except Exception:
            continue
    # Абсолютный fallback
    return os.path.join(BASE_DIR, "music.db")

DB_PATH = _resolve_db_path()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favorites (user_id TEXT, track_id TEXT, title TEXT, artist TEXT)''')
    # Подписки на артистов
    c.execute('''CREATE TABLE IF NOT EXISTS artist_subscriptions (
        user_id TEXT NOT NULL,
        artist_name TEXT NOT NULL,
        subscribed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, artist_name)
    )''')
    # Дружба (двусторонняя — запись создаётся при взаимной подписке)
    c.execute('''CREATE TABLE IF NOT EXISTS friend_requests (
        from_user_id TEXT NOT NULL,
        from_user_name TEXT,
        from_user_avatar TEXT,
        to_user_id TEXT NOT NULL,
        status TEXT DEFAULT 'pending',  -- pending | accepted | declined
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(from_user_id, to_user_id)
    )''')
    # Уведомления
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        type TEXT NOT NULL,   -- new_release | friend_request | friend_accepted
        title TEXT,
        body TEXT,
        payload TEXT,   -- JSON: { artist, track_id, track_title, from_user_id, ... }
        is_read INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    # Пользователи (кеш профилей)
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
        user_id TEXT PRIMARY KEY,
        display_name TEXT,
        username TEXT,
        avatar_url TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS history (user_id TEXT, track_id TEXT, title TEXT, artist TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cache (track_id TEXT PRIMARY KEY, file_id TEXT)''')
    
    # Новая таблица: ЧЕРНЫЙ СПИСОК
    c.execute('''CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT, track_id TEXT, UNIQUE(user_id, track_id))''')
    
    # Плейлисты
    c.execute("""CREATE TABLE IF NOT EXISTS playlists (user_id TEXT, name TEXT, track_id TEXT, title TEXT, artist TEXT, source TEXT, UNIQUE(user_id, name, track_id))""")

    # ── АНИМЕ (Anixart) ──────────────────────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS anime_favorites (
        user_id TEXT, release_id INTEGER, title TEXT, poster TEXT,
        added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, release_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS anime_history (
        user_id TEXT, release_id INTEGER, title TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    # Подписки на новые серии (last_episodes — сколько серий было при подписке/последней проверке)
    c.execute('''CREATE TABLE IF NOT EXISTS anime_subscriptions (
        user_id TEXT, release_id INTEGER, title TEXT, last_episodes INTEGER DEFAULT 0,
        subscribed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, release_id)
    )''')
    # Привязанный личный аккаунт Anixart (хранится ТОЛЬКО токен, не пароль)
    c.execute('''CREATE TABLE IF NOT EXISTS anixart_accounts (
        user_id TEXT PRIMARY KEY, anixart_token TEXT, anixart_login TEXT,
        linked_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Миграция старых колонок (если их не было)
    try: c.execute("ALTER TABLE favorites ADD COLUMN artwork_url TEXT")
    except: pass
    try: c.execute("ALTER TABLE favorites ADD COLUMN source TEXT")
    except: pass
    try: c.execute("ALTER TABLE history ADD COLUMN artwork_url TEXT")
    except: pass
    try: c.execute("ALTER TABLE history ADD COLUMN source TEXT")
    except: pass
    try: c.execute("ALTER TABLE playlists ADD COLUMN artwork_url TEXT")
    except: pass
    try: c.execute("ALTER TABLE history ADD COLUMN duration_sec INTEGER DEFAULT 0")
    except: pass

    conn.commit()
    conn.close()
    logger.info("🟢 SQLite БД обновлена (добавлен blacklist)")

# --- БАЗОВЫЕ ФУНКЦИИ ---
def get_cached_file_id(track_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT file_id FROM cache WHERE track_id=?", (str(track_id),))
    res = c.fetchone()
    conn.close()
    return res[0] if res else None

def save_cached_file_id(track_id, file_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO cache (track_id, file_id) VALUES (?, ?)", (str(track_id), str(file_id)))
    conn.commit()
    conn.close()

# --- ИЗБРАННОЕ И ИСТОРИЯ ---
def save_music_fav(user_id, track_info):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM favorites WHERE user_id=? AND track_id=?", (str(user_id), str(track_info['id'])))
    if c.fetchone():
        conn.close()
        return False
    c.execute("INSERT INTO favorites (user_id, track_id, title, artist, artwork_url, source) VALUES (?, ?, ?, ?, ?, ?)",
              (str(user_id), str(track_info['id']), track_info.get('title', ''), track_info.get('artist', ''), track_info.get('artwork_url', ''), track_info.get('source', 'SoundCloud')))
    conn.commit()
    conn.close()
    return True

def remove_music_fav(user_id, track_id):
    """Удалить трек из избранного."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM favorites WHERE user_id=? AND track_id=?", (str(user_id), str(track_id)))
    conn.commit()
    conn.close()
    return True

def get_music_favs(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_id, title, artist, artwork_url, source FROM favorites WHERE user_id=?", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "artist": r[2], "artwork_url": r[3], "source": r[4]} for r in rows]

def log_track_history(user_id, track_info):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    duration_sec = int(track_info.get('duration_sec', 0) or 0)
    c.execute("DELETE FROM history WHERE user_id=? AND track_id=?", (str(user_id), str(track_info['id'])))
    c.execute("INSERT INTO history (user_id, track_id, title, artist, artwork_url, source, duration_sec) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (str(user_id), str(track_info['id']), track_info.get('title', ''), track_info.get('artist', ''), track_info.get('artwork_url', ''), track_info.get('source', 'SoundCloud'), duration_sec))
    c.execute("""DELETE FROM history WHERE user_id=? AND rowid NOT IN (SELECT rowid FROM history WHERE user_id=? ORDER BY timestamp DESC LIMIT 200)""", (str(user_id), str(user_id)))
    conn.commit()
    conn.close()
    
def clear_history(user_id):
    """Полная очистка истории пользователя."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM history WHERE user_id=?", (str(user_id),))
    conn.commit()
    conn.close()

def get_user_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_id, title, artist, artwork_url, source, COALESCE(duration_sec,0) FROM history WHERE user_id=? ORDER BY timestamp DESC", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "artist": r[2], "artwork_url": r[3], "source": r[4], "duration_sec": r[5]} for r in rows]

def get_total_listen_seconds(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(duration_sec),0) FROM history WHERE user_id=?", (str(user_id),))
    res = c.fetchone()
    conn.close()
    return int(res[0]) if res else 0
    
# --- ПЛЕЙЛИСТЫ ---
def get_playlists(user_id) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT name, track_id, title, artist, source, artwork_url FROM playlists WHERE user_id=? ORDER BY name, rowid", (str(user_id),))
        rows = c.fetchall()
    except Exception: rows = []
    conn.close()
    result = {}
    for name, tid, title, artist, source, artwork_url in rows:
        result.setdefault(name, []).append({"id": tid, "title": title, "artist": artist, "source": source or "SoundCloud", "artwork_url": artwork_url})
    return result

def save_playlist_track(user_id, name, track_info):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO playlists (user_id, name, track_id, title, artist, source, artwork_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (str(user_id), name, str(track_info['id']), track_info.get('title', ''), track_info.get('artist', ''), track_info.get('source', 'SoundCloud'), track_info.get('artwork_url', '')))
        conn.commit()
    except Exception: pass
    conn.close()
    return True

def rename_playlist(user_id, old_name, new_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE playlists SET name=? WHERE user_id=? AND name=?", (new_name, str(user_id), old_name))
    conn.commit()
    conn.close()

def remove_track_from_playlist(user_id, playlist_name, track_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM playlists WHERE user_id=? AND name=? AND track_id=?", (str(user_id), playlist_name, str(track_id)))
    conn.commit()
    conn.close()

def delete_playlist_db(user_id, playlist_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM playlists WHERE user_id=? AND name=?", (str(user_id), playlist_name))
    conn.commit()
    conn.close()

# --- BLACKLIST (НОВОЕ) ---
def add_dislike(user_id, track_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blacklist (user_id, track_id) VALUES (?, ?)", (str(user_id), str(track_id)))
    c.execute("DELETE FROM favorites WHERE user_id=? AND track_id=?", (str(user_id), str(track_id)))
    conn.commit()
    conn.close()

def get_blacklist(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_id FROM blacklist WHERE user_id=?", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return {r[0] for r in rows}

# ═══════════════════════════════════════════════════════════════
# ПРОФИЛИ ПОЛЬЗОВАТЕЛЕЙ
# ═══════════════════════════════════════════════════════════════

def upsert_user_profile(user_id: str, display_name: str, username: str, avatar_url: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO user_profiles (user_id, display_name, username, avatar_url, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = excluded.display_name,
            username = excluded.username,
            avatar_url = excluded.avatar_url,
            updated_at = CURRENT_TIMESTAMP
    """, (str(user_id), display_name, username, avatar_url))
    conn.commit(); conn.close()

def get_user_profile(user_id: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT user_id, display_name, username, avatar_url FROM user_profiles WHERE user_id=?",
        (str(user_id),)
    ).fetchone()
    conn.close()
    if not row: return None
    return {"user_id": row[0], "display_name": row[1], "username": row[2], "avatar_url": row[3]}

def search_users(query: str, exclude_user_id: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    q = f"%{query.lower()}%"
    rows = conn.execute("""
        SELECT user_id, display_name, username, avatar_url FROM user_profiles
        WHERE user_id != ? AND (LOWER(display_name) LIKE ? OR LOWER(username) LIKE ?)
        LIMIT 20
    """, (str(exclude_user_id), q, q)).fetchall()
    conn.close()
    return [{"user_id": r[0], "display_name": r[1], "username": r[2], "avatar_url": r[3]} for r in rows]

# ═══════════════════════════════════════════════════════════════
# ПОДПИСКИ НА АРТИСТОВ
# ═══════════════════════════════════════════════════════════════

def subscribe_artist(user_id: str, artist_name: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT OR IGNORE INTO artist_subscriptions (user_id, artist_name) VALUES (?, ?)",
                     (str(user_id), artist_name))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        conn.close(); return False

def unsubscribe_artist(user_id: str, artist_name: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM artist_subscriptions WHERE user_id=? AND artist_name=?",
                 (str(user_id), artist_name))
    conn.commit(); conn.close()
    return True

def get_subscribed_artists(user_id: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT artist_name FROM artist_subscriptions WHERE user_id=? ORDER BY subscribed_at DESC",
        (str(user_id),)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]

def is_subscribed_artist(user_id: str, artist_name: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM artist_subscriptions WHERE user_id=? AND artist_name=?",
        (str(user_id), artist_name)
    ).fetchone()
    conn.close()
    return row is not None

def get_artist_subscribers(artist_name: str) -> list:
    """Все user_id кто подписан на артиста (для рассылки уведомлений)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user_id FROM artist_subscriptions WHERE artist_name=?", (artist_name,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]

# ═══════════════════════════════════════════════════════════════
# ДРУЖБА
# ═══════════════════════════════════════════════════════════════

def send_friend_request(from_id: str, from_name: str, from_avatar: str, to_id: str) -> str:
    """Отправить запрос в друзья. Возвращает 'sent'|'already'|'accepted' (если уже есть встречный запрос)."""
    conn = sqlite3.connect(DB_PATH)
    # Проверяем встречный запрос
    row = conn.execute(
        "SELECT status FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
        (str(to_id), str(from_id))
    ).fetchone()
    if row and row[0] == 'pending':
        # Встречный запрос — сразу принимаем оба
        conn.execute("UPDATE friend_requests SET status='accepted' WHERE from_user_id=? AND to_user_id=?",
                     (str(to_id), str(from_id)))
        conn.execute("""INSERT OR REPLACE INTO friend_requests (from_user_id, from_user_name, from_user_avatar, to_user_id, status)
                        VALUES (?, ?, ?, ?, 'accepted')""",
                     (str(from_id), from_name, from_avatar, str(to_id)))
        conn.commit(); conn.close()
        return 'accepted'
    # Проверяем существующий
    existing = conn.execute(
        "SELECT status FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
        (str(from_id), str(to_id))
    ).fetchone()
    if existing:
        conn.close()
        return 'already'
    conn.execute("""INSERT INTO friend_requests (from_user_id, from_user_name, from_user_avatar, to_user_id)
                    VALUES (?, ?, ?, ?)""",
                 (str(from_id), from_name, from_avatar, str(to_id)))
    conn.commit(); conn.close()
    return 'sent'

def accept_friend_request(from_id: str, to_id: str, to_name: str, to_avatar: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE friend_requests SET status='accepted' WHERE from_user_id=? AND to_user_id=?",
                 (str(from_id), str(to_id)))
    # Создаём встречную запись тоже accepted
    conn.execute("""INSERT OR REPLACE INTO friend_requests (from_user_id, from_user_name, from_user_avatar, to_user_id, status)
                    VALUES (?, ?, ?, ?, 'accepted')""",
                 (str(to_id), to_name, to_avatar, str(from_id)))
    conn.commit(); conn.close()
    return True

def get_friends(user_id: str) -> list:
    """Список принятых друзей (пользователей с двусторонним accepted)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT r.to_user_id, p.display_name, p.username, p.avatar_url
        FROM friend_requests r
        LEFT JOIN user_profiles p ON p.user_id = r.to_user_id
        WHERE r.from_user_id=? AND r.status='accepted'
    """, (str(user_id),)).fetchall()
    conn.close()
    return [{"user_id": r[0], "display_name": r[1] or "Пользователь", "username": r[2], "avatar_url": r[3]} for r in rows]

def get_friend_requests_incoming(user_id: str) -> list:
    """Входящие запросы в друзья (pending)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT r.from_user_id, r.from_user_name, r.from_user_avatar, r.created_at
        FROM friend_requests r
        WHERE r.to_user_id=? AND r.status='pending'
        ORDER BY r.created_at DESC
    """, (str(user_id),)).fetchall()
    conn.close()
    return [{"user_id": r[0], "display_name": r[1] or "Пользователь", "avatar_url": r[2], "created_at": r[3]} for r in rows]

def get_friend_status(user_id: str, other_id: str) -> str:
    """none | pending_sent | pending_received | friends"""
    conn = sqlite3.connect(DB_PATH)
    # Проверяем мою запись к другому
    r1 = conn.execute("SELECT status FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
                      (str(user_id), str(other_id))).fetchone()
    # Проверяем его запись ко мне
    r2 = conn.execute("SELECT status FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
                      (str(other_id), str(user_id))).fetchone()
    conn.close()
    if r1 and r1[0] == 'accepted':
        return 'friends'
    if r1 and r1[0] == 'pending':
        return 'pending_sent'
    if r2 and r2[0] == 'pending':
        return 'pending_received'
    return 'none'

def get_friend_favs(friend_id: str) -> list:
    return get_music_favs(friend_id)

def get_friend_history(friend_id: str) -> list:
    return get_user_history(friend_id)

# ═══════════════════════════════════════════════════════════════
# УВЕДОМЛЕНИЯ
# ═══════════════════════════════════════════════════════════════

def add_notification(user_id: str, ntype: str, title: str, body: str, payload: dict | None = None):
    import json as _json
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""INSERT INTO notifications (user_id, type, title, body, payload)
                    VALUES (?, ?, ?, ?, ?)""",
                 (str(user_id), ntype, title, body, _json.dumps(payload or {})))
    # Оставляем только последние 50 уведомлений на пользователя
    conn.execute("""DELETE FROM notifications WHERE user_id=? AND id NOT IN (
        SELECT id FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50
    )""", (str(user_id), str(user_id)))
    conn.commit(); conn.close()

def get_notifications(user_id: str, limit: int = 30) -> list:
    import json as _json
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, type, title, body, payload, is_read, created_at
        FROM notifications WHERE user_id=?
        ORDER BY created_at DESC LIMIT ?
    """, (str(user_id), limit)).fetchall()
    conn.close()
    result = []
    for r in rows:
        try: payload = _json.loads(r[4] or '{}')
        except: payload = {}
        result.append({
            "id": r[0], "type": r[1], "title": r[2], "body": r[3],
            "payload": payload, "is_read": bool(r[5]), "created_at": r[6]
        })
    return result

def mark_notifications_read(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (str(user_id),))
    conn.commit(); conn.close()

def get_unread_notifications_count(user_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (str(user_id),)).fetchone()
    conn.close()
    return row[0] if row else 0




import secrets

def _init_collab_tables(conn):
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS collab_meta (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            owner_name TEXT,
            owner_avatar TEXT,
            name TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS collab_tracks (
            collab_id TEXT NOT NULL,
            track_id TEXT NOT NULL,
            title TEXT, artist TEXT, artwork_url TEXT, source TEXT,
            added_by TEXT NOT NULL,
            added_by_name TEXT,
            added_by_avatar TEXT,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(collab_id, track_id),
            FOREIGN KEY(collab_id) REFERENCES collab_meta(id)
        )
    """)
    conn.commit()

def collab_create(owner_id: str, owner_name: str, owner_avatar: str, name: str) -> str:
    """Создаёт коллаб-плейлист. Возвращает его короткий ID."""
    cid = secrets.token_urlsafe(8)   # 8 байт = ~11 символов, URL-safe
    conn = sqlite3.connect(DB_PATH)
    _init_collab_tables(conn)
    conn.execute(
        "INSERT INTO collab_meta (id, owner_id, owner_name, owner_avatar, name) VALUES (?,?,?,?,?)",
        (cid, owner_id, owner_name, owner_avatar, name)
    )
    conn.commit(); conn.close()
    return cid

def collab_get_meta(cid: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    _init_collab_tables(conn)
    row = conn.execute(
        "SELECT id, owner_id, owner_name, owner_avatar, name FROM collab_meta WHERE id=?", (cid,)
    ).fetchone()
    conn.close()
    if not row: return None
    return {"id": row[0], "owner_id": row[1], "owner_name": row[2], "owner_avatar": row[3], "name": row[4]}

def collab_get_tracks(cid: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    _init_collab_tables(conn)
    rows = conn.execute("""
        SELECT track_id, title, artist, artwork_url, source,
               added_by, added_by_name, added_by_avatar
        FROM collab_tracks WHERE collab_id=? ORDER BY added_at ASC
    """, (cid,)).fetchall()
    conn.close()
    return [{
        "id": r[0], "title": r[1], "artist": r[2],
        "artwork_url": r[3], "source": r[4] or "SoundCloud",
        "added_by": r[5], "added_by_name": r[6], "added_by_avatar": r[7]
    } for r in rows]

def collab_add_track(cid: str, track: dict, user_id: str, user_name: str, user_avatar: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    _init_collab_tables(conn)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO collab_tracks
                (collab_id, track_id, title, artist, artwork_url, source,
                 added_by, added_by_name, added_by_avatar)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (cid, str(track["id"]), track.get("title"), track.get("artist"),
              track.get("artwork_url"), track.get("source", "SoundCloud"),
              user_id, user_name, user_avatar))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        conn.close()
        return False

def collab_remove_track(cid: str, track_id: str, user_id: str, owner_id: str) -> bool:
    """Удалить трек может тот, кто добавил, или владелец плейлиста."""
    conn = sqlite3.connect(DB_PATH)
    _init_collab_tables(conn)
    conn.execute("""
        DELETE FROM collab_tracks
        WHERE collab_id=? AND track_id=?
          AND (added_by=? OR ?=?)
    """, (cid, track_id, user_id, user_id, owner_id))
    conn.commit(); conn.close()
    return True

def collab_delete(cid: str, owner_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    _init_collab_tables(conn)
    conn.execute("DELETE FROM collab_tracks WHERE collab_id=?", (cid,))
    conn.execute("DELETE FROM collab_meta WHERE id=? AND owner_id=?", (cid, owner_id))
    conn.commit(); conn.close()
    return True


# --- АНИМЕ (Anixart): избранное, история, подписки, привязка аккаунта ---
def save_anime_fav(user_id, release: dict) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM anime_favorites WHERE user_id=? AND release_id=?",
              (str(user_id), int(release["id"])))
    if c.fetchone():
        conn.close()
        return False
    c.execute("INSERT INTO anime_favorites (user_id, release_id, title, poster) VALUES (?, ?, ?, ?)",
              (str(user_id), int(release["id"]), release.get("title", ""), release.get("poster", "")))
    conn.commit(); conn.close()
    return True

def remove_anime_fav(user_id, release_id) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM anime_favorites WHERE user_id=? AND release_id=?",
                 (str(user_id), int(release_id)))
    conn.commit(); conn.close()
    return True

def is_anime_fav(user_id, release_id) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM anime_favorites WHERE user_id=? AND release_id=?",
              (str(user_id), int(release_id)))
    res = c.fetchone(); conn.close()
    return bool(res)

def get_anime_favs(user_id) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT release_id, title, poster FROM anime_favorites WHERE user_id=? ORDER BY added_at DESC",
              (str(user_id),))
    rows = c.fetchall(); conn.close()
    return [{"id": r[0], "title": r[1], "poster": r[2]} for r in rows]

def log_anime_history(user_id, release: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM anime_history WHERE user_id=? AND release_id=?",
              (str(user_id), int(release["id"])))
    c.execute("INSERT INTO anime_history (user_id, release_id, title) VALUES (?, ?, ?)",
              (str(user_id), int(release["id"]), release.get("title", "")))
    c.execute("""DELETE FROM anime_history WHERE user_id=? AND rowid NOT IN
                 (SELECT rowid FROM anime_history WHERE user_id=? ORDER BY timestamp DESC LIMIT 100)""",
              (str(user_id), str(user_id)))
    conn.commit(); conn.close()

def get_anime_history(user_id, limit=20) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT release_id, title, timestamp FROM anime_history WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
              (str(user_id), limit))
    rows = c.fetchall(); conn.close()
    return [{"id": r[0], "title": r[1], "timestamp": r[2]} for r in rows]

def subscribe_anime(user_id, release: dict) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM anime_subscriptions WHERE user_id=? AND release_id=?",
              (str(user_id), int(release["id"])))
    if c.fetchone():
        conn.close()
        return False
    c.execute("INSERT INTO anime_subscriptions (user_id, release_id, title, last_episodes) VALUES (?, ?, ?, ?)",
              (str(user_id), int(release["id"]), release.get("title", ""),
               int(release.get("episodes_released", 0) or 0)))
    conn.commit(); conn.close()
    return True

def unsubscribe_anime(user_id, release_id) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM anime_subscriptions WHERE user_id=? AND release_id=?",
                 (str(user_id), int(release_id)))
    conn.commit(); conn.close()
    return True

def is_anime_subscribed(user_id, release_id) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM anime_subscriptions WHERE user_id=? AND release_id=?",
              (str(user_id), int(release_id)))
    res = c.fetchone(); conn.close()
    return bool(res)

def get_anime_subscriptions(user_id) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT release_id, title, last_episodes FROM anime_subscriptions WHERE user_id=?",
              (str(user_id),))
    rows = c.fetchall(); conn.close()
    return [{"id": r[0], "title": r[1], "last_episodes": r[2]} for r in rows]

def get_all_anime_subscriptions() -> list:
    """Все подписки всех пользователей — используется кроном проверки новых серий."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, release_id, title, last_episodes FROM anime_subscriptions")
    rows = c.fetchall(); conn.close()
    return [{"user_id": r[0], "id": r[1], "title": r[2], "last_episodes": r[3]} for r in rows]

def update_anime_sub_episodes(user_id, release_id, episodes: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE anime_subscriptions SET last_episodes=? WHERE user_id=? AND release_id=?",
                 (int(episodes), str(user_id), int(release_id)))
    conn.commit(); conn.close()

def save_anixart_token(user_id, token: str, login: str):
    """Сохраняет только токен привязанного аккаунта Anixart. Пароль здесь никогда не хранится."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("REPLACE INTO anixart_accounts (user_id, anixart_token, anixart_login) VALUES (?, ?, ?)",
                 (str(user_id), token, login))
    conn.commit(); conn.close()

def get_anixart_token(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT anixart_token, anixart_login FROM anixart_accounts WHERE user_id=?", (str(user_id),))
    res = c.fetchone(); conn.close()
    return {"token": res[0], "login": res[1]} if res else None

def remove_anixart_token(user_id) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM anixart_accounts WHERE user_id=?", (str(user_id),))
    conn.commit(); conn.close()
    return True
