"""
instagram_viewer.py — доступ к Instagram через instagrapi.

Архитектура:
- Пул аккаунтов-доноров (ротация при 429 / LoginRequired / PleaseWaitFewMinutes)
- Сессии сохраняются в JSON-файлы (повторный login только при необходимости)
- Публичный интерфейс совпадает со старым файлом:
    get_user_info(username) → dict | None
    get_stories(username)   → list
    get_posts(username, after_cursor) → dict
  + всё из подписок (SQLite) — без изменений

Установка:
    pip install instagrapi

Конфигурация — переменные окружения (или .env):
    IG_ACCOUNTS=login1:password1,login2:password2,login3:password3
    IG_SESSION_DIR=/data/ig_sessions   # куда сохранять сессии (опционально)
    DB_DIR=/data                       # для instagram.db (как раньше)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── ЗАВИСИМОСТЬ ───────────────────────────────────────────────────────────────
try:
    from instagrapi import Client
    from instagrapi.exceptions import (
        LoginRequired,
        PleaseWaitFewMinutes,
        UserNotFound,
        PrivateAccount,
        ClientError,
        RateLimitError,
    )
    _INSTAGRAPI_OK = True
except ImportError:
    _INSTAGRAPI_OK = False
    logger.error("instagrapi не установлена! pip install instagrapi")


# ── КОНФИГ ────────────────────────────────────────────────────────────────────

def _parse_accounts() -> list[tuple[str, str]]:
    """Читаем IG_ACCOUNTS=login1:pass1,login2:pass2"""
    raw = os.getenv("IG_ACCOUNTS", "")
    accounts: list[tuple[str, str]] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        login, _, pwd = pair.partition(":")
        if login and pwd:
            accounts.append((login.strip(), pwd.strip()))
    return accounts


def _session_dir() -> Path:
    # Если явно задан — используем сразу, без проверки на запись
    # (BotHost монтирует /app/data с правами только для процесса бота)
    explicit = os.getenv("IG_SESSION_DIR")
    if explicit:
        p = Path(explicit)
        p.mkdir(parents=True, exist_ok=True)
        logger.info(f"[pool] session_dir = {p}  (IG_SESSION_DIR)")
        return p

    # Автоопределение: DATA_DIR (BotHost) → DB_DIR → /app/data → /data → рядом
    base_candidates = [
        os.getenv("DATA_DIR"),       # BotHost стандартная переменная
        os.getenv("DB_DIR"),
        "/app/data",
        "/data",
        os.path.dirname(os.path.abspath(__file__)),
    ]
    for base in base_candidates:
        if not base:
            continue
        p = Path(base) / "ig_sessions"
        try:
            p.mkdir(parents=True, exist_ok=True)
            test = p / ".wt"
            test.write_text("ok")
            test.unlink()
            logger.info(f"[pool] session_dir = {p}")
            return p
        except Exception:
            continue

    # Последний fallback
    p = Path("/app/data/ig_sessions")
    p.mkdir(parents=True, exist_ok=True)
    logger.info(f"[pool] session_dir = {p}  (fallback)")
    return p


# ── POOL КЛИЕНТОВ ─────────────────────────────────────────────────────────────

class _AccountPool:
    """
    Пул instagrapi.Client.
    При ошибках (429, LoginRequired) переключается на следующий аккаунт.
    """

    def __init__(self):
        self._accounts = _parse_accounts()
        self._clients: list[Client] = []
        self._current = 0
        self._lock = asyncio.Lock()
        self._initialized = False

    # ── инициализация (вызывается один раз при первом запросе) ──────────────

    def _make_client(self, login: str, pwd: str) -> Client:
        cl = Client()
        cl.delay_range = [2, 5]
        session_file = _session_dir() / f"{login}.json"

        if not session_file.exists():
            raise RuntimeError(
                f"Файл сессии для {login} не найден: {session_file}\n"
                f"Создай его через браузерные куки и положи в {session_file.parent}/"
            )

        # Загружаем настройки
        settings = json.loads(session_file.read_text())
        cl.set_settings(settings)

        # Инжектируем куки напрямую в requests-сессию
        cookies = settings.get("cookies", {})
        for name, value in cookies.items():
            cl.private.cookies.set(name, value, domain=".instagram.com")

        # Устанавливаем user_agent из сессии
        ua = settings.get("user_agent", "")
        if ua:
            cl.private.headers["User-Agent"] = ua

        # Устанавливаем Authorization header если есть sessionid
        sessionid = cookies.get("sessionid", "")
        if sessionid:
            cl.private.headers["Authorization"] = f"Bearer IGT:2:{sessionid}"

        # Читаем ds_user_id для логирования (не присваиваем user_id — read-only)
        ds_user_id = cookies.get("ds_user_id", "")
        logger.info(f"[pool] ✅ сессия загружена: {login} (ds_user_id={ds_user_id})")
        return cl

    def _init_all(self):
        if not _INSTAGRAPI_OK:
            return
        if not self._accounts:
            logger.error("[pool] IG_ACCOUNTS не задан — Instagram работать не будет")
            return
        for login, pwd in self._accounts:
            try:
                cl = self._make_client(login, pwd)
                self._clients.append(cl)
            except Exception as e:
                logger.error(f"[pool] не удалось залогинить {login}: {e}")
        self._initialized = True
        logger.info(f"[pool] готов, аккаунтов: {len(self._clients)}/{len(self._accounts)}")

    # ── получение клиента ────────────────────────────────────────────────────

    async def _ensure_init(self):
        if self._initialized:
            return
        async with self._lock:
            if not self._initialized:
                await asyncio.get_event_loop().run_in_executor(None, self._init_all)

    def _next_client(self) -> Optional[Client]:
        if not self._clients:
            return None
        self._current = (self._current + 1) % len(self._clients)
        return self._clients[self._current]

    def _current_client(self) -> Optional[Client]:
        if not self._clients:
            return None
        return self._clients[self._current % len(self._clients)]

    # ── выполнение запроса с ротацией ────────────────────────────────────────

    async def run(self, fn, *args, retries: int = 3, **kwargs):
        """
        Запустить fn(client, *args, **kwargs) в executor.
        При 429 / LoginRequired переключить аккаунт и повторить.
        """
        await self._ensure_init()
        if not self._clients:
            return None

        last_err = None
        for attempt in range(retries):
            cl = self._current_client()
            if cl is None:
                return None
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: fn(cl, *args, **kwargs)
                )
                return result
            except (PleaseWaitFewMinutes, RateLimitError) as e:
                logger.warning(f"[pool] 429 на акк #{self._current}, ротация... ({e})")
                cl = self._next_client()
                await asyncio.sleep(random.uniform(3, 8))
                last_err = e
            except LoginRequired as e:
                # Сессия протухла — ротируем на следующий аккаунт
                # (перелогин невозможен с забаненного IP)
                login = self._accounts[self._current % len(self._accounts)][0]
                logger.warning(
                    f"[pool] LoginRequired @{login} — сессия протухла, ротация.\n"
                    f"👉 Обнови куки для {login} и пересоздай JSON-файл сессии."
                )
                self._next_client()
                last_err = e
            except (UserNotFound, PrivateAccount) as e:
                # Не ошибка пула — пробрасываем сразу
                raise
            except ClientError as e:
                logger.warning(f"[pool] ClientError attempt {attempt}: {e}")
                self._next_client()
                await asyncio.sleep(2)
                last_err = e
            except Exception as e:
                logger.error(f"[pool] неожиданная ошибка: {e}")
                last_err = e
                break

        logger.error(f"[pool] все попытки исчерпаны, последняя ошибка: {last_err}")
        return None


_POOL = _AccountPool()


# ── КЭШ ПОСТОВ ────────────────────────────────────────────────────────────────

_POSTS_CACHE: dict = {}
_POSTS_CACHE_TTL = 600  # 10 минут


def _cache_set(username: str, posts: list) -> None:
    _POSTS_CACHE[username] = {"posts": posts, "ts": time.time()}


def _cache_get(username: str) -> list | None:
    e = _POSTS_CACHE.get(username)
    if not e:
        return None
    if time.time() - e["ts"] > _POSTS_CACHE_TTL:
        del _POSTS_CACHE[username]
        return None
    return e["posts"]


# ── КОНВЕРТЕРЫ instagrapi → наш формат ───────────────────────────────────────

def _user_to_dict(user) -> dict:
    """UserShort / UserInfo → наш dict."""
    try:
        followers  = getattr(user, "follower_count", 0) or 0
        following  = getattr(user, "following_count", 0) or 0
        posts_cnt  = getattr(user, "media_count", 0) or 0
        biography  = getattr(user, "biography", "") or ""
        is_private = getattr(user, "is_private", False)
        is_verified= getattr(user, "is_verified", False)
        full_name  = getattr(user, "full_name", "") or ""
        username   = getattr(user, "username", "") or ""
        pk         = str(getattr(user, "pk", "") or "")

        # Аватарка — берём HD, fallback обычная
        avatar_url = ""
        hd = getattr(user, "profile_pic_url_hd", None)
        sd = getattr(user, "profile_pic_url", None)
        if hd:
            avatar_url = str(hd)
        elif sd:
            avatar_url = str(sd)

        return {
            "id":          pk,
            "username":    username,
            "full_name":   full_name,
            "biography":   biography,
            "followers":   followers,
            "following":   following,
            "posts_count": posts_cnt,
            "avatar_url":  avatar_url,
            "is_private":  is_private,
            "is_verified": is_verified,
            "posts":       [],
        }
    except Exception as e:
        logger.error(f"_user_to_dict error: {e}")
        return {}


def _media_to_post(media) -> dict:
    """instagrapi Media → наш dict поста."""
    try:
        sc  = getattr(media, "code", "") or str(getattr(media, "pk", ""))
        pid = str(getattr(media, "pk", sc))
        ts  = int(getattr(media, "taken_at", 0).timestamp() if getattr(media, "taken_at", None) else 0)
        cap = getattr(media, "caption_text", "") or ""
        likes    = getattr(media, "like_count", 0) or 0
        comments = getattr(media, "comment_count", 0) or 0

        media_type = getattr(media, "media_type", 1)  # 1=photo, 2=video, 8=album

        def _best_url(resource) -> str:
            """Наилучший URL из ресурса."""
            # Для видео
            vv = getattr(resource, "video_url", None)
            if vv:
                return str(vv)
            # Для фото — берём наибольшее разрешение из thumbnail_url
            tu = getattr(resource, "thumbnail_url", None)
            if tu:
                return str(tu)
            return ""

        urls: list[dict] = []

        if media_type == 8:  # альбом
            resources = getattr(media, "resources", []) or []
            for r in resources:
                r_type = getattr(r, "media_type", 1)
                if r_type == 2:
                    urls.append({"type": "video",
                                 "url":   str(getattr(r, "video_url", "") or ""),
                                 "thumb": str(getattr(r, "thumbnail_url", "") or "")})
                else:
                    u = str(getattr(r, "thumbnail_url", "") or "")
                    urls.append({"type": "photo", "url": u, "thumb": u})
        elif media_type == 2:  # видео
            vurl  = str(getattr(media, "video_url", "") or "")
            thumb = str(getattr(media, "thumbnail_url", "") or "")
            urls.append({"type": "video", "url": vurl, "thumb": thumb})
        else:  # фото
            u = str(getattr(media, "thumbnail_url", "") or "")
            urls.append({"type": "photo", "url": u, "thumb": u})

        return {
            "id":            pid,
            "shortcode":     sc,
            "timestamp":     ts,
            "caption":       cap[:500],
            "like_count":    likes,
            "comment_count": comments,
            "media":         urls,
        }
    except Exception as e:
        logger.error(f"_media_to_post error: {e}")
        return {}


def _story_to_dict(item) -> dict:
    """StoryItem → наш dict истории."""
    try:
        pk  = str(getattr(item, "pk", "") or "")
        mtype = getattr(item, "media_type", 1)
        is_video = (mtype == 2)

        if is_video:
            url   = str(getattr(item, "video_url", "") or "")
            thumb = str(getattr(item, "thumbnail_url", "") or "")
            dur   = float(getattr(item, "video_duration", 15) or 15)
        else:
            url   = str(getattr(item, "thumbnail_url", "") or "")
            thumb = url
            dur   = 5.0

        return {
            "id":       pk,
            "type":     "video" if is_video else "photo",
            "url":      url,
            "thumb":    thumb,
            "duration": dur,
        }
    except Exception as e:
        logger.error(f"_story_to_dict error: {e}")
        return {}


# ── ПУБЛИЧНЫЙ API ─────────────────────────────────────────────────────────────

async def get_user_info(username: str) -> dict | None:
    """Получить инфо о пользователе Instagram."""
    username = username.lower().strip("@").strip()
    if not username:
        return None

    def _fetch(cl: Client):
        user = cl.user_info_by_username(username)
        return _user_to_dict(user)

    try:
        result = await _POOL.run(_fetch)
        if result:
            logger.info(f"✅ get_user_info @{username}: {result.get('followers')} фолловеров")
        return result
    except UserNotFound:
        logger.warning(f"UserNotFound: @{username}")
        return None
    except PrivateAccount:
        logger.info(f"PrivateAccount: @{username}")
        # Возвращаем минимальный dict с is_private=True
        return {"id": "", "username": username, "full_name": username,
                "biography": "", "followers": 0, "following": 0, "posts_count": 0,
                "avatar_url": "", "is_private": True, "is_verified": False, "posts": []}
    except Exception as e:
        logger.error(f"get_user_info @{username}: {e}")
        return None


async def get_stories(username: str) -> list:
    """Получить активные истории пользователя."""
    info = await get_user_info(username)
    if not info or info.get("is_private"):
        return []

    user_id = info.get("id", "")
    if not user_id:
        return []

    def _fetch(cl: Client):
        return cl.user_stories(int(user_id))

    try:
        items = await _POOL.run(_fetch)
        if not items:
            return []
        stories = [_story_to_dict(s) for s in items]
        stories = [s for s in stories if s.get("url")]
        logger.info(f"✅ get_stories @{username}: {len(stories)} шт.")
        return stories
    except PrivateAccount:
        return []
    except Exception as e:
        logger.error(f"get_stories @{username}: {e}")
        return []


async def get_posts(username: str, after_cursor: str = "") -> dict:
    """
    Получить посты пользователя.
    after_cursor — числовой offset (строка) или пустая строка для начала.
    """
    info = await get_user_info(username)
    if not info:
        return {"posts": [], "next_cursor": "", "has_more": False}
    if info.get("is_private"):
        return {"posts": [], "next_cursor": "", "has_more": False, "private": True}

    user_id = info.get("id", "")
    offset = int(after_cursor) if after_cursor.isdigit() else 0

    # Кэш
    all_posts = _cache_get(username)

    if all_posts is None:
        def _fetch(cl: Client):
            # Загружаем до 33 постов (3 страницы × 12) для кэша
            medias = cl.user_medias(int(user_id), amount=33)
            return [_media_to_post(m) for m in medias if m]

        try:
            all_posts = await _POOL.run(_fetch) or []
            # Фильтрация пустых
            all_posts = [p for p in all_posts if p.get("id")]
            if all_posts:
                _cache_set(username, all_posts)
                logger.info(f"📦 get_posts @{username}: {len(all_posts)} постов в кэш")
        except PrivateAccount:
            return {"posts": [], "next_cursor": "", "has_more": False, "private": True}
        except Exception as e:
            logger.error(f"get_posts @{username}: {e}")
            return {"posts": [], "next_cursor": "", "has_more": False, "user": info}

    chunk    = all_posts[offset: offset + 10]
    has_more = (offset + 10) < len(all_posts)
    next_cur = str(offset + 10) if has_more else ""

    return {
        "posts":        chunk,
        "next_cursor":  next_cur,
        "has_more":     has_more,
        "total_cached": len(all_posts),
        "user":         info,
    }


# ── АВАТАРКА (байты) ──────────────────────────────────────────────────────────

async def get_avatar_bytes(username: str) -> bytes | None:
    """Скачать аватарку через instagrapi (надёжнее, чем прямой URL)."""
    import tempfile

    def _fetch(cl: Client):
        user = cl.user_info_by_username(username)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = cl.user_download_profile_pic(user.pk, folder=tmpdir)
            with open(path, "rb") as f:
                return f.read()

    try:
        return await _POOL.run(_fetch)
    except Exception as e:
        logger.error(f"get_avatar_bytes @{username}: {e}")
        return None


# ── БД (подписки — без изменений) ────────────────────────────────────────────

def _db_path() -> str:
    for candidate in [os.getenv("DB_DIR"), "/data", "/app/data",
                      os.path.dirname(os.path.abspath(__file__))]:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            test = os.path.join(candidate, ".wt")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(candidate, "instagram.db")
        except Exception:
            continue
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "instagram.db")


IG_DB = _db_path()


def init_ig_db():
    conn = sqlite3.connect(IG_DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ig_subscriptions (
        user_id     TEXT NOT NULL,
        ig_username TEXT NOT NULL,
        sub_type    TEXT NOT NULL,
        last_seen   TEXT DEFAULT '',
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, ig_username, sub_type)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ig_sent (
        user_id     TEXT NOT NULL,
        media_id    TEXT NOT NULL,
        sent_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, media_id)
    )""")
    conn.commit()
    conn.close()
    logger.info("🟢 Instagram DB инициализирована")


def ig_subscribe(user_id: str, ig_username: str, sub_type: str, last_seen: str = "") -> bool:
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ig_subscriptions (user_id, ig_username, sub_type, last_seen) VALUES (?,?,?,?)",
            (str(user_id), ig_username, sub_type, last_seen)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"ig_subscribe error: {e}")
        return False
    finally:
        conn.close()


def ig_unsubscribe(user_id: str, ig_username: str, sub_type: str) -> bool:
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    conn.execute(
        "DELETE FROM ig_subscriptions WHERE user_id=? AND ig_username=? AND sub_type=?",
        (str(user_id), ig_username, sub_type)
    )
    conn.commit()
    conn.close()
    return True


def ig_get_subscriptions(user_id: str) -> list:
    conn = sqlite3.connect(IG_DB)
    rows = conn.execute(
        "SELECT ig_username, sub_type, last_seen FROM ig_subscriptions WHERE user_id=? ORDER BY ig_username",
        (str(user_id),)
    ).fetchall()
    conn.close()
    return [{"ig_username": r[0], "sub_type": r[1], "last_seen": r[2]} for r in rows]


def ig_is_subscribed(user_id: str, ig_username: str, sub_type: str) -> bool:
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    row = conn.execute(
        "SELECT 1 FROM ig_subscriptions WHERE user_id=? AND ig_username=? AND sub_type=?",
        (str(user_id), ig_username, sub_type)
    ).fetchone()
    conn.close()
    return row is not None


def ig_update_last_seen(user_id: str, ig_username: str, sub_type: str, last_seen: str):
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    conn.execute(
        "UPDATE ig_subscriptions SET last_seen=? WHERE user_id=? AND ig_username=? AND sub_type=?",
        (last_seen, str(user_id), ig_username, sub_type)
    )
    conn.commit()
    conn.close()


def ig_all_subscriptions() -> list:
    conn = sqlite3.connect(IG_DB)
    rows = conn.execute(
        "SELECT user_id, ig_username, sub_type, last_seen FROM ig_subscriptions ORDER BY ig_username"
    ).fetchall()
    conn.close()
    return [{"user_id": r[0], "ig_username": r[1], "sub_type": r[2], "last_seen": r[3]} for r in rows]


def ig_mark_sent(user_id: str, media_id: str):
    conn = sqlite3.connect(IG_DB)
    conn.execute("INSERT OR IGNORE INTO ig_sent (user_id, media_id) VALUES (?,?)", (str(user_id), media_id))
    conn.commit()
    conn.close()


def ig_already_sent(user_id: str, media_id: str) -> bool:
    conn = sqlite3.connect(IG_DB)
    row = conn.execute(
        "SELECT 1 FROM ig_sent WHERE user_id=? AND media_id=?",
        (str(user_id), media_id)
    ).fetchone()
    conn.close()
    return row is not None
