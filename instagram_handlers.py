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
import difflib
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
    WebAppInfo,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument,
    InlineQueryResultArticle, InlineQueryResultCachedPhoto, InlineQueryResultCachedVideo,
    InputTextMessageContent,
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
    ig_cache_get, ig_cache_put, ig_cache_drop, ig_get_filter, ig_set_filter,
    search_accounts, reset_notfound_cache, clear_profile_caches,
    get_lookup_stats, cached_usernames, get_recent_events,
    ig_follow_rows, ig_follow_state, ig_follow_logins, ig_like_get, ig_bm_has, ig_bm_count, ig_bm_list, ig_bm_del,
    ig_all_subscriptions as _ig_all_subs_for_follow,
    follow_user, unfollow_user, refresh_pending_follows, like_toggle, bookmark_toggle,
    set_action_block_callback, ig_blocks_all, ig_block_clear, ig_blocked_logins,
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
# Авто-сжатие видео 49–200 МБ через ffmpeg вместо пропуска файла
IG_COMPRESS          = os.getenv("IG_COMPRESS", "1") != "0"
IG_COMPRESS_TIMEOUT  = int(os.getenv("IG_COMPRESS_TIMEOUT", "300"))   # сек на один проход ffmpeg
_COMPRESS_TARGET     = 45 * 1024 * 1024
_COMPRESS_SEM        = asyncio.Semaphore(1)                           # ffmpeg тяжёлый — по одному
IG_INLINE_DL_TIMEOUT = float(os.getenv("IG_INLINE_DL_TIMEOUT", "5"))  # ждём скачивания ролика в inline
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
    waiting_new_login = State()


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


async def _compress_video(data: bytes) -> bytes | None:
    """
    Пережать видео до ≤45 МБ (720p, crf 28; если мало — 480p, crf 33). None — не вышло / нет ffmpeg.
    Работает отдельным процессом, event loop не блокируется; одновременно — одно сжатие.
    """
    import tempfile
    async with _COMPRESS_SEM:
        with tempfile.TemporaryDirectory(prefix="igc_") as d:
            src = os.path.join(d, "in.mp4")
            with open(src, "wb") as f:
                f.write(data)
            for crf, h in ((28, 720), (33, 480)):
                dst = os.path.join(d, f"out_{crf}.mp4")
                cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
                       "-vf", f"scale=-2:min({h}\\,ih)",
                       "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
                       "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                       "-threads", "2", dst]
                try:
                    p = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                    try:
                        _, err = await asyncio.wait_for(p.communicate(), timeout=IG_COMPRESS_TIMEOUT)
                    except asyncio.TimeoutError:
                        p.kill()
                        await p.wait()
                        logger.warning("ffmpeg compress: таймаут")
                        return None
                except FileNotFoundError:
                    logger.warning("ffmpeg не найден — сжатие недоступно")
                    return None
                except Exception as e:
                    logger.warning(f"ffmpeg compress: {e}")
                    return None
                if p.returncode != 0 or not os.path.exists(dst):
                    logger.warning(f"ffmpeg compress rc={p.returncode}: {(err or b'')[:200]!r}")
                    return None
                size = os.path.getsize(dst)
                if 500 < size <= _COMPRESS_TARGET:
                    logger.info(f"сжато {len(data)/1048576:.1f} → {size/1048576:.1f} МБ (crf {crf}, {h}p)")
                    with open(dst, "rb") as f:
                        return f.read()
    return None


async def _fetch_indexed(media_list: list[dict], limit: int = 10) -> list:
    """
    Скачать элементы параллельно (до 4). Результат — СТРОГО по порядку входа:
    dict {kind,data,meta,declared,degraded,compressed} | "big" | None (не скачалось).
    Видео 49–200 МБ не отбрасываются, а сжимаются ffmpeg (IG_COMPRESS=0 — выключить).
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
        compressed = False
        if len(data) > _TG_FILE_LIMIT:
            if kind == "video" and IG_COMPRESS:
                small = await _compress_video(data)
                if small:
                    data, compressed = small, True
            if len(data) > _TG_FILE_LIMIT:
                return "big"
        meta = await _video_meta(data) if kind == "video" else {}
        return {"kind": kind, "data": data, "meta": meta, "declared": declared,
                "degraded": degraded or (m.get("type") == "video" and kind != "video"),
                "compressed": compressed}

    return list(await asyncio.gather(*[_one(m) for m in media_list[:limit]]))


async def _fetch_media_files(media_list: list[dict], limit: int = 10) -> tuple[list[dict], int]:
    """
    Скачать элементы медиа (dict'ы с type/url/thumb) параллельно.
    Возвращает ([{kind, data, meta, declared, degraded, compressed}], сколько_не_влезло_в_50МБ).
    """
    res = await _fetch_indexed(media_list, limit)
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
            d = {"kind": "video", "file_id": msg.video.file_id}
            th = getattr(msg.video, "thumbnail", None) or getattr(msg.video, "thumb", None)
            if th:
                d["thumb"] = th.file_id            # превью для мини-аппы «Лента»
            return d
        if getattr(msg, "photo", None):
            d = {"kind": "photo", "file_id": msg.photo[-1].file_id}
            if len(msg.photo) > 1:
                d["thumb"] = msg.photo[1].file_id  # ~320px — для сетки хватает и грузится быстро
            return d
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


def _ago(ts) -> str:
    if not ts:
        return ""
    d = max(0, int(time.time() - float(ts)))
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин. назад"
    if d < 86400:
        return f"{d // 3600} ч. назад"
    return f"{d // 86400} дн. назад"


def _safe_link(u: str) -> str:
    u = (u or "").strip()
    return u if u.startswith(("http://", "https://")) and len(u) <= 400 else ""


def _story_caption(user: str, story: dict) -> str:
    """📸 История @user · 🕒 3 ч. назад / 🔗 ссылка / 👥 отмечены. HTML."""
    import html as _h
    head = f"📸 История @{user}"
    ago = _ago(story.get("taken_at"))
    if ago:
        head += f" · 🕒 {ago}"
    lines = [head]
    link = _safe_link(story.get("link", ""))
    if link:
        lines.append(f'🔗 <a href="{_h.escape(link, quote=True)}">Ссылка со стикера</a>')
    ments = story.get("mentions") or []
    if ments:
        lines.append("👥 Отмечены: " + ", ".join(
            f'<a href="https://instagram.com/{_h.escape(m)}">@{_h.escape(m)}</a>' for m in ments[:5]))
    return "\n".join(lines)[:1020]


def _stories_batch_caption(user: str, stories: list[dict]) -> str:
    """Единая подпись к альбому историй: заголовок + по строке на историю."""
    import html as _h
    n = len(stories)
    if n == 1:
        return _story_caption(user, stories[0])
    lines = [f'📸 <b>@{_h.escape(user)}</b> добавил(а) {n} новых историй']
    for i, s in enumerate(stories, 1):
        parts = []
        ago = _ago(s.get("taken_at"))
        if ago:
            parts.append(f"🕒 {ago}")
        if _safe_link(s.get("link", "")):
            parts.append("🔗 ссылка")
        if s.get("mentions"):
            parts.append("👥 " + " ".join("@" + m for m in s["mentions"][:3]))
        if parts:
            line = f"{i}. " + " · ".join(parts)
            if sum(len(x) + 1 for x in lines) + len(line) > 950:
                break
            lines.append(line)
    return "\n".join(lines)[:1020]


def _stories_meta_kb(stories: list[dict]):
    """Кнопки: профили отмеченных людей + ссылки со стикеров. None — если нечего показывать."""
    rows, row, seen = [], [], set()
    for s in stories:
        for m in s.get("mentions") or []:
            if m in seen or len(seen) >= 6:
                continue
            seen.add(m)
            row.append(InlineKeyboardButton(text=f"👤 @{m}", callback_data=f"ig_profile:{m}"))
            if len(row) == 2:
                rows.append(row)
                row = []
    if row:
        rows.append(row)
    links = [(i, _safe_link(s.get("link", ""))) for i, s in enumerate(stories, 1)]
    links = [(i, u) for i, u in links if u][:4]
    if links:
        rows.append([InlineKeyboardButton(
            text=("🔗 Ссылка со стикера" if len(stories) == 1 else f"🔗 История #{i}"), url=u)
            for i, u in links[:2]])
        if len(links) > 2:
            rows.append([InlineKeyboardButton(text=f"🔗 История #{i}", url=u) for i, u in links[2:]])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _mdu(text: str) -> str:
    """Экранировать ник/текст для legacy-Markdown (underscore в нике ломал разбор: np_lf)."""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


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
            InlineKeyboardButton(text=_follow_label(ig_username), callback_data=f"ig_fl:{ig_username}"),
            InlineKeyboardButton(text="🔖 Закладки", callback_data="ig_bml:0"),
        ],
        [
            InlineKeyboardButton(text="👥 Похожие", callback_data=f"ig_sim:{ig_username}"),
            InlineKeyboardButton(text="📦 Архив", callback_data=f"ig_dmp:{ig_username}"),
        ],
        [
            InlineKeyboardButton(text="🖼 Скачать аватарку", callback_data=f"ig_avdl:{ig_username}"),
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
    ig_username: str, user_id: int, next_cursor: str, has_more: bool,
    carousels: list | None = None, acts: list | None = None,
) -> InlineKeyboardMarkup:
    buttons = []
    if acts:                                   # ❤️ лайк и 📑 закладка по номеру поста в ленте
        lk = [InlineKeyboardButton(text=f"{'❤️' if ig_like_get(sc) else '🤍'}{n}",
                                   callback_data=f"ig_lk:{sc}:{ig_username}") for n, sc in acts if sc]
        bm = [InlineKeyboardButton(text=f"{'🔖' if ig_bm_has(str(user_id), sc) else '📑'}{n}",
                                   callback_data=f"ig_bm:{sc}:{ig_username}") for n, sc in acts if sc]
        for grp in (lk, bm):
            for i in range(0, len(grp), 5):
                buttons.append(grp[i:i + 5])
    # «🗂 Пост #3 (10 слайдов)» — по клику выгружаем все слайды альбомом
    row = []
    for num, sc, n in (carousels or [])[:12]:
        row.append(InlineKeyboardButton(
            text=f"🗂 Пост #{num} ({n} сл.)", callback_data=f"ig_car:{sc}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
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
_CAROUSEL_CACHE: dict[str, dict] = {}      # shortcode → пост (все слайды уже известны — 0 запросов к IG)


def _carousel_remember(sc: str, post: dict, username: str) -> None:
    if len(_CAROUSEL_CACHE) > 300:
        for k in list(_CAROUSEL_CACHE)[:100]:
            _CAROUSEL_CACHE.pop(k, None)
    _CAROUSEL_CACHE[sc] = {**post, "username": username}


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


def _cb(prefix: str, text: str) -> str:
    """callback_data ≤ 64 БАЙТ (кириллица — 2 байта на букву): режем по байтам."""
    room = 64 - len(prefix.encode("utf-8"))
    return prefix + text.encode("utf-8")[:room].decode("utf-8", "ignore")


def _suggest_usernames(name: str, tg_user_id: int, n: int = 3) -> list[str]:
    """Похожие ники из того, что бот и пользователь уже видели (подсказка при опечатке)."""
    cands: set[str] = set()
    for fn in (
        lambda: [x["ig_username"] for x in ig_get_subscriptions(str(tg_user_id))],
        lambda: [x["username"] for x in _get_my_viewed(str(tg_user_id), 50)],
        lambda: [x["username"] for x in _get_popular_profiles(100)],
        lambda: cached_usernames(300),
    ):
        try:
            cands.update(u.lower() for u in fn() if u)
        except Exception:
            pass
    cands.discard(name)
    return difflib.get_close_matches(name, sorted(cands), n=n, cutoff=0.6)


async def _profile_fail_message(source, ig_username: str, tg_user_id: int, status: str) -> None:
    """Сообщение, когда профиль не получен: «нет такого» и «инста глючит» — разные вещи."""
    import html
    esc = html.escape(ig_username)
    rows: list[list[InlineKeyboardButton]] = []
    q = ig_username[:30]
    if status == "notfound":
        text = f"❌ <b>@{esc}</b> — такого ника нет\n<i>проверь, как он написан</i>"
        sugg = _suggest_usernames(ig_username, tg_user_id)
        if sugg:
            text += "\n\nможет, это?"
            rows += [[InlineKeyboardButton(text=f"👤 @{u}", callback_data=f"ig_profile:{u}")] for u in sugg]
        rows.append([InlineKeyboardButton(text="🔎 искать по имени", callback_data=_cb("ig_find:", q))])
    else:
        text = (f"⚠️ инста сейчас не отдаёт профиль <b>@{esc}</b> (лимиты или сессия). "
                "это не значит, что ника нет — попробуй ещё раз через минуту")
        rows.append([InlineKeyboardButton(text="🔄 повторить", callback_data=_cb("ig_retry:", q))])
        rows.append([InlineKeyboardButton(text="🔎 искать по имени", callback_data=_cb("ig_find:", q))])
    await source.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _show_profile(source, ig_username: str, tg_user_id: int) -> bool:
    """Показать профиль Instagram. True — карточка показана (в т.ч. устаревшая), False — нет."""
    import html
    ig_username = ig_username.lower().strip("@")

    anim = _LoadingAnimation(source)
    await anim.start(f"🔍 Ищу профиль @{ig_username}...")
    await asyncio.sleep(0.3)

    status, info = await get_user_status(ig_username, allow_stale=True, swr=True)

    if status not in ("ok", "stale", "swr") or not info:
        await anim.done()
        await _profile_fail_message(source, ig_username, tg_user_id, status)
        return False

    stale_ts = info.pop("_stale_ts", None) if status in ("stale", "swr") else None
    old_info = dict(info)

    await anim.step("📸 Загружаю аватарку...")

    avatar_bytes: bytes | None = None
    if not stale_ts:                     # из кэша (swr / сбой IG) пул ради аватарки не гоняем — зависнем на ретраях
        try:
            avatar_bytes = await get_avatar_bytes(ig_username)
        except Exception:
            pass
    if not avatar_bytes:
        avatar_url = info.get("avatar_url", "")
        if avatar_url:
            try:
                avatar_bytes = await _download(avatar_url)
            except Exception:
                avatar_bytes = None

    await anim.step("✨ Формирую карточку...")
    await anim.done()

    _record_view(ig_username, str(tg_user_id))
    kb = _profile_keyboard(ig_username, tg_user_id)
    sent = await _send_profile_with_counter_animation(source, info, kb, avatar_bytes)
    if status == "swr" and sent is not None:
        # карточка показана мгновенно из кэша — свежие цифры подтянем фоном и поправим сообщение
        _spawn(_swr_refresh_card(sent, ig_username, old_info, kb))
    elif status == "stale" and stale_ts:
        when = datetime.fromtimestamp(stale_ts).strftime("%d.%m %H:%M")
        await source.answer(
            f"⚠️ инста сейчас не отвечает — это сохранённая карточка от {when}, цифры могли поменяться",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 обновить", callback_data=_cb("ig_retry:", ig_username[:30]))
            ]]),
        )
    return True


async def _swr_refresh_card(sent, ig_username: str, old: dict, kb) -> None:
    """Фон: берём свежий профиль и, если цифры/био поменялись, правим подпись уже отправленной карточки."""
    try:
        set_background_priority()
        status, new = await get_user_status(ig_username, force=True)
        if status != "ok" or not new or not _profile_changed(old, new):
            return
        text = _profile_text_final(new)
        if getattr(sent, "photo", None):
            await sent.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            await sent.edit_text(text, parse_mode="HTML", reply_markup=kb)
        logger.info(f"[swr] @{ig_username}: карточка обновлена в фоне")
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"[swr] @{ig_username}: {e}")


# ── ПОИСК ПО ИМЕНИ / ПОВТОР ───────────────────────────────────────────────────

_RETRY_TS: dict[int, float] = {}


async def callback_ig_retry(callback: types.CallbackQuery):
    username = callback.data.split(":", 1)[1].lower().strip("@")
    uid = callback.from_user.id
    now = time.time()
    if now - _RETRY_TS.get(uid, 0) < 5:
        await callback.answer("подожди пару секунд", show_alert=False)
        return
    _RETRY_TS[uid] = now
    await callback.answer("🔄 пробую ещё раз...")
    reset_notfound_cache(username)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _show_profile(callback.message, username, uid)


async def _do_name_search(source, query: str) -> None:
    import html
    query = " ".join(query.split())[:60]
    esc = html.escape(query)
    wait = await source.answer(f"🔎 ищу «{esc}»...", parse_mode="HTML")
    res = await search_accounts(query, 8)
    try:
        await wait.delete()
    except Exception:
        pass

    if res is None:
        await source.answer(
            "⚠️ инста сейчас не отдаёт поиск, попробуй через минуту",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 повторить", callback_data=_cb("ig_find:", query))
            ]]),
        )
        return
    if not res:
        await source.answer(f"😶 по «{esc}» ничего не нашёл. попробуй написать иначе или латиницей",
                            parse_mode="HTML")
        return

    lines = [f"🔎 нашёл по «{esc}»:\n"]
    rows = []
    for u in res:
        mark = "✅" if u["is_verified"] else ("🔒" if u["is_private"] else "▫️")
        name = f" — {html.escape(u['full_name'])}" if u["full_name"] else ""
        lines.append(f"{mark} <b>@{html.escape(u['username'])}</b>{name}")
        rows.append([InlineKeyboardButton(text=f"👤 @{u['username']}",
                                          callback_data=f"ig_profile:{u['username']}")])
    lines.append("\n<i>✅ верифицирован · 🔒 закрытый</i>")
    await source.answer("\n".join(lines), parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def callback_ig_find(callback: types.CallbackQuery):
    query = callback.data.split(":", 1)[1]
    await callback.answer("🔎 ищу...")
    await _do_name_search(callback.message, query)


async def cmd_ig_find(message: types.Message):
    """/ig_find <имя или ник> — поиск аккаунтов Instagram по имени."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("напиши имя или ник: <code>/ig_find leya cakey</code>", parse_mode="HTML")
        return
    await _do_name_search(message, parts[1])


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
            f"😶 У *@{_mdu(ig_username)}* нет активных историй прямо сейчас.\n"
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
            await _send_files(out, files, _story_caption(ig_username, story),
                              kb=_stories_meta_kb([story]))
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


async def callback_ig_avatar_dl(callback: types.CallbackQuery):
    """Скачать аватарку профиля в полном размере (фото + оригинал файлом)."""
    ig_username = callback.data.split(":", 1)[1]
    await callback.answer("🖼 Скачиваю аватарку...")
    info = await get_user_info(ig_username)
    data = None
    if info and info.get("avatar_url"):
        data = await _download(info["avatar_url"])          # HD-ссылка с CDN
    if not data:
        data = await get_avatar_bytes(ig_username)          # запасной путь через instagrapi
    if not data:
        await callback.message.answer("❌ Не удалось скачать аватарку.")
        return
    cap = f'🖼 Аватарка <a href="https://instagram.com/{ig_username}">@{ig_username}</a>'
    try:
        await callback.message.answer_photo(
            BufferedInputFile(data, filename=f"{ig_username}.jpg"), caption=cap, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"avatar photo: {e}")
    try:    # оригинал без сжатия Telegram
        await callback.message.answer_document(
            BufferedInputFile(data, filename=f"{ig_username}_avatar.jpg"))
    except Exception as e:
        logger.warning(f"avatar doc: {e}")


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
        _fs = ig_follow_state(ig_username)
        _hint = ("⏳ заявка уже отправлена — жду одобрения." if _fs == "pending"
                 else "Нажми «➕ Подписать бота» — бот отправит заявку, а после одобрения посты и истории откроются.")
        await callback.message.answer(
            f"🔒 Профиль *@{_mdu(ig_username)}* приватный — посты недоступны.\n_{_hint}_",
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
    carousels: list = []         # (номер в ленте, shortcode, число слайдов)
    to_cache: list = []          # параллельно album_items: (shortcode, total) или None
    acts: list = []              # (номер в ленте, shortcode) — для кнопок лайк/закладка
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

        cap = f"#{len(album_items) + 1}  ❤️ {_fmt_count(likes)}  💬 {_fmt_count(comments)}"
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
            if n_media > 1 and sc:
                carousels.append((len(album_items) + 1, sc, n_media))
                _carousel_remember(sc, post, ig_username)
            acts.append((len(album_items) + 1, sc))
            album_items.append(_mk_cached_media(row["items"][0], cap))
            to_cache.append(None)
            continue

        files, _ = await _fetch_media_files(media_list[:1], limit=1)
        if not files:
            continue
        f = files[0]
        if n_media > 1 and sc:
            carousels.append((len(album_items) + 1, sc, n_media))
            _carousel_remember(sc, post, ig_username)
        acts.append((len(album_items) + 1, sc))
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
    total_str  = f" из {total}" if total else ""
    footer = f"✅ Посты {shown_from+1}–{shown_from+len(posts)}{total_str} @{ig_username}"
    if acts:
        footer += "\n🤍 лайк (в Instagram) · 📑 в закладки — кнопки с номером поста"
    await callback.message.answer(
        footer,
        reply_markup=_posts_keyboard(ig_username, callback.from_user.id, cursor_key, has_more,
                                     carousels, acts)
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
        if sub_type in ("posts", "stories"):
            _spawn(_private_hint(callback.message, ig_username))

    try:
        await callback.message.edit_reply_markup(
            reply_markup=_profile_keyboard(ig_username, callback.from_user.id)
        )
    except Exception:
        pass


# ── МОИ ПОДПИСКИ ──────────────────────────────────────────────────────────────

def _subs_view(uid: str):
    """Текст и клавиатура «Мои подписки»: у каждого ника быстрые действия."""
    subs = ig_get_subscriptions(uid)
    if not subs:
        return None, None
    grouped: dict[str, list[str]] = {}
    for sb in subs:
        grouped.setdefault(sb["ig_username"], []).append(sb["sub_type"])

    icons = {"stories": "📸", "posts": "🖼", "avatar": "👤"}
    lines = ["📋 *Мои Instagram подписки:*\n"]
    buttons = []
    for uname, types_list in list(grouped.items())[:15]:
        lines.append(f"{' '.join(icons.get(t, t) for t in types_list)} @{_mdu(uname)}")
        buttons.append([InlineKeyboardButton(text=f"🔍 @{uname}", callback_data=f"ig_profile:{uname}")])
        buttons.append([
            InlineKeyboardButton(text="🖼 Посл. пост", callback_data=f"ig_last:{uname}"),
            InlineKeyboardButton(text="📸 Истории",    callback_data=f"ig_stall:{uname}"),
            InlineKeyboardButton(text="👤 Аватарка",   callback_data=f"ig_avdl:{uname}"),
        ])
    if len(grouped) > 15:
        lines.append(f"\n_…и ещё {len(grouped) - 15}_")
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="ig_my_subs")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def callback_ig_my_subs(callback: types.CallbackQuery):
    text, kb = _subs_view(str(callback.from_user.id))
    if not text:
        await callback.answer("📋 У тебя нет подписок на Instagram-аккаунты.", show_alert=True)
        return
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()


async def cmd_ig_subs(message: types.Message):
    """/ig_subs — список активных подписок."""
    text, kb = _subs_view(str(message.from_user.id))
    if not text:
        await message.answer("📋 У тебя нет Instagram-подписок. Найди профиль через /ig и подпишись.")
        return
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


async def callback_ig_last_post(callback: types.CallbackQuery):
    """Самый свежий пост ника (закреплённые не считаются) — прямо из «Мои подписки»."""
    ig_username = callback.data.split(":", 1)[1]
    await callback.answer("🖼 Ищу последний пост...")
    posts = await get_latest_posts(ig_username)
    if not posts:
        await callback.message.answer(
            f"😶 Не получил посты @{ig_username}: профиль приватный, пустой или Instagram сейчас недоступен.")
        return
    post = max(posts, key=lambda p: p.get("timestamp", 0))
    media = (post.get("media") or [])[:10]
    if not media:
        await callback.message.answer("❌ В посте нет медиа.")
        return

    sc = post.get("shortcode", "")
    cap = _post_caption_html(ig_username, post.get("caption") or "",
                             post.get("like_count", 0), post.get("comment_count", 0))
    if sc:
        cap = (cap + f'\n🔗 <a href="https://www.instagram.com/p/{sc}/">Открыть в Instagram</a>')[:1020]
    out = _out_message(callback.message, reply=False)
    kb = _profile_btn(ig_username) if len(media) == 1 else None

    row = ig_cache_get(sc) if sc else None
    if row and len(row["items"]) == row["total"] == len(media):
        try:
            await _send_cached(out, row, caption=cap, kb=kb)
            return
        except Exception as e:
            logger.warning(f"last post cached: {e}")
            ig_cache_drop(sc)

    files, too_big = await _fetch_media_files(media, limit=10)
    if not files:
        await callback.message.answer("❌ Не удалось скачать файлы поста.")
        return
    try:
        msgs, complete = await _send_files_safe(out, files, cap, kb=kb)
    except Exception as e:
        logger.warning(f"last post send: {e}")
        await callback.message.answer("❌ Ошибка отправки поста.")
        return
    if sc and complete and not too_big and len(files) == len(media) and not any(f["degraded"] for f in files):
        ig_cache_put(sc, ig_username,
                     _post_caption_html(ig_username, post.get("caption") or "",
                                        post.get("like_count", 0), post.get("comment_count", 0)),
                     _cache_items(msgs), len(media))


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
    if not _FOLLOW_TASK["started"]:
        _FOLLOW_TASK["started"] = True
        _spawn(ig_follow_watch_task(bot))

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
    has_access = (not priv) or bool(ig_follow_logins(ig_username, "following"))
    if cur_pk:                                              # без pk (приватный fallback) состояние не трогаем
        if st.get("is_private") is not None and st["is_private"] != priv:
            await _notify_users(
                bot, _sub_users(subs),
                ((f"🔒 <b>@{ig_username}</b> закрыл(а) аккаунт, но бот подписан — слежу дальше.") if has_access else
                 (f"🔒 <b>@{ig_username}</b> закрыл(а) аккаунт — посты и истории недоступны, "
                  f"пока он приватный.")) if priv else
                (f"🔓 <b>@{ig_username}</b> снова открытый аккаунт — слежу дальше."),
            )
        upd["is_private"] = priv
        await _check_profile_text(bot, ig_username, subs, st, info, upd)
    if upd:
        ig_set_user_state(ig_username, **upd)

    # ── лёгкая проверка постов ──
    # Тянем несколько последних постов, и только если media_count изменился
    # (или давно не проверяли — защита от «удалил один, выложил другой»).
    latest = None
    post_subs = [x for x in subs if x["sub_type"] == "posts"]
    if post_subs and has_access:
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


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\r", "")).strip()


async def _check_profile_text(bot: Bot, ig_username: str, subs: list[dict], st: dict,
                              info: dict, upd: dict) -> None:
    """
    Смена БИО / имени. Данные уже пришли в get_user_status(force=True) — доп. запросов к IG ноль.
    Первый раз просто запоминаем (без уведомления). Пишем в upd — сохранит вызывающий.
    """
    import html as _h
    new_bio = (info.get("biography") or "").strip()
    new_name = (info.get("full_name") or "").strip()
    old_bio, old_name = st.get("biography"), st.get("full_name")
    msgs: list[str] = []

    def _q(x: str) -> str:
        return f"<i>{_h.escape(x[:250])}</i>" if x else "<i>(пусто)</i>"

    if old_bio is None:
        upd["biography"] = new_bio
    elif _norm_text(old_bio) != _norm_text(new_bio):
        upd["biography"] = new_bio
        msgs.append(f"📝 <b>@{_h.escape(ig_username)}</b> обновил(а) биографию:\n"
                    f"<b>Было:</b> {_q(old_bio)}\n<b>Стало:</b> {_q(new_bio)}")
    if old_name is None:
        upd["full_name"] = new_name
    elif _norm_text(old_name) != _norm_text(new_name):
        upd["full_name"] = new_name
        msgs.append(f"✏️ <b>@{_h.escape(ig_username)}</b> сменил(а) имя:\n"
                    f"{_q(old_name)} → {_q(new_name)}")
    if msgs:
        await _notify_users(bot, _sub_users(subs), "\n\n".join(msgs),
                            kb=_profile_btn(ig_username))


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
                caption=f"👤 *@{_mdu(ig_username)}* сменил(а) аватарку!",
                parse_mode="Markdown",
            )
        else:
            await bot.send_message(
                user_id, f"👤 *@{_mdu(ig_username)}* сменил(а) аватарку!", parse_mode="Markdown"
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
    new_stories.sort(key=lambda s: s.get("taken_at") or 0)      # от старых к новым

    # Сначала качаем — и только потом шлём, иначе при недоступном CDN
    # заголовок прилетал бы каждый цикл без самих историй.
    batch = new_stories[:10]
    res = await _fetch_indexed(batch, limit=10)
    ready: list[tuple[dict, dict]] = []
    for story, r in zip(batch, res):
        if isinstance(r, dict):
            ready.append((story, r))
        else:
            _story_fail(user_id, story["id"])
    if not ready:
        return False

    # Одной пачкой (альбом до 10, подпись — в первом элементе) вместо N отдельных пушей.
    out = _out_chat(bot, user_id)
    st_list = [s for s, _ in ready]
    files = [f for _, f in ready]
    kb = _stories_meta_kb(st_list)
    delivered: list[dict] = []
    try:
        await _send_files(out, files, _stories_batch_caption(ig_username, st_list),
                          kb=kb if len(files) == 1 else None)
        delivered = st_list
        if len(files) > 1 and kb is not None:      # кнопки к альбому — тихим сообщением (без звука)
            try:
                await bot.send_message(user_id, f"👥 Отметки и ссылки из историй @{ig_username}:",
                                       reply_markup=kb, disable_notification=True)
            except Exception as e:
                logger.debug(f"stories kb {user_id}: {e}")
    except Exception as e:
        logger.warning(f"IG stories album {user_id}: {e} — шлю по одной")
        for i, (story, f) in enumerate(ready):
            try:
                await _send_files(out, [f], _story_caption(ig_username, story),
                                  kb=_stories_meta_kb([story]))
                delivered.append(story)
            except Exception as e2:
                logger.warning(f"IG story notify {user_id}: {e2}")
                _story_fail(user_id, story["id"])
            await asyncio.sleep(0.5)
    for story in delivered:
        ig_mark_sent(user_id, f"story_{story['id']}")
        _STORY_FAILS.pop((user_id, story["id"]), None)
    return bool(delivered)


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
# Мобильное «Поделиться → Копировать ссылку»: instagram.com/share/reel/XXXX, /share/p/XXXX, /share/XXXX
_IG_SHARE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/share/(?:(?:p|reel|reels|tv|story|stories)/)?[A-Za-z0-9_-]+[^\s]*",
    re.I)
_IG_HL_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/stories/highlights/(\d+)", re.I)
_IG_STORY_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/stories/([A-Za-z0-9_.]+)/(\d+)", re.I)
_IG_USTORIES_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/stories/([A-Za-z0-9_.]+)/?(?![A-Za-z0-9_./])", re.I)


def _extract_media_link(text: str) -> str | None:
    m = _IG_MEDIA_RE.search(text or "")
    return m.group(0) if m else None


def _shortcode_from_link(link: str) -> str:
    if "/share/" in (link or ""):          # код в share-ссылке ≠ shortcode поста
        return ""
    m = _IG_SC_RE.search(link or "")
    return m.group(1) if m else ""


def _parse_ig_link(text: str):
    """
    Что за ссылка на Instagram (share-ссылки проверяются первыми, иначе «share/reel/X» ложно
    принимается за пост):
      ("share", (url,))               — instagram.com/share/... (резолвится редиректом в _dispatch_ig_link)
    остальное — см. _parse_ig_link_core.
    """
    m = _IG_SHARE_RE.search(text or "")
    if m:
        url = m.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        return "share", (url,)
    return _parse_ig_link_core(text)


def _parse_ig_link_core(text: str):
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


async def _resolve_share_link(url: str, timeout: float = 6.0) -> str | None:
    """
    instagram.com/share/... → конечная ссылка на пост/рилс/сторис.
    Идём по редиректам вручную (allow_redirects=False), без скачивания тела.
    Если IG отправил на логин (accounts/login/?next=/reel/XXX/) — достаём путь из next.
    """
    from urllib.parse import urljoin, parse_qs
    if not url.startswith("http"):
        url = "https://" + url
    headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"),
        "Accept": "text/html,*/*;q=0.8",
    }
    cur = url
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(6):
                async with s.get(cur, headers=headers, allow_redirects=False, ssl=False,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    loc = r.headers.get("Location")
                    if r.status not in (301, 302, 303, 307, 308) or not loc:
                        break
                    cur = urljoin(cur, loc)
                nxt = parse_qs(urlparse(cur).query).get("next")
                if "accounts/login" in cur and nxt:
                    cur = urljoin("https://www.instagram.com", nxt[0])
                clean = cur.split("?")[0]
                if _IG_SHARE_RE.search(clean):
                    continue
                if _parse_ig_link_core(clean):
                    return clean
    except Exception as e:
        logger.warning(f"share resolve {url[:70]}: {e}")
    return None


async def _is_ig_media_link(message: types.Message) -> bool:
    t = message.text or ""
    return bool(t) and not t.startswith("/") and _parse_ig_link(t) is not None


# ── Музыка из Reels ───────────────────────────────────────────────────────────
_MUSIC_MEM: dict[str, dict] = {}


def _remember_music(sc: str, post: dict) -> None:
    m = post.get("music")
    if not sc or not m:
        return
    if len(_MUSIC_MEM) > 300:
        for k in list(_MUSIC_MEM)[:100]:
            _MUSIC_MEM.pop(k, None)
    vurl = next((x.get("url") for x in post.get("media", []) if x.get("type") == "video"), "")
    _MUSIC_MEM[sc] = {**m, "video_url": vurl or "", "username": post.get("username", "")}


def _music_line(music: dict) -> str:
    import html as _h
    a, t = music.get("artist", ""), music.get("title", "")
    if music.get("kind") == "original":
        txt = f"Оригинальный звук — {a}" if a else (t or "Оригинальный звук")
    else:
        txt = f"{a} — {t}" if a and t else (t or a)
    return f"🎵 {_h.escape(txt)}"[:200]


def _post_kb(user: str, sc: str, music_kind: str | None, uid=None):
    """Лайк/закладка + профиль + (для рилсов со звуком) кнопки «найти трек» / «скачать звук»."""
    rows = []
    if sc and uid is not None:
        ar = _act_row(sc, user, uid)
        if ar:
            rows.append(ar)
    if sc and music_kind == "licensed":
        rows.append([InlineKeyboardButton(text="🎵 Найти трек", callback_data=f"ig_music:s:{sc}"),
                     InlineKeyboardButton(text="🎧 Только звук", callback_data=f"ig_music:a:{sc}")])
    elif sc and music_kind == "original":
        rows.append([InlineKeyboardButton(text="🎧 Скачать звук", callback_data=f"ig_music:a:{sc}")])
    if user:
        rows.append([InlineKeyboardButton(text=f"👤 профиль @{user}", callback_data=f"ig_profile:{user}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _music_kind_from_caption(caption: str) -> str | None:
    c = caption or ""
    if "🎵 Оригинальный звук" in c:
        return "original"
    return "licensed" if "🎵 " in c else None


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
                kb = (_post_kb(row["username"], url_sc, _music_kind_from_caption(row.get("caption", "")),
                               uid=message.chat.id)
                      if row["total"] == 1 else None)
                await _send_cached(out, row, kb=kb)
                return True
            except Exception as e:
                logger.warning(f"cached send {url_sc}: {e} — сбрасываю кэш")
                ig_cache_drop(url_sc)

    try:
        await message.bot.send_chat_action(message.chat.id, "upload_video")
    except Exception:
        pass

    post = await get_media_by_url(link)       # пул → yt-dlp → embed
    if not post or not post.get("media"):
        return False
    return await _send_post_dict(message, post, url_sc)


async def _send_post_dict(message: types.Message, post: dict, url_sc: str = "") -> bool:
    """Скачать и отправить уже известный пост (dict из get_media_by_url / get_posts). Карусель >10 — несколькими альбомами."""
    out = _out_message(message, reply=True)
    media = post["media"][:20]
    user = post.get("username", "") or ""
    sc = post.get("shortcode") or url_sc
    music = post.get("music")
    if music and sc:
        _remember_music(sc, post)
    base_caption = _post_caption_html(
        user, post.get("caption") or "", post.get("like_count", 0), post.get("comment_count", 0),
        extra=(("\n" + _music_line(music)) if music else ""))
    kb_single = _post_kb(user, sc, music.get("kind") if music else None, uid=message.chat.id)

    all_msgs: list = []
    n_files = too_big_total = compressed = 0
    degraded = False
    complete_all = True
    for ci in range(0, len(media), 10):
        chunk = media[ci:ci + 10]
        files, too_big = await _fetch_media_files(chunk, limit=10)
        too_big_total += too_big
        if not files:
            if ci == 0:
                return False
            complete_all = False
            continue
        cap = base_caption if ci == 0 else f"🗂 {ci + 1}–{ci + len(chunk)} из {len(media)}"
        notes = []
        if too_big:
            notes.append(f"⚠️ {too_big} файл(ов) больше 50 МБ — не влезли")
        comp = sum(1 for f in files if f.get("compressed"))
        if comp:
            notes.append("🗜 видео сжато до ≤45 МБ")
        if notes:
            cap = (cap + "\n\n" + "\n".join(notes))[:1020]
        try:
            msgs, complete = await _send_files_safe(
                out, files, cap, kb=kb_single if len(media) == 1 and len(files) == 1 else None)
        except Exception as e:
            logger.warning(f"_send_post_dict {sc}: {e}")
            if ci == 0:
                return False
            complete_all = False
            continue
        all_msgs += msgs
        complete_all = complete_all and complete
        n_files += len(files)
        compressed += comp
        degraded = degraded or any(f["degraded"] for f in files)
        if len(media) > 10:
            await asyncio.sleep(1.0)

    # Запоминаем file_id — только если пост ушёл целиком и без деградации (видео → превью)
    if (complete_all and not too_big_total and not compressed and n_files == len(media)
            and not degraded and len(media) <= 10):
        items = _cache_items(all_msgs)
        for key in {post.get("shortcode") or "", url_sc} - {""}:
            ig_cache_put(key, user, base_caption, items, len(media))
    return True


async def callback_ig_carousel(callback: types.CallbackQuery):
    """«🗂 Пост #N (K слайдов)» → все слайды поста альбомом."""
    sc = callback.data.split(":", 1)[1]
    await callback.answer("🗂 Загружаю карусель...")
    post = _CAROUSEL_CACHE.get(sc)
    row = ig_cache_get(sc)
    cached_full = bool(row and len(row["items"]) == row["total"] and row["total"] > 1)
    ok = False
    try:
        if post and not cached_full:
            ok = await _send_post_dict(callback.message, post, sc)     # слайды уже известны — IG не дёргаем
        if not ok:
            ok = await _send_ig_media(callback.message, f"https://www.instagram.com/p/{sc}/")
    except Exception as e:
        logger.error(f"callback carousel {sc}: {e}")
    if not ok:
        await callback.message.answer("❌ Не удалось загрузить карусель.")


# ── «🎵 Найти трек / Скачать звук» ────────────────────────────────────────────

async def _to_mp3(data: bytes) -> bytes | None:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="igm_") as d:
        src, dst = os.path.join(d, "in.mp4"), os.path.join(d, "out.mp3")
        with open(src, "wb") as f:
            f.write(data)
        try:
            p = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-v", "error", "-i", src, "-vn",
                "-c:a", "libmp3lame", "-q:a", "3", dst,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.wait(), timeout=90)
        except Exception as e:
            logger.warning(f"ffmpeg mp3: {e}")
            return None
        if p.returncode != 0 or not os.path.exists(dst):
            return None
        with open(dst, "rb") as f:
            out = f.read()
    return out if len(out) > 500 else None


async def _music_meta(sc: str, refresh: bool = False) -> dict | None:
    if not refresh and sc in _MUSIC_MEM:
        return _MUSIC_MEM[sc]
    post = await get_media_by_url(f"https://www.instagram.com/reel/{sc}/")
    if post:
        if not post.get("music"):
            post["music"] = {"kind": "original", "artist": post.get("username", ""),
                             "title": "Оригинальный звук", "audio_url": ""}
        _remember_music(sc, post)
    return _MUSIC_MEM.get(sc)


async def _reel_audio_mp3(sc: str, meta: dict) -> bytes | None:
    for attempt in range(2):
        for url in (meta.get("video_url"), meta.get("audio_url")):
            if not url:
                continue
            data = await _download(url, "video")
            if data:
                mp3 = await _to_mp3(data)
                if mp3:
                    return mp3
        if attempt == 0:                                  # CDN-ссылка могла протухнуть — перечитаем пост
            fresh = await _music_meta(sc, refresh=True)
            if not fresh:
                break
            meta = fresh
    return None


async def callback_ig_music(callback: types.CallbackQuery):
    """ig_music:s:<sc> — найти полный трек; ig_music:a:<sc> — вырезать звук из видео."""
    _, mode, sc = callback.data.split(":", 2)
    await callback.answer("🎵 Ищу трек..." if mode == "s" else "🎧 Достаю звук...")
    meta = await _music_meta(sc)
    if not meta:
        await callback.message.answer("❌ Не удалось получить данные о звуке.")
        return
    artist, title = meta.get("artist", ""), meta.get("title", "")

    if mode == "s" and meta.get("kind") == "licensed":
        try:
            from music_engine import music_engine
            from music_handlers import send_track_to_user
            tracks = await music_engine.search_multi(f"{artist} {title}".strip(), limit=3)
            if tracks:
                await send_track_to_user(callback.message, tracks[0]["id"])
                return
        except Exception as e:
            logger.warning(f"ig_music search {sc}: {e}")
        await callback.message.answer("🔎 Полную версию не нашёл — вырезаю звук из видео…")

    mp3 = await _reel_audio_mp3(sc, meta)
    if not mp3:
        await callback.message.answer("❌ Не удалось вырезать звук (нет ffmpeg или видео недоступно).")
        return
    try:
        await callback.message.answer_audio(
            BufferedInputFile(mp3, filename=f"{(artist or 'reel')}_{sc}.mp3"),
            title=title or "Звук из Reels", performer=artist or meta.get("username") or None)
    except Exception as e:
        logger.error(f"ig_music send audio: {e}")
        await callback.message.answer("❌ Не получилось отправить аудио.")


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
    kb = _stories_meta_kb([story])
    if kb is None:
        kb = _profile_btn(user)
    await _send_files(
        _out_message(message, reply=True), files,
        _story_caption(user, story), kb=kb,
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
    if kind == "share":                       # instagram.com/share/... → реальная ссылка
        real = await _resolve_share_link(args[0])
        parsed = _parse_ig_link_core(real) if real else None
        if not parsed:
            return False
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

IG_HEALTH_INTERVAL = int(os.getenv("IG_HEALTH_INTERVAL", str(60 * 60)))   # по умолчанию раз в час


async def _run_session_health_check(bot: Bot) -> dict:
    """
    Один проход проверки сессий. Проверяет РЕАЛЬНУЮ авторизацию (приватный users/<pk>/info/),
    помечает мёртвые (с отметкой времени, чтобы пул их реально пропускал), снимает пометку
    с ожививших и шлёт админам алерт о новых смертях и воскрешениях.
    Сбой сети/лимит — не повод объявлять сессию мёртвой.
    Возвращает {"ok": [...], "dead": [...], "new_dead": [...], "revived": [...]}.
    """
    from instagram_viewer import _POOL as _ig_pool
    statuses = get_sessions_status()
    ok_logins, dead_logins, revived = [], [], []

    for s in statuses:
        login = s["login"]
        if not s["alive"]:                      # клиент не загрузился (нет/битый файл сессии)
            dead_logins.append(login)
            continue
        try:
            ok = await validate_session(login, strict=False)
        except Exception as e:
            logger.warning(f"[health] ошибка проверки {login}: {e}")
            continue                            # неопределённо — состояние не трогаем
        logger.info(f"[health] {'✅' if ok else '💀'} {login} (запросов: {s['requests']})")
        if ok:
            ok_logins.append(login)
            if _ig_pool.mark_alive(login):
                revived.append(login)
        else:
            dead_logins.append(login)
        await asyncio.sleep(random.uniform(_IG_HEALTH_DELAY_MIN, _IG_HEALTH_DELAY_MAX))

    new_dead = [l for l in dead_logins if _ig_pool.mark_dead(l)]
    admin_ids = _get_admin_ids()

    if new_dead:
        logger.warning(f"[health] новые мёртвые сессии: {new_dead}")
        dead_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🔄 Обновить {l}", callback_data=f"ig_refresh_pick:{l}")]
            for l in new_dead
        ])
        dead_list = "\n".join(f"• <code>{l}</code>" for l in new_dead)
        for uid in admin_ids:
            try:
                await bot.send_message(
                    uid, f"⚠️ <b>Health Check: мёртвые сессии</b>\n\n{dead_list}",
                    parse_mode="HTML", reply_markup=dead_kb,
                )
            except Exception as e:
                logger.warning(f"[health] алерт {uid}: {e}")
    if revived:
        logger.info(f"[health] ожили: {revived}")
        lst = "\n".join(f"• <code>{l}</code>" for l in revived)
        for uid in admin_ids:
            try:
                await bot.send_message(uid, f"✅ <b>сессии снова живы</b>\n\n{lst}", parse_mode="HTML")
            except Exception as e:
                logger.warning(f"[health] алерт {uid}: {e}")
    if not dead_logins:
        logger.info("[health] все сессии живы ✅")
    return {"ok": ok_logins, "dead": dead_logins, "new_dead": new_dead, "revived": revived}


async def ig_session_health_task(bot: Bot):
    """
    Фоновая проверка всех сессий пула (раз в IG_HEALTH_INTERVAL сек, по умолчанию час):
    мёртвую сессию замечаем сами, не дожидаясь, пока на неё наткнётся пользователь.
    Запускать из main.py: asyncio.create_task(ig_session_health_task(bot))
    """
    set_background_priority()
    STARTUP_DELAY = 3 * 60       # первый чек через 3 минуты (пусть бот прогреется)
    logger.info("🟡 Instagram session health-check запущен")
    await asyncio.sleep(STARTUP_DELAY)
    while True:
        logger.info("🔍 IG session health check...")
        try:
            await _run_session_health_check(bot)
        except Exception as e:
            logger.error(f"ig_session_health_task error: {e}")
        await asyncio.sleep(max(300, IG_HEALTH_INTERVAL))


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

def _profile_text_final(info: dict) -> str:
    """Подпись карточки профиля с реальными цифрами (используется и при фоновом обновлении)."""
    import html
    safe_name = html.escape(info.get("full_name") or info.get("username", ""))
    safe_user = html.escape(info.get("username", ""))
    verified  = "✅" if info.get("is_verified") else ""
    private   = "🔒 приватный" if info.get("is_private") else "🌐 открытый"
    bio_line  = (f"\n📝 <i>{html.escape(info.get('biography','').strip())}</i>"
                 if info.get("biography", "").strip() else "")
    return (
        f"📸 <b>{safe_name}</b> {verified}\n"
        f"👤 @{safe_user} · {private}\n\n"
        f"👥 <b>{_fmt_count(info.get('followers', 0))}</b> подписчиков · "
        f"👣 <b>{_fmt_count(info.get('following', 0))}</b> подписок · "
        f"🖼 <b>{_fmt_count(info.get('posts_count', 0))}</b> постов"
        f"{bio_line}"
    )


_PROFILE_WATCH = ("followers", "following", "posts_count", "biography", "full_name",
                  "is_verified", "is_private")


def _profile_changed(old: dict, new: dict) -> bool:
    return any(old.get(k) != new.get(k) for k in _PROFILE_WATCH)


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
    text_final = _profile_text_final(info)

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
        return sent

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
    return sent


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
    return ig_get_filter(user_id, ig_username, sub_type)      # хранится в БД, переживает рестарт


def _set_filter(user_id: str, ig_username: str, sub_type: str, f: str):
    ig_set_filter(user_id, ig_username, sub_type, f)


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
    act_kb = None
    if sc:
        _ar = _act_row(sc, ig_username, user_id)
        act_kb = InlineKeyboardMarkup(inline_keyboard=[_ar]) if _ar else None

    async def _album_buttons():                 # к альбому кнопку не прикрепить — тихим сообщением
        if act_kb is not None and n > 1:
            try:
                await bot.send_message(user_id, f"Пост @{ig_username}:", reply_markup=act_kb,
                                       disable_notification=True)
            except Exception as e:
                logger.debug(f"post kb {user_id}: {e}")

    row = ig_cache_get(sc) if sc else None
    if row and len(row["items"]) == row["total"] == n:
        try:
            await _send_cached(out, row, caption=caption[:1020], kb=act_kb if n == 1 else None)
            await _album_buttons()
            return
        except Exception as e:
            logger.warning(f"cached notify {sc}: {e}")
            ig_cache_drop(sc)

    files, too_big = await _fetch_media_files(media_list, limit=10)
    if not files:
        await _text_only()
        return

    try:
        msgs, complete = await _send_files_safe(out, files, caption[:1020],
                                                kb=act_kb if len(files) == 1 else None)
        await _album_buttons()
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
        await message.answer(
            "❌ В пуле нет аккаунтов. Добавь первый прямо из бота:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="ig_acc:add")]]),
        )
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

    buttons.append([InlineKeyboardButton(text="➕ Добавить / 🗑 удалить аккаунт", callback_data="ig_acc:list")])
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


# ── /ig_accounts — ручное добавление и удаление доноров ──────────────────────

def _acc_admin_ok(uid: int) -> bool:
    """Для добавления/удаления аккаунтов админ должен быть задан явно (IG_ADMIN_IDS)."""
    return uid in _get_admin_ids()


def _acc_list_view(note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    from instagram_viewer import ig_blocked_map
    statuses = get_sessions_status()
    try:
        blocked = ig_blocked_map()
    except Exception:
        blocked = {}
    lines = [f"👥 <b>Аккаунты-доноры</b> ({len(statuses)})", ""]
    if note:
        lines += [note, ""]
    if not statuses:
        lines.append("Пул пуст. Нажми «Добавить аккаунт».")
    btns = []
    for s in statuses:
        broken = (not s["alive"]) or s.get("dead")
        icon = "💀" if broken else "✅"
        extra = f" · ⏸ пауза действий {_left_txt(blocked[s['login']])}" if s["login"] in blocked else ""
        cur = " 👈" if s.get("is_current") else ""
        lines.append(f"{icon} <code>{_htmlmod.escape(s['login'])}</code>{cur} · запросов: {s['requests']}{extra}")
        btns.append([
            InlineKeyboardButton(text=f"🔄 {s['login']}", callback_data=f"ig_refresh_pick:{s['login']}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ig_acc:del:{s['login']}"),
        ])
    btns.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="ig_acc:add")])
    btns.append([InlineKeyboardButton(text="🔃 Обновить список", callback_data="ig_acc:list")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=btns)


async def cmd_ig_accounts(message: types.Message):
    """/ig_accounts — список доноров, добавить новый, удалить существующий."""
    if not _acc_admin_ok(message.from_user.id):
        await message.answer("⛔ Только для администратора (нужен IG_ADMIN_IDS).")
        return
    text, kb = _acc_list_view()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def callback_ig_acc(callback: types.CallbackQuery, state: FSMContext):
    if not _acc_admin_ok(callback.from_user.id):
        await callback.answer("⛔ Только админ (IG_ADMIN_IDS)", show_alert=True)
        return
    from instagram_viewer import _POOL

    parts = callback.data.split(":", 2)
    act = parts[1] if len(parts) > 1 else "list"
    arg = parts[2] if len(parts) > 2 else ""

    async def _show(text: str, kb: InlineKeyboardMarkup):
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)

    if act == "list":
        if await state.get_state() == IGStates.waiting_new_login.state:
            await state.clear()
        await callback.answer()
        text, kb = _acc_list_view()
        await _show(text, kb)

    elif act == "add":
        await state.set_state(IGStates.waiting_new_login)
        await callback.answer()
        await _show(
            "➕ <b>Новый аккаунт-донор</b>\n\n"
            "Пришли его <b>логин</b> (username), например <code>my_account</code>.\n"
            "Следом попрошу куки — как в /ig_refresh.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="ig_acc:list")]]),
        )

    elif act == "del":
        if _POOL._idx_of(arg) is None:
            await callback.answer("Такого аккаунта уже нет", show_alert=True)
            text, kb = _acc_list_view()
            await _show(text, kb)
            return
        await callback.answer()
        last = "\n\n⚠️ <b>Это последний аккаунт — Instagram в боте перестанет работать.</b>" \
            if len(_POOL._accounts) <= 1 else ""
        await _show(
            f"🗑 Удалить <code>{_htmlmod.escape(arg)}</code> из пула?\n\n"
            f"Сотрутся его сессия, паузы, лайки и подписки, сделанные с него. "
            f"Вернуть можно только заново добавив аккаунт с куками.{last}",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"ig_acc:delc:{arg}"),
                InlineKeyboardButton(text="↩️ Отмена", callback_data="ig_acc:list"),
            ]]),
        )

    elif act == "delc":
        ok, msg = await _POOL.remove_account(arg)
        await callback.answer("Удалён" if ok else "Не вышло", show_alert=not ok)
        note = (f"🗑 <code>{_htmlmod.escape(arg)}</code> удалён. "
                f"<i>Если он есть в IG_ACCOUNTS — после рестарта он не вернётся, "
                f"но лучше убери его и оттуда.</i>") if ok else f"❌ {_htmlmod.escape(msg)}"
        text, kb = _acc_list_view(note)
        await _show(text, kb)

    else:
        await callback.answer()


async def process_ig_new_login(message: types.Message, state: FSMContext):
    """Получили логин нового донора — просим куки (дальше работает process_ig_cookies)."""
    if not _acc_admin_ok(message.from_user.id):
        await state.clear()
        return
    from instagram_viewer import _POOL, norm_login

    raw = (message.text or "").strip()
    login = norm_login(raw)
    if not login:
        await message.answer(
            "❌ Логин не подходит: только латиница, цифры, точка и подчёркивание, до 30 символов.\n"
            "Пришли ещё раз или /cancel."
        )
        return
    if any(l.lower() == login for l, _ in _POOL._accounts):
        await state.clear()
        await message.answer(f"ℹ️ <code>{login}</code> уже в пуле. Обновить куки: /ig_refresh", parse_mode="HTML")
        return

    await state.set_state(IGStates.waiting_cookies)
    await state.update_data(refresh_username=login, new_account=True)
    await message.answer(
        f"👤 Аккаунт: <b>{login}</b>\n\n"
        "Теперь пришли куки этого аккаунта:\n"
        "1. Зайди на <code>instagram.com</code> именно под ним\n"
        "2. EditThisCookie → <b>Экспорт</b>\n"
        "3. Отправь JSON сюда текстом или файлом\n\n"
        "<i>Подойдут и sessionid + ds_user_id в формате key: value.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="ig_refresh_cancel")]]),
    )


async def cmd_ig_cancel(message: types.Message, state: FSMContext):
    """/cancel — отменить текущий шаг (ввод username или кук)."""
    cur = await state.get_state()
    if cur in (IGStates.waiting_username.state, IGStates.waiting_cookies.state,
               IGStates.waiting_new_login.state):
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
    new_account = bool(data.get("new_account"))
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
    if new_account:
        reload_ok, reload_msg = await _POOL.add_account(username)
    else:
        reload_ok, reload_msg = await _POOL.reload_client(username)

    try:
        await status_msg.delete()
    except Exception:
        pass

    if not reload_ok and new_account:
        for f in (session_file, backup_file):
            try:
                f.unlink()
            except Exception:
                pass
        await message.answer(
            f"❌ <b>Аккаунт не добавлен</b>\n\n"
            f"👤 <b>{_htmlmod.escape(username)}</b>\n"
            f"Причина: {_htmlmod.escape(str(reload_msg))}\n\n"
            f"Проверь, что куки именно от этого аккаунта, и повтори: /ig_accounts",
            parse_mode="HTML",
        )
        return

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
    head = ("✅ <b>Аккаунт добавлен в пул!</b>" if new_account
            else "✅ <b>Куки обновлены и проверены!</b>")
    tail = "" if new_account else f"\n💾 Бэкап: <code>{backup_file.name}</code>"
    await message.answer(
        f"{head}\n\n"
        f"👤 Аккаунт: <b>{username}</b>\n"
        f"🔑 sessionid: <code>{sessionid[:30]}...</code>\n"
        f"🆔 ds_user_id: <code>{ds_user_id}</code>\n"
        f"✅ Клиент протестирован — запрос к Instagram прошёл"
        f"{tail}",
        parse_mode="HTML",
    )
    logger.info(f"✅ ig_refresh: куки {username} обновлены пользователем {message.from_user.id}")


# ── /ig_status — статус всех сессий ──────────────────────────────────────────

async def cmd_ig_status(message: types.Message):
    """/ig_status — статус пула, методов получения профиля и кэша."""
    import html as _h
    admin_ids = _get_admin_ids()
    if admin_ids and message.from_user.id not in admin_ids:
        await message.answer("⛔ Команда доступна только администратору.")
        return

    statuses = get_sessions_status()
    if not statuses:
        await message.answer("❌ Аккаунтов в пуле нет. Добавь через /ig_accounts")
        return

    lines = ["📊 <b>Статус Instagram аккаунтов:</b>\n"]
    for s in statuses:
        broken = (not s["alive"]) or s.get("dead")
        icon = "💀" if broken else "✅"
        cur  = " 👈 текущий" if s["is_current"] else ""
        lim  = f"  ⏳ в 429 ещё ~{max(1, s['limited_for'] // 60)} мин" if s.get("limited_for") else ""
        dead = f"  ☠️ мертва уже ~{max(1, s['dead_for'] // 60)} мин" if s.get("dead") else ""
        px   = "  🌐 прокси" if s.get("proxy") else ""
        lines.append(
            f"{icon} <code>{s['login']}</code>{cur}{lim}{dead}{px}\n"
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
            ago = int((time.time() - le["ts"]) // 60)
            lines.append(
                f"    последняя ошибка ({ago} мин назад): "
                f"<code>{_h.escape(le['kind'])}</code> {_h.escape(le['msg'][:120])}"
            )
    except Exception as e:
        logger.warning(f"ig_status health: {e}")

    try:
        st = get_lookup_stats()
        meth = "  ".join(f"{k} {v['ok']}✓/{v['fail']}✗" for k, v in st["methods"].items())
        order = " → ".join(st["order"])
        brk = (f"\n    ⏸ временно пропускаю: {', '.join(st['broken'])}" if st["broken"] else "")
        total = st["hit"] + st["miss"]
        ratio = f"{st['hit'] * 100 // total}%" if total else "—"
        lines.append(
            f"\n🧭 <b>методы получения профиля</b> (с запуска):\n    {meth}\n"
            f"    сейчас порядок: {order}{brk}\n"
            f"\n💾 <b>кэш профилей:</b>\n"
            f"    в памяти: {st['cache_mem']}, на диске: {st['cache_db']}\n"
            f"    попаданий: {ratio} ({st['hit']} / {total}), «не найден» из кэша: {st['nf_hit']}\n"
            f"    мгновенных (с фоновым обновлением): {st['swr']}, устаревших при сбое: {st['stale']}, "
            f"ников в «не найден»: {st['notfound']}"
        )
    except Exception as e:
        logger.warning(f"ig_status lookup stats: {e}")

    buttons = []
    for s in statuses:
        if (not s["alive"]) or s.get("dead"):
            buttons.append([InlineKeyboardButton(
                text=f"🔄 Обновить {s['login']}",
                callback_data=f"ig_refresh_pick:{s['login']}"
            )])
    buttons.append([
        InlineKeyboardButton(text="🔍 проверить сессии", callback_data="ig_stat:check"),
        InlineKeyboardButton(text="🧹 сбросить кэш", callback_data="ig_stat:clear"),
    ])
    buttons.append([InlineKeyboardButton(text="📜 лог сбоев", callback_data="ig_stat:log")])

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 90:
        return f"{d}с"
    if d < 5400:
        return f"{d // 60}м"
    return f"{d // 3600}ч"


def _ig_log_view(include_ok: bool = False, n: int = 10) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки для /ig_log: последние сбои каскада и ошибки пула."""
    import html as _h
    ev = get_recent_events(n, include_ok)
    title = "📜 <b>Instagram: последние события</b>" + (" (с успешными)" if include_ok else " (только сбои)")
    lines = [title, ""]
    if not ev:
        lines.append("пока тихо — сбоев не было 🎉")
    for e in ev:
        t = _ago(e["ts"])
        if e["kind"] == "pool":
            lines.append(f"⚠️ <code>{t}</code> пул · <b>{_h.escape(str(e['type']))}</b>: "
                         f"{_h.escape(str(e['msg'])[:140])}")
            continue
        acc = f" · <code>{_h.escape(e['acc'])}</code>" if e.get("acc") not in (None, "", "?") else ""
        notes = f"\n      <i>{_h.escape(e['notes'][:200])}</i>" if e.get("notes") else ""
        if e["result"] == "ok":
            lines.append(f"✅ <code>{t}</code> @{_h.escape(e['user'])} · через <b>{e['method']}</b>{acc}{notes}")
        elif e["result"] == "notfound":
            lines.append(f"🤷 <code>{t}</code> @{_h.escape(e['user'])} · не найден"
                         f"{(' (' + e['method'] + ')') if e['method'] else ''}{acc}{notes}")
        else:
            lines.append(f"❌ <code>{t}</code> @{_h.escape(e['user'])} · все методы упали{acc}{notes}")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900].rsplit("\n", 1)[0] + "\n…"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 обновить", callback_data="ig_stat:logall" if include_ok else "ig_stat:log"),
        InlineKeyboardButton(text=("только сбои" if include_ok else "показать и успешные"),
                             callback_data="ig_stat:log" if include_ok else "ig_stat:logall"),
    ]])
    return text, kb


async def cmd_ig_log(message: types.Message):
    """/ig_log [all] — последние сбои Instagram с причиной по каждому методу (админ)."""
    admin_ids = _get_admin_ids()
    if admin_ids and message.from_user.id not in admin_ids:
        await message.answer("⛔ Команда доступна только администратору.")
        return
    parts = (message.text or "").split(maxsplit=1)
    include_ok = len(parts) > 1 and parts[1].strip().lower() in ("all", "ok", "все", "всё")
    text, kb = _ig_log_view(include_ok)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def callback_ig_stat(callback: types.CallbackQuery):
    """Кнопки /ig_status: проверка сессий и сброс кэша (только админ)."""
    admin_ids = _get_admin_ids()
    if admin_ids and callback.from_user.id not in admin_ids:
        await callback.answer("только для админа", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action in ("log", "logall"):
        text, kb = _ig_log_view(action == "logall")
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            await callback.answer("обновил")
        except Exception:
            await callback.answer("без изменений")
        return
    if action == "clear":
        n = clear_profile_caches()
        await callback.answer(
            f"стёр: профилей {n['mem']}, «не найден» {n['nf']}, поисков {n['search']} (+ кэш на диске)",
            show_alert=True,
        )
    elif action == "check":
        await callback.answer("🔍 проверяю сессии, минутку...")
        try:
            res = await _run_session_health_check(_BOT_REF or callback.bot)
        except Exception as e:
            await callback.message.answer(f"⚠️ проверка упала: <code>{e}</code>", parse_mode="HTML")
            return
        parts = []
        if res["ok"]:
            parts.append("✅ живы: " + ", ".join(f"<code>{l}</code>" for l in res["ok"]))
        if res["dead"]:
            parts.append("💀 мертвы: " + ", ".join(f"<code>{l}</code>" for l in res["dead"]))
        if res["revived"]:
            parts.append("🎉 ожили: " + ", ".join(f"<code>{l}</code>" for l in res["revived"]))
        await callback.message.answer("🔍 <b>проверка сессий</b>\n\n" + ("\n".join(parts) or "нечего проверять"),
                                      parse_mode="HTML")
    else:
        await callback.answer()


# ── INLINE: «@бот ig natgeo» → карточка профиля в любом чате ───────────────────

_IG_INLINE_RE = re.compile(
    r"^\s*(?:ig|insta|instagram|инста|инст)\s*[:\s]\s*@?([A-Za-z0-9_.]{1,30})\s*$", re.I)
_IG_INLINE_BARE_RE = re.compile(r"^\s*(?:ig|insta|instagram|инста|инст)\s*:?\s*$", re.I)
_IG_INLINE_LINK_RE = re.compile(
    r"^\s*(?:(?:ig|insta|instagram|инста|инст)\s*[:\s]\s*)?(?:https?://)?(?:www\.)?"
    r"(instagram\.com/(?:share/(?:[a-z]+/)?|(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/)[A-Za-z0-9_-]+[^\s]*)\s*$",
    re.I)
_IG_INLINE_URL_RE = re.compile(
    r"^\s*(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]{1,30})/?(?:\?\S*)?\s*$", re.I)
_IG_RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "share", "accounts"}


def parse_ig_inline(text: str) -> str | None:
    """
    None — это не Instagram-запрос (пусть обработают музыка/аниме);
    ""   — введён только префикс («ig») — покажем личный список (подписки/недавние);
    "http…" — ссылка на пост/рилс (в т.ч. share) → inline-скачивание;
    иначе — ник.
    """
    t = (text or "").strip()
    ml = _IG_INLINE_LINK_RE.match(t)
    if ml:
        return "https://www." + ml.group(1)
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


_INLINE_BUILDS: dict[str, asyncio.Task] = {}
_VIEW_LABELS = {"stories": "истории", "posts": "посты", "avatar": "аватарка"}


async def _inline_personal(iq: types.InlineQuery) -> None:
    """Пустой «ig»: подписки пользователя + недавно просмотренные профили → поделиться в один тап."""
    try:
        from instagram_viewer import _ucache_get
    except Exception:       # pragma: no cover
        _ucache_get = lambda u: None            # noqa: E731
    uid = str(iq.from_user.id)
    subs: dict[str, list[str]] = {}
    for s in ig_get_subscriptions(uid):
        subs.setdefault(s["ig_username"], []).append(_VIEW_LABELS.get(s["sub_type"], s["sub_type"]))
    names = list(subs)
    for v in _get_my_viewed(uid, 10):
        if v["username"] not in names:
            names.append(v["username"])
    names = names[:25]

    results = []
    for u in names:
        link = f"https://instagram.com/{u}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📸 Открыть в Instagram", url=link)]])
        info = _ucache_get(u)                      # только из кэша — без запросов к IG
        desc = ("🔔 " + ", ".join(subs[u])) if u in subs else "👁 недавно смотрел"
        if info:
            text = _inline_profile_text(info)
            title = f"{info.get('full_name') or u} (@{u})"
            thumb = info.get("avatar_url") or None
        else:
            text, title, thumb = f'📸 <a href="{link}">@{u}</a> в Instagram', f"@{u}", None
        results.append(_ig_article(_iq_id("me", u), title, desc, text, kb, thumb=thumb))
    if not results:
        results.append(_ig_article(
            "ig_hint", "📸 Instagram: введи ник или ссылку", "например: ig natgeo  или  ссылка на рилс",
            "📸 Напиши после имени бота: <code>ig ник</code> или вставь ссылку на пост/рилс"))
    try:
        await iq.answer(results, cache_time=5, is_personal=True)
    except Exception as e:
        logger.warning(f"inline ig personal: {e}")


async def _inline_media_row(bot: Bot, link: str, sc: str) -> dict | None:
    """Строка ig_file_cache для ссылки: из кэша или скачать → залить в IG_INLINE_STORAGE_CHAT (≤ IG_INLINE_DL_TIMEOUT с)."""
    row = ig_cache_get(sc) if sc else None
    if row and row["items"] and len(row["items"]) == row["total"]:
        return row
    chat = _inline_storage_chat()
    if chat is None or not sc:
        return None

    task = _INLINE_BUILDS.get(sc)
    if task is None or task.done():
        async def _build():
            post = await get_media_by_url(link)
            if not post or not post.get("media"):
                return None
            media = post["media"][:10]
            files, too_big = await _fetch_media_files(media, limit=10)
            if not files:
                return None
            user = post.get("username", "") or ""
            music = post.get("music")
            cap = _post_caption_html(
                user, post.get("caption") or "", post.get("like_count", 0), post.get("comment_count", 0),
                extra=(("\n" + _music_line(music)) if music else ""))
            msgs, complete = await _send_files_safe(_out_chat(bot, chat), files, cap)
            if (complete and not too_big and len(files) == len(media)
                    and not any(f["degraded"] or f.get("compressed") for f in files)):
                items = _cache_items(msgs)
                for key in {post.get("shortcode") or "", sc} - {""}:
                    ig_cache_put(key, user, cap, items, len(media))
                return ig_cache_get(sc)
            return None

        task = asyncio.ensure_future(_build())
        _INLINE_BUILDS[sc] = task
        _BG_TASKS.add(task)

        def _done(t, _sc=sc):
            _BG_TASKS.discard(t)
            _INLINE_BUILDS.pop(_sc, None)
            if not t.cancelled() and t.exception():
                logger.warning(f"inline media build {_sc}: {t.exception()}")
        task.add_done_callback(_done)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=IG_INLINE_DL_TIMEOUT)
    except Exception:
        return None


async def _inline_media(iq: types.InlineQuery, link: str) -> None:
    """@бот <ссылка на пост/рилс> → видео/фото прямо в чат через CachedVideo/CachedPhoto."""
    if "/share/" in link:
        try:
            real = await asyncio.wait_for(_resolve_share_link(link, timeout=3), timeout=3.5)
        except Exception:
            real = None
        if not real:
            await iq.answer([_ig_article(
                _iq_id("shr", link), "📸 Не удалось раскрыть ссылку", "попробуй обычную ссылку на рилс",
                f'📸 <a href="{link}">Instagram</a>')], cache_time=3, is_personal=True)
            return
        link = real
    sc = _shortcode_from_link(link)
    row = await _inline_media_row(iq.bot, link, sc)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📸 Открыть в Instagram", url=link)]])
    results = []
    if row:
        items, cap, n = row["items"], row.get("caption") or None, len(row["items"])
        for i, it in enumerate(items[:10]):
            rid = _iq_id("m", f"{sc}:{i}")
            suffix = f" · слайд {i + 1}/{n}" if n > 1 else ""
            if it.get("kind") == "video":
                results.append(InlineQueryResultCachedVideo(
                    id=rid, video_file_id=it["file_id"], title=f"🎬 Видео{suffix}",
                    description=f"@{row['username']}" if row.get("username") else None,
                    caption=cap, parse_mode="HTML", reply_markup=kb))
            else:
                results.append(InlineQueryResultCachedPhoto(
                    id=rid, photo_file_id=it["file_id"], title=f"🖼 Фото{suffix}",
                    description=f"@{row['username']}" if row.get("username") else None,
                    caption=cap, parse_mode="HTML", reply_markup=kb))
        cache_time = 60
    else:
        if _inline_storage_chat() is None:
            desc = "для видео в inline задай IG_INLINE_STORAGE_CHAT — пока отправлю ссылку"
        else:
            desc = "ролик ещё скачивается — повтори запрос через пару секунд"
        results.append(_ig_article(
            _iq_id("pend", sc or link), "⏳ Скачиваю… (или отправить ссылкой)", desc,
            f'📸 <a href="{link}">Instagram</a>', kb))
        cache_time = 1
    try:
        await iq.answer(results, cache_time=cache_time, is_personal=False)
    except Exception as e:
        logger.warning(f"inline ig media answer: {e}")


async def inline_instagram(iq: types.InlineQuery):
    """Inline: `ig` → мои подписки; `ig natgeo` / ссылка на профиль → карточка; ссылка на пост/рилс → видео."""
    uname = parse_ig_inline(iq.query)
    if uname == "":
        await _inline_personal(iq)
        return
    if not uname:
        return
    if uname.startswith("http"):
        await _inline_media(iq, uname)
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

# ══════════════════════════════════════════════════════════════════════════════
#  ПОДПИСКА БОТА НА АККАУНТЫ / ЛАЙКИ / ЗАКЛАДКИ
# ══════════════════════════════════════════════════════════════════════════════
import html as _htmlmod
from collections import deque as _deque

# Подписывать донор-аккаунты может только админ (IG_ADMIN_IDS): это меняет «лицо» аккаунтов
# для всех пользователей бота. Если IG_ADMIN_IDS пуст — разрешено всем. Отключить: IG_FOLLOW_ADMIN_ONLY=0
IG_FOLLOW_ADMIN_ONLY = os.getenv("IG_FOLLOW_ADMIN_ONLY", "1") != "0"
IG_ACT_PER_HOUR      = int(os.getenv("IG_ACT_PER_HOUR", "30"))       # лайков на пользователя в час
IG_FOLLOW_WATCH_EVERY = int(os.getenv("IG_FOLLOW_WATCH_EVERY", str(20 * 60)))   # проверка заявок, сек
_ACT_HITS: dict[str, _deque] = {}
_FOLLOW_TASK = {"started": False}


def _left_txt(until) -> str:
    """«через ~5 ч» / «через ~40 мин»"""
    try:
        sec = max(0, float(until) - time.time())
    except Exception:
        return "позже"
    return f"через ~{sec / 3600:.0f} ч" if sec >= 5400 else f"через ~{max(1, round(sec / 60))} мин"


async def _notify_admins_action_block(login: str, until: float, hours: float, reason: str) -> None:
    """Донор получил блок действий — лайки/подписки с него отключены, просмотры работают."""
    if not _BOT_REF:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Снять паузу", callback_data=f"ig_blk:{login}"),
        InlineKeyboardButton(text="📋 Паузы", callback_data="ig_blk:list"),
    ]])
    text = (f"⏸ <b>Пауза действий у донора</b> <code>{_htmlmod.escape(login)}</code>\n\n"
            f"Instagram ограничил лайки/подписки. Они отключены для этого аккаунта на "
            f"<b>{hours:.0f} ч</b> ({_left_txt(until)}), просмотры продолжают работать.\n"
            f"<i>{_htmlmod.escape(reason)[:200]}</i>\n\n"
            f"Совет: снизь IG_FOLLOW_PER_DAY / IG_LIKE_PER_DAY и не снимай паузу раньше срока.")
    for uid in _get_admin_ids():
        try:
            await _BOT_REF.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.warning(f"_notify_admins_action_block {uid}: {e}")


def _blocks_view():
    rows = [b for b in ig_blocks_all() if b["until"] > time.time()]
    if not rows:
        return "✅ Пауз нет — все доноры могут лайкать и подписываться.", None
    lines = ["⏸ <b>Паузы доноров</b>"]
    btns = []
    for b in rows:
        lines.append(f"• <code>{_htmlmod.escape(b['login'])}</code> — {_left_txt(b['until'])} (страйк {b['strikes']})")
        btns.append([InlineKeyboardButton(text=f"▶️ {b['login']}", callback_data=f"ig_blk:{b['login']}")])
    btns.append([InlineKeyboardButton(text="▶️ Снять все", callback_data="ig_blk:all")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=btns)


async def cmd_ig_blocks(message: types.Message):
    admins = _get_admin_ids()
    if admins and message.from_user.id not in admins:
        await message.answer("⛔ Команда доступна только администратору.")
        return
    text, kb = _blocks_view()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def callback_ig_blk(callback: types.CallbackQuery):
    admins = _get_admin_ids()
    if admins and callback.from_user.id not in admins:
        await callback.answer("Только админ", show_alert=True)
        return
    arg = callback.data.split(":", 1)[1]
    if arg == "list":
        pass
    elif arg == "all":
        ig_block_clear(None)
    else:
        ig_block_clear(arg)
    await callback.answer("Готово" if arg != "list" else "")
    text, kb = _blocks_view()
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


def _rate_ok(uid, kind: str, limit: int) -> bool:
    key = f"{kind}:{uid}"
    dq = _ACT_HITS.setdefault(key, _deque())
    now = time.time()
    while dq and now - dq[0] > 3600:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def _can_follow(uid: int) -> bool:
    if not IG_FOLLOW_ADMIN_ONLY:
        return True
    admins = _get_admin_ids()
    return (not admins) or uid in admins


def _follow_label(ig_username: str) -> str:
    st = ig_follow_state(ig_username)
    return {"following": "✅ Бот подписан", "pending": "⏳ Заявка отправлена"}.get(st, "➕ Подписать бота")


def _act_row(sc: str, user: str, uid) -> list:
    """Ряд кнопок «лайк / закладка» под постом."""
    if not sc:
        return []
    user = (user or "")[:30]
    cb_l, cb_b = f"ig_lk:{sc}:{user}", f"ig_bm:{sc}:{user}"
    if len(cb_l.encode()) > 64 or len(cb_b.encode()) > 64:
        return []
    liked = bool(ig_like_get(sc))
    saved = ig_bm_has(str(uid), sc)
    return [
        InlineKeyboardButton(text="❤️ Лайкнуто" if liked else "🤍 Лайк", callback_data=cb_l),
        InlineKeyboardButton(text="🔖 В закладках" if saved else "📑 В закладки", callback_data=cb_b),
    ]


def _patch_act_buttons(markup, sc: str, user: str, uid):
    """Перерисовать кнопки лайк/закладка конкретного поста в уже отправленной клавиатуре."""
    if markup is None:
        return None
    liked = bool(ig_like_get(sc))
    saved = ig_bm_has(str(uid), sc)
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            d = b.callback_data or ""
            if d.startswith(f"ig_lk:{sc}:"):
                num = re.search(r"\d+", b.text or "")
                txt = (("❤️" if liked else "🤍") + num.group(0)) if num else ("❤️ Лайкнуто" if liked else "🤍 Лайк")
                b = InlineKeyboardButton(text=txt, callback_data=d)
            elif d.startswith(f"ig_bm:{sc}:"):
                num = re.search(r"\d+", b.text or "")
                txt = (("🔖" if saved else "📑") + num.group(0)) if num else ("🔖 В закладках" if saved else "📑 В закладки")
                b = InlineKeyboardButton(text=txt, callback_data=d)
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _private_hint(message: types.Message, ig_username: str) -> None:
    """Подписался на уведомления приватного аккаунта, к которому у бота нет доступа — объясняем."""
    try:
        info = await get_user_info(ig_username)
        if info and info.get("is_private") and ig_follow_state(ig_username) == "none":
            await message.answer(
                f"🔒 @{ig_username} приватный — уведомления придут, только когда бот подпишется на него "
                f"и владелец примет заявку. Кнопка «➕ Подписать бота» в профиле.")
    except Exception as e:
        logger.debug(f"private hint @{ig_username}: {e}")


async def _refresh_profile_kb(message: types.Message, ig_username: str, uid: int) -> None:
    try:
        await message.edit_reply_markup(reply_markup=_profile_keyboard(ig_username, uid))
    except Exception:
        pass


# ── ➕ подписать бота ─────────────────────────────────────────────────────────

async def callback_ig_follow(callback: types.CallbackQuery):
    ig_username = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    if not _can_follow(uid):
        await callback.answer("Подписывать бота может только админ", show_alert=True)
        return
    state = ig_follow_state(ig_username)

    if state != "none":                                   # уже подписан / ждёт одобрения → спросим про отписку
        await callback.answer()
        what = "Отозвать заявку" if state == "pending" else "Отписать бота"
        await callback.message.answer(
            f"{what} от @{ig_username}?" + (
                "\n\n⚠️ Если аккаунт приватный, чтобы вернуть доступ, владельцу придётся снова принять заявку."
                if state == "following" else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data=f"ig_flc:{ig_username}:1"),
                InlineKeyboardButton(text="✖️ Нет", callback_data=f"ig_flc:{ig_username}:0"),
            ]]))
        return

    if not _rate_ok(uid, "follow", 10):
        await callback.answer("Слишком часто, подожди немного", show_alert=True)
        return
    await callback.answer("⏳ Подписываюсь...")
    res = await follow_user(ig_username, requested_by=str(uid))

    if res.get("ok"):
        n = len(res.get("logins", {}))
        if res["status"] == "pending":
            text = (f"⏳ Заявка на @{ig_username} отправлена ({n} акк.). "
                    f"Как только владелец её примет, я напишу — и посты с историями станут доступны.")
        else:
            text = f"✅ Бот подписался на @{ig_username}" + (f" ({n} акк.)" if n > 1 else "") + "."
        if res.get("errors"):
            text += f"\n⚠️ Часть аккаунтов не смогла: {len(res['errors'])}"
        await callback.message.answer(text)
    else:
        err = res.get("error")
        text = {
            "notfound":    f"❌ @{ig_username} не найден.",
            "unavailable": "❌ Instagram сейчас не отвечает, попробуй позже.",
            "nopk":        "❌ Не удалось определить аккаунт (нет id). Открой профиль ещё раз и повтори.",
            "nodonors":    "❌ Нет живых аккаунтов-доноров (проверь /ig_status).",
            "limit":       "⛔ Дневной лимит подписок исчерпан — это защита доноров от бана.",
            "blocked":     f"⏸ Подписки на паузе: Instagram ограничил действия аккаунтов-доноров. "
                           f"Снова доступны {_left_txt(res.get('until'))}.",
        }.get(err, "❌ Instagram не принял подписку (возможен лимит/проверка). Подробности — в логах.")
        await callback.message.answer(text)
        for e in (res.get("errors") or [])[:3]:
            logger.warning(f"[follow] @{ig_username}: {e}")
    await _refresh_profile_kb(callback.message, ig_username, uid)


async def callback_ig_follow_confirm(callback: types.CallbackQuery):
    _, ig_username, yes = callback.data.split(":", 2)
    uid = callback.from_user.id
    try:
        await callback.message.delete()
    except Exception:
        pass
    if yes != "1":
        await callback.answer("Отмена")
        return
    if not _can_follow(uid):
        await callback.answer("Только админ", show_alert=True)
        return
    await callback.answer("⏳")
    res = await unfollow_user(ig_username)
    if res.get("ok"):
        await callback.message.answer(f"🔕 Бот отписался от @{ig_username}.")
    else:
        await callback.message.answer(f"❌ Не вышло отписаться от @{ig_username}: Instagram не ответил.")


async def ig_follow_watch_task(bot: Bot):
    """Раз в IG_FOLLOW_WATCH_EVERY проверяет заявки: одобрили → сообщаем и пересеиваем подписки."""
    set_background_priority()
    await asyncio.sleep(120)
    while True:
        try:
            changes = await refresh_pending_follows()
            by_user: dict[str, list[dict]] = {}
            for ch in changes:
                by_user.setdefault(ch["username"], []).append(ch)
            for uname, chs in by_user.items():
                approved = any(c["new"] == "following" for c in chs)
                targets = {c["requested_by"] for c in chs if c.get("requested_by")}
                subs = [x for x in _ig_all_subs_for_follow() if x["ig_username"] == uname]
                targets |= {x["user_id"] for x in subs}
                if approved:
                    text = (f"✅ <b>@{uname}</b> принял(а) заявку — бот теперь видит посты и истории. "
                            f"Уведомления для этого аккаунта работают.")
                    for x in subs:                       # старые посты не слать «новыми»
                        if x["sub_type"] in ("posts", "stories"):
                            try:
                                await _seed_subscription(x["user_id"], uname, x["sub_type"])
                            except Exception as e:
                                logger.debug(f"reseed {uname}: {e}")
                else:
                    text = f"❌ <b>@{uname}</b> отклонил(а) заявку (или отозвал(а) её)."
                await _notify_users(bot, targets, text, kb=_profile_btn(uname))
        except Exception as e:
            logger.error(f"ig_follow_watch_task: {e}")
        await asyncio.sleep(IG_FOLLOW_WATCH_EVERY)


# ── ❤️ лайк ───────────────────────────────────────────────────────────────────

async def callback_ig_like(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    sc = parts[1]
    user = parts[2] if len(parts) > 2 else ""
    uid = callback.from_user.id
    if not _rate_ok(uid, "like", IG_ACT_PER_HOUR):
        await callback.answer("Слишком часто — подожди немного", show_alert=True)
        return
    await callback.answer("⏳")
    res = await like_toggle(sc, user, str(uid))
    if not res.get("ok"):
        msg = {
            "private": "🔒 Аккаунт приватный, а бот на него не подписан",
            "limit":   "⛔ Дневной лимит лайков исчерпан",
            "blocked": f"⏸ Лайки на паузе: Instagram ограничил действия аккаунта-донора. "
                       f"Вернутся примерно {_left_txt(res.get('until'))}.",
        }.get(res.get("error"), "❌ Instagram не принял лайк")
        await callback.answer(msg, show_alert=True)
        return
    await callback.answer("❤️ Лайк поставлен" if res["liked"] else "💔 Лайк снят")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_patch_act_buttons(callback.message.reply_markup, sc, user, uid))
    except Exception:
        pass


# ── 🔖 закладки ───────────────────────────────────────────────────────────────

def _caption_plain(sc: str) -> str:
    cap = ""
    row = ig_cache_get(sc)
    if row:
        cap = row.get("caption") or ""
    if not cap:
        cap = (_CAROUSEL_CACHE.get(sc) or {}).get("caption") or ""
    cap = re.sub(r"<[^>]+>", "", cap)
    return _htmlmod.unescape(cap).replace("\n", " ").strip()[:200]


async def callback_ig_bookmark(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    sc = parts[1]
    user = parts[2] if len(parts) > 2 else ""
    uid = callback.from_user.id
    if not user:
        row = ig_cache_get(sc)
        user = (row or {}).get("username", "")
    res = await bookmark_toggle(str(uid), sc, user, _caption_plain(sc))
    if not res.get("ok"):
        await callback.answer("❌ Не удалось сохранить", show_alert=True)
        return
    await callback.answer("🔖 Добавлено в закладки (/ig_saved)" if res["saved"] else "Убрано из закладок")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_patch_act_buttons(callback.message.reply_markup, sc, user, uid))
    except Exception:
        pass


_BM_PAGE = 8


def _bm_view(uid: str, page: int):
    total = ig_bm_count(uid)
    if not total:
        return ("🔖 <b>Закладки</b>\n\nПока пусто. Под постами жми «📑 В закладки».\n"
                "Поиск по постам: <code>/ig_search слово</code>", None)
    pages = max(1, (total + _BM_PAGE - 1) // _BM_PAGE)
    page = max(0, min(page, pages - 1))
    items = ig_bm_list(uid, page * _BM_PAGE, _BM_PAGE)
    rows = []
    for it in items:
        label = f"@{it['username']} · " if it["username"] else ""
        label += (it["caption"] or it["shortcode"])[:30]
        rows.append([
            InlineKeyboardButton(text=label[:48], callback_data=f"ig_bmo:{it['shortcode']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"ig_bmd:{it['shortcode']}:{page}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ig_bml:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"ig_bml:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ig_bml:{page + 1}"))
    rows.append(nav)
    if os.getenv("IG_WEBAPP_URL", "").startswith("https://"):
        rows.append([InlineKeyboardButton(text="🖼 Открыть ленту", web_app=WebAppInfo(url=os.getenv("IG_WEBAPP_URL")))])
    return (f"🔖 <b>Закладки</b> · {total}\nТап по посту — открыть, 🗑 — убрать.\nПоиск: <code>/ig_search слово</code>",
            InlineKeyboardMarkup(inline_keyboard=rows))


async def cmd_ig_saved(message: types.Message):
    text, kb = _bm_view(str(message.from_user.id), 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def callback_ig_bm_list(callback: types.CallbackQuery):
    page = int(callback.data.split(":", 1)[1] or 0)
    text, kb = _bm_view(str(callback.from_user.id), page)
    await callback.answer()
    try:
        if callback.message.text and callback.message.text.startswith("🔖"):
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:                                            # нажали из карточки профиля — новым сообщением
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


async def callback_ig_bm_open(callback: types.CallbackQuery):
    sc = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Открываю...")
    ok = False
    try:
        ok = await _send_ig_media(callback.message, f"https://www.instagram.com/p/{sc}/")
    except Exception as e:
        logger.warning(f"bookmark open {sc}: {e}")
    if not ok:
        await callback.message.answer(
            f"❌ Не удалось достать пост (удалён или аккаунт закрыт).\nhttps://www.instagram.com/p/{sc}/")


async def callback_ig_bm_del(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    sc, page = parts[1], int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    ig_bm_del(str(callback.from_user.id), sc)
    await callback.answer("Убрано")
    text, kb = _bm_view(str(callback.from_user.id), page)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass



def register_ig_handlers(dp: Dispatcher):
    """Зарегистрировать все Instagram-хендлеры в диспетчере."""
    init_ig_db()

    # Регистрируем callback для алертов при смерти сессии
    set_session_dead_callback(_notify_admins_session_dead)
    set_action_block_callback(_notify_admins_action_block)

    dp.message.register(cmd_ig, Command(commands=["ig", "instagram", "insta"]))
    dp.message.register(cmd_ig_subs,    Command("ig_subs"))
    dp.message.register(cmd_ig_popular, Command("ig_popular"))
    dp.message.register(cmd_ig_status,  Command("ig_status"))
    dp.message.register(cmd_ig_find,    Command("ig_find"))
    dp.message.register(cmd_ig_log,     Command("ig_log"))

    # /cancel — регистрируем РАНЬШЕ хендлеров состояний, чтобы команда
    # перехватывалась в любом состоянии, а не улетала в парсер username/кук
    dp.message.register(cmd_ig_cancel, Command("cancel"))

    dp.message.register(process_ig_username, IGStates.waiting_username)

    dp.callback_query.register(callback_ig_profile,         F.data.startswith("ig_profile:"))
    dp.callback_query.register(callback_ig_stories,         F.data.startswith("ig_stories:"))
    dp.callback_query.register(callback_ig_stories_all,     F.data.startswith("ig_stall:"))
    dp.callback_query.register(callback_ig_avatar_dl,       F.data.startswith("ig_avdl:"))
    dp.callback_query.register(callback_ig_last_post,       F.data.startswith("ig_last:"))
    dp.callback_query.register(callback_ig_highlights,      F.data.startswith("ig_hl:"))
    dp.callback_query.register(callback_ig_highlight_view,  F.data.startswith("ig_hlv:"))
    dp.callback_query.register(callback_ig_posts,           F.data.startswith("ig_posts:"))
    dp.callback_query.register(callback_ig_carousel,        F.data.startswith("ig_car:"))
    dp.callback_query.register(callback_ig_music,           F.data.startswith("ig_music:"))
    dp.callback_query.register(callback_ig_sub,             F.data.startswith("ig_sub:"))
    dp.callback_query.register(callback_ig_my_subs,         F.data == "ig_my_subs")
    dp.callback_query.register(callback_ig_related,         F.data.startswith("ig_related:"))
    dp.callback_query.register(callback_ig_sub_filter,      F.data.startswith("ig_sf:"))
    dp.callback_query.register(callback_ig_sub_filter_menu, F.data.startswith("ig_sfm:"))

    # подписка бота на аккаунты, лайки, закладки
    dp.message.register(cmd_ig_saved,                    Command("ig_saved"))
    dp.message.register(cmd_ig_blocks,                   Command("ig_blocks"))
    dp.callback_query.register(callback_ig_blk,          F.data.startswith("ig_blk:"))
    dp.callback_query.register(callback_ig_follow,       F.data.startswith("ig_fl:"))
    dp.callback_query.register(callback_ig_follow_confirm, F.data.startswith("ig_flc:"))
    dp.callback_query.register(callback_ig_like,         F.data.startswith("ig_lk:"))
    dp.callback_query.register(callback_ig_bookmark,     F.data.startswith("ig_bm:"))
    dp.callback_query.register(callback_ig_bm_list,      F.data.startswith("ig_bml:"))
    dp.callback_query.register(callback_ig_bm_open,      F.data.startswith("ig_bmo:"))
    dp.callback_query.register(callback_ig_bm_del,       F.data.startswith("ig_bmd:"))

    dp.message.register(cmd_ig_refresh,          Command("ig_refresh"))
    dp.callback_query.register(callback_ig_retry,          F.data.startswith("ig_retry:"))
    dp.callback_query.register(callback_ig_find,           F.data.startswith("ig_find:"))
    dp.callback_query.register(callback_ig_stat,           F.data.startswith("ig_stat:"))
    dp.callback_query.register(callback_ig_refresh_pick,   F.data.startswith("ig_refresh_pick:"))
    dp.callback_query.register(callback_ig_refresh_cancel, F.data == "ig_refresh_cancel")
    dp.message.register(process_ig_cookies,      IGStates.waiting_cookies)

    # ручное добавление / удаление доноров
    dp.message.register(cmd_ig_accounts,         Command("ig_accounts"))
    dp.callback_query.register(callback_ig_acc,  F.data.startswith("ig_acc:"))
    dp.message.register(process_ig_new_login,    IGStates.waiting_new_login)

    # Скачивание поста/рилса по ссылке. Регистрируем ПОСЛЕДНИМИ среди текстовых
    # хендлеров IG, чтобы состояния (username / куки) имели приоритет.
    dp.message.register(cmd_igdl, Command("igdl"))
    if IG_AUTO_LINKS:
        dp.message.register(handle_ig_media_link, StateFilter(None), _is_ig_media_link)

    logger.info("✅ Instagram хендлеры зарегистрированы")
