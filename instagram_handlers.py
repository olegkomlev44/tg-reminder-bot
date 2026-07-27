"""
instagram_handlers.py — хендлеры Telegram-бота для анонимного просмотра Instagram.

Подключение в main.py:
    from instagram_handlers import register_ig_handlers, ig_checker_task
    register_ig_handlers(dp)
    asyncio.create_task(ig_checker_task(bot))
"""

import asyncio
import io
import logging
import re
import time
import os

import aiohttp
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
    get_user_info, get_stories, get_posts,
    ig_subscribe, ig_unsubscribe, ig_get_subscriptions,
    ig_is_subscribed, ig_update_last_seen,
    ig_all_subscriptions, ig_mark_sent, ig_already_sent,
)

logger = logging.getLogger(__name__)

# ── FSM ───────────────────────────────────────────────────────────────────────

class IGStates(StatesGroup):
    waiting_username = State()


# ── ВСПОМОГАТЕЛЬНЫЕ ───────────────────────────────────────────────────────────

def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _clean_username(text: str) -> str:
    text = text.strip().lstrip("@")
    # убираем URL если вставили ссылку
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", text)
    if m:
        return m.group(1).lower()
    return re.sub(r"[^A-Za-z0-9_.]", "", text).lower()


async def _download(url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.error(f"IG download {url[:60]}: {e}")
    return None


def _profile_keyboard(ig_username: str, user_id: int) -> InlineKeyboardMarkup:
    uid = str(user_id)
    sub_s = ig_is_subscribed(uid, ig_username, "stories")
    sub_p = ig_is_subscribed(uid, ig_username, "posts")
    sub_a = ig_is_subscribed(uid, ig_username, "avatar")

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📸 Истории", callback_data=f"ig_stories:{ig_username}"),
            InlineKeyboardButton(text="🖼 Посты", callback_data=f"ig_posts:{ig_username}:"),
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


def _posts_keyboard(ig_username: str, user_id: int, next_cursor: str, has_more: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_more and next_cursor:
        safe_cursor = next_cursor[:100]  # callback_data ≤ 64 байт → обрежем в ключе кэша
        buttons.append([InlineKeyboardButton(text="➡️ Далее (следующие посты)", callback_data=f"ig_posts:{ig_username}:{safe_cursor}")])
    buttons.append([InlineKeyboardButton(text="◀️ Профиль", callback_data=f"ig_profile:{ig_username}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── CURSOR КЭШИ (in-memory, достаточно для сессии) ───────────────────────────
# callback_data ограничен 64 байтами — храним cursor в памяти по короткому ключу

_CURSOR_CACHE: dict[str, str] = {}  # key → full_cursor


def _store_cursor(key: str, cursor: str) -> str:
    """Сохранить cursor, вернуть короткий ключ."""
    short = key[:60]
    _CURSOR_CACHE[short] = cursor
    return short


def _get_cursor(key: str) -> str:
    return _CURSOR_CACHE.get(key, key)  # если не найден — пробуем сам ключ


# ── ХЕНДЛЕРЫ ──────────────────────────────────────────────────────────────────

async def cmd_ig(message: types.Message, state: FSMContext):
    """Команда /ig или /instagram — запрос username."""
    # Если username передан сразу: /ig username
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
    # Сохраняем id статусного сообщения чтобы удалить его потом
    await state.update_data(prompt_msg_id=sent.message_id)


async def process_ig_username(message: types.Message, state: FSMContext):
    """Обработка введённого username."""
    data = await state.get_data()
    await state.clear()
    # Удаляем сообщение-приглашение
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
    """Показать профиль Instagram и меню действий."""
    ig_username = ig_username.lower().strip("@")

    # source может быть Message или callback.message
    is_msg = isinstance(source, types.Message)
    send_fn = source.answer if is_msg else source.edit_text

    status = await source.answer(f"🔍 Ищу профиль @{ig_username}...")

    info = await get_user_info(ig_username)

    try:
        await status.delete()
    except Exception:
        pass

    if not info:
        await source.answer(
            f"❌ Пользователь *@{ig_username}* не найден или недоступен.\n"
            "_Возможно, профиль приватный или имя написано неверно._",
            parse_mode="Markdown"
        )
        return

    verified = "✅" if info.get("is_verified") else ""
    private = "🔒 приватный" if info.get("is_private") else "🌐 открытый"
    bio = info.get("biography", "").strip()
    bio_line = f"\n📝 _{bio}_" if bio else ""

    text = (
        f"📸 *{info.get('full_name') or ig_username}* {verified}\n"
        f"👤 @{ig_username} · {private}\n\n"
        f"👥 {_fmt_count(info.get('followers', 0))} подписчиков · "
        f"👣 {_fmt_count(info.get('following', 0))} подписок · "
        f"🖼 {_fmt_count(info.get('posts_count', 0))} постов"
        f"{bio_line}"
    )

    kb = _profile_keyboard(ig_username, tg_user_id)

    # Отправляем аватарку
    avatar_url = info.get("avatar_url", "")
    if avatar_url:
        avatar_bytes = await _download(avatar_url)
        if avatar_bytes:
            await source.answer_photo(
                BufferedInputFile(avatar_bytes, filename="avatar.jpg"),
                caption=text,
                parse_mode="Markdown",
                reply_markup=kb
            )
            return

    await source.answer(text, parse_mode="Markdown", reply_markup=kb)


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

    # Отправляем истории по одной (видео / фото)
    # Помечаем все как отправленные (без сохранения на диск)
    sent_count = 0
    for story in stories[:20]:  # макс 20 историй за раз
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
        # Пауза между файлами (флудконтроль Telegram)
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
    # Восстанавливаем полный cursor
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
            f"😶 У @{ig_username} нет постов или они не загрузились.",
            reply_markup=_profile_keyboard(ig_username, callback.from_user.id)
        )
        return

    # Каждый пост — одно или несколько медиа (альбом)
    # Отправляем первые 10 постов; у каждого берём первое фото для превью
    sent_posts = 0
    for post in posts[:10]:
        media_list = post.get("media", [])
        if not media_list:
            continue

        caption = post.get("caption", "").strip()
        likes = post.get("like_count", 0)
        comments = post.get("comment_count", 0)
        sc = post.get("shortcode", "")
        link = f"https://www.instagram.com/p/{sc}/" if sc else ""

        caption_text = (
            f"❤️ {_fmt_count(likes)}  💬 {_fmt_count(comments)}"
            + (f"\n\n{caption[:200]}" if caption else "")
            + (f"\n🔗 {link}" if link else "")
        )

        # Если у поста один элемент — отправляем напрямую
        if len(media_list) == 1:
            m = media_list[0]
            data = await _download(m["url"] or m["thumb"])
            if not data:
                continue
            try:
                if m["type"] == "video":
                    await callback.message.answer_video(
                        BufferedInputFile(data, filename="post.mp4"),
                        caption=caption_text[:1020],
                    )
                else:
                    await callback.message.answer_photo(
                        BufferedInputFile(data, filename="post.jpg"),
                        caption=caption_text[:1020],
                    )
                sent_posts += 1
            except Exception as e:
                logger.error(f"IG send post error: {e}")
            await asyncio.sleep(0.4)
            continue

        # Альбом: несколько медиа
        album_items = []
        for i, m in enumerate(media_list[:10]):  # max 10 в альбоме (лимит TG)
            data = await _download(m["url"] or m["thumb"])
            if not data:
                continue
            cap = caption_text[:1020] if i == 0 else None
            if m["type"] == "video":
                album_items.append(InputMediaVideo(
                    media=BufferedInputFile(data, filename=f"media_{i}.mp4"),
                    caption=cap,
                ))
            else:
                album_items.append(InputMediaPhoto(
                    media=BufferedInputFile(data, filename=f"media_{i}.jpg"),
                    caption=cap,
                ))
            await asyncio.sleep(0.1)  # не грузим сеть сразу

        if not album_items:
            continue
        try:
            await callback.message.answer_media_group(album_items)
            sent_posts += 1
        except Exception as e:
            logger.error(f"IG send album error: {e}")
        await asyncio.sleep(0.6)

    # Кнопка «Далее»
    next_cursor = result.get("next_cursor", "")
    has_more = result.get("has_more", False)

    if next_cursor:
        # Сохраняем полный cursor в кэш
        cursor_key = _store_cursor(f"{ig_username}_{next_cursor[:40]}", next_cursor)
    else:
        cursor_key = ""

    footer = f"✅ Показано {sent_posts} постов @{ig_username}"
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
        await callback.answer(f"🔕 Отписался от {labels.get(sub_type, sub_type)} @{ig_username}", show_alert=False)
    else:
        ig_subscribe(uid, ig_username, sub_type)
        labels = {"stories": "истории", "posts": "посты", "avatar": "аватарку"}
        await callback.answer(f"🔔 Подписался на {labels.get(sub_type, sub_type)} @{ig_username}!", show_alert=False)

    # Перерисовываем клавиатуру профиля
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

    # Группируем по username
    grouped: dict[str, list[str]] = {}
    for s in subs:
        grouped.setdefault(s["ig_username"], []).append(s["sub_type"])

    lines = ["📋 *Мои Instagram подписки:*\n"]
    type_labels = {"stories": "📸 истории", "posts": "🖼 посты", "avatar": "👤 аватарка"}
    for uname, types_list in grouped.items():
        t_str = " · ".join(type_labels.get(t, t) for t in types_list)
        lines.append(f"@{uname} → {t_str}")

    buttons = []
    for uname in grouped:
        buttons.append([InlineKeyboardButton(
            text=f"🔍 @{uname}",
            callback_data=f"ig_profile:{uname}"
        )])

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

    buttons = []
    for uname in grouped:
        buttons.append([InlineKeyboardButton(
            text=f"🔍 @{uname}",
            callback_data=f"ig_profile:{uname}"
        )])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="ig_my_subs")])

    await message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ── ФОНОВЫЙ ОПРОС ПОДПИСОК ───────────────────────────────────────────────────

async def ig_checker_task(bot: Bot):
    """
    Фоновая задача: каждые 30 минут проверяет подписки.
    Новые посты / истории / смена аватарки → шлёт уведомление.
    """
    CHECK_INTERVAL = 30 * 60  # 30 минут
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

    # Группируем подписки по ig_username, чтобы не делать лишних запросов
    by_username: dict[str, list[dict]] = {}
    for s in subs:
        by_username.setdefault(s["ig_username"], []).append(s)

    for ig_username, user_subs in by_username.items():
        try:
            await _check_user(bot, ig_username, user_subs)
        except Exception as e:
            logger.error(f"ig check {ig_username}: {e}")
        await asyncio.sleep(3)  # пауза между аккаунтами


async def _check_user(bot: Bot, ig_username: str, subs: list[dict]):
    """Проверка новых постов / историй / аватарки для одного ig_username."""
    info = await get_user_info(ig_username)
    if not info:
        return

    for sub in subs:
        user_id = sub["user_id"]
        sub_type = sub["sub_type"]
        last_seen = sub.get("last_seen", "")

        if sub_type == "avatar":
            current_avatar = info.get("avatar_url", "")
            if current_avatar and current_avatar != last_seen:
                # Новая аватарка!
                ig_update_last_seen(user_id, ig_username, "avatar", current_avatar)
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
            new_posts = [p for p in posts if p["id"] and not ig_already_sent(user_id, f"post_{p['id']}")]
            if not new_posts:
                continue

            await bot.send_message(
                user_id,
                f"🖼 *@{ig_username}* опубликовал(а) {len(new_posts)} новых постов!",
                parse_mode="Markdown"
            )
            for post in new_posts[:5]:
                media_list = post.get("media", [])
                if not media_list:
                    continue
                m = media_list[0]
                data = await _download(m.get("url") or m.get("thumb", ""))
                if not data:
                    continue
                cap = (
                    (post.get("caption", "")[:200] or "") +
                    f"\n\n❤️ {_fmt_count(post.get('like_count', 0))}  "
                    f"💬 {_fmt_count(post.get('comment_count', 0))}"
                ).strip()
                sc = post.get("shortcode", "")
                if sc:
                    cap += f"\n🔗 https://www.instagram.com/p/{sc}/"
                try:
                    if m["type"] == "video":
                        await bot.send_video(user_id, BufferedInputFile(data, filename="post.mp4"), caption=cap[:1020])
                    else:
                        await bot.send_photo(user_id, BufferedInputFile(data, filename="post.jpg"), caption=cap[:1020])
                    ig_mark_sent(user_id, f"post_{post['id']}")
                except Exception as e:
                    logger.warning(f"IG post notify {user_id}: {e}")
                await asyncio.sleep(0.5)


# ── РЕГИСТРАЦИЯ ───────────────────────────────────────────────────────────────

def register_ig_handlers(dp: Dispatcher):
    """Зарегистрировать все Instagram-хендлеры в диспетчере."""
    init_ig_db()

    dp.message.register(cmd_ig, Command(commands=["ig", "instagram", "insta"]))
    dp.message.register(cmd_ig_subs, Command("ig_subs"))
    dp.message.register(process_ig_username, IGStates.waiting_username)

    dp.callback_query.register(callback_ig_profile, F.data.startswith("ig_profile:"))
    dp.callback_query.register(callback_ig_stories, F.data.startswith("ig_stories:"))
    dp.callback_query.register(callback_ig_posts, F.data.startswith("ig_posts:"))
    dp.callback_query.register(callback_ig_sub, F.data.startswith("ig_sub:"))
    dp.callback_query.register(callback_ig_my_subs, F.data == "ig_my_subs")

    logger.info("✅ Instagram хендлеры зарегистрированы")
