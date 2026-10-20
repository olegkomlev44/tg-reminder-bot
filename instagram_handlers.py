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
)
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


async def _download(url: str) -> bytes | None:
    """
    Скачать медиафайл с CDN Instagram.
    CDN требует корректный Referer, иначе отдаёт 403.
    """
    if not url:
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Accept": "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
                ssl=False,
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return data if len(data) > 500 else None
                logger.warning(f"IG download {url[:60]!r} → {resp.status}")
    except Exception as e:
        logger.error(f"IG download {url[:60]}: {e}")
    return None


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

    link = _extract_media_link(message.text or "")
    if link:
        if not await _send_ig_media(message, link):
            await message.answer("не смог достать этот пост — попробуй позже или кинь другую ссылку")
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

    sent_count = 0
    for story in stories[:20]:
        url = story.get("url", "")
        if not url:
            continue
        data = await _download(url)
        if not data:
            continue
        try:
            if story["type"] == "video":
                await callback.message.answer_video(
                    BufferedInputFile(data, filename="story.mp4"),
                    caption=f"📸 История @{ig_username}",
                    width=720, height=1280,
                )
            else:
                await callback.message.answer_photo(
                    BufferedInputFile(data, filename="story.jpg"),
                    caption=f"📸 История @{ig_username}",
                )
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
    for i, post in enumerate(posts[:10]):
        media_list = post.get("media", [])
        if not media_list:
            continue

        m = media_list[0]
        is_video = m.get("type") == "video"

        vid_url   = m.get("url", "") if is_video else ""
        thumb_url = m.get("thumb") or m.get("url", "")
        fetch_url = vid_url or thumb_url

        data = await _download(fetch_url)
        if not data and thumb_url and thumb_url != fetch_url:
            data = await _download(thumb_url)
        if not data:
            continue

        caption  = post.get("caption", "").strip()
        likes    = post.get("like_count", 0)
        comments = post.get("comment_count", 0)
        sc   = post.get("shortcode", "")
        link = f"https://www.instagram.com/p/{sc}/" if sc else ""

        cap = f"❤️ {_fmt_count(likes)}  💬 {_fmt_count(comments)}"
        if caption:
            cap += f"\n\n{caption[:200]}"
        if link:
            cap += f"\n🔗 {link}"

        real_video = is_video and vid_url and (
            vid_url.lower().endswith(".mp4") or "/videos/" in vid_url
        )
        if real_video:
            album_items.append(InputMediaVideo(
                media=BufferedInputFile(data, filename=f"post_{i}.mp4"),
                caption=cap[:1020]
            ))
        else:
            prefix = "🎬 " if is_video else ""
            album_items.append(InputMediaPhoto(
                media=BufferedInputFile(data, filename=f"post_{i}.jpg"),
                caption=f"{prefix}{cap}"[:1020]
            ))
        await asyncio.sleep(0.3)

    if not album_items:
        await callback.message.answer(
            "❌ Не удалось скачать файлы для этих постов.",
            reply_markup=_profile_keyboard(ig_username, callback.from_user.id)
        )
        return

    try:
        await callback.message.answer_media_group(album_items)
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
        # Запоминаем текущее состояние, чтобы не завалить уведомлениями о старом
        if sub_type in ("posts", "avatar"):
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
    """
    try:
        if sub_type == "posts":
            data = await get_posts(ig_username)
            ids = [f"post_{p['id']}" for p in data.get("posts", []) if p.get("id")]
            ig_mark_sent_many(user_id, ids)
            ig_update_last_seen(user_id, ig_username, "posts", "seeded")
        elif sub_type == "avatar":
            info = await get_user_info(ig_username)
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
    due = []
    for uname, user_subs in by_username.items():
        last_checked, last_activity = states.get(uname, (0.0, 0.0))
        interval = _ig_check_interval(user_subs, last_activity or now, now)
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


async def _check_user(bot: Bot, ig_username: str, subs: list[dict]) -> bool:
    """Проверка аватарки / историй / постов для одного ника. True — было что-то новое."""
    info = await get_user_info(ig_username, force=True)
    if not info:
        return False

    found = False
    for sub in subs:
        user_id   = sub["user_id"]
        sub_type  = sub["sub_type"]
        last_seen = sub.get("last_seen", "")
        try:
            if sub_type == "avatar":
                found |= await _check_avatar(bot, user_id, ig_username, info, last_seen)
            elif sub_type == "stories":
                found |= await _check_stories(bot, user_id, ig_username)
            elif sub_type == "posts":
                found |= await _check_posts(bot, user_id, ig_username, last_seen)
        except Exception as e:
            logger.error(f"ig check {ig_username}/{sub_type} → {user_id}: {e}")
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
        data = await _download(story.get("url", ""))
        if data:
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
            if story["type"] == "video":
                await bot.send_video(
                    user_id, BufferedInputFile(data, filename="story.mp4"),
                    caption=f"📸 История @{ig_username}",
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


async def _check_posts(bot: Bot, user_id: str, ig_username: str, last_seen: str) -> bool:
    # Пункт 1: раньше тут брали info["posts"], а он ВСЕГДА пустой — посты не приходили.
    data = await get_posts(ig_username)
    if data.get("private"):
        return False
    posts = data.get("posts", [])
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


# ── СКАЧИВАНИЕ ПОСТА / РИЛСА ПО ССЫЛКЕ (пункт 9) ──────────────────────────────

_IG_MEDIA_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/[A-Za-z0-9_-]+[^\s]*",
    re.I,
)
_TG_FILE_LIMIT = 49 * 1024 * 1024


def _extract_media_link(text: str) -> str | None:
    m = _IG_MEDIA_RE.search(text or "")
    return m.group(0) if m else None


async def _is_ig_media_link(message: types.Message) -> bool:
    t = message.text or ""
    return bool(t) and not t.startswith("/") and bool(_extract_media_link(t))


async def _send_ig_media(message: types.Message, link: str) -> bool:
    """
    Достать пост/рилс по ссылке и отправить в чат (карусель — альбомом).
    Возвращает False, если ничего не отправили (тогда вызывающий может уступить дорогу другим хендлерам).
    """
    import html as _html
    try:
        await message.bot.send_chat_action(message.chat.id, "upload_video")
    except Exception:
        pass

    post = await get_media_by_url(link)
    if not post or not post.get("media"):
        return False

    files: list[tuple[str, bytes]] = []
    too_big = 0
    for m in post["media"][:10]:
        kind = m.get("type", "photo")
        url = m.get("url") or ""
        if kind == "video" and not url:
            kind, url = "photo", m.get("thumb", "")      # видео-ссылки нет — хотя бы превью
        data = await _download(url or m.get("thumb", ""))
        if not data:
            continue
        if len(data) > _TG_FILE_LIMIT:
            too_big += 1
            continue
        files.append((kind, data))
    if not files:
        return False

    user = post.get("username", "")
    cap_raw = (post.get("caption") or "").strip()[:400]
    while len(_html.escape(cap_raw)) > 700:
        cap_raw = cap_raw[:-50]
    caption = (
        (f'📸 <a href="https://instagram.com/{user}">@{user}</a>\n' if user else "")
        + (f"<i>{_html.escape(cap_raw)}</i>\n" if cap_raw else "")
        + f"❤️ {_fmt_count(post.get('like_count', 0))}   💬 {_fmt_count(post.get('comment_count', 0))}"
        + (f"\n\n⚠️ {too_big} файл(ов) больше 50 МБ — не влезли" if too_big else "")
    )[:1020]

    try:
        if len(files) == 1:
            kind, data = files[0]
            kb = (
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text=f"👤 профиль @{user}", callback_data=f"ig_profile:{user}")]])
                if user else None
            )
            if kind == "video":
                await message.reply_video(
                    BufferedInputFile(data, filename="reel.mp4"),
                    caption=caption, parse_mode="HTML", reply_markup=kb,
                )
            else:
                await message.reply_photo(
                    BufferedInputFile(data, filename="post.jpg"),
                    caption=caption, parse_mode="HTML", reply_markup=kb,
                )
        else:
            group = []
            for i, (kind, data) in enumerate(files):
                cls = InputMediaVideo if kind == "video" else InputMediaPhoto
                fname = f"{i}.mp4" if kind == "video" else f"{i}.jpg"
                group.append(cls(
                    media=BufferedInputFile(data, filename=fname),
                    caption=caption if i == 0 else None,
                    parse_mode="HTML" if i == 0 else None,
                ))
            await message.reply_media_group(group)
        return True
    except Exception as e:
        logger.warning(f"_send_ig_media {link[:60]}: {e}")
        return False


async def handle_ig_media_link(message: types.Message):
    """Ссылка на пост/рилс в чате → скачиваем. Не вышло — отдаём сообщение дальше по цепочке хендлеров."""
    link = _extract_media_link(message.text or "")
    if link and await _send_ig_media(message, link):
        return
    if SkipHandler is not None:
        raise SkipHandler()


async def cmd_igdl(message: types.Message):
    """/igdl <ссылка> — скачать пост или рилс."""
    parts = (message.text or "").split(maxsplit=1)
    link = _extract_media_link(parts[1]) if len(parts) > 1 else None
    if not link:
        await message.answer("кинь ссылку на пост или рилс: /igdl https://instagram.com/reel/...")
        return
    if not await _send_ig_media(message, link):
        await message.answer("не смог достать этот пост — приватный, удалён или инста сейчас капризничает")


# ── HEALTH CHECK TASK ─────────────────────────────────────────────────────────

async def ig_session_health_task(bot: Bot):
    """
    Каждые 4 часа проверяет все сессии в пуле.
    Если сессия мертва — шлёт алерт админам.
    Запускать из main.py: asyncio.create_task(ig_session_health_task(bot))
    """
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
    import html as _html
    media_list = post.get("media", [])
    if not media_list:
        return
    m = media_list[0]
    is_video = m.get("type") == "video"

    cap_raw  = post.get("caption", "").strip()[:200]
    likes    = _fmt_count(post.get("like_count", 0))
    comments = _fmt_count(post.get("comment_count", 0))
    sc       = post.get("shortcode", "")
    icon     = "🎬" if is_video else "🖼"

    caption = (
        f"{'─' * 22}\n"
        f"{icon} <b>Новый пост</b> от <a href=\"https://instagram.com/{ig_username}\">@{ig_username}</a>\n"
        f"{'─' * 22}\n"
        + (f"📝 <i>{_html.escape(cap_raw)}</i>\n" if cap_raw else "")
        + f"\n❤️ {likes}   💬 {comments}"
        + (f"\n\n🔗 <a href=\"https://www.instagram.com/p/{sc}/\">Открыть в Instagram</a>" if sc else "")
    )

    data = await _download(m.get("url") or m.get("thumb", ""))
    if not data:
        await bot.send_message(user_id, caption, parse_mode="HTML",
                                disable_web_page_preview=True)
        return

    try:
        if is_video:
            await bot.send_video(user_id,
                BufferedInputFile(data, filename="post.mp4"),
                caption=caption[:1020], parse_mode="HTML")
        else:
            await bot.send_photo(user_id,
                BufferedInputFile(data, filename="post.jpg"),
                caption=caption[:1020], parse_mode="HTML")
    except Exception as e:
        logger.warning(f"_notify_new_post_styled {user_id}: {e}")


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
        lines.append(
            f"{icon} <code>{s['login']}</code>{cur}\n"
            f"    запросов: {s['requests']}"
        )

    try:
        h = get_pool_health(3600)
        errs = ", ".join(f"{k}: {v}" for k, v in h["errors"].items()) or "нет"
        lines.append(
            f"\n📈 <b>за последний час:</b>\n"
            f"    успешных запросов: {h['ok']}\n"
            f"    ошибки: {errs}\n"
            f"    в 429 сейчас: {h['rate_limited']} из {h['total']}"
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
