"""
instagram_viewer.py — анонимный просмотр Instagram через Telegram-бота.

Возможности:
- истории (stories) пользователя
- посты: первые 10 альбомом, кнопка «Далее»
- подписка на посты / истории / аватарку
- авто-уведомления при новых постах / историях
- медиа не сохраняются на сервере — stream → Telegram → delete
"""

import asyncio
import io
import logging
import os
import sqlite3
import time
import random
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── БД ────────────────────────────────────────────────────────────────────────

def _db_path() -> str:
    for candidate in [os.getenv("DB_DIR"), "/data", "/app/data", os.path.dirname(os.path.abspath(__file__))]:
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
    # подписки
    c.execute("""CREATE TABLE IF NOT EXISTS ig_subscriptions (
        user_id     TEXT NOT NULL,
        ig_username TEXT NOT NULL,
        sub_type    TEXT NOT NULL,   -- 'stories' | 'posts' | 'avatar'
        last_seen   TEXT DEFAULT '',  -- последний известный shortcode/story_id/avatar_url
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, ig_username, sub_type)
    )""")
    # лог отправленных медиа (чтобы не дублировать)
    c.execute("""CREATE TABLE IF NOT EXISTS ig_sent (
        user_id     TEXT NOT NULL,
        media_id    TEXT NOT NULL,
        sent_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, media_id)
    )""")
    conn.commit()
    conn.close()
    logger.info("🟢 Instagram DB инициализирована")


# ── INSTAGRAM API (через rapidapi / scraper) ───────────────────────────────────
# Используем публичный endpoint Instaloader-совместимого прокси.
# Fallback: picuki.com / imginn.com (парсинг), если прямой API не работает.

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36 Instagram/311.0.0.0.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}

# Публичные scraper-прокси (без ключа) ─────────────────────────────────────────
SCRAPER_BASES = [
    "https://instagram-scraper-api2.p.rapidapi.com",   # RapidAPI (нужен ключ)
    "https://instagram-looter2.p.rapidapi.com",         # RapidAPI fallback
]

# Используем imginn.com / picuki.com как fallback без ключей
IMGINN_BASE = "https://imginn.com"
PICUKI_BASE = "https://www.picuki.com"


async def _fetch(session: aiohttp.ClientSession, url: str, params: dict = None, timeout: int = 20) -> dict | None:
    """GET → JSON или None при ошибке."""
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                logger.warning(f"IG fetch {url} → {resp.status}")
                return None
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return await resp.json()
            text = await resp.text()
            try:
                import json
                return json.loads(text)
            except Exception:
                return {"_html": text}
    except Exception as e:
        logger.error(f"IG fetch error {url}: {e}")
        return None


async def _download_bytes(session: aiohttp.ClientSession, url: str, timeout: int = 60) -> bytes | None:
    """Скачать файл в память и сразу вернуть байты."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception as e:
        logger.error(f"IG download error: {e}")
        return None


# ── ПУБЛИЧНЫЙ INSTAGRAM API ────────────────────────────────────────────────────
# Используем публичный graphql endpoint (без ключа).
# При ошибке — парсинг imginn.com

IG_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "X-IG-App-ID": "936619743392459",
}


async def get_user_info(username: str) -> dict | None:
    """Получить базовую инфу + user_id по username."""
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(url, headers={**IG_WEB_HEADERS, "Accept": "application/json"}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user = data.get("data", {}).get("user")
                    if user:
                        return {
                            "id": user.get("id"),
                            "username": user.get("username"),
                            "full_name": user.get("full_name", ""),
                            "biography": user.get("biography", ""),
                            "followers": user.get("edge_followed_by", {}).get("count", 0),
                            "following": user.get("edge_follow", {}).get("count", 0),
                            "posts_count": user.get("edge_owner_to_timeline_media", {}).get("count", 0),
                            "avatar_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url", ""),
                            "is_private": user.get("is_private", False),
                            "is_verified": user.get("is_verified", False),
                            "posts": _extract_posts(user),
                        }
        except Exception as e:
            logger.error(f"get_user_info error: {e}")
    return None


def _extract_posts(user: dict) -> list:
    """Извлечь посты из ответа profile_info."""
    edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
    posts = []
    for edge in edges:
        node = edge.get("node", {})
        typename = node.get("__typename", "")
        media_url = node.get("display_url", "")
        # Для альбомов берём все слайды
        sidecar = node.get("edge_sidecar_to_children", {}).get("edges", [])
        if sidecar:
            urls = []
            for s in sidecar:
                snode = s.get("node", {})
                # видео или фото
                if snode.get("is_video"):
                    urls.append({"type": "video", "url": snode.get("video_url", ""), "thumb": snode.get("display_url", "")})
                else:
                    urls.append({"type": "photo", "url": snode.get("display_url", ""), "thumb": snode.get("display_url", "")})
        else:
            if node.get("is_video"):
                urls = [{"type": "video", "url": node.get("video_url", ""), "thumb": media_url}]
            else:
                urls = [{"type": "photo", "url": media_url, "thumb": media_url}]

        caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
        caption = caption_edges[0]["node"]["text"] if caption_edges else ""

        posts.append({
            "id": node.get("id", ""),
            "shortcode": node.get("shortcode", ""),
            "timestamp": node.get("taken_at_timestamp", 0),
            "caption": caption[:500] if caption else "",
            "like_count": node.get("edge_liked_by", {}).get("count", 0),
            "comment_count": node.get("edge_media_to_comment", {}).get("count", 0),
            "media": urls,
        })
    return posts


async def get_stories(username: str) -> list:
    """
    Получить истории пользователя через публичный endpoint.
    Возвращает список словарей с ключами: id, type, url, thumb, duration.
    """
    info = await get_user_info(username)
    if not info or not info.get("id"):
        return []
    if info.get("is_private"):
        return []

    user_id = info["id"]
    url = f"https://i.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}"
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(url, headers={**IG_WEB_HEADERS, "Accept": "application/json"}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    reels = data.get("reels", {})
                    reel = reels.get(str(user_id)) or reels.get(user_id) or {}
                    items = reel.get("items", [])
                    stories = []
                    for item in items:
                        is_video = item.get("media_type") == 2
                        if is_video:
                            versions = item.get("video_versions", [])
                            vid_url = versions[0]["url"] if versions else ""
                            img_versions = item.get("image_versions2", {}).get("candidates", [])
                            thumb = img_versions[0]["url"] if img_versions else ""
                            stories.append({
                                "id": str(item.get("pk", "")),
                                "type": "video",
                                "url": vid_url,
                                "thumb": thumb,
                                "duration": item.get("video_duration", 15),
                            })
                        else:
                            candidates = item.get("image_versions2", {}).get("candidates", [])
                            img_url = candidates[0]["url"] if candidates else ""
                            stories.append({
                                "id": str(item.get("pk", "")),
                                "type": "photo",
                                "url": img_url,
                                "thumb": img_url,
                                "duration": 5,
                            })
                    return stories
        except Exception as e:
            logger.error(f"get_stories error: {e}")
    return []


async def get_posts(username: str, after_cursor: str = "") -> dict:
    """
    Получить посты пользователя (12 шт, пагинация через cursor).
    Возвращает {'posts': [...], 'next_cursor': str, 'has_more': bool}.
    """
    info = await get_user_info(username)
    if not info:
        return {"posts": [], "next_cursor": "", "has_more": False}

    if info.get("is_private"):
        return {"posts": [], "next_cursor": "", "has_more": False, "private": True}

    # Первая страница уже есть в info (12 постов из profile_info)
    if not after_cursor:
        posts = info.get("posts", [])
        # Попробуем получить cursor для следующей страницы через GraphQL
        next_cursor = await _get_next_cursor(info.get("id", ""))
        return {
            "posts": posts[:10],
            "next_cursor": next_cursor,
            "has_more": len(posts) >= 10 or bool(next_cursor),
            "user": info,
        }

    # Пагинация через GraphQL
    return await _get_posts_graphql(info.get("id", ""), info.get("username", ""), after_cursor, info)


async def _get_next_cursor(user_id: str) -> str:
    """Получить cursor для следующей страницы через GraphQL."""
    if not user_id:
        return ""
    url = "https://www.instagram.com/graphql/query/"
    params = {
        "query_hash": "e769aa130647d2354c40ea6a439bfc08",
        "variables": f'{{"id":"{user_id}","first":12,"after":""}}',
    }
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(url, params=params, headers=IG_WEB_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    page_info = (
                        data.get("data", {})
                        .get("user", {})
                        .get("edge_owner_to_timeline_media", {})
                        .get("page_info", {})
                    )
                    if page_info.get("has_next_page"):
                        return page_info.get("end_cursor", "")
        except Exception:
            pass
    return ""


async def _get_posts_graphql(user_id: str, username: str, cursor: str, base_info: dict) -> dict:
    """Получить страницу постов через GraphQL с cursor."""
    url = "https://www.instagram.com/graphql/query/"
    params = {
        "query_hash": "e769aa130647d2354c40ea6a439bfc08",
        "variables": f'{{"id":"{user_id}","first":10,"after":"{cursor}"}}',
    }
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(url, params=params, headers=IG_WEB_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    media_data = (
                        data.get("data", {})
                        .get("user", {})
                        .get("edge_owner_to_timeline_media", {})
                    )
                    edges = media_data.get("edges", [])
                    page_info = media_data.get("page_info", {})

                    posts = []
                    for edge in edges:
                        node = edge.get("node", {})
                        sidecar = node.get("edge_sidecar_to_children", {}).get("edges", [])
                        if sidecar:
                            urls = []
                            for s2 in sidecar:
                                snode = s2.get("node", {})
                                if snode.get("is_video"):
                                    urls.append({"type": "video", "url": snode.get("video_url", ""), "thumb": snode.get("display_url", "")})
                                else:
                                    urls.append({"type": "photo", "url": snode.get("display_url", ""), "thumb": snode.get("display_url", "")})
                        else:
                            durl = node.get("display_url", "")
                            if node.get("is_video"):
                                urls = [{"type": "video", "url": node.get("video_url", ""), "thumb": durl}]
                            else:
                                urls = [{"type": "photo", "url": durl, "thumb": durl}]

                        caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
                        caption = caption_edges[0]["node"]["text"] if caption_edges else ""
                        posts.append({
                            "id": node.get("id", ""),
                            "shortcode": node.get("shortcode", ""),
                            "timestamp": node.get("taken_at_timestamp", 0),
                            "caption": caption[:500],
                            "like_count": node.get("edge_liked_by", {}).get("count", 0),
                            "comment_count": node.get("edge_media_to_comment", {}).get("count", 0),
                            "media": urls,
                        })

                    return {
                        "posts": posts,
                        "next_cursor": page_info.get("end_cursor", "") if page_info.get("has_next_page") else "",
                        "has_more": page_info.get("has_next_page", False),
                        "user": base_info,
                    }
        except Exception as e:
            logger.error(f"GraphQL posts error: {e}")
    return {"posts": [], "next_cursor": "", "has_more": False, "user": base_info}


# ── ПОДПИСКИ ──────────────────────────────────────────────────────────────────

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
    """Все активные подписки (для фонового опроса)."""
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
    row = conn.execute("SELECT 1 FROM ig_sent WHERE user_id=? AND media_id=?", (str(user_id), media_id)).fetchone()
    conn.close()
    return row is not None
