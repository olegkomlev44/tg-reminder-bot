"""
anixart_handler.py — Anixart для Telegram-бота на aiogram 3.x
Переработан из плагина для exteraGram.

Команды:
  /anime <запрос>        — поиск аниме
  /anime random          — случайный релиз
  /anime id <id>         — инфо по ID
  /anime genres          — список жанров

Инлайн-режим:
  @botname <запрос>      — быстрый поиск аниме

Подключение в main.py:
    from anixart_handler import register_anixart_handlers
    register_anixart_handlers(dp)
"""

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logger = logging.getLogger(__name__)

# ── API ──────────────────────────────────────────────────────────────────────
BASE_URL    = "https://api.anixart.tv"
BASE_URL_V2 = "https://api.anixart.tv"  # api2.anixsekai.com недоступен
USER_AGENT = (
    "AnixartApp/9.0 BETA 7-25082901 "
    "(Android 9; SDK 28; x86_64; ROG ASUS AI2201_B; ru)"
)

LIST_MAP = {
    "watching":  1,
    "planned":   2,
    "completed": 3,
    "hold":      4,
    "dropped":   5,
}
LIST_NAMES = {
    "favorites": "Избранное",
    "watching":  "Смотрю",
    "planned":   "В планах",
    "completed": "Просмотрено",
    "hold":      "Отложено",
    "dropped":   "Брошено",
}

# ── HTTP-клиент ───────────────────────────────────────────────────────────────
def _headers(token: Optional[str] = None) -> dict:
    h = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    if token:
        h["token"] = token
    return h


async def _api_get(path: str, query: dict = None, token: str = None) -> dict:
    url = BASE_URL + path
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=query, headers=_headers(token), timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            return await r.json()


async def _api_post(path: str, body: dict = None, v2: bool = False, token: str = None) -> dict:
    base = BASE_URL_V2 if v2 else BASE_URL
    url  = base + path
    h = _headers(token)
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body or {}, headers=h, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            return await r.json()


async def _download_poster(url: str) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception:
        pass
    return None


def _img_url(raw: str) -> str:
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("//"):
        return "https:" + raw
    base = "https://s.anixstatic.org"
    if "." not in raw:
        raw += ".jpg"
    return f"{base}/posters/{raw}"


# ── Форматирование релиза ─────────────────────────────────────────────────────
def _fmt_release(rel: dict) -> str:
    rid        = rel.get("id", 0)
    title_ru   = rel.get("title_ru") or "Без названия"
    title_orig = rel.get("title_original") or ""
    year       = rel.get("year") or "?"
    status_obj = rel.get("status")
    status     = status_obj.get("name") if isinstance(status_obj, dict) else str(status_obj or "")
    rating     = rel.get("rating", 0)
    ep_rel     = rel.get("episodes_released", 0)
    ep_tot     = rel.get("episodes_total", 0) or "?"
    genres_raw = rel.get("genres") or []
    if isinstance(genres_raw, list):
        genres = ", ".join(
            (g.get("name") if isinstance(g, dict) else str(g))
            for g in genres_raw[:6]
        ) or "—"
    else:
        genres = str(genres_raw)

    desc_full = rel.get("description") or ""
    desc      = desc_full[:500] + ("…" if len(desc_full) > 500 else "")

    lines = [f"<b>{title_ru}</b>"]
    if title_orig and title_orig != title_ru:
        lines.append(f"<i>{title_orig}</i>")
    lines += [
        "",
        f"📅 <b>Год:</b> {year}",
        f"📊 <b>Статус:</b> {status}",
        f"⭐ <b>Рейтинг:</b> {rating}",
        f"🎬 <b>Серии:</b> {ep_rel}/{ep_tot}",
        f"🏷 <b>Жанры:</b> {genres}",
    ]
    if desc:
        lines += ["", desc]
    lines += [
        "",
        f"🔗 <a href=\"https://anixart.tv/release/{rid}\">Открыть на сайте</a>",
    ]
    return "\n".join(lines)


def _release_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 Открыть", url=f"https://anixart.tv/release/{rid}"),
        InlineKeyboardButton(text="🔀 Ещё случайный", callback_data="anix:random"),
    ]])


# ── Отправка релиза ──────────────────────────────────────────────────────────
async def _send_release(msg_or_cq, rel: dict):
    """Отправляет карточку релиза с постером в чат (Message или CallbackQuery)."""
    rid     = rel.get("id", 0)
    caption = _fmt_release(rel)
    kb      = _release_keyboard(rid)

    # Определяем объект Message
    if isinstance(msg_or_cq, CallbackQuery):
        chat_id = msg_or_cq.message.chat.id
        bot     = msg_or_cq.bot
    else:
        chat_id = msg_or_cq.chat.id
        bot     = msg_or_cq.bot

    poster_raw = rel.get("poster") or rel.get("image")
    poster_url = _img_url(poster_raw) if poster_raw else None

    if poster_url:
        data = await _download_poster(poster_url)
        if data:
            try:
                fname = f"anixart_{hashlib.md5(poster_url.encode()).hexdigest()[:8]}.jpg"
                await bot.send_photo(
                    chat_id,
                    BufferedInputFile(data, filename=fname),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                return
            except Exception as e:
                logger.warning(f"Не удалось отправить фото: {e}")

    await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False)


# ── Роутер ────────────────────────────────────────────────────────────────────
router = Router(name="anixart")

HELP_TEXT = (
    "<b>🎬 Anixart — поиск аниме</b>\n\n"
    "<code>/anime &lt;запрос&gt;</code> — поиск\n"
    "<code>/anime random</code> — случайный релиз\n"
    "<code>/anime id &lt;id&gt;</code> — инфо по ID\n"
    "<code>/anime genres</code> — список жанров\n\n"
    "<i>Инлайн-режим:</i> @botname <code>&lt;запрос&gt;</code>"
)


# /anime
@router.message(Command("anime"))
async def cmd_anime(msg: Message):
    args = (msg.text or "").split(maxsplit=1)
    query = args[1].strip() if len(args) > 1 else ""

    if not query:
        await msg.answer(HELP_TEXT, parse_mode="HTML")
        return

    parts = query.split(maxsplit=1)
    sub   = parts[0].lower()

    if sub == "random":
        await _do_random(msg)

    elif sub == "id" and len(parts) > 1 and parts[1].strip().isdigit():
        await _do_info(msg, int(parts[1].strip()))

    elif sub == "genres":
        await _do_genres(msg)

    else:
        await _do_search(msg, query)


async def _do_random(ctx):
    wait = await _reply(ctx, "🎲 Подбираю случайный релиз…")
    try:
        data = await _api_get("/release/random", {"extended_mode": "true"})
        rel  = data.get("release")
        if not rel:
            await wait.edit_text("❌ Не удалось получить случайный релиз.")
            return
        await wait.delete()
        await _send_release(ctx, rel)
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


async def _do_info(ctx, rid: int):
    wait = await _reply(ctx, f"🔍 Загружаю релиз #{rid}…")
    try:
        data = await _api_get(f"/release/{rid}", {"extended_mode": "true"})
        rel  = data.get("release")
        if not rel:
            await wait.edit_text(f"❌ Релиз <code>{rid}</code> не найден.", parse_mode="HTML")
            return
        await wait.delete()
        await _send_release(ctx, rel)
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


async def _do_search(ctx, query: str):
    wait = await _reply(ctx, f"🔍 Ищу «{query}»…")
    try:
        # Пробуем POST поиск
        try:
            data = await _api_post("/search/releases/0", {"query": query, "searchBy": 0})
        except Exception:
            # Fallback: GET поиск
            data = await _api_get("/search/releases/0", {"q": query, "query": query})
        content = data.get("releases") or data.get("content", []) or data.get("data", [])
        if not content:
            await wait.edit_text(f"😔 По запросу «{query}» ничего не найдено.")
            return

        results = content[:10]
        buttons = []
        for item in results:
            title   = item.get("title_ru") or item.get("title_original") or "Без названия"
            year    = item.get("year") or "?"
            st      = item.get("status", {})
            status  = st.get("name") if isinstance(st, dict) else str(st)
            ep_rel  = item.get("episodes_released", 0)
            ep_tot  = item.get("episodes_total", 0) or "?"
            rid     = item.get("id", 0)
            label   = f"{title[:35]} ({year}, {ep_rel}/{ep_tot} эп.)"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"anix:info:{rid}")])

        buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await wait.edit_text(
            f"🔍 Результаты по «<b>{query}</b>» ({len(results)} из {len(content)}):",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка поиска: <code>{e}</code>", parse_mode="HTML")


async def _do_genres(ctx):
    wait = await _reply(ctx, "🎭 Загружаю жанры…")
    try:
        data   = await _api_get("/genre/list")
        genres = data.get("genres") or data.get("content") or []
        if not genres:
            await wait.edit_text("❌ Не удалось загрузить жанры.")
            return

        buttons = []
        row = []
        for g in genres[:30]:
            name = g.get("name") or str(g)
            gid  = g.get("id", 0)
            row.append(InlineKeyboardButton(text=name, callback_data=f"anix:genre:{gid}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

        await wait.edit_text(
            "🎭 <b>Выберите жанр:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


# ── Callback-хэндлеры ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "anix:random")
async def cb_random(cq: CallbackQuery):
    await cq.answer("🎲 Подбираю…")
    await _do_random(cq)


@router.callback_query(F.data.startswith("anix:info:"))
async def cb_info(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer(f"📄 Загружаю #{rid}…")
    await _do_info(cq, rid)


@router.callback_query(F.data.startswith("anix:genre:"))
async def cb_genre(cq: CallbackQuery):
    gid  = int(cq.data.split(":")[2])
    await cq.answer("🔍 Ищу…")
    wait = await cq.message.answer("🔍 Загружаю аниме по жанру…")
    try:
        data    = await _api_get(f"/genre/{gid}/releases/0")
        content = data.get("content") or []
        if not content:
            await wait.edit_text("😔 По этому жанру ничего не найдено.")
            return

        results = content[:10]
        buttons = []
        for item in results:
            title = item.get("title_ru") or item.get("title_original") or "Без названия"
            year  = item.get("year") or "?"
            rid   = item.get("id", 0)
            buttons.append([InlineKeyboardButton(text=f"{title[:40]} ({year})", callback_data=f"anix:info:{rid}")])

        buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
        await wait.edit_text(
            f"🎭 Найдено {len(results)} аниме:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


@router.callback_query(F.data == "anix:close")
async def cb_close(cq: CallbackQuery):
    await cq.answer()
    try:
        await cq.message.delete()
    except Exception:
        pass



# ── Вспомогалки ──────────────────────────────────────────────────────────────
async def _reply(ctx, text: str) -> Message:
    """Отправляет ответ независимо от типа контекста."""
    if isinstance(ctx, CallbackQuery):
        return await ctx.message.answer(text)
    return await ctx.answer(text)


def register_anixart_handlers(dp: Dispatcher):
    dp.include_router(router)
    logger.info("🎬 anixart_handler зарегистрирован")
