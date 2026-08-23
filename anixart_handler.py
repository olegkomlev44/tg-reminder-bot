"""
anixart_handler.py — Anixart для Telegram-бота на aiogram 3.x

Улучшения v2:
  - TTL-кэш: жанры 24ч, релизы по ID 1ч (asyncio.Lock + dict)
  - Пагинация поиска: кнопка «Ещё» → callback anix:search:{query}:{offset}
  - Inline с InlineQueryResultPhoto (постер + краткое описание)
  - switch_pm_parameter="anime" для авторизации

Команды:
  /anime <запрос>   — поиск с пагинацией
  /anime random     — случайный релиз
  /anime id <id>    — инфо по ID
  /anime genres     — список жанров
Инлайн: @bot a:<запрос>
"""

import asyncio
import hashlib
import logging
import time
from typing import Optional

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, InlineQueryResultArticle, InlineQueryResultPhoto,
    InputTextMessageContent, Message,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.anixart.tv"
USER_AGENT = (
    "AnixartApp/9.0 BETA 7-25082901 "
    "(Android 9; SDK 28; x86_64; ROG ASUS AI2201_B; ru)"
)

# ── TTL-кэш ──────────────────────────────────────────────────────────────────
_cache: dict = {}          # key → (value, expire_ts)
_cache_lock = asyncio.Lock()

async def _cache_get(key: str):
    async with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry[1]:
            return entry[0]
        return None

async def _cache_set(key: str, value, ttl: int):
    async with _cache_lock:
        _cache[key] = (value, time.time() + ttl)


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _headers(token: Optional[str] = None) -> dict:
    h = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if token: h["token"] = token
    return h

async def _api_get(path: str, query: dict = None, token: str = None) -> dict:
    url = BASE_URL + path
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=query, headers=_headers(token),
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            return await r.json()

async def _api_post(path: str, body: dict = None, token: str = None) -> dict:
    url = BASE_URL + path
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body or {}, headers=_headers(token),
                          timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            return await r.json()

async def _download_poster(url: str) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": USER_AGENT},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception:
        pass
    return None

def _img_url(raw: str) -> str:
    if not raw: return ""
    if raw.startswith("http"): return raw
    if raw.startswith("//"): return "https:" + raw
    if "." not in raw: raw += ".jpg"
    return f"https://s.anixstatic.org/posters/{raw}"


# ── Форматирование ────────────────────────────────────────────────────────────
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
    genres = ", ".join(
        (g.get("name") if isinstance(g, dict) else str(g))
        for g in genres_raw[:6]
    ) or "—"
    desc = (rel.get("description") or "")[:500]
    if len(rel.get("description") or "") > 500: desc += "…"

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
    if desc: lines += ["", desc]
    lines += ["", f'🔗 <a href="https://anixart.tv/release/{rid}">Открыть на сайте</a>']
    return "\n".join(lines)

def _release_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 Открыть", url=f"https://anixart.tv/release/{rid}"),
        InlineKeyboardButton(text="🔀 Ещё случайный", callback_data="anix:random"),
    ]])

async def _send_release(ctx, rel: dict):
    rid     = rel.get("id", 0)
    caption = _fmt_release(rel)
    kb      = _release_keyboard(rid)
    chat_id = ctx.message.chat.id if isinstance(ctx, CallbackQuery) else ctx.chat.id
    bot     = ctx.bot

    poster_url = _img_url(rel.get("poster") or rel.get("image") or "")
    if poster_url:
        data = await _download_poster(poster_url)
        if data:
            try:
                fname = f"anix_{hashlib.md5(poster_url.encode()).hexdigest()[:8]}.jpg"
                await bot.send_photo(chat_id, BufferedInputFile(data, fname),
                                     caption=caption, parse_mode="HTML", reply_markup=kb)
                return
            except Exception as e:
                logger.warning(f"Не удалось отправить фото: {e}")
    await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)


# ── Кэшированные запросы ──────────────────────────────────────────────────────
async def _get_release(rid: int) -> Optional[dict]:
    key = f"release:{rid}"
    cached = await _cache_get(key)
    if cached: return cached
    try:
        data = await _api_get(f"/release/{rid}", {"extended_mode": "true"})
        rel = data.get("release")
        if rel:
            await _cache_set(key, rel, ttl=3600)  # 1 час
        return rel
    except Exception:
        return None

async def _get_genres() -> list:
    key = "genres"
    cached = await _cache_get(key)
    if cached: return cached
    try:
        data = await _api_get("/genre/list")
        genres = data.get("genres") or data.get("content") or []
        if genres:
            await _cache_set(key, genres, ttl=86400)  # 24 часа
        return genres
    except Exception:
        return []


# ── Хэндлеры команд ──────────────────────────────────────────────────────────
router = Router(name="anixart")

HELP_TEXT = (
    "<b>🎬 Anixart — поиск аниме</b>\n\n"
    "<code>/anime &lt;запрос&gt;</code> — поиск\n"
    "<code>/anime random</code> — случайный релиз\n"
    "<code>/anime id &lt;id&gt;</code> — инфо по ID\n"
    "<code>/anime genres</code> — список жанров\n\n"
    "<i>Инлайн:</i> @botname <code>a:&lt;запрос&gt;</code>"
)

@router.message(Command("anime"))
async def cmd_anime(msg: Message):
    args  = (msg.text or "").split(maxsplit=1)
    query = args[1].strip() if len(args) > 1 else ""
    if not query:
        await msg.answer(HELP_TEXT, parse_mode="HTML"); return
    parts = query.split(maxsplit=1)
    sub   = parts[0].lower()
    if sub == "random":              await _do_random(msg)
    elif sub == "id" and len(parts) > 1 and parts[1].strip().isdigit():
        await _do_info(msg, int(parts[1].strip()))
    elif sub == "genres":            await _do_genres(msg)
    else:                            await _do_search(msg, query, offset=0)


async def _do_random(ctx):
    wait = await _reply(ctx, "🎲 Подбираю случайный релиз…")
    try:
        data = await _api_get("/release/random", {"extended_mode": "true"})
        rel  = data.get("release")
        if not rel:
            await wait.edit_text("❌ Не удалось получить случайный релиз."); return
        await wait.delete()
        await _send_release(ctx, rel)
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


async def _do_info(ctx, rid: int):
    wait = await _reply(ctx, f"🔍 Загружаю релиз #{rid}…")
    rel  = await _get_release(rid)
    if not rel:
        await wait.edit_text(f"❌ Релиз <code>{rid}</code> не найден.", parse_mode="HTML"); return
    await wait.delete()
    await _send_release(ctx, rel)


async def _do_search(ctx, query: str, offset: int = 0):
    wait = await _reply(ctx, f"🔍 Ищу «{query}»…")
    try:
        # Пробуем POST, потом GET
        try:
            data = await _api_post(f"/search/releases/{offset}", {"query": query, "searchBy": 0})
        except Exception:
            data = await _api_get(f"/search/releases/{offset}", {"q": query, "query": query})

        content = data.get("releases") or data.get("content", []) or data.get("data", [])
        if not content:
            await wait.edit_text(f"😔 По запросу «{query}» ничего не найдено."); return

        results = content[:10]
        buttons = []
        for item in results:
            title  = (item.get("title_ru") or item.get("title_original") or "Без названия")[:35]
            year   = item.get("year") or "?"
            ep_rel = item.get("episodes_released", 0)
            ep_tot = item.get("episodes_total", 0) or "?"
            rid    = item.get("id", 0)
            buttons.append([InlineKeyboardButton(
                text=f"{title} ({year}, {ep_rel}/{ep_tot} эп.)",
                callback_data=f"anix:info:{rid}"
            )])

        # Кнопка «Ещё» если есть следующая страница
        total = data.get("total") or data.get("totalCount") or 0
        if total > offset + 10 or len(content) == 10:
            buttons.append([InlineKeyboardButton(
                text="⏩ Ещё результаты",
                callback_data=f"anix:search:{query}:{offset + 10}"
            )])
        buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        suffix = f" (стр. {offset//10 + 1})" if offset > 0 else ""
        await wait.edit_text(
            f"🔍 Результаты по «<b>{query}</b>»{suffix}:",
            parse_mode="HTML", reply_markup=kb
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка поиска: <code>{e}</code>", parse_mode="HTML")


async def _do_genres(ctx):
    wait   = await _reply(ctx, "🎭 Загружаю жанры…")
    genres = await _get_genres()
    if not genres:
        await wait.edit_text("❌ Не удалось загрузить жанры."); return

    buttons, row = [], []
    for g in genres[:30]:
        name = g.get("name") or str(g)
        gid  = g.get("id", 0)
        row.append(InlineKeyboardButton(text=name, callback_data=f"anix:genre:{gid}"))
        if len(row) == 3:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

    await wait.edit_text(
        "🎭 <b>Выберите жанр:</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ── Callbacks ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "anix:random")
async def cb_random(cq: CallbackQuery):
    await cq.answer("🎲 Подбираю…")
    await _do_random(cq)

@router.callback_query(F.data.startswith("anix:info:"))
async def cb_info(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer(f"📄 Загружаю #{rid}…")
    await _do_info(cq, rid)

@router.callback_query(F.data.startswith("anix:search:"))
async def cb_search_page(cq: CallbackQuery):
    # anix:search:{query}:{offset}
    parts  = cq.data.split(":", 3)
    query  = parts[2]
    offset = int(parts[3])
    await cq.answer(f"⏩ Страница {offset//10 + 1}…")
    await _do_search(cq, query, offset=offset)

@router.callback_query(F.data.startswith("anix:genre:"))
async def cb_genre(cq: CallbackQuery):
    gid  = int(cq.data.split(":")[2])
    await cq.answer("🔍 Ищу…")
    wait = await cq.message.answer("🔍 Загружаю аниме по жанру…")
    try:
        data    = await _api_get(f"/genre/{gid}/releases/0")
        content = data.get("content") or []
        if not content:
            await wait.edit_text("😔 По этому жанру ничего не найдено."); return
        buttons = []
        for item in content[:10]:
            title = (item.get("title_ru") or item.get("title_original") or "Без названия")[:40]
            year  = item.get("year") or "?"
            rid   = item.get("id", 0)
            buttons.append([InlineKeyboardButton(text=f"{title} ({year})", callback_data=f"anix:info:{rid}")])
        buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
        await wait.edit_text(f"🎭 Найдено {len(content[:10])} аниме:",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")

@router.callback_query(F.data == "anix:close")
async def cb_close(cq: CallbackQuery):
    await cq.answer()
    try: await cq.message.delete()
    except: pass


# ── Inline с фото ─────────────────────────────────────────────────────────────
async def inline_anime(iq: InlineQuery):
    """Срабатывает на запросы вида 'a:<название>'."""
    raw   = (iq.query or "").strip()
    query = raw[2:].strip() if raw.startswith("a:") else raw.strip()

    if not query or len(query) < 2:
        await iq.answer(
            [], cache_time=5,
            switch_pm_text="Введите: a:<название аниме>",
            switch_pm_parameter="anime"
        )
        return

    try:
        try:
            data = await _api_post("/search/releases/0", {"query": query, "searchBy": 0})
        except Exception:
            data = await _api_get("/search/releases/0", {"q": query, "query": query})
        content = data.get("releases") or data.get("content", [])
    except Exception:
        content = []

    results = []
    for item in content[:10]:
        rid        = item.get("id", 0)
        title_ru   = item.get("title_ru") or item.get("title_original") or "Без названия"
        title_orig = item.get("title_original") or ""
        year       = item.get("year") or "?"
        st         = item.get("status", {})
        status     = st.get("name") if isinstance(st, dict) else str(st)
        ep_rel     = item.get("episodes_released", 0)
        ep_tot     = item.get("episodes_total", 0) or "?"
        rating     = item.get("rating", 0)
        poster_raw = item.get("poster") or item.get("image") or ""
        poster_url = _img_url(poster_raw)

        text = (
            f"<b>{title_ru}</b>\n"
            + (f"<i>{title_orig}</i>\n\n" if title_orig and title_orig != title_ru else "\n")
            + f"📅 {year} | 📊 {status}\n"
            f"⭐ {rating} | 🎬 {ep_rel}/{ep_tot} эп.\n\n"
            f"🔗 https://anixart.tv/release/{rid}"
        )
        desc = f"{status} • {ep_rel}/{ep_tot} эп. • ⭐{rating}"

        if poster_url:
            results.append(InlineQueryResultPhoto(
                id=str(rid),
                photo_url=poster_url,
                thumbnail_url=poster_url,
                title=f"{title_ru} ({year})",
                description=desc,
                caption=text,
                parse_mode="HTML",
            ))
        else:
            results.append(InlineQueryResultArticle(
                id=str(rid),
                title=f"{title_ru} ({year})",
                description=desc,
                input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML"),
            ))

    await iq.answer(
        results, cache_time=60,
        switch_pm_text="Поиск: a:<название>",
        switch_pm_parameter="anime"
    )


# ── Утилиты ───────────────────────────────────────────────────────────────────
async def _reply(ctx, text: str) -> Message:
    if isinstance(ctx, CallbackQuery):
        return await ctx.message.answer(text)
    return await ctx.answer(text)


def register_anixart_handlers(dp):
    dp.include_router(router)
    logger.info("🎬 anixart_handler зарегистрирован")
