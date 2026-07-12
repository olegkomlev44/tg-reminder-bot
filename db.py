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
    # Таблица избранного
    c.execute('''CREATE TABLE IF NOT EXISTS favorites
                 (user_id TEXT, track_id TEXT, title TEXT, artist TEXT)''')
    # Таблица истории
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (user_id TEXT, track_id TEXT, title TEXT, artist TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    # Таблица кэша Telegram
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
    # Проверка на дубликат
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
    # Удаляем старую запись этого же трека, чтобы он поднялся вверх истории
    c.execute("DELETE FROM history WHERE user_id=? AND track_id=?", (str(user_id), str(track_info['id'])))
    c.execute("INSERT INTO history (user_id, track_id, title, artist) VALUES (?, ?, ?, ?)",
              (str(user_id), str(track_info['id']), track_info['title'], track_info['artist']))
    # Оставляем только 30 последних
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
