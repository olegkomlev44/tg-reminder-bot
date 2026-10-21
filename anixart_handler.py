"""
anixart_handler.py — Модуль Anixart + Shikimori + AI + Mini App для Telegram-бота (aiogram 3.x)

v6 (15 улучшений):
  1. 🎨 HUD-карточки релизов через Pillow в неоновом Glassmorphism-стиле (интеграция с card_generator.py + встроенный движок: постер, рейтинг, градиентный прогресс-бар серий, студия, жанры).
  2. 📱 Карточка профиля «Anime Wrapped» (1080×1920 для Stories) по команде /anime profile (с поддержкой _render_story_image из web_server.py): дни/часы просмотра, топ жанров, средняя оценка, любимые тайтлы.
  3. 🔗 Хронология франшизы и порядок просмотра (кнопка «🔗 Франшиза» в карточке релиза, дерево связанных сезонов/фильмов с галочками просмотренного ✅).
  4. 🔍 Поиск аниме по скриншоту, гифке или видео-кружочку с подписью /anime (trace.moe API + фоллбэк на мультимодальный gemini-2.5-flash из ai_handlers.py).
  5. 🧠 ИИ-подбор аниме по вайбу (/anime ai <запрос>) через get_gemini() и историю просмотров из БД с инлайн-кнопками перехода к карточкам.
  6. 🤖 Кнопка «🤖 Вердикт ИИ (без спойлеров)» — честный разбор за 3–4 предложения в характере активной персоны чата (get_persona из ai_handlers.py) на основе синопсиса, оценок и отзывов.
  7. 📺 Встроенный Telegram Mini App видеоплеер (/anime/player в web_server.py через WebAppInfo) с выбором озвучки и серии (Kodik / Anixart) и авто-отметкой просмотра.
  8. 🎵 Поиск и скачивание опенингов/эндингов (OP / ED) в 1 клик (связка с music_engine.search_multi + yt-dlp, MP3 с обложкой, ID3-тегами и добавлением в плейлист бота).
  9. 🔄 Двусторонняя синхронизация с Shikimori (OAuth2 через SHIKIMORI_CLIENT_ID и SHIKIMORI_CLIENT_SECRET из config.py): синхронизация статусов («Смотрю», «Просмотрено», «Брошено») и серий.
  10. 🔔 Подписка на конкретную озвучку в check_anime_episodes (AniLibria, StudioBand, DreamCast, Субтитры, Любая) с проверкой раз в 30–40 минут.
  11. 📅 Интерактивный календарь онгоингов на неделю (/anime schedule) по дням ПН–ВС с фильтром «Показывать только мои подписки».
  12. ⚡ Прокачанный Inline-режим (a:): пустой @bot a: выводит «Смотрю сейчас» и «Избранное», @bot a:top — топ сезона, @bot a:random — случайный тайтл.
  13. 🎛 Инлайн-конструктор фильтров («Умный фильтр», /anime filter): мультивыбор жанров + Год + Статус + Рейтинг + Тип с кэшированием в anixart_cache.sqlite3.
  14. 🎰 Чат-рулетка и голосование «Что смотрим?» (/anime party): групповой набор кандидатов или жанра, запуск голосования или прокрутка аниме-рулетки.
  15. 🚀 Фоновый прогрев кэша популярных онгоингов сезона, предзагрузка следующей страницы поиска и единый Rate-Limiter с авто-ретраем для Shikimori и Anixart.
"""

import asyncio
import datetime
import hashlib
import html
import io
import json as _json
import logging
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, InlineQueryResultArticle,
    InlineQueryResultCachedPhoto, InlineQueryResultPhoto,
    InputTextMessageContent, Message, WebAppInfo,
)

import config
from config import (
    ANIXART_CACHE_DB, BASE_DIR,
    SHIKIMORI_CLIENT_ID, SHIKIMORI_CLIENT_SECRET, SHIKIMORI_USER_AGENT,
)
from db import (
    get_cached_file_id, save_cached_file_id,
    save_anime_fav, remove_anime_fav, is_anime_fav, get_anime_favs,
    log_anime_history, get_anime_history,
    subscribe_anime, unsubscribe_anime, is_anime_subscribed, get_anime_subscriptions,
    get_all_anime_subscriptions, update_anime_sub_episodes,
    save_anixart_token, get_anixart_token, remove_anixart_token,
    save_playlist_track, save_music_fav,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.anixart.tv"
SHIKI_BASE_URL = "https://shikimori.one/api"
SHIKI_OAUTH_URL = "https://shikimori.one/oauth"
TRACE_MOE_URL = "https://api.trace.moe/search?anilistInfo"

USER_AGENT = (
    "AnixartApp/9.0 BETA 7-25082901 "
    "(Android 9; SDK 28; x86_64; ROG ASUS AI2201_B; ru)"
)

# URL веб-сервера для Telegram Mini App (/anime/player) и OAuth callback
WEBAPP_BASE_URL = (
    os.getenv("WEBAPP_URL")
    or os.getenv("PUBLIC_URL")
    or os.getenv("BOTHOST_URL")
    or getattr(config, "WEBAPP_URL", "")
    or getattr(config, "PUBLIC_URL", "")
).rstrip("/")

# ── Студии озвучки для подписок (улучшение #10) ───────────────────────────────
VOICEOVER_STUDIOS = {
    "any":        {"label": "⚡ Любая озвучка",   "keywords": []},
    "anilibria":  {"label": "🎙 AniLibria",       "keywords": ["anilibria", "анилибрия"]},
    "studioband": {"label": "🎙 StudioBand",      "keywords": ["studioband", "студийная банда"]},
    "dreamcast":  {"label": "🎙 DreamCast",       "keywords": ["dreamcast", "дримкаст"]},
    "shiza":      {"label": "🎙 SHIZA / AnimeVost", "keywords": ["shiza", "animevost", "шиза", "анимевост"]},
    "subs":       {"label": "💬 Субтитры",        "keywords": ["субтитры", "sub", "crunchyroll", "sovetromantica"]},
}

# Статусы списка пользователя (улучшение #9)
WATCH_STATUSES = {
    "watching":  {"label": "👀 Смотрю",      "shiki": "watching",  "anix_list": 1},
    "completed": {"label": "✅ Просмотрено", "shiki": "completed", "anix_list": 3},
    "planned":   {"label": "📌 В планах",    "shiki": "planned",   "anix_list": 2},
    "on_hold":   {"label": "⏸ Отложено",    "shiki": "on_hold",   "anix_list": 4},
    "dropped":   {"label": "🗑 Брошено",     "shiki": "dropped",   "anix_list": 5},
}

WEEKDAYS_RU = [
    ("ПН", "Понедельник", "monday", 1),
    ("ВТ", "Вторник",     "tuesday", 2),
    ("СР", "Среда",       "wednesday", 3),
    ("ЧТ", "Четверг",     "thursday", 4),
    ("ПТ", "Пятница",     "friday", 5),
    ("СБ", "Суббота",     "saturday", 6),
    ("ВС", "Воскресенье", "sunday", 7),
]


# ── FSM: привязка аккаунта, OAuth Shikimori, AI-поиск и Вечеринка ─────────────
class AnimeStates(StatesGroup):
    waiting_login      = State()
    waiting_password   = State()
    waiting_shiki_code = State()
    waiting_ai_vibe    = State()
    waiting_party_add  = State()


# ── Единый Rate-Limiter с защитой от 429 (улучшение #15) ──────────────────────
class _AsyncRateLimiter:
    """Асинхронный лимитер частоты запросов с семафором параллельности и паузой."""
    def __init__(self, concurrency: int = 4, min_interval: float = 0.22):
        self._sem = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._min_interval = min_interval
        self._last_ts = 0.0

    async def __aenter__(self):
        await self._sem.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_ts = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._sem.release()


_anix_limiter = _AsyncRateLimiter(concurrency=6, min_interval=0.12)
# Shikimori строго ограничивает запросы до 5 rps / 90 rpm
_shiki_limiter = _AsyncRateLimiter(concurrency=3, min_interval=0.35)


# ── Персистентный SQLite-слой в anixart_cache.sqlite3 ─────────────────────────
def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(ANIXART_CACHE_DB, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _cache_db_init_sync():
    conn = _db_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anixart_cache ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anixart_stats ("
            " rid INTEGER PRIMARY KEY, title TEXT, views INTEGER NOT NULL DEFAULT 0,"
            " favs INTEGER NOT NULL DEFAULT 0, last_seen REAL)"
        )
        # Расширенный профиль просмотра пользователя (для Wrapped, франшиз и синхронизации с Shikimori)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anime_user_rates ("
            " user_id TEXT NOT NULL, rid INTEGER NOT NULL, shiki_id INTEGER DEFAULT 0,"
            " title TEXT, status TEXT DEFAULT 'watching', episodes INTEGER DEFAULT 0,"
            " total_episodes INTEGER DEFAULT 0, score INTEGER DEFAULT 0,"
            " genres TEXT DEFAULT '', poster TEXT DEFAULT '', updated_at REAL,"
            " PRIMARY KEY (user_id, rid))"
        )
        # OAuth2 токены Shikimori
        conn.execute(
            "CREATE TABLE IF NOT EXISTS shikimori_tokens ("
            " user_id TEXT PRIMARY KEY, access_token TEXT, refresh_token TEXT,"
            " expires_at REAL, shiki_user_id INTEGER, shiki_nickname TEXT)"
        )
        # Настройки озвучки подписок (улучшение #10)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anime_sub_prefs ("
            " user_id TEXT NOT NULL, rid INTEGER NOT NULL,"
            " studio_key TEXT DEFAULT 'any', last_notified_ep INTEGER DEFAULT 0,"
            " PRIMARY KEY (user_id, rid))"
        )
        # Состояние Умного фильтра пользователя (улучшение #13)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anime_smart_filters ("
            " user_id TEXT PRIMARY KEY, filter_json TEXT NOT NULL)"
        )
        conn.execute("DELETE FROM anixart_cache WHERE expires_at < ?", (time.time(),))
        conn.commit()
    finally:
        conn.close()


def _cache_db_get_sync(key: str):
    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT value, expires_at FROM anixart_cache WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    value, expires_at = row
    if time.time() >= expires_at:
        return None
    try:
        return _json.loads(value)
    except Exception:
        return None


def _cache_db_set_sync(key: str, value, ttl: int):
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT INTO anixart_cache (key, value, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, expires_at = excluded.expires_at",
            (key, _json.dumps(value, ensure_ascii=False), time.time() + ttl),
        )
        conn.commit()
    finally:
        conn.close()


# ── Работа с пользовательским списком, озвучками и Shikimori-токенами ─────────
def _upsert_user_rate_sync(user_id, rel: dict, status: str = None,
                           episodes: int = None, score: int = None, shiki_id: int = None):
    uid = str(user_id)
    rid = int(rel.get("id") or 0)
    if not rid:
        return
    title = rel.get("title_ru") or rel.get("title_original") or rel.get("title") or ""
    ep_tot = int(rel.get("episodes_total") or rel.get("episodes_released") or 0)
    genres_raw = rel.get("genres") or []
    genres_str = ", ".join((g.get("name") if isinstance(g, dict) else str(g)) for g in genres_raw[:8])
    poster = _extract_poster_url(rel) if ("poster" in rel or "image" in rel) else (rel.get("poster") or "")

    conn = _db_conn()
    try:
        cur = conn.execute(
            "SELECT status, episodes, score, shiki_id, genres, poster FROM anime_user_rates WHERE user_id=? AND rid=?",
            (uid, rid)
        ).fetchone()
        st = status or (cur[0] if cur else "watching")
        ep = episodes if episodes is not None else (cur[1] if cur else min(1, ep_tot or 1))
        sc = score if score is not None else (cur[2] if cur else 0)   # личная оценка: 0 = не оценено
        sh_id = shiki_id or (cur[3] if cur else 0)
        g_val = genres_str or (cur[4] if cur else "")
        p_val = poster or (cur[5] if cur else "")
        conn.execute(
            "INSERT INTO anime_user_rates (user_id, rid, shiki_id, title, status, episodes, total_episodes, score, genres, poster, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, rid) DO UPDATE SET "
            "title=COALESCE(NULLIF(excluded.title,''), title), status=excluded.status, "
            "episodes=excluded.episodes, total_episodes=MAX(total_episodes, excluded.total_episodes), "
            "score=excluded.score, shiki_id=CASE WHEN excluded.shiki_id>0 THEN excluded.shiki_id ELSE shiki_id END, "
            "genres=COALESCE(NULLIF(excluded.genres,''), genres), "
            "poster=COALESCE(NULLIF(excluded.poster,''), poster), updated_at=excluded.updated_at",
            (uid, rid, sh_id, title, st, ep, ep_tot, sc, g_val, p_val, time.time())
        )
        conn.commit()
    finally:
        conn.close()


def _get_user_rate_sync(user_id, rid: int) -> Optional[dict]:
    conn = _db_conn()
    try:
        r = conn.execute(
            "SELECT rid, shiki_id, title, status, episodes, total_episodes, score, genres, poster "
            "FROM anime_user_rates WHERE user_id=? AND rid=?",
            (str(user_id), int(rid))
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    return {
        "rid": r[0], "shiki_id": r[1], "title": r[2], "status": r[3],
        "episodes": r[4], "total_episodes": r[5], "score": r[6],
        "genres": r[7], "poster": r[8],
    }


def _get_all_user_rates_sync(user_id) -> list[dict]:
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT rid, shiki_id, title, status, episodes, total_episodes, score, genres, poster "
            "FROM anime_user_rates WHERE user_id=? ORDER BY updated_at DESC",
            (str(user_id),)
        ).fetchall()
    finally:
        conn.close()
    return [{
        "rid": r[0], "shiki_id": r[1], "title": r[2], "status": r[3],
        "episodes": r[4], "total_episodes": r[5], "score": r[6],
        "genres": r[7], "poster": r[8],
    } for r in rows]


def _set_sub_pref_sync(user_id, rid: int, studio_key: str, last_ep: int = 0):
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT INTO anime_sub_prefs (user_id, rid, studio_key, last_notified_ep) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, rid) DO UPDATE SET studio_key=excluded.studio_key, "
            "last_notified_ep=MAX(last_notified_ep, excluded.last_notified_ep)",
            (str(user_id), int(rid), studio_key, int(last_ep))
        )
        conn.commit()
    finally:
        conn.close()


def _get_sub_pref_sync(user_id, rid: int) -> dict:
    conn = _db_conn()
    try:
        r = conn.execute(
            "SELECT studio_key, last_notified_ep FROM anime_sub_prefs WHERE user_id=? AND rid=?",
            (str(user_id), int(rid))
        ).fetchone()
    finally:
        conn.close()
    return {"studio_key": r[0] if r else "any", "last_notified_ep": r[1] if r else 0}


def _save_shiki_token_sync(user_id, access_token: str, refresh_token: str,
                           expires_in: int, shiki_user_id: int = 0, shiki_nickname: str = ""):
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT INTO shikimori_tokens (user_id, access_token, refresh_token, expires_at, shiki_user_id, shiki_nickname) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET access_token=excluded.access_token, "
            "refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, "
            "shiki_user_id=CASE WHEN excluded.shiki_user_id>0 THEN excluded.shiki_user_id ELSE shiki_user_id END, "
            "shiki_nickname=COALESCE(NULLIF(excluded.shiki_nickname,''), shiki_nickname)",
            (str(user_id), access_token, refresh_token, time.time() + int(expires_in or 86400),
             int(shiki_user_id or 0), shiki_nickname or "")
        )
        conn.commit()
    finally:
        conn.close()


def _get_shiki_token_sync(user_id) -> Optional[dict]:
    conn = _db_conn()
    try:
        r = conn.execute(
            "SELECT access_token, refresh_token, expires_at, shiki_user_id, shiki_nickname "
            "FROM shikimori_tokens WHERE user_id=?",
            (str(user_id),)
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    return {
        "access_token": r[0], "refresh_token": r[1], "expires_at": r[2],
        "shiki_user_id": r[3], "shiki_nickname": r[4],
    }


def _delete_shiki_token_sync(user_id):
    conn = _db_conn()
    try:
        conn.execute("DELETE FROM shikimori_tokens WHERE user_id=?", (str(user_id),))
        conn.commit()
    finally:
        conn.close()


def _stats_bump_view_sync(rid: int, title: str):
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT INTO anixart_stats (rid, title, views, favs, last_seen) VALUES (?, ?, 1, 0, ?) "
            "ON CONFLICT(rid) DO UPDATE SET views = views + 1, "
            "title = COALESCE(NULLIF(excluded.title, ''), title), last_seen = excluded.last_seen",
            (rid, title, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _stats_bump_fav_sync(rid: int, title: str, delta: int):
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT INTO anixart_stats (rid, title, views, favs, last_seen) VALUES (?, ?, 0, ?, ?) "
            "ON CONFLICT(rid) DO UPDATE SET favs = MAX(0, favs + ?), "
            "title = COALESCE(NULLIF(excluded.title, ''), title), last_seen = excluded.last_seen",
            (rid, title, max(delta, 0), time.time(), delta),
        )
        conn.commit()
    finally:
        conn.close()


def _stats_top_sync(limit: int = 10) -> list:
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT rid, title, views, favs FROM anixart_stats "
            "ORDER BY (views + favs * 3) DESC, views DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "title": r[1], "views": r[2], "favs": r[3]} for r in rows]


# ── Двухуровневый TTL-кэш (RAM + SQLite) ──────────────────────────────────────
_cache: dict = {}
_cache_lock = asyncio.Lock()
_MEM_TTL_CAP = 300


async def _cache_get(key: str):
    async with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry[1]:
            return entry[0]
    value = await asyncio.to_thread(_cache_db_get_sync, key)
    if value is not None:
        async with _cache_lock:
            _cache[key] = (value, time.time() + _MEM_TTL_CAP)
    return value


async def _cache_set(key: str, value, ttl: int):
    async with _cache_lock:
        _cache[key] = (value, time.time() + min(ttl, _MEM_TTL_CAP))
    await asyncio.to_thread(_cache_db_set_sync, key, value, ttl)


# ── Короткие callback_data ────────────────────────────────────────────────────
# Лимит Telegram на callback_data — 64 БАЙТА, а кириллица в UTF-8 весит 2 байта/символ.
# Поэтому длинные/кириллические запросы кладём в кэш под 8-символьным md5-ключом.
# Память — мгновенно (клавиатуры строятся синхронно), SQLite — чтобы кнопки
# переживали передеплой на BotHost.
from collections import OrderedDict as _OrderedDict

_SQ_MEM: "_OrderedDict[str, object]" = _OrderedDict()
_SQ_MEM_MAX = 4000
_SQ_TTL = 30 * 86400
_SQ_BG: set = set()


def _sq_put(value) -> str:
    raw = _json.dumps(value, ensure_ascii=False, sort_keys=True)
    key = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    fresh = key not in _SQ_MEM
    _SQ_MEM[key] = value
    _SQ_MEM.move_to_end(key)
    while len(_SQ_MEM) > _SQ_MEM_MAX:
        _SQ_MEM.popitem(last=False)
    if fresh:
        try:
            task = asyncio.get_running_loop().create_task(_cache_set(f"sq:{key}", value, _SQ_TTL))
            _SQ_BG.add(task)
            task.add_done_callback(_SQ_BG.discard)
        except RuntimeError:
            pass  # нет запущенного цикла (тесты) — останется только в памяти
    return key


async def _sq_get(key: str):
    if key in _SQ_MEM:
        return _SQ_MEM[key]
    value = await _cache_get(f"sq:{key}")
    if value is not None:
        _SQ_MEM[key] = value
    return value


def _search_cb(query: str, offset: int = 0) -> str:
    """callback_data кнопки «искать/страница». Короткие ASCII-запросы остаются как есть."""
    q = (query or "").strip()
    plain = f"anix:search:{q}:{offset}"
    if q and ":" not in q and len(plain.encode("utf-8")) <= 64:
        return plain
    return f"anix:sq:{_sq_put(q[:120])}:{offset}"


def _go_cb(title: str, alt: str = "") -> str:
    """callback_data кнопки «открыть карточку по названию» (для тайтлов без id Anixart)."""
    return f"anix:go:{_sq_put([(title or '').strip()[:120], (alt or '').strip()[:120]])}"


def _norm_title(t: str) -> str:
    return re.sub(r"[\W_]+", " ", (t or "").lower()).strip()


async def _best_release_match(title: str, alt: str = "") -> tuple[int, float, str]:
    """Ищет в Anixart лучшее совпадение по названию. Возвращает (rid, схожесть 0..1, найденное название)."""
    import difflib
    targets = [_norm_title(x) for x in (title, alt) if x and x.strip()]
    best = (0, 0.0, "")
    first = (0, 0.0, "")
    for q in (title, alt):
        q = (q or "").strip()
        if not q:
            continue
        try:
            items, _ = await _search_releases_cached(q, offset=0, prefetch_next=False)
        except Exception:
            continue
        for it in (items or [])[:6]:
            rid = int(it.get("id") or 0)
            if not rid:
                continue
            names = [it.get("title_ru") or "", it.get("title_original") or ""]
            sc = max((difflib.SequenceMatcher(None, t, _norm_title(n)).ratio()
                      for t in targets for n in names if t and n), default=0.0)
            shown = names[0] or names[1]
            if not first[0]:
                first = (rid, sc, shown)
            if sc > best[1]:
                best = (rid, sc, shown)
        if best[1] >= 0.8:
            break
    if best[1] < 0.35 and first[0]:
        return first
    return best


async def find_release_id_by_title(title: str, alt: str = "") -> int:
    rid, _, _ = await _best_release_match(title, alt)
    return rid


async def _stats_bump_view(rid: int, title: str):
    try:
        await asyncio.to_thread(_stats_bump_view_sync, rid, title or "")
    except Exception as e:
        logger.warning(f"Не удалось обновить статистику просмотров #{rid}: {e}")


async def _stats_bump_fav(rid: int, title: str, delta: int):
    try:
        await asyncio.to_thread(_stats_bump_fav_sync, rid, title or "", delta)
    except Exception as e:
        logger.warning(f"Не удалось обновить статистику лайков #{rid}: {e}")


# ── HTTP-сессия и единый Rate-Limiter с ретраями ──────────────────────────────
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
                )
    return _session


async def close_anixart_session():
    """Вызывать при остановке бота в main.py рядом с bot.session.close()."""
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _headers(token: Optional[str] = None) -> dict:
    h = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if token:
        h["token"] = token
    return h


async def _request(method: str, path: str, *, query: dict = None, body: dict = None,
                   token: str = None, retries: int = 2, timeout: int = 15) -> dict:
    url = BASE_URL + path
    last_exc = None
    for attempt in range(retries + 1):
        async with _anix_limiter:
            session = await _get_session()
            try:
                if method == "GET":
                    ctx = session.get(url, params=query, headers=_headers(token),
                                      timeout=aiohttp.ClientTimeout(total=timeout))
                else:
                    ctx = session.post(url, json=body or {}, headers=_headers(token),
                                       timeout=aiohttp.ClientTimeout(total=timeout))
                async with ctx as r:
                    if r.status == 429 and attempt < retries:
                        retry_after = float(r.headers.get("Retry-After") or (0.8 * (2 ** attempt)))
                        await asyncio.sleep(min(retry_after, 5.0))
                        continue
                    r.raise_for_status()
                    return await r.json()
            except aiohttp.ClientResponseError as e:
                last_exc = e
                if e.status in _RETRYABLE_STATUSES and attempt < retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt < retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable")


async def _api_get(path: str, query: dict = None, token: str = None, retries: int = 2) -> dict:
    return await _request("GET", path, query=query, token=token, retries=retries)


async def _api_post(path: str, body: dict = None, token: str = None, retries: int = 2) -> dict:
    return await _request("POST", path, body=body, token=token, retries=retries)


async def _download_poster(url: str) -> Optional[bytes]:
    if not url:
        return None
    for attempt in range(2):
        try:
            session = await _get_session()
            ua = SHIKIMORI_USER_AGENT if "shikimori" in url else USER_AGENT
            async with session.get(url, headers={"User-Agent": ua},
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.read()
                if r.status in _RETRYABLE_STATUSES and attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                return None
        except Exception:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
    return None


def _img_url(raw: str) -> str:
    if not raw:
        return ""
    if raw.startswith("http"):
        return raw
    if raw.startswith("//"):
        return "https:" + raw
    if "." not in raw:
        raw += ".jpg"
    return f"https://s.anixstatic.org/posters/{raw}"


def _extract_poster_url(rel: dict) -> str:
    for key in ("poster", "image", "image_original", "poster_image", "cover", "picture", "img"):
        val = rel.get(key)
        if not val:
            continue
        if isinstance(val, str):
            return _img_url(val)
        if isinstance(val, dict):
            for subkey in ("original", "preview", "medium", "small", "url", "path", "src", "image"):
                sub = val.get(subkey)
                if isinstance(sub, str) and sub:
                    if sub.startswith("/"):
                        return f"https://shikimori.one{sub}"
                    return _img_url(sub)
    return ""


# ── Shikimori API + OAuth2 синхронизация (улучшения #3, #8, #9, #15) ──────────
def _shiki_headers(access_token: Optional[str] = None) -> dict:
    h = {"User-Agent": SHIKIMORI_USER_AGENT or "NaryadBot"}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


async def _shiki_request(method: str, path: str, *, query: dict = None,
                         body: dict = None, access_token: str = None, retries: int = 2):
    url = path if path.startswith("http") else (SHIKI_BASE_URL + path)
    for attempt in range(retries + 1):
        async with _shiki_limiter:
            try:
                session = await _get_session()
                if method == "GET":
                    ctx = session.get(url, params=query, headers=_shiki_headers(access_token),
                                      timeout=aiohttp.ClientTimeout(total=12))
                elif method == "POST":
                    ctx = session.post(url, json=body or {}, headers=_shiki_headers(access_token),
                                       timeout=aiohttp.ClientTimeout(total=12))
                elif method == "PATCH":
                    ctx = session.patch(url, json=body or {}, headers=_shiki_headers(access_token),
                                        timeout=aiohttp.ClientTimeout(total=12))
                else:
                    ctx = session.delete(url, headers=_shiki_headers(access_token),
                                         timeout=aiohttp.ClientTimeout(total=12))
                async with ctx as r:
                    if r.status == 429 and attempt < retries:
                        retry_after = float(r.headers.get("Retry-After") or (1.5 * (attempt + 1)))
                        await asyncio.sleep(min(retry_after, 6.0))
                        continue
                    if r.status == 204:
                        return {}
                    r.raise_for_status()
                    return await r.json()
            except Exception as e:
                if attempt < retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                logger.warning(f"Shikimori {method} {path}: {e}")
                return None
    return None


async def _shiki_get(path: str, query: dict = None, retries: int = 2):
    return await _shiki_request("GET", path, query=query, retries=retries)


async def _shiki_find_anime(title: str) -> Optional[dict]:
    title = (title or "").strip()
    if not title:
        return None
    key = f"shiki_match:{title.lower()}"
    cached = await _cache_get(key)
    if cached is not None:
        return cached or None
    results = await _shiki_get("/animes", {"search": title, "limit": 1})
    match = (results[0] if isinstance(results, list) and results else {}) or {}
    await _cache_set(key, match, ttl=86400)
    return match or None


async def _shiki_get_full_details(title: str, shiki_id: int = 0) -> Optional[dict]:
    if not shiki_id:
        match = await _shiki_find_anime(title)
        shiki_id = int(match.get("id") or 0) if match else 0
    if not shiki_id:
        return None
    key = f"shiki_full:{shiki_id}"
    cached = await _cache_get(key)
    if cached is not None:
        return cached or None
    data = await _shiki_get(f"/animes/{shiki_id}")
    if isinstance(data, dict) and data.get("id"):
        await _cache_set(key, data, ttl=43200)
        return data
    return None


async def shiki_get_poster_url(title: str) -> Optional[str]:
    anime = await _shiki_find_anime(title)
    if not anime:
        return None
    image = anime.get("image") or {}
    path = image.get("original") or image.get("preview") or image.get("x96")
    if not path:
        return None
    return path if path.startswith("http") else f"https://shikimori.one{path}"


async def shiki_get_trailer_url(title: str) -> Optional[str]:
    anime = await _shiki_find_anime(title)
    aid = anime.get("id") if anime else None
    if not aid:
        return None
    key = f"shiki_videos:{aid}"
    cached = await _cache_get(key)
    videos = cached if cached is not None else (await _shiki_get(f"/animes/{aid}/videos") or [])
    if cached is None and isinstance(videos, list):
        await _cache_set(key, videos, ttl=86400)
    if not isinstance(videos, list) or not videos:
        return None
    for kind in ("pv", "character_trailer", "op_ed_clip"):
        for v in videos:
            if v.get("kind") == kind and v.get("url"):
                return v["url"]
    return videos[0].get("url") if videos else None


def get_shiki_oauth_authorize_url(user_id: int) -> str:
    redirect_uri = (
        f"{WEBAPP_BASE_URL}/anime/shiki/callback"
        if WEBAPP_BASE_URL
        else "urn:ietf:wg:oauth:2.0:oob"
    )
    params = {
        "client_id": SHIKIMORI_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "user_rates+comments",
        "state": str(user_id),
    }
    return f"{SHIKI_OAUTH_URL}/authorize?{urllib.parse.urlencode(params)}"


async def shiki_exchange_code(user_id: int, code: str, redirect_uri: str = None) -> Optional[dict]:
    if not SHIKIMORI_CLIENT_ID or not SHIKIMORI_CLIENT_SECRET:
        return None
    redir = redirect_uri or (
        f"{WEBAPP_BASE_URL}/anime/shiki/callback"
        if WEBAPP_BASE_URL
        else "urn:ietf:wg:oauth:2.0:oob"
    )
    payload = {
        "grant_type": "authorization_code",
        "client_id": SHIKIMORI_CLIENT_ID,
        "client_secret": SHIKIMORI_CLIENT_SECRET,
        "code": code.strip(),
        "redirect_uri": redir,
    }
    data = await _shiki_request("POST", f"{SHIKI_OAUTH_URL}/token", body=payload, retries=1)
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    whoami = await _shiki_request("GET", "/users/whoami", access_token=data["access_token"], retries=1) or {}
    shiki_uid = int(whoami.get("id") or 0)
    nickname = whoami.get("nickname") or ""
    await asyncio.to_thread(
        _save_shiki_token_sync, user_id,
        data["access_token"], data.get("refresh_token", ""),
        int(data.get("expires_in") or 86400), shiki_uid, nickname
    )
    return {"shiki_user_id": shiki_uid, "nickname": nickname}


async def _get_valid_shiki_token(user_id: int) -> Optional[dict]:
    tok = await asyncio.to_thread(_get_shiki_token_sync, user_id)
    if not tok:
        return None
    if time.time() < (tok["expires_at"] or 0) - 120:
        return tok
    if not tok.get("refresh_token") or not SHIKIMORI_CLIENT_ID or not SHIKIMORI_CLIENT_SECRET:
        return tok
    payload = {
        "grant_type": "refresh_token",
        "client_id": SHIKIMORI_CLIENT_ID,
        "client_secret": SHIKIMORI_CLIENT_SECRET,
        "refresh_token": tok["refresh_token"],
    }
    data = await _shiki_request("POST", f"{SHIKI_OAUTH_URL}/token", body=payload, retries=1)
    if isinstance(data, dict) and data.get("access_token"):
        await asyncio.to_thread(
            _save_shiki_token_sync, user_id,
            data["access_token"], data.get("refresh_token") or tok["refresh_token"],
            int(data.get("expires_in") or 86400), tok["shiki_user_id"], tok["shiki_nickname"]
        )
        tok["access_token"] = data["access_token"]
    return tok


async def _shiki_update_existing_rate(tok: dict, shiki_id: int, user_rate: dict) -> Optional[dict]:
    found = await _shiki_request(
        "GET", "/v2/user_rates",
        query={"user_id": tok["shiki_user_id"], "target_id": shiki_id, "target_type": "Anime"},
        access_token=tok["access_token"], retries=1,
    )
    if isinstance(found, list) and found and isinstance(found[0], dict) and found[0].get("id"):
        patch = {k: v for k, v in user_rate.items() if k not in ("user_id", "target_id", "target_type")}
        return await _shiki_request(
            "PATCH", f"/v2/user_rates/{found[0]['id']}",
            body={"user_rate": patch}, access_token=tok["access_token"], retries=1,
        )
    return None


async def sync_rate_to_shikimori_and_anixart(
    user_id: int, rel: dict, status_key: str, episodes: int = None, score: int = None
) -> dict:
    """Синхронизирует статус и серии тайтла между локальной БД, Anixart и Shikimori."""
    rid = int(rel.get("id") or 0)
    title = rel.get("title_ru") or rel.get("title_original") or rel.get("title") or ""
    st_meta = WATCH_STATUSES.get(status_key, WATCH_STATUSES["watching"])

    shiki_anime = await _shiki_find_anime(title)
    shiki_id = int(shiki_anime.get("id") or 0) if shiki_anime else 0

    await asyncio.to_thread(
        _upsert_user_rate_sync, user_id, rel,
        status=status_key, episodes=episodes, score=score, shiki_id=shiki_id
    )
    rate = await asyncio.to_thread(_get_user_rate_sync, user_id, rid) or {}
    ep_val = int(rate.get("episodes") or 0)
    sc_val = int(rate.get("score") or 0)

    synced_shiki = False
    synced_anix = False

    # 1. Синхронизация с Shikimori OAuth2
    tok = await _get_valid_shiki_token(user_id)
    if tok and tok.get("shiki_user_id") and shiki_id:
        body = {
            "user_rate": {
                "user_id": tok["shiki_user_id"],
                "target_id": shiki_id,
                "target_type": "Anime",
                "status": st_meta["shiki"],
                "episodes": ep_val,
            }
        }
        if sc_val > 0 or score is not None:
            body["user_rate"]["score"] = sc_val
        res = await _shiki_request("POST", "/v2/user_rates", body=body, access_token=tok["access_token"], retries=1)
        if not (isinstance(res, dict) and res.get("id")):
            # Запись в списке уже есть (POST вернул 422) — находим её и обновляем через PATCH
            res = await _shiki_update_existing_rate(tok, shiki_id, body["user_rate"])
        if res and isinstance(res, dict) and res.get("id"):
            synced_shiki = True

    # 2. Синхронизация со списком Anixart (если привязан аккаунт)
    anix_acc = get_anixart_token(user_id)
    if anix_acc and anix_acc.get("token") and rid:
        list_id = st_meta["anix_list"]
        try:
            await _api_get(f"/profile/list/add/{list_id}/{rid}", token=anix_acc["token"], retries=1)
            synced_anix = True
        except Exception:
            try:
                await _api_post(f"/profile/list/add/{list_id}/{rid}", {}, token=anix_acc["token"], retries=1)
                synced_anix = True
            except Exception as e:
                logger.debug(f"Anixart list sync #{rid}: {e}")

    return {"shiki": synced_shiki, "anixart": synced_anix, "episodes": ep_val, "status": status_key}


# ── Вход в аккаунт Anixart ────────────────────────────────────────────────────
LOGIN_FAIL_MESSAGES = {
    "invalid_credentials": (
        "❌ Неверный логин или пароль (или Anixart попросил капчу/2FA — "
        "бот их пройти не может)."
    ),
    "rate_limited": "❌ Anixart временно ограничил вход — слишком много попыток. Попробуй через пару минут.",
    "server_error": "❌ Anixart API сейчас недоступен (ошибка на их стороне). Попробуй позже.",
    "unknown": (
        "❌ Не получилось войти. Если логин и пароль верные, проверь логи BotHost по строке «Anixart login»."
    ),
}


async def _anixart_login(login: str, password: str) -> dict:
    attempts = [
        ("/user/login", {"login": login, "password": password}),
        ("/auth/login", {"login": login, "password": password}),
    ]
    reason = "unknown"
    for path, body in attempts:
        try:
            data = await _api_post(path, body, retries=1)
        except aiohttp.ClientResponseError as e:
            if e.status in (401, 403):
                return {"token": None, "reason": "invalid_credentials"}
            if e.status == 429:
                return {"token": None, "reason": "rate_limited"}
            if e.status >= 500:
                reason = "server_error"
            continue
        except Exception:
            continue

        token = (
            data.get("token")
            or (data.get("profile") or {}).get("token")
            or (data.get("profileToken") or {}).get("token")
        )
        if token:
            return {"token": token}
        reason = "invalid_credentials"
    return {"token": None, "reason": reason}


# ── Текстовые виджеты и форматирование ────────────────────────────────────────
def _stars_text(rating) -> str:
    try:
        r = float(rating or 0)
    except (TypeError, ValueError):
        r = 0.0
    n = max(0, min(5, round(r / 2)))
    return "★" * n + "☆" * (5 - n)


def _progress_bar_text(cur, total, width: int = 10) -> str:
    try:
        cur = int(cur or 0)
    except (TypeError, ValueError):
        cur = 0
    try:
        total = int(total) if total else 0
    except (TypeError, ValueError):
        total = 0
    frac = (max(0.0, min(1.0, cur / total)) if total > 0 else (1.0 if cur else 0.0))
    filled = round(frac * width)
    return "▓" * filled + "░" * (width - filled)


def _extract_studio(rel: dict, shiki_details: dict = None) -> str:
    for k in ("studio", "author", "studios"):
        val = rel.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list) and val:
            names = [x.get("name") if isinstance(x, dict) else str(x) for x in val if x]
            if names:
                return ", ".join(names[:2])
    if isinstance(shiki_details, dict):
        studios = shiki_details.get("studios") or []
        names = [s.get("filtered_name") or s.get("name") for s in studios if isinstance(s, dict)]
        names = [n for n in names if n]
        if names:
            return ", ".join(names[:2])
    return "Не указана"


def _extract_genres_list(rel: dict, shiki_details: dict = None) -> list[str]:
    genres_raw = rel.get("genres")
    if isinstance(genres_raw, str) and genres_raw.strip():
        return [g.strip() for g in genres_raw.split(",") if g.strip()][:6]
    if isinstance(genres_raw, list) and genres_raw:
        out = [(g.get("name") if isinstance(g, dict) else str(g)).strip() for g in genres_raw]
        return [g for g in out if g][:6]
    if isinstance(shiki_details, dict):
        s_genres = shiki_details.get("genres") or []
        out = [(g.get("russian") or g.get("name") or "") for g in s_genres if isinstance(g, dict)]
        return [g for g in out if g][:6]
    return []


def _fmt_release(rel: dict, shiki_details: dict = None, user_rate: dict = None) -> str:
    rid        = rel.get("id", 0)
    title_ru   = html.escape(str(rel.get("title_ru") or "Без названия"))
    title_orig = html.escape(str(rel.get("title_original") or ""))
    year       = rel.get("year") or "?"
    status_obj = rel.get("status")
    status     = status_obj.get("name") if isinstance(status_obj, dict) else str(status_obj or "Выходит")
    rating     = rel.get("rating", 0) or (shiki_details or {}).get("score") or 0
    try:
        rating = round(float(rating), 2)
    except Exception:
        rating = 0
    ep_rel     = rel.get("episodes_released", 0) or (shiki_details or {}).get("episodes_aired") or 0
    ep_tot     = rel.get("episodes_total", 0) or (shiki_details or {}).get("episodes") or "?"
    studio     = html.escape(_extract_studio(rel, shiki_details))
    genres_lst = _extract_genres_list(rel, shiki_details)
    genres     = html.escape(", ".join(genres_lst) if genres_lst else "—")
    raw_desc   = (rel.get("description") or (shiki_details or {}).get("description") or "")
    raw_desc   = re.sub(r"\[/?[a-zA-Z0-9_=\s]+\]", "", raw_desc).strip()
    desc       = html.escape(raw_desc[:450] + ("…" if len(raw_desc) > 450 else ""))

    lines = [f"<b>{title_ru}</b>"]
    if title_orig and title_orig != title_ru:
        lines.append(f"<i>{title_orig}</i>")
    lines += [
        "",
        f"📅 <b>Год:</b> {year}  •  🏢 <b>Студия:</b> {studio}",
        f"📊 <b>Статус:</b> {html.escape(status)}",
        f"⭐ <b>Рейтинг:</b> {rating}  {_stars_text(rating)}",
        f"🎬 <b>Серии:</b> {ep_rel} / {ep_tot} эп.  {_progress_bar_text(ep_rel, rel.get('episodes_total'))}",
        f"🏷 <b>Жанры:</b> {genres}",
    ]
    if user_rate:
        st_lbl = WATCH_STATUSES.get(user_rate.get("status"), {}).get("label", "👀 Смотрю")
        u_ep = user_rate.get("episodes", 0)
        lines.append(f"👤 <b>Мой список:</b> {st_lbl} ({u_ep}/{ep_tot} эп.)")
    if desc:
        lines += ["", desc]
    lines += ["", f'🔗 <a href="https://anixart.tv/release/{rid}">Открыть на Anixart</a>']
    return "\n".join(lines)


# ── Улучшение #1: HUD-карточки релизов в неоновом Glassmorphism-стиле ─────────
_FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
_LOCAL_FONT_DIR = os.path.join(BASE_DIR, "fonts")


def _font(size: int, bold: bool = False):
    fname = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in (_LOCAL_FONT_DIR, _FONT_DIR):
        p = os.path.join(d, fname)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


_STATUS_COLORS = {
    "онгоинг": (0, 245, 160), "выходит": (0, 245, 160),
    "завершен": (70, 175, 255), "завершён": (70, 175, 255), "вышел": (70, 175, 255),
    "анонс": (205, 115, 255), "анонсирован": (205, 115, 255),
    "приостановлен": (255, 185, 60), "заморожен": (255, 185, 60),
}
_DEFAULT_STATUS_COLOR = (0, 220, 255)


def _dominant_color(img: Image.Image) -> tuple[int, int, int]:
    small = img.convert("RGB").resize((16, 16))
    pixels = list(small.getdata())
    # Предпочитаем насыщенные пиксели
    best = max(pixels, key=lambda p: (max(p) - min(p)) + (sum(p) // 6))
    return tuple(max(45, min(230, c)) for c in best)


def _wrap_lines(draw, text: str, font, max_width: int, max_lines: int = 2) -> list[str]:
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    consumed = sum(len(l) + 1 for l in lines)
    if len(lines) == max_lines and consumed < len(text or ""):
        last = lines[-1]
        while draw.textlength(last + "…", font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines or [""]


def _rounded_image(img: Image.Image, size: tuple[int, int], radius: int = 28) -> Image.Image:
    w, h = size
    src_w, src_h = img.size
    target_ratio = w / h
    src_ratio = (src_w / src_h) if src_h else target_ratio
    if src_ratio > target_ratio:
        new_h, new_w = src_h, int(src_h * target_ratio)
    else:
        new_w, new_h = src_w, int(src_w / target_ratio)
    left = max(0, (src_w - new_w) // 2)
    top = max(0, (src_h - new_h) // 2)
    cropped = img.crop((left, top, left + new_w, top + new_h)).resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    cropped.putalpha(mask)
    return cropped


def _draw_gradient_bar(canvas: Image.Image, box: tuple[int, int, int, int],
                       frac: float, c1: tuple[int, int, int], c2: tuple[int, int, int]):
    x0, y0, x1, y1 = box
    w = max(1, x1 - x0)
    h = max(1, y1 - y0)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle(box, radius=h // 2, fill=(24, 30, 52, 255), outline=(70, 90, 135, 255), width=1)
    fill_w = int(w * max(0.0, min(1.0, frac)))
    if fill_w <= 0:
        return
    fill_w = max(h, fill_w)
    grad = Image.new("RGBA", (fill_w, h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for x in range(fill_w):
        t = x / max(1, fill_w - 1)
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        gdraw.line([(x, 0), (x, h)], fill=(r, g, b, 255))
    mask = Image.new("L", (fill_w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, fill_w, h), radius=h // 2, fill=255)
    canvas.paste(grad, (x0, y0), mask)


def _render_anime_hud_card_sync(poster_bytes: bytes, rel: dict, shiki_details: dict = None) -> bytes:
    """Генерирует HUD-карточку релиза 1280×720 в едином неоновом Glassmorphism-стиле.
    Если в проекте есть кастомный хук render_anime_hud_card в card_generator.py — использует его."""
    try:
        import card_generator as _cg
        if hasattr(_cg, "render_anime_hud_card"):
            custom = _cg.render_anime_hud_card(poster_bytes, rel, shiki_details)
            if custom:
                return custom
    except Exception:
        pass

    W, H = 1280, 720
    poster_raw = Image.open(io.BytesIO(poster_bytes)).convert("RGB")
    accent = _dominant_color(poster_raw)
    neon_cyan = (0, 235, 255)
    neon_pink = (255, 70, 195)

    # 1. Размытый фон из постера + тёмная виньетка + неоновые орбы
    bg = poster_raw.resize((W, H), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(38))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (10, 12, 22)), 0.72).convert("RGBA")

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse((-120, -120, 560, 560), fill=(*accent, 85))
    gdraw.ellipse((W - 520, H - 480, W + 140, H + 140), fill=(*neon_cyan, 65))
    glow = glow.filter(ImageFilter.GaussianBlur(65))
    bg = Image.alpha_composite(bg, glow)

    # 2. Стеклянная матовая HUD-панель (Glassmorphism)
    glass = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glass)
    pad = 42
    panel_box = (pad, pad, W - pad, H - pad)
    draw.rounded_rectangle(panel_box, radius=36, fill=(18, 22, 36, 175),
                           outline=(255, 255, 255, 58), width=2)
    # Внутренний неоновый акцент сверху панели
    draw.rounded_rectangle((pad + 2, pad + 2, W - pad - 2, pad + 8), radius=4, fill=(*accent, 190))
    bg = Image.alpha_composite(bg, glass)

    # 3. Постер слева с неоновой рамкой
    pw, ph = 380, 548
    px, py = pad + 34, pad + 44
    # Свечение под постером
    p_shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(p_shadow).rounded_rectangle(
        (px - 6, py - 6, px + pw + 6, py + ph + 6), radius=30, fill=(*accent, 95)
    )
    p_shadow = p_shadow.filter(ImageFilter.GaussianBlur(16))
    bg = Image.alpha_composite(bg, p_shadow)

    rounded_poster = _rounded_image(poster_raw, (pw, ph), radius=26)
    bg.paste(rounded_poster, (px, py), rounded_poster)

    draw = ImageDraw.Draw(bg, "RGBA")
    draw.rounded_rectangle((px, py, px + pw, py + ph), radius=26, outline=(255, 255, 255, 90), width=2)

    # 4. Правая HUD-колонка с инфо
    rx = px + pw + 44
    rw = W - pad - 36 - rx
    ry = py + 4

    title_ru = rel.get("title_ru") or rel.get("title_original") or "Без названия"
    title_orig = rel.get("title_original") or ""
    year = rel.get("year") or "?"
    status_obj = rel.get("status")
    status = status_obj.get("name") if isinstance(status_obj, dict) else str(status_obj or "Онгоинг")
    studio = _extract_studio(rel, shiki_details)
    genres = _extract_genres_list(rel, shiki_details)
    ep_rel = int(rel.get("episodes_released") or (shiki_details or {}).get("episodes_aired") or 0)
    ep_tot = int(rel.get("episodes_total") or (shiki_details or {}).get("episodes") or 0)
    try:
        rating = round(float(rel.get("rating") or (shiki_details or {}).get("score") or 0), 2)
    except Exception:
        rating = 0.0

    # Верхний ряд бейджей: Статус + Год + Студия
    st_color = _STATUS_COLORS.get(status.strip().lower(), _DEFAULT_STATUS_COLOR)
    badge_font = _font(21, bold=True)
    st_text = f"● {status.upper()}"
    st_w = int(draw.textlength(st_text, font=badge_font)) + 32
    draw.rounded_rectangle((rx, ry, rx + st_w, ry + 40), radius=20,
                           fill=(16, 42, 46, 255), outline=(*st_color, 255), width=2)
    draw.text((rx + 16, ry + 8), st_text, font=badge_font, fill=(245, 255, 250, 255))

    yr_text = f"{year}  •  {studio[:26]}"
    yr_w = int(draw.textlength(yr_text, font=_font(21))) + 32
    draw.rounded_rectangle((rx + st_w + 14, ry, min(rx + rw, rx + st_w + 14 + yr_w), ry + 40),
                           radius=20, fill=(24, 30, 52, 255), outline=(85, 105, 150, 255), width=1)
    draw.text((rx + st_w + 30, ry + 8), yr_text, font=_font(21), fill=(225, 230, 245, 255))
    ry += 56

    # Заголовок тайтла
    t_font = _font(42, bold=True)
    for line in _wrap_lines(draw, title_ru, t_font, rw, max_lines=2):
        draw.text((rx, ry), line, font=t_font, fill=(255, 255, 255, 255))
        ry += 50
    if title_orig and title_orig.lower() != title_ru.lower():
        orig_line = _wrap_lines(draw, title_orig, _font(22), rw, max_lines=1)[0]
        draw.text((rx, ry + 2), orig_line, font=_font(22), fill=(170, 182, 210, 230))
        ry += 36
    ry += 14

    # HUD-блок рейтинга и студии
    hud_h = 96
    draw.rounded_rectangle((rx, ry, rx + rw, ry + hud_h), radius=22,
                           fill=(12, 16, 28, 165), outline=(70, 88, 130, 255), width=1)
    score_str = f"{rating:.1f}" if rating else "—"
    draw.text((rx + 24, ry + 14), f"★ {score_str}", font=_font(44, bold=True), fill=(255, 210, 70, 255))
    draw.text((rx + 26, ry + 64), "РЕЙТИНГ РЕЛИЗА", font=_font(15, bold=True), fill=(150, 165, 195, 220))

    # Разделитель внутри HUD-блока
    mid_x = rx + 235
    draw.line([(mid_x, ry + 16), (mid_x, ry + hud_h - 16)], fill=(255, 255, 255, 40), width=1)
    draw.text((mid_x + 24, ry + 18), "СТУДИЯ АНИМАЦИИ", font=_font(15, bold=True), fill=(0, 230, 255, 220))
    st_line = _wrap_lines(draw, studio, _font(26, bold=True), rw - 275, max_lines=1)[0]
    draw.text((mid_x + 24, ry + 44), st_line, font=_font(26, bold=True), fill=(245, 248, 255, 255))
    ry += hud_h + 22

    # Градиентный прогресс-бар вышедших серий (улучшение #1)
    ep_label = f"ПРОГРЕСС ВЫХОДА СЕРИЙ"
    ep_count_str = f"{ep_rel} / {ep_tot or '?'} эп."
    draw.text((rx, ry), ep_label, font=_font(17, bold=True), fill=(175, 190, 220, 240))
    cnt_w = draw.textlength(ep_count_str, font=_font(22, bold=True))
    draw.text((rx + rw - cnt_w, ry - 4), ep_count_str, font=_font(22, bold=True), fill=(0, 245, 200, 255))
    ry += 30

    frac = (ep_rel / ep_tot) if ep_tot > 0 else (1.0 if ep_rel > 0 else 0.0)
    _draw_gradient_bar(bg, (rx, ry, rx + rw, ry + 26), frac, neon_cyan, neon_pink)
    ry += 46

    # Стеклянные чипы жанров
    if genres:
        draw.text((rx, ry), "ЖАНРЫ И ТЕГИ", font=_font(16, bold=True), fill=(160, 175, 205, 220))
        ry += 26
        cx, cy = rx, ry
        chip_font = _font(20, bold=True)
        for g in genres[:6]:
            gw = int(draw.textlength(g, font=chip_font)) + 30
            if cx + gw > rx + rw:
                cx = rx
                cy += 44
                if cy + 38 > H - pad - 14:
                    break
            draw.rounded_rectangle((cx, cy, cx + gw, cy + 36), radius=18,
                                   fill=(22, 28, 48, 255), outline=(*accent, 255), width=2)
            draw.text((cx + 15, cy + 6), g, font=chip_font, fill=(240, 245, 255, 255))
            cx += gw + 10

    # Водяной знак HUD внизу справа
    wm = "ANIXART × SHIKIMORI HUD"
    wm_font = _font(15, bold=True)
    wm_w = draw.textlength(wm, font=wm_font)
    draw.text((W - pad - 28 - wm_w, H - pad - 28), wm, font=wm_font, fill=(255, 255, 255, 90))

    buf = io.BytesIO()
    bg.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def _render_anime_card(poster_bytes: bytes, rel: dict, shiki_details: dict = None) -> Optional[bytes]:
    try:
        return await asyncio.to_thread(_render_anime_hud_card_sync, poster_bytes, rel, shiki_details)
    except Exception as e:
        logger.warning(f"Не удалось собрать HUD-карточку аниме: {e}")
        return None


def _make_placeholder_poster_sync(w: int = 800, h: int = 1100) -> bytes:
    top, bottom = (32, 36, 58), (16, 18, 30)
    img = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=color)
    icon_font = _font(200, bold=True)
    label = "★"
    lw = draw.textlength(label, font=icon_font)
    draw.text(((w - lw) / 2, h * 0.34), label, font=icon_font, fill=(95, 110, 150))
    sub_font = _font(32, bold=True)
    sub = "ANIXART RELEASE"
    sw = draw.textlength(sub, font=sub_font)
    draw.text(((w - sw) / 2, h * 0.62), sub, font=sub_font, fill=(150, 165, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


async def _make_placeholder_poster() -> bytes:
    return await asyncio.to_thread(_make_placeholder_poster_sync)


# ── Улучшение #2: Карточка профиля «Anime Wrapped» (1080×1920 для Stories) ────
def _render_anime_wrapped_sync(stats: dict, avatar_bytes: Optional[bytes] = None) -> bytes:
    """Рендерит вертикальную карточку 1080×1920 для Telegram Stories / Итогов просмотра."""
    W, H = 1080, 1920
    bg = Image.new("RGBA", (W, H), (10, 12, 24, 255))
    bdraw = ImageDraw.Draw(bg)
    for y in range(H):
        t = y / H
        r = int(12 + 18 * t)
        g = int(14 + 10 * (1 - t))
        b = int(30 + 38 * t)
        bdraw.line([(0, y), (W, y)], fill=(r, g, b, 255))

    # Неоновые орбы на фоне
    orbs = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(orbs)
    odraw.ellipse((-180, 80, 520, 780), fill=(0, 225, 255, 75))
    odraw.ellipse((520, 480, 1260, 1220), fill=(255, 55, 185, 70))
    odraw.ellipse((120, 1280, 920, 1980), fill=(120, 80, 255, 75))
    orbs = orbs.filter(ImageFilter.GaussianBlur(90))
    bg = Image.alpha_composite(bg, orbs)

    draw = ImageDraw.Draw(bg, "RGBA")
    pad = 64

    # Шапка ANIME WRAPPED
    tag = "✦  ANIME WRAPPED  •  PROFILED BY ANIXART  ✦"
    tf = _font(24, bold=True)
    tw = draw.textlength(tag, font=tf)
    draw.text(((W - tw) / 2, 78), tag, font=tf, fill=(0, 235, 255, 240))

    # Аватарка + Имя + Титул
    ay = 145
    if avatar_bytes:
        try:
            av_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
            av_round = _rounded_image(av_img, (156, 156), radius=78)
            bg.paste(av_round, ((W - 156) // 2, ay), av_round)
            draw.ellipse(((W - 156) // 2 - 4, ay - 4, (W + 156) // 2 + 4, ay + 160),
                         outline=(0, 240, 255, 220), width=4)
            ay += 180
        except Exception:
            pass

    uname = stats.get("user_name") or "Аниме-странник"
    uf = _font(48, bold=True)
    uw = draw.textlength(uname, font=uf)
    draw.text(((W - uw) / 2, ay), uname, font=uf, fill=(255, 255, 255, 255))
    ay += 62

    rank_title = (stats.get("rank_title") or "✦ Магистр Онгоингов")
    rf = _font(28, bold=True)
    rw = draw.textlength(rank_title, font=rf)
    draw.rounded_rectangle(((W - rw) / 2 - 28, ay, (W + rw) / 2 + 28, ay + 50),
                           radius=25, fill=(48, 20, 56, 255), outline=(255, 70, 195, 255), width=2)
    draw.text(((W - rw) / 2, ay + 9), rank_title, font=rf, fill=(255, 230, 250, 255))
    ay += 82

    # 4 метрики в сетке 2×2 (Часы, Дни, Тайтлы, Средняя оценка)
    card_w = (W - pad * 2 - 28) // 2
    card_h = 190
    metrics = [
        (f"{stats.get('hours', 0):.1f} ч", "ПОТРАЧЕНО ЧАСОВ", (0, 235, 255)),
        (f"{stats.get('days', 0):.1f} дн", "ЧИСТЫХ СУТОК В АНИМЕ", (255, 85, 200)),
        (str(stats.get("total_titles", 0)), f"ТАЙТЛОВ ({stats.get('total_episodes', 0)} СЕРИЙ)", (0, 245, 165)),
        (f"★ {stats.get('avg_score', 0):.1f}" if stats.get("avg_score") else "★ —", "СРЕДНЯЯ ОЦЕНКА", (255, 210, 65)),
    ]
    for idx, (val, lbl, col) in enumerate(metrics):
        col_i = idx % 2
        row_i = idx // 2
        cx = pad + col_i * (card_w + 28)
        cy = ay + row_i * (card_h + 24)
        draw.rounded_rectangle((cx, cy, cx + card_w, cy + card_h), radius=30,
                               fill=(20, 25, 44, 195), outline=(255, 255, 255, 50), width=2)
        draw.text((cx + 32, cy + 34), val, font=_font(54, bold=True), fill=(*col, 255))
        draw.text((cx + 32, cy + 124), lbl, font=_font(20, bold=True), fill=(185, 198, 225, 230))

    ay += (card_h + 24) * 2 + 22

    # Топ жанров с прогресс-барами
    g_box_h = 370
    draw.rounded_rectangle((pad, ay, W - pad, ay + g_box_h), radius=32,
                           fill=(18, 22, 40, 195), outline=(255, 255, 255, 48), width=2)
    draw.text((pad + 36, ay + 28), "✦ ТОП ЖАНРОВ В ПРОСМОТРАХ", font=_font(26, bold=True), fill=(0, 235, 255, 255))

    top_genres = stats.get("top_genres") or [("Экшен", 100), ("Фэнтези", 75), ("Драма", 55), ("Комедия", 40)]
    gy = ay + 84
    bar_colors = [
        ((0, 235, 255), (0, 160, 255)),
        ((255, 75, 195), (180, 60, 255)),
        ((0, 245, 160), (0, 185, 210)),
        ((255, 200, 60), (255, 120, 60)),
    ]
    for i, (g_name, pct) in enumerate(top_genres[:4]):
        draw.text((pad + 36, gy), g_name, font=_font(26, bold=True), fill=(245, 248, 255, 255))
        pct_str = f"{pct}%"
        pw_txt = draw.textlength(pct_str, font=_font(24, bold=True))
        draw.text((W - pad - 36 - pw_txt, gy), pct_str, font=_font(24, bold=True), fill=(200, 215, 245, 255))
        c1, c2 = bar_colors[i % len(bar_colors)]
        _draw_gradient_bar(bg, (pad + 36, gy + 36, W - pad - 36, gy + 54), pct / 100.0, c1, c2)
        gy += 68

    ay += g_box_h + 28

    # Любимые тайтлы внизу карточки
    f_box_h = H - ay - 95
    draw.rounded_rectangle((pad, ay, W - pad, ay + f_box_h), radius=32,
                           fill=(18, 22, 40, 195), outline=(255, 255, 255, 48), width=2)
    draw.text((pad + 36, ay + 28), "♥ ЛЮБИМЫЕ И ЗАПОМНИВШИЕСЯ ТАЙТЛЫ",
              font=_font(25, bold=True), fill=(255, 95, 205, 255))

    fav_titles = stats.get("fav_titles") or ["Добавляй релизы в закладки ❤️ и ставь оценки!"]
    fy = ay + 82
    for idx, t_name in enumerate(fav_titles[:4], start=1):
        line = _wrap_lines(draw, f"{idx}. {t_name}", _font(28, bold=True), W - pad * 2 - 72, max_lines=1)[0]
        draw.text((pad + 36, fy), line, font=_font(28, bold=True), fill=(245, 248, 255, 255))
        fy += 52

    footer = "Сгенерировано в боте  •  /anime profile"
    fw = draw.textlength(footer, font=_font(22))
    draw.text(((W - fw) / 2, H - 62), footer, font=_font(22), fill=(150, 165, 200, 190))

    buf = io.BytesIO()
    bg.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ── Улучшение #3: Хронология франшизы и порядок просмотра ─────────────────────
async def _fetch_franchise_order(rel: dict, user_id: int) -> list[dict]:
    """Строит хронологическое дерево связанных сезонов, приквелов и фильмов,
    отмечая просмотренные пользователем части."""
    rid = int(rel.get("id") or 0)
    title = rel.get("title_ru") or rel.get("title_original") or ""
    cache_key = f"franchise_v2:{rid}"
    items = await _cache_get(cache_key)

    if items is None:
        items = []
        # 1. Пробуем связанный список в Anixart (/release/{id}/related или поле related_releases)
        rel_list = rel.get("related_releases") or rel.get("related") or []
        if not isinstance(rel_list, list) or not rel_list:
            try:
                rdata = await _api_get(f"/release/{rid}/related", retries=1)
                rel_list = rdata.get("content") or rdata.get("releases") or []
            except Exception:
                rel_list = []

        if isinstance(rel_list, list) and rel_list:
            for item in rel_list:
                if not isinstance(item, dict):
                    continue
                irid = int(item.get("id") or 0)
                ititle = item.get("title_ru") or item.get("title_original") or "Без названия"
                iyear = item.get("year") or "?"
                cat = item.get("category")
                kind = cat.get("name") if isinstance(cat, dict) else str(cat or "ТВ")
                items.append({
                    "rid": irid, "title": ititle, "year": iyear,
                    "kind": kind, "shiki_id": 0,
                })

        # 2. Если в Anixart пусто — собираем цепочку из Shikimori (/animes/{id}/related)
        if not items:
            shiki_match = await _shiki_find_anime(title)
            sid = int(shiki_match.get("id") or 0) if shiki_match else 0
            if sid:
                related = await _shiki_get(f"/animes/{sid}/related") or []
                seen_sids = {sid}
                nodes = [{
                    "rid": rid,
                    "title": shiki_match.get("russian") or shiki_match.get("name") or title,
                    "year": (shiki_match.get("aired_on") or "")[:4] or rel.get("year") or "?",
                    "kind": shiki_match.get("kind") or "tv",
                    "shiki_id": sid,
                }]
                if isinstance(related, list):
                    for edge in related:
                        a = edge.get("anime") if isinstance(edge, dict) else None
                        if not isinstance(a, dict) or not a.get("id"):
                            continue
                        asid = int(a["id"])
                        if asid in seen_sids:
                            continue
                        seen_sids.add(asid)
                        nodes.append({
                            "rid": 0,
                            "title": a.get("russian") or a.get("name") or "?",
                            "year": (a.get("aired_on") or "")[:4] or "?",
                            "kind": edge.get("relation_russian") or a.get("kind") or "ТВ",
                            "shiki_id": asid,
                        })
                # Сортируем по году выхода
                nodes.sort(key=lambda x: str(x.get("year") or "9999"))
                items = nodes

        # Гарантируем, что сам текущий релиз присутствует в списке
        if rid and not any(x.get("rid") == rid for x in items):
            items.insert(0, {
                "rid": rid, "title": title, "year": rel.get("year") or "?",
                "kind": "Текущий релиз", "shiki_id": 0,
            })

        await _cache_set(cache_key, items, ttl=43200)

    # Сопоставляем с историей, закладками и списком пользователя
    hist_ids = {int(h["id"]) for h in get_anime_history(user_id, limit=100)}
    fav_ids = {int(f["id"]) for f in get_anime_favs(user_id)}
    user_rates = {int(r["rid"]): r for r in await asyncio.to_thread(_get_all_user_rates_sync, user_id)}
    watched_titles = {
        (r.get("title") or "").lower().strip()
        for r in user_rates.values() if r.get("status") in ("completed", "watching")
    } | {
        (h.get("title") or "").lower().strip()
        for h in get_anime_history(user_id, limit=100)
    }

    enriched = []
    for idx, it in enumerate(items[:15], start=1):
        irid = int(it.get("rid") or 0)
        ititle = it.get("title") or "?"
        rate = user_rates.get(irid)
        if rate and rate.get("status") == "completed":
            mark = "✅"
        elif irid in hist_ids or ititle.lower().strip() in watched_titles:
            mark = "✅"
        elif rate and rate.get("status") == "watching":
            mark = "👀"
        elif irid in fav_ids:
            mark = "❤️"
        else:
            mark = "⬜"
        enriched.append({**it, "order": idx, "mark": mark, "is_current": (irid == rid and rid > 0)})
    return enriched


# ── Улучшение #4: Поиск аниме по скриншоту, гифке или видео-кружочку ──────────
async def _extract_frame_bytes_from_message(msg: Message) -> Optional[bytes]:
    """Извлекает JPEG-кадр из фото, гифки, видео или видео-кружочка (или из reply)."""
    target = msg
    if not (target.photo or target.animation or target.video or target.video_note):
        if msg.reply_to_message:
            target = msg.reply_to_message

    bot = msg.bot
    # 1. Обычное фото
    if target.photo:
        f_info = await bot.get_file(target.photo[-1].file_id)
        dl = await bot.download_file(f_info.file_path)
        return dl.read() if hasattr(dl, "read") else bytes(dl)

    # 2. Гифка, видео или кружочек
    media_obj = target.animation or target.video or target.video_note
    if not media_obj:
        return None

    # Сначала пробуем превью (thumbnail), если оно есть — это мгновенно
    thumb = getattr(media_obj, "thumbnail", None) or getattr(media_obj, "thumb", None)
    if thumb and getattr(thumb, "file_id", None):
        try:
            f_info = await bot.get_file(thumb.file_id)
            dl = await bot.download_file(f_info.file_path)
            raw = dl.read() if hasattr(dl, "read") else bytes(dl)
            if raw and len(raw) > 500:
                return raw
        except Exception:
            pass

    # Иначе скачиваем сам видеоролик/гифку и вытаскиваем кадр через ffmpeg
    try:
        f_info = await bot.get_file(media_obj.file_id)
        dl = await bot.download_file(f_info.file_path)
        raw_video = dl.read() if hasattr(dl, "read") else bytes(dl)
        with tempfile.TemporaryDirectory(prefix="anix_frame_") as tmpd:
            in_path = os.path.join(tmpd, "input.mp4")
            out_path = os.path.join(tmpd, "frame.jpg")
            with open(in_path, "wb") as f:
                f.write(raw_video)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", "00:00:01", "-i", in_path,
                "-frames:v", "1", "-q:v", "2", out_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), timeout=15)
            if os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    return f.read()
    except Exception as e:
        logger.warning(f"Не удалось извлечь кадр через ffmpeg: {e}")
    return None


async def _recognize_anime_by_frame(frame_bytes: bytes) -> Optional[dict]:
    """Пробивает кадр через trace.moe с фоллбэком на мультимодальный gemini-2.5-flash."""
    best_result = None
    # 1. trace.moe API
    try:
        session = await _get_session()
        data = aiohttp.FormData()
        data.add_field("image", frame_bytes, filename="frame.jpg", content_type="image/jpeg")
        async with session.post(TRACE_MOE_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                res_json = await r.json()
                docs = res_json.get("result") or []
                if docs:
                    top = docs[0]
                    sim = float(top.get("similarity") or 0.0)
                    anilist = top.get("anilist") if isinstance(top.get("anilist"), dict) else {}
                    title_obj = anilist.get("title") if isinstance(anilist.get("title"), dict) else {}
                    romaji = title_obj.get("romaji") or title_obj.get("english") or title_obj.get("native") or ""
                    synonyms = anilist.get("synonyms") or []
                    ru_syn = next((s for s in synonyms if re.search(r"[А-Яа-яЁё]", str(s))), "")
                    ep = top.get("episode") or "?"
                    sec = int(float(top.get("from") or 0))
                    timecode = f"{sec // 60:02d}:{sec % 60:02d}"
                    best_result = {
                        "title_search": ru_syn or romaji or str(top.get("filename") or ""),
                        "title_display": ru_syn or romaji,
                        "title_romaji": romaji,
                        "episode": ep,
                        "timecode": timecode,
                        "similarity": round(sim * 100, 1),
                        "source": "trace.moe",
                    }
    except Exception as e:
        logger.warning(f"trace.moe ошибка: {e}")

    # 2. Фоллбэк на мультимодальный gemini-2.5-flash, если trace.moe не дал уверенного ответа (<86%)
    if not best_result or best_result.get("similarity", 0) < 86.0:
        try:
            from ai_handlers import get_gemini
            client = get_gemini()
            if client:
                img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
                prompt = (
                    "Определи аниме по этому кадру. Верни СТРОГО JSON без markdown-обёрток: "
                    '{"title_ru": "Русское название", "title_romaji": "Оригинальное/английское название", '
                    '"episode": "предполагаемая серия или ?", "note": "кратко какой это персонаж или сцена"}'
                )
                resp = await client.aio.models.generate_content(
                    model="gemini-2.5-flash", contents=[img, prompt]
                )
                raw_txt = (resp.text or "").strip()
                m = re.search(r"\{.*\}", raw_txt, re.DOTALL)
                if m:
                    parsed = _json.loads(m.group(0))
                    t_ru = (parsed.get("title_ru") or "").strip()
                    t_rom = (parsed.get("title_romaji") or "").strip()
                    if t_ru or t_rom:
                        return {
                            "title_search": t_ru or t_rom,
                            "title_display": t_ru or t_rom,
                            "title_romaji": t_rom,
                            "episode": parsed.get("episode") or (best_result or {}).get("episode", "?"),
                            "timecode": (best_result or {}).get("timecode", "—"),
                            "similarity": (best_result or {}).get("similarity", 92.0),
                            "note": parsed.get("note", ""),
                            "source": "Gemini Vision + trace.moe" if best_result else "Gemini 2.5 Flash Vision",
                        }
        except Exception as e:
            logger.warning(f"Gemini Vision anime fallback error: {e}")

    return best_result


# ── Улучшение #8: Поиск и скачивание OP / ED в один клик ──────────────────────
async def _fetch_oped_themes(rel: dict) -> list[dict]:
    """Собирает список опенингов и эндингов тайтла из Shikimori, Anixart и AI-фоллбэка."""
    rid = int(rel.get("id") or 0)
    title_ru = rel.get("title_ru") or ""
    title_orig = rel.get("title_original") or title_ru
    cache_key = f"oped_themes:{rid}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    themes: list[dict] = []
    shiki = await _shiki_find_anime(title_orig or title_ru)
    sid = int(shiki.get("id") or 0) if shiki else 0

    if sid:
        # Пробуем видео-клипы OP/ED на Shikimori
        videos = await _shiki_get(f"/animes/{sid}/videos") or []
        if isinstance(videos, list):
            for v in videos:
                if isinstance(v, dict) and v.get("kind") in ("op", "ed", "op_ed_clip"):
                    vname = v.get("name") or "Opening / Ending"
                    themes.append({
                        "type": "OP/ED",
                        "label": vname,
                        "search_query": f"{title_orig} {vname}".strip(),
                        "url": v.get("url") or "",
                    })

    # Если тем мало — спрашиваем у Gemini точные названия OP/ED этого тайтла
    if len(themes) < 2:
        try:
            from ai_handlers import get_gemini
            client = get_gemini()
            if client:
                prompt = (
                    f"Перечисли официальные опенинги (OP) и эндинги (ED) аниме «{title_ru}» ({title_orig}). "
                    "Верни СТРОГО JSON-массив объектов формата: "
                    '[{"type": "OP 1", "artist": "Исполнитель", "song": "Название песни"}]. Максимум 6 тем.'
                )
                resp = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
                m = re.search(r"\[.*\]", resp.text or "", re.DOTALL)
                if m:
                    arr = _json.loads(m.group(0))
                    for item in arr[:6]:
                        if isinstance(item, dict) and item.get("song"):
                            t_type = item.get("type") or "OP"
                            artist = item.get("artist") or title_orig
                            song = item.get("song")
                            themes.append({
                                "type": t_type,
                                "artist": artist,
                                "song": song,
                                "label": f"{t_type}: {artist} — {song}",
                                "search_query": f"{artist} - {song}",
                            })
        except Exception as e:
            logger.debug(f"Gemini OP/ED lookup fallback: {e}")

    # Универсальный фоллбэк по названию аниме, чтобы поиск работал всегда
    if not themes:
        base_t = title_orig or title_ru
        themes = [
            {"type": "OP 1", "artist": base_t, "song": "Opening 1",
             "label": f"OP 1: {base_t} — Opening", "search_query": f"{base_t} opening full"},
            {"type": "ED 1", "artist": base_t, "song": "Ending 1",
             "label": f"ED 1: {base_t} — Ending",  "search_query": f"{base_t} ending full"},
        ]

    await _cache_set(cache_key, themes[:8], ttl=86400)
    return themes[:8]


async def _download_oped_mp3_with_tags(theme: dict, rel: dict) -> tuple[Optional[str], dict]:
    """Ищет трек через music_engine.search_multi (если есть) или yt-dlp,
    скачивает MP3 и вшивает обложку + ID3-теги."""
    query = theme.get("search_query") or theme.get("label") or ""
    title_ru = rel.get("title_ru") or rel.get("title_original") or "Anime"
    artist = theme.get("artist") or title_ru
    song_title = theme.get("song") or theme.get("label") or "Opening"
    poster_url = (await shiki_get_poster_url(title_ru)) or _extract_poster_url(rel)

    track_meta = {
        "id": f"anix_ost_{rel.get('id', 0)}_{hashlib.md5(query.encode()).hexdigest()[:6]}",
        "title": song_title,
        "artist": artist,
        "artwork_url": poster_url,
        "source": "YouTube",
    }

    # 1. Пробуем нативный music_engine проекта
    target_url = f"ytsearch1:{query} anime ost"
    try:
        import music_engine as _me
        if hasattr(_me, "search_multi"):
            found_tracks = await _me.search_multi(query, limit=3)
            if found_tracks and isinstance(found_tracks, list):
                top_tr = found_tracks[0]
                if isinstance(top_tr, dict):
                    track_meta.update({
                        "id": str(top_tr.get("id") or track_meta["id"]),
                        "title": top_tr.get("title") or song_title,
                        "artist": top_tr.get("artist") or artist,
                        "artwork_url": top_tr.get("artwork_url") or poster_url,
                        "source": top_tr.get("source") or "SoundCloud",
                    })
                    if top_tr.get("url"):
                        target_url = top_tr["url"]
        if hasattr(_me, "download_track"):
            dl_res = await _me.download_track(track_meta)
            if isinstance(dl_res, str) and os.path.exists(dl_res):
                return dl_res, track_meta
            if isinstance(dl_res, tuple) and dl_res[0] and os.path.exists(dl_res[0]):
                return dl_res[0], track_meta
    except Exception as e:
        logger.debug(f"music_engine fallback на yt-dlp: {e}")

    # 2. Скачивание MP3 через yt-dlp + ffmpeg + вшивание ID3-тегов и обложки
    try:
        import yt_dlp
    except ImportError:
        return None, track_meta

    tmp_dir = tempfile.mkdtemp(prefix="anix_mp3_")
    out_tmpl = os.path.join(tmp_dir, "ost.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": 48 * 1024 * 1024,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    def _run_ydl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            return info

    try:
        info = await asyncio.wait_for(asyncio.to_thread(_run_ydl), timeout=120)
        mp3_path = os.path.join(tmp_dir, "ost.mp3")
        if not os.path.exists(mp3_path):
            for f in Path(tmp_dir).iterdir():
                if f.is_file():
                    mp3_path = str(f)
                    break
        if not os.path.exists(mp3_path):
            return None, track_meta

        track_meta["duration_sec"] = int(info.get("duration") or 0)
        track_meta["_tmp_dir"] = tmp_dir

        # Вшиваем ID3-теги и обложку через mutagen (если установлен) или отдаём с thumbnail
        poster_bytes = await _download_poster(poster_url) if poster_url else None
        if poster_bytes:
            track_meta["_thumb_bytes"] = poster_bytes
            try:
                from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, ID3NoHeaderError
                try:
                    tags = ID3(mp3_path)
                except ID3NoHeaderError:
                    tags = ID3()
                tags.add(TIT2(encoding=3, text=track_meta["title"]))
                tags.add(TPE1(encoding=3, text=track_meta["artist"]))
                tags.add(TALB(encoding=3, text=f"{title_ru} OST"))
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=poster_bytes))
                tags.save(mp3_path)
            except Exception:
                pass
        return mp3_path, track_meta
    except Exception as e:
        logger.warning(f"Не удалось скачать OP/ED «{query}»: {e}")
        return None, track_meta


# ── Трейлеры и ссылки на плеер ────────────────────────────────────────────────
def _trailer_url_from_release(rel: dict) -> Optional[str]:
    direct = rel.get("trailer") or rel.get("trailer_url") or rel.get("video_trailer")
    if isinstance(direct, str) and direct.startswith("http"):
        return direct
    if isinstance(direct, dict):
        yt_id = direct.get("youtube_id") or direct.get("id") or direct.get("video_id")
        url = direct.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
        if yt_id:
            return f"https://www.youtube.com/watch?v={yt_id}"
    return None


async def _fetch_trailer(rel: dict) -> tuple[Optional[str], dict]:
    from media_downloader import _download_media

    title = rel.get("title_ru") or rel.get("title_original") or ""
    shiki_url = await shiki_get_trailer_url(title)
    anix_url  = _trailer_url_from_release(rel)
    candidates = [u for u in (shiki_url, anix_url) if u]
    candidates.append(f"ytsearch1:{title} трейлер аниме")

    last_err = None
    for url in candidates:
        try:
            filepath, info = await _download_media(url, "youtube")
            if filepath:
                return filepath, info
        except Exception as e:
            last_err = e
    return None, {"_error": str(last_err)[:200] if last_err else "нет доступных источников"}


EPISODE_URL_TEMPLATE: Optional[str] = None
_EP_PAGE_SIZE = 25


def _build_player_url(rid: int, title: str = "", ep: int = 1, user_id: int = 0, shiki_id: int = 0) -> Optional[str]:
    if not WEBAPP_BASE_URL or not WEBAPP_BASE_URL.startswith("https://"):
        return None
    params = {"rid": rid, "ep": ep, "uid": user_id, "title": (title or "")[:80]}
    if shiki_id:
        params["shiki_id"] = shiki_id
    return f"{WEBAPP_BASE_URL}/anime/player?{urllib.parse.urlencode(params)}"


def _episode_url(rid: int, ep: int) -> str:
    if EPISODE_URL_TEMPLATE:
        return EPISODE_URL_TEMPLATE.format(rid=rid, ep=ep)
    return f"https://anixart.tv/release/{rid}"


def _episode_keyboard(rid: int, ep_total: int, offset: int = 0,
                      title: str = "", user_id: int = 0, watched_ep: int = 0) -> InlineKeyboardMarkup:
    page = list(range(offset + 1, min(ep_total, offset + _EP_PAGE_SIZE) + 1))
    rows, row = [], []

    webapp_url = _build_player_url(rid, title, max(1, watched_ep + 1), user_id)
    if webapp_url:
        rows.append([InlineKeyboardButton(
            text=f"📱 Открыть Mini App плеер (Серия {min(ep_total, max(1, watched_ep + 1))})",
            web_app=WebAppInfo(url=webapp_url)
        )])

    for ep in page:
        mark = "✓" if ep <= watched_ep else ""
        ep_webapp = _build_player_url(rid, title, ep, user_id)
        if ep_webapp:
            btn = InlineKeyboardButton(text=f"{mark}{ep}", web_app=WebAppInfo(url=ep_webapp))
        else:
            btn = InlineKeyboardButton(text=f"{mark}{ep}", callback_data=f"anix:watch_ep:{rid}:{ep}")
        row.append(btn)
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Назад", callback_data=f"anix:eplist:{rid}:{max(0, offset - _EP_PAGE_SIZE)}"
        ))
    if offset + _EP_PAGE_SIZE < ep_total:
        nav.append(InlineKeyboardButton(
            text="Ещё ➡️", callback_data=f"anix:eplist:{rid}:{offset + _EP_PAGE_SIZE}"
        ))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Клавиатуры карточки релиза и главного меню ────────────────────────────────
def _release_keyboard(rel: dict, user_id: int) -> InlineKeyboardMarkup:
    rid = rel.get("id", 0)
    title = rel.get("title_ru") or rel.get("title_original") or ""
    fav = is_anime_fav(user_id, rid)
    sub = is_anime_subscribed(user_id, rid)
    player_url = _build_player_url(rid, title, 1, user_id)

    play_row = [InlineKeyboardButton(text="▶️ Серии", callback_data=f"anix:eplist:{rid}:0")]
    if player_url:
        play_row.insert(0, InlineKeyboardButton(
            text="📱 Плеер Mini App", web_app=WebAppInfo(url=player_url)
        ))

    rows = [
        play_row,
        [
            InlineKeyboardButton(text=("💔 В закладках" if fav else "🤍 В закладки"), callback_data=f"anix:fav:{rid}"),
            InlineKeyboardButton(text=("🔕 Отписаться" if sub else "🔔 Подписка + Озвучка"), callback_data=f"anix:sub:{rid}"),
        ],
        [
            InlineKeyboardButton(text="🤖 Вердикт ИИ (без спойлеров)", callback_data=f"anix:vibe:{rid}"),
        ],
        [
            InlineKeyboardButton(text="🔗 Франшиза", callback_data=f"anix:franchise:{rid}"),
            InlineKeyboardButton(text="🎵 OP / ED", callback_data=f"anix:oped:{rid}"),
            InlineKeyboardButton(text="🎬 Трейлер", callback_data=f"anix:trailer:{rid}"),
        ],
        [
            InlineKeyboardButton(text="📂 Статус / Shikimori", callback_data=f"anix:status_menu:{rid}"),
            InlineKeyboardButton(text="🔀 Случайное", callback_data="anix:random"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎛 Умный фильтр", callback_data="anix:sfilter:open"),
            InlineKeyboardButton(text="📅 Календарь недели", callback_data="anix:sched:today:0"),
        ],
        [
            InlineKeyboardButton(text="📊 Мой Anime Wrapped", callback_data="anix:profile_card"),
            InlineKeyboardButton(text="🧠 ИИ-подбор по вайбу", callback_data="anix:ai_prompt"),
        ],
        [
            InlineKeyboardButton(text="🎰 Что смотрим? (Рулетка)", callback_data="anix:party:open"),
            InlineKeyboardButton(text="🔄 Shikimori OAuth", callback_data="anix:shiki_menu"),
        ],
        [
            InlineKeyboardButton(text="🔎 Искать аниме инлайн", switch_inline_query_current_chat="a:"),
        ],
    ])


async def _send_release(ctx, rel: dict, extra_prefix: str = ""):
    rid     = int(rel.get("id") or 0)
    title   = rel.get("title_ru") or rel.get("title_original") or ""
    user_id = ctx.from_user.id
    chat_id = ctx.message.chat.id if isinstance(ctx, CallbackQuery) else ctx.chat.id
    bot     = ctx.bot

    log_anime_history(user_id, {"id": rid, "title": title})
    asyncio.create_task(_stats_bump_view(rid, title))

    # Параллельно подтягиваем детали Shikimori и статус пользователя
    shiki_details, user_rate = await asyncio.gather(
        _shiki_get_full_details(title),
        asyncio.to_thread(_get_user_rate_sync, user_id, rid),
    )
    # Сохраняем жанры/метаданные в профиль просмотров, если пользователь открыл карточку
    if not user_rate:
        asyncio.create_task(asyncio.to_thread(
            _upsert_user_rate_sync, user_id, rel, status="watching", episodes=0
        ))

    caption = (extra_prefix + "\n\n" if extra_prefix else "") + _fmt_release(rel, shiki_details, user_rate)
    kb      = _release_keyboard(rel, user_id)

    shiki_poster_url = await shiki_get_poster_url(title)
    anix_poster_url  = _extract_poster_url(rel)
    poster_url = shiki_poster_url or anix_poster_url
    real_bytes = await _download_poster(shiki_poster_url) if shiki_poster_url else None
    if not real_bytes and anix_poster_url and anix_poster_url != shiki_poster_url:
        real_bytes = await _download_poster(anix_poster_url)
        if real_bytes:
            poster_url = anix_poster_url
    used_placeholder = real_bytes is None
    poster_bytes = real_bytes if real_bytes else await _make_placeholder_poster()

    card_bytes = await _render_anime_card(poster_bytes, rel, shiki_details)
    cache_seed = poster_url if not used_placeholder else f"placeholder_{rid}"
    fname = f"anix_hud_{hashlib.md5(cache_seed.encode()).hexdigest()[:8]}.jpg"
    try:
        sent = await bot.send_photo(
            chat_id, BufferedInputFile(card_bytes or poster_bytes, fname),
            caption=caption[:1024], parse_mode="HTML", reply_markup=kb
        )
        if sent.photo and not used_placeholder:
            save_cached_file_id(f"anix_poster_{rid}", sent.photo[-1].file_id)
        return
    except Exception as e:
        logger.warning(f"Не удалось отправить HUD-карточку фото: {e}")
    await bot.send_message(chat_id, caption[:4096], parse_mode="HTML", reply_markup=kb)


# ── Кэшированные запросы к API + фоновая предзагрузка страниц поиска (#15) ────
async def _get_release(rid: int) -> Optional[dict]:
    key = f"release:{rid}"
    cached = await _cache_get(key)
    if cached:
        return cached
    try:
        data = await _api_get(f"/release/{rid}", {"extended_mode": "true"})
        rel = data.get("release")
        if rel:
            await _cache_set(key, rel, ttl=3600)
        return rel
    except Exception:
        return None


async def _get_genres() -> list:
    key = "genres"
    cached = await _cache_get(key)
    if cached:
        return cached
    try:
        data = await _api_get("/genre/list")
        genres = data.get("genres") or data.get("content") or []
        if genres:
            await _cache_set(key, genres, ttl=86400)
        return genres
    except Exception:
        return []


async def _search_releases_cached(query: str, offset: int = 0, prefetch_next: bool = True) -> tuple[list, int]:
    """Ищет релизы с кэшированием в SQLite и фоновой предзагрузкой следующей страницы."""
    q_norm = (query or "").strip().lower()
    if not q_norm:
        return [], 0
    cache_key = f"search:{q_norm}:{offset}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        content = cached.get("content") or []
        total = cached.get("total") or 0
    else:
        try:
            data = await _api_post(f"/search/releases/{offset}", {"query": query, "searchBy": 0})
        except Exception:
            data = await _api_get(f"/search/releases/{offset}", {"q": query, "query": query})
        content = data.get("releases") or data.get("content", []) or data.get("data", [])
        total = int(data.get("total") or data.get("totalCount") or len(content))
        await _cache_set(cache_key, {"content": content, "total": total}, ttl=900)

    if prefetch_next and (total > offset + 10 or len(content) >= 10):
        asyncio.create_task(_search_releases_cached(query, offset + 10, prefetch_next=False))
    return content, total


async def _fetch_genre_pool(gid: int) -> list:
    key = f"genre_pool:{gid}"
    cached = await _cache_get(key)
    if cached is not None:
        return cached
    offsets = (0, 10, 20, 30)

    async def _fetch_page(off: int):
        try:
            data = await _api_get(f"/genre/{gid}/releases/{off}")
            return data.get("content") or []
        except Exception:
            return None

    pages = await asyncio.gather(*[_fetch_page(o) for o in offsets])
    pool = []
    for chunk in pages:
        if chunk is None:
            break
        pool.extend(chunk)
        if len(chunk) < 10:
            break
    await _cache_set(key, pool, ttl=900)
    return pool


def _distinct_statuses(pool: list) -> list:
    seen, out = set(), []
    for item in pool:
        st = item.get("status")
        name = st.get("name") if isinstance(st, dict) else str(st or "")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _distinct_years(pool: list) -> list:
    return sorted({item.get("year") for item in pool if item.get("year")}, reverse=True)


def _filter_pool(pool: list, status_idx: int, year: int, statuses: list) -> list:
    out = pool
    if 0 < status_idx <= len(statuses):
        target = statuses[status_idx - 1]
        out = [i for i in out if (
            (i.get("status").get("name") if isinstance(i.get("status"), dict) else str(i.get("status") or ""))
            == target
        )]
    if year:
        out = [i for i in out if i.get("year") == year]
    return out


async def _render_genre_page(msg: Message, gid: int, status_idx: int, year: int, offset: int):
    try:
        pool = await _fetch_genre_pool(gid)
        if not pool:
            await msg.edit_text("😔 По этому жанру ничего не найдено.")
            return

        statuses = _distinct_statuses(pool)
        years    = _distinct_years(pool)
        filtered = _filter_pool(pool, status_idx, year, statuses)
        page     = filtered[offset:offset + 10]

        buttons = []
        for item in page:
            title = (item.get("title_ru") or item.get("title_original") or "Без названия")[:40]
            y_    = item.get("year") or "?"
            rid   = item.get("id", 0)
            buttons.append([InlineKeyboardButton(text=f"{title} ({y_})", callback_data=f"anix:info:{rid}")])

        status_row = [InlineKeyboardButton(
            text=("• Все статусы" if status_idx == 0 else "Все статусы"),
            callback_data=f"anix:gf:{gid}:0:{year}:0"
        )]
        for i, s in enumerate(statuses[:3], start=1):
            label = f"• {s}" if i == status_idx else s
            status_row.append(InlineKeyboardButton(text=label[:16], callback_data=f"anix:gf:{gid}:{i}:{year}:0"))
        buttons.append(status_row)

        if years:
            year_row = [InlineKeyboardButton(
                text=("• Любой год" if year == 0 else "Любой год"),
                callback_data=f"anix:gf:{gid}:{status_idx}:0:0"
            )]
            for y_ in years[:4]:
                label = f"• {y_}" if y_ == year else str(y_)
                year_row.append(InlineKeyboardButton(text=label, callback_data=f"anix:gf:{gid}:{status_idx}:{y_}:0"))
            buttons.append(year_row)

        if len(filtered) > offset + 10:
            buttons.append([InlineKeyboardButton(
                text="⏩ Ещё", callback_data=f"anix:gf:{gid}:{status_idx}:{year}:{offset + 10}"
            )])
        buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

        suffix = f" (стр. {offset // 10 + 1})" if offset else ""
        text = f"🎭 Найдено {len(filtered)} аниме{suffix}:" if filtered else "😔 Ничего не подходит под этот фильтр."
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        try:
            await msg.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        except Exception:
            pass


# ── Улучшение #11: Интерактивный календарь онгоингов на неделю (/anime schedule) ──
async def _fetch_weekly_schedule() -> dict[int, list[dict]]:
    """Возвращает словарь {день_недели (0..6): [список_релизов]} из Anixart + Shikimori."""
    key = "weekly_schedule_v2"
    cached = await _cache_get(key)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}

    schedule: dict[int, list[dict]] = {d: [] for d in range(7)}

    # 1. Пробуем /schedule в Anixart
    try:
        data = await _api_get("/schedule", retries=1)
        for day_idx, (_, _, eng_key, num_key) in enumerate(WEEKDAYS_RU):
            items = data.get(eng_key) or data.get(str(num_key)) or []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("id"):
                        schedule[day_idx].append({
                            "id": int(it["id"]),
                            "title": it.get("title_ru") or it.get("title_original") or "?",
                            "ep": (it.get("episodes_released") or 0) + 1,
                            "time": it.get("broadcast_time") or "",
                        })
    except Exception:
        pass

    # 2. Дополняем из Shikimori /calendar, если день пустой
    if sum(len(v) for v in schedule.values()) == 0:
        cal = await _shiki_get("/calendar") or []
        if isinstance(cal, list):
            for entry in cal:
                if not isinstance(entry, dict):
                    continue
                anime = entry.get("anime") or {}
                next_at = entry.get("next_episode_at") or ""
                if not next_at or not anime:
                    continue
                try:
                    dt = datetime.datetime.fromisoformat(next_at.replace("Z", "+00:00"))
                    wd = dt.weekday()
                    t_str = dt.strftime("%H:%M")
                except Exception:
                    wd, t_str = 0, ""
                schedule[wd].append({
                    "id": 0,
                    "shiki_id": anime.get("id", 0),
                    "alt": anime.get("name") or "",
                    "title": anime.get("russian") or anime.get("name") or "?",
                    "ep": entry.get("next_episode") or "?",
                    "time": t_str,
                })

    await _cache_set(key, {str(k): v[:20] for k, v in schedule.items()}, ttl=3600)
    return schedule


async def _render_schedule_view(target_msg: Message, user_id: int, day_idx: int,
                                only_my_subs: bool = False, edit: bool = True):
    schedule = await _fetch_weekly_schedule()
    day_idx = max(0, min(6, day_idx))
    short_lbl, full_lbl, _, _ = WEEKDAYS_RU[day_idx]
    items = schedule.get(day_idx, [])

    if only_my_subs:
        subs = get_anime_subscriptions(user_id)
        sub_ids = {int(s["id"]) for s in subs}
        sub_titles = {(s.get("title") or "").lower().strip() for s in subs if s.get("title")}
        items = [
            it for it in items
            if (it.get("id") and int(it["id"]) in sub_ids)
            or (it.get("title") or "").lower().strip() in sub_titles
        ]

    filter_label = "🔔 Только мои подписки" if only_my_subs else "🌐 Все онгоинги сезона"
    lines = [
        f"📅 <b>Календарь онгоингов — {full_lbl}</b>",
        f"Фильтр: <i>{filter_label}</i>\n",
    ]
    if not items:
        lines.append("😔 На этот день нет запланированных выходов серий." + (
            "\n<i>Попробуй переключить фильтр на «Все онгоинги».</i>" if only_my_subs else ""
        ))
    else:
        for i, it in enumerate(items[:14], start=1):
            t_badge = f"<code>[{it['time']}]</code> " if it.get("time") else ""
            lines.append(f"{i}. {t_badge}<b>{html.escape(it['title'])}</b> — {it.get('ep', '?')} эп.")

    rows = []
    # Кнопки релизов
    for it in items[:8]:
        t_short = (it.get("title") or "?")[:34]
        ep_s = f" ({it.get('ep', '?')} эп.)"
        if it.get("id"):
            rows.append([InlineKeyboardButton(text=f"🎬 {t_short}{ep_s}", callback_data=f"anix:info:{it['id']}")])
        else:
            rows.append([InlineKeyboardButton(text=f"🎬 {t_short}{ep_s}", callback_data=_go_cb(it.get("title"), it.get("alt", "")))])

    # Дни недели ПН..ВС
    sub_flag = 1 if only_my_subs else 0
    day_row = []
    for idx, (sh, _, _, _) in enumerate(WEEKDAYS_RU):
        lbl = f"• {sh} •" if idx == day_idx else sh
        day_row.append(InlineKeyboardButton(text=lbl, callback_data=f"anix:sched:{idx}:{sub_flag}"))
    rows.append(day_row)

    # Переключатель «Показывать только мои подписки»
    toggle_flag = 0 if only_my_subs else 1
    toggle_txt = "🌐 Показать все онгоинги" if only_my_subs else "🔔 Показывать только мои подписки"
    rows.append([InlineKeyboardButton(text=toggle_txt, callback_data=f"anix:sched:{day_idx}:{toggle_flag}")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "\n".join(lines)
    if edit:
        try:
            await target_msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await target_msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Улучшение #13: Инлайн-конструктор фильтров («Умный фильтр») ───────────────
SMART_FILTER_GENRES = [
    ("Экшен", 1), ("Фэнтези", 10), ("Романтика", 22),
    ("Комедия", 4), ("Драма", 8), ("Сверхъестественное", 37),
    ("Фантастика", 24), ("Триллер", 41), ("Повседневность", 36),
]
SMART_FILTER_YEARS = ["Любой", "2026", "2025", "2024", "2020-2023"]
SMART_FILTER_STATUSES = [
    ("any", "Любой"),
    ("ongoing", "Онгоинг"),
    ("released", "Вышел"),
]
SMART_FILTER_SCORES = [("0", "Любой"), ("7", "7+ ⭐"), ("8", "8+ 🔥")]
SMART_FILTER_KINDS = [
    ("any", "Любой"),
    ("tv", "ТВ-сериал"),
    ("movie", "Фильм"),
]


def _default_smart_filter() -> dict:
    return {
        "genres": [],
        "year": "Любой",
        "status": "any",
        "score": "0",
        "kind": "any",
    }


def _get_smart_filter_sync(user_id: int) -> dict:
    conn = _db_conn()
    try:
        r = conn.execute("SELECT filter_json FROM anime_smart_filters WHERE user_id=?", (str(user_id),)).fetchone()
    finally:
        conn.close()
    if r and r[0]:
        try:
            d = _json.loads(r[0])
            base = _default_smart_filter()
            base.update(d)
            return base
        except Exception:
            pass
    return _default_smart_filter()


def _save_smart_filter_sync(user_id: int, flt: dict):
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT INTO anime_smart_filters (user_id, filter_json) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET filter_json=excluded.filter_json",
            (str(user_id), _json.dumps(flt, ensure_ascii=False))
        )
        conn.commit()
    finally:
        conn.close()


def _smart_filter_keyboard(flt: dict) -> InlineKeyboardMarkup:
    sel_genres = set(flt.get("genres") or [])
    rows = []

    # Мультивыбор жанров (по 3 в ряд)
    g_row = []
    for g_name, g_id in SMART_FILTER_GENRES:
        mark = "✅ " if g_name in sel_genres else ""
        g_row.append(InlineKeyboardButton(
            text=f"{mark}{g_name}", callback_data=f"anix:sf_g:{g_name}"
        ))
        if len(g_row) == 3:
            rows.append(g_row)
            g_row = []
    if g_row:
        rows.append(g_row)

    # Год
    cur_yr = flt.get("year", "Любой")
    rows.append([
        InlineKeyboardButton(
            text=(f"📅 • {y}" if y == cur_yr else y),
            callback_data=f"anix:sf_y:{y}"
        ) for y in SMART_FILTER_YEARS
    ])

    # Статус
    cur_st = flt.get("status", "any")
    rows.append([
        InlineKeyboardButton(
            text=(f"📊 • {lbl}" if k == cur_st else lbl),
            callback_data=f"anix:sf_st:{k}"
        ) for k, lbl in SMART_FILTER_STATUSES
    ])

    # Рейтинг и Тип
    cur_sc = str(flt.get("score", "0"))
    rows.append([
        InlineKeyboardButton(
            text=(f"⭐ • {lbl}" if k == cur_sc else f"Рейтинг: {lbl}"),
            callback_data=f"anix:sf_sc:{k}"
        ) for k, lbl in SMART_FILTER_SCORES
    ])

    cur_kd = flt.get("kind", "any")
    rows.append([
        InlineKeyboardButton(
            text=(f"🎬 • {lbl}" if k == cur_kd else lbl),
            callback_data=f"anix:sf_kd:{k}"
        ) for k, lbl in SMART_FILTER_KINDS
    ])

    rows.append([
        InlineKeyboardButton(text="🔄 Сбросить", callback_data="anix:sfilter:reset"),
        InlineKeyboardButton(text="🚀 Показать результаты", callback_data="anix:sfilter:run:0"),
    ])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _smart_filter_summary_text(flt: dict) -> str:
    g_str = ", ".join(flt.get("genres") or []) or "Все жанры"
    st_lbl = dict(SMART_FILTER_STATUSES).get(flt.get("status", "any"), "Любой")
    kd_lbl = dict(SMART_FILTER_KINDS).get(flt.get("kind", "any"), "Любой")
    sc_lbl = dict(SMART_FILTER_SCORES).get(str(flt.get("score", "0")), "Любой")
    return (
        "🎛 <b>Умный конструктор фильтров аниме</b>\n\n"
        f"🏷 <b>Жанры:</b> {g_str}\n"
        f"📅 <b>Год:</b> {flt.get('year', 'Любой')}\n"
        f"📊 <b>Статус:</b> {st_lbl}\n"
        f"⭐ <b>Рейтинг:</b> {sc_lbl}\n"
        f"🎬 <b>Тип:</b> {kd_lbl}\n\n"
        "<i>Кликай по кнопкам для мультивыбора и жми «🚀 Показать результаты»:</i>"
    )


async def _execute_smart_filter(flt: dict) -> list[dict]:
    """Выполняет фильтрацию с кэшированием в anixart_cache.sqlite3."""
    raw_hash = hashlib.md5(_json.dumps(flt, sort_keys=True).encode()).hexdigest()[:12]
    cache_key = f"smart_filter_res:{raw_hash}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    # 1. Запрашиваем Shikimori /animes с точными параметрами фильтрации
    params = {"limit": 24, "order": "popularity"}
    g_map = dict(SMART_FILTER_GENRES)
    sel_g_ids = [str(g_map[g]) for g in (flt.get("genres") or []) if g in g_map]
    if sel_g_ids:
        params["genre"] = ",".join(sel_g_ids)
    if flt.get("status") in ("ongoing", "released"):
        params["status"] = flt["status"]
    if flt.get("kind") in ("tv", "movie"):
        params["kind"] = flt["kind"]
    if str(flt.get("score", "0")) in ("7", "8"):
        params["score"] = str(flt["score"])
    yr = flt.get("year", "Любой")
    if yr and yr != "Любой":
        params["season"] = yr.replace("-", "_")

    shiki_items = await _shiki_get("/animes", params) or []
    results = []
    if isinstance(shiki_items, list):
        for it in shiki_items:
            if not isinstance(it, dict):
                continue
            results.append({
                "id": 0,
                "shiki_id": it.get("id", 0),
                "alt": it.get("name") or "",
                "title": it.get("russian") or it.get("name") or "?",
                "year": (it.get("aired_on") or "")[:4] or "?",
                "score": it.get("score") or "?",
                "kind": (it.get("kind") or "tv").upper(),
            })

    # 2. Если Shikimori не ответил — фильтруем по локальному пулу Anixart
    if not results:
        all_genres = await _get_genres()
        gid = all_genres[0].get("id", 1) if all_genres else 1
        pool = await _fetch_genre_pool(gid)
        for item in pool:
            try:
                r_sc = float(item.get("rating") or 0)
            except Exception:
                r_sc = 0.0
            min_sc = float(flt.get("score") or 0)
            if r_sc < min_sc:
                continue
            results.append({
                "id": item.get("id", 0),
                "title": item.get("title_ru") or item.get("title_original") or "?",
                "year": item.get("year") or "?",
                "score": item.get("rating") or "?",
                "kind": "TV",
            })

    await _cache_set(cache_key, results, ttl=1800)
    return results


# ── Улучшение #14: Чат-рулетка и голосование «Что смотрим?» (/anime party) ────
ANIME_PARTY_SESSIONS: dict[int, dict] = {}


def _get_party_session(chat_id: int) -> dict:
    if chat_id not in ANIME_PARTY_SESSIONS:
        ANIME_PARTY_SESSIONS[chat_id] = {
            "candidates": [],   # [{"rid": int, "title": str, "added_by": str, "votes": set[int]}]
            "genre_name": "Любой жанр",
            "voting_active": False,
        }
    return ANIME_PARTY_SESSIONS[chat_id]


def _party_message_text(chat_id: int) -> str:
    sess = _get_party_session(chat_id)
    cands = sess["candidates"]
    lines = [
        "🎰 <b>Чат-рулетка и голосование «Что смотрим?»</b>",
        f"🎭 Жанр пула: <b>{html.escape(sess['genre_name'])}</b>\n",
    ]
    if not cands:
        lines.append(
            "Список кандидатов пока пуст!\n"
            "• Нажми <b>«➕ Предложить тайтл»</b> (или напиши <code>/anime party Название</code>)\n"
            "• Либо нажми <b>«🎲 Набрать 5 тайтлов по жанру»</b>\n"
            "• Затем запусти общее голосование или крути рулетку!"
        )
    else:
        lines.append("<b>Кандидаты на просмотр:</b>")
        for idx, c in enumerate(cands, start=1):
            v_cnt = len(c["votes"])
            v_str = f" — 🗳 <b>{v_cnt} гол.</b>" if sess["voting_active"] or v_cnt else ""
            lines.append(f"{idx}. 🎬 <b>{html.escape(c['title'])}</b> <i>(от {html.escape(c['added_by'])})</i>{v_str}")
    return "\n".join(lines)


def _party_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    sess = _get_party_session(chat_id)
    cands = sess["candidates"]
    rows = []

    if sess["voting_active"] and cands:
        for idx, c in enumerate(cands):
            v_cnt = len(c["votes"])
            rows.append([InlineKeyboardButton(
                text=f"🗳 [{v_cnt}] {c['title'][:34]}",
                callback_data=f"anix:party:vote:{idx}"
            )])
        rows.append([
            InlineKeyboardButton(text="🏆 Подвести итог голосования", callback_data="anix:party:finish"),
            InlineKeyboardButton(text="🎰 Крутить рулетку!", callback_data="anix:party:spin"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="➕ Предложить тайтл", callback_data="anix:party:add"),
            InlineKeyboardButton(text="🎲 Набрать 5 хитов", callback_data="anix:party:autofill"),
        ])
        if cands:
            rows.append([
                InlineKeyboardButton(text="📊 Запустить голосование", callback_data="anix:party:start_vote"),
                InlineKeyboardButton(text="🎰 Крутить рулетку!", callback_data="anix:party:spin"),
            ])
        rows.append([
            InlineKeyboardButton(text="🧹 Очистить список", callback_data="anix:party:clear"),
            InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Хэндлеры команд и медиа (/anime ...) ──────────────────────────────────────
router = Router(name="anixart")

HELP_TEXT = (
    "<b>🎬 Anixart × Shikimori × AI — Аниме-центр</b>\n\n"
    "<code>/anime &lt;запрос&gt;</code> — поиск с HUD-карточками\n"
    "<code>/anime profile</code> — твой Anime Wrapped (1080×1920 для Stories)\n"
    "<code>/anime ai &lt;вайб&gt;</code> — ИИ-подбор аниме по атмосфере и истории\n"
    "<code>/anime schedule</code> — интерактивный календарь онгоингов (ПН–ВС)\n"
    "<code>/anime filter</code> — умный конструктор фильтров\n"
    "<code>/anime party</code> — чат-рулетка и голосование «Что смотрим?»\n"
    "<code>/anime shiki</code> — двусторонняя синхронизация с Shikimori (OAuth2)\n"
    "<code>/anime random</code> — случайный релиз\n"
    "<code>/anime genres</code> — классический каталог по жанрам\n"
    "<code>/anime favs</code> | <code>history</code> | <code>top</code> | <code>login</code>\n\n"
    "📸 <b>Поиск по кадру:</b> отправь скриншот, гифку или видео-кружочек с подписью <code>/anime</code>!\n"
    "⚡ <b>Инлайн:</b> <code>@bot a:</code> (твои списки), <code>a:top</code>, <code>a:random</code> или <code>a:название</code>"
)


@router.message(Command("anime"))
async def cmd_anime(msg: Message, state: FSMContext):
    # Улучшение #4: если команда /anime отправлена с фото/гифкой/видео или реплаем на медиа
    has_direct_media = bool(msg.photo or msg.animation or msg.video or msg.video_note)
    has_reply_media = bool(
        msg.reply_to_message and (
            msg.reply_to_message.photo
            or msg.reply_to_message.animation
            or msg.reply_to_message.video
            or msg.reply_to_message.video_note
        )
    )
    raw_text = (msg.text or msg.caption or "").strip()
    args = raw_text.split(maxsplit=1)
    query = args[1].strip() if len(args) > 1 else ""

    if (has_direct_media or has_reply_media) and not query:
        await _do_trace_media(msg)
        return

    if not query:
        await msg.answer(HELP_TEXT, parse_mode="HTML", reply_markup=_help_keyboard())
        return

    parts = query.split(maxsplit=1)
    sub = parts[0].lower()
    sub_arg = parts[1].strip() if len(parts) > 1 else ""

    if sub == "random":
        await _do_random(msg)
    elif sub == "id" and sub_arg.isdigit():
        await _do_info(msg, int(sub_arg))
    elif sub in ("profile", "wrapped", "stat", "stats", "профиль"):
        await _do_profile(msg)
    elif sub in ("ai", "vibe", "подбор", "вайб"):
        if sub_arg:
            await _do_ai_recommend(msg, sub_arg)
        else:
            await msg.answer(
                "🧠 <b>ИИ-подбор аниме по вайбу</b>\n\n"
                "Опиши, чего хочется — например:\n"
                "<i>«хочу мрачное киберпанк-аниме на вечер без гарема»</i>\n"
                "или <i>«уютная повседневность про путешествия с красивой рисовкой»</i>:",
                parse_mode="HTML"
            )
            await state.set_state(AnimeStates.waiting_ai_vibe)
    elif sub in ("schedule", "calendar", "расписание", "календарь"):
        today_idx = datetime.datetime.now(config.TIMEZONE).weekday()
        await _render_schedule_view(msg, msg.from_user.id, today_idx, only_my_subs=False, edit=False)
    elif sub in ("filter", "filters", "фильтр"):
        flt = await asyncio.to_thread(_get_smart_filter_sync, msg.from_user.id)
        await msg.answer(_smart_filter_summary_text(flt), parse_mode="HTML",
                         reply_markup=_smart_filter_keyboard(flt))
    elif sub in ("party", "roulette", "vote", "рулетка", "голосование"):
        if sub_arg:
            await _party_add_candidate_by_query(msg, sub_arg)
        else:
            await msg.answer(_party_message_text(msg.chat.id), parse_mode="HTML",
                             reply_markup=_party_keyboard(msg.chat.id))
    elif sub in ("shiki", "shikimori", "шики"):
        await _do_shiki_menu(msg, state, sub_arg)
    elif sub == "genres":
        await _do_genres(msg)
    elif sub in ("favs", "favorites", "избранное", "закладки"):
        await _do_favs(msg)
    elif sub in ("history", "история"):
        await _do_history(msg)
    elif sub in ("top", "топ", "популярное"):
        await _do_top(msg)
    elif sub in ("login", "аккаунт", "account"):
        await _start_anime_login(msg, state)
    else:
        await _do_search(msg, query, offset=0)


# Перехват фото/гифок/видео с подписью /anime (на случай если фильтр Command не сработал на caption)
@router.message(
    (F.photo | F.animation | F.video) & F.caption.regexp(r"^/(?:anime|whatanime)\b")
)
async def handle_anime_media_caption(msg: Message):
    await _do_trace_media(msg)


# ── Улучшение #4: Обработчик поиска по скриншоту / гифке / кружочку ───────────
async def _do_trace_media(msg: Message):
    wait = await msg.reply("🔍 Анализирую кадр через <b>trace.moe</b> и <b>Gemini Vision</b>…", parse_mode="HTML")
    frame_bytes = await _extract_frame_bytes_from_message(msg)
    if not frame_bytes:
        await wait.edit_text("❌ Не удалось извлечь кадр из медиафайла.")
        return

    found = await _recognize_anime_by_frame(frame_bytes)
    if not found:
        await wait.edit_text("😔 Не удалось распознать аниме по этому кадру. Попробуй более чёткий скриншот.")
        return

    t_disp = html.escape(str(found.get("title_display") or found.get("title_romaji") or "Неизвестно"))
    t_rom = html.escape(str(found.get("title_romaji") or ""))
    ep = html.escape(str(found.get("episode") or "?"))
    tc = html.escape(str(found.get("timecode") or "—"))
    sim = found.get("similarity", 0)
    src = html.escape(str(found.get("source") or "trace.moe"))
    note = html.escape(str(found.get("note") or ""))

    header = (
        f"🎯 <b>Кадр распознан ({src}, точность {sim}%)</b>\n"
        f"🎬 <b>Тайтл:</b> {t_disp}" + (f" (<i>{t_rom}</i>)" if t_rom and t_rom != t_disp else "") + "\n"
        f"🎞 <b>Серия:</b> {ep}  •  ⏱ <b>Таймкод:</b> {tc}"
        + (f"\n💡 <i>{note}</i>" if note else "")
    )

    # Ищем релиз в Anixart по русскому и оригинальному названию
    rel = None
    for q_cand in (found.get("title_search"), found.get("title_romaji")):
        if not q_cand:
            continue
        content, _ = await _search_releases_cached(str(q_cand), offset=0, prefetch_next=False)
        if content:
            first_id = content[0].get("id")
            rel = (await _get_release(int(first_id))) if first_id else content[0]
            if rel:
                break

    await wait.delete()
    if rel:
        await _send_release(msg, rel, extra_prefix=header)
    else:
        await msg.reply(
            header + "\n\n<i>В каталоге Anixart точного совпадения не нашлось — попробуй кнопку поиска ниже:</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔎 Найти инлайн", switch_inline_query_current_chat=f"a:{t_disp[:25]}")
            ]])
        )


# ── Улучшение #2: Генерация карточки профиля «Anime Wrapped» (1080×1920) ──────
async def _collect_user_wrapped_stats(user_id: int, user_name: str) -> dict:
    favs = get_anime_favs(user_id)
    hist = get_anime_history(user_id, limit=100)
    rates = await asyncio.to_thread(_get_all_user_rates_sync, user_id)

    # Объединяем просмотренные тайтлы и считаем серии, часы и жанры
    all_rids = {int(r["rid"]) for r in rates} | {int(h["id"]) for h in hist} | {int(f["id"]) for f in favs}
    total_titles = max(len(all_rids), len(hist))

    total_eps = sum(max(1, int(r.get("episodes") or 0)) for r in rates)
    if total_eps < len(hist) * 6:
        total_eps = max(total_eps, len(hist) * 8 + len(favs) * 4)

    hours = round(total_eps * 24.0 / 60.0, 1)
    days = round(hours / 24.0, 1)

    scores = [int(r["score"]) for r in rates if int(r.get("score") or 0) > 0]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0   # 0 = пользователь ещё ничего не оценил

    genre_counts: dict[str, int] = {}
    for r in rates:
        for g in (r.get("genres") or "").split(","):
            g = g.strip().capitalize()
            if g:
                genre_counts[g] = genre_counts.get(g, 0) + 1

    # Если жанров в локальных оценках ещё мало — подтягиваем из первых 5 тайтлов истории/закладок
    if len(genre_counts) < 3:
        sample_ids = [int(x["id"]) for x in (favs[:3] + hist[:3]) if x.get("id")]
        for s_id in sample_ids[:5]:
            rel = await _get_release(s_id)
            if rel:
                for g in _extract_genres_list(rel):
                    g_cap = g.strip().capitalize()
                    if g_cap:
                        genre_counts[g_cap] = genre_counts.get(g_cap, 0) + 1

    if genre_counts:
        max_g = max(genre_counts.values())
        sorted_g = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:4]
        top_genres = [(name, max(20, int(round(cnt / max_g * 100)))) for name, cnt in sorted_g]
    else:
        top_genres = [("Экшен", 92), ("Фэнтези", 78), ("Драма", 64), ("Комедия", 48)]

    fav_titles = [f["title"] for f in favs if f.get("title")][:4]
    if not fav_titles:
        fav_titles = [r["title"] for r in rates if r.get("title")][:4]
    if not fav_titles:
        fav_titles = [h["title"] for h in hist if h.get("title")][:4]

    if hours >= 200:
        rank = "✦ Повелитель Хроник и Исекаев"
    elif hours >= 50:
        rank = "✦ Ветеран Сезонных Онгоингов"
    elif hours >= 10:
        rank = "✦ Ценитель Годных Тайтлов"
    else:
        rank = "✦ Начинающий Аниме-Странник"

    return {
        "user_name": user_name,
        "rank_title": rank,
        "hours": hours,
        "days": days,
        "total_titles": total_titles,
        "total_episodes": total_eps,
        "avg_score": avg_score,
        "top_genres": top_genres,
        "fav_titles": fav_titles,
        "fav_count": len(favs),
    }


async def _do_profile(ctx):
    user = ctx.from_user
    chat_id = ctx.message.chat.id if isinstance(ctx, CallbackQuery) else ctx.chat.id
    bot = ctx.bot
    wait = await _reply(ctx, "🎨 Собираю твою карточку <b>Anime Wrapped (1080×1920)</b>…")

    stats = await _collect_user_wrapped_stats(user.id, user.first_name or user.username or "Анимешник")

    # Пробуем скачать аватарку пользователя
    avatar_bytes = None
    try:
        photos = await bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count > 0 and photos.photos:
            f_info = await bot.get_file(photos.photos[0][-1].file_id)
            dl = await bot.download_file(f_info.file_path)
            avatar_bytes = dl.read() if hasattr(dl, "read") else bytes(dl)
    except Exception:
        pass

    # Интеграция с web_server._render_story_image (если есть и принимает anime-режим) + наш генератор 1080×1920
    img_bytes = await asyncio.to_thread(_render_anime_wrapped_sync, stats, avatar_bytes)

    caption = (
        f"📊 <b>Anime Wrapped — {html.escape(stats['user_name'])}</b>\n"
        f"🏅 Ранг: <b>{html.escape(stats['rank_title'])}</b>\n\n"
        f"⏱ <b>Время за просмотром:</b> {stats['hours']} ч ({stats['days']} сут.)\n"
        f"🎬 <b>Тайтлов в активе:</b> {stats['total_titles']} ({stats['total_episodes']} серий)\n"
        f"⭐ <b>Средняя оценка:</b> {stats['avg_score'] or '—'} / 10\n"
        f"❤️ <b>В закладках:</b> {stats['fav_count']}\n"
        f"🎭 <b>Топ жанров:</b> {', '.join(g for g, _ in stats['top_genres'])}\n\n"
        "<i>Карточка в формате 1080×1920 — идеально подходит для публикации в Telegram Stories!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❤️ Мои закладки", callback_data="anix:my_favs"),
            InlineKeyboardButton(text="🔄 Синхронизация Shikimori", callback_data="anix:shiki_menu"),
        ],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")],
    ])
    try:
        await wait.delete()
    except Exception:
        pass
    await bot.send_photo(
        chat_id, BufferedInputFile(img_bytes, "anime_wrapped_story.jpg"),
        caption=caption, parse_mode="HTML", reply_markup=kb
    )


# ── Улучшение #5: ИИ-подбор аниме по вайбу (/anime ai) ────────────────────────
@router.message(AnimeStates.waiting_ai_vibe)
async def process_ai_vibe_input(message: Message, state: FSMContext):
    vibe_query = (message.text or "").strip()
    await state.clear()
    if not vibe_query or vibe_query.lower() in ("отмена", "/cancel", "cancel"):
        await message.answer("Отменено.")
        return
    await _do_ai_recommend(message, vibe_query)


async def _do_ai_recommend(ctx, vibe_query: str):
    user_id = ctx.from_user.id
    wait = await _reply(ctx, f"🧠 Подбираю аниме под вайб: «<i>{html.escape(vibe_query)}</i>»…")

    from ai_handlers import get_gemini
    client = get_gemini()
    if not client:
        await wait.edit_text("❌ Gemini AI сейчас недоступен.")
        return

    hist = get_anime_history(user_id, limit=15)
    favs = get_anime_favs(user_id)
    rates = await asyncio.to_thread(_get_all_user_rates_sync, user_id)

    watched_names = list({
        x.get("title") for x in (favs[:10] + hist[:10] + rates[:10]) if x.get("title")
    })
    watched_context = ", ".join(watched_names[:15]) if watched_names else "пока пусто"

    prompt = (
        f"Запрос пользователя на подбор аниме: «{vibe_query}».\n"
        f"Пользователь уже смотрел / добавил в избранное: {watched_context}.\n\n"
        "Подбери 4 лучших аниме, идеально попадающих в этот запрос (не предлагай те, что уже есть в списке просмотренного).\n"
        "Верни СТРОГО JSON-массив из 4 объектов без markdown-обёрток:\n"
        '[{"title_ru": "Точное русское название", "title_orig": "Оригинальное название", '
        '"year": 2023, "why": "2 ярких предложения без спойлеров, почему это идеальное попадание в вайб"}]'
    )

    try:
        resp = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        raw = (resp.text or "").strip()
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        recs = _json.loads(m.group(0)) if m else []
    except Exception as e:
        logger.error(f"Ошибка /anime ai: {e}")
        await wait.edit_text("❌ Не удалось получить подборку от нейросети, попробуй ещё раз.")
        return

    if not recs:
        await wait.edit_text("😔 ИИ не смог сформулировать подборку. Попробуй уточнить запрос.")
        return

    # Параллельно ищем каждый рекомендованный тайтл в Anixart, чтобы дать прямые кнопки карточек
    async def _resolve_rec(item: dict):
        t_ru = item.get("title_ru") or ""
        t_or = item.get("title_orig") or ""
        for q in (t_ru, t_or):
            if not q:
                continue
            found, _ = await _search_releases_cached(q, offset=0, prefetch_next=False)
            if found:
                return {**item, "rid": found[0].get("id", 0)}
        return {**item, "rid": 0}

    resolved = await asyncio.gather(*[_resolve_rec(r) for r in recs[:5] if isinstance(r, dict)])

    lines = [f"🧠 <b>ИИ-подборка по вайбу:</b> «<i>{html.escape(vibe_query)}</i>»\n"]
    buttons = []
    for idx, item in enumerate(resolved, start=1):
        t_ru = html.escape(str(item.get("title_ru") or "Без названия"))
        yr = item.get("year") or "?"
        why = html.escape(str(item.get("why") or ""))
        rid = int(item.get("rid") or 0)
        lines.append(f"<b>{idx}. {t_ru} ({yr})</b>\n{why}\n")
        if rid:
            buttons.append([InlineKeyboardButton(
                text=f"🎬 Открыть «{item.get('title_ru', '')[:32]}»",
                callback_data=f"anix:info:{rid}"
            )])
        else:
            q_s = (item.get("title_ru") or item.get("title_orig") or "")
            buttons.append([InlineKeyboardButton(
                text=f"🔎 Найти «{q_s[:24]}»",
                callback_data=_search_cb(q_s, 0)
            )])

    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    await wait.edit_text("\n".join(lines), parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ── Улучшение #9: Меню OAuth2 Shikimori и двусторонняя синхронизация ──────────
async def _do_shiki_menu(ctx, state: FSMContext = None, code_arg: str = ""):
    user_id = ctx.from_user.id
    if code_arg:
        wait = await _reply(ctx, "🔐 Проверяю код авторизации Shikimori…")
        res = await shiki_exchange_code(user_id, code_arg)
        if res:
            await wait.edit_text(
                f"✅ Аккаунт Shikimori <b>{html.escape(res.get('nickname') or str(res.get('shiki_user_id')))}</b> успешно подключён!\n"
                "Теперь изменение статуса («Смотрю», «Просмотрено», «Брошено») и серий синхронизируется автоматически.",
                parse_mode="HTML"
            )
        else:
            await wait.edit_text("❌ Не удалось обменять код на токен. Проверь правильность кода и переменные SHIKIMORI_CLIENT_ID / SECRET.")
        return

    tok = await asyncio.to_thread(_get_shiki_token_sync, user_id)
    if tok and tok.get("access_token"):
        nick = html.escape(tok.get("shiki_nickname") or f"ID {tok.get('shiki_user_id')}")
        text = (
            f"🔄 <b>Shikimori OAuth2 подключён</b>\n"
            f"👤 Профиль: <b>{nick}</b>\n\n"
            "Все изменения статуса («Смотрю», «Просмотрено», «Брошено») и просмотренных серий "
            "в карточках аниме автоматически отправляются в твой список Shikimori и Anixart."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Импортировать мой список из Shikimori", callback_data="anix:shiki_import")],
            [InlineKeyboardButton(text="🔓 Отвязать Shikimori", callback_data="anix:shiki_unlink")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")],
        ])
        await _reply_kb(ctx, text, kb)
        return

    if not SHIKIMORI_CLIENT_ID or not SHIKIMORI_CLIENT_SECRET:
        await _reply(
            ctx,
            "ℹ️ Для OAuth2-синхронизации с Shikimori укажи <code>SHIKIMORI_CLIENT_ID</code> и "
            "<code>SHIKIMORI_CLIENT_SECRET</code> в переменных окружения BotHost.\n"
            "Локальное отслеживание статусов и серий («Смотрю», «Просмотрено», «Брошено») уже работает во всех карточках!"
        )
        return

    auth_url = get_shiki_oauth_authorize_url(user_id)
    text = (
        "🔄 <b>Привязка аккаунта Shikimori (OAuth2)</b>\n\n"
        "1. Нажми кнопку <b>«🔗 Авторизовать на Shikimori»</b> ниже и подтверди доступ.\n"
        "2. Скопируй выданный код авторизации и отправь его сюда (или командой <code>/anime shiki КОД</code>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Авторизовать на Shikimori", url=auth_url)],
        [InlineKeyboardButton(text="✍️ Ввести код авторизации", callback_data="anix:shiki_enter_code")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")],
    ])
    await _reply_kb(ctx, text, kb)


@router.message(AnimeStates.waiting_shiki_code)
async def process_shiki_code_msg(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    await state.clear()
    if not code or code.lower() in ("отмена", "/cancel", "cancel"):
        await message.answer("Отменено.")
        return
    await _do_shiki_menu(message, state, code_arg=code)


# ── Добавление кандидата в Чат-рулетку (#14) ──────────────────────────────────
async def _party_add_candidate_by_query(msg: Message, query: str):
    content, _ = await _search_releases_cached(query, offset=0, prefetch_next=False)
    if not content:
        await msg.answer(f"😔 Не нашёл тайтл по запросу «{html.escape(query)}».", parse_mode="HTML")
        return
    top = content[0]
    rid = int(top.get("id") or 0)
    title = top.get("title_ru") or top.get("title_original") or query
    sess = _get_party_session(msg.chat.id)
    if len(sess["candidates"]) >= 8:
        await msg.answer("⚠️ В пуле уже максимум (8) кандидатов! Запускай голосование или крути рулетку.")
        return
    if not any(c["rid"] == rid and rid > 0 for c in sess["candidates"]):
        sess["candidates"].append({
            "rid": rid,
            "title": title,
            "added_by": msg.from_user.first_name or "Участник",
            "votes": set(),
        })
    await msg.answer(_party_message_text(msg.chat.id), parse_mode="HTML",
                     reply_markup=_party_keyboard(msg.chat.id))


@router.message(AnimeStates.waiting_party_add)
async def process_party_add_msg(message: Message, state: FSMContext):
    q = (message.text or "").strip()
    await state.clear()
    if not q or q.lower() in ("отмена", "/cancel", "cancel"):
        await message.answer("Отменено.")
        return
    await _party_add_candidate_by_query(message, q)


# ── Базовые команды поиска, жанров, закладок, истории, топа и логина ──────────
async def _do_random(ctx):
    wait = await _reply(ctx, "🎲 Подбираю случайный релиз…")
    try:
        data = await _api_get("/release/random", {"extended_mode": "true"})
        rel  = data.get("release")
        if not rel:
            await wait.edit_text("❌ Не удалось получить случайный релиз.")
            return
        await wait.delete()
        await _send_release(ctx, rel)
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


async def _do_info(ctx, rid: int):
    wait = await _reply(ctx, f"🔍 Загружаю релиз #{rid}…")
    rel  = await _get_release(rid)
    if not rel:
        await wait.edit_text(f"❌ Релиз <code>{rid}</code> не найден.", parse_mode="HTML")
        return
    await wait.delete()
    await _send_release(ctx, rel)


async def _do_search(ctx, query: str, offset: int = 0):
    wait = await _reply(ctx, f"🔍 Ищу «{query}»…")
    try:
        content, total = await _search_releases_cached(query, offset=offset, prefetch_next=True)
        if not content:
            await wait.edit_text(f"😔 По запросу «{query}» ничего не найдено.")
            return

        results = content[:10]
        buttons = []
        for item in results:
            title  = (item.get("title_ru") or item.get("title_original") or "Без названия")[:35]
            year   = item.get("year") or "?"
            ep_rel = item.get("episodes_released", 0)
            ep_tot = item.get("episodes_total", 0) or "?"
            rid    = item.get("id", 0)
            buttons.append([InlineKeyboardButton(
                text=f"{title} ({year}, {ep_rel}/{ep_tot} эп.)",
                callback_data=f"anix:info:{rid}"
            )])

        nav_row = []
        if offset >= 10:
            nav_row.append(InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=_search_cb(query, max(0, offset - 10))
            ))
        if total > offset + 10 or len(content) >= 10:
            nav_row.append(InlineKeyboardButton(
                text="⏩ Ещё результаты",
                callback_data=_search_cb(query, offset + 10)
            ))
        if nav_row:
            buttons.append(nav_row)
        buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        suffix = f" (стр. {offset // 10 + 1})" if offset > 0 else ""
        await wait.edit_text(
            f"🔍 Результаты по «<b>{html.escape(query)}</b>»{suffix}:",
            parse_mode="HTML", reply_markup=kb
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка поиска: <code>{e}</code>", parse_mode="HTML")


async def _do_genres(ctx):
    wait   = await _reply(ctx, "🎭 Загружаю жанры…")
    genres = await _get_genres()
    if not genres:
        await wait.edit_text("❌ Не удалось загрузить жанры.")
        return

    buttons = [[InlineKeyboardButton(text="🎛 Открыть Умный конструктор фильтров", callback_data="anix:sfilter:open")]]
    row = []
    for g in genres[:30]:
        name = g.get("name") or str(g)
        gid  = g.get("id", 0)
        row.append(InlineKeyboardButton(text=name, callback_data=f"anix:genre:{gid}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

    await wait.edit_text(
        "🎭 <b>Выберите жанр или откройте Умный фильтр:</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


async def _do_favs(ctx):
    user_id = ctx.from_user.id
    favs = get_anime_favs(user_id)
    if not favs:
        await _reply(ctx, "🤍 Пока нет закладок. Открой любой релиз и нажми «В закладки».")
        return
    buttons = [[InlineKeyboardButton(text=f['title'][:40] or f"#{f['id']}", callback_data=f"anix:info:{f['id']}")]
               for f in favs[:20]]
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    await _reply_kb(ctx, f"<b>❤️ Твои закладки ({len(favs)}):</b>",
                    InlineKeyboardMarkup(inline_keyboard=buttons))


async def _do_history(ctx):
    user_id = ctx.from_user.id
    hist = get_anime_history(user_id, limit=20)
    if not hist:
        await _reply(ctx, "👀 История пока пуста.")
        return
    buttons = [[InlineKeyboardButton(text=h['title'][:40] or f"#{h['id']}", callback_data=f"anix:info:{h['id']}")]
               for h in hist]
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    await _reply_kb(ctx, "<b>👀 Недавно смотрел(а):</b>",
                    InlineKeyboardMarkup(inline_keyboard=buttons))


async def _do_top(ctx):
    top = await asyncio.to_thread(_stats_top_sync, 10)
    if not top:
        await _reply(ctx, "📊 Пока не набралось статистики — смотрите аниме, и топ появится!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["<b>🔥 Топ аниме в группе:</b>", ""]
    buttons = []
    for i, t in enumerate(top):
        title_display = t["title"] or f"#{t['id']}"
        medal = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{medal} {html.escape(title_display)} — 👀{t['views']} ❤️{t['favs']}")
        buttons.append([InlineKeyboardButton(text=title_display[:40], callback_data=f"anix:info:{t['id']}")])
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

    await _reply_kb(ctx, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


async def _start_anime_login(ctx, state: FSMContext):
    existing = get_anixart_token(ctx.from_user.id)
    if existing:
        text = (
            f"🔗 Уже привязан аккаунт <b>{html.escape(existing['login'])}</b>.\n"
            "Напиши /anime_unlink, чтобы отвязать."
        )
        await ctx.answer(text, parse_mode="HTML")
        return
    text = (
        "🔗 <b>Привязка аккаунта Anixart</b>\n\n"
        "Пароль нигде не сохраняется: бот обменивает его на токен и сразу удаляет сообщение.\n"
        "Напиши логин или e-mail от Anixart (или «отмена»):"
    )
    await ctx.answer(text, parse_mode="HTML")
    await state.set_state(AnimeStates.waiting_login)


@router.message(AnimeStates.waiting_login)
async def process_anime_login(message: Message, state: FSMContext):
    login = (message.text or "").strip()
    if login.lower() in ("отмена", "/cancel", "cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return
    await state.update_data(login=login)
    await state.set_state(AnimeStates.waiting_password)
    await message.answer("Теперь напиши пароль (сообщение с ним я сразу удалю):")


@router.message(AnimeStates.waiting_password)
async def process_anime_password(message: Message, state: FSMContext):
    data     = await state.get_data()
    login    = data.get("login", "")
    password = (message.text or "").strip()
    user_id  = message.from_user.id
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    if password.lower() in ("отмена", "/cancel", "cancel"):
        await message.answer("Отменено.")
        return

    wait = await message.answer("🔐 Пробую войти…")
    result = await _anixart_login(login, password)
    password = None
    if result.get("token"):
        save_anixart_token(user_id, result["token"], login)
        await wait.edit_text(f"✅ Аккаунт <b>{html.escape(login)}</b> привязан.", parse_mode="HTML")
    else:
        reason = result.get("reason", "unknown")
        await wait.edit_text(LOGIN_FAIL_MESSAGES.get(reason, LOGIN_FAIL_MESSAGES["unknown"]))


@router.message(Command("anime_unlink"))
async def cmd_anime_unlink(message: Message):
    remove_anixart_token(message.from_user.id)
    await message.answer("🔓 Аккаунт Anixart отвязан.")


# ── Callback-хэндлеры всех 15 функций ─────────────────────────────────────────
@router.callback_query(F.data == "anix:random")
async def cb_random(cq: CallbackQuery):
    await cq.answer("🎲 Подбираю…")
    await _do_random(cq)


@router.callback_query(F.data == "anix:profile_card")
async def cb_profile_card(cq: CallbackQuery):
    await cq.answer("📊 Собираю карточку…")
    await _do_profile(cq)


@router.callback_query(F.data == "anix:my_favs")
async def cb_my_favs(cq: CallbackQuery):
    await cq.answer()
    await _do_favs(cq)


@router.callback_query(F.data == "anix:ai_prompt")
async def cb_ai_prompt(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.answer(
        "🧠 <b>ИИ-подбор аниме по вайбу</b>\n\n"
        "Опиши, какое настроение или сюжет ищешь — например:\n"
        "<i>«хочу мрачное киберпанк-аниме на вечер без гарема»</i>:",
        parse_mode="HTML"
    )
    await state.set_state(AnimeStates.waiting_ai_vibe)


@router.callback_query(F.data.startswith("anix:info:"))
async def cb_info(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer(f"📄 Загружаю #{rid}…")
    await _do_info(cq, rid)


@router.callback_query(F.data.startswith("anix:search:"))
async def cb_search_page(cq: CallbackQuery):
    parts  = cq.data.split(":", 3)
    query  = parts[2]
    offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    await cq.answer(f"⏩ Страница {offset // 10 + 1}…")
    await _do_search(cq, query, offset=offset)


@router.callback_query(F.data.startswith("anix:sq:"))
async def cb_search_hashed(cq: CallbackQuery):
    """Поиск/пагинация по запросу, лежащему в кэше (кириллица и длинные запросы)."""
    parts = cq.data.split(":")
    offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    query = await _sq_get(parts[2]) if len(parts) > 2 else None
    if not isinstance(query, str) or not query:
        await cq.answer("⌛ Кнопка устарела — повтори поиск", show_alert=True)
        return
    await cq.answer(f"⏩ Страница {offset // 10 + 1}…" if offset else "🔍 Ищу…")
    await _do_search(cq, query, offset=offset)


@router.callback_query(F.data.startswith("anix:go:"))
async def cb_go(cq: CallbackQuery):
    """Тайтл без id Anixart (Shikimori / календарь): находим его в Anixart и сразу открываем HUD-карточку."""
    val = await _sq_get(cq.data.split(":")[2])
    if not (isinstance(val, list) and val and val[0]):
        await cq.answer("⌛ Кнопка устарела — открой список заново", show_alert=True)
        return
    title, alt = val[0], (val[1] if len(val) > 1 else "")
    await cq.answer("🎬 Открываю карточку…")
    rid = await find_release_id_by_title(title, alt)
    if rid:
        await _do_info(cq, rid)
    else:
        await _do_search(cq, title)   # в Anixart не нашёлся — покажем обычный поиск


@router.callback_query(F.data.startswith("anix:genre:"))
async def cb_genre(cq: CallbackQuery):
    gid = int(cq.data.split(":")[2])
    await cq.answer("🔍 Ищу…")
    wait = await cq.message.answer("🔍 Загружаю аниме по жанру…")
    await _render_genre_page(wait, gid, status_idx=0, year=0, offset=0)


@router.callback_query(F.data.startswith("anix:gf:"))
async def cb_genre_filtered(cq: CallbackQuery):
    _, _, gid, status_idx, year, offset = cq.data.split(":")
    await cq.answer()
    await _render_genre_page(cq.message, int(gid), int(status_idx), int(year), int(offset))


@router.callback_query(F.data.startswith("anix:fav:"))
async def cb_fav_toggle(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    user_id = cq.from_user.id
    if is_anime_fav(user_id, rid):
        remove_anime_fav(user_id, rid)
        await cq.answer("💔 Убрано из закладок")
        title_for_stats = ""
        delta = -1
    else:
        rel = await _get_release(rid) or {"id": rid, "title": ""}
        t_name = rel.get("title_ru") or rel.get("title_original") or rel.get("title") or ""
        save_anime_fav(user_id, {"id": rid, "title": t_name, "poster": _extract_poster_url(rel)})
        await cq.answer("❤️ Добавлено в закладки!")
        title_for_stats = t_name
        delta = 1
    asyncio.create_task(_stats_bump_fav(rid, title_for_stats, delta))
    rel = await _get_release(rid) or {"id": rid}
    try:
        await cq.message.edit_reply_markup(reply_markup=_release_keyboard(rel, user_id))
    except Exception:
        pass


# ── Улучшение #10: Подписка на конкретную студию озвучки ──────────────────────
@router.callback_query(F.data.startswith("anix:sub:"))
async def cb_sub_toggle(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    user_id = cq.from_user.id
    if is_anime_subscribed(user_id, rid):
        unsubscribe_anime(user_id, rid)
        await cq.answer("🔕 Подписка на новые серии отменена")
        rel = await _get_release(rid) or {"id": rid}
        try:
            await cq.message.edit_reply_markup(reply_markup=_release_keyboard(rel, user_id))
        except Exception:
            pass
        return

    await cq.answer("🎙 Выбери любимую озвучку")
    rel = await _get_release(rid) or {"id": rid}
    title = rel.get("title_ru") or rel.get("title_original") or f"#{rid}"
    cur_pref = await asyncio.to_thread(_get_sub_pref_sync, user_id, rid)
    cur_st = cur_pref.get("studio_key", "any")

    rows = []
    for st_key, meta in VOICEOVER_STUDIOS.items():
        mark = "✅ " if st_key == cur_st else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{meta['label']}",
            callback_data=f"anix:sub_voice:{rid}:{st_key}"
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="anix:close")])
    await cq.message.answer(
        f"🔔 <b>Подписка на «{html.escape(title)}»</b>\n\n"
        "Выбери студию озвучки — бот пришлёт уведомление именно тогда, "
        "когда новая серия выйдет в выбранном переводе:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("anix:sub_voice:"))
async def cb_sub_voice_select(cq: CallbackQuery):
    _, _, rid_s, studio_key = cq.data.split(":", 3)
    rid = int(rid_s)
    user_id = cq.from_user.id
    rel = await _get_release(rid) or {"id": rid, "title_ru": f"#{rid}", "episodes_released": 0}
    title = rel.get("title_ru") or rel.get("title_original") or f"#{rid}"
    ep_now = int(rel.get("episodes_released") or 0)

    subscribe_anime(user_id, {"id": rid, "title": title, "episodes_released": ep_now})
    await asyncio.to_thread(_set_sub_pref_sync, user_id, rid, studio_key, ep_now)
    studio_lbl = VOICEOVER_STUDIOS.get(studio_key, VOICEOVER_STUDIOS["any"])["label"]
    await cq.answer(f"🔔 Подписка оформлена ({studio_lbl})!")
    try:
        await cq.message.edit_text(
            f"✅ Подписка на <b>{html.escape(title)}</b> включена!\n"
            f"🎙 Выбранная озвучка: <b>{html.escape(studio_lbl)}</b>\n"
            f"⏱ Проверка выходов: каждые 30–40 минут.",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ── Улучшение #6: Кнопка «🤖 Вердикт ИИ (без спойлеров)» в персоне чата ──────
@router.callback_query(F.data.startswith("anix:vibe:"))
async def cb_vibe(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer("🤖 Собираю отзывы и готовлю вердикт…")
    rel = await _get_release(rid)
    if not rel:
        await cq.message.answer("❌ Не нашёл этот релиз.")
        return

    from ai_handlers import get_gemini, get_persona, PERSONAS, MOOD_PROMPT_SUFFIX
    client = get_gemini()
    if not client:
        await cq.message.answer("🤖 AI сейчас недоступен.")
        return

    chat_id = cq.message.chat.id
    persona_key = get_persona(chat_id)
    persona = PERSONAS.get(persona_key, PERSONAS.get("advisor", {"label": "🎯 Советник", "prompt": ""}))

    title = rel.get("title_ru") or rel.get("title_original") or "?"
    shiki_details = await _shiki_get_full_details(title)
    studio = _extract_studio(rel, shiki_details)
    genres = ", ".join(_extract_genres_list(rel, shiki_details)) or "—"
    rating = rel.get("rating") or (shiki_details or {}).get("score") or "?"
    desc = (rel.get("description") or (shiki_details or {}).get("description") or "")[:700]

    # Подтягиваем пару комментариев/отзывов с Shikimori, если есть topic_id
    reviews_text = ""
    topic_id = (shiki_details or {}).get("topic_id")
    if topic_id:
        comments = await _shiki_get("/comments", {"commentable_id": topic_id, "commentable_type": "Topic", "limit": 3})
        if isinstance(comments, list) and comments:
            snippets = [re.sub(r"\[/?[^\]]+\]", "", str(c.get("body") or ""))[:180] for c in comments if isinstance(c, dict)]
            reviews_text = " | ".join(s for s in snippets if s)

    mood_suffix = ""
    try:
        from duty_handlers import get_daily_mood
        mood_suffix = MOOD_PROMPT_SUFFIX.get(get_daily_mood(), "")
    except Exception:
        pass

    prompt = (
        f"{persona.get('prompt', '')}{mood_suffix}\n\n"
        f"Твоя задача — выдать честный вердикт по аниме «{title}» СТРОГО В ХАРАКТЕРЕ СВОЕЙ ПЕРСОНЫ ({persona.get('label', '')}).\n"
        f"Жанры: {genres}. Студия: {studio}. Оценка зрителей: {rating}/10.\n"
        f"Синопсис: {desc or 'нет описания'}.\n"
        + (f"Выжимка из отзывов зрителей: {reviews_text}\n" if reviews_text else "")
        + "\nНапиши 3–4 ёмких предложения БЕЗ СПОЙЛЕРОВ: стоит ли тратить время, в чём главный кайф или подвох, и кому точно зайдёт."
    )
    try:
        response = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = (response.text or "").strip()
        if text:
            await cq.message.answer(
                f"🤖 <b>Вердикт ИИ ({html.escape(persona.get('label', 'AI'))}):</b>\n"
                f"🎬 <i>{html.escape(title)}</i>\n\n{html.escape(text)}",
                parse_mode="HTML"
            )
        else:
            await cq.message.answer("🤖 AI не смог сформулировать вердикт, попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Ошибка AI-вердикта: {e}")
        await cq.message.answer("❌ AI сейчас недоступен, попробуй позже.")


# ── Улучшение #3: Кнопка «🔗 Франшиза» (хронология и порядок просмотра) ──────
@router.callback_query(F.data.startswith("anix:franchise:"))
async def cb_franchise(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer("🔗 Строю хронологию франшизы…")
    rel = await _get_release(rid)
    if not rel:
        await cq.message.answer("❌ Релиз не найден.")
        return

    await _send_franchise(cq.message, rel, cq.from_user.id)


async def _send_franchise(msg: Message, rel: dict, user_id: int):
    """Хронология франшизы с кнопками (общая для кнопки и ИИ)."""
    title = rel.get("title_ru") or rel.get("title_original") or "?"
    items = await _fetch_franchise_order(rel, user_id)

    lines = [
        f"🔗 <b>Хронология и порядок просмотра:</b>\n🎬 <i>{html.escape(title)}</i>\n",
        "<i>Легенда: ✅ просмотрено • 👀 смотрю • ❤️ в закладках • 📍 открыто сейчас</i>\n",
    ]
    buttons = []
    for it in items:
        cur_pin = " 📍" if it.get("is_current") else ""
        lines.append(
            f"{it['order']}. {it['mark']} <b>{html.escape(it['title'])}</b> "
            f"({it.get('year', '?')}, {html.escape(str(it.get('kind', 'ТВ')))})" + cur_pin
        )
        irid = int(it.get("rid") or 0)
        btn_lbl = f"{it['mark']} {it['order']}. {it['title'][:32]} ({it.get('year', '?')})"
        if irid > 0:
            buttons.append([InlineKeyboardButton(text=btn_lbl, callback_data=f"anix:info:{irid}")])
        else:
            buttons.append([InlineKeyboardButton(text=btn_lbl, callback_data=_go_cb(it.get("title")))])

    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    await msg.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ── Улучшение #8: Кнопка «🎵 OP / ED» (поиск, MP3 с обложкой и в плейлист) ────
@router.callback_query(F.data.startswith("anix:oped:"))
async def cb_oped(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer("🎵 Ищу опенинги и эндинги…")
    rel = await _get_release(rid)
    if not rel:
        await cq.message.answer("❌ Релиз не найден.")
        return

    title = rel.get("title_ru") or rel.get("title_original") or "?"
    themes = await _fetch_oped_themes(rel)

    lines = [
        f"🎵 <b>Опенинги и Эндинги (OP / ED):</b>\n🎬 <i>{html.escape(title)}</i>\n",
        "Нажми <b>«⬇️ MP3»</b>, чтобы скачать трек с обложкой и ID3-тегами, "
        "или <b>«➕ В плейлист»</b>, чтобы сохранить его в музыкальный плеер бота:",
    ]
    rows = []
    for idx, th in enumerate(themes[:6]):
        lbl = th.get("label") or f"Тема #{idx + 1}"
        lines.append(f"• <b>{html.escape(lbl)}</b>")
        rows.append([
            InlineKeyboardButton(text=f"⬇️ MP3: {lbl[:24]}", callback_data=f"anix:oped_dl:{rid}:{idx}"),
            InlineKeyboardButton(text="➕ В плейлист", callback_data=f"anix:oped_pl:{rid}:{idx}"),
        ])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    await cq.message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("anix:oped_dl:"))
async def cb_oped_download(cq: CallbackQuery):
    _, _, rid_s, idx_s = cq.data.split(":")
    rid, idx = int(rid_s), int(idx_s)
    await cq.answer("🎧 Качаю MP3 с обложкой…")

    rel = await _get_release(rid)
    if not rel:
        return
    themes = await _fetch_oped_themes(rel)
    if idx >= len(themes):
        await cq.message.answer("❌ Тема не найдена.")
        return
    theme = themes[idx]
    await _send_oped_audio(cq.message, rid, idx, theme, rel)


async def _send_oped_audio(msg: Message, rid: int, idx: int, theme: dict, rel: dict):
    """Скачивает MP3 опенинга/эндинга с тегами и обложкой и отправляет в чат (общая для кнопки и ИИ)."""
    status = await msg.answer(f"🎧 Скачиваю и тегирую: <b>{html.escape(theme.get('label', 'OP/ED'))}</b>…", parse_mode="HTML")

    mp3_path, track_meta = await _download_oped_mp3_with_tags(theme, rel)
    if not mp3_path:
        await status.edit_text("😔 Не удалось скачать MP3 для этой темы. Попробуй другую из списка.")
        return

    thumb_file = None
    if track_meta.get("_thumb_bytes"):
        thumb_file = BufferedInputFile(track_meta["_thumb_bytes"], filename="cover.jpg")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Добавить в плейлист «Anime OST»", callback_data=f"anix:oped_pl:{rid}:{idx}")
    ]])
    try:
        await msg.answer_audio(
            FSInputFile(mp3_path, filename=f"{track_meta.get('artist', 'Anime')} - {track_meta.get('title', 'OST')}.mp3"),
            title=track_meta.get("title") or "Opening",
            performer=track_meta.get("artist") or "Anime OST",
            duration=track_meta.get("duration_sec") or None,
            thumbnail=thumb_file,
            caption=f"🎵 <b>{html.escape(theme.get('label', 'Anime OST'))}</b>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await status.delete()
    except Exception as e:
        logger.warning(f"Ошибка отправки OP/ED аудио: {e}")
        await status.edit_text(f"❌ Ошибка при отправке MP3: <code>{html.escape(str(e)[:120])}</code>", parse_mode="HTML")
    finally:
        tmp_d = track_meta.get("_tmp_dir")
        if tmp_d and os.path.isdir(tmp_d):
            import shutil
            shutil.rmtree(tmp_d, ignore_errors=True)


@router.callback_query(F.data.startswith("anix:oped_pl:"))
async def cb_oped_playlist_add(cq: CallbackQuery):
    _, _, rid_s, idx_s = cq.data.split(":")
    rid, idx = int(rid_s), int(idx_s)
    rel = await _get_release(rid)
    if not rel:
        await cq.answer("❌ Релиз не найден")
        return
    themes = await _fetch_oped_themes(rel)
    if idx >= len(themes):
        await cq.answer("❌ Тема не найдена")
        return
    theme = themes[idx]
    title_ru = rel.get("title_ru") or rel.get("title_original") or "Anime"
    poster_url = (await shiki_get_poster_url(title_ru)) or _extract_poster_url(rel)
    query = theme.get("search_query") or theme.get("label") or title_ru

    track_info = {
        "id": f"anix_ost_{rid}_{idx}",
        "title": theme.get("song") or theme.get("label") or "OP/ED",
        "artist": theme.get("artist") or title_ru,
        "artwork_url": poster_url,
        "source": "YouTube",
    }
    try:
        import music_engine as _me
        if hasattr(_me, "search_multi"):
            found = await _me.search_multi(query, limit=1)
            if found and isinstance(found, list) and isinstance(found[0], dict):
                track_info.update({
                    "id": str(found[0].get("id") or track_info["id"]),
                    "title": found[0].get("title") or track_info["title"],
                    "artist": found[0].get("artist") or track_info["artist"],
                    "artwork_url": found[0].get("artwork_url") or poster_url,
                    "source": found[0].get("source") or "SoundCloud",
                })
    except Exception:
        pass

    save_playlist_track(cq.from_user.id, "Anime OST", track_info)
    save_music_fav(cq.from_user.id, track_info)
    await cq.answer("✅ Добавлено в плейлист «Anime OST» и в Избранное музыки!", show_alert=True)


# ── Улучшение #9: Управление статусом просмотра и синхронизация с Shikimori ───
async def _render_status_menu(msg: Message, user_id: int, rid: int, edit: bool = False):
    rel = await _get_release(rid) or {"id": rid, "title_ru": f"#{rid}"}
    title = rel.get("title_ru") or rel.get("title_original") or f"#{rid}"
    rate = await asyncio.to_thread(_get_user_rate_sync, user_id, rid) or {"status": "watching", "episodes": 0, "score": 0}
    cur_st = rate.get("status", "watching")
    cur_ep = rate.get("episodes", 0)
    cur_sc = int(rate.get("score") or 0)
    ep_tot = rel.get("episodes_total") or rel.get("episodes_released") or "?"

    rows = []
    st_row = []
    for k, meta in WATCH_STATUSES.items():
        mark = "• " if k == cur_st else ""
        st_row.append(InlineKeyboardButton(
            text=f"{mark}{meta['label']}", callback_data=f"anix:set_st:{rid}:{k}"
        ))
        if len(st_row) == 2:
            rows.append(st_row)
            st_row = []
    if st_row:
        rows.append(st_row)

    rows.append([
        InlineKeyboardButton(text=f"➕ +1 серия (сейчас {cur_ep}/{ep_tot})", callback_data=f"anix:inc_ep:{rid}"),
        InlineKeyboardButton(text="🔄 Настройки Shikimori", callback_data="anix:shiki_menu"),
    ])

    # Личная оценка 1–10 (улетает в Shikimori и идёт в средний балл Anime Wrapped)
    for lo, hi in ((1, 5), (6, 10)):
        rows.append([
            InlineKeyboardButton(
                text=(f"⭐{n}" if n == cur_sc else str(n)),
                callback_data=f"anix:rate:{rid}:{n}",
            ) for n in range(lo, hi + 1)
        ])
    if cur_sc:
        rows.append([InlineKeyboardButton(text="🗑 Снять оценку", callback_data=f"anix:rate:{rid}:0")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

    text = (
        f"📂 <b>Статус и синхронизация:</b>\n🎬 <b>{html.escape(title)}</b>\n\n"
        f"Текущий статус: <b>{WATCH_STATUSES.get(cur_st, {}).get('label', cur_st)}</b>\n"
        f"Просмотрено серий: <b>{cur_ep} / {ep_tot}</b>\n"
        f"Твоя оценка: <b>{('⭐ ' + str(cur_sc) + ' / 10') if cur_sc else '—'}</b>\n\n"
        "<i>Изменения мгновенно синхронизируются с твоим профилем Anixart и Shikimori (OAuth2):</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("anix:status_menu:"))
async def cb_status_menu(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer()
    await _render_status_menu(cq.message, cq.from_user.id, rid, edit=False)


@router.callback_query(F.data.startswith("anix:rate:"))
async def cb_rate(cq: CallbackQuery):
    _, _, rid_s, n_s = cq.data.split(":")
    rid = int(rid_s)
    score = max(0, min(10, int(n_s)))
    user_id = cq.from_user.id
    rel = await _get_release(rid) or {"id": rid}
    rate = await asyncio.to_thread(_get_user_rate_sync, user_id, rid)
    st_key = (rate or {}).get("status") or "watching"
    ep = int((rate or {}).get("episodes") or 0)

    res = await sync_rate_to_shikimori_and_anixart(user_id, rel, st_key, episodes=ep, score=score)
    badges = []
    if res.get("shiki"):
        badges.append("Shikimori ✅")
    if res.get("anixart"):
        badges.append("Anixart ✅")
    note = f" ({', '.join(badges)})" if badges else " (сохранено в профиле)"
    await cq.answer((f"⭐ Оценка {score}/10" if score else "🗑 Оценка снята") + note)
    await _render_status_menu(cq.message, user_id, rid, edit=True)


@router.callback_query(F.data.startswith("anix:set_st:"))
async def cb_set_status(cq: CallbackQuery):
    _, _, rid_s, st_key = cq.data.split(":")
    rid = int(rid_s)
    user_id = cq.from_user.id
    rel = await _get_release(rid) or {"id": rid}
    ep_Override = int(rel.get("episodes_released") or rel.get("episodes_total") or 0) if st_key == "completed" else None

    res = await sync_rate_to_shikimori_and_anixart(user_id, rel, st_key, episodes=ep_Override)
    st_lbl = WATCH_STATUSES.get(st_key, {}).get("label", st_key)
    sync_badges = []
    if res.get("shiki"):
        sync_badges.append("Shikimori ✅")
    if res.get("anixart"):
        sync_badges.append("Anixart ✅")
    sync_note = f" ({', '.join(sync_badges)})" if sync_badges else " (сохранено в профиле)"
    await cq.answer(f"{st_lbl}{sync_note}!", show_alert=False)
    try:
        await cq.message.edit_text(
            f"✅ Статус для <b>{html.escape(rel.get('title_ru') or f'#{rid}')}</b> обновлён: <b>{st_lbl}</b> "
            f"({res.get('episodes', 0)} эп.){sync_note}",
            parse_mode="HTML"
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("anix:inc_ep:"))
async def cb_inc_episode(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    user_id = cq.from_user.id
    rel = await _get_release(rid) or {"id": rid}
    rate = await asyncio.to_thread(_get_user_rate_sync, user_id, rid) or {"status": "watching", "episodes": 0}
    new_ep = int(rate.get("episodes") or 0) + 1
    tot = int(rel.get("episodes_total") or 0)
    st_key = "completed" if (tot > 0 and new_ep >= tot) else (rate.get("status") or "watching")

    res = await sync_rate_to_shikimori_and_anixart(user_id, rel, st_key, episodes=new_ep)
    await cq.answer(f"🎬 Отмечена серия #{new_ep}!")
    try:
        await cq.message.edit_text(
            f"🎬 Прогресс по <b>{html.escape(rel.get('title_ru') or f'#{rid}')}</b>: "
            f"<b>{res.get('episodes', new_ep)} эп.</b> ({WATCH_STATUSES.get(st_key, {}).get('label', st_key)})",
            parse_mode="HTML"
        )
    except Exception:
        pass


@router.callback_query(F.data == "anix:shiki_menu")
async def cb_shiki_menu(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _do_shiki_menu(cq, state)


@router.callback_query(F.data == "anix:shiki_enter_code")
async def cb_shiki_enter_code(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.answer("✍️ Отправь код авторизации, полученный на странице Shikimori (или «отмена»):")
    await state.set_state(AnimeStates.waiting_shiki_code)


@router.callback_query(F.data == "anix:shiki_unlink")
async def cb_shiki_unlink(cq: CallbackQuery):
    await asyncio.to_thread(_delete_shiki_token_sync, cq.from_user.id)
    await cq.answer("🔓 Shikimori отвязан")
    await cq.message.edit_text("🔓 Аккаунт Shikimori отвязан.")


@router.callback_query(F.data == "anix:shiki_import")
async def cb_shiki_import(cq: CallbackQuery):
    user_id = cq.from_user.id
    tok = await _get_valid_shiki_token(user_id)
    if not tok or not tok.get("shiki_user_id"):
        await cq.answer("❌ Сначала авторизуйся через /anime shiki", show_alert=True)
        return
    await cq.answer("📥 Импортирую список с Shikimori…")
    rates = await _shiki_request(
        "GET", "/v2/user_rates",
        query={"user_id": tok["shiki_user_id"], "target_type": "Anime", "limit": 40},
        access_token=tok["access_token"]
    ) or []
    imported = 0
    if isinstance(rates, list):
        for r in rates[:30]:
            if not isinstance(r, dict):
                continue
            sid = int(r.get("target_id") or 0)
            st = r.get("status") or "watching"
            eps = int(r.get("episodes") or 0)
            sc = int(r.get("score") or 0)
            if not sid:
                continue
            s_info = await _shiki_get_full_details("", shiki_id=sid)
            if not s_info:
                continue
            title = s_info.get("russian") or s_info.get("name") or f"Shiki #{sid}"
            fake_rel = {
                "id": sid + 10_000_000,
                "title_ru": title,
                "episodes_total": s_info.get("episodes") or eps,
                "rating": sc or s_info.get("score") or 0,
                "genres": [g.get("russian") or g.get("name") for g in (s_info.get("genres") or []) if isinstance(g, dict)],
            }
            await asyncio.to_thread(
                _upsert_user_rate_sync, user_id, fake_rel,
                status=st if st in WATCH_STATUSES else "watching",
                episodes=eps, score=sc, shiki_id=sid
            )
            imported += 1
    await cq.message.answer(f"✅ Импортировано и синхронизировано <b>{imported}</b> тайтлов из Shikimori!", parse_mode="HTML")


# ── Улучшение #11: Callbacks календаря онгоингов ──────────────────────────────
@router.callback_query(F.data.startswith("anix:sched:"))
async def cb_schedule(cq: CallbackQuery):
    _, _, day_s, sub_s = cq.data.split(":")
    if day_s == "today":
        day_idx = datetime.datetime.now(config.TIMEZONE).weekday()
    else:
        day_idx = int(day_s)
    only_subs = (sub_s == "1")
    await cq.answer()
    await _render_schedule_view(cq.message, cq.from_user.id, day_idx, only_my_subs=only_subs, edit=True)


# ── Улучшение #13: Callbacks Умного фильтра ───────────────────────────────────
@router.callback_query(F.data.startswith("anix:sf_"))
async def cb_smart_filter_toggle(cq: CallbackQuery):
    user_id = cq.from_user.id
    flt = await asyncio.to_thread(_get_smart_filter_sync, user_id)
    _, action, val = cq.data.split(":", 2)

    if action == "sf_g":
        g_list = list(flt.get("genres") or [])
        if val in g_list:
            g_list.remove(val)
        else:
            if len(g_list) >= 4:
                g_list.pop(0)
            g_list.append(val)
        flt["genres"] = g_list
    elif action == "sf_y":
        flt["year"] = val
    elif action == "sf_st":
        flt["status"] = val
    elif action == "sf_sc":
        flt["score"] = val
    elif action == "sf_kd":
        flt["kind"] = val

    await asyncio.to_thread(_save_smart_filter_sync, user_id, flt)
    await cq.answer("Фильтр обновлён")
    try:
        await cq.message.edit_text(
            _smart_filter_summary_text(flt), parse_mode="HTML",
            reply_markup=_smart_filter_keyboard(flt)
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("anix:sfilter:"))
async def cb_smart_filter_action(cq: CallbackQuery):
    user_id = cq.from_user.id
    parts = cq.data.split(":")
    cmd = parts[2]

    if cmd == "open":
        flt = await asyncio.to_thread(_get_smart_filter_sync, user_id)
        await cq.answer()
        await cq.message.answer(
            _smart_filter_summary_text(flt), parse_mode="HTML",
            reply_markup=_smart_filter_keyboard(flt)
        )
        return

    if cmd == "reset":
        flt = _default_smart_filter()
        await asyncio.to_thread(_save_smart_filter_sync, user_id, flt)
        await cq.answer("🔄 Фильтры сброшены")
        try:
            await cq.message.edit_text(
                _smart_filter_summary_text(flt), parse_mode="HTML",
                reply_markup=_smart_filter_keyboard(flt)
            )
        except Exception:
            pass
        return

    if cmd == "run":
        offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await cq.answer("🚀 Подбираю по фильтру…")
        flt = await asyncio.to_thread(_get_smart_filter_sync, user_id)
        items = await _execute_smart_filter(flt)
        page = items[offset:offset + 8]
        if not page:
            await cq.message.answer("😔 Ничего не нашлось под эти критерии. Попробуй ослабить фильтр.")
            return

        buttons = []
        for it in page:
            lbl = f"{it['title'][:32]} ({it.get('year', '?')} • ⭐{it.get('score', '?')})"
            if it.get("id"):
                buttons.append([InlineKeyboardButton(text=lbl, callback_data=f"anix:info:{it['id']}")])
            else:
                buttons.append([InlineKeyboardButton(text=lbl, callback_data=_go_cb(it.get("title"), it.get("alt", "")))])

        nav = []
        if offset >= 8:
            nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"anix:sfilter:run:{offset - 8}"))
        if len(items) > offset + 8:
            nav.append(InlineKeyboardButton(text="⏩ Ещё", callback_data=f"anix:sfilter:run:{offset + 8}"))
        if nav:
            buttons.append(nav)
        buttons.append([
            InlineKeyboardButton(text="🎛 Изменить фильтр", callback_data="anix:sfilter:open"),
            InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close"),
        ])

        text = f"🎛 <b>Найдено по Умному фильтру ({len(items)} тайтлов, стр. {offset // 8 + 1}):</b>"
        try:
            await cq.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except Exception:
            await cq.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ── Улучшение #14: Callbacks Чат-рулетки и голосования «Что смотрим?» ─────────
@router.callback_query(F.data.startswith("anix:party:"))
async def cb_party_action(cq: CallbackQuery, state: FSMContext):
    chat_id = cq.message.chat.id
    sess = _get_party_session(chat_id)
    parts = cq.data.split(":")
    act = parts[2]

    if act == "open":
        await cq.answer()
        await cq.message.answer(_party_message_text(chat_id), parse_mode="HTML",
                                reply_markup=_party_keyboard(chat_id))
        return

    if act == "add":
        await cq.answer()
        await cq.message.answer(
            "➕ Напиши название аниме, которое хочешь добавить в пул голосования/рулетки "
            "(или используй <code>/anime party Название</code>):",
            parse_mode="HTML"
        )
        await state.set_state(AnimeStates.waiting_party_add)
        return

    if act == "autofill":
        await cq.answer("🎲 Набираю 5 популярных тайтлов…")
        genres = await _get_genres()
        chosen_g = random.choice(genres[:12]) if genres else {"id": 1, "name": "Экшен"}
        pool = await _fetch_genre_pool(int(chosen_g.get("id") or 1))
        sample = random.sample(pool, min(5, len(pool))) if pool else []
        sess["genre_name"] = chosen_g.get("name") or "Популярное"
        sess["candidates"] = [
            {
                "rid": int(it.get("id") or 0),
                "title": it.get("title_ru") or it.get("title_original") or "Тайтл",
                "added_by": "🎲 Авто-пул",
                "votes": set(),
            }
            for it in sample
        ]
        sess["voting_active"] = False
        try:
            await cq.message.edit_text(_party_message_text(chat_id), parse_mode="HTML",
                                       reply_markup=_party_keyboard(chat_id))
        except Exception:
            pass
        return

    if act == "start_vote":
        if not sess["candidates"]:
            await cq.answer("Сначала добавь хотя бы 1 тайтл!", show_alert=True)
            return
        sess["voting_active"] = True
        await cq.answer("🗳 Голосование запущено!")
        try:
            await cq.message.edit_text(_party_message_text(chat_id), parse_mode="HTML",
                                       reply_markup=_party_keyboard(chat_id))
        except Exception:
            pass
        return

    if act == "vote":
        idx = int(parts[3])
        cands = sess["candidates"]
        if idx >= len(cands):
            await cq.answer()
            return
        uid = cq.from_user.id
        for i, c in enumerate(cands):
            if i != idx and uid in c["votes"]:
                c["votes"].discard(uid)
        if uid in cands[idx]["votes"]:
            cands[idx]["votes"].discard(uid)
            await cq.answer("Голос снят")
        else:
            cands[idx]["votes"].add(uid)
            await cq.answer(f"Голос за «{cands[idx]['title'][:24]}» учтён!")
        try:
            await cq.message.edit_text(_party_message_text(chat_id), parse_mode="HTML",
                                       reply_markup=_party_keyboard(chat_id))
        except Exception:
            pass
        return

    if act == "finish":
        cands = sess["candidates"]
        if not cands:
            await cq.answer("Пул пуст!")
            return
        winner = max(cands, key=lambda c: len(c["votes"]))
        sess["voting_active"] = False
        await cq.answer(f"🏆 Победитель: {winner['title'][:28]}!")
        header = (
            f"🏆 <b>Победитель голосования чата ({len(winner['votes'])} гол.):</b>\n"
            f"🎬 <b>{html.escape(winner['title'])}</b> <i>(предложил: {html.escape(winner['added_by'])})</i>"
        )
        rel = await _get_release(int(winner["rid"])) if winner.get("rid") else None
        if rel:
            await _send_release(cq, rel, extra_prefix=header)
        else:
            await cq.message.answer(header, parse_mode="HTML")
        return

    if act == "spin":
        cands = sess["candidates"]
        if not cands:
            await cq.answer("🎲 Кручу случайный релиз из каталога!")
            await _do_random(cq)
            return
        await cq.answer("🎰 Крутим рулетку…")
        winner = random.choice(cands)
        header = (
            f"🎰 <b>Рулетка выбрала для общего просмотра:</b>\n"
            f"🎬 <b>{html.escape(winner['title'])}</b> <i>(предложил: {html.escape(winner['added_by'])})</i>"
        )
        rel = await _get_release(int(winner["rid"])) if winner.get("rid") else None
        if rel:
            await _send_release(cq, rel, extra_prefix=header)
        else:
            await cq.message.answer(header, parse_mode="HTML")
        return

    if act == "clear":
        sess["candidates"] = []
        sess["voting_active"] = False
        await cq.answer("🧹 Список очищен")
        try:
            await cq.message.edit_text(_party_message_text(chat_id), parse_mode="HTML",
                                       reply_markup=_party_keyboard(chat_id))
        except Exception:
            pass


# ── Трейлер, список серий и отметка просмотренной серии ───────────────────────
@router.callback_query(F.data.startswith("anix:trailer:"))
async def cb_trailer(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer("🎬 Ищу трейлер…")
    rel = await _get_release(rid)
    if not rel:
        await cq.message.answer("❌ Не нашёл этот релиз.")
        return

    title = rel.get("title_ru") or rel.get("title_original") or "?"
    status = await cq.message.answer(f"🎬 Ищу трейлер «{html.escape(title)}»…", parse_mode="HTML")

    from media_downloader import MAX_VIDEO_MB, _cleanup
    filepath, info = await _fetch_trailer(rel)

    if not filepath:
        if info.get("_too_large"):
            text = f"❌ Трейлер нашёлся, но весит больше {MAX_VIDEO_MB} МБ — Telegram не пропустит."
        else:
            text = "😔 Не нашёл трейлер для этого релиза. Попробуй поискать вручную на YouTube."
        try:
            await status.edit_text(text)
        except Exception:
            await cq.message.answer(text)
        return

    try:
        await status.edit_text("🎬 Загружаю трейлер в Telegram…")
    except Exception:
        pass

    try:
        video_file = FSInputFile(filepath, filename="trailer.mp4")
        await cq.message.answer_video(
            video_file,
            caption=f"🎬 Трейлер: <b>{html.escape(title)}</b>",
            parse_mode="HTML",
            width=info.get("width") or None,
            height=info.get("height") or None,
            duration=int(info.get("duration") or 0) or None,
            supports_streaming=True,
        )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        try:
            await status.edit_text(f"❌ Не удалось отправить трейлер: <code>{html.escape(str(e)[:150])}</code>", parse_mode="HTML")
        except Exception:
            pass
    finally:
        _cleanup(filepath, info)


@router.callback_query(F.data.startswith("anix:eplist:"))
async def cb_episodes(cq: CallbackQuery):
    _, _, rid_s, offset_s = cq.data.split(":")
    rid, offset = int(rid_s), int(offset_s)
    await cq.answer()

    rel = await _get_release(rid)
    if not rel:
        await cq.message.answer("❌ Не нашёл этот релиз.")
        return

    ep_total = int(rel.get("episodes_released") or rel.get("episodes_total") or 0)
    if not ep_total:
        await cq.message.answer("😔 Пока нет вышедших серий.")
        return

    title = rel.get("title_ru") or rel.get("title_original") or "?"
    rate = await asyncio.to_thread(_get_user_rate_sync, cq.from_user.id, rid) or {}
    watched_ep = int(rate.get("episodes") or 0)

    kb = _episode_keyboard(rid, ep_total, offset, title=title, user_id=cq.from_user.id, watched_ep=watched_ep)
    note = (
        f"\n👁 Просмотрено тобой: <b>{watched_ep}/{ep_total} эп.</b>\n"
        "<i>Нажми на номер серии, чтобы открыть плеер и отметить её просмотренной</i>"
    )
    text = f"▶️ <b>{html.escape(title)}</b>\nДоступно серий: {ep_total}{note}"

    if cq.message.text and cq.message.text.startswith("▶️"):
        try:
            await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await cq.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("anix:watch_ep:"))
async def cb_watch_episode(cq: CallbackQuery):
    _, _, rid_s, ep_s = cq.data.split(":")
    rid, ep = int(rid_s), int(ep_s)
    user_id = cq.from_user.id
    rel = await _get_release(rid) or {"id": rid, "title_ru": f"#{rid}"}
    title = rel.get("title_ru") or rel.get("title_original") or f"#{rid}"
    tot = int(rel.get("episodes_total") or rel.get("episodes_released") or 0)
    st_key = "completed" if (tot > 0 and ep >= tot) else "watching"

    await sync_rate_to_shikimori_and_anixart(user_id, rel, st_key, episodes=ep)
    await cq.answer(f"✅ Серия {ep} отмечена просмотренной!")

    ep_total = int(rel.get("episodes_released") or rel.get("episodes_total") or ep)
    kb = _episode_keyboard(rid, ep_total, offset=((ep - 1) // _EP_PAGE_SIZE) * _EP_PAGE_SIZE,
                           title=title, user_id=user_id, watched_ep=ep)
    kb.inline_keyboard.insert(0, [InlineKeyboardButton(
        text=f"🌐 Смотреть {ep} серию на Anixart", url=_episode_url(rid, ep)
    )])
    try:
        await cq.message.edit_text(
            f"▶️ <b>{html.escape(title)}</b>\n"
            f"✅ Серия <b>#{ep}</b> отмечена просмотренной и синхронизирована!",
            parse_mode="HTML", reply_markup=kb
        )
    except Exception:
        pass


@router.callback_query(F.data == "anix:close")
async def cb_close(cq: CallbackQuery):
    await cq.answer()
    try:
        await cq.message.delete()
    except Exception:
        pass


# ── Улучшение #12: Прокачанный Inline-режим (a:, a:top, a:random, a:<запрос>) ─
def _build_inline_result_item(item: dict, prefix_tag: str = "", unique_suffix: str = ""):
    rid        = int(item.get("id") or 0)
    title_ru   = item.get("title_ru") or item.get("title") or item.get("title_original") or "Без названия"
    title_orig = item.get("title_original") or ""
    year       = item.get("year") or "?"
    st         = item.get("status", {})
    status     = st.get("name") if isinstance(st, dict) else str(st or "Аниме")
    ep_rel     = item.get("episodes_released") or item.get("episodes") or 0
    ep_tot     = item.get("episodes_total") or item.get("total_episodes") or "?"
    rating     = item.get("rating") or item.get("score") or 0
    poster_url = _extract_poster_url(item)

    display_title = f"{prefix_tag}{title_ru}" + (f" ({year})" if year != "?" else "")
    text = (
        f"<b>{html.escape(title_ru)}</b>\n"
        + (f"<i>{html.escape(title_orig)}</i>\n\n" if title_orig and title_orig != title_ru else "\n")
        + f"📅 {year} | 📊 {html.escape(status)}\n"
        f"⭐ {rating} {_stars_text(rating)} | 🎬 {ep_rel}/{ep_tot} эп.\n\n"
        f"🔗 https://anixart.tv/release/{rid}"
    )
    desc = f"{status} • {ep_rel}/{ep_tot} эп. • ⭐{rating}"
    res_id = f"{rid}_{unique_suffix}" if unique_suffix else str(rid or hashlib.md5(title_ru.encode()).hexdigest()[:10])

    cached_fid = get_cached_file_id(f"anix_poster_{rid}") if rid else None
    if cached_fid:
        return InlineQueryResultCachedPhoto(
            id=res_id, photo_file_id=cached_fid,
            title=display_title, description=desc,
            caption=text, parse_mode="HTML",
        )
    if poster_url:
        return InlineQueryResultPhoto(
            id=res_id, photo_url=poster_url, thumbnail_url=poster_url,
            title=display_title, description=desc,
            caption=text, parse_mode="HTML",
        )
    return InlineQueryResultArticle(
        id=res_id, title=display_title, description=desc,
        input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML"),
    )


async def inline_anime(iq: InlineQuery):
    """Маршрутизируется из main.py при префиксе 'a:'.
    - Пустой 'a:' → персональный список «Смотрю сейчас» и «Избранное»
    - 'a:top'     → топ сезона и топ группы
    - 'a:random'  → случайный тайтл
    - 'a:запрос'  → поиск с пагинацией"""
    raw   = (iq.query or "").strip()
    query = raw[2:].strip() if raw.lower().startswith("a:") else raw.strip()
    user_id = iq.from_user.id

    try:
        offset = int(iq.offset) if iq.offset else 0
    except ValueError:
        offset = 0

    q_low = query.lower()

    # 1. Пустой запрос @bot a: → «Смотрю сейчас» + «Избранное» пользователя
    if not query:
        rates = await asyncio.to_thread(_get_all_user_rates_sync, user_id)
        watching = [r for r in rates if r.get("status") == "watching"][:5]
        favs = get_anime_favs(user_id)[:5]
        hist = get_anime_history(user_id, limit=5)

        results = []
        seen_rids = set()
        for r in watching:
            rid = int(r.get("rid") or 0)
            if rid and rid not in seen_rids:
                seen_rids.add(rid)
                results.append(_build_inline_result_item(
                    {"id": rid, "title_ru": r["title"], "episodes": r["episodes"],
                     "total_episodes": r["total_episodes"], "score": r["score"],
                     "poster": r.get("poster", ""), "status": "👀 Смотрю сейчас"},
                    prefix_tag="👀 ", unique_suffix="w"
                ))
        for f in favs:
            rid = int(f.get("id") or 0)
            if rid and rid not in seen_rids:
                seen_rids.add(rid)
                results.append(_build_inline_result_item(
                    {"id": rid, "title_ru": f["title"], "poster": f.get("poster", ""), "status": "❤️ В избранном"},
                    prefix_tag="❤️ ", unique_suffix="f"
                ))
        for h in hist:
            rid = int(h.get("id") or 0)
            if rid and rid not in seen_rids and len(results) < 10:
                seen_rids.add(rid)
                results.append(_build_inline_result_item(
                    {"id": rid, "title_ru": h["title"], "status": "🕒 Недавно смотрел"},
                    prefix_tag="🕒 ", unique_suffix="h"
                ))

        # Если личный список пока пуст — показываем популярные онгоинги из прогретого кэша
        if not results:
            popular = await _cache_get("warmup_ongoing_pool") or []
            for it in popular[:8]:
                results.append(_build_inline_result_item(it, prefix_tag="🔥 ", unique_suffix="pop"))

        await iq.answer(
            results[:15], cache_time=5, is_personal=True,
            switch_pm_text="🎬 Мои списки | a:top | a:random",
            switch_pm_parameter="anime"
        )
        return

    # 2. @bot a:top → Топ сезона и группы
    if q_low in ("top", "топ"):
        popular = await _cache_get("warmup_ongoing_pool")
        if not popular:
            genres = await _get_genres()
            gid = genres[0].get("id", 1) if genres else 1
            popular = await _fetch_genre_pool(gid)
        results = [
            _build_inline_result_item(it, prefix_tag=f"🏆 #{i + 1} ", unique_suffix=f"top{i}")
            for i, it in enumerate((popular or [])[:12])
        ]
        await iq.answer(results, cache_time=60, switch_pm_text="🔥 Топ сезона", switch_pm_parameter="anime")
        return

    # 3. @bot a:random → Случайный релиз
    if q_low in ("random", "рандом", "случайное"):
        results = []
        try:
            data = await _api_get("/release/random", {"extended_mode": "true"}, retries=1)
            rel = data.get("release")
            if rel:
                results.append(_build_inline_result_item(rel, prefix_tag="🎲 ", unique_suffix=str(int(time.time()))))
        except Exception:
            pass
        await iq.answer(results, cache_time=1, is_personal=True,
                        switch_pm_text="🎲 Случайный тайтл", switch_pm_parameter="anime")
        return

    # 4. Обычный поиск @bot a:<запрос>
    content, total = await _search_releases_cached(query, offset=offset, prefetch_next=True)
    results = [_build_inline_result_item(item, unique_suffix=f"s{offset}_{i}") for i, item in enumerate(content[:10])]
    next_offset = str(offset + 10) if (total > offset + 10 or len(content) >= 10) else ""
    await iq.answer(
        results, cache_time=60, next_offset=next_offset,
        switch_pm_text="Поиск: a:<название> | a:top | a:random",
        switch_pm_parameter="anime"
    )


# ── Улучшение #10: Проверка новых серий с фильтром по любимой озвучке ─────────
_EPISODE_CHECK_CONCURRENCY = 5


def _release_has_voiceover(rel: dict, studio_key: str) -> bool:
    """Проверяет, вышла ли серия в выбранной пользователем студии озвучки."""
    if not studio_key or studio_key == "any" or studio_key not in VOICEOVER_STUDIOS:
        return True
    keywords = VOICEOVER_STUDIOS[studio_key]["keywords"]
    if not keywords:
        return True

    # Собираем текстовые поля переводчиков/озвучек из ответа релиза Anixart
    haystack_parts = []
    for key in ("translators", "dubbers", "voices", "types", "episodes", "last_view_episode"):
        val = rel.get(key)
        if val:
            haystack_parts.append(_json.dumps(val, ensure_ascii=False).lower())
    if not haystack_parts:
        # Если API не прислал детализацию студий в объекте релиза — не блокируем уведомление
        return True
    haystack = " ".join(haystack_parts)
    return any(kw in haystack for kw in keywords)


async def check_anime_episodes(bot):
    """Запускается из main.py (рекомендуемый интервал — каждые 30–40 минут)
    или встроенным фоновым тикером. Учитывает выбранную студию озвучки."""
    subs = get_all_anime_subscriptions()
    if not subs:
        return

    sem = asyncio.Semaphore(_EPISODE_CHECK_CONCURRENCY)

    async def _process(sub):
        async with sem:
            rid = int(sub["id"])
            user_id = sub["user_id"]
            rel = await _get_release(rid)
            if not rel:
                return
            new_ep = int(rel.get("episodes_released") or 0)
            last_ep = int(sub.get("last_episodes") or 0)
            if new_ep <= last_ep:
                return

            pref = await asyncio.to_thread(_get_sub_pref_sync, user_id, rid)
            studio_key = pref.get("studio_key", "any")
            if not _release_has_voiceover(rel, studio_key):
                return

            studio_lbl = VOICEOVER_STUDIOS.get(studio_key, VOICEOVER_STUDIOS["any"])["label"]
            title = rel.get("title_ru") or sub.get("title") or "?"
            player_url = _build_player_url(rid, title, new_ep, int(user_id) if str(user_id).isdigit() else 0)

            btn_row = [InlineKeyboardButton(text="👀 Карточка релиза", callback_data=f"anix:info:{rid}")]
            if player_url:
                btn_row.insert(0, InlineKeyboardButton(
                    text=f"📱 Смотреть {new_ep} эп.", web_app=WebAppInfo(url=player_url)
                ))
            else:
                btn_row.insert(0, InlineKeyboardButton(
                    text="🌐 На сайт", url=f"https://anixart.tv/release/{rid}"
                ))

            try:
                await bot.send_message(
                    user_id,
                    f"🔔 <b>Новая серия!</b>\n"
                    f"🎬 <b>{html.escape(title)}</b>\n"
                    f"🎞 Вышел эпизод: <b>{new_ep} эп.</b>\n"
                    f"🎙 Озвучка: <i>{html.escape(studio_lbl)}</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[btn_row])
                )
                update_anime_sub_episodes(user_id, rid, new_ep)
                await asyncio.to_thread(_set_sub_pref_sync, user_id, rid, studio_key, new_ep)
            except Exception as e:
                logger.warning(f"Не удалось уведомить {user_id} о «{title}»: {e}")

    await asyncio.gather(*[_process(sub) for sub in subs])


# ── Видеопоток для Mini App: Kodik (по shikimori_id) → фоллбэк Anixart API ───
# KODIK_TOKEN — токен Kodik API (переменная окружения BotHost). Без него работает только
# best-effort фоллбэк через Anixart API; если и он ничего не вернул, плеер покажет кнопку
# «Открыть на Anixart».
KODIK_TOKEN = os.getenv("KODIK_TOKEN", "")
KODIK_API_URL = os.getenv("KODIK_API_URL", "https://kodikapi.com").rstrip("/")


def _voice_key(label: str, vtype: str) -> str:
    """Сопоставляет название озвучки с ключом из VOICEOVER_STUDIOS (anilibria, shiza, subs …)."""
    if vtype == "subtitles":
        return "subs"
    low = (label or "").lower()
    for key, meta in VOICEOVER_STUDIOS.items():
        if key in ("any", "subs"):
            continue
        if any(k in low for k in meta["keywords"] if len(k) > 3):
            return key
    return ""


def _norm_iframe_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith("http") else ""


async def _kodik_voices(shiki_id: int) -> list[dict]:
    """Список озвучек/субтитров тайтла в Kodik. Один результат Kodik = одна озвучка со своей iframe-ссылкой."""
    if not (KODIK_TOKEN and shiki_id):
        return []
    cache_key = f"kodik_voices:{shiki_id}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached
    voices: list[dict] = []
    try:
        session = await _get_session()
        params = {
            "token": KODIK_TOKEN, "shikimori_id": shiki_id,
            "types": "anime,anime-serial", "with_episodes": "false",
        }
        async with session.get(f"{KODIK_API_URL}/search", params=params,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            r.raise_for_status()
            data = await r.json()
        seen = set()
        for it in data.get("results") or []:
            link = _norm_iframe_url(it.get("link") or "")
            tr = it.get("translation") or {}
            tid = tr.get("id")
            if not link or tid in seen:
                continue
            seen.add(tid)
            label = tr.get("title") or "Озвучка"
            vtype = tr.get("type") or "voice"
            voices.append({
                "id": str(tid), "label": label, "type": vtype,
                "key": _voice_key(label, vtype), "url": link,
                "episodes": int(it.get("last_episode") or it.get("episodes_count") or 0),
            })
    except Exception as e:
        logger.warning(f"Kodik search shiki#{shiki_id}: {e}")
        return []     # не кэшируем сбой
    voices.sort(key=lambda v: (-v["episodes"], v["label"].lower()))
    await _cache_set(cache_key, voices, ttl=3600 if voices else 300)
    return voices


async def _anixart_voices(rid: int) -> list[dict]:
    """Best-effort фоллбэк: /episode/{rid} → озвучки → источник → серии со ссылками на плеер.
    Формат ответов Anixart не документирован, поэтому разбор максимально терпимый."""
    cache_key = f"anix_voices:{rid}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached
    voices: list[dict] = []
    try:
        data = await _api_get(f"/episode/{rid}", retries=1)
        types_ = data.get("types") or data.get("content") or []
        for t in types_[:4]:
            tid = t.get("id")
            if tid is None:
                continue
            src_data = await _api_get(f"/episode/{rid}/{tid}", retries=1)
            sources = src_data.get("sources") or src_data.get("content") or []
            if not sources or sources[0].get("id") is None:
                continue
            ep_data = await _api_get(f"/episode/{rid}/{tid}/{sources[0]['id']}", retries=1)
            ep_urls: dict[str, str] = {}
            for e in ep_data.get("episodes") or ep_data.get("content") or []:
                pos = e.get("position") or e.get("number")
                url = _norm_iframe_url(e.get("url") or e.get("iframe") or e.get("link") or "")
                if pos and url:
                    ep_urls[str(pos)] = url
            if ep_urls:
                label = t.get("name") or "Озвучка"
                voices.append({
                    "id": f"a{tid}", "label": label, "type": "voice",
                    "key": _voice_key(label, "voice"), "url": "",
                    "episodes": len(ep_urls), "ep_urls": ep_urls,
                })
    except Exception as e:
        logger.debug(f"Anixart episodes fallback #{rid}: {e}")
        return []
    await _cache_set(cache_key, voices, ttl=3600 if voices else 300)
    return voices


def _voice_url(voice: dict, ep: int) -> str:
    if voice.get("ep_urls"):
        return voice["ep_urls"].get(str(ep)) or ""
    base = voice.get("url") or ""
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}episode={ep}&hide_selectors=true"


async def resolve_stream(rid: int, ep: int, studio: str = "", title: str = "", shiki_id: int = 0) -> dict:
    """Возвращает iframe-ссылку на конкретную серию и выбранную озвучку (используется /api/anime/stream)."""
    rel = await _get_release(rid) if rid else None
    t_ru = (rel or {}).get("title_ru") or title or ""
    t_or = (rel or {}).get("title_original") or ""
    if not shiki_id:
        for t in (t_or, t_ru):
            m = await _shiki_find_anime(t) if t else None
            if m and m.get("id"):
                shiki_id = int(m["id"])
                break

    voices = await _kodik_voices(shiki_id) if shiki_id else []
    source = "kodik" if voices else ""
    if not voices and rid:
        voices = await _anixart_voices(rid)
        source = "anixart" if voices else ""

    anix_url = f"https://anixart.tv/release/{rid}" if rid else ""
    if not voices:
        reason = ("Источник серий не настроен (нужен KODIK_TOKEN)" if not KODIK_TOKEN
                  else "Эта серия не нашлась в плеере")
        return {"ok": False, "error": reason, "fallback_url": anix_url}

    studio = (studio or "").strip()
    sel = (next((v for v in voices if v["id"] == studio), None)
           or next((v for v in voices if studio and v["key"] == studio), None)
           or voices[0])
    ep_total = int(sel.get("episodes") or (rel or {}).get("episodes_released") or 0)
    ep = max(1, min(ep, ep_total)) if ep_total else max(1, ep)
    url = _voice_url(sel, ep)
    if not url:
        return {"ok": False, "error": f"Серия {ep} недоступна в этой озвучке", "fallback_url": anix_url,
                "voices": [{k: v[k] for k in ("id", "label", "type", "episodes")} for v in voices],
                "selected": sel["id"], "episodes": ep_total}
    return {
        "ok": True, "source": source, "url": url, "episode": ep, "episodes": ep_total,
        "selected": sel["id"], "fallback_url": anix_url,
        "voices": [{k: v[k] for k in ("id", "label", "type", "episodes")} for v in voices],
    }


# ── Улучшение #7: Встроенный Telegram Mini App видеоплеер для web_server.py ───
ANIME_PLAYER_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Anixart Mini App Player</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { --bg: #0b0e1a; --panel: rgba(20, 26, 46, 0.85); --cyan: #00ebff; --pink: #ff46c3; --text: #f2f5ff; }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
  body { background: radial-gradient(circle at top left, #141c38, var(--bg)); color: var(--text); min-height: 100vh; padding: 14px; }
  .hud-card { background: var(--panel); border: 1px solid rgba(255,255,255,0.14); border-radius: 20px; padding: 16px; backdrop-filter: blur(16px); box-shadow: 0 10px 30px rgba(0,0,0,0.45); margin-bottom: 14px; }
  .title { font-size: 18px; font-weight: 700; margin-bottom: 4px; color: #fff; }
  .sub { font-size: 12px; color: #9ab0d9; margin-bottom: 12px; }
  .controls { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
  select, button { background: rgba(12, 16, 30, 0.9); color: #fff; border: 1px solid rgba(0, 235, 255, 0.4); border-radius: 12px; padding: 10px 12px; font-size: 14px; outline: none; }
  button.primary { background: linear-gradient(90deg, var(--cyan), var(--pink)); color: #080b14; font-weight: 700; border: none; cursor: pointer; }
  .player-wrap { position: relative; width: 100%; padding-top: 56.25%; border-radius: 16px; overflow: hidden; background: #05070e; border: 1px solid rgba(255,255,255,0.12); }
  .player-wrap iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
  .overlay { position: absolute; inset: 0; display: none; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 16px; text-align: center; font-size: 14px; color: #ffb3d9; }
  .overlay button { border-color: var(--pink); cursor: pointer; }
  .ep-grid { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; max-height: 160px; overflow-y: auto; }
  .ep-btn { min-width: 44px; padding: 8px 10px; border-radius: 10px; background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.12); color: #fff; font-size: 13px; cursor: pointer; }
  .ep-btn.active { background: var(--cyan); color: #080b14; font-weight: 700; border-color: var(--cyan); }
  .status-bar { margin-top: 10px; font-size: 13px; color: #7ef7c2; min-height: 18px; }
</style>
</head>
<body>
  <div class="hud-card">
    <div class="title" id="animeTitle">Загрузка тайтла…</div>
    <div class="sub" id="animeMeta">Anixart × Kodik WebApp Player</div>
    <div class="controls">
      <select id="studioSelect"><option value="">🎙 Озвучка загружается…</option></select>
      <button class="primary" id="markWatchedBtn">✅ Серия просмотрена</button>
    </div>
    <div class="player-wrap">
      <iframe id="videoFrame" allowfullscreen allow="autoplay; fullscreen"></iframe>
      <div class="overlay" id="overlay">
        <div id="overlayText"></div>
        <button id="openExternal">🌐 Открыть на Anixart</button>
      </div>
    </div>
    <div class="status-bar" id="statusBar"></div>
    <div class="ep-grid" id="epGrid"></div>
  </div>
<script>
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { tg.ready(); tg.expand(); }
  const params = new URLSearchParams(window.location.search);
  const rid = parseInt(params.get('rid') || '0', 10);
  const shikiId = params.get('shiki_id') || '';
  let curEp = parseInt(params.get('ep') || '1', 10);
  const uid = params.get('uid') || (tg && tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : '0');
  const titleParam = params.get('title') || ('Релиз #' + rid);
  const $ = (id) => document.getElementById(id);
  let curVoice = '';
  let epTotal = 0;
  let fallbackUrl = 'https://anixart.tv/release/' + rid;
  let loadSeq = 0;

  $('animeTitle').textContent = titleParam;

  function showOverlay(text) {
    $('overlayText').textContent = text;
    $('overlay').style.display = 'flex';
    $('videoFrame').removeAttribute('src');
  }

  function fillVoices(voices, selected) {
    const sel = $('studioSelect');
    const sig = voices.map(v => v.id).join(',');
    if (sel.dataset.sig !== sig) {
      sel.innerHTML = '';
      voices.forEach(v => {
        const o = document.createElement('option');
        o.value = v.id;
        o.textContent = (v.type === 'subtitles' ? '💬 ' : '🎙 ') + v.label + (v.episodes ? ' (' + v.episodes + ')' : '');
        sel.appendChild(o);
      });
      sel.dataset.sig = sig;
    }
    if (selected) sel.value = selected;
  }

  async function loadStream() {
    const seq = ++loadSeq;
    $('statusBar').textContent = '⏳ Ищу источник серии…';
    let d;
    try {
      const qs = new URLSearchParams({ rid: rid, ep: curEp, studio: curVoice, title: titleParam });
      if (shikiId) qs.set('shiki_id', shikiId);
      const r = await fetch('/api/anime/stream?' + qs.toString());
      d = await r.json();
    } catch (e) {
      d = { ok: false, error: 'Нет связи с сервером' };
    }
    if (seq !== loadSeq) return;
    if (d.fallback_url) fallbackUrl = d.fallback_url;
    if (d.voices && d.voices.length) { fillVoices(d.voices, d.selected); curVoice = d.selected; }
    if (d.episodes) epTotal = d.episodes;
    if (!d.ok) {
      showOverlay('😔 ' + (d.error || 'Источник не найден'));
      $('statusBar').textContent = '';
      renderEpButtons(epTotal || 24);
      return;
    }
    $('overlay').style.display = 'none';
    curEp = d.episode || curEp;
    $('videoFrame').src = d.url;
    const opt = $('studioSelect').selectedOptions[0];
    $('animeMeta').textContent = 'Серия ' + curEp + (epTotal ? ' из ' + epTotal : '') + ' • ' + (opt ? opt.textContent : '');
    $('statusBar').textContent = '';
    renderEpButtons(epTotal || 24);
    markProgress(curEp, true);
  }

  function renderEpButtons(total) {
    const grid = $('epGrid');
    grid.innerHTML = '';
    for (let i = 1; i <= total; i++) {
      const b = document.createElement('button');
      b.className = 'ep-btn' + (i === curEp ? ' active' : '');
      b.textContent = i;
      b.onclick = () => { curEp = i; loadStream(); };
      grid.appendChild(b);
    }
  }

  async function markProgress(ep, silent) {
    try {
      const r = await fetch('/api/anime/progress', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({user_id: uid, rid: rid, episode: ep, title: titleParam})
      });
      const d = await r.json();
      if (d.ok) { $('statusBar').textContent = '✓ Серия ' + ep + ' отмечена просмотренной'; }
    } catch (e) {}
  }

  $('studioSelect').onchange = () => { curVoice = $('studioSelect').value; loadStream(); };
  $('markWatchedBtn').onclick = () => markProgress(curEp, false);
  $('openExternal').onclick = () => { if (tg && tg.openLink) tg.openLink(fallbackUrl); else window.open(fallbackUrl, '_blank'); };
  loadStream();
</script>
</body>
</html>
"""


def register_anime_web_routes(app):
    """Регистрирует маршруты Mini App плеера (/anime/player, /api/anime/progress)
    и OAuth2-коллбэка Shikimori (/anime/shiki/callback) в aiohttp.web.Application из web_server.py."""
    from aiohttp import web

    if getattr(app, "_anixart_routes_registered", False):
        return

    async def handle_anime_player(request: web.Request):
        return web.Response(text=ANIME_PLAYER_HTML, content_type="text/html")

    async def handle_anime_progress(request: web.Request):
        try:
            body = await request.json()
            uid = int(body.get("user_id") or 0)
            rid = int(body.get("rid") or 0)
            ep = int(body.get("episode") or 1)
            title = str(body.get("title") or f"#{rid}")
            if uid and rid:
                rel = await _get_release(rid) or {"id": rid, "title_ru": title}
                await sync_rate_to_shikimori_and_anixart(uid, rel, "watching", episodes=ep)
            return web.json_response({"ok": True, "episode": ep})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_shiki_callback(request: web.Request):
        code = request.query.get("code", "").strip()
        state_uid = request.query.get("state", "").strip()
        if code and state_uid.isdigit():
            res = await shiki_exchange_code(int(state_uid), code)
            if res:
                return web.Response(
                    text=f"<html><body style='background:#0b0e1a;color:#fff;font-family:sans-serif;padding:40px;text-align:center'>"
                         f"<h2>✅ Shikimori ({html.escape(res.get('nickname') or '')}) успешно привязан к боту!</h2>"
                         f"<p>Можешь вернуться в Telegram.</p></body></html>",
                    content_type="text/html"
                )
        return web.Response(
            text=f"<html><body style='background:#0b0e1a;color:#fff;font-family:sans-serif;padding:40px;text-align:center'>"
                 f"<h2>Код авторизации Shikimori:</h2><code>{html.escape(code or 'не передан')}</code>"
                 f"<p>Скопируй его и отправь боту командой <b>/anime shiki КОД</b></p></body></html>",
            content_type="text/html"
        )

    async def handle_anime_stream(request: web.Request):
        q = request.query
        try:
            rid = int(q.get("rid") or 0)
            ep = int(q.get("ep") or 1)
            shiki_id = int(q.get("shiki_id") or 0)
        except ValueError:
            return web.json_response({"ok": False, "error": "Некорректные параметры"}, status=400)
        if rid <= 0 and shiki_id <= 0:
            return web.json_response({"ok": False, "error": "Не указан тайтл"}, status=400)
        try:
            data = await resolve_stream(rid, ep, studio=q.get("studio", ""), title=q.get("title", ""),
                                        shiki_id=shiki_id)
        except Exception as e:
            logger.warning(f"/api/anime/stream rid={rid}: {e}")
            return web.json_response({"ok": False, "error": "Ошибка сервера при поиске серии"}, status=500)
        return web.json_response(data)

    app.router.add_get("/anime/player", handle_anime_player)
    app.router.add_get("/api/anime/stream", handle_anime_stream)
    app.router.add_post("/api/anime/progress", handle_anime_progress)
    app.router.add_get("/anime/shiki/callback", handle_shiki_callback)
    app._anixart_routes_registered = True
    logger.info("📺 Маршруты Mini App видеоплеера (/anime/player) зарегистрированы в web_server")


# ── Улучшение #15: Фоновый прогрев кэша популярных онгоингов сезона ───────────
async def _warmup_popular_cache():
    """Предзагружает список жанров, календарь недели и популярные онгоинги сезона
    в anixart_cache.sqlite3 в фоновом режиме."""
    try:
        genres = await _get_genres()
        await _fetch_weekly_schedule()
        ongoing_shiki = await _shiki_get("/animes", {"status": "ongoing", "order": "popularity", "limit": 15}) or []
        pool = []
        if genres:
            first_gid = int(genres[0].get("id") or 1)
            pool = await _fetch_genre_pool(first_gid)
        if pool:
            await _cache_set("warmup_ongoing_pool", pool[:15], ttl=3600)
        elif isinstance(ongoing_shiki, list) and ongoing_shiki:
            mapped = [{
                "id": 0,
                "title_ru": a.get("russian") or a.get("name") or "?",
                "title_original": a.get("name") or "",
                "year": (a.get("aired_on") or "")[:4] or "2026",
                "rating": a.get("score") or 0,
                "episodes_released": a.get("episodes_aired") or 0,
                "episodes_total": a.get("episodes") or "?",
                "image": a.get("image") or {},
                "status": "Онгоинг",
            } for a in ongoing_shiki if isinstance(a, dict)]
            await _cache_set("warmup_ongoing_pool", mapped, ttl=3600)
        logger.info("🚀 Фоновый прогрев кэша онгоингов и жанров Anixart/Shikimori завершён")
    except Exception as e:
        logger.debug(f"Фоновый прогрев кэша пропущен: {e}")


def _try_auto_attach_web_routes():
    """Если web_server уже импортирован в процессе — автоматически подключает /anime/player."""
    ws = sys.modules.get("web_server")
    if not ws:
        return
    for attr in ("app", "web_app", "aiohttp_app"):
        candidate = getattr(ws, attr, None)
        if candidate is not None and hasattr(candidate, "router"):
            try:
                register_anime_web_routes(candidate)
                return
            except Exception:
                pass


# ── Утилиты ответа и точки входа для main.py ──────────────────────────────────
async def _reply(ctx, text: str) -> Message:
    if isinstance(ctx, CallbackQuery):
        return await ctx.message.answer(text, parse_mode="HTML")
    return await ctx.answer(text, parse_mode="HTML")


async def _reply_kb(ctx, text: str, kb: InlineKeyboardMarkup) -> Message:
    if isinstance(ctx, CallbackQuery):
        return await ctx.message.answer(text, parse_mode="HTML", reply_markup=kb)
    return await ctx.answer(text, parse_mode="HTML", reply_markup=kb)


def register_anixart_handlers(dp):
    dp.include_router(router)
    _try_auto_attach_web_routes()
    logger.info(
        "🎬 anixart_handler зарегистрирован (v6: HUD-карточки, Anime Wrapped 1080x1920, "
        "trace.moe+Gemini Vision, ИИ-вайб, вердикт персоны, Франшизы, OP/ED MP3, "
        "Shikimori OAuth2, Mini App плеер, Календарь, Умный фильтр, Чат-рулетка)"
    )


async def anixart_startup_selftest():
    """Вызывать из main.py при старте бота (до dp.start_polling)."""
    try:
        await asyncio.to_thread(_cache_db_init_sync)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось инициализировать sqlite-кэш anixart ({ANIXART_CACHE_DB}): {e}")

    _try_auto_attach_web_routes()

    try:
        data = await _api_get("/release/random", {"extended_mode": "true"}, retries=1)
        rel = data.get("release")
        if rel and rel.get("id"):
            logger.info(f"✅ Anixart API отвечает (тестовый релиз #{rel['id']})")
        else:
            logger.warning(f"⚠️ Anixart API на самотесте вернул неожиданный формат: {str(data)[:300]}")
    except Exception as e:
        logger.warning(f"⚠️ Anixart API недоступен на старте ({e}).")

    # Запускаем фоновый прогрев популярных онгоингов сезона в кэш
    asyncio.create_task(_warmup_popular_cache())