"""
instagram_handlers.py — хендлеры Telegram-бота для анонимного просмотра Instagram.

Подключение в main.py:
    from instagram_handlers import register_ig_handlers, ig_checker_task, ig_session_health_task, _set_bot_ref
    _set_bot_ref(bot)                                # ← регистрируем бота для алертов
    register_ig_handlers(dp)
    asyncio.create_task(ig_checker_task(bot))
    asyncio.create_task(ig_session_health_task(bot)) # ← новый health-check таск
"""

import asyncio
import hashlib
import io
import logging
import random
import re
import time
import os
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from io import BytesIO
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument,
    InlineQueryResultArticle, InlineQueryResultCachedPhoto, InputTextMessageContent,
)
try:  # aiogram ≥ 3.2
    from aiogram.types import LinkPreviewOptions
except Exception:  # pragma: no cover
    LinkPreviewOptions = None
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

try:  # aiogram 3.x: пропустить хендлер и отдать сообщение следующему (media_downloader, ИИ-чат)
    from aiogram.dispatcher.event.bases import SkipHandler
except Exception:  # pragma: no cover
    SkipHandler = None

from instagram_viewer import (
    init_ig_db,
    get_user_info, get_stories, get_posts, get_avatar_bytes,
    ig_subscribe, ig_unsubscribe, ig_get_subscriptions,
    ig_is_subscribed, ig_update_last_seen,
    ig_all_subscriptions, ig_mark_sent, ig_already_sent,
    validate_session, get_sessions_status,
    set_session_dead_callback,
    ig_mark_sent_many, ig_get_check_states, ig_set_check_state, ig_cleanup_sent,
    get_pool_health, get_media_by_url,
    set_background_priority, persist_session, IG_POSTS_FORCE_EVERY,
    get_user_status, get_user_status_by_pk, get_latest_posts,
    get_story_by_url, get_highlights, get_highlight_items,
    ig_get_user_state, ig_get_all_user_states, ig_set_user_state, ig_rename_username,
    ig_cache_get, ig_cache_put, ig_cache_drop,
)

logger = logging.getLogger(__name__)

# ── НАСТРОЙКИ ФОНОВОЙ ПРОВЕРКИ ────────────────────────────────────────────────
IG_CHECK_BASE        = int(os.getenv("IG_CHECK_BASE", str(30 * 60)))   # базовый интервал на ник, сек
IG_CHECK_TICK        = int(os.getenv("IG_CHECK_TICK", str(10 * 60)))   # как часто планировщик смотрит «кто пора»
IG_MAX_PER_TICK      = int(os.getenv("IG_MAX_PER_TICK", "12"))         # максимум ников за один тик
IG_NIGHT_START       = int(os.getenv("IG_NIGHT_START", "1"))           # ночью проверяем реже (локальные часы)
IG_NIGHT_END         = int(os.getenv("IG_NIGHT_END", "7"))
IG_POST_MAX_AGE_DAYS = int(os.getenv("IG_POST_MAX_AGE_DAYS", "7"))      # старше — не считаем «новым» постом
IG_STORY_MAX_FAILS   = int(os.getenv("IG_STORY_MAX_FAILS", "3"))        # после стольких неудач историю бросаем
IG_ALERT_ERRORS      = int(os.getenv("IG_ALERT_ERRORS", "8"))           # ошибок/час для алерта админам
IG_AUTO_LINKS        = os.getenv("IG_AUTO_LINKS", "1") != "0"           # авто-скачивание по ссылкам на посты/рилсы
IG_GONE_CONFIRM      = int(os.getenv("IG_GONE_CONFIRM", "2"))           # сколько раз подряд «не найден», прежде чем объявить аккаунт удалённым
IG_GONE_INTERVAL     = int(os.getenv("IG_GONE_INTERVAL", str(24 * 3600)))  # как часто перепроверять «удалённые»
IG_INLINE_TIMEOUT    = float(os.getenv("IG_INLINE_TIMEOUT", "7"))       # сколько ждём IG в inline-режиме
# Куда залить карточку профиля, чтобы получить file_id для inline (id чата/канала, где бот может писать).
# Без этого inline отдаёт текстовую карточку с аватаркой-превью.
_IG_INLINE_STORAGE   = os.getenv("IG_INLINE_STORAGE_CHAT", "").strip()
_TG_FILE_LIMIT       = 49 * 1024 * 1024
_DL_HARD_LIMIT       = 200 * 1024 * 1024
_BG_TASKS: set = set()


def _spawn(coro) -> None:
    """create_task с удержанием ссылки (иначе задачу может собрать GC)."""
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


def _ig_tz():
    name = os.getenv("IG_TZ", "")
    if not name:
        try:
            from config import TIMEZONE as _cfg_tz
            if hasattr(_cfg_tz, "utcoffset"):
                return _cfg_tz
            name = str(_cfg_tz)
        except Exception:
            name = "Europe/Moscow"
    try:
        import pytz
        return pytz.timezone(name)
    except Exception:
        return timezone(timedelta(hours=3))


def _ig_local_hour() -> int:
    try:
        return datetime.now(_ig_tz()).hour
    except Exception:
        return datetime.now().hour


# ── АВАТАРКА: состояние в last_seen (пункт 3) ────────────────────────────────
# CDN-ссылка на одну и ту же картинку меняется (подпись, срок жизни, хост),
# поэтому сравнивать целиком URL нельзя. Храним «av2|<path>|<sha1-16>»:
# path — стабильная часть URL, sha1 — хэш байтов картинки.

def _avatar_path(url: str) -> str:
    try:
        return urlparse(url).path or ""
    except Exception:
        return ""


def _avatar_state_parse(last_seen: str) -> tuple[str, str]:
    if not last_seen:
        return "", ""
    if last_seen.startswith("av2|"):
        parts = last_seen.split("|")
        return (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")
    if last_seen.startswith("http"):          # старый формат — голый URL
        return _avatar_path(last_seen), ""
    return "", ""


def _avatar_state_fmt(path: str, digest: str) -> str:
    return f"av2|{path}|{digest}"


def _sha(data: bytes | None) -> str:
    return hashlib.sha1(data).hexdigest()[:16] if data else ""


# ── ГЛОБАЛЬНАЯ ССЫЛКА НА БОТА (для алертов) ──────────────────────────────────

_BOT_REF: "Bot | None" = None

# Пауза между проверками аккаунтов в health-check — с джиттером, чтобы не
# долбить IG пачкой запросов подряд с одного IP.
_IG_HEALTH_DELAY_MIN = float(os.getenv("IG_HEALTH_DELAY_MIN", "8"))
_IG_HEALTH_DELAY_MAX = float(os.getenv("IG_HEALTH_DELAY_MAX", "15"))


def _set_bot_ref(bot: "Bot") -> None:
    """Вызвать из main.py сразу после создания бота."""
    global _BOT_REF
    _BOT_REF = bot


async def _notify_admins_session_dead(ig_login: str) -> None:
    """Оповестить всех IG_ADMIN_IDS о смерти сессии."""
    if not _BOT_REF:
        return
    admin_ids = _get_admin_ids()
    if not admin_ids:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить куки сейчас", callback_data=f"ig_refresh_pick:{ig_login}")
    ]])
    for uid in admin_ids:
        try:
            await _BOT_REF.send_message(
                uid,
                f"⚠️ <b>Сессия Instagram умерла!</b>\n\n"
                f"👤 Аккаунт: <code>{ig_login}</code>\n\n"
                f"<i>Бот продолжает ротировать другие аккаунты.</i>",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"_notify_admins_session_dead {uid}: {e}")


# ── FSM ───────────────────────────────────────────────────────────────────────

class IGStates(StatesGroup):
    waiting_username = State()
    waiting_cookies  = State()


# ── ВСПОМОГАТЕЛЬНЫЕ ───────────────────────────────────────────────────────────

def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _clean_username(text: str) -> str:
    text = text.strip().lstrip("@")
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", text)
    if m:
        first = m.group(1).lower()
        if first in {"p", "reel", "reels", "tv", "stories", "explore", "share"}:
            return ""
        return first
    return re.sub(r"[^A-Za-z0-9_.]", "", text).lower()


async def _download(url: str, kind: str = "photo") -> bytes | None:
    """
    Скачать медиафайл с CDN Instagram.
    CDN требует корректный Referer, иначе отдаёт 403.
    kind="video" — заголовки и таймаут под видео.
    """
    if not url:
        return None
    is_video = kind == "video"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Accept": "*/*" if is_video else "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Sec-Fetch-Dest": "video" if is_video else "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=150 if is_video else 60),
                ssl=False,
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    if (resp.content_length or 0) > _DL_HARD_LIMIT:
                        logger.warning(f"IG download {url[:60]!r}: файл {resp.content_length} Б — пропускаю")
                        return None
                    data = await resp.read()
                    return data if len(data) > 500 else None
                logger.warning(f"IG download {url[:60]!r} → {resp.status}")
    except Exception as e:
        logger.error(f"IG download {url[:60]}: {e}")
    return None


# ── МЕДИА: определение типа по байтам, отправка, кэш file_id ─────────────────
# Раньше тип видео/фото определялся по расширению URL (".mp4"), но у CDN
# Instagram URL заканчивается query-строкой (…mp4?_nc_ht=…), проверка
# endswith(".mp4") не срабатывала, и видео уходило как фото. Теперь тип
# определяется по сигнатуре самого файла.

_IMG_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1", b"avif"}


def _sniff_kind(data: bytes, declared: str = "photo") -> str:
    """'video' / 'photo' по магическим байтам; не распознали — верим declared."""
    if not data or len(data) < 12:
        return declared
    if data[4:8] == b"ftyp":                       # ISO BMFF: mp4/mov или heic/avif
        return "photo" if data[8:12] in _IMG_BRANDS else "video"
    if data[:4] == b"\x1a\x45\xdf\xa3":             # webm / mkv
        return "video"
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return "photo"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "photo"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "photo"
    return declared


async def _video_meta(data: bytes) -> dict:
    """width/height/duration через ffprobe (ffmpeg есть в Dockerfile). Нет ffprobe — пустой dict."""
    def _probe() -> dict:
        import json as _j
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(data)
            f.flush()
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height:format=duration",
                 "-of", "json", f.name],
                capture_output=True, timeout=20,
            )
        j = _j.loads(out.stdout or b"{}")
        st = (j.get("streams") or [{}])[0]
        dur = float((j.get("format") or {}).get("duration") or 0)
        meta = {
            "width": int(st.get("width") or 0) or None,
            "height": int(st.get("height") or 0) or None,
            "duration": int(dur) or None,
        }
        return {k: v for k, v in meta.items() if v}

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _probe)
    except Exception:
        return {}


async def _fetch_media_files(media_list: list[dict], limit: int = 10) -> tuple[list[dict], int]:
    """
    Скачать элементы медиа (dict'ы с type/url/thumb) параллельно (до 4 одновременно).
    Возвращает ([{kind, data, meta, declared, degraded}], сколько_не_влезло_в_50МБ).
    kind — реальный тип по байтам; degraded=True — видео не скачалось, взяли превью.
    """
    sem = asyncio.Semaphore(4)

    async def _one(m: dict):
        declared = m.get("type", "photo")
        url = m.get("url") or ""
        degraded = False
        if declared == "video" and not url:
            declared, url, degraded = "photo", m.get("thumb", ""), True
        async with sem:
            data = await _download(url or m.get("thumb", ""), declared)
            if not data and declared == "video" and m.get("thumb"):
                data = await _download(m["thumb"], "photo")      # видео не далось — хотя бы превью
                degraded = bool(data)
        if not data:
            return None
        kind = _sniff_kind(data, declared)
        if len(data) > _TG_FILE_LIMIT:
            return "big"
        meta = await _video_meta(data) if kind == "video" else {}
        return {"kind": kind, "data": data, "meta": meta,
                "declared": declared, "degraded": degraded or (m.get("type") == "video" and kind != "video")}

    res = await asyncio.gather(*[_one(m) for m in media_list[:limit]])
    files = [r for r in res if isinstance(r, dict)]
    too_big = sum(1 for r in res if r == "big")
    return files, too_big


def _mk_input_media(f: dict, caption=None, parse_mode=None, fname: str = "item"):
    if f["kind"] == "video":
        return InputMediaVideo(
            media=BufferedInputFile(f["data"], filename=f"{fname}.mp4"),
            caption=caption, parse_mode=parse_mode, supports_streaming=True,
            **f.get("meta", {}),
        )
    return InputMediaPhoto(
        media=BufferedInputFile(f["data"], filename=f"{fname}.jpg"),
        caption=caption, parse_mode=parse_mode,
    )


def _mk_cached_media(it: dict, caption=None, parse_mode=None):
    cls = InputMediaVideo if it.get("kind") == "video" else InputMediaPhoto
    return cls(media=it["file_id"], caption=caption, parse_mode=parse_mode)


class _Out:
    """Куда слать: ответом на сообщение / в чат / в личку по id. Единый интерфейс."""

    def __init__(self, video, photo, group):
        self.video, self.photo, self.group = video, photo, group


def _out_message(message: types.Message, reply: bool = True) -> _Out:
    if reply:
        return _Out(message.reply_video, message.reply_photo, message.reply_media_group)
    return _Out(message.answer_video, message.answer_photo, message.answer_media_group)


def _out_chat(bot: Bot, chat_id) -> _Out:
    from functools import partial
    return _Out(partial(bot.send_video, chat_id), partial(bot.send_photo, chat_id),
                partial(bot.send_media_group, chat_id))


async def _send_files(out: _Out, files: list[dict], caption: str | None, kb=None) -> list:
    """Один файл — обычным сообщением (с кнопкой), несколько — альбомом. Подпись HTML."""
    if len(files) == 1:
        f = files[0]
        if f["kind"] == "video":
            m = await out.video(
                BufferedInputFile(f["data"], filename="video.mp4"),
                caption=caption, parse_mode="HTML", reply_markup=kb,
                supports_streaming=True, **f.get("meta", {}),
            )
        else:
            m = await out.photo(
                BufferedInputFile(f["data"], filename="photo.jpg"),
                caption=caption, parse_mode="HTML", reply_markup=kb,
            )
        return [m]
    group = [
        _mk_input_media(f, caption if i == 0 else None, "HTML" if i == 0 else None, str(i))
        for i, f in enumerate(files)
    ]
    return await out.group(group)


async def _send_files_safe(out: _Out, files: list[dict], caption: str | None, kb=None) -> tuple[list, bool]:
    """
    _send_files, но если альбом отклонён (битый файл и т.п.) — шлём по одному.
    Возвращает (сообщения, всё_ушло_одним_заходом). Если не ушло ничего — бросает исключение.
    """
    try:
        return await _send_files(out, files, caption, kb), True
    except Exception as e:
        if len(files) == 1:
            raise
        logger.warning(f"альбом не ушёл ({e}) — отправляю по одному")
    msgs, last_err = [], None
    for i, f in enumerate(files):
        try:
            msgs += await _send_files(out, [f], caption if i == 0 else None)
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.4)
    if not msgs and last_err:
        raise last_err
    return msgs, False


def _fid_of(msg) -> dict | None:
    """{kind, file_id} из отправленного сообщения (для кэша)."""
    try:
        if getattr(msg, "video", None):
            return {"kind": "video", "file_id": msg.video.file_id}
        if getattr(msg, "photo", None):
            return {"kind": "photo", "file_id": msg.photo[-1].file_id}
    except Exception:
        pass
    return None


async def _send_cached(out: _Out, row: dict, caption: str | None = None, kb=None) -> list:
    """Отправить пост по закэшированным file_id — без скачивания и без запросов к IG."""
    caption = row.get("caption") if caption is None else caption
    items = row["items"]
    if len(items) == 1:
        it = items[0]
        send = out.video if it["kind"] == "video" else out.photo
        return [await send(it["file_id"], caption=caption, parse_mode="HTML", reply_markup=kb)]
    group = [
        _mk_cached_media(it, caption if i == 0 else None, "HTML" if i == 0 else None)
        for i, it in enumerate(items)
    ]
    return await out.group(group)


def _cache_items(msgs: list) -> list[dict]:
    return [x for x in (_fid_of(m) for m in msgs) if x]


def _trim_caption(text: str, limit: int = 400) -> str:
    import html as _html
    cap = (text or "").strip()[:limit]
    while len(_html.escape(cap)) > 700:
        cap = cap[:-50]
    return cap


def _post_caption_html(user: str, caption_text: str, likes: int, comments: int, extra: str = "") -> str:
    import html as _html
    cap_raw = _trim_caption(caption_text)
    return (
        (f'📸 <a href="https://instagram.com/{user}">@{user}</a>\n' if user else "")
        + (f"<i>{_html.escape(cap_raw)}</i>\n" if cap_raw else "")
        + f"❤️ {_fmt_count(likes)}   💬 {_fmt_count(comments)}"
        + extra
    )[:1020]


def _profile_btn(user: str):
    if not user:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"👤 профиль @{user}", callback_data=f"ig_profile:{user}")]])


async def _send_story_albums(message: types.Message, stories: list[dict], title_html: str,
                             *, reply: bool = False, limit: int = 50) -> int:
    """
    Отправить истории альбомами по 10 штук (память: качаем и шлём по одной десятке).
    Возвращает, сколько файлов ушло.
    """
    out = _out_message(message, reply=reply)
    stories = stories[:limit]
    n = len(stories)
    sent = 0
    for i in range(0, n, 10):
        chunk = stories[i:i + 10]
        files, _ = await _fetch_media_files(chunk, limit=10)
        if not files:
            continue
        cap = title_html + (f" · {i + 1}–{i + len(chunk)} из {n}" if n > 10 else f" · {n}")
        try:
            msgs, _ok = await _send_files_safe(out, files, cap)
            sent += len(msgs)
        except Exception as e:
            logger.error(f"send story album: {e}")
        await asyncio.sleep(1.0)
    return sent


def _profile_keyboard(ig_username: str, user_id: int) -> InlineKeyboardMarkup:
    uid = str(user_id)
    sub_s = ig_is_subscribed(uid, ig_username, "stories")
    sub_p = ig_is_subscribed(uid, ig_username, "posts")
    sub_a = ig_is_subscribed(uid, ig_username, "avatar")
    filter_p = _get_filter(uid, ig_username, "posts")
    filter_icon = {"all": "🔔", "reels": "🎬", "photos": "🖼", "hot": "🔥"}.get(filter_p, "🔔")

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📸 Истории",  callback_data=f"ig_stories:{ig_username}"),
            InlineKeyboardButton(text="🖼 Посты",     callback_data=f"ig_posts:{ig_username}:"),
            InlineKeyboardButton(text="👥 Похожие",  callback_data=f"ig_related:{ig_username}"),
        ],
        [
            InlineKeyboardButton(
                text=("🔔 Истории: вкл" if sub_s else "🔕 Истории: выкл"),
                callback_data=f"ig_sub:stories:{ig_username}"
            ),
            InlineKeyboardButton(
                text=("🔔 Посты: вкл" if sub_p else "🔕 Посты: выкл"),
                callback_data=f"ig_sub:posts:{ig_username}"
            ),
        ],
        [
            InlineKeyboardButton(
                text=("🔔 Аватарка: вкл" if sub_a else "🔕 Аватарка: выкл"),
                callback_data=f"ig_sub:avatar:{ig_username}"
            ),
            InlineKeyboardButton(
                text=f"{filter_icon} Фильтр уведомлений",
                callback_data=f"ig_sfm:posts:{ig_username}"
            ),
        ],
        [
            InlineKeyboardButton(text="📦 Все истории альбомом", callback_data=f"ig_stall:{ig_username}"),
            InlineKeyboardButton(text="🌟 Хайлайты", callback_data=f"ig_hl:{ig_username}"),
        ],
        [
            InlineKeyboardButton(text="📋 Мои подписки", callback_data="ig_my_subs"),
        ],
    ])


def _stories_keyboard(ig_username: str, user_id: int) -> InlineKeyboardMarkup:
    uid = str(user_id)
    sub_s = ig_is_subscribed(uid, ig_username, "stories")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("🔔 Отписаться от историй" if sub_s else "🔔 Подписаться на истории"),
            callback_data=f"ig_sub:stories:{ig_username}"
        )],
        [
            InlineKeyboardButton(text="📦 Все альбомом", callback_data=f"ig_stall:{ig_username}"),
            InlineKeyboardButton(text="🌟 Хайлайты", callback_data=f"ig_hl:{ig_username}"),
        ],
        [InlineKeyboardButton(text="◀️ Профиль", callback_data=f"ig_profile:{ig_username}")],
    ])


def _posts_keyboard(
    ig_username: str, user_id: int, next_cursor: str, has_more: bool
) -> InlineKeyboardMarkup:
    buttons = []
    if has_more and next_cursor:
        safe_cursor = next_cursor[:100]
        buttons.append([InlineKeyboardButton(
            text="➡️ Далее (следующие посты)",
            callback_data=f"ig_posts:{ig_username}:{safe_cursor}"
        )])
    buttons.append([InlineKeyboardButton(
        text="◀️ Профиль", callback_data=f"ig_profile:{ig_username}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── CURSOR КЭШИ ───────────────────────────────────────────────────────────────

_CURSOR_CACHE: dict[str, str] = {}


def _store_cursor(key: str, cursor: str) -> str:
    short = key[:60]
    _CURSOR_CACHE[short] = cursor
    return short


def _get_cursor(key: str) -> str:
    return _CURSOR_CACHE.get(key, key)


# ── ХЕНДЛЕРЫ ──────────────────────────────────────────────────────────────────

async def cmd_ig(message: types.Message, state: FSMContext):
    """Команда /ig или /instagram."""
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) > 1:
        username = _clean_username(arg[1])
        if username:
            await _show_profile(message, username, message.from_user.id)
            return
    await state.set_state(IGStates.waiting_username)
    sent = await message.answer(
        "📸 *Instagram Viewer*\n\n"
        "Введи имя пользователя Instagram (или вставь ссылку на профиль):\n"
        "_Пример:_ `natgeo` _или_ `https://instagram.com/natgeo`",
        parse_mode="Markdown"
    )
    await state.update_data(prompt_msg_id=sent.message_id)


async def process_ig_username(message: types.Message, state: FSMContext):
    """Обработка введённого username."""
    data = await state.get_data()
    await state.clear()
    try:
        prompt_id = data.get("prompt_msg_id")
        if prompt_id:
            await message.bot.delete_message(message.chat.id, prompt_id)
    except Exception:
        pass

    parsed = _parse_ig_link(message.text or "")
    if parsed:
        if not await _dispatch_ig_link(message, parsed):
            await message.answer("не смог достать — попробуй позже или кинь другую ссылку")
        return

    username = _clean_username(message.text or "")
    if not username:
        await message.answer("❌ Неверный формат. Попробуй ещё раз: /ig username")
        return

    await _show_profile(message, username, message.from_user.id)


async def _show_profile(source, ig_username: str, tg_user_id: int):
    """Показать профиль Instagram."""
    import html
    ig_username = ig_username.lower().strip("@")

    anim = _LoadingAnimation(source)
    await anim.start(f"🔍 Ищу профиль @{ig_username}...")
    await asyncio.sleep(0.3)

    info = await get_user_info(ig_username)

    if not info:
        await anim.done()
        await source.answer(
            f"❌ Пользователь <b>@{html.escape(ig_username)}</b> не найден или недоступен.\n"
            "<i>Возможно, профиль приватный или имя написано неверно.</i>",
            parse_mode="HTML"
        )
        return

    await anim.step("📸 Загружаю аватарку...")

    avatar_bytes: bytes | None = None
    try:
        avatar_bytes = await get_avatar_bytes(ig_username)
    except Exception:
        pass
    if not avatar_bytes:
        avatar_url = info.get("avatar_url", "")
        if avatar_url:
            avatar_bytes = await _download(avatar_url)

    await anim.step("✨ Формирую карточку...")
    await anim.done()

    _record_view(ig_username, str(tg_user_id))
    kb = _profile_keyboard(ig_username, tg_user_id)
    await _send_profile_with_counter_animation(source, info, kb, avatar_bytes)


# ── КОЛБЭК: ПРОФИЛЬ ───────────────────────────────────────────────────────────

async def callback_ig_profile(callback: types.CallbackQuery):
    ig_username = callback.data.split(":", 1)[1]
    await callback.answer("🔍 Загружаю профиль...")
    await _show_profile(callback.message, ig_username, callback.from_user.id)


# ── КОЛБЭК: ИСТОРИИ ───────────────────────────────────────────────────────────

async def callback_ig_stories(callback: types.CallbackQuery):
    ig_username = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Загружаю истории...")

    status = await callback.message.answer(f"📸 Получаю истории @{ig_username}...")
    stories = await get_stories(ig_username)

    try:
        await status.delete()
    except Exception:
        pass

    if not stories:
        await callback.message.answer(
            f"😶 У *@{ig_username}* нет активных историй прямо сейчас.\n"
            "_Истории исчезают через 24 ч. Подпишись, чтобы получать их автоматически!_",
            parse_mode="Markdown",
            reply_markup=_stories_keyboard(ig_username, callback.from_user.id)
        )
        return

    out = _out_message(callback.message, reply=False)
    sent_count = 0
    for story in stories[:20]:
        files, _ = await _fetch_media_files([story], limit=1)
        if not files:
            continue
        try:
            await _send_files(out, files, f"📸 История @{ig_username}")
            sent_count += 1
        except Exception as e:
            logger.error(f"IG send story error: {e}")
        await asyncio.sleep(0.4)

    if sent_count == 0:
        await callback.message.answer("❌ Не удалось загрузить ни одну историю.")
        return

    await callback.message.answer(
        f"✅ Отправлено {sent_count} из {len(stories)} историй @{ig_username}",
        reply_markup=_stories_keyboard(ig_username, callback.from_user.id)
    )


async def callback_ig_stories_all(callback: types.CallbackQuery):
    """«Скачать все истории» — одним альбомом (по 10 в альбоме, если их больше)."""
    ig_username = callback.data.split(":", 1)[1]
    await callback.answer("📦 Собираю альбом...")
    status = await callback.message.answer(f"📦 Собираю истории @{ig_username}...")
    stories = await get_stories(ig_username)
    try:
        await status.delete()
    except Exception:
        pass

    if not stories:
        await callback.message.answer(
            f"😶 У @{ig_username} нет активных историй прямо сейчас.",
            reply_markup=_stories_keyboard(ig_username, callback.from_user.id),
        )
        return

    sent = await _send_story_albums(callback.message, stories, f"📸 Истории @{ig_username}")
    if not sent:
        await callback.message.answer("❌ Не удалось загрузить истории.")
        return
    await callback.message.answer(
        f"✅ Альбом: {sent} из {len(stories)} историй @{ig_username}",
        reply_markup=_stories_keyboard(ig_username, callback.from_user.id),
    )


async def callback_ig_highlights(callback: types.CallbackQuery):
    """Список закреплённых коллекций историй (хайлайтов)."""
    import html as _html
    ig_username = callback.data.split(":", 1)[1]
    await callback.answer("🌟 Ищу хайлайты...")
    hls = await get_highlights(ig_username)
    back = [InlineKeyboardButton(text="◀️ Профиль", callback_data=f"ig_profile:{ig_username}")]

    if not hls:
        await callback.message.answer(
            f"😶 У @{ig_username} нет хайлайтов (или профиль приватный).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[back]),
        )
        return

    buttons, row = [], []
    for h in hls[:30]:
        label = h["title"][:16] + (f" ({h['count']})" if h["count"] else "")
        row.append(InlineKeyboardButton(
            text=label, callback_data=f"ig_hlv:{h['pk']}:{ig_username}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(back)

    await callback.message.answer(
        f"🌟 <b>Хайлайты @{_html.escape(ig_username)}</b> — {len(hls)} шт.\nВыбери, что открыть:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def callback_ig_highlight_view(callback: types.CallbackQuery):
    """Открыть/скачать один хайлайт альбомами."""
    import html as _html
    _, pk, ig_username = callback.data.split(":", 2)
    await callback.answer("⏳ Загружаю хайлайт...")
    data = await get_highlight_items(pk)
    if not data:
        await callback.message.answer("❌ Не удалось получить этот хайлайт.")
        return

    user = data.get("username") or ig_username
    title = data.get("title") or "хайлайт"
    stories = data["stories"]
    sent = await _send_story_albums(
        callback.message, stories, f"🌟 {_html.escape(title)} · @{user}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К хайлайтам", callback_data=f"ig_hl:{user}")],
        [InlineKeyboardButton(text="◀️ Профиль", callback_data=f"ig_profile:{user}")],
    ])
    await callback.message.answer(
        (f"✅ «{_html.escape(title)}»: {sent} из {len(stories)}" if sent
         else "❌ Не удалось загрузить файлы хайлайта"),
        parse_mode="HTML", reply_markup=kb,
    )


# ── КОЛБЭК: ПОСТЫ ─────────────────────────────────────────────────────────────

async def callback_ig_posts(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    ig_username = parts[1]
    cursor_key = parts[2] if len(parts) > 2 else ""
    cursor = _get_cursor(cursor_key) if cursor_key else ""

    await callback.answer("⏳ Загружаю посты...")
    status = await callback.message.answer(f"🖼 Получаю посты @{ig_username}...")

    result = await get_posts(ig_username, cursor)

    try:
        await status.delete()
    except Exception:
        pass

    if result.get("private"):
        await callback.message.answer(
            f"🔒 Профиль *@{ig_username}* приватный — посты недоступны.",
            parse_mode="Markdown",
            reply_markup=_profile_keyboard(ig_username, callback.from_user.id)
        )
        return

    posts = result.get("posts", [])
    if not posts:
        await callback.message.answer(
            f"😶 У @{ig_username} больше нет загружаемых постов.",
            reply_markup=_profile_keyboard(ig_username, callback.from_user.id)
        )
        return

    album_items = []
    to_cache: list = []          # параллельно album_items: (shortcode, total) или None
    for i, post in enumerate(posts[:10]):
        media_list = post.get("media", [])
        if not media_list:
            continue

        caption  = post.get("caption", "").strip()
        likes    = post.get("like_count", 0)
        comments = post.get("comment_count", 0)
        sc   = post.get("shortcode", "")
        link = f"https://www.instagram.com/p/{sc}/" if sc else ""
        n_media = len(media_list)

        cap = f"❤️ {_fmt_count(likes)}  💬 {_fmt_count(comments)}"
        if n_media > 1:
            cap = f"🗂 {n_media}  " + cap
        if caption:
            cap += f"\n\n{caption[:200]}"
        if link:
            cap += f"\n🔗 {link}"
        cap = cap[:1020]

        # Уже отправляли этот пост — берём file_id, ничего не качаем
        row = ig_cache_get(sc) if sc else None
        if row and row["items"]:
            album_items.append(_mk_cached_media(row["items"][0], cap))
            to_cache.append(None)
            continue

        files, _ = await _fetch_media_files(media_list[:1], limit=1)
        if not files:
            continue
        f = files[0]
        album_items.append(_mk_input_media(f, cap, None, f"post_{i}"))
        # в кэш кладём только одиночные посты: набор должен быть полным
        to_cache.append((sc, 1) if (sc and n_media == 1 and not f["degraded"]) else None)
        await asyncio.sleep(0.3)

    if not album_items:
        await callback.message.answer(
            "❌ Не удалось скачать файлы для этих постов.",
            reply_markup=_profile_keyboard(ig_username, callback.from_user.id)
        )
        return

    try:
        if len(album_items) == 1:
            # альбом из одного элемента Telegram не принимает
            it = album_items[0]
            if isinstance(it, InputMediaVideo):
                msgs = [await callback.message.answer_video(
                    it.media, caption=it.caption, supports_streaming=True,
                    width=it.width, height=it.height, duration=it.duration)]
            else:
                msgs = [await callback.message.answer_photo(it.media, caption=it.caption)]
        else:
            msgs = await callback.message.answer_media_group(album_items)
        for m, tc in zip(msgs, to_cache):
            if tc:
                fid = _fid_of(m)
                if fid:
                    ig_cache_put(tc[0], ig_username, "", [fid], tc[1])
    except Exception as e:
        logger.error(f"IG send album error: {e}")
        await callback.message.answer("❌ Ошибка отправки альбома.")

    next_cursor = result.get("next_cursor", "")
    has_more    = result.get("has_more", False)
    total       = result.get("total_cached", 0)

    cursor_key = _store_cursor(f"{ig_username}_{next_cursor[:40]}", next_cursor) if next_cursor else ""
    shown_up   = int(next_cursor) if next_cursor.isdigit() else 0
    shown_from = max(0, shown_up - 10)
    total_str  = f" из ~{total}" if total else ""
    footer = f"✅ Посты {shown_from+1}–{shown_from+len(album_items)}{total_str} @{ig_username}"
    await callback.message.answer(
        footer,
        reply_markup=_posts_keyboard(ig_username, callback.from_user.id, cursor_key, has_more)
    )


# ── КОЛБЭК: ПОДПИСКИ ──────────────────────────────────────────────────────────

async def callback_ig_sub(callback: types.CallbackQuery):
    _, sub_type, ig_username = callback.data.split(":", 2)
    uid = str(callback.from_user.id)

    if ig_is_subscribed(uid, ig_username, sub_type):
        ig_unsubscribe(uid, ig_username, sub_type)
        labels = {"stories": "историй", "posts": "постов", "avatar": "аватарки"}
        await callback.answer(f"🔕 Отписался от {labels.get(sub_type, sub_type)} @{ig_username}")
    else:
        ig_subscribe(uid, ig_username, sub_type)
        labels = {"stories": "истории", "posts": "посты", "avatar": "аватарку"}
        await callback.answer(f"🔔 Подписался на {labels.get(sub_type, sub_type)} @{ig_username}!")
        # Запоминаем текущее состояние (и pk аккаунта — чтобы пережить смену ника),
        # чтобы не завалить уведомлениями о старом
        _spawn(_seed_subscription(uid, ig_username, sub_type))

    try:
        await callback.message.edit_reply_markup(
            reply_markup=_profile_keyboard(ig_username, callback.from_user.id)
        )
    except Exception:
        pass


# ── МОИ ПОДПИСКИ ──────────────────────────────────────────────────────────────

async def callback_ig_my_subs(callback: types.CallbackQuery):
    uid = str(callback.from_user.id)
    subs = ig_get_subscriptions(uid)

    if not subs:
        await callback.answer("📋 У тебя нет подписок на Instagram-аккаунты.", show_alert=True)
        return

    grouped: dict[str, list[str]] = {}
    for s in subs:
        grouped.setdefault(s["ig_username"], []).append(s["sub_type"])

    lines = ["📋 *Мои Instagram подписки:*\n"]
    type_labels = {"stories": "📸 истории", "posts": "🖼 посты", "avatar": "👤 аватарка"}
    for uname, types_list in grouped.items():
        t_str = " · ".join(type_labels.get(t, t) for t in types_list)
        lines.append(f"@{uname} → {t_str}")

    buttons = [
        [InlineKeyboardButton(text=f"🔍 @{uname}", callback_data=f"ig_profile:{uname}")]
        for uname in grouped
    ]

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    )
    await callback.answer()


async def cmd_ig_subs(message: types.Message):
    """/ig_subs — список активных подписок."""
    uid = str(message.from_user.id)
    subs = ig_get_subscriptions(uid)
    if not subs:
        await message.answer("📋 У тебя нет Instagram-подписок. Найди профиль через /ig и подпишись.")
        return

    grouped: dict[str, list[str]] = {}
    for s in subs:
        grouped.setdefault(s["ig_username"], []).append(s["sub_type"])

    type_labels = {"stories": "📸", "posts": "🖼", "avatar": "👤"}
    lines = ["📋 *Мои Instagram подписки:*\n"]
    for uname, types_list in grouped.items():
        icons = " ".join(type_labels.get(t, t) for t in types_list)
        lines.append(f"{icons} @{uname}")

    buttons = [
        [InlineKeyboardButton(text=f"🔍 @{uname}", callback_data=f"ig_profile:{uname}")]
        for uname in grouped
    ]
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="ig_my_subs")])

    await message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ── ФОНОВЫЙ ОПРОС ПОДПИСОК ────────────────────────────────────────────────────

async def _seed_subscription(user_id: str, ig_username: str, sub_type: str) -> None:
    """
    Пункты 1/3: сразу после подписки запоминаем текущее состояние, чтобы
    не слать «новыми» посты, которые уже были, и не ловить ложную смену аватарки.
    Заодно запоминаем pk аккаунта — по нему бот переживёт смену ника.
    """
    try:
        info = await get_user_info(ig_username)
        if info and info.get("id"):
            st = ig_get_user_state(ig_username) or {}
            upd = {}
            if not st.get("pk"):
                upd["pk"] = info["id"]
            if st.get("is_private") is None:
                upd["is_private"] = info.get("is_private", False)
            if upd:
                ig_set_user_state(ig_username, **upd)

        if sub_type == "posts":
            data = await get_posts(ig_username)
            ids = [f"post_{p['id']}" for p in data.get("posts", []) if p.get("id")]
            ig_mark_sent_many(user_id, ids)
            ig_update_last_seen(user_id, ig_username, "posts", "seeded")
            if info and info.get("posts_count") is not None:
                ig_set_user_state(ig_username, media_count=info["posts_count"],
                                  last_posts_fetch=time.time())
        elif sub_type == "avatar":
            url = (info or {}).get("avatar_url", "")
            if url:
                img = await _download(url)
                ig_update_last_seen(
                    user_id, ig_username, "avatar",
                    _avatar_state_fmt(_avatar_path(url), _sha(img)),
                )
    except Exception as e:
        logger.warning(f"seed {sub_type} @{ig_username}: {e}")


def _ig_check_interval(user_subs: list[dict], last_activity: float, now: float) -> float:
    """
    Пункт 8: адаптивный интервал проверки ника.
    Активные — каждые IG_CHECK_BASE, тихие реже; ночью вдвое реже.
    Если есть подписка на истории — не реже раза в час (истории живут сутки).
    """
    base = float(IG_CHECK_BASE)
    idle_days = (now - last_activity) / 86400 if last_activity else 0.0
    if idle_days >= 14:
        iv = base * 6
    elif idle_days >= 3:
        iv = base * 2
    else:
        iv = base
    if any(s["sub_type"] == "stories" for s in user_subs):
        iv = min(iv, base * 2)
    if IG_NIGHT_START <= _ig_local_hour() < IG_NIGHT_END:
        iv *= 2
    return iv


_ALERT_TASK_STARTED = False


async def ig_checker_task(bot: Bot):
    """
    Фоновая задача проверки подписок.
    Раз в IG_CHECK_TICK смотрит, каких ников пора проверить (у каждого свой
    адаптивный интервал), и проверяет не больше IG_MAX_PER_TICK за раз.
    Заодно запускает алерты о здоровье пула и ежедневную чистку БД.
    """
    global _ALERT_TASK_STARTED
    set_background_priority()      # все запросы этой задачи уступают пользовательским
    logger.info("🟡 Instagram checker запущен")
    if not _ALERT_TASK_STARTED:
        _ALERT_TASK_STARTED = True
        _spawn(ig_pool_alert_task(bot))

    last_cleanup = 0.0
    await asyncio.sleep(90)   # состояние проверок хранится в БД — долго ждать не нужно
    while True:
        try:
            if time.time() - last_cleanup > 24 * 3600:
                last_cleanup = time.time()
                ig_cleanup_sent()
            await _run_ig_check(bot)
        except Exception as e:
            logger.error(f"ig_checker_task error: {e}")
        await asyncio.sleep(IG_CHECK_TICK)


async def _run_ig_check(bot: Bot):
    subs = ig_all_subscriptions()
    if not subs:
        return

    by_username: dict[str, list[dict]] = {}
    for s in subs:
        by_username.setdefault(s["ig_username"], []).append(s)

    now = time.time()
    states = ig_get_check_states()
    ustates = ig_get_all_user_states()
    due = []
    for uname, user_subs in by_username.items():
        last_checked, last_activity = states.get(uname, (0.0, 0.0))
        interval = _ig_check_interval(user_subs, last_activity or now, now)
        if ustates.get(uname, {}).get("is_gone"):
            interval = max(interval, IG_GONE_INTERVAL)      # удалённых проверяем раз в сутки
        if now - last_checked >= interval:
            due.append((last_checked, uname, user_subs))

    if not due:
        return
    due.sort(key=lambda t: t[0])          # самые давно не проверявшиеся — первыми
    due = due[:IG_MAX_PER_TICK]
    logger.info(f"[ig-check] к проверке: {len(due)} из {len(by_username)} ников")

    for _, uname, user_subs in due:
        found = False
        try:
            found = await _check_user(bot, uname, user_subs)
        except Exception as e:
            logger.error(f"ig check {uname}: {e}")
        t = time.time()
        ig_set_check_state(uname, t, t if found else None)
        await asyncio.sleep(random.uniform(2, 5))


def _rename_filters(old: str, new: str) -> None:
    """Перенести фильтры уведомлений на новый ник."""
    for k in [k for k in _SUB_FILTERS if k.split(":")[1:2] == [old]]:
        uid, _, sub_type = k.split(":", 2)
        _SUB_FILTERS[f"{uid}:{new}:{sub_type}"] = _SUB_FILTERS.pop(k)


async def _notify_users(bot: Bot, user_ids, text: str, kb=None) -> None:
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.warning(f"ig notify {uid}: {e}")
        await asyncio.sleep(0.2)


def _sub_users(subs: list[dict]) -> list[str]:
    return sorted({s["user_id"] for s in subs})


async def _handle_missing_or_renamed(bot: Bot, old: str, subs: list[dict], st: dict,
                                     taken_by_other: bool) -> bool:
    """
    Ник «пропал» или теперь принадлежит другому человеку (pk не совпал).
    Ищем исходный аккаунт по pk:
      • нашли под другим ником  → переносим подписки, пишем «@old теперь @new»;
      • pk тоже не найден       → после IG_GONE_CONFIRM подряд сообщаем, что аккаунт недоступен.
    Пока ник занят чужим человеком, его контент НЕ рассылаем.
    """
    pk = st.get("pk", "")
    status, new_info = ("error", None)
    if pk:
        status, new_info = await get_user_status_by_pk(pk)

    if status == "ok" and new_info:
        new = (new_info.get("username") or "").lower()
        if new and new != old:
            users = ig_rename_username(old, new)
            _rename_filters(old, new)
            ig_set_user_state(new, pk=pk, is_private=new_info.get("is_private", False),
                              notfound_count=0, is_gone=False)
            logger.info(f"[ig-check] @{old} → @{new} (pk {pk}), подписчиков: {len(users)}")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=f"👤 @{new}", callback_data=f"ig_profile:{new}")]])
            await _notify_users(
                bot, users,
                f"🔄 <b>@{old}</b> теперь <b>@{new}</b>\n"
                f"<i>Подписки перенёс на новый ник — ничего делать не нужно.</i>",
                kb,
            )
            return True
        return False       # pk указывает на тот же ник — «не найден» был сбоем

    if status == "error" and pk:
        return False       # не смогли проверить — не делаем выводов

    # pk неизвестен (старая подписка) или аккаунт по pk не найден
    n = st.get("notfound_count", 0) + 1
    ig_set_user_state(old, notfound_count=n)
    if n >= IG_GONE_CONFIRM and not st.get("is_gone"):
        ig_set_user_state(old, is_gone=True)
        await _notify_users(
            bot, _sub_users(subs),
            f"👻 <b>@{old}</b> больше недоступен — аккаунт удалён, заблокирован или скрыт.\n"
            f"<i>Подписки оставил: если аккаунт вернётся, продолжу следить.</i>",
        )
    return False


async def _check_user(bot: Bot, ig_username: str, subs: list[dict]) -> bool:
    """Проверка аватарки / историй / постов для одного ника. True — было что-то новое."""
    status, info = await get_user_status(ig_username, force=True)
    if status == "error":
        return False                       # сбой IG/сети — не путаем с «аккаунта нет»

    st = ig_get_user_state(ig_username) or {}
    stored_pk = st.get("pk", "")
    cur_pk = (info or {}).get("id", "")

    # ник исчез, либо его занял другой человек (pk не совпал)
    if status == "notfound" or (stored_pk and cur_pk and stored_pk != cur_pk):
        return await _handle_missing_or_renamed(
            bot, ig_username, subs, st, taken_by_other=(status == "ok"))

    # ── аккаунт на месте ──
    if st.get("is_gone"):
        await _notify_users(bot, _sub_users(subs), f"✅ <b>@{ig_username}</b> снова доступен!")
    upd: dict = {}
    if st.get("is_gone") or st.get("notfound_count"):
        upd.update(is_gone=False, notfound_count=0)
    if cur_pk and cur_pk != stored_pk:
        upd["pk"] = cur_pk
    priv = bool(info.get("is_private"))
    if cur_pk:                                              # без pk (приватный fallback) состояние не трогаем
        if st.get("is_private") is not None and st["is_private"] != priv:
            await _notify_users(
                bot, _sub_users(subs),
                (f"🔒 <b>@{ig_username}</b> закрыл(а) аккаунт — посты и истории недоступны, "
                 f"пока он приватный.") if priv else
                (f"🔓 <b>@{ig_username}</b> снова открытый аккаунт — слежу дальше."),
            )
        upd["is_private"] = priv
    if upd:
        ig_set_user_state(ig_username, **upd)

    # ── лёгкая проверка постов ──
    # Тянем несколько последних постов, и только если media_count изменился
    # (или давно не проверяли — защита от «удалил один, выложил другой»).
    latest = None
    post_subs = [x for x in subs if x["sub_type"] == "posts"]
    if post_subs and not priv:
        count = info.get("posts_count")
        unchanged = (
            st.get("media_count") is not None and count is not None and st["media_count"] == count
            and time.time() - (st.get("last_posts_fetch") or 0) < IG_POSTS_FORCE_EVERY
            and all(x.get("last_seen") == "seeded" for x in post_subs)
        )
        if unchanged:
            logger.info(f"[ig-check] @{ig_username}: media_count={count} не менялся — посты пропускаю")
        else:
            latest = await get_latest_posts(ig_username)

    found = False
    posts_ok = latest is not None
    for sub in subs:
        user_id   = sub["user_id"]
        sub_type  = sub["sub_type"]
        last_seen = sub.get("last_seen", "")
        try:
            if sub_type == "avatar":
                found |= await _check_avatar(bot, user_id, ig_username, info, last_seen)
            elif sub_type == "stories":
                found |= await _check_stories(bot, user_id, ig_username)
            elif sub_type == "posts" and latest is not None:
                found |= await _check_posts(bot, user_id, ig_username, last_seen, latest)
        except Exception as e:
            if sub_type == "posts":
                posts_ok = False
            logger.error(f"ig check {ig_username}/{sub_type} → {user_id}: {e}")

    if latest is not None and posts_ok:
        ig_set_user_state(ig_username, media_count=info.get("posts_count"),
                          last_posts_fetch=time.time())
    return found


async def _check_avatar(bot: Bot, user_id: str, ig_username: str, info: dict, last_seen: str) -> bool:
    cur_url = info.get("avatar_url", "")
    if not cur_url:
        return False
    cur_path = _avatar_path(cur_url)
    old_path, old_hash = _avatar_state_parse(last_seen)

    if old_path == cur_path:
        # та же картинка; старый формат last_seen просто мигрируем
        if not last_seen.startswith("av2|"):
            ig_update_last_seen(user_id, ig_username, "avatar", _avatar_state_fmt(cur_path, old_hash))
        return False

    # путь поменялся — сверяем содержимое, а не ссылку
    data = await _download(cur_url) or await get_avatar_bytes(ig_username)
    new_hash = _sha(data)

    if not old_path:
        # состояния не было (старая подписка) — молча запоминаем
        ig_update_last_seen(user_id, ig_username, "avatar", _avatar_state_fmt(cur_path, new_hash))
        return False

    if data and old_hash and new_hash == old_hash:
        # CDN перетасовал ссылку, картинка та же — не уведомляем
        ig_update_last_seen(user_id, ig_username, "avatar", _avatar_state_fmt(cur_path, new_hash))
        return False

    ig_update_last_seen(user_id, ig_username, "avatar", _avatar_state_fmt(cur_path, new_hash))
    try:
        if data:
            await bot.send_photo(
                user_id,
                BufferedInputFile(data, filename="avatar.jpg"),
                caption=f"👤 *@{ig_username}* сменил(а) аватарку!",
                parse_mode="Markdown",
            )
        else:
            await bot.send_message(
                user_id, f"👤 *@{ig_username}* сменил(а) аватарку!", parse_mode="Markdown"
            )
    except Exception as e:
        logger.warning(f"IG avatar notify {user_id}: {e}")
    return True


# Пункт 7: счётчик неудач по историям — чтобы не крутить одну и ту же вечно
_STORY_FAILS: dict[tuple, int] = {}


def _story_fail(user_id: str, story_id: str) -> bool:
    """Записать неудачу. True — лимит исчерпан, историю помечаем отправленной (бросаем)."""
    key = (user_id, story_id)
    n = _STORY_FAILS.get(key, 0) + 1
    if n >= IG_STORY_MAX_FAILS:
        _STORY_FAILS.pop(key, None)
        ig_mark_sent(user_id, f"story_{story_id}")
        logger.warning(f"story {story_id} → {user_id}: {n} неудач подряд, пропускаю")
        return True
    _STORY_FAILS[key] = n
    return False


async def _check_stories(bot: Bot, user_id: str, ig_username: str) -> bool:
    stories = await get_stories(ig_username)
    if not stories:
        return False
    new_stories = [
        s for s in stories
        if s.get("id") and not ig_already_sent(user_id, f"story_{s['id']}")
    ]
    if not new_stories:
        return False

    # Сначала качаем — и только потом пишем «добавил(а) N историй»,
    # иначе при недоступном CDN заголовок прилетал бы каждый цикл без самих историй.
    ready = []
    for story in new_stories[:10]:
        data = await _download(story.get("url", ""), story.get("type", "photo"))
        if data and len(data) <= _TG_FILE_LIMIT:
            ready.append((story, data))
        else:
            _story_fail(user_id, story["id"])
        await asyncio.sleep(0.3)
    if not ready:
        return False

    await bot.send_message(
        user_id,
        f"📸 *@{ig_username}* добавил(а) {len(ready)} новых историй!",
        parse_mode="Markdown",
    )
    for story, data in ready:
        try:
            kind = _sniff_kind(data, story.get("type", "photo"))
            if kind == "video":
                await bot.send_video(
                    user_id, BufferedInputFile(data, filename="story.mp4"),
                    caption=f"📸 История @{ig_username}",
                    supports_streaming=True, **(await _video_meta(data)),
                )
            else:
                await bot.send_photo(
                    user_id, BufferedInputFile(data, filename="story.jpg"),
                    caption=f"📸 История @{ig_username}",
                )
            ig_mark_sent(user_id, f"story_{story['id']}")
            _STORY_FAILS.pop((user_id, story["id"]), None)
        except Exception as e:
            logger.warning(f"IG story notify {user_id}: {e}")
            _story_fail(user_id, story["id"])
        await asyncio.sleep(0.5)
    return True


async def _check_posts(bot: Bot, user_id: str, ig_username: str, last_seen: str,
                       posts: list[dict]) -> bool:
    """`posts` — свежие посты, уже полученные в _check_user (общие для всех подписчиков ника)."""
    if not posts:
        return False

    if last_seen != "seeded":
        # старая подписка / сидинг не успел — молча запоминаем текущие посты
        ig_mark_sent_many(user_id, [f"post_{p['id']}" for p in posts if p.get("id")])
        ig_update_last_seen(user_id, ig_username, "posts", "seeded")
        return False

    active_filter = _get_filter(user_id, ig_username, "posts")
    cutoff = time.time() - IG_POST_MAX_AGE_DAYS * 86400
    new_posts = [
        p for p in posts
        if p.get("id")
        and not ig_already_sent(user_id, f"post_{p['id']}")
        and (not p.get("timestamp") or p["timestamp"] >= cutoff)   # закреп/старьё не считаем новым
        and _post_passes_filter(p, active_filter)
    ]
    if not new_posts:
        return False

    # берём 5 самых свежих, но шлём от старых к новым
    batch = sorted(new_posts, key=lambda p: p.get("timestamp", 0), reverse=True)[:5]
    batch.reverse()
    for post in batch:
        await _notify_new_post_styled(bot, user_id, ig_username, post)
        ig_mark_sent(user_id, f"post_{post['id']}")
        await asyncio.sleep(0.8)
    return True


# ── АЛЕРТЫ О ЗДОРОВЬЕ ПУЛА (пункт 10) ─────────────────────────────────────────

async def _alert_admins(bot: Bot, text: str) -> None:
    for uid in _get_admin_ids():
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"pool alert {uid}: {e}")


async def ig_pool_alert_task(bot: Bot):
    """
    Раз в 10 минут смотрит на здоровье пула. Шлёт админам алерт, если:
      • все аккаунты мертвы;
      • много «неожиданных»/сетевых ошибок и давно нет ни одного успешного запроса;
      • 429 на половине аккаунтов и больше.
    Повторы одного и того же алерта — не чаще раза в 3 часа; когда всё починилось — сообщение «снова работает».
    """
    import html as _html
    COOLDOWN = 3 * 3600
    last_alert: dict[str, float] = {}
    active: set[str] = set()
    await asyncio.sleep(5 * 60)

    while True:
        try:
            h = get_pool_health(3600)
            now = time.time()
            problems: dict[str, str] = {}

            if h["initialized"] and h["total"] and h["live"] == 0:
                problems["dead_all"] = "все аккаунты пула мертвы — обнови куки через /ig_refresh"

            bad = h["errors"].get("unexpected", 0) + h["errors"].get("net", 0)
            ok_ago = h["last_ok_ago"]
            if bad >= IG_ALERT_ERRORS and (ok_ago is None or ok_ago > 900):
                le = h["last_error"] or {}
                since = "ни разу" if ok_ago is None else f"{int(ok_ago // 60)} мин назад"
                problems["errors"] = (
                    f"{bad} ошибок за час, последний успешный запрос: {since}\n"
                    f"последняя ошибка: <code>{_html.escape(str(le.get('kind', '?')))}</code> — "
                    f"<code>{_html.escape(str(le.get('msg', ''))[:250])}</code>"
                )

            if h["total"] and h["rate_limited"] and h["rate_limited"] * 2 >= h["total"]:
                problems["rate"] = f"429 на {h['rate_limited']} из {h['total']} аккаунтов — IG душит, подожди или добавь аккаунты"

            for key, msg in problems.items():
                if now - last_alert.get(key, 0) >= COOLDOWN:
                    last_alert[key] = now
                    active.add(key)
                    await _alert_admins(bot, f"🚨 <b>instagram: проблема</b>\n\n{msg}")

            if active and not problems:
                active.clear()
                last_alert.clear()
                await _alert_admins(bot, "✅ <b>instagram</b> снова работает")
        except Exception as e:
            logger.error(f"ig_pool_alert_task error: {e}")
        await asyncio.sleep(10 * 60)


# ── ССЫЛКИ: ПОСТ / РИЛС / ИСТОРИЯ / ХАЙЛАЙТ ───────────────────────────────────

_IG_MEDIA_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/[A-Za-z0-9_-]+[^\s]*",
    re.I,
)
_IG_SC_RE = re.compile(
    r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.I)
_IG_HL_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/stories/highlights/(\d+)", re.I)
_IG_STORY_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/stories/([A-Za-z0-9_.]+)/(\d+)", re.I)
_IG_USTORIES_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/stories/([A-Za-z0-9_.]+)/?(?![A-Za-z0-9_./])", re.I)


def _extract_media_link(text: str) -> str | None:
    m = _IG_MEDIA_RE.search(text or "")
    return m.group(0) if m else None


def _shortcode_from_link(link: str) -> str:
    m = _IG_SC_RE.search(link or "")
    return m.group(1) if m else ""


def _parse_ig_link(text: str):
    """
    Что за ссылка на Instagram:
      ("highlight", (pk,))            — instagram.com/stories/highlights/<pk>
      ("story", (user, story_id, url))— instagram.com/stories/<user>/<id>
      ("userstories", (user,))        — instagram.com/stories/<user>
      ("media", (url,))               — пост / рилс / IGTV
    """
    text = text or ""
    m = _IG_HL_RE.search(text)
    if m:
        return "highlight", (m.group(1),)
    m = _IG_STORY_RE.search(text)
    if m and m.group(1).lower() != "highlights":
        url = m.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        return "story", (m.group(1).lower(), m.group(2), url)
    m = _IG_USTORIES_RE.search(text)
    if m and m.group(1).lower() != "highlights":
        return "userstories", (m.group(1).lower(),)
    link = _extract_media_link(text)
    if link:
        return "media", (link,)
    return None


async def _is_ig_media_link(message: types.Message) -> bool:
    t = message.text or ""
    return bool(t) and not t.startswith("/") and _parse_ig_link(t) is not None


async def _send_ig_media(message: types.Message, link: str) -> bool:
    """
    Достать пост/рилс по ссылке и отправить в чат (карусель — альбомом).
    Повторная ссылка (в этот или другой чат) отдаётся мгновенно по file_id.
    Возвращает False, если ничего не отправили (тогда вызывающий может уступить дорогу другим хендлерам).
    """
    out = _out_message(message, reply=True)
    url_sc = _shortcode_from_link(link)

    # 1. Кэш file_id: ни скачивания, ни запросов к Instagram
    if url_sc:
        row = ig_cache_get(url_sc)
        if row and len(row["items"]) == row["total"]:
            try:
                await _send_cached(out, row, kb=_profile_btn(row["username"]) if row["total"] == 1 else None)
                return True
            except Exception as e:
                logger.warning(f"cached send {url_sc}: {e} — сбрасываю кэш")
                ig_cache_drop(url_sc)

    try:
        await message.bot.send_chat_action(message.chat.id, "upload_video")
    except Exception:
        pass

    post = await get_media_by_url(link)
    if not post or not post.get("media"):
        return False

    media = post["media"][:10]
    files, too_big = await _fetch_media_files(media, limit=10)
    if not files:
        return False

    user = post.get("username", "")
    base_caption = _post_caption_html(
        user, post.get("caption") or "", post.get("like_count", 0), post.get("comment_count", 0))
    caption = base_caption
    if too_big:
        caption = (base_caption + f"\n\n⚠️ {too_big} файл(ов) больше 50 МБ — не влезли")[:1020]

    try:
        msgs, complete = await _send_files_safe(
            out, files, caption, kb=_profile_btn(user) if len(files) == 1 else None)
    except Exception as e:
        logger.warning(f"_send_ig_media {link[:60]}: {e}")
        return False

    # 2. Запоминаем file_id — только если пост ушёл целиком и без деградации (видео → превью)
    if complete and not too_big and len(files) == len(media) and not any(f["degraded"] for f in files):
        items = _cache_items(msgs)
        for key in {post.get("shortcode") or "", url_sc} - {""}:
            ig_cache_put(key, user, base_caption, items, len(media))
    return True


async def _send_story_link(message: types.Message, user: str, sid: str, url: str) -> bool:
    """instagram.com/stories/<user>/<id> → одна история."""
    stories = await get_stories(user)
    story = next((x for x in stories if str(x.get("id", "")).split("_")[0] == sid), None)
    if not story:                        # уже не в активных / запрос по прямой ссылке
        story = await get_story_by_url(url)
        if story and story.get("username"):
            user = story["username"].lower()
    if not story:
        return False
    files, _ = await _fetch_media_files([story], limit=1)
    if not files:
        return False
    await _send_files(
        _out_message(message, reply=True), files,
        f'📸 История <a href="https://instagram.com/{user}">@{user}</a>',
        kb=_profile_btn(user),
    )
    return True


async def _send_user_stories_link(message: types.Message, user: str) -> bool:
    """instagram.com/stories/<user> → все активные истории альбомом."""
    stories = await get_stories(user)
    if not stories:
        return False
    return (await _send_story_albums(message, stories, f"📸 Истории @{user}", reply=True)) > 0


async def _send_highlight_link(message: types.Message, pk: str) -> bool:
    """instagram.com/stories/highlights/<pk> → хайлайт альбомами."""
    import html as _html
    data = await get_highlight_items(pk)
    if not data:
        return False
    title = _html.escape(data.get("title") or "хайлайт")
    user = data.get("username") or ""
    head = f"🌟 {title}" + (f" · @{user}" if user else "")
    return (await _send_story_albums(message, data["stories"], head, reply=True)) > 0


async def _dispatch_ig_link(message: types.Message, parsed) -> bool:
    kind, args = parsed
    if kind == "media":
        return await _send_ig_media(message, args[0])
    try:
        await message.bot.send_chat_action(message.chat.id, "upload_video")
    except Exception:
        pass
    if kind == "story":
        return await _send_story_link(message, *args)
    if kind == "userstories":
        return await _send_user_stories_link(message, args[0])
    if kind == "highlight":
        return await _send_highlight_link(message, args[0])
    return False


async def handle_ig_media_link(message: types.Message):
    """Ссылка на пост/рилс/историю/хайлайт в чате → скачиваем. Не вышло — для постов отдаём сообщение дальше по цепочке."""
    parsed = _parse_ig_link(message.text or "")
    if parsed and await _dispatch_ig_link(message, parsed):
        return
    if parsed and parsed[0] != "media":
        await message.reply("не смог достать — аккаунт приватный, история уже исчезла или инста капризничает")
        return
    if SkipHandler is not None:
        raise SkipHandler()


async def cmd_igdl(message: types.Message):
    """/igdl <ссылка> — скачать пост, рилс, историю или хайлайт."""
    parts = (message.text or "").split(maxsplit=1)
    parsed = _parse_ig_link(parts[1]) if len(parts) > 1 else None
    if not parsed:
        await message.answer(
            "кинь ссылку на пост, рилс, историю или хайлайт:\n/igdl https://instagram.com/reel/...")
        return
    if not await _dispatch_ig_link(message, parsed):
        await message.answer("не смог достать — приватный, удалён или инста сейчас капризничает")


# ── HEALTH CHECK TASK ─────────────────────────────────────────────────────────

async def ig_session_health_task(bot: Bot):
    """
    Каждые 4 часа проверяет все сессии в пуле.
    Если сессия мертва — шлёт алерт админам.
    Запускать из main.py: asyncio.create_task(ig_session_health_task(bot))
    """
    set_background_priority()
    CHECK_INTERVAL = 4 * 60 * 60  # 4 часа
    STARTUP_DELAY  = 3 * 60       # первый чек через 3 минуты (пусть бот прогреется)
    logger.info("🟡 Instagram session health-check запущен")
    await asyncio.sleep(STARTUP_DELAY)

    while True:
        logger.info("🔍 IG session health check...")
        try:
            from instagram_viewer import _POOL as _ig_pool
            statuses = get_sessions_status()
            dead_logins = []

            for s in statuses:
                login = s["login"]
                if not s["alive"]:
                    dead_logins.append(login)
                    continue

                # Тест только для живых клиентов
                try:
                    ok = await validate_session(login)
                    status_icon = "✅" if ok else "💀"
                    logger.info(f"[health] {status_icon} {login} (запросов: {s['requests']})")
                    if not ok:
                        dead_logins.append(login)
                except Exception as e:
                    logger.warning(f"[health] ошибка проверки {login}: {e}")
                    dead_logins.append(login)

                await asyncio.sleep(random.uniform(_IG_HEALTH_DELAY_MIN, _IG_HEALTH_DELAY_MAX))

            # Алертим только новые смерти — о которых ещё не уведомляли
            new_dead = [l for l in dead_logins if l not in _ig_pool._known_dead]
            already_known = [l for l in dead_logins if l in _ig_pool._known_dead]

            if already_known:
                logger.info(f"[health] уже известны как мёртвые (алерт не повторяем): {already_known}")

            if new_dead:
                # Помечаем как известных мёртвых
                _ig_pool._known_dead.update(new_dead)
                logger.warning(f"[health] новые мёртвые сессии: {new_dead}")
                admin_ids = _get_admin_ids()
                dead_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"🔄 Обновить {l}", callback_data=f"ig_refresh_pick:{l}")]
                    for l in new_dead
                ])
                for uid in admin_ids:
                    try:
                        dead_list = "\n".join(f"• <code>{l}</code>" for l in new_dead)
                        await bot.send_message(
                            uid,
                            f"⚠️ <b>Health Check: мёртвые сессии</b>\n\n"
                            f"{dead_list}",
                            parse_mode="HTML",
                            reply_markup=dead_kb,
                        )
                    except Exception as e:
                        logger.warning(f"[health] алерт {uid}: {e}")
            elif not dead_logins:
                logger.info("[health] все сессии живы ✅")

        except Exception as e:
            logger.error(f"ig_session_health_task error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


# ════════════════════════════════════════════════════════════════════════════════
# ФИЧИ: Ф1 умные уведомления | Ф7 похожие аккаунты | Ф13 счётчик просмотров
#        В4 анимация загрузки  | В5 красивые уведомления
#        В13 анимация счётчиков | В15 тёмная карточка Yandex-стиль
# ════════════════════════════════════════════════════════════════════════════════

import sqlite3 as _sqlite3

# ── В15 + В13: ГЕНЕРАЦИЯ ТЁМНОЙ КАРТОЧКИ ПРОФИЛЯ ──────────────────────────────

def _dominant_color(img_bytes: bytes) -> tuple[int, int, int]:
    try:
        img = Image.open(BytesIO(img_bytes)).convert("RGB").resize((50, 50))
        pixels = list(img.getdata())
        r = sum(p[0] for p in pixels) // len(pixels)
        g = sum(p[1] for p in pixels) // len(pixels)
        b = sum(p[2] for p in pixels) // len(pixels)
        return (max(0, r - 80), max(0, g - 80), max(0, b - 80))
    except Exception:
        return (18, 18, 18)


def _make_profile_card(info: dict, avatar_bytes: bytes | None = None) -> bytes | None:
    """В15: Тёмная карточка профиля в стиле Yandex Music."""
    try:
        W, H = 800, 320
        BG     = (12, 12, 12)
        YELLOW = (255, 213, 0)
        WHITE  = (255, 255, 255)
        GREY   = (160, 160, 160)
        CARD_R = (28, 28, 28)

        accent = _dominant_color(avatar_bytes) if avatar_bytes else (40, 40, 40)

        img  = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)

        for y in range(6):
            alpha = int(255 * (1 - y / 6))
            r = int(accent[0] * alpha / 255 + YELLOW[0] * (255 - alpha) / 255)
            g = int(accent[1] * alpha / 255 + YELLOW[1] * (255 - alpha) / 255)
            b = int(accent[2] * alpha / 255 + YELLOW[2] * (255 - alpha) / 255)
            draw.line([(0, y), (W, y)], fill=(r, g, b))

        draw.rounded_rectangle([16, 16, W - 16, H - 16], radius=18, fill=CARD_R)
        draw.rounded_rectangle([16, 16, 22, H - 16], radius=4, fill=YELLOW)

        avatar_x, avatar_y, avatar_size = 40, 40, 200
        if avatar_bytes:
            try:
                av = Image.open(BytesIO(avatar_bytes)).convert("RGBA").resize((avatar_size, avatar_size))
                mask = Image.new("L", (avatar_size, avatar_size), 0)
                ImageDraw.Draw(mask).ellipse([0, 0, avatar_size, avatar_size], fill=255)
                av_out = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
                av_out.paste(av, mask=mask)
                ring = Image.new("RGBA", (avatar_size + 6, avatar_size + 6), (0, 0, 0, 0))
                ImageDraw.Draw(ring).ellipse([0, 0, avatar_size + 5, avatar_size + 5],
                                             outline=YELLOW, width=3)
                img.paste(ring, (avatar_x - 3, avatar_y - 3), ring)
                img.paste(av_out, (avatar_x, avatar_y), av_out)
            except Exception:
                draw.ellipse([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
                             fill=(40, 40, 40), outline=YELLOW, width=2)
        else:
            draw.ellipse([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
                         fill=(40, 40, 40), outline=YELLOW, width=2)

        tx, ty = avatar_x + avatar_size + 28, 45

        try:
            font_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
            font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
        except Exception:
            font_big = font_med = font_small = ImageFont.load_default()

        full_name = (info.get("full_name") or info.get("username", ""))[:28]
        verified  = " ✓" if info.get("is_verified") else ""
        draw.text((tx, ty), full_name + verified, font=font_big, fill=WHITE)
        draw.text((tx, ty + 34), f"@{info.get('username', '')}", font=font_med, fill=YELLOW)

        badge_text = "🔒 Приватный" if info.get("is_private") else "🌐 Открытый"
        draw.text((tx, ty + 60), badge_text, font=font_small, fill=GREY)
        draw.line([(tx, ty + 88), (W - 32, ty + 88)], fill=(50, 50, 50), width=1)

        counters = [
            (_fmt_count(info.get("followers", 0)),   "подписчики"),
            (_fmt_count(info.get("following", 0)),   "подписки"),
            (_fmt_count(info.get("posts_count", 0)), "посты"),
        ]
        block_w = (W - tx - 32) // 3
        for i, (val, label) in enumerate(counters):
            bx = tx + i * block_w
            draw.text((bx, ty + 100), val,   font=font_big,   fill=YELLOW)
            draw.text((bx, ty + 132), label, font=font_small, fill=GREY)

        bio = (info.get("biography") or "").strip()[:60]
        if bio:
            draw.text((tx, ty + 162), bio, font=font_small, fill=GREY)

        followers = info.get("followers", 0)
        badge_x, badge_y = 40, H - 52
        badges = []
        if info.get("is_verified"):   badges.append(("✅ Верифицирован", (30, 120, 60)))
        if followers >= 1_000_000:    badges.append(("🌟 1M+",           (120, 80, 0)))
        elif followers >= 100_000:    badges.append(("📈 100K+",         (0, 80, 120)))
        for badge_text, badge_color in badges[:3]:
            bw = len(badge_text) * 9 + 16
            draw.rounded_rectangle([badge_x, badge_y, badge_x + bw, badge_y + 26],
                                   radius=6, fill=badge_color)
            draw.text((badge_x + 8, badge_y + 5), badge_text, font=font_small, fill=WHITE)
            badge_x += bw + 8

        draw.text((W - 160, H - 30), "Instagram Viewer", font=font_small, fill=(50, 50, 50))

        out = BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()

    except Exception as e:
        logger.error(f"_make_profile_card error: {e}")
        return None


# ── В4: АНИМИРОВАННАЯ ЗАГРУЗКА ────────────────────────────────────────────────

class _LoadingAnimation:
    def __init__(self, message: types.Message):
        self._msg  = message
        self._sent = None

    async def start(self, text: str = "🔍 Ищу профиль..."):
        self._sent = await self._msg.answer(text)
        return self

    async def step(self, text: str):
        if self._sent:
            try:
                self._sent = await self._sent.edit_text(text)
            except Exception:
                pass

    async def done(self):
        if self._sent:
            try:
                await self._sent.delete()
            except Exception:
                pass
        self._sent = None


# ── В13: АНИМАЦИЯ СЧЁТЧИКОВ ───────────────────────────────────────────────────

async def _send_profile_with_counter_animation(
    source, info: dict, kb: InlineKeyboardMarkup, avatar_bytes: bytes | None
):
    import html
    safe_name = html.escape(info.get("full_name") or info.get("username", ""))
    safe_user = html.escape(info.get("username", ""))
    verified  = "✅" if info.get("is_verified") else ""
    private   = "🔒 приватный" if info.get("is_private") else "🌐 открытый"
    bio_line  = (f"\n📝 <i>{html.escape(info.get('biography','').strip())}</i>"
                 if info.get("biography","").strip() else "")

    text_loading = (
        f"📸 <b>{safe_name}</b> {verified}\n"
        f"👤 @{safe_user} · {private}\n\n"
        f"👥 <i>загружаю...</i> подписчиков · "
        f"👣 <i>загружаю...</i> подписок · "
        f"🖼 <i>загружаю...</i> постов"
        f"{bio_line}"
    )
    text_final = (
        f"📸 <b>{safe_name}</b> {verified}\n"
        f"👤 @{safe_user} · {private}\n\n"
        f"👥 <b>{_fmt_count(info.get('followers', 0))}</b> подписчиков · "
        f"👣 <b>{_fmt_count(info.get('following', 0))}</b> подписок · "
        f"🖼 <b>{_fmt_count(info.get('posts_count', 0))}</b> постов"
        f"{bio_line}"
    )

    card = _make_profile_card(info, avatar_bytes)
    if card:
        sent = await source.answer_photo(
            BufferedInputFile(card, filename="profile.png"),
            caption=text_loading,
            parse_mode="HTML",
            reply_markup=kb,
        )
        await asyncio.sleep(0.8)
        try:
            await sent.edit_caption(caption=text_final, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return

    if avatar_bytes:
        sent = await source.answer_photo(
            BufferedInputFile(avatar_bytes, filename="avatar.jpg"),
            caption=text_loading, parse_mode="HTML", reply_markup=kb,
        )
        await asyncio.sleep(0.8)
        try:
            await sent.edit_caption(caption=text_final, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    else:
        sent = await source.answer(text_loading, parse_mode="HTML", reply_markup=kb)
        await asyncio.sleep(0.8)
        try:
            await sent.edit_text(text_final, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


# ── Ф13: СЧЁТЧИК ПРОСМОТРОВ ───────────────────────────────────────────────────

def _ig_views_db() -> str:
    from instagram_viewer import _db_path as _ig_db_path
    return _ig_db_path().replace("instagram.db", "ig_views.db")


def _init_views_db():
    conn = _sqlite3.connect(_ig_views_db())
    conn.execute("""CREATE TABLE IF NOT EXISTS ig_profile_views (
        ig_username TEXT NOT NULL,
        user_id     TEXT NOT NULL,
        view_count  INTEGER DEFAULT 1,
        last_viewed DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (ig_username, user_id)
    )""")
    conn.commit()
    conn.close()


def _record_view(ig_username: str, user_id: str):
    try:
        _init_views_db()
        conn = _sqlite3.connect(_ig_views_db())
        conn.execute("""
            INSERT INTO ig_profile_views (ig_username, user_id, view_count, last_viewed)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(ig_username, user_id) DO UPDATE SET
                view_count = view_count + 1,
                last_viewed = CURRENT_TIMESTAMP
        """, (ig_username, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"_record_view: {e}")


def _get_popular_profiles(limit: int = 5) -> list[dict]:
    try:
        _init_views_db()
        conn = _sqlite3.connect(_ig_views_db())
        rows = conn.execute("""
            SELECT ig_username, SUM(view_count) as total, MAX(last_viewed) as last_v
            FROM ig_profile_views
            GROUP BY ig_username
            ORDER BY total DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [{"username": r[0], "views": r[1], "last": r[2]} for r in rows]
    except Exception:
        return []


def _get_my_viewed(user_id: str, limit: int = 5) -> list[dict]:
    try:
        _init_views_db()
        conn = _sqlite3.connect(_ig_views_db())
        rows = conn.execute("""
            SELECT ig_username, view_count, last_viewed
            FROM ig_profile_views WHERE user_id=?
            ORDER BY last_viewed DESC LIMIT ?
        """, (user_id, limit)).fetchall()
        conn.close()
        return [{"username": r[0], "views": r[1], "last": r[2]} for r in rows]
    except Exception:
        return []


# ── Ф7: ПОХОЖИЕ АККАУНТЫ ──────────────────────────────────────────────────────

async def callback_ig_related(callback: types.CallbackQuery):
    ig_username = callback.data.split(":", 1)[1]
    await callback.answer("🔍 Ищу похожие аккаунты...")

    try:
        from instagram_viewer import _POOL

        def _fetch(cl):
            user = cl.user_info_by_username(ig_username)
            related = cl.user_related_profiles(user.pk)
            return related[:6]

        profiles = await _POOL.run(_fetch)
    except Exception as e:
        logger.error(f"ig_related {ig_username}: {e}")
        profiles = []

    if not profiles:
        await callback.message.answer("😕 Не удалось найти похожие аккаунты.")
        return

    lines = [f"👥 <b>Похожие на @{ig_username}:</b>\n"]
    buttons = []
    for p in profiles:
        uname     = getattr(p, "username", "")
        fname     = getattr(p, "full_name", "") or uname
        followers = getattr(p, "follower_count", 0) or 0
        verified  = " ✅" if getattr(p, "is_verified", False) else ""
        lines.append(f"• <b>{fname}</b>{verified} @{uname} · {_fmt_count(followers)} подп.")
        if uname:
            buttons.append([InlineKeyboardButton(
                text=f"👤 @{uname}", callback_data=f"ig_profile:{uname}"
            )])

    buttons.append([InlineKeyboardButton(
        text="◀️ Назад", callback_data=f"ig_profile:{ig_username}"
    )])

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ── Ф1: УМНЫЕ УВЕДОМЛЕНИЯ ────────────────────────────────────────────────────

_SUB_FILTERS: dict[str, str] = {}

NOTIFY_FILTERS = {
    "all":    "🔔 Всё",
    "reels":  "🎬 Только Reels",
    "photos": "🖼 Только фото",
    "hot":    "🔥 Только популярные (>1K лайков)",
}


def _filter_key(user_id: str, ig_username: str, sub_type: str) -> str:
    return f"{user_id}:{ig_username}:{sub_type}"


def _get_filter(user_id: str, ig_username: str, sub_type: str) -> str:
    return _SUB_FILTERS.get(_filter_key(user_id, ig_username, sub_type), "all")


def _set_filter(user_id: str, ig_username: str, sub_type: str, f: str):
    _SUB_FILTERS[_filter_key(user_id, ig_username, sub_type)] = f


def _post_passes_filter(post: dict, filter_name: str) -> bool:
    if filter_name == "all":
        return True
    media = post.get("media", [{}])
    mtype = media[0].get("type", "photo") if media else "photo"
    if filter_name == "reels":
        return mtype == "video"
    if filter_name == "photos":
        return mtype == "photo"
    if filter_name == "hot":
        return post.get("like_count", 0) >= 1000
    return True


async def callback_ig_sub_filter(callback: types.CallbackQuery):
    _, sub_type, ig_username, filter_name = callback.data.split(":", 3)
    uid = str(callback.from_user.id)
    _set_filter(uid, ig_username, sub_type, filter_name)
    label = NOTIFY_FILTERS.get(filter_name, filter_name)
    await callback.answer(f"✅ Фильтр: {label}", show_alert=False)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_sub_filter_keyboard(ig_username, uid, sub_type)
        )
    except Exception:
        pass


def _sub_filter_keyboard(ig_username: str, user_id: str, sub_type: str) -> InlineKeyboardMarkup:
    current = _get_filter(user_id, ig_username, sub_type)
    rows = []
    for key, label in NOTIFY_FILTERS.items():
        mark = " ✅" if key == current else ""
        rows.append([InlineKeyboardButton(
            text=label + mark,
            callback_data=f"ig_sf:{sub_type}:{ig_username}:{key}"
        )])
    rows.append([InlineKeyboardButton(
        text="◀️ Профиль", callback_data=f"ig_profile:{ig_username}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def callback_ig_sub_filter_menu(callback: types.CallbackQuery):
    _, sub_type, ig_username = callback.data.split(":", 2)
    uid = str(callback.from_user.id)
    current = _get_filter(uid, ig_username, sub_type)
    label   = NOTIFY_FILTERS.get(current, current)
    await callback.answer()
    await callback.message.answer(
        f"🔔 <b>Фильтр уведомлений</b>\n"
        f"@{ig_username} · тип: {sub_type}\n"
        f"Сейчас: {label}",
        parse_mode="HTML",
        reply_markup=_sub_filter_keyboard(ig_username, uid, sub_type),
    )


# ── В5: КРАСИВЫЕ УВЕДОМЛЕНИЯ О НОВЫХ ПОСТАХ ──────────────────────────────────

async def _notify_new_post_styled(bot: Bot, user_id: str, ig_username: str, post: dict):
    """Новый пост подписки: фото/видео или вся карусель альбомом (до 10 элементов)."""
    import html as _html
    all_media = post.get("media") or []
    media_list = all_media[:10]
    if not media_list:
        return

    n = len(media_list)
    is_video = media_list[0].get("type") == "video"
    icon = "🗂" if n > 1 else ("🎬" if is_video else "🖼")
    cap_raw  = post.get("caption", "").strip()[:200]
    likes    = _fmt_count(post.get("like_count", 0))
    comments = _fmt_count(post.get("comment_count", 0))
    sc       = post.get("shortcode", "")
    more = f" · ещё {len(all_media) - 10} в Instagram" if len(all_media) > 10 else ""

    caption = (
        f"{'─' * 22}\n"
        f"{icon} <b>Новый пост</b>{f' ({n} шт.{more})' if n > 1 else ''} от "
        f"<a href=\"https://instagram.com/{ig_username}\">@{ig_username}</a>\n"
        f"{'─' * 22}\n"
        + (f"📝 <i>{_html.escape(cap_raw)}</i>\n" if cap_raw else "")
        + f"\n❤️ {likes}   💬 {comments}"
        + (f"\n\n🔗 <a href=\"https://www.instagram.com/p/{sc}/\">Открыть в Instagram</a>" if sc else "")
    )

    async def _text_only():
        try:
            await bot.send_message(user_id, caption, parse_mode="HTML",
                                   disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"_notify_new_post_styled text {user_id}: {e}")

    out = _out_chat(bot, user_id)

    # Пост уже уходил кому-то (или по ссылке) — шлём по file_id, без скачивания
    row = ig_cache_get(sc) if sc else None
    if row and len(row["items"]) == row["total"] == n:
        try:
            await _send_cached(out, row, caption=caption[:1020])
            return
        except Exception as e:
            logger.warning(f"cached notify {sc}: {e}")
            ig_cache_drop(sc)

    files, too_big = await _fetch_media_files(media_list, limit=10)
    if not files:
        await _text_only()
        return

    try:
        msgs, complete = await _send_files_safe(out, files, caption[:1020])
    except Exception as e:
        logger.warning(f"_notify_new_post_styled {user_id}: {e}")
        await _text_only()
        return

    if sc and complete and not too_big and len(files) == n and not any(f["degraded"] for f in files):
        link_cap = _post_caption_html(
            ig_username, post.get("caption") or "",
            post.get("like_count", 0), post.get("comment_count", 0))
        ig_cache_put(sc, ig_username, link_cap, _cache_items(msgs), n)


# ── /ig_popular ───────────────────────────────────────────────────────────────

async def cmd_ig_popular(message: types.Message):
    popular = _get_popular_profiles(5)
    my      = _get_my_viewed(str(message.from_user.id), 3)

    if not popular and not my:
        await message.answer("📊 Статистика просмотров пока пуста.")
        return

    lines = ["📊 <b>Статистика просмотров Instagram</b>\n"]
    if popular:
        lines.append("🏆 <b>Топ профилей (все пользователи):</b>")
        for i, p in enumerate(popular, 1):
            lines.append(f"{i}. @{p['username']} — {p['views']} просмотров")
    if my:
        lines.append("\n👤 <b>Ты смотрел недавно:</b>")
        for p in my:
            lines.append(f"• @{p['username']} — {p['views']} раз")

    buttons = [
        [InlineKeyboardButton(text=f"👤 @{p['username']}", callback_data=f"ig_profile:{p['username']}")]
        for p in popular[:3]
    ]

    await message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    )


# ── /ig_refresh — обновление кук ──────────────────────────────────────────────

def _get_admin_ids() -> set[int]:
    """Список admin user_id из env IG_ADMIN_IDS=123456,789012"""
    raw = os.getenv("IG_ADMIN_IDS", "")
    ids = set()
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            ids.add(int(x))
    return ids


def _parse_cookie_block(text: str) -> dict:
    """
    Универсальный парсер кук. Поддерживает:

    1. JSON массив от EditThisCookie / J2TEAM Cookies:
       [{"name": "sessionid", "value": "...", ...}, ...]

    2. JSON объект:
       {"sessionid": "...", "ds_user_id": "..."}

    3. Netscape формат (curl --cookie-jar / wget):
       .instagram.com  TRUE  /  FALSE  0  sessionid  <value>

    4. key:value / key=value / key  value (построчно):
       sessionid: abc123
       ds_user_id=456

    Возвращает dict с именами кук в нижнем регистре.
    """
    import json as _json

    text = text.strip()
    result: dict[str, str] = {}

    # ── Формат 1: JSON массив (EditThisCookie) ───────────────────────────────
    if text.startswith("["):
        try:
            items = _json.loads(text)
            for item in items:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    key = str(item["name"]).lower().strip()
                    val = str(item["value"]).strip()
                    if key and val:
                        result[key] = val
            if result:
                logger.info(f"[cookie] распарсен JSON-массив: {list(result.keys())}")
                return result
        except Exception:
            pass

    # ── Формат 2: JSON объект ────────────────────────────────────────────────
    if text.startswith("{"):
        try:
            obj = _json.loads(text)
            for k, v in obj.items():
                if isinstance(v, str):
                    result[k.lower().strip()] = v.strip()
            if result:
                logger.info(f"[cookie] распарсен JSON-объект: {list(result.keys())}")
                return result
        except Exception:
            pass

    # ── Формат 3: Netscape (7 колонок через таб) ─────────────────────────────
    # .instagram.com  TRUE  /  FALSE  0  sessionid  <value>
    netscape_found = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 7:
            key = parts[5].strip().lower()
            val = parts[6].strip()
            if key:
                result[key] = val
                netscape_found = True

    if netscape_found:
        logger.info(f"[cookie] распарсен Netscape-формат: {list(result.keys())}")
        return result

    # ── Формат 4: key:value / key=value / key  value ─────────────────────────
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in (":", "=", "\t"):
            if sep in line:
                key, _, val = line.partition(sep)
                key = key.strip().lower()
                val = val.strip().split()[0]  # отрезаем лишнее (домен, размер и т.д.)
                if key:
                    result[key] = val
                break

    if result:
        logger.info(f"[cookie] распарсен key-value формат: {list(result.keys())}")
    return result


# ── Дефолты устройства/гео (используются ТОЛЬКО при первой настройке аккаунта,
# когда старого session.json ещё нет — при обновлении кук всё ниже переиспользуется
# из старого файла, см. _build_session_json). Переопределяются через env, чтобы
# можно было подогнать под реальную гео аккаунтов без правки кода.
_IG_DEFAULT_DEVICE = {
    "app_version":     os.getenv("IG_APP_VERSION", "365.0.0.39.109"),
    "android_version": int(os.getenv("IG_ANDROID_VERSION", "34")),
    "android_release": os.getenv("IG_ANDROID_RELEASE", "14"),
    "dpi":             os.getenv("IG_DEVICE_DPI", "480dpi"),
    "resolution":      os.getenv("IG_DEVICE_RESOLUTION", "1080x2400"),
    "manufacturer":    os.getenv("IG_DEVICE_MANUFACTURER", "samsung"),
    "device":          os.getenv("IG_DEVICE_CODE", "dm3q"),
    "model":           os.getenv("IG_DEVICE_MODEL", "SM-S918B"),
    "cpu":             os.getenv("IG_DEVICE_CPU", "arm64-v8a"),
    "version_code":    os.getenv("IG_VERSION_CODE", "568858490"),
}
_IG_DEFAULT_COUNTRY        = os.getenv("IG_DEFAULT_COUNTRY", "US")
_IG_DEFAULT_COUNTRY_CODE   = int(os.getenv("IG_DEFAULT_COUNTRY_CODE", "1"))
_IG_DEFAULT_LOCALE         = os.getenv("IG_DEFAULT_LOCALE", "en_US")
_IG_DEFAULT_TZ_OFFSET      = int(os.getenv("IG_DEFAULT_TZ_OFFSET", "10800"))


def _build_session_json(username: str, cookies: dict, existing: dict | None = None) -> dict | None:
    """
    Собрать session.json из словаря кук.

    existing — содержимое ПРЕДЫДУЩЕГО session.json для этого аккаунта (если есть).
    Если передан, uuids/device_settings/user_agent/гео/данные переиспользуются
    из него, а не генерируются заново — иначе при каждом /ig_refresh куки
    "приезжают" с новым отпечатком устройства на старый аккаунт, что для IG
    выглядит как чужое устройство и провоцирует повторные вылеты/чекпоинты.
    Новые uuids/device_settings генерируются только при первой настройке
    аккаунта (existing отсутствует или в нём нет этих полей).
    """
    import uuid as _uuid, base64 as _b64, random as _rnd, time as _time

    sessionid  = cookies.get("sessionid", "")
    ds_user_id = cookies.get("ds_user_id", "")
    if not sessionid or not ds_user_id:
        return None

    existing = existing or {}
    old_uuids   = existing.get("uuids") or {}
    old_device  = existing.get("device_settings") or {}
    old_cookies = existing.get("cookies") or {}

    ig_did = cookies.get("ig_did") or old_cookies.get("ig_did") or old_uuids.get("phone_id") \
        or str(_uuid.uuid4()).upper()

    uuids = {
        "phone_id":          old_uuids.get("phone_id", ig_did),
        "uuid":              old_uuids.get("uuid") or str(_uuid.uuid4()),
        "client_session_id": old_uuids.get("client_session_id") or str(_uuid.uuid4()),
        "advertising_id":    old_uuids.get("advertising_id") or str(_uuid.uuid4()),
        "device_id":         old_uuids.get("device_id")
                              or ("android-" + _b64.b16encode(_rnd.randbytes(8)).decode().lower()),
    }

    device_settings = old_device if old_device else dict(_IG_DEFAULT_DEVICE)

    user_agent = existing.get("user_agent") or (
        "Instagram {app_version} Android "
        "({android_version}/{android_release}; {dpi}; {resolution}; "
        "{manufacturer}; {model}; {device}; {cpu}; {locale}; {version_code})"
    ).format(
        locale=existing.get("locale", _IG_DEFAULT_LOCALE),
        **device_settings,
    )

    return {
        "uuids": uuids,
        "cookies": {
            "sessionid":  sessionid,
            "ds_user_id": ds_user_id,
            "csrftoken":  cookies.get("csrftoken", "") or old_cookies.get("csrftoken", ""),
            "mid":        cookies.get("mid", "") or old_cookies.get("mid", ""),
            "ig_did":     ig_did,
            "datr":       cookies.get("datr", "") or old_cookies.get("datr", ""),
            "rur":        cookies.get("rur", "") or old_cookies.get("rur", ""),
        },
        "last_login": _time.time(),
        "device_settings": device_settings,
        "user_agent": user_agent,
        "country":         existing.get("country", _IG_DEFAULT_COUNTRY),
        "country_code":    existing.get("country_code", _IG_DEFAULT_COUNTRY_CODE),
        "locale":          existing.get("locale", _IG_DEFAULT_LOCALE),
        "timezone_offset": existing.get("timezone_offset", _IG_DEFAULT_TZ_OFFSET),
        "authorization_data": {
            "ds_user_id": ds_user_id,
            "sessionid":  sessionid,
        },
    }


async def cmd_ig_refresh(message: types.Message, state: FSMContext):
    """/ig_refresh — обновить куки аккаунта-донора."""
    admin_ids = _get_admin_ids()
    if admin_ids and message.from_user.id not in admin_ids:
        await message.answer("⛔ Команда доступна только администратору.")
        return

    from instagram_viewer import _POOL, _session_dir
    accounts = [login for login, _ in _POOL._accounts]

    if not accounts:
        await message.answer("❌ Нет настроенных аккаунтов (IG_ACCOUNTS не задан).")
        return

    statuses = get_sessions_status()
    status_map = {s["login"]: s for s in statuses}

    buttons = []
    for acc in accounts:
        s    = status_map.get(acc, {})
        icon = "✅" if s.get("alive") else "💀"
        req  = s.get("requests", 0)
        cur  = " 👈" if s.get("is_current") else ""
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {acc}  ({req} req){cur}",
            callback_data=f"ig_refresh_pick:{acc}"
        )])

    await message.answer(
        "🔄 <b>Обновление кук Instagram</b>\n\n"
        "Выбери аккаунт:\n"
        "<i>✅ — сессия живая  💀 — требует обновления</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def callback_ig_refresh_pick(callback: types.CallbackQuery, state: FSMContext):
    """Выбран аккаунт — просим прислать куки."""
    admin_ids = _get_admin_ids()
    if admin_ids and callback.from_user.id not in admin_ids:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    username = callback.data.split(":", 1)[1]
    await state.set_state(IGStates.waiting_cookies)
    await state.update_data(refresh_username=username)
    await callback.answer()

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="ig_refresh_cancel")
    ]])

    await callback.message.edit_text(
        f"👤 Аккаунт: <b>{username}</b>\n\n"

        "━━━ Способ 1: EditThisCookie (рекомендую) ━━━\n"
        "1. Установи расширение <b>EditThisCookie</b> в Chrome/Firefox\n"
        "2. Зайди на <code>instagram.com</code> под нужным аккаунтом\n"
        "3. Нажми иконку расширения → кнопка <b>Экспорт</b> (📤)\n"
        "4. Отправь сюда весь JSON — текстом ИЛИ файлом (можно прислать\n"
        "   .json/.txt файл экспорта прямо как документ, я его тоже прочитаю)\n\n"

        "━━━ Способ 2: DevTools (F12) ━━━\n"
        "1. <code>F12</code> → вкладка <b>Application</b> → <b>Cookies</b> → instagram.com\n"
        "2. Скопируй <code>sessionid</code> и <code>ds_user_id</code> в формате:\n"
        "<code>sessionid: ВОТ_ЗНАЧЕНИЕ\n"
        "ds_user_id: ВОТ_ЗНАЧЕНИЕ</code>\n\n"

        "━━━ Способ 3: Firefox — консоль ━━━\n"
        "1. Нажми <code>F12</code> → <b>Консоль</b>\n"
        "2. Введи: <code>copy(document.cookie)</code>\n"
        "3. Вставь из буфера сюда\n\n"

        "<i>Поддерживаю: JSON от EditThisCookie (текстом или файлом), "
        "Netscape-формат, key:value</i>",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )


async def callback_ig_refresh_cancel(callback: types.CallbackQuery, state: FSMContext):
    """Кнопка «❌ Отмена» под инструкцией обновления кук."""
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text("🚫 Обновление кук отменено.")
    except Exception:
        pass


async def cmd_ig_cancel(message: types.Message, state: FSMContext):
    """/cancel — отменить текущий шаг (ввод username или кук)."""
    cur = await state.get_state()
    if cur in (IGStates.waiting_username.state, IGStates.waiting_cookies.state):
        await state.clear()
        await message.answer("🚫 Отменено.")
    else:
        await message.answer("Нечего отменять.")


async def process_ig_cookies(message: types.Message, state: FSMContext):
    """Получили блок кук — парсим, сохраняем, тестируем, перезагружаем."""
    admin_ids = _get_admin_ids()
    if admin_ids and message.from_user.id not in admin_ids:
        await state.clear()
        return

    data = await state.get_data()
    username = data.get("refresh_username", "")
    await state.clear()

    if not username:
        await message.answer("❌ Сессия устарела. Начни заново: /ig_refresh")
        return

    # ── Достаём текст: либо из сообщения, либо из присланного файла ───────────
    raw_text = message.text or message.caption or ""
    if message.document:
        try:
            file = await message.bot.get_file(message.document.file_id)
            buf = await message.bot.download_file(file.file_path)
            raw_text = buf.read().decode("utf-8", errors="ignore")
        except Exception as e:
            await message.answer(f"❌ Не удалось прочитать файл: {e}")
            return

    # ── Парсим куки ───────────────────────────────────────────────────────────
    cookies = _parse_cookie_block(raw_text)
    sessionid  = cookies.get("sessionid", "")
    ds_user_id = cookies.get("ds_user_id", "")

    if not sessionid:
        await message.answer(
            "❌ Не нашёл <b>sessionid</b> в тексте.\n\n"
            "Убедись, что скопировал полностью.\n"
            "Попробуй снова: /ig_refresh",
            parse_mode="HTML",
        )
        return

    if not ds_user_id:
        await message.answer(
            "⚠️ Не нашёл <b>ds_user_id</b>. Попробую без него, "
            "но рекомендую экспортировать все куки через EditThisCookie.",
            parse_mode="HTML",
        )

    # ── Читаем старый session.json (если есть) — и для переиспользования
    #    отпечатка устройства, и для бэкапа на случай если новые куки не взлетят ──
    from instagram_viewer import _session_dir, _POOL
    import json as _json

    session_dir  = _session_dir()
    session_file = session_dir / f"{username}.json"
    backup_file  = session_dir / f"{username}.json.bak"

    existing_settings = None
    if session_file.exists():
        try:
            existing_settings = _json.loads(session_file.read_text())
        except Exception as e:
            logger.warning(f"[refresh] не удалось прочитать старый session.json {username}: {e}")
        try:
            backup_file.write_bytes(session_file.read_bytes())
            logger.info(f"[refresh] бэкап: {backup_file}")
        except Exception as e:
            logger.warning(f"[refresh] не удалось создать бэкап: {e}")

    # ── Собираем session.json (переиспользуя отпечаток устройства из старого) ──
    session_data = _build_session_json(username, cookies, existing=existing_settings)
    if not session_data:
        await message.answer(
            "❌ Не хватает данных для сборки сессии.\n"
            "Нужны как минимум <b>sessionid</b> и <b>ds_user_id</b>.\n"
            "Попробуй снова: /ig_refresh",
            parse_mode="HTML",
        )
        return

    # ── Сохраняем новый файл ──────────────────────────────────────────────────
    try:
        session_file.write_text(_json.dumps(session_data, indent=2))
    except Exception as e:
        await message.answer(f"❌ Не удалось сохранить файл: {e}")
        return

    # ── Статусное сообщение ───────────────────────────────────────────────────
    status_msg = await message.answer("🔄 Файл сохранён. Тестирую новые куки...")

    # ── Безопасный горячий перезапуск ────────────────────────────────────────
    reload_ok, reload_msg = await _POOL.reload_client(username)

    try:
        await status_msg.delete()
    except Exception:
        pass

    if not reload_ok:
        # Восстанавливаем бэкап если он есть
        restored = False
        if backup_file.exists():
            try:
                session_file.write_bytes(backup_file.read_bytes())
                # Тихо перезагружаем старый клиент
                await _POOL.reload_client(username)
                persist_session(username)
                restored = True
                logger.info(f"[refresh] восстановлен бэкап для {username}")
            except Exception as e:
                logger.error(f"[refresh] не удалось восстановить бэкап: {e}")

        restore_note = (
            "\n✅ Предыдущая рабочая сессия восстановлена из бэкапа." if restored
            else "\n⚠️ Бэкап недоступен — аккаунт в пуле может быть нерабочим."
        )

        await message.answer(
            f"❌ <b>Куки не работают!</b>\n\n"
            f"👤 Аккаунт: <b>{username}</b>\n"
            f"Причина: {reload_msg}"
            f"{restore_note}\n\n"
            f"Получи свежие куки и попробуй снова: /ig_refresh",
            parse_mode="HTML",
        )
        return

    persist_session(username)      # копия в БД — переживёт стирание диска
    await message.answer(
        f"✅ <b>Куки обновлены и проверены!</b>\n\n"
        f"👤 Аккаунт: <b>{username}</b>\n"
        f"🔑 sessionid: <code>{sessionid[:30]}...</code>\n"
        f"🆔 ds_user_id: <code>{ds_user_id}</code>\n"
        f"✅ Клиент протестирован — запрос к Instagram прошёл\n"
        f"💾 Бэкап: <code>{backup_file.name}</code>",
        parse_mode="HTML",
    )
    logger.info(f"✅ ig_refresh: куки {username} обновлены пользователем {message.from_user.id}")


# ── /ig_status — статус всех сессий ──────────────────────────────────────────

async def cmd_ig_status(message: types.Message):
    """/ig_status — текущий статус всех аккаунтов пула."""
    admin_ids = _get_admin_ids()
    if admin_ids and message.from_user.id not in admin_ids:
        await message.answer("⛔ Команда доступна только администратору.")
        return

    statuses = get_sessions_status()
    if not statuses:
        await message.answer("❌ Аккаунтов в пуле нет (IG_ACCOUNTS не задан).")
        return

    lines = ["📊 <b>Статус Instagram аккаунтов:</b>\n"]
    for s in statuses:
        icon = "✅" if s["alive"] else "💀"
        cur  = " 👈 текущий" if s["is_current"] else ""
        lim  = f"  ⏳ в 429 ещё ~{max(1, s['limited_for'] // 60)} мин" if s.get("limited_for") else ""
        lines.append(
            f"{icon} <code>{s['login']}</code>{cur}{lim}\n"
            f"    запросов: {s['requests']}"
        )

    try:
        h = get_pool_health(3600)
        errs = ", ".join(f"{k}: {v}" for k, v in h["errors"].items()) or "нет"
        lines.append(
            f"\n📈 <b>за последний час:</b>\n"
            f"    успешных запросов: {h['ok']}\n"
            f"    ошибки: {errs}\n"
            f"    в 429 сейчас: {h['rate_limited']} из {h['total']}\n"
            f"    очередь к IG: {h.get('queue', 0)}"
        )
        le = h["last_error"]
        if le:
            import html as _h
            ago = int((time.time() - le["ts"]) // 60)
            lines.append(
                f"    последняя ошибка ({ago} мин назад): "
                f"<code>{_h.escape(le['kind'])}</code> {_h.escape(le['msg'][:120])}"
            )
    except Exception as e:
        logger.warning(f"ig_status health: {e}")

    buttons = []
    for s in statuses:
        if not s["alive"]:
            buttons.append([InlineKeyboardButton(
                text=f"🔄 Обновить {s['login']}",
                callback_data=f"ig_refresh_pick:{s['login']}"
            )])

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )


# ── INLINE: «@бот ig natgeo» → карточка профиля в любом чате ───────────────────

_IG_INLINE_RE = re.compile(
    r"^\s*(?:ig|insta|instagram|инста|инст)\s*[:\s]\s*@?([A-Za-z0-9_.]{1,30})\s*$", re.I)
_IG_INLINE_BARE_RE = re.compile(r"^\s*(?:ig|insta|instagram|инста|инст)\s*:?\s*$", re.I)
_IG_INLINE_URL_RE = re.compile(
    r"^\s*(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]{1,30})/?(?:\?\S*)?\s*$", re.I)
_IG_RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "share", "accounts"}


def parse_ig_inline(text: str) -> str | None:
    """
    None — это не Instagram-запрос (пусть обработают музыка/аниме);
    ""   — введён только префикс («ig») — покажем подсказку;
    иначе — ник.
    """
    t = (text or "").strip()
    m = _IG_INLINE_RE.match(t)
    if m:
        return m.group(1).lower()
    if _IG_INLINE_BARE_RE.match(t):
        return ""
    m = _IG_INLINE_URL_RE.match(t)
    if m and m.group(1).lower() not in _IG_RESERVED:
        return m.group(1).lower()
    return None


def _iq_id(prefix: str, key: str) -> str:
    return hashlib.md5(f"{prefix}:{key}".encode()).hexdigest()


def _inline_profile_text(info: dict) -> str:
    import html
    name = html.escape(info.get("full_name") or info.get("username", ""))
    user = html.escape(info.get("username", ""))
    verified = " ✅" if info.get("is_verified") else ""
    private = "🔒 приватный" if info.get("is_private") else "🌐 открытый"
    bio = (info.get("biography") or "").strip()
    return (
        f"📸 <b>{name}</b>{verified}\n"
        f'👤 <a href="https://instagram.com/{user}">@{user}</a> · {private}\n\n'
        f"👥 <b>{_fmt_count(info.get('followers', 0))}</b> подписчиков · "
        f"👣 <b>{_fmt_count(info.get('following', 0))}</b> подписок · "
        f"🖼 <b>{_fmt_count(info.get('posts_count', 0))}</b> постов"
        + (f"\n📝 <i>{html.escape(bio[:300])}</i>" if bio else "")
    )


def _inline_storage_chat():
    if not _IG_INLINE_STORAGE:
        return None
    try:
        return int(_IG_INLINE_STORAGE)
    except ValueError:
        return _IG_INLINE_STORAGE


async def _inline_card_fid(bot: Bot, info: dict) -> str | None:
    """
    file_id картинки-карточки профиля. Inline не умеет загружать файлы, поэтому карточку
    один раз заливаем в служебный чат (IG_INLINE_STORAGE_CHAT) и дальше переиспользуем file_id.
    Не успели за пару секунд — вернём None (сборка продолжится в фоне, в следующий раз будет готово).
    """
    chat = _inline_storage_chat()
    if chat is None:
        return None
    u = (info.get("username") or "").lower()
    digest = "|".join(str(x) for x in (
        info.get("followers"), info.get("following"), info.get("posts_count"),
        _avatar_path(info.get("avatar_url", "")), (info.get("biography") or "")[:40],
        info.get("full_name"), info.get("is_verified"),
    ))
    key = f"card:{u}"
    row = ig_cache_get(key)
    if row and row["caption"] == digest and time.time() - row["created"] < 6 * 3600 and row["items"]:
        return row["items"][0]["file_id"]

    async def _build() -> str | None:
        avatar = await _download(info.get("avatar_url", "")) if info.get("avatar_url") else None
        card = await asyncio.get_event_loop().run_in_executor(None, _make_profile_card, info, avatar)
        if not card:
            return None
        msg = await bot.send_photo(
            chat, BufferedInputFile(card, filename=f"{u}.png"), disable_notification=True)
        fid = msg.photo[-1].file_id
        ig_cache_put(key, u, digest, [{"kind": "photo", "file_id": fid}], 1)
        return fid

    task = asyncio.ensure_future(_build())
    _BG_TASKS.add(task)
    task.add_done_callback(lambda t: (_BG_TASKS.discard(t), t.cancelled() or t.exception()))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=4)
    except Exception:
        return None


def _ig_article(rid: str, title: str, desc: str, text: str, kb=None, thumb: str | None = None):
    kw = {}
    if LinkPreviewOptions is not None:
        content = InputTextMessageContent(
            message_text=text, parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True))
    else:  # pragma: no cover
        content = InputTextMessageContent(
            message_text=text, parse_mode="HTML", disable_web_page_preview=True)
    if thumb:
        kw["thumbnail_url"] = thumb
    return InlineQueryResultArticle(
        id=rid, title=title, description=desc, input_message_content=content,
        reply_markup=kb, **kw)


async def inline_instagram(iq: types.InlineQuery):
    """Inline: `ig natgeo` / `ig:natgeo` / ссылка на профиль → карточка профиля."""
    uname = parse_ig_inline(iq.query)
    if not uname:
        await iq.answer([_ig_article(
            "ig_hint", "📸 Instagram: введи ник", "например: ig natgeo",
            "📸 Напиши после имени бота: <code>ig ник</code>")], cache_time=5, is_personal=True)
        return

    try:
        status, info = await asyncio.wait_for(get_user_status(uname), timeout=IG_INLINE_TIMEOUT)
    except asyncio.TimeoutError:
        status, info = "timeout", None

    link = f"https://instagram.com/{uname}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📸 Открыть в Instagram", url=link)]])
    results = []
    cache_time = 3

    if status == "ok" and info:
        cache_time = 30
        text = _inline_profile_text(info)
        title = f"{info.get('full_name') or uname} (@{uname})"
        desc = (f"{_fmt_count(info.get('followers', 0))} подписчиков · "
                f"{_fmt_count(info.get('posts_count', 0))} постов")
        fid = await _inline_card_fid(iq.bot, info)
        if fid:
            results.append(InlineQueryResultCachedPhoto(
                id=_iq_id("card", uname), photo_file_id=fid, title=title, description=desc,
                caption=text, parse_mode="HTML", reply_markup=kb))
        results.append(_ig_article(
            _iq_id("txt", uname), title, desc, text, kb, thumb=info.get("avatar_url") or None))
    elif status == "notfound":
        results.append(_ig_article(
            _iq_id("nf", uname), f"❌ @{uname} не найден", "проверь написание ника",
            f"❌ Пользователь @{uname} не найден в Instagram"))
    else:       # timeout / error — пул занят или IG капризничает; отдаём хотя бы ссылку
        results.append(_ig_article(
            _iq_id("lnk", uname), f"📸 @{uname}", "профиль не успел загрузиться — отправить ссылку",
            f'📸 <a href="{link}">@{uname}</a> в Instagram', kb))

    try:
        await iq.answer(results, cache_time=cache_time, is_personal=False)
    except Exception as e:       # «query is too old» и т.п.
        logger.warning(f"inline ig answer: {e}")


# ── РЕГИСТРАЦИЯ ───────────────────────────────────────────────────────────────

def register_ig_handlers(dp: Dispatcher):
    """Зарегистрировать все Instagram-хендлеры в диспетчере."""
    init_ig_db()

    # Регистрируем callback для алертов при смерти сессии
    set_session_dead_callback(_notify_admins_session_dead)

    dp.message.register(cmd_ig, Command(commands=["ig", "instagram", "insta"]))
    dp.message.register(cmd_ig_subs,    Command("ig_subs"))
    dp.message.register(cmd_ig_popular, Command("ig_popular"))
    dp.message.register(cmd_ig_status,  Command("ig_status"))

    # /cancel — регистрируем РАНЬШЕ хендлеров состояний, чтобы команда
    # перехватывалась в любом состоянии, а не улетала в парсер username/кук
    dp.message.register(cmd_ig_cancel, Command("cancel"))

    dp.message.register(process_ig_username, IGStates.waiting_username)

    dp.callback_query.register(callback_ig_profile,         F.data.startswith("ig_profile:"))
    dp.callback_query.register(callback_ig_stories,         F.data.startswith("ig_stories:"))
    dp.callback_query.register(callback_ig_stories_all,     F.data.startswith("ig_stall:"))
    dp.callback_query.register(callback_ig_highlights,      F.data.startswith("ig_hl:"))
    dp.callback_query.register(callback_ig_highlight_view,  F.data.startswith("ig_hlv:"))
    dp.callback_query.register(callback_ig_posts,           F.data.startswith("ig_posts:"))
    dp.callback_query.register(callback_ig_sub,             F.data.startswith("ig_sub:"))
    dp.callback_query.register(callback_ig_my_subs,         F.data == "ig_my_subs")
    dp.callback_query.register(callback_ig_related,         F.data.startswith("ig_related:"))
    dp.callback_query.register(callback_ig_sub_filter,      F.data.startswith("ig_sf:"))
    dp.callback_query.register(callback_ig_sub_filter_menu, F.data.startswith("ig_sfm:"))

    dp.message.register(cmd_ig_refresh,          Command("ig_refresh"))
    dp.callback_query.register(callback_ig_refresh_pick,   F.data.startswith("ig_refresh_pick:"))
    dp.callback_query.register(callback_ig_refresh_cancel, F.data == "ig_refresh_cancel")
    dp.message.register(process_ig_cookies,      IGStates.waiting_cookies)

    # Скачивание поста/рилса по ссылке. Регистрируем ПОСЛЕДНИМИ среди текстовых
    # хендлеров IG, чтобы состояния (username / куки) имели приоритет.
    dp.message.register(cmd_igdl, Command("igdl"))
    if IG_AUTO_LINKS:
        dp.message.register(handle_ig_media_link, StateFilter(None), _is_ig_media_link)

    logger.info("✅ Instagram хендлеры зарегистрированы")
