"""
random18_handler.py — /random18: случайное видео из выдачи Webmasters API.

Что делает:
  • берёт случайную страницу поиска, выбирает видео и шлёт превью (под спойлером)
    с названием, длительностью, рейтингом и кнопкой «Смотреть» → ссылка на сайт;
  • сам файл НЕ скачивает и не перезаливает — API его не отдаёт;
  • требует однократного подтверждения 18+ (хранится в JSON-файле);
  • работает в личке; в группах — только в чатах из R18_CHAT_IDS;
  • кулдаун на пользователя + общий лимит запросов к API + кэш выдачи.

Переменные окружения (всё необязательно):
  R18_CHAT_IDS   — id групп через запятую, где команда разрешена (по умолчанию пусто)
  R18_ADMIN_IDS  — id админов, для которых кулдаун не действует
  R18_COOLDOWN   — секунды между запросами одного пользователя (по умолчанию 8)
  R18_DB         — путь к JSON с подтверждениями 18+ (по умолчанию r18_ok.json рядом с кодом)

Подключение — в main.py: from random18_handler import register_random18_handlers
и вызвать register_random18_handlers(dp) рядом с register_film_handlers(dp).
Зависимости: только aiohttp (уже есть в requirements.txt).
"""

import asyncio
import html
import json
import logging
import os
import random
import time
from collections import deque

import aiohttp
from aiogram import F, Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BASE_DIR

logger = logging.getLogger(__name__)

API_URL = "https://www.pornhub.com/webmasters/search"
USER_AGENT = "Mozilla/5.0 (compatible; TelegramBot/1.0)"
ORDERINGS = ("featured", "newest", "mostviewed", "rating")
MAX_PAGE = 30            # из скольких первых страниц выдачи берём случайную
CACHE_TTL = 600          # сек, кэш одной страницы выдачи
API_MIN_INTERVAL = 1.0   # сек между запросами к API (общий лимит)
REQUEST_TIMEOUT = 12


def _ids(name: str) -> set[int]:
    out = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


ALLOWED_CHATS = _ids("R18_CHAT_IDS")
ADMINS = _ids("R18_ADMIN_IDS")
COOLDOWN = float(os.getenv("R18_COOLDOWN", "8"))
DB_PATH = os.getenv("R18_DB", os.path.join(BASE_DIR, "r18_ok.json"))

# ── Подтверждение 18+ ─────────────────────────────────────────────────────────
_confirmed: set[int] = set()


def _load_confirmed() -> None:
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            _confirmed.update(int(x) for x in json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"r18: не удалось прочитать {DB_PATH}: {e}")


def _save_confirmed() -> None:
    try:
        tmp = DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(_confirmed), f)
        os.replace(tmp, DB_PATH)
    except Exception as e:
        logger.warning(f"r18: не удалось сохранить {DB_PATH}: {e}")


_load_confirmed()

# ── Состояние ─────────────────────────────────────────────────────────────────
_last_call: dict[int, float] = {}
_last_query: dict[int, str] = {}
_seen: dict[int, deque] = {}
_cache: dict[tuple, tuple[float, list]] = {}
_api_lock = asyncio.Lock()
_api_last = 0.0
_session: aiohttp.ClientSession | None = None


class ApiError(Exception):
    pass


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    return _session


async def close_random18_session() -> None:
    """Можно вызвать при остановке бота (по аналогии с close_anixart_session)."""
    if _session and not _session.closed:
        await _session.close()


async def _search(q: str, page: int, ordering: str) -> list:
    """Одна страница выдачи Webmasters API (с кэшем и общим лимитом)."""
    global _api_last
    key = (q, page, ordering)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    async with _api_lock:
        wait = API_MIN_INTERVAL - (time.time() - _api_last)
        if wait > 0:
            await asyncio.sleep(wait)
        session = await _get_session()
        params = {"search": q, "page": page, "thumbsize": "large", "ordering": ordering}
        try:
            async with session.get(
                API_URL, params=params, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as r:
                _api_last = time.time()
                if r.status != 200:
                    raise ApiError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except asyncio.TimeoutError:
            raise ApiError("timeout")
        except aiohttp.ClientError as e:
            raise ApiError(str(e))

    videos = data.get("videos") if isinstance(data, dict) else None
    videos = videos if isinstance(videos, list) else []
    _cache[key] = (time.time(), videos)
    if len(_cache) > 200:
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:50]:
            _cache.pop(k, None)
    return videos


def _thumb(v: dict) -> str | None:
    t = v.get("default_thumb") or v.get("thumb")
    if not t:
        thumbs = v.get("thumbs") or []
        if thumbs and isinstance(thumbs[0], dict):
            t = thumbs[0].get("src")
    return t if isinstance(t, str) and t.startswith("http") else None


async def _pick_video(user_id: int, q: str) -> dict | None:
    seen = _seen.setdefault(user_id, deque(maxlen=60))
    ordering = random.choice(ORDERINGS)
    pages = [random.randint(1, MAX_PAGE) for _ in range(2)] + [random.randint(1, 5), 1]
    for page in pages:
        videos = await _search(q, page, ordering)
        fresh = [v for v in videos if v.get("video_id") not in seen and v.get("url")]
        if fresh:
            v = random.choice(fresh)
            seen.append(v.get("video_id"))
            return v
    return None


# ── Хэндлеры ──────────────────────────────────────────────────────────────────
def _allowed_here(chat: types.Chat) -> bool:
    return chat.type == "private" or chat.id in ALLOWED_CHATS


def _confirm_kb() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Мне есть 18", callback_data="r18:ok")
    kb.button(text="❌ Нет", callback_data="r18:no")
    kb.adjust(2)
    return kb.as_markup()


def _video_kb(url: str) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Смотреть", url=url)
    kb.button(text="🎲 Ещё", callback_data="r18:next")
    kb.adjust(2)
    return kb.as_markup()


async def _send_random(bot: Bot, chat_id: int, user_id: int, q: str) -> str | None:
    """Отправляет видео в чат. Возвращает текст ошибки или None."""
    try:
        v = await _pick_video(user_id, q)
    except ApiError as e:
        logger.warning(f"r18: API error: {e}")
        return "API сейчас недоступно (возможно, блокирует IP хостинга). Попробуй позже."
    if not v:
        return "Ничего не нашлось. Попробуй другой запрос (лучше на английском)."

    title = html.escape(str(v.get("title") or "Без названия"))
    parts = []
    if v.get("duration"):
        parts.append(f"⏱ {html.escape(str(v['duration']))}")
    if v.get("rating"):
        parts.append(f"⭐ {html.escape(str(v['rating']))}")
    if v.get("views"):
        parts.append(f"👁 {html.escape(str(v['views']))}")
    caption = f"🔞 <b>{title}</b>\n" + " · ".join(parts)
    kb = _video_kb(v["url"])

    thumb = _thumb(v)
    if thumb:
        try:
            await bot.send_photo(
                chat_id, thumb, caption=caption, reply_markup=kb,
                parse_mode="HTML", has_spoiler=True,
            )
            return None
        except TelegramBadRequest as e:
            logger.info(f"r18: превью не отправилось ({e}), шлю текстом")
    await bot.send_message(
        chat_id, caption, reply_markup=kb, parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return None


def _cooldown_left(user_id: int) -> float:
    if user_id in ADMINS:
        return 0.0
    return max(0.0, COOLDOWN - (time.time() - _last_call.get(user_id, 0.0)))


async def cmd_random18(message: types.Message, command: CommandObject) -> None:
    if not _allowed_here(message.chat):
        await message.reply("Эта команда работает только в личке с ботом.")
        return
    uid = message.from_user.id
    q = (command.args or "").strip()[:60]
    _last_query[uid] = q

    if uid not in _confirmed:
        await message.reply(
            "🔞 Контент для взрослых. Подтверди, что тебе есть 18 лет:",
            reply_markup=_confirm_kb(),
        )
        return

    left = _cooldown_left(uid)
    if left > 0:
        await message.reply(f"Подожди ещё {left:.0f} с.")
        return
    _last_call[uid] = time.time()

    err = await _send_random(message.bot, message.chat.id, uid, q)
    if err:
        await message.reply(err)


async def callback_r18(cb: types.CallbackQuery) -> None:
    action = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    msg = cb.message

    if not msg or not _allowed_here(msg.chat):
        await cb.answer("Недоступно в этом чате.", show_alert=True)
        return

    if action == "no":
        await cb.answer()
        await msg.edit_text("Ок, отмена.")
        return

    if action == "ok":
        _confirmed.add(uid)
        _save_confirmed()
        await cb.answer("Подтверждено")
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
    elif uid not in _confirmed:
        await cb.answer("Сначала подтверди 18+ через /random18", show_alert=True)
        return

    left = _cooldown_left(uid)
    if left > 0:
        await cb.answer(f"Подожди ещё {left:.0f} с.", show_alert=True)
        return
    _last_call[uid] = time.time()
    await cb.answer()

    err = await _send_random(cb.bot, msg.chat.id, uid, _last_query.get(uid, ""))
    if err:
        await cb.bot.send_message(msg.chat.id, err)


def register_random18_handlers(dp: Dispatcher) -> None:
    dp.message.register(cmd_random18, Command("random18"))
    dp.callback_query.register(callback_r18, F.data.startswith("r18:"))
