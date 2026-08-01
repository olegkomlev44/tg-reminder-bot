"""
instagram_viewer.py — доступ к Instagram через instagrapi.

Архитектура:
- Пул аккаунтов-доноров (ротация при 429 / LoginRequired / PleaseWaitFewMinutes)
- Сессии сохраняются в JSON-файлы (повторный login только при необходимости)
- Публичный интерфейс:
    get_user_info(username) → dict | None
    get_stories(username)   → list
    get_posts(username, after_cursor) → dict
    validate_session(login) → bool          ← НОВОЕ
    get_sessions_status()   → list[dict]    ← НОВОЕ
  + всё из подписок (SQLite)

Установка:
    pip install instagrapi

Конфигурация (env / .env):
    IG_ACCOUNTS=login1:password1,login2:password2
    IG_SESSION_DIR=/data/ig_sessions
    IG_ADMIN_IDS=123456,789012
    DB_DIR=/data
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── ЗАВИСИМОСТЬ ───────────────────────────────────────────────────────────────
try:
    from instagrapi import Client
    from instagrapi.exceptions import (
        ClientError,
        LoginRequired,
        PleaseWaitFewMinutes,
        PrivateAccount,
        RateLimitError,
        UserNotFound,
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
    explicit = os.getenv("IG_SESSION_DIR")
    if explicit:
        p = Path(explicit)
        p.mkdir(parents=True, exist_ok=True)
        logger.info(f"[pool] session_dir = {p}  (IG_SESSION_DIR)")
        return p

    base_candidates = [
        os.getenv("DATA_DIR"),
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

    p = Path("/app/data/ig_sessions")
    p.mkdir(parents=True, exist_ok=True)
    logger.info(f"[pool] session_dir = {p}  (fallback)")
    return p


# ── CALLBACK ПРИ СМЕРТИ СЕССИИ ────────────────────────────────────────────────
# Устанавливается из instagram_handlers через set_session_dead_callback().
# Так мы избегаем циклического импорта: viewer ничего не знает про handlers.

_on_session_dead: Optional[Callable] = None


def set_session_dead_callback(cb: Callable) -> None:
    """
    Регистрирует async callback(login: str), вызывается при LoginRequired.
    Вызывать из register_ig_handlers().
    """
    global _on_session_dead
    _on_session_dead = cb


# ── POOL КЛИЕНТОВ ─────────────────────────────────────────────────────────────

class _AccountPool:
    """
    Пул instagrapi.Client.
    При ошибках (429, LoginRequired) переключается на следующий аккаунт.
    Новое: reload_client() — безопасная горячая замена клиента после /ig_refresh.
    """

    def __init__(self):
        self._accounts = _parse_accounts()
        self._clients: list[Optional[Client]] = []
        self._current = 0
        self._lock = asyncio.Lock()
        self._initialized = False
        # Счётчик успешных запросов по индексу аккаунта (для статистики)
        self._request_counts: dict[int, int] = {}
        # Уже известные мёртвые сессии — не спамим алертами повторно
        self._known_dead: set[str] = set()

    # ── инициализация ────────────────────────────────────────────────────────

    def _make_client(self, login: str, pwd: str) -> Client:
        """
        Загружает клиент из session_dir/{login}.json.
        Пароль здесь не используется — только кукисы из файла.
        """
        cl = Client()
        cl.delay_range = [2, 5]
        session_file = _session_dir() / f"{login}.json"

        if not session_file.exists():
            raise FileNotFoundError(
                f"Файл сессии не найден: {session_file}\n"
                f"Создай его через /ig_refresh или положи вручную в {session_file.parent}/"
            )

        settings = json.loads(session_file.read_text())
        cl.set_settings(settings)

        cookies = settings.get("cookies", {})
        for name, value in cookies.items():
            cl.private.cookies.set(name, value, domain=".instagram.com")

        ua = settings.get("user_agent", "")
        if ua:
            cl.private.headers["User-Agent"] = ua

        sessionid = cookies.get("sessionid", "")
        if sessionid:
            cl.private.headers["Authorization"] = f"Bearer IGT:2:{sessionid}"

        ds_user_id = cookies.get("ds_user_id", "")
        logger.info(f"[pool] ✅ загружен: {login}  ds_user_id={ds_user_id}")
        return cl

    def _init_all(self):
        if not _INSTAGRAPI_OK:
            return
        if not self._accounts:
            logger.error("[pool] IG_ACCOUNTS не задан — Instagram недоступен")
            return
        for login, pwd in self._accounts:
            try:
                cl = self._make_client(login, pwd)
                self._clients.append(cl)
                self._request_counts[len(self._clients) - 1] = 0
            except Exception as e:
                logger.error(f"[pool] не удалось загрузить {login}: {e}")
                self._clients.append(None)  # держим слот чтобы индексы совпадали
        self._initialized = True
        live = sum(1 for c in self._clients if c is not None)
        logger.info(f"[pool] готов: {live}/{len(self._accounts)} аккаунтов")

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
        cl = self._clients[self._current % len(self._clients)]
        if cl is None:
            # Слот пустой (failed init или убитый) — ищем живой
            for i, c in enumerate(self._clients):
                if c is not None:
                    self._current = i
                    return c
            return None
        return cl

    # ── НОВОЕ: безопасный горячий перезапуск ─────────────────────────────────

    async def reload_client(self, login: str) -> tuple[bool, str]:
        """
        Безопасно перезагружает клиент для login:
        1. Создаёт нового клиента из обновлённого session.json
        2. Тестирует — делает реальный запрос к Instagram
        3. Только если тест прошёл — заменяет старый клиент в пуле

        Returns: (success: bool, message: str)
        """
        await self._ensure_init()

        idx = next(
            (i for i, (l, _) in enumerate(self._accounts) if l == login),
            None,
        )
        if idx is None:
            msg = f"{login} не найден в IG_ACCOUNTS"
            logger.warning(f"[pool] reload_client: {msg}")
            return False, msg

        # Создаём клиента
        try:
            new_cl = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._make_client(login, "")
            )
        except Exception as e:
            msg = f"Ошибка загрузки session.json: {e}"
            logger.error(f"[pool] reload_client {login}: {msg}")
            return False, msg

        # Тестируем — реальный запрос к IG
        try:
            def _ping(cl: Client):
                return cl.user_info_by_username(login)

            await asyncio.get_event_loop().run_in_executor(
                None, lambda: _ping(new_cl)
            )
        except LoginRequired as e:
            msg = f"Куки не работают — LoginRequired: {e}"
            logger.error(f"[pool] reload_client {login}: {msg}")
            return False, msg
        except Exception as e:
            msg = f"Тест не прошёл: {e}"
            logger.error(f"[pool] reload_client {login}: {msg}")
            return False, msg

        # Всё ок — заменяем под блокировкой
        async with self._lock:
            while len(self._clients) <= idx:
                self._clients.append(None)
            self._clients[idx] = new_cl
            self._request_counts[idx] = 0
            self._known_dead.discard(login)  # сессия ожила — сбрасываем флаг

        logger.info(f"[pool] ✅ reload_client {login}: клиент заменён и протестирован")
        return True, "Клиент успешно перезагружен и протестирован"

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
                # Считаем успешный запрос
                idx = self._current % len(self._clients)
                self._request_counts[idx] = self._request_counts.get(idx, 0) + 1
                return result

            except (PleaseWaitFewMinutes, RateLimitError) as e:
                # Увеличенный backoff — не спешить ротировать
                wait = random.uniform(15, 35)
                logger.warning(
                    f"[pool] 429 на акк #{self._current}, "
                    f"ждём {wait:.0f}с перед ротацией... ({e})"
                )
                self._next_client()
                await asyncio.sleep(wait)
                last_err = e

            except LoginRequired as e:
                login = self._accounts[self._current % len(self._accounts)][0]
                logger.warning(
                    f"[pool] LoginRequired @{login} — сессия протухла, ротация.\n"
                    f"👉 Обнови куки: /ig_refresh"
                )
                self._next_client()
                last_err = e
                # Алерт только если эта сессия ещё не была помечена мёртвой
                if login not in self._known_dead:
                    self._known_dead.add(login)
                    if _on_session_dead:
                        try:
                            asyncio.ensure_future(_on_session_dead(login))
                        except Exception:
                            pass

            except (UserNotFound, PrivateAccount):
                raise  # не ошибка пула, пробрасываем выше

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


# ── СТАТУС И ВАЛИДАЦИЯ СЕССИЙ ─────────────────────────────────────────────────

def get_sessions_status() -> list[dict]:
    """
    Вернуть статус всех аккаунтов пула.
    Используется в health check таске и /ig_status.
    """
    total = len(_POOL._accounts)
    result = []
    for i, (login, _) in enumerate(_POOL._accounts):
        cl = _POOL._clients[i] if i < len(_POOL._clients) else None
        result.append({
            "login": login,
            "alive": cl is not None,
            "is_current": total > 0 and (i == _POOL._current % total),
            "requests": _POOL._request_counts.get(i, 0),
        })
    return result


async def validate_session(login: str) -> bool:
    """
    Проверить что сессия для login работает.
    Делает лёгкий запрос к профилю самого аккаунта.
    Используется в /ig_refresh для подтверждения новых кук.
    """
    idx = next(
        (i for i, (l, _) in enumerate(_POOL._accounts) if l == login),
        None,
    )
    if idx is None:
        return False
    if idx >= len(_POOL._clients) or _POOL._clients[idx] is None:
        return False

    cl = _POOL._clients[idx]

    def _ping(c: Client):
        return c.user_info_by_username(login)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _ping(cl)
        )
        return bool(result)
    except Exception as e:
        logger.warning(f"validate_session {login}: {e}")
        return False


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
        followers   = getattr(user, "follower_count", 0) or 0
        following   = getattr(user, "following_count", 0) or 0
        posts_cnt   = getattr(user, "media_count", 0) or 0
        biography   = getattr(user, "biography", "") or ""
        is_private  = getattr(user, "is_private", False)
        is_verified = getattr(user, "is_verified", False)
        full_name   = getattr(user, "full_name", "") or ""
        username    = getattr(user, "username", "") or ""
        pk          = str(getattr(user, "pk", "") or "")

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
        sc       = getattr(media, "code", "") or str(getattr(media, "pk", ""))
        pid      = str(getattr(media, "pk", sc))
        taken_at = getattr(media, "taken_at", None)
        ts       = int(taken_at.timestamp()) if taken_at else 0
        cap      = getattr(media, "caption_text", "") or ""
        likes    = getattr(media, "like_count", 0) or 0
        comments = getattr(media, "comment_count", 0) or 0

        media_type = getattr(media, "media_type", 1)

        urls: list[dict] = []
        if media_type == 8:  # альбом
            resources = getattr(media, "resources", []) or []
            for r in resources:
                r_type = getattr(r, "media_type", 1)
                if r_type == 2:
                    urls.append({
                        "type":  "video",
                        "url":   str(getattr(r, "video_url", "") or ""),
                        "thumb": str(getattr(r, "thumbnail_url", "") or ""),
                    })
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
        pk       = str(getattr(item, "pk", "") or "")
        mtype    = getattr(item, "media_type", 1)
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
        return {
            "id": "", "username": username, "full_name": username,
            "biography": "", "followers": 0, "following": 0, "posts_count": 0,
            "avatar_url": "", "is_private": True, "is_verified": False, "posts": [],
        }
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

    all_posts = _cache_get(username)

    if all_posts is None:
        def _fetch(cl: Client):
            medias = cl.user_medias(int(user_id), amount=33)
            return [_media_to_post(m) for m in medias if m]

        try:
            all_posts = await _POOL.run(_fetch) or []
            all_posts = [p for p in all_posts if p.get("id")]
            if all_posts:
                _cache_set(username, all_posts)
                logger.info(f"📦 get_posts @{username}: {len(all_posts)} в кэш")
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


# ── БД (подписки) ─────────────────────────────────────────────────────────────

def _db_path() -> str:
    for candidate in [
        os.getenv("DB_DIR"), "/data", "/app/data",
        os.path.dirname(os.path.abspath(__file__)),
    ]:
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
            "INSERT OR REPLACE INTO ig_subscriptions "
            "(user_id, ig_username, sub_type, last_seen) VALUES (?,?,?,?)",
            (str(user_id), ig_username, sub_type, last_seen),
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
        (str(user_id), ig_username, sub_type),
    )
    conn.commit()
    conn.close()
    return True


def ig_get_subscriptions(user_id: str) -> list:
    conn = sqlite3.connect(IG_DB)
    rows = conn.execute(
        "SELECT ig_username, sub_type, last_seen FROM ig_subscriptions "
        "WHERE user_id=? ORDER BY ig_username",
        (str(user_id),),
    ).fetchall()
    conn.close()
    return [{"ig_username": r[0], "sub_type": r[1], "last_seen": r[2]} for r in rows]


def ig_is_subscribed(user_id: str, ig_username: str, sub_type: str) -> bool:
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    row = conn.execute(
        "SELECT 1 FROM ig_subscriptions WHERE user_id=? AND ig_username=? AND sub_type=?",
        (str(user_id), ig_username, sub_type),
    ).fetchone()
    conn.close()
    return row is not None


def ig_update_last_seen(user_id: str, ig_username: str, sub_type: str, last_seen: str):
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    conn.execute(
        "UPDATE ig_subscriptions SET last_seen=? "
        "WHERE user_id=? AND ig_username=? AND sub_type=?",
        (last_seen, str(user_id), ig_username, sub_type),
    )
    conn.commit()
    conn.close()


def ig_all_subscriptions() -> list:
    conn = sqlite3.connect(IG_DB)
    rows = conn.execute(
        "SELECT user_id, ig_username, sub_type, last_seen "
        "FROM ig_subscriptions ORDER BY ig_username"
    ).fetchall()
    conn.close()
    return [
        {"user_id": r[0], "ig_username": r[1], "sub_type": r[2], "last_seen": r[3]}
        for r in rows
    ]


def ig_mark_sent(user_id: str, media_id: str):
    conn = sqlite3.connect(IG_DB)
    conn.execute(
        "INSERT OR IGNORE INTO ig_sent (user_id, media_id) VALUES (?,?)",
        (str(user_id), media_id),
    )
    conn.commit()
    conn.close()


def ig_already_sent(user_id: str, media_id: str) -> bool:
    conn = sqlite3.connect(IG_DB)
    row = conn.execute(
        "SELECT 1 FROM ig_sent WHERE user_id=? AND media_id=?",
        (str(user_id), media_id),
    ).fetchone()
    conn.close()
    return row is not None
