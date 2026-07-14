import sqlite3
import os
import logging

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "music.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favorites (user_id TEXT, track_id TEXT, title TEXT, artist TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS history (user_id TEXT, track_id TEXT, title TEXT, artist TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cache (track_id TEXT PRIMARY KEY, file_id TEXT)''')
    
    # Новая таблица: ЧЕРНЫЙ СПИСОК
    c.execute('''CREATE TABLE IF NOT EXISTS blacklist (user_id TEXT, track_id TEXT, UNIQUE(user_id, track_id))''')
    
    # Плейлисты
    c.execute("""CREATE TABLE IF NOT EXISTS playlists (user_id TEXT, name TEXT, track_id TEXT, title TEXT, artist TEXT, source TEXT, UNIQUE(user_id, name, track_id))""")
    
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
    c.execute("DELETE FROM history WHERE user_id=? AND track_id=?", (str(user_id), str(track_info['id'])))
    c.execute("INSERT INTO history (user_id, track_id, title, artist, artwork_url, source) VALUES (?, ?, ?, ?, ?, ?)",
              (str(user_id), str(track_info['id']), track_info.get('title', ''), track_info.get('artist', ''), track_info.get('artwork_url', ''), track_info.get('source', 'SoundCloud')))
    c.execute("""DELETE FROM history WHERE user_id=? AND rowid NOT IN (SELECT rowid FROM history WHERE user_id=? ORDER BY timestamp DESC LIMIT 30)""", (str(user_id), str(user_id)))
    conn.commit()
    conn.close()

def get_user_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_id, title, artist, artwork_url, source FROM history WHERE user_id=? ORDER BY timestamp DESC", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "artist": r[2], "artwork_url": r[3], "source": r[4]} for r in rows]

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
