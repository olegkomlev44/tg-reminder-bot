"""
feed_handlers.py — бесконечная лента видео/мемов из Reddit
с системой рекомендаций на основе лайков/скипов.

Команды:
    /feed          — запустить ленту
    /saved         — сохранённые посты
    /feed_settings — настройки (NSFW и т.д.)

Callback data:
    feed:like:{post_id}
    feed:skip:{post_id}
    feed:save:{post_id}
    feed:next
    feed:settings
    feed:toggle_nsfw
    feed:category:{name}
    feed:saved_list:{page}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import time
import tempfile
import hashlib
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, InputMediaVideo, InputMediaPhoto,
)

logger = logging.getLogger(__name__)

# ── БД ────────────────────────────────────────────────────────────────────────

def _db_path() -> str:
    for candidate in [
        os.getenv("DATA_DIR"),
        os.getenv("DB_DIR"),
        "/app/data", "/data",
        os.path.dirname(os.path.abspath(__file__)),
    ]:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            t = os.path.join(candidate, ".wt")
            open(t, "w").write("ok"); os.remove(t)
            return os.path.join(candidate, "feed.db")
        except Exception:
            continue
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.db")

FEED_DB = _db_path()

def init_feed_db():
    conn = sqlite3.connect(FEED_DB)
    c = conn.cursor()

    # Веса сабреддитов для каждого пользователя
    c.execute("""CREATE TABLE IF NOT EXISTS feed_weights (
        user_id     TEXT NOT NULL,
        subreddit   TEXT NOT NULL,
        weight      REAL DEFAULT 0.5,
        updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, subreddit)
    )""")

    # История просмотров (чтобы не повторять)
    c.execute("""CREATE TABLE IF NOT EXISTS feed_seen (
        user_id     TEXT NOT NULL,
        post_id     TEXT NOT NULL,
        seen_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, post_id)
    )""")

    # Лайки
    c.execute("""CREATE TABLE IF NOT EXISTS feed_likes (
        user_id     TEXT NOT NULL,
        post_id     TEXT NOT NULL,
        subreddit   TEXT,
        title       TEXT,
        url         TEXT,
        thumb_url   TEXT,
        is_video    INTEGER DEFAULT 0,
        is_nsfw     INTEGER DEFAULT 0,
        liked_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, post_id)
    )""")

    # Настройки пользователя
    c.execute("""CREATE TABLE IF NOT EXISTS feed_settings (
        user_id         TEXT PRIMARY KEY,
        nsfw_enabled    INTEGER DEFAULT 0,
        active_category TEXT DEFAULT 'all'
    )""")

    # Кэш file_id уже загруженных медиа
    c.execute("""CREATE TABLE IF NOT EXISTS feed_cache (
        post_id     TEXT PRIMARY KEY,
        file_id     TEXT,
        media_type  TEXT,
        cached_at   DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()
    conn.close()
    logger.info("🟢 Feed DB инициализирована")


# ── САБРЕДДИТЫ ────────────────────────────────────────────────────────────────

CATEGORIES = {
    "all": {
        "label": "🌀 Всё подряд",
        "subs": [
            "funny", "memes", "dankmemes", "me_irl",
            "unexpected", "instant_regret", "WatchPeopleDieInside",
            "nextfuckinglevel", "PublicFreakout", "Whatcouldgowrong",
            "oddlysatisfying", "interestingasfuck", "blackmagicfuckery",
            "AbruptChaos", "maybemaybemaybe",
        ],
    },
    "memes": {
        "label": "😂 Мемы",
        "subs": [
            "memes", "dankmemes", "me_irl", "AdviceAnimals",
            "ComedyCemetery", "shitposting", "surrealmemes",
        ],
    },
    "wow": {
        "label": "🤯 Вау-контент",
        "subs": [
            "nextfuckinglevel", "interestingasfuck", "blackmagicfuckery",
            "oddlysatisfying", "BeAmazed", "woahdude",
        ],
    },
    "fails": {
        "label": "😬 Фейлы",
        "subs": [
            "instant_regret", "WatchPeopleDieInside", "Whatcouldgowrong",
            "AbruptChaos", "PublicFreakout", "funny",
        ],
    },
    "animals": {
        "label": "🐾 Животные",
        "subs": [
            "aww", "AnimalsBeingDerps", "AnimalsBeingBros",
            "rarepuppers", "Catswhoyell", "therewasanattempt",
        ],
    },
    "nsfw": {
        "label": "🔞 NSFW",
        "subs": [
            "gonewild", "nsfw", "realgirls", "Amateur",
            "NSFW_GIF", "nsfw_videos", "onlyfans_wild",
            "collegesluts", "legalteens", "RealGirls",
        ],
        "nsfw_only": True,
    },
}

SFW_CATEGORIES  = [k for k, v in CATEGORIES.items() if not v.get("nsfw_only")]
ALL_CATEGORIES  = list(CATEGORIES.keys())

# Начальные веса (равномерные)
DEFAULT_WEIGHTS = {sub: 0.5 for cat in CATEGORIES.values() for sub in cat["subs"]}

LIKE_BOOST  =  0.25
SKIP_DECAY  = -0.08
MAX_WEIGHT  =  1.0
MIN_WEIGHT  =  0.05
SEEN_LIMIT  = 500   # сколько хранить в feed_seen на юзера


# ── DB HELPERS ────────────────────────────────────────────────────────────────

def _get_settings(user_id: str) -> dict:
    conn = sqlite3.connect(FEED_DB)
    row = conn.execute(
        "SELECT nsfw_enabled, active_category FROM feed_settings WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    if row:
        return {"nsfw": bool(row[0]), "category": row[1] or "all"}
    return {"nsfw": False, "category": "all"}


def _save_settings(user_id: str, nsfw: bool, category: str):
    conn = sqlite3.connect(FEED_DB)
    conn.execute(
        "INSERT OR REPLACE INTO feed_settings (user_id, nsfw_enabled, active_category) VALUES (?,?,?)",
        (user_id, int(nsfw), category)
    )
    conn.commit()
    conn.close()


def _get_weights(user_id: str) -> dict:
    conn = sqlite3.connect(FEED_DB)
    rows = conn.execute(
        "SELECT subreddit, weight FROM feed_weights WHERE user_id=?", (user_id,)
    ).fetchall()
    conn.close()
    weights = dict(DEFAULT_WEIGHTS)
    for sub, w in rows:
        weights[sub] = w
    return weights


def _update_weight(user_id: str, subreddit: str, delta: float):
    conn = sqlite3.connect(FEED_DB)
    current = conn.execute(
        "SELECT weight FROM feed_weights WHERE user_id=? AND subreddit=?",
        (user_id, subreddit)
    ).fetchone()
    w = (current[0] if current else 0.5) + delta
    w = max(MIN_WEIGHT, min(MAX_WEIGHT, w))
    conn.execute(
        "INSERT OR REPLACE INTO feed_weights (user_id, subreddit, weight) VALUES (?,?,?)",
        (user_id, subreddit, w)
    )
    conn.commit()
    conn.close()


def _mark_seen(user_id: str, post_id: str):
    conn = sqlite3.connect(FEED_DB)
    conn.execute(
        "INSERT OR IGNORE INTO feed_seen (user_id, post_id) VALUES (?,?)",
        (user_id, post_id)
    )
    # Чистим старые записи если накопилось много
    count = conn.execute(
        "SELECT COUNT(*) FROM feed_seen WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    if count > SEEN_LIMIT:
        conn.execute("""
            DELETE FROM feed_seen WHERE user_id=? AND post_id IN (
                SELECT post_id FROM feed_seen WHERE user_id=?
                ORDER BY seen_at ASC LIMIT ?
            )
        """, (user_id, user_id, count - SEEN_LIMIT + 100))
    conn.commit()
    conn.close()


def _is_seen(user_id: str, post_id: str) -> bool:
    conn = sqlite3.connect(FEED_DB)
    row = conn.execute(
        "SELECT 1 FROM feed_seen WHERE user_id=? AND post_id=?",
        (user_id, post_id)
    ).fetchone()
    conn.close()
    return row is not None


def _save_like(user_id: str, post: dict):
    conn = sqlite3.connect(FEED_DB)
    conn.execute(
        """INSERT OR REPLACE INTO feed_likes
           (user_id, post_id, subreddit, title, url, thumb_url, is_video, is_nsfw)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, post["id"], post.get("subreddit",""),
         post.get("title","")[:200], post.get("url",""),
         post.get("thumb",""), int(post.get("is_video", False)),
         int(post.get("nsfw", False)))
    )
    conn.commit()
    conn.close()


def _remove_like(user_id: str, post_id: str):
    conn = sqlite3.connect(FEED_DB)
    conn.execute("DELETE FROM feed_likes WHERE user_id=? AND post_id=?", (user_id, post_id))
    conn.commit()
    conn.close()


def _is_liked(user_id: str, post_id: str) -> bool:
    conn = sqlite3.connect(FEED_DB)
    row = conn.execute(
        "SELECT 1 FROM feed_likes WHERE user_id=? AND post_id=?",
        (user_id, post_id)
    ).fetchone()
    conn.close()
    return row is not None


def _get_likes(user_id: str, page: int = 0, per_page: int = 5) -> list:
    conn = sqlite3.connect(FEED_DB)
    rows = conn.execute(
        """SELECT post_id, subreddit, title, url, thumb_url, is_video, is_nsfw
           FROM feed_likes WHERE user_id=?
           ORDER BY liked_at DESC LIMIT ? OFFSET ?""",
        (user_id, per_page, page * per_page)
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "subreddit": r[1], "title": r[2],
         "url": r[3], "thumb": r[4], "is_video": bool(r[5]), "nsfw": bool(r[6])}
        for r in rows
    ]


def _count_likes(user_id: str) -> int:
    conn = sqlite3.connect(FEED_DB)
    n = conn.execute("SELECT COUNT(*) FROM feed_likes WHERE user_id=?", (user_id,)).fetchone()[0]
    conn.close()
    return n


def _get_cached_file_id(post_id: str) -> tuple[str, str] | None:
    conn = sqlite3.connect(FEED_DB)
    row = conn.execute(
        "SELECT file_id, media_type FROM feed_cache WHERE post_id=?", (post_id,)
    ).fetchone()
    conn.close()
    return (row[0], row[1]) if row else None


def _save_cached_file_id(post_id: str, file_id: str, media_type: str):
    conn = sqlite3.connect(FEED_DB)
    conn.execute(
        "INSERT OR REPLACE INTO feed_cache (post_id, file_id, media_type) VALUES (?,?,?)",
        (post_id, file_id, media_type)
    )
    conn.commit()
    conn.close()


# ── REDDIT API ────────────────────────────────────────────────────────────────

REDDIT_HEADERS = {
    "User-Agent": "TelegramFeedBot/1.0 (by /u/feedbot_tg)",
    "Accept": "application/json",
}

# Хранилище постов в памяти (буфер на юзера)
_POST_BUFFER: dict[str, list] = {}
_BUFFER_SIZE = 20


async def _fetch_reddit_posts(
    subreddit: str,
    sort: str = "hot",
    limit: int = 25,
    nsfw_ok: bool = False,
) -> list[dict]:
    """Получить посты из сабреддита через Reddit JSON API."""
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1"
    if nsfw_ok:
        url += "&include_over_18=on"

    try:
        async with aiohttp.ClientSession(headers=REDDIT_HEADERS) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15), ssl=False) as resp:
                if resp.status != 200:
                    logger.warning(f"Reddit {subreddit} → {resp.status}")
                    return []
                data = await resp.json()

        posts = []
        for child in data.get("data", {}).get("children", []):
            p = child.get("data", {})
            pid = p.get("id", "")
            if not pid:
                continue

            is_video = bool(p.get("is_video")) or p.get("post_hint") == "rich:video"
            is_gallery = bool(p.get("is_gallery"))
            nsfw = bool(p.get("over_18"))

            # Пропускаем NSFW если не разрешено
            if nsfw and not nsfw_ok:
                continue
            # Пропускаем текстовые посты без медиа
            if not is_video and not is_gallery and p.get("post_hint") not in ("image", "link"):
                hint = p.get("post_hint", "")
                url_lower = p.get("url", "").lower()
                if not any(url_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".gifv", ".mp4", ".webp")):
                    if hint not in ("image",):
                        continue

            # URL медиа
            media_url = ""
            thumb_url = ""

            if is_video:
                # Reddit hosted video
                rv = p.get("media", {})
                if rv:
                    rv = rv.get("reddit_video", {})
                    media_url = rv.get("fallback_url", "") or rv.get("hls_url", "")
                # Fallback: v.redd.it
                if not media_url:
                    media_url = p.get("url", "")
                thumb_url = p.get("thumbnail", "") or ""
            else:
                media_url = p.get("url", "")
                thumb_url = p.get("thumbnail", "") or media_url

            if not media_url:
                continue

            # gifv → mp4
            if media_url.endswith(".gifv"):
                media_url = media_url[:-5] + ".mp4"
                is_video = True

            posts.append({
                "id":         pid,
                "subreddit":  subreddit,
                "title":      p.get("title", "")[:200],
                "url":        media_url,
                "thumb":      thumb_url,
                "score":      p.get("score", 0),
                "is_video":   is_video,
                "nsfw":       nsfw,
                "permalink":  "https://reddit.com" + p.get("permalink", ""),
            })

        return posts

    except Exception as e:
        logger.error(f"_fetch_reddit_posts {subreddit}: {e}")
        return []


def _pick_subreddit(weights: dict, category: str, nsfw_enabled: bool) -> str:
    """Выбрать сабреддит с учётом весов и категории."""
    cat_data = CATEGORIES.get(category, CATEGORIES["all"])

    # Если выбрана категория nsfw — берём только nsfw subs
    if cat_data.get("nsfw_only") and not nsfw_enabled:
        category = "all"
        cat_data = CATEGORIES["all"]

    subs = cat_data["subs"]

    # Если all — добавляем nsfw subs при включённом NSFW
    if category == "all" and nsfw_enabled:
        subs = subs + CATEGORIES["nsfw"]["subs"]

    available = [s for s in subs if s in weights]
    if not available:
        available = subs

    w = [max(MIN_WEIGHT, weights.get(s, 0.5)) for s in available]
    total = sum(w)
    probs = [x / total for x in w]

    return random.choices(available, weights=probs, k=1)[0]


async def _get_next_post(user_id: str) -> dict | None:
    """Достать следующий пост для юзера из буфера или загрузить новые."""
    settings = _get_settings(user_id)
    nsfw_ok  = settings["nsfw"]
    category = settings["category"]
    weights  = _get_weights(user_id)

    # Пробуем достать из буфера
    buf = _POST_BUFFER.get(user_id, [])

    # Фильтруем буфер: убираем уже виденные, убираем nsfw если не разрешено
    buf = [
        p for p in buf
        if not _is_seen(user_id, p["id"])
        and (nsfw_ok or not p.get("nsfw"))
    ]
    _POST_BUFFER[user_id] = buf

    if not buf:
        # Грузим свежие посты
        sub = _pick_subreddit(weights, category, nsfw_ok)
        sort = random.choice(["hot", "top", "new"]) if random.random() < 0.3 else "hot"
        new_posts = await _fetch_reddit_posts(sub, sort=sort, limit=25, nsfw_ok=nsfw_ok)

        # Фильтруем виденные
        new_posts = [p for p in new_posts if not _is_seen(user_id, p["id"])]

        if not new_posts:
            # Если всё видели — пробуем другой сабреддит
            for _ in range(5):
                sub2 = _pick_subreddit(weights, category, nsfw_ok)
                new_posts = await _fetch_reddit_posts(sub2, sort="new", limit=25, nsfw_ok=nsfw_ok)
                new_posts = [p for p in new_posts if not _is_seen(user_id, p["id"])]
                if new_posts:
                    break

        _POST_BUFFER[user_id] = new_posts

    if not _POST_BUFFER.get(user_id):
        return None

    post = _POST_BUFFER[user_id].pop(0)
    _mark_seen(user_id, post["id"])

    # Подгружаем буфер в фоне если мало
    if len(_POST_BUFFER.get(user_id, [])) < 5:
        asyncio.create_task(_prefetch(user_id, settings, weights))

    return post


async def _prefetch(user_id: str, settings: dict, weights: dict):
    """Фоновая подгрузка постов в буфер."""
    try:
        nsfw_ok  = settings["nsfw"]
        category = settings["category"]
        sub = _pick_subreddit(weights, category, nsfw_ok)
        posts = await _fetch_reddit_posts(sub, limit=25, nsfw_ok=nsfw_ok)
        posts = [p for p in posts if not _is_seen(user_id, p["id"])]
        existing = _POST_BUFFER.get(user_id, [])
        _POST_BUFFER[user_id] = existing + posts
    except Exception as e:
        logger.error(f"_prefetch {user_id}: {e}")


# ── ОТПРАВКА ПОСТА ────────────────────────────────────────────────────────────

async def _download_media(url: str) -> bytes | None:
    """Скачать медиафайл."""
    if not url:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.reddit.com/",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
                ssl=False
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return data if len(data) > 500 else None
                logger.warning(f"download {url[:60]} → {resp.status}")
    except Exception as e:
        logger.error(f"download {url[:60]}: {e}")
    return None


def _post_keyboard(post: dict, user_id: str) -> InlineKeyboardMarkup:
    liked = _is_liked(user_id, post["id"])
    heart = "❤️" if liked else "🤍"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{heart} Лайк",  callback_data=f"feed:like:{post['id']}"),
        InlineKeyboardButton(text="👎 Скип",         callback_data=f"feed:skip:{post['id']}"),
        InlineKeyboardButton(text="🔁 Ещё",          callback_data="feed:next"),
    ], [
        InlineKeyboardButton(text="💾 Сохранить",    callback_data=f"feed:save:{post['id']}"),
        InlineKeyboardButton(text="⚙️ Настройки",    callback_data="feed:settings"),
    ]])


def _caption(post: dict) -> str:
    nsfw_tag = " 🔞" if post.get("nsfw") else ""
    return (
        f"{'🎬' if post['is_video'] else '🖼'} <b>{post['title'][:180]}</b>{nsfw_tag}\n"
        f"<i>r/{post['subreddit']}</i>"
    )


async def _send_post(bot: Bot, chat_id: int | str, post: dict, user_id: str) -> bool:
    """Отправить пост пользователю. Возвращает True если успешно."""
    kb = _post_keyboard(post, user_id)
    cap = _caption(post)

    # Проверяем кэш file_id
    cached = _get_cached_file_id(post["id"])
    if cached:
        file_id, mtype = cached
        try:
            if mtype == "video":
                msg = await bot.send_video(chat_id, file_id, caption=cap,
                                           parse_mode="HTML", reply_markup=kb)
            else:
                msg = await bot.send_photo(chat_id, file_id, caption=cap,
                                           parse_mode="HTML", reply_markup=kb)
            return True
        except Exception:
            pass  # file_id протух — грузим заново

    # Скачиваем
    data = await _download_media(post["url"])
    if not data:
        # Если не скачалось — пробуем thumbnail как фото
        if post.get("thumb") and post["thumb"].startswith("http"):
            data = await _download_media(post["thumb"])
            if data:
                post = {**post, "is_video": False}

    if not data:
        logger.warning(f"Не удалось скачать {post['url'][:60]}")
        return False

    fname_base = f"feed_{post['id']}"
    try:
        if post["is_video"]:
            msg = await bot.send_video(
                chat_id,
                BufferedInputFile(data, filename=f"{fname_base}.mp4"),
                caption=cap,
                parse_mode="HTML",
                reply_markup=kb,
                supports_streaming=True,
            )
            fid = msg.video.file_id if msg.video else None
            if fid:
                _save_cached_file_id(post["id"], fid, "video")
        else:
            msg = await bot.send_photo(
                chat_id,
                BufferedInputFile(data, filename=f"{fname_base}.jpg"),
                caption=cap,
                parse_mode="HTML",
                reply_markup=kb,
            )
            fid = msg.photo[-1].file_id if msg.photo else None
            if fid:
                _save_cached_file_id(post["id"], fid, "photo")
        return True
    except Exception as e:
        logger.error(f"_send_post {post['id']}: {e}")
        return False


# Временное хранилище текущего поста на юзера (для обработки лайков)
_CURRENT_POST: dict[str, dict] = {}


# ── HANDLERS ──────────────────────────────────────────────────────────────────

async def cmd_feed(message: types.Message):
    user_id = str(message.from_user.id)
    init_feed_db()

    wait_msg = await message.answer("⏳ Загружаю...")

    post = await _get_next_post(user_id)
    await wait_msg.delete()

    if not post:
        await message.answer("😕 Не удалось загрузить контент. Попробуй позже.")
        return

    _CURRENT_POST[user_id] = post
    ok = await _send_post(message.bot, message.chat.id, post, user_id)
    if not ok:
        # Пробуем следующий
        post2 = await _get_next_post(user_id)
        if post2:
            _CURRENT_POST[user_id] = post2
            await _send_post(message.bot, message.chat.id, post2, user_id)


async def cb_feed_next(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    await callback.answer()

    post = await _get_next_post(user_id)
    if not post:
        await callback.message.answer("😕 Контент закончился, попробуй позже.")
        return

    _CURRENT_POST[user_id] = post
    ok = await _send_post(callback.bot, callback.message.chat.id, post, user_id)
    if not ok:
        post2 = await _get_next_post(user_id)
        if post2:
            _CURRENT_POST[user_id] = post2
            await _send_post(callback.bot, callback.message.chat.id, post2, user_id)


async def cb_feed_like(callback: types.CallbackQuery):
    user_id  = str(callback.from_user.id)
    post_id  = callback.data.split(":", 2)[2]

    # Получаем данные поста
    post = _CURRENT_POST.get(user_id)
    if not post or post["id"] != post_id:
        # Восстановить из лайков если уже лайкнут
        await callback.answer("❤️" if _is_liked(user_id, post_id) else "👍")
        return

    already_liked = _is_liked(user_id, post_id)

    if already_liked:
        _remove_like(user_id, post_id)
        _update_weight(user_id, post["subreddit"], -LIKE_BOOST / 2)
        await callback.answer("💔 Убрал лайк")
        heart = "🤍"
    else:
        _save_like(user_id, post)
        _update_weight(user_id, post["subreddit"], LIKE_BOOST)
        await callback.answer("❤️ Лайк!")
        heart = "❤️"

    # Обновляем кнопки
    try:
        new_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{heart} Лайк", callback_data=f"feed:like:{post_id}"),
            InlineKeyboardButton(text="👎 Скип",        callback_data=f"feed:skip:{post_id}"),
            InlineKeyboardButton(text="🔁 Ещё",         callback_data="feed:next"),
        ], [
            InlineKeyboardButton(text="💾 Сохранить",   callback_data=f"feed:save:{post_id}"),
            InlineKeyboardButton(text="⚙️ Настройки",   callback_data="feed:settings"),
        ]])
        await callback.message.edit_reply_markup(reply_markup=new_kb)
    except Exception:
        pass


async def cb_feed_skip(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    post_id = callback.data.split(":", 2)[2]

    post = _CURRENT_POST.get(user_id)
    if post and post["id"] == post_id:
        _update_weight(user_id, post["subreddit"], SKIP_DECAY)

    await callback.answer("👎 Скипнул")

    # Сразу показываем следующий
    next_post = await _get_next_post(user_id)
    if next_post:
        _CURRENT_POST[user_id] = next_post
        await _send_post(callback.bot, callback.message.chat.id, next_post, user_id)


async def cb_feed_save(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    post_id = callback.data.split(":", 2)[2]

    post = _CURRENT_POST.get(user_id)
    if not post or post["id"] != post_id:
        await callback.answer("❌ Не могу найти пост")
        return

    if _is_liked(user_id, post_id):
        await callback.answer("✅ Уже сохранён в лайках")
        return

    _save_like(user_id, post)
    _update_weight(user_id, post["subreddit"], LIKE_BOOST)
    await callback.answer("💾 Сохранено!")


async def cmd_feed_saved(message: types.Message):
    user_id = str(message.from_user.id)
    init_feed_db()
    await _show_saved(message.bot, message.chat.id, user_id, 0, reply_to=message)


async def cb_feed_saved_list(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    page = int(callback.data.split(":")[-1])
    await callback.answer()
    await _show_saved(callback.bot, callback.message.chat.id, user_id, page)


async def _show_saved(bot: Bot, chat_id, user_id: str, page: int, reply_to=None):
    total = _count_likes(user_id)
    if total == 0:
        text = "💔 У тебя пока нет сохранённых постов.\nЛайкай посты в ленте — они появятся здесь."
        if reply_to:
            await reply_to.answer(text)
        else:
            await bot.send_message(chat_id, text)
        return

    per_page = 5
    posts = _get_likes(user_id, page, per_page)
    total_pages = (total - 1) // per_page + 1

    text = f"❤️ <b>Сохранённые посты</b> (стр. {page+1}/{total_pages}):\n\n"
    for i, p in enumerate(posts, 1):
        icon = "🎬" if p["is_video"] else "🖼"
        nsfw = " 🔞" if p["nsfw"] else ""
        text += f"{i}. {icon} <a href=\"{p['url']}\">{p['title'][:80]}</a>{nsfw}\n<i>r/{p['subreddit']}</i>\n\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"feed:saved_list:{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"feed:saved_list:{page+1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])

    if reply_to:
        await reply_to.answer(text, parse_mode="HTML", reply_markup=kb,
                               disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb,
                                disable_web_page_preview=True)


# ── НАСТРОЙКИ ─────────────────────────────────────────────────────────────────

def _settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    nsfw_btn = "🔞 NSFW: ВКЛ ✅" if settings["nsfw"] else "🔞 NSFW: ВЫКЛ ❌"
    cat = settings["category"]

    cat_buttons = []
    for key, data in CATEGORIES.items():
        if data.get("nsfw_only") and not settings["nsfw"]:
            continue
        mark = " ✅" if key == cat else ""
        cat_buttons.append(
            InlineKeyboardButton(text=data["label"] + mark,
                                 callback_data=f"feed:category:{key}")
        )

    # Разбиваем кнопки категорий по 2 в ряд
    cat_rows = [cat_buttons[i:i+2] for i in range(0, len(cat_buttons), 2)]

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=nsfw_btn, callback_data="feed:toggle_nsfw")],
        *cat_rows,
        [InlineKeyboardButton(text="▶️ В ленту", callback_data="feed:next")],
    ])


async def cb_feed_settings(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    settings = _get_settings(user_id)
    await callback.answer()

    cat_name = CATEGORIES.get(settings["category"], {}).get("label", settings["category"])
    text = (
        f"⚙️ <b>Настройки ленты</b>\n\n"
        f"Категория: {cat_name}\n"
        f"NSFW: {'включён 🔞' if settings['nsfw'] else 'выключен ✅'}\n\n"
        f"<i>NSFW контент — для взрослых 18+.\n"
        f"Включая эту опцию ты подтверждаешь, что тебе исполнилось 18 лет.</i>"
    )
    try:
        await callback.message.edit_caption(caption=text, parse_mode="HTML",
                                             reply_markup=_settings_keyboard(settings))
    except Exception:
        try:
            await callback.message.edit_text(text, parse_mode="HTML",
                                              reply_markup=_settings_keyboard(settings))
        except Exception:
            await callback.message.answer(text, parse_mode="HTML",
                                           reply_markup=_settings_keyboard(settings))


async def cb_feed_toggle_nsfw(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    settings = _get_settings(user_id)
    new_nsfw = not settings["nsfw"]

    if new_nsfw:
        # Показываем предупреждение при первом включении
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Мне 18+, включить",
                                 callback_data="feed:nsfw_confirm"),
            InlineKeyboardButton(text="❌ Отмена",
                                 callback_data="feed:settings"),
        ]])
        await callback.answer()
        try:
            await callback.message.edit_caption(
                caption="🔞 <b>Подтверждение</b>\n\nNSFW-контент предназначен только для лиц старше 18 лет.\nПодтверди свой возраст:",
                parse_mode="HTML", reply_markup=confirm_kb
            )
        except Exception:
            await callback.message.edit_text(
                "🔞 <b>Подтверждение</b>\n\nNSFW-контент предназначен только для лиц старше 18 лет.\nПодтверди свой возраст:",
                parse_mode="HTML", reply_markup=confirm_kb
            )
        return

    _save_settings(user_id, False, settings["category"])
    await callback.answer("✅ NSFW выключен")
    # Обновляем меню
    settings["nsfw"] = False
    await _refresh_settings_menu(callback, settings)


async def cb_feed_nsfw_confirm(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    settings = _get_settings(user_id)
    _save_settings(user_id, True, settings["category"])
    # Сбрасываем буфер чтобы подтянуть NSFW контент
    _POST_BUFFER.pop(user_id, None)
    await callback.answer("🔞 NSFW включён")
    settings["nsfw"] = True
    await _refresh_settings_menu(callback, settings)


async def cb_feed_category(callback: types.CallbackQuery):
    user_id  = str(callback.from_user.id)
    category = callback.data.split(":", 2)[2]
    settings = _get_settings(user_id)

    if category not in CATEGORIES:
        await callback.answer("❌ Неизвестная категория")
        return

    if CATEGORIES[category].get("nsfw_only") and not settings["nsfw"]:
        await callback.answer("🔞 Сначала включи NSFW в настройках", show_alert=True)
        return

    _save_settings(user_id, settings["nsfw"], category)
    _POST_BUFFER.pop(user_id, None)  # сбрасываем буфер
    await callback.answer(f"✅ Категория: {CATEGORIES[category]['label']}")
    settings["category"] = category
    await _refresh_settings_menu(callback, settings)


async def _refresh_settings_menu(callback: types.CallbackQuery, settings: dict):
    cat_name = CATEGORIES.get(settings["category"], {}).get("label", settings["category"])
    text = (
        f"⚙️ <b>Настройки ленты</b>\n\n"
        f"Категория: {cat_name}\n"
        f"NSFW: {'включён 🔞' if settings['nsfw'] else 'выключен ✅'}"
    )
    try:
        await callback.message.edit_caption(caption=text, parse_mode="HTML",
                                             reply_markup=_settings_keyboard(settings))
    except Exception:
        try:
            await callback.message.edit_text(text, parse_mode="HTML",
                                              reply_markup=_settings_keyboard(settings))
        except Exception:
            pass


async def cmd_feed_settings(message: types.Message):
    user_id = str(message.from_user.id)
    init_feed_db()
    settings = _get_settings(user_id)
    cat_name = CATEGORIES.get(settings["category"], {}).get("label", settings["category"])
    text = (
        f"⚙️ <b>Настройки ленты</b>\n\n"
        f"Категория: {cat_name}\n"
        f"NSFW: {'включён 🔞' if settings['nsfw'] else 'выключен ✅'}"
    )
    await message.answer(text, parse_mode="HTML",
                         reply_markup=_settings_keyboard(settings))


# ── РЕГИСТРАЦИЯ ───────────────────────────────────────────────────────────────

def register_feed_handlers(dp: Dispatcher):
    init_feed_db()

    dp.message.register(cmd_feed,          Command("feed"))
    dp.message.register(cmd_feed_saved,    Command("saved"))
    dp.message.register(cmd_feed_settings, Command("feed_settings"))

    dp.callback_query.register(cb_feed_next,       F.data == "feed:next")
    dp.callback_query.register(cb_feed_like,       F.data.startswith("feed:like:"))
    dp.callback_query.register(cb_feed_skip,       F.data.startswith("feed:skip:"))
    dp.callback_query.register(cb_feed_save,       F.data.startswith("feed:save:"))
    dp.callback_query.register(cb_feed_settings,   F.data == "feed:settings")
    dp.callback_query.register(cb_feed_toggle_nsfw,F.data == "feed:toggle_nsfw")
    dp.callback_query.register(cb_feed_nsfw_confirm,F.data == "feed:nsfw_confirm")
    dp.callback_query.register(cb_feed_category,   F.data.startswith("feed:category:"))
    dp.callback_query.register(cb_feed_saved_list, F.data.startswith("feed:saved_list:"))

    logger.info("✅ Feed handlers зарегистрированы")
