import sqlite3
import random
from db import DB_PATH, get_blacklist
from music_engine import music_engine

async def generate_wave_tracks(user_id: str, limit: int = 5) -> list:
    """Генерирует треки для 'Моей Волны' локально."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 1. Собираем вкусы пользователя (Избранное + История)
    c.execute("""
        SELECT DISTINCT track_id, artist FROM (
            SELECT track_id, artist FROM favorites WHERE user_id = ?
            UNION
            SELECT track_id, artist FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 50
        )
    """, (str(user_id), str(user_id)))
    user_data = c.fetchall()
    
    user_track_ids = {t[0] for t in user_data}
    user_artists = list({t[1] for t in user_data if t[1]})
    
    recs = []
    blacklist = get_blacklist(user_id)
    
    # 2. КОЛЛАБОРАТИВНАЯ ФИЛЬТРАЦИЯ
    # Если юзер уже что-то слушал, ищем пересечения с другими юзерами
    if user_track_ids:
        placeholders = ','.join(['?'] * len(user_track_ids))
        
        # Магия SQL: находим треки, которые лайкали люди с похожим вкусом, 
        # сортируем по частоте совпадений (score)
        query = f"""
            SELECT track_id, title, artist, artwork_url, source, COUNT(*) as score
            FROM favorites
            WHERE user_id IN (
                SELECT DISTINCT user_id FROM favorites 
                WHERE track_id IN ({placeholders}) AND user_id != ?
            )
            AND track_id NOT IN ({placeholders})
            GROUP BY track_id
            ORDER BY score DESC
            LIMIT ?
        """
        params = list(user_track_ids) + [str(user_id), limit]
        c.execute(query, params)
        cf_results = c.fetchall()
        
        for r in cf_results:
            if str(r[0]) not in blacklist:
                recs.append({
                    "id": str(r[0]), "title": r[1], "artist": r[2], 
                    "artwork_url": r[3], "source": r[4]
                })

    conn.close()

    # 3. КОНТЕНТНЫЙ ФОЛЛБЭК
    # Если коллаборативная фильтрация дала мало треков (база еще маленькая)
    need_more = limit - len(recs)
    if need_more > 0:
        if user_artists:
            # Берем случайных любимых артистов юзера
            sample_artists = random.sample(user_artists, min(3, len(user_artists)))
            for artist in sample_artists:
                # Ищем треки этого артиста через music_engine
                search_res = await music_engine.search_multi(artist, limit=3)
                for t in search_res:
                    if str(t['id']) not in user_track_ids and str(t['id']) not in blacklist:
                        # Проверяем, нет ли уже этого трека в рекомендациях
                        if not any(r['id'] == str(t['id']) for r in recs):
                            recs.append(t)
                            if len(recs) >= limit:
                                break
                if len(recs) >= limit:
                    break
        
        # 4. ГЛОБАЛЬНЫЙ ФОЛЛБЭК (Если юзер абсолютно новый)
        if len(recs) < limit:
            charts = await music_engine.get_charts(limit=10)
            for t in charts:
                if str(t['id']) not in blacklist and not any(r['id'] == str(t['id']) for r in recs):
                    recs.append(t)
                    if len(recs) >= limit:
                        break

    random.shuffle(recs) # Слегка перемешиваем, чтобы волна не была предсказуемой
    return recs[:limit]
