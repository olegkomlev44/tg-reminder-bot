import sqlite3
import os
import logging

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "music.db")

def init_db():
    """Создает таблицы, если их нет"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favorites
                 (user_id TEXT, track_id TEXT, title TEXT, artist TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (user_id TEXT, track_id TEXT, title TEXT, artist TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cache
                 (track_id TEXT PRIMARY KEY, file_id TEXT)''')
    conn.commit()
    conn.close()
    logger.info("🟢 SQLite база данных music.db инициализирована")

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

def save_music_fav(user_id, track_info):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM favorites WHERE user_id=? AND track_id=?", (str(user_id), str(track_info['id'])))
    if c.fetchone():
        conn.close()
        return False
    c.execute("INSERT INTO favorites (user_id, track_id, title, artist) VALUES (?, ?, ?, ?)",
              (str(user_id), str(track_info['id']), track_info['title'], track_info['artist']))
    conn.commit()
    conn.close()
    return True

def get_music_favs(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_id, title, artist FROM favorites WHERE user_id=?", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "artist": r[2]} for r in rows]

def log_track_history(user_id, track_info):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM history WHERE user_id=? AND track_id=?", (str(user_id), str(track_info['id'])))
    c.execute("INSERT INTO history (user_id, track_id, title, artist) VALUES (?, ?, ?, ?)",
              (str(user_id), str(track_info['id']), track_info['title'], track_info['artist']))
    c.execute("""DELETE FROM history WHERE user_id=? AND rowid NOT IN 
                 (SELECT rowid FROM history WHERE user_id=? ORDER BY timestamp DESC LIMIT 30)""", 
              (str(user_id), str(user_id)))
    conn.commit()
    conn.close()

def get_user_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_id, title, artist FROM history WHERE user_id=? ORDER BY timestamp DESC", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "artist": r[2]} for r in rows]


# ── Плейлисты ──────────────────────────────────
def init_db_extended():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS playlists
                 (user_id TEXT, name TEXT, track_id TEXT, title TEXT, artist TEXT, source TEXT,
                  UNIQUE(user_id, name, track_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS queue
                 (user_id TEXT, track_id TEXT, title TEXT, artist TEXT, source TEXT, pos INTEGER)""")
    conn.commit()
    conn.close()

def get_playlists(user_id) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT name, track_id, title, artist, source FROM playlists WHERE user_id=? ORDER BY name, rowid", (str(user_id),))
        rows = c.fetchall()
    except Exception:
        rows = []
    conn.close()
    result: dict = {}
    for name, tid, title, artist, source in rows:
        result.setdefault(name, []).append({"id": tid, "title": title, "artist": artist, "source": source or "SoundCloud"})
    return result

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

def get_user_queue(user_id) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT track_id, title, artist, source FROM queue WHERE user_id=? ORDER BY pos", (str(user_id),))
        rows = c.fetchall()
    except Exception:
        rows = []
    conn.close()
    return [{"id": r[0], "title": r[1], "artist": r[2], "source": r[3] or "SoundCloud"} for r in rows]
