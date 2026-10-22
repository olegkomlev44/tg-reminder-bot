"""
film_handler.py — фильмы и сериалы через TMDB.

  /film Интерстеллар        — карточка: постер, рейтинг, жанры, режиссёр, актёры, где смотреть, трейлер
  /film                     — в тренде за неделю
  /film random              — случайный популярный фильм
  /film ai мрачный детектив — ИИ-подборка (Gemini → поиск в TMDB)
  ИИ-чат тоже умеет: «найди фильм Дюна», «что сейчас в тренде», «посоветуй сериал про …»

Переменные окружения (BotHost → Переменные):
  TMDB_API_KEY  — ключ v3 (32 символа) ИЛИ токен v4 (длинный, начинается с eyJ) — любой из двух
  TMDB_TOKEN    — (необязательно) токен v4 отдельно
  TMDB_REGION   — страна для «где смотреть», по умолчанию RU
  TMDB_API_BASE — (необязательно) свой прокси/зеркало API, если api.themoviedb.org недоступен

Подключение: register_film_handlers(dp) в main.py.
"""

import asyncio
import html
import json
import logging
import os
import random
import re
import secrets
import time
import urllib.parse as up
from collections import OrderedDict

import aiohttp
from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

_RAW_KEY = os.getenv("TMDB_API_KEY", "").strip()
_RAW_TOKEN = os.getenv("TMDB_TOKEN", "").strip()
TMDB_TOKEN = _RAW_TOKEN or (_RAW_KEY if _RAW_KEY.startswith("eyJ") else "")
TMDB_KEY = "" if _RAW_KEY.startswith("eyJ") else _RAW_KEY
REGION = os.getenv("TMDB_REGION", "RU").upper()
BASES = [b for b in [os.getenv("TMDB_API_BASE", "").rstrip("/"),
                     "https://api.themoviedb.org/3", "https://api.tmdb.org/3"] if b]
IMG = "https://image.tmdb.org/t/p/w500"
LANG = "ru-RU"

_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 600
_SEARCHES: "OrderedDict[str, dict]" = OrderedDict()   # token → {query, results}


def tmdb_configured() -> bool:
    return bool(TMDB_KEY or TMDB_TOKEN)


async def _tmdb(path: str, **params) -> dict | None:
    """GET к TMDB с кэшем и перебором зеркал. None — ошибка/не найдено."""
    if not tmdb_configured():
        return None
    ck = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    headers = {"Accept": "application/json"}
    q = dict(params)
    if TMDB_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_TOKEN}"
    else:
        q["api_key"] = TMDB_KEY
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(headers=headers) as s:
        for base in BASES:
            try:
                async with s.get(base + path, params=q, timeout=timeout) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        _CACHE[ck] = (time.time(), data)
                        if len(_CACHE) > 400:
                            _CACHE.pop(next(iter(_CACHE)))
                        return data
                    if r.status in (401, 403):
                        logger.error("TMDB: ключ отклонён (HTTP %s)", r.status)
                        return None
                    if r.status == 404:
                        return None
                    logger.info("TMDB %s → HTTP %s", base, r.status)
            except Exception as e:
                logger.info("TMDB %s: %s: %s", base, type(e).__name__, e)
    return None


# ── Поиск ─────────────────────────────────────────────────────────────────────
def _year(item: dict) -> str:
    d = item.get("release_date") or item.get("first_air_date") or ""
    return d[:4]


def _title(item: dict) -> str:
    return item.get("title") or item.get("name") or item.get("original_title") or item.get("original_name") or "?"


def _mt(item: dict) -> str:
    """'m' для фильма, 't' для сериала."""
    mt = item.get("media_type")
    if mt == "tv" or (mt is None and ("first_air_date" in item or "name" in item) and "title" not in item):
        return "t"
    return "m"


async def search_titles(query: str) -> list[dict]:
    q = query.strip()
    year = None
    m = re.search(r"[\s(]((?:19|20)\d{2})\)?\s*$", q)
    if m:
        year, q = m.group(1), q[:m.start()].strip()
    data = await _tmdb("/search/multi", query=q, language=LANG, include_adult="false")
    res = [r for r in (data or {}).get("results", []) if r.get("media_type") in ("movie", "tv")]
    if year:
        filtered = [r for r in res if _year(r) == year]
        res = filtered or res
    return res[:8]


async def get_details(mt: str, tid: int) -> dict | None:
    kind = "movie" if mt == "m" else "tv"
    return await _tmdb(
        f"/{kind}/{tid}", language=LANG,
        append_to_response="credits,videos,watch/providers,external_ids",
        include_video_language="ru,en,null")


# ── Форматирование ────────────────────────────────────────────────────────────
def _runtime(d: dict, mt: str) -> str:
    mins = d.get("runtime") if mt == "m" else (d.get("episode_run_time") or [None])[0]
    if not mins:
        return ""
    h, m = divmod(int(mins), 60)
    return (f"{h} ч {m:02d} мин" if h else f"{m} мин") + ("" if mt == "m" else "/серия")


def _money(n: int) -> str:
    return f"${n:,}".replace(",", " ")


def pick_trailer(d: dict) -> str | None:
    vids = ((d.get("videos") or {}).get("results")) or []
    yt = [v for v in vids if v.get("site") == "YouTube" and v.get("key")]
    if not yt:
        return None

    def score(v):
        return ((v.get("type") == "Trailer") * 4 + (v.get("type") == "Teaser") * 2
                + bool(v.get("official")) * 2 + (v.get("iso_639_1") == "ru"))
    best = max(yt, key=score)
    return f"https://www.youtube.com/watch?v={best['key']}"


def card_text(d: dict, mt: str, limit: int = 1000) -> str:
    e = html.escape
    title, orig = _title(d), d.get("original_title") or d.get("original_name") or ""
    yr = _year(d)
    head = f"{'🎬' if mt == 'm' else '📺'} <b>{e(title)}</b>" + (f" ({yr})" if yr else "")
    lines = [head]
    if orig and orig != title:
        lines.append(f"<i>{e(orig)}</i>")
    rating = d.get("vote_average") or 0
    votes = d.get("vote_count") or 0
    meta = []
    if votes:
        meta.append(f"⭐ <b>{rating:.1f}</b>/10 <i>({votes:,} гол.)</i>".replace(",", " "))
    rt = _runtime(d, mt)
    if rt:
        meta.append(f"🕐 {rt}")
    if meta:
        lines.append(" · ".join(meta))
    genres = ", ".join(g["name"] for g in d.get("genres", [])[:4])
    if genres:
        lines.append(f"🎭 {e(genres)}")
    if mt == "t":
        seas, eps = d.get("number_of_seasons"), d.get("number_of_episodes")
        status = {"Returning Series": "выходит", "Ended": "завершён", "Canceled": "закрыт",
                  "In Production": "в производстве"}.get(d.get("status", ""), d.get("status", ""))
        bits = [x for x in (f"{seas} сез." if seas else "", f"{eps} сер." if eps else "", status) if x]
        if bits:
            lines.append("📚 " + ", ".join(bits))
        nxt = d.get("next_episode_to_air") or {}
        if nxt.get("air_date"):
            lines.append(f"🗓 Следующая серия: {nxt['air_date']}")
    countries = ", ".join(c.get("name", "") for c in d.get("production_countries", [])[:2])
    if countries:
        lines.append(f"🌍 {e(countries)}")
    credits = d.get("credits") or {}
    if mt == "m":
        directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"][:2]
        if directors:
            lines.append("🎥 " + e(", ".join(directors)))
    else:
        creators = [c["name"] for c in d.get("created_by", [])][:2]
        if creators:
            lines.append("🎥 " + e(", ".join(creators)))
    cast = [c["name"] for c in credits.get("cast", [])[:4]]
    if cast:
        lines.append("👥 " + e(", ".join(cast)))
    if mt == "m" and d.get("budget") and d.get("revenue"):
        lines.append(f"💰 {_money(d['budget'])} → {_money(d['revenue'])}")
    if d.get("tagline"):
        lines.append(f"\n<i>«{e(d['tagline'])}»</i>")
    head_txt = "\n".join(lines)
    overview = (d.get("overview") or "").strip()
    if overview:
        room = limit - len(head_txt) - 4
        if room > 80:
            ov = overview if len(overview) <= room else overview[:room].rsplit(" ", 1)[0] + "…"
            head_txt += f"\n\n{e(ov)}"
    return head_txt


def providers_text(d: dict, mt: str) -> str:
    e = html.escape
    allp = (d.get("watch/providers") or {}).get("results") or {}
    title = _title(d)
    names = {"flatrate": "📺 По подписке", "free": "🆓 Бесплатно", "ads": "🆓 Бесплатно (с рекламой)",
             "rent": "🎟 Аренда", "buy": "🛒 Покупка"}

    def block(region: str) -> list[str]:
        r = allp.get(region) or {}
        out = []
        for key, label in names.items():
            items = [p["provider_name"] for p in r.get(key, [])][:6]
            if items:
                out.append(f"{label}: {e(', '.join(items))}")
        return out

    lines = [f"📺 <b>Где смотреть: {e(title)}</b>"]
    here = block(REGION)
    if here:
        lines.append(f"\n🌍 <b>{REGION}</b>")
        lines += here
    else:
        lines.append(f"\nВ регионе {REGION} TMDB данных не имеет (для РФ они часто пустые).")
        for reg in ("US", "GB", "DE", "FR", "KZ", "TR"):
            b = block(reg)
            if b:
                lines.append(f"\n🌍 <b>{reg}</b> (для справки)")
                lines += b[:3]
                break
    lines.append("\n<i>Данные JustWatch через TMDB. Российские онлайн-кинотеатры проверь по кнопкам ниже.</i>")
    return "\n".join(lines)


def _kp_url(d: dict) -> str:
    return "https://www.kinopoisk.ru/index.php?kp_query=" + up.quote(_title(d))


def card_kb(d: dict, mt: str, tid: int, search_tok: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📺 Где смотреть", callback_data=f"film:w:{mt}:{tid}")]]
    trailer = pick_trailer(d)
    if trailer:
        rows[0].append(InlineKeyboardButton(text="▶️ Трейлер", url=trailer))
        rows.append([InlineKeyboardButton(text="⬇️ Скинуть трейлер видео", callback_data=f"film:d:{mt}:{tid}")])
    links = [InlineKeyboardButton(text="🔍 Кинопоиск", url=_kp_url(d))]
    imdb = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")
    if imdb:
        links.insert(0, InlineKeyboardButton(text="IMDb", url=f"https://www.imdb.com/title/{imdb}/"))
    rows.append(links)
    tail = [InlineKeyboardButton(text="🎞 Похожие", callback_data=f"film:r:{mt}:{tid}")]
    if search_tok and search_tok in _SEARCHES:
        tail.append(InlineKeyboardButton(text="🔎 Другие варианты", callback_data=f"film:s:{search_tok}"))
    rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def list_kb(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for it in items[:8]:
        mt = _mt(it)
        rate = it.get("vote_average") or 0
        label = f"{'🎬' if mt == 'm' else '📺'} {_title(it)[:38]}" + (f" ({_year(it)})" if _year(it) else "")
        if rate:
            label += f" ★{rate:.1f}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"film:c:{mt}:{it['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Отправка карточки ─────────────────────────────────────────────────────────
async def send_card(target: types.Message, mt: str, tid: int, search_tok: str | None = None) -> bool:
    d = await get_details(mt, tid)
    if not d:
        await target.answer("❌ Не удалось получить данные TMDB.")
        return False
    kb = card_kb(d, mt, tid, search_tok)
    text = card_text(d, mt)
    poster = d.get("poster_path")
    if poster:
        try:
            await target.answer_photo(IMG + poster, caption=text, parse_mode="HTML", reply_markup=kb)
            return True
        except Exception as e:
            logger.info("film: постер по URL не ушёл (%s), пробую байтами", e)
            try:
                from aiogram.types import BufferedInputFile
                async with aiohttp.ClientSession() as s:
                    async with s.get(IMG + poster, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            await target.answer_photo(BufferedInputFile(await r.read(), "poster.jpg"),
                                                      caption=text, parse_mode="HTML", reply_markup=kb)
                            return True
            except Exception as e2:
                logger.info("film: постер байтами тоже не вышел: %s", e2)
    await target.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    return True


async def _edit_any(msg: types.Message, text: str, kb: InlineKeyboardMarkup):
    try:
        if msg.photo:
            await msg.edit_caption(caption=text[:1024], parse_mode="HTML", reply_markup=kb)
        else:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        logger.debug("film edit: %s", e)


def _no_key_text() -> str:
    return ("🎬 Поиск фильмов не настроен: добавь переменную <code>TMDB_API_KEY</code> на BotHost "
            "(бесплатный ключ: themoviedb.org → Settings → API).")


# ── Сценарии ──────────────────────────────────────────────────────────────────
async def film_search_flow(message: types.Message, query: str) -> bool:
    """Поиск по названию: карточка лучшего совпадения + кнопка «другие варианты»."""
    if not tmdb_configured():
        await message.answer(_no_key_text(), parse_mode="HTML")
        return False
    results = await search_titles(query)
    if not results:
        await message.answer(f"🤷 По запросу «{html.escape(query)}» ничего не нашлось в TMDB.", parse_mode="HTML")
        return False
    tok = None
    if len(results) > 1:
        tok = secrets.token_hex(4)
        _SEARCHES[tok] = {"query": query, "results": results}
        while len(_SEARCHES) > 100:
            _SEARCHES.popitem(last=False)
    top = results[0]
    return await send_card(message, _mt(top), top["id"], tok)


async def show_trending(message: types.Message) -> bool:
    if not tmdb_configured():
        await message.answer(_no_key_text(), parse_mode="HTML")
        return False
    data = await _tmdb("/trending/all/week", language=LANG)
    items = [r for r in (data or {}).get("results", []) if r.get("media_type") in ("movie", "tv")][:8]
    if not items:
        await message.answer("❌ TMDB не отдал тренды.")
        return False
    await message.answer("🔥 <b>В тренде за неделю</b> — жми, чтобы открыть карточку:",
                         parse_mode="HTML", reply_markup=list_kb(items))
    return True


async def show_random(message: types.Message) -> bool:
    if not tmdb_configured():
        await message.answer(_no_key_text(), parse_mode="HTML")
        return False
    page = random.randint(1, 25)
    data = await _tmdb("/discover/movie", language=LANG, sort_by="popularity.desc",
                       **{"vote_count.gte": 1500, "vote_average.gte": 6.5}, page=page)
    items = (data or {}).get("results") or []
    if not items:
        await message.answer("❌ TMDB не отдал подборку.")
        return False
    pick = random.choice(items)
    return await send_card(message, "m", pick["id"])


async def ai_recommend(message: types.Message, vibe: str) -> bool:
    """Gemini предлагает тайтлы → ищем каждый в TMDB → список кнопок."""
    from ai_handlers import get_gemini
    if not tmdb_configured():
        await message.answer(_no_key_text(), parse_mode="HTML")
        return False
    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return False
    status = await message.answer("🧠 Подбираю…")
    prompt = (
        f"Посоветуй 5 фильмов или сериалов под запрос: «{vibe}». Только реально существующие, разнообразные, "
        'известные. Ответ — ТОЛЬКО JSON-массив: [{"title":"название на русском или оригинальное","year":2010}]'
    )
    try:
        from google.genai import types as gt
        resp = await client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
            config=gt.GenerateContentConfig(response_mime_type="application/json"))
        raw = (resp.text or "").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        picks = json.loads(raw)
        picks = picks if isinstance(picks, list) else []
    except Exception as e:
        logger.warning("film ai: %s", e)
        await status.edit_text("❌ ИИ не смог подобрать, попробуй переформулировать.")
        return False
    found: list[dict] = []
    seen: set[int] = set()
    results = await asyncio.gather(*[search_titles(f"{p.get('title', '')} {p.get('year', '')}".strip())
                                     for p in picks[:5] if isinstance(p, dict)])
    for res in results:
        if res and res[0]["id"] not in seen:
            seen.add(res[0]["id"])
            found.append(res[0])
    if not found:
        await status.edit_text("🤷 Не нашёл подходящего в TMDB.")
        return False
    await status.edit_text(f"🧠 <b>Подборка:</b> {html.escape(vibe[:80])}", parse_mode="HTML",
                           reply_markup=list_kb(found))
    return True


# ── Команда и колбэки ─────────────────────────────────────────────────────────
async def cmd_film(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    low = arg.lower()
    if not arg or low in ("trending", "тренд", "тренды", "top"):
        await show_trending(message)
    elif low in ("random", "рандом", "случайный"):
        await show_random(message)
    elif low.startswith(("ai ", "ии ")):
        await ai_recommend(message, arg.split(maxsplit=1)[1])
    else:
        await film_search_flow(message, arg)


async def callback_film(callback: types.CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    action = parts[1]

    if action == "s":                                   # другие варианты поиска
        sr = _SEARCHES.get(parts[2])
        if not sr:
            await callback.answer("Поиск устарел — повтори /film", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(f"🔎 <b>Результаты:</b> {html.escape(sr['query'][:60])}",
                                      parse_mode="HTML", reply_markup=list_kb(sr["results"]))
        return

    if len(parts) < 4 or not parts[3].isdigit():
        await callback.answer()
        return
    mt, tid = parts[2], int(parts[3])

    if action == "c":                                   # открыть карточку
        await callback.answer()
        await send_card(callback.message, mt, tid)
        return
    d = await get_details(mt, tid)
    if not d:
        await callback.answer("TMDB не ответил, попробуй позже", show_alert=True)
        return
    if action == "w":                                   # где смотреть
        back = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Кинопоиск", url=_kp_url(d))],
            [InlineKeyboardButton(text="⬅️ Назад к карточке", callback_data=f"film:b:{mt}:{tid}")]])
        link = (((d.get("watch/providers") or {}).get("results") or {}).get(REGION) or {}).get("link")
        if link:
            back.inline_keyboard.insert(0, [InlineKeyboardButton(text="🌐 JustWatch", url=link)])
        await _edit_any(callback.message, providers_text(d, mt), back)
        await callback.answer()
    elif action == "b":                                 # назад к карточке
        await _edit_any(callback.message, card_text(d, mt), card_kb(d, mt, tid))
        await callback.answer()
    elif action == "d":                                 # скачать трейлер как видео
        url = pick_trailer(d)
        if not url:
            await callback.answer("Трейлера нет", show_alert=True)
            return
        await callback.answer("Качаю трейлер…")
        from media_downloader import handle_media_link
        await handle_media_link(callback.message, custom_url=url, custom_platform="youtube")
    elif action == "r":                                 # похожие
        kind = "movie" if mt == "m" else "tv"
        data = await _tmdb(f"/{kind}/{tid}/recommendations", language=LANG)
        items = []
        for r in (data or {}).get("results", [])[:8]:
            r["media_type"] = kind
            items.append(r)
        if not items:
            await callback.answer("Похожих не нашлось", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(f"🎞 <b>Похожие на «{html.escape(_title(d))}»:</b>",
                                      parse_mode="HTML", reply_markup=list_kb(items))
    else:
        await callback.answer()


def register_film_handlers(dp):
    dp.message.register(cmd_film, Command("film", "movie", "kino"))
    dp.callback_query.register(callback_film, F.data.startswith("film:"))
    logger.info("🎬 film_handler зарегистрирован (TMDB %s)", "настроен" if tmdb_configured() else "БЕЗ КЛЮЧА")
