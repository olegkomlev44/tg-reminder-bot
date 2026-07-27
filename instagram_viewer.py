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
import re
import sqlite3
import json
import time
import random
import html
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── КЭШ ПОСТОВ (in-memory) ────────────────────────────────────────────────────
# Хранит полный список постов по username, чтобы пагинация offset-ом работала
# стабильно (без повторного парсинга при нажатии «Далее»).
# Структура: { username: {"posts": [...], "ts": float} }
_POSTS_CACHE: dict[str, dict] = {}
_POSTS_CACHE_TTL = 300  # секунды (5 минут)


def _cache_set_posts(username: str, posts: list):
    _POSTS_CACHE[username] = {"posts": posts, "ts": time.time()}


def _cache_get_posts(username: str) -> list | None:
    entry = _POSTS_CACHE.get(username)
    if not entry:
        return None
    if time.time() - entry["ts"] > _POSTS_CACHE_TTL:
        del _POSTS_CACHE[username]
        return None
    return entry["posts"]


def _is_video_url(url: str) -> bool:
    """Определить по URL, что это видео (mp4 / /videos/)."""
    if not url:
        return False
    u = url.lower().split("?")[0]
    return u.endswith(".mp4") or "/videos/" in u or "video" in u


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
    c.execute("""CREATE TABLE IF NOT EXISTS ig_subscriptions (
        user_id     TEXT NOT NULL,
        ig_username TEXT NOT NULL,
        sub_type    TEXT NOT NULL,   -- 'stories' | 'posts' | 'avatar'
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


# ── ЗАГОЛОВКИ ─────────────────────────────────────────────────────────────────

# Заголовки браузера для обхода блокировок
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",  # <-- Убрали br для стабильности
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


# Заголовки для API-запросов Instagram (внутренний API)
IG_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": "936619743392459",
    "X-ASBD-ID": "198387",
    "X-IG-WWW-Claim": "0",
    "Origin": "https://www.instagram.com",
    "Referer": "https://www.instagram.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


# ── ВСПОМОГАТЕЛЬНЫЕ ───────────────────────────────────────────────────────────

async def _fetch_json(session: aiohttp.ClientSession, url: str, headers: dict, params: dict = None, timeout: int = 20) -> dict | None:
    """GET → JSON или None при ошибке."""
    try:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            ssl=False  # некоторые scraper-сайты с самоподписанными сертами
        ) as resp:
            if resp.status != 200:
                logger.warning(f"IG fetch {url[:80]} → {resp.status}")
                return None
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return await resp.json(content_type=None)
            text = await resp.text()
            try:
                return json.loads(text)
            except Exception:
                return {"_html": text}
    except Exception as e:
        logger.error(f"IG fetch error {url[:80]}: {e}")
        return None


async def _download_bytes(session: aiohttp.ClientSession, url: str, timeout: int = 60) -> bytes | None:
    """Скачать файл в память."""
    # CDN Instagram (cdninstagram.com / scontent-*.cdninstagram.com) требует
    # Referer на instagram.com, иначе отдаёт 403/empty
    cdn_headers = {
        **BROWSER_HEADERS,
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Dest": "image",
    }
    try:
        async with session.get(
            url, headers=cdn_headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            ssl=False,
            allow_redirects=True,
        ) as resp:
            if resp.status == 200:
                data = await resp.read()
                return data if len(data) > 500 else None
            logger.warning(f"IG download {url[:60]!r} → {resp.status}")
    except Exception as e:
        logger.error(f"IG download error: {e}")
    return None

async def _get_user_via_ig_meta(username: str) -> dict | None:
    """Прямой парсинг мета-тегов с самого Instagram, прикидываясь поисковиком (Googlebot)."""
    url = f"https://www.instagram.com/{username}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return None
                
                # ПЕРЕИМЕНОВАЛИ ПЕРЕМЕННУЮ В html_text, ЧТОБЫ НЕ СЛОМАТЬ МОДУЛЬ html
                html_text = await resp.text()

                meta_desc = re.search(r'<meta property="og:description" content="([^"]*)"', html_text)
                meta_title = re.search(r'<meta property="og:title" content="([^"]*)"', html_text)
                meta_image = re.search(r'<meta property="og:image" content="([^"]*)"', html_text)

                if not meta_desc or not meta_title:
                    return None

                desc = meta_desc.group(1)
                f_match = re.search(r'([\d,KkMm\.]+)\s+[Ff]ollowers', desc)
                fing_match = re.search(r'([\d,KkMm\.]+)\s+[Ff]ollowing', desc)
                p_match = re.search(r'([\d,KkMm\.]+)\s+[Pp]osts', desc)

                def _parse_num(s_val):
                    if not s_val: return 0
                    s_val = s_val.upper().replace(",", "").replace(" ", "")
                    if "M" in s_val: return int(float(s_val.replace("M", "")) * 1_000_000)
                    if "K" in s_val: return int(float(s_val.replace("K", "")) * 1_000)
                    try: return int(s_val)
                    except: return 0

                id_match = re.search(r'"profilePage_([0-9]+)"', html_text)
                if not id_match:
                    id_match = re.search(r'"id":"([0-9]+)"', html_text)
                user_id = id_match.group(1) if id_match else ""

                # ТЕПЕРЬ МОДУЛЬ html СРАБОТАЕТ КОРРЕКТНО
                title = html.unescape(meta_title.group(1))
                name_match = re.search(r'^(.+?)\s*[@(]', title)
                full_name = name_match.group(1).strip() if name_match else username

                return {
                    "id": user_id,
                    "username": username,
                    "full_name": full_name,
                    "biography": "", 
                    "followers": _parse_num(f_match.group(1) if f_match else ""),
                    "following": _parse_num(fing_match.group(1) if fing_match else ""),
                    "posts_count": _parse_num(p_match.group(1) if p_match else ""),
                    "avatar_url": meta_image.group(1).replace("\\u0026", "&") if meta_image else "",
                    "is_private": False,
                    "is_verified": False,
                    "posts": [],
                }
    except Exception as e:
        logger.error(f"ig_meta error @{username}: {e}")
    return None

# ── МЕТОД 1: SCRAPER VIA PICNOB ───────────────────────────────────────────────

async def _get_user_via_picnob(username: str) -> dict | None:
    """Получить данные профиля через picnob.com (публичный парсинг без API)."""
    url = f"https://www.picnob.com/profile/{username}/"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=BROWSER_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"picnob {username} → {resp.status}")
                    return None
                html = await resp.text()

        # Парсим JSON из window.__additionalData или __reactRouterContext
        # Ищем ключевые данные профиля через regex
        info = {}

        # Извлекаем количество подписчиков
        followers_match = re.search(r'"edge_followed_by":\{"count":(\d+)\}', html)
        following_match = re.search(r'"edge_follow":\{"count":(\d+)\}', html)
        posts_match = re.search(r'"edge_owner_to_timeline_media":\{"count":(\d+)', html)
        fullname_match = re.search(r'"full_name":"([^"]*)"', html)
        bio_match = re.search(r'"biography":"([^"]*)"', html)
        avatar_match = re.search(r'"profile_pic_url_hd":"([^"]+)"', html)
        if not avatar_match:
            avatar_match = re.search(r'"profile_pic_url":"([^"]+)"', html)
        id_match = re.search(r'"id":"(\d+)"', html)
        private_match = re.search(r'"is_private":(true|false)', html)
        verified_match = re.search(r'"is_verified":(true|false)', html)

        if not (followers_match or id_match):
            # Пробуем альтернативный парсинг meta-тегов
            meta_desc = re.search(r'<meta property="og:description" content="([^"]*)"', html)
            meta_title = re.search(r'<meta property="og:title" content="([^"]*)"', html)
            if meta_desc and meta_title:
                # "969 Followers, 291 Following, 55 Posts - See Instagram..."
                desc = meta_desc.group(1)
                followers_m = re.search(r'([\d,]+)\s+Followers?', desc)
                following_m = re.search(r'([\d,]+)\s+Following', desc)
                posts_m = re.search(r'([\d,]+)\s+Posts?', desc)
                if followers_m:
                    info["followers"] = int(followers_m.group(1).replace(",", ""))
                if following_m:
                    info["following"] = int(following_m.group(1).replace(",", ""))
                if posts_m:
                    info["posts_count"] = int(posts_m.group(1).replace(",", ""))
                # username из title
                title = meta_title.group(1)
                name_m = re.search(r'^(.+?)\s*[@(]', title)
                if name_m:
                    info["full_name"] = name_m.group(1).strip()
                # avatar
                avatar_og = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                if avatar_og:
                    info["avatar_url"] = avatar_og.group(1).replace("\\u0026", "&")
                if info.get("followers") is not None:
                    info.update({
                        "id": "",
                        "username": username,
                        "biography": "",
                        "is_private": False,
                        "is_verified": False,
                        "posts": [],
                    })
                    logger.info(f"picnob meta-парсинг успешен для @{username}")
                    return info

            logger.warning(f"picnob: не удалось распарсить данные @{username}")
            return None

        info = {
            "id": id_match.group(1) if id_match else "",
            "username": username,
            "full_name": _unescape_unicode(fullname_match.group(1)) if fullname_match else "",
            "biography": _unescape_unicode(bio_match.group(1)) if bio_match else "",
            "followers": int(followers_match.group(1)) if followers_match else 0,
            "following": int(following_match.group(1)) if following_match else 0,
            "posts_count": int(posts_match.group(1)) if posts_match else 0,
            "avatar_url": avatar_match.group(1).replace("\\u0026", "&") if avatar_match else "",
            "is_private": (private_match.group(1) == "true") if private_match else False,
            "is_verified": (verified_match.group(1) == "true") if verified_match else False,
            "posts": [],
        }
        logger.info(f"picnob успешно: @{username} ({info['followers']} подписчиков)")
        return info
    except Exception as e:
        logger.error(f"picnob error @{username}: {e}")
        return None


# ── МЕТОД 2: IMGINN ───────────────────────────────────────────────────────────

async def _get_user_via_imginn(username: str) -> dict | None:
    """Получить данные профиля через imginn.com."""
    url = f"https://imginn.com/{username}/"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=BROWSER_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"imginn {username} → {resp.status}")
                    return None
                html = await resp.text()

        info = {}

        # imginn использует специфичный HTML
        # Парсим через meta + inline данные
        meta_desc = re.search(r'<meta name="description" content="([^"]*)"', html)
        meta_og_image = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        meta_title = re.search(r'<meta property="og:title" content="([^"]*)"', html)

        # Счётчики
        followers_m = re.search(r'([\d,]+)\s*followers', html, re.IGNORECASE)
        following_m = re.search(r'([\d,]+)\s*following', html, re.IGNORECASE)
        posts_m = re.search(r'([\d,]+)\s*posts', html, re.IGNORECASE)

        # Full name
        fullname_m = re.search(r'<h1[^>]*class="[^"]*fullname[^"]*"[^>]*>([^<]+)</h1>', html)
        if not fullname_m:
            fullname_m = re.search(r'<span[^>]*class="[^"]*fullname[^"]*"[^>]*>([^<]+)</span>', html)

        # Bio
        bio_m = re.search(r'<p[^>]*class="[^"]*desc[^"]*"[^>]*>(.*?)</p>', html, re.DOTALL)

        if not (followers_m or posts_m):
            logger.warning(f"imginn: не удалось распарсить @{username}")
            return None

        avatar_url = meta_og_image.group(1) if meta_og_image else ""

        info = {
            "id": "",
            "username": username,
            "full_name": fullname_m.group(1).strip() if fullname_m else (username if not meta_title else re.sub(r'\s*[@(].*', '', meta_title.group(1)).strip()),
            "biography": re.sub(r'<[^>]+>', '', bio_m.group(1)).strip() if bio_m else "",
            "followers": int(followers_m.group(1).replace(",", "")) if followers_m else 0,
            "following": int(following_m.group(1).replace(",", "")) if following_m else 0,
            "posts_count": int(posts_m.group(1).replace(",", "")) if posts_m else 0,
            "avatar_url": avatar_url,
            "is_private": "private" in html.lower() and "this account is private" in html.lower(),
            "is_verified": False,
            "posts": [],
        }
        logger.info(f"imginn успешно: @{username}")
        return info
    except Exception as e:
        logger.error(f"imginn error @{username}: {e}")
        return None


# ── МЕТОД 3: STORIESIG / ANON STORY VIEWER ────────────────────────────────────

async def _get_user_via_storiesig(username: str) -> dict | None:
    """Получить данные через storiesig API (публичный endpoint)."""
    url = f"https://storiesig.info/api/ig/user/{username}"
    try:
        async with aiohttp.ClientSession() as s:
            data = await _fetch_json(s, url, headers=BROWSER_HEADERS, timeout=15)
            if not data or data.get("error"):
                return None
            user = data.get("data") or data.get("user") or data
            if not user or not isinstance(user, dict):
                return None

            return {
                "id": str(user.get("pk") or user.get("id") or ""),
                "username": user.get("username", username),
                "full_name": user.get("full_name", ""),
                "biography": user.get("biography", ""),
                "followers": user.get("follower_count") or user.get("edge_followed_by", {}).get("count", 0),
                "following": user.get("following_count") or user.get("edge_follow", {}).get("count", 0),
                "posts_count": user.get("media_count") or user.get("edge_owner_to_timeline_media", {}).get("count", 0),
                "avatar_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url", ""),
                "is_private": user.get("is_private", False),
                "is_verified": user.get("is_verified", False),
                "posts": [],
            }
    except Exception as e:
        logger.error(f"storiesig error @{username}: {e}")
    return None


# ── МЕТОД 4: INSTAGRAM ОФИЦИАЛЬНЫЙ ENDPOINT (fallback) ────────────────────────

async def _get_user_via_official(username: str) -> dict | None:
    """
    Попытка через официальный Instagram API (может не работать без cookies).
    Используется как последний fallback.
    """
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers={**IG_API_HEADERS, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    user = data.get("data", {}).get("user")
                    if user:
                        return {
                            "id": user.get("id", ""),
                            "username": user.get("username", username),
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
        logger.error(f"official API error @{username}: {e}")
    return None


# ── МЕТОД 5: INSTAGRAM VIA OEMBED ─────────────────────────────────────────────

async def _get_user_via_oembed(username: str) -> dict | None:
    """
    Instagram oEmbed endpoint — работает для публичных профилей.
    """
    url = f"https://www.instagram.com/{username}/?__a=1&__d=dis"
    headers = {
        **BROWSER_HEADERS,
        "Accept": "application/json, text/html, */*",
        "X-Requested-With": "XMLHttpRequest"
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                        user = data.get("graphql", {}).get("user") or data.get("data", {}).get("user")
                        if user:
                            return {
                                "id": str(user.get("id", "")),
                                "username": username,
                                "full_name": user.get("full_name", username),
                                "biography": user.get("biography", ""),
                                "followers": user.get("edge_followed_by", {}).get("count", 0),
                                "following": user.get("edge_follow", {}).get("count", 0),
                                "posts_count": user.get("edge_owner_to_timeline_media", {}).get("count", 0),
                                "avatar_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url", ""),
                                "is_private": user.get("is_private", False),
                                "is_verified": user.get("is_verified", False),
                                "posts": [],
                            }
                    except Exception:
                        pass
    except Exception as e:
        logger.debug(f"oembed error @{username}: {e}")
    return None



# ── ГЛАВНАЯ ФУНКЦИЯ: КАСКАДНЫЙ ПОИСК ─────────────────────────────────────────

async def get_user_info(username: str) -> dict | None:
    """
    Получить инфу о пользователе Instagram.
    Пробует методы по очереди: picnob → imginn → storiesig → official API → oembed.
    Возвращает первый успешный результат или None.
    """
    username = username.lower().strip("@").strip()
    if not username:
        return None

    # Добавляем случайную задержку чтобы не палиться как бот
    await asyncio.sleep(random.uniform(0.3, 0.8))

    methods = [
        ("ig_meta", _get_user_via_ig_meta),  # <-- ДОБАВИЛИ СЮДА ПЕРВЫМ
        ("picnob", _get_user_via_picnob),
        ("imginn", _get_user_via_imginn),
        ("storiesig", _get_user_via_storiesig),
        ("official", _get_user_via_official),
        ("oembed", _get_user_via_oembed),
    ]

    for method_name, method_fn in methods:
        try:
            logger.info(f"Пробую {method_name} для @{username}...")
            result = await method_fn(username)
            if result:
                logger.info(f"✅ {method_name} вернул данные для @{username}")
                return result
        except Exception as e:
            logger.warning(f"❌ {method_name} упал для @{username}: {e}")
        # Пауза между методами
        await asyncio.sleep(0.3)

    logger.error(f"Все методы не дали результата для @{username}")
    return None


def _unescape_unicode(s: str) -> str:
    """Декодировать \\uXXXX escape-последовательности."""
    try:
        return s.encode("raw_unicode_escape").decode("unicode_escape")
    except Exception:
        return s


def _extract_posts(user: dict) -> list:
    """Извлечь посты из ответа profile_info (для официального API)."""
    edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
    posts = []
    for edge in edges:
        node = edge.get("node", {})
        media_url = node.get("display_url", "")
        sidecar = node.get("edge_sidecar_to_children", {}).get("edges", [])
        if sidecar:
            urls = []
            for s in sidecar:
                snode = s.get("node", {})
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


# ── ИСТОРИИ ───────────────────────────────────────────────────────────────────

async def get_stories(username: str) -> list:
    """
    Получить истории пользователя.
    Пробует несколько методов.
    """
    info = await get_user_info(username)
    if not info:
        return []
    if info.get("is_private"):
        return []

    user_id = info.get("id", "")

    # Метод 1: официальный reels_media endpoint (нужен user_id)
    if user_id:
        stories = await _get_stories_official(user_id)
        if stories:
            return stories

    # Метод 2: через storiesig (по username — user_id не нужен)
    stories = await _get_stories_via_storiesig(username)
    if stories:
        return stories

    # Метод 3: через instastories.io
    stories = await _get_stories_via_instastories(username)
    if stories:
        return stories

    # Метод 4: snapinsta / iganony как последний шанс
    stories = await _get_stories_via_iganony(username)
    return stories


async def _get_stories_via_iganony(username: str) -> list:
    """Запрос через iganony.io API — работает без user_id, по username."""
    url = f"https://iganony.io/api/stories?username={username}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers={**BROWSER_HEADERS, "Referer": "https://iganony.io/"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"iganony stories {username} → {resp.status}")
                    return []
                data = await resp.json(content_type=None)
        items = data if isinstance(data, list) else (data.get("data") or data.get("items") or [])
        stories = []
        for item in items:
            is_video = item.get("is_video") or item.get("type") == "video"
            stories.append({
                "id": str(item.get("id") or item.get("pk") or f"ig_{len(stories)}"),
                "type": "video" if is_video else "photo",
                "url": item.get("video_url") or item.get("url") or item.get("display_url", ""),
                "thumb": item.get("thumbnail_url") or item.get("display_url", ""),
                "duration": item.get("video_duration", 15) if is_video else 5,
            })
        if stories:
            logger.info(f"✅ iganony вернул {len(stories)} историй для @{username}")
        return stories
    except Exception as e:
        logger.error(f"iganony stories error @{username}: {e}")
    return []


async def _get_stories_official(user_id: str) -> list:
    """Через официальный endpoint Instagram."""
    url = f"https://i.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers={**IG_API_HEADERS, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    reels = data.get("reels", {})
                    reel = reels.get(str(user_id)) or reels.get(user_id) or {}
                    items = reel.get("items", [])
                    if items:
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
        logger.error(f"official stories error: {e}")
    return []


async def _get_stories_via_storiesig(username: str) -> list:
    """Через storiesig.info API."""
    url = f"https://storiesig.info/api/ig/stories/{username}"
    try:
        async with aiohttp.ClientSession() as s:
            data = await _fetch_json(s, url, headers=BROWSER_HEADERS, timeout=15)
            if not data:
                return []
            items = data.get("data") or data.get("items") or []
            if not isinstance(items, list):
                return []
            stories = []
            for item in items:
                is_video = item.get("is_video") or item.get("media_type") == 2
                stories.append({
                    "id": str(item.get("pk") or item.get("id") or ""),
                    "type": "video" if is_video else "photo",
                    "url": item.get("video_url") or item.get("url") or item.get("display_url", ""),
                    "thumb": item.get("thumbnail_url") or item.get("display_url", ""),
                    "duration": item.get("video_duration", 15) if is_video else 5,
                })
            return stories
    except Exception as e:
        logger.error(f"storiesig stories error: {e}")
    return []


async def _get_stories_via_instastories(username: str) -> list:
    """Через instastories.io (парсинг)."""
    url = f"https://www.instastories.watch/p/{username}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=BROWSER_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()

        # Ищем ссылки на медиа
        stories = []
        video_urls = re.findall(r'"videoSrc":"([^"]+)"', html)
        photo_urls = re.findall(r'"imgSrc":"([^"]+)"', html)

        for i, url in enumerate(video_urls):
            stories.append({
                "id": f"v_{i}",
                "type": "video",
                "url": url.replace("\\u0026", "&"),
                "thumb": "",
                "duration": 15,
            })
        for i, url in enumerate(photo_urls):
            stories.append({
                "id": f"p_{i}",
                "type": "photo",
                "url": url.replace("\\u0026", "&"),
                "thumb": url.replace("\\u0026", "&"),
                "duration": 5,
            })
        return stories
    except Exception as e:
        logger.error(f"instastories error: {e}")
    return []


# ── ПОСТЫ ─────────────────────────────────────────────────────────────────────

async def get_posts(username: str, after_cursor: str = "") -> dict:
    info = await get_user_info(username)
    if not info:
        return {"posts": [], "next_cursor": "", "has_more": False}

    if info.get("is_private"):
        return {"posts": [], "next_cursor": "", "has_more": False, "private": True}

    # Определяем тип курсора (числовой для скраперов, строка для GraphQL)
    is_graphql_cursor = after_cursor and not after_cursor.isdigit()
    offset = int(after_cursor) if after_cursor.isdigit() else 0

    # Если курсор от GraphQL, сразу идём туда
    if is_graphql_cursor and info.get("id"):
        result = await _get_posts_graphql(info["id"], username, after_cursor, info)
        return result

    # КАСКАДНЫЙ ПОИСК ПОСТОВ — с кэшем.
    # При первом запросе (offset==0) проверяем кэш или парсим заново.
    # При последующих (offset>0) ВСЕГДА берём из кэша — не парсим снова.
    posts = _cache_get_posts(username) if offset > 0 else None

    if posts is None:
        # Парсим и кэшируем
        posts = []
        if not posts:
            posts = await _get_posts_via_ig_html(username)
        if not posts:
            posts = await _get_posts_via_picuki(username)
        if not posts:
            posts = await _get_posts_via_imginn(username)
        if not posts:
            posts = await _get_posts_via_picnob(username)

        if posts:
            # Дедупликация по shortcode
            seen_ids: set = set()
            unique: list = []
            for p in posts:
                pid = p.get("shortcode") or p.get("id") or ""
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    unique.append(p)
            posts = unique
            _cache_set_posts(username, posts)

    if posts:
        chunk = posts[offset : offset + 10]
        has_more = (offset + 10) < len(posts)
        next_cursor = str(offset + 10) if has_more else ""

        return {
            "posts": chunk,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total_cached": len(posts),
            "user": info,
        }

    # Последний шанс: официальный GraphQL
    if info.get("id"):
        cursor = await _get_next_cursor(info["id"])
        result = await _get_posts_graphql(info["id"], username, cursor or "", info)
        return result

    return {"posts": [], "next_cursor": "", "has_more": False, "user": info}


async def _get_posts_via_ig_html(username: str) -> list:
    url = f"https://www.instagram.com/{username}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return []
                html_text = await resp.text()

        posts = []
        raw_posts = re.findall(r'"shortcode":"([A-Za-z0-9_-]+)".{1,500}?"display_url":"(https?:\/\/[^"]+)"', html_text)
        
        seen = set()
        for shortcode, img_url in raw_posts:
            if shortcode in seen:
                continue
            seen.add(shortcode)
            clean_url = img_url.replace("\\u0026", "&").replace("\\/", "/")
            mtype = "video" if _is_video_url(clean_url) else "photo"
            posts.append({
                "id": shortcode,
                "shortcode": shortcode,
                "timestamp": 0,
                "caption": "",
                "like_count": 0,
                "comment_count": 0,
                "media": [{"type": mtype, "url": clean_url, "thumb": clean_url}],
            })
        if posts:
            logger.info(f"✅ Прямой парсинг HTML вытащил {len(posts)} постов для @{username}")
        return posts
    except Exception as e:
        logger.error(f"ig_html posts error @{username}: {e}")
    return []

async def _get_posts_via_picuki(username: str) -> list:
    url = f"https://www.picuki.com/profile/{username}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=20),
                             allow_redirects=True) as resp:
                if resp.status != 200:
                    return []
                html_text = await resp.text()

        posts = []
        items = re.findall(r'href="/media/([^"]+)"[^>]*>.*?<img[^>]+src="(https?://[^"]+)"', html_text, re.DOTALL)
        if not items:
            items = re.findall(r'href="/media/([^"]+)"[^>]*>.*?<img[^>]+(?:data-src|src)="(https?://[^"]+)"', html_text, re.DOTALL)
            
        seen = set()
        for shortcode, img_url in items:
            sc = shortcode.split("/")[0].split("?")[0]
            if sc in seen or not sc:
                continue
            seen.add(sc)
            clean_url = img_url.replace("&amp;", "&")
            mtype = "video" if _is_video_url(clean_url) else "photo"
            posts.append({
                "id": sc,
                "shortcode": sc,
                "timestamp": 0,
                "caption": "",
                "like_count": 0,
                "comment_count": 0,
                "media": [{"type": mtype, "url": clean_url, "thumb": clean_url}],
            })
        return posts
    except Exception as e:
        logger.error(f"picuki posts error @{username}: {e}")
    return []

async def _get_posts_via_imginn(username: str) -> list:
    url = f"https://imginn.com/{username}/"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=BROWSER_HEADERS, timeout=15) as resp:
                if resp.status != 200:
                    return []
                html_text = await resp.text()

        posts = []
        items = re.findall(r'href="/p/([^/]+)/"[^>]*>.*?<img[^>]+src="([^"]+)"', html_text, re.DOTALL)
        for shortcode, img_url in items:
            clean_url = img_url.replace("&amp;", "&")
            mtype = "video" if _is_video_url(clean_url) else "photo"
            posts.append({
                "id": shortcode,
                "shortcode": shortcode,
                "timestamp": 0,
                "caption": "",
                "like_count": 0,
                "comment_count": 0,
                "media": [{"type": mtype, "url": clean_url, "thumb": clean_url}],
            })
        return posts
    except Exception as e:
        logger.error(f"imginn posts error: {e}")
    return []

async def _get_posts_via_picnob(username: str) -> list:
    url = f"https://www.picnob.com/profile/{username}/"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=BROWSER_HEADERS, timeout=20) as resp:
                if resp.status != 200:
                    return []
                html_text = await resp.text()

        posts = []
        items = re.findall(r'data-shortcode="([^"]+)"[^>]*>.*?<img[^>]+(?:data-src|src)="([^"]+)"', html_text, re.DOTALL)
        for shortcode, img_url in items:
            clean_url = img_url.replace("&amp;", "&")
            mtype = "video" if _is_video_url(clean_url) else "photo"
            posts.append({
                "id": shortcode,
                "shortcode": shortcode,
                "timestamp": 0,
                "caption": "",
                "like_count": 0,
                "comment_count": 0,
                "media": [{"type": mtype, "url": clean_url, "thumb": clean_url}],
            })
        return posts
    except Exception as e:
        logger.error(f"picnob posts error: {e}")
    return []

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
            async with s.get(
                url, params=params, headers=IG_API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
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
            async with s.get(
                url, params=params, headers=IG_API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
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
    
def _clean_username(text: str) -> str:
    text = text.strip().lstrip("@")
    # Извлекаем логин из ссылки, отсекая query-параметры (?igsh=...) и слэши
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", text)
    if m:
        return m.group(1).rstrip("/").lower()
    return re.sub(r"[^A-Za-z0-9_.]", "", text).lower()


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
