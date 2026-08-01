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
import io
import logging
import re
import time
import os

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from io import BytesIO
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from instagram_viewer import (
    init_ig_db,
    get_user_info, get_stories, get_posts, get_avatar_bytes,
    ig_subscribe, ig_unsubscribe, ig_get_subscriptions,
    ig_is_subscribed, ig_update_last_seen,
    ig_all_subscriptions, ig_mark_sent, ig_already_sent,
    validate_session, get_sessions_status,
    set_session_dead_callback,
)

logger = logging.getLogger(__name__)


# ── ГЛОБАЛЬНАЯ ССЫЛКА НА БОТА (для алертов) ──────────────────────────────────

_BOT_REF: "Bot | None" = None


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
    for uid in admin_ids:
        try:
            await _BOT_REF.send_message(
                uid,
                f"⚠️ <b>Сессия Instagram умерла!</b>\n\n"
                f"👤 Аккаунт: <code>{ig_login}</code>\n"
                f"Обнови куки: /ig_refresh\n\n"
                f"<i>Бот продолжает ротировать другие аккаунты.</i>",
                parse_mode="HTML",
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
        return m.group(1).lower()
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

async def ig_checker_task(bot: Bot):
    """
    Фоновая задача: каждые 30 минут проверяет подписки.
    Новые посты / истории / смена аватарки → уведомление.
    """
    CHECK_INTERVAL = 30 * 60
    logger.info("🟡 Instagram checker запущен")
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            await _run_ig_check(bot)
        except Exception as e:
            logger.error(f"ig_checker_task error: {e}")


async def _run_ig_check(bot: Bot):
    subs = ig_all_subscriptions()
    if not subs:
        return

    by_username: dict[str, list[dict]] = {}
    for s in subs:
        by_username.setdefault(s["ig_username"], []).append(s)

    for ig_username, user_subs in by_username.items():
        try:
            await _check_user(bot, ig_username, user_subs)
        except Exception as e:
            logger.error(f"ig check {ig_username}: {e}")
        await asyncio.sleep(3)


async def _check_user(bot: Bot, ig_username: str, subs: list[dict]):
    """Проверка новых постов / историй / аватарки для одного ig_username."""
    info = await get_user_info(ig_username)
    if not info:
        return

    for sub in subs:
        user_id  = sub["user_id"]
        sub_type = sub["sub_type"]
        last_seen = sub.get("last_seen", "")

        if sub_type == "avatar":
            current_avatar = info.get("avatar_url", "")
            if current_avatar and current_avatar != last_seen:
                ig_update_last_seen(user_id, ig_username, "avatar", current_avatar)
                data = await get_avatar_bytes(ig_username)
                if not data:
                    data = await _download(current_avatar)
                if data:
                    try:
                        await bot.send_photo(
                            user_id,
                            BufferedInputFile(data, filename="avatar.jpg"),
                            caption=f"👤 *@{ig_username}* сменил(а) аватарку!",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.warning(f"IG avatar notify {user_id}: {e}")

        elif sub_type == "stories":
            stories = await get_stories(ig_username)
            if not stories:
                continue
            new_stories = [s for s in stories if s["id"] and not ig_already_sent(user_id, f"story_{s['id']}")]
            if not new_stories:
                continue

            await bot.send_message(
                user_id,
                f"📸 *@{ig_username}* добавил(а) {len(new_stories)} новых историй!",
                parse_mode="Markdown"
            )
            for story in new_stories[:10]:
                data = await _download(story.get("url", ""))
                if not data:
                    continue
                try:
                    if story["type"] == "video":
                        await bot.send_video(
                            user_id,
                            BufferedInputFile(data, filename="story.mp4"),
                            caption=f"📸 История @{ig_username}"
                        )
                    else:
                        await bot.send_photo(
                            user_id,
                            BufferedInputFile(data, filename="story.jpg"),
                            caption=f"📸 История @{ig_username}"
                        )
                    ig_mark_sent(user_id, f"story_{story['id']}")
                except Exception as e:
                    logger.warning(f"IG story notify {user_id}: {e}")
                await asyncio.sleep(0.5)

        elif sub_type == "posts":
            posts = info.get("posts", [])
            if not posts:
                continue
            active_filter = _get_filter(user_id, ig_username, "posts")
            new_posts = [
                p for p in posts
                if p["id"]
                and not ig_already_sent(user_id, f"post_{p['id']}")
                and _post_passes_filter(p, active_filter)
            ]
            if not new_posts:
                continue
            for post in new_posts[:5]:
                await _notify_new_post_styled(bot, user_id, ig_username, post)
                ig_mark_sent(user_id, f"post_{post['id']}")
                await asyncio.sleep(0.8)


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

                await asyncio.sleep(5)

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
                for uid in admin_ids:
                    try:
                        dead_list = "\n".join(f"• <code>{l}</code>" for l in new_dead)
                        await bot.send_message(
                            uid,
                            f"⚠️ <b>Health Check: мёртвые сессии</b>\n\n"
                            f"{dead_list}\n\n"
                            f"Обнови куки: /ig_refresh",
                            parse_mode="HTML",
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


def _build_session_json(username: str, cookies: dict) -> dict | None:
    """Собрать session.json из словаря кук."""
    import uuid as _uuid, base64 as _b64, random as _rnd, time as _time

    sessionid  = cookies.get("sessionid", "")
    ds_user_id = cookies.get("ds_user_id", "")
    if not sessionid or not ds_user_id:
        return None

    ig_did = cookies.get("ig_did", str(_uuid.uuid4()).upper())

    return {
        "uuids": {
            "phone_id":          ig_did,
            "uuid":              str(_uuid.uuid4()),
            "client_session_id": str(_uuid.uuid4()),
            "advertising_id":    str(_uuid.uuid4()),
            "device_id":         "android-" + _b64.b16encode(_rnd.randbytes(8)).decode().lower(),
        },
        "cookies": {
            "sessionid":  sessionid,
            "ds_user_id": ds_user_id,
            "csrftoken":  cookies.get("csrftoken", ""),
            "mid":        cookies.get("mid", ""),
            "ig_did":     ig_did,
            "datr":       cookies.get("datr", ""),
            "rur":        cookies.get("rur", ""),
        },
        "last_login": _time.time(),
        "device_settings": {
            # Версию можно обновить: берётся из instagrapi → constants.py → SUPPORTED_APP_VERSION
            # Или смотри тут: https://github.com/subzeroid/instagrapi/blob/master/instagrapi/constants.py
            "app_version":     "365.0.0.39.109",
            "android_version": 34,
            "android_release": "14",
            "dpi":             "480dpi",
            "resolution":      "1080x2400",
            "manufacturer":    "samsung",
            "device":          "dm3q",
            "model":           "SM-S918B",
            "cpu":             "arm64-v8a",
            "version_code":    "568858490",
        },
        "user_agent": (
            "Instagram 365.0.0.39.109 Android "
            "(34/14; 480dpi; 1080x2400; samsung; SM-S918B; dm3q; arm64-v8a; en_US; 568858490)"
        ),
        "country":        "US",
        "country_code":   1,
        "locale":         "en_US",
        "timezone_offset": 10800,
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

    await callback.message.edit_text(
        f"👤 Аккаунт: <b>{username}</b>\n\n"

        "━━━ Способ 1: EditThisCookie (рекомендую) ━━━\n"
        "1. Установи расширение <b>EditThisCookie</b> в Chrome/Firefox\n"
        "2. Зайди на <code>instagram.com</code> под нужным аккаунтом\n"
        "3. Нажми иконку расширения → кнопка <b>Экспорт</b> (📤)\n"
        "4. Скопируй весь JSON и отправь сюда\n\n"

        "━━━ Способ 2: DevTools (F12) ━━━\n"
        "1. <code>F12</code> → вкладка <b>Application</b> → <b>Cookies</b> → instagram.com\n"
        "2. Скопируй <code>sessionid</code> и <code>ds_user_id</code> в формате:\n"
        "<code>sessionid: ВОТ_ЗНАЧЕНИЕ\n"
        "ds_user_id: ВОТ_ЗНАЧЕНИЕ</code>\n\n"

        "━━━ Способ 3: Firefox — консоль ━━━\n"
        "1. Нажми <code>F12</code> → <b>Консоль</b>\n"
        "2. Введи: <code>copy(document.cookie)</code>\n"
        "3. Вставь из буфера сюда\n\n"

        "<i>Поддерживаю: JSON от EditThisCookie, Netscape-формат, key:value</i>\n\n"
        "Для отмены напиши /cancel",
        parse_mode="HTML",
    )


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

    # ── Парсим куки ───────────────────────────────────────────────────────────
    cookies = _parse_cookie_block(message.text or "")
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

    # ── Собираем session.json ─────────────────────────────────────────────────
    session_data = _build_session_json(username, cookies)
    if not session_data:
        await message.answer(
            "❌ Не хватает данных для сборки сессии.\n"
            "Нужны как минимум <b>sessionid</b> и <b>ds_user_id</b>.\n"
            "Попробуй снова: /ig_refresh",
            parse_mode="HTML",
        )
        return

    # ── Бэкап старого файла ───────────────────────────────────────────────────
    from instagram_viewer import _session_dir, _POOL
    import json as _json

    session_dir  = _session_dir()
    session_file = session_dir / f"{username}.json"
    backup_file  = session_dir / f"{username}.json.bak"

    if session_file.exists():
        try:
            backup_file.write_bytes(session_file.read_bytes())
            logger.info(f"[refresh] бэкап: {backup_file}")
        except Exception as e:
            logger.warning(f"[refresh] не удалось создать бэкап: {e}")

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
    dp.callback_query.register(callback_ig_refresh_pick, F.data.startswith("ig_refresh_pick:"))
    dp.message.register(process_ig_cookies,      IGStates.waiting_cookies)

    logger.info("✅ Instagram хендлеры зарегистрированы")
