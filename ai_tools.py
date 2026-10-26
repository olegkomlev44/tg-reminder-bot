"""
ai_tools.py — Function Calling для ИИ бота (Gemini).

ИИ сам решает, когда вызвать функцию бота: профиль Instagram, поиск/отправка песни,
аниме, товары, скачивание видео, 18+ видео (со спойлером, авто-переводом тегов,
превью или прямой отправкой и сборкой больших роликов в альбом), наряды, дембель-таймер,
картинки, поиск в интернете.

Отключить целиком: переменная окружения AI_TOOLS=0 (на BotHost, без правки кода).
"""

import asyncio
import html
import logging
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram import types

logger = logging.getLogger(__name__)

TOOLS_ENABLED = os.getenv("AI_TOOLS", "1") != "0"
MODEL = "gemini-2.5-flash"

MAX_STEPS = 4              # максимум «раундов» модель → функции → модель
MAX_CALLS_PER_STEP = 3     # максимум функций за один раунд
RATE_LIMIT_CALLS = 6       # вызовов функций на пользователя …
RATE_LIMIT_WINDOW = 60     # … за столько секунд

_RATE: dict[int, deque] = defaultdict(lambda: deque(maxlen=RATE_LIMIT_CALLS))


@dataclass
class ToolContext:
    message: types.Message
    user_id: int
    chat_id: int


@dataclass
class Tool:
    name: str
    description: str
    properties: dict            # {"имя": {"type": "STRING", "description": "..."}}
    required: list[str]
    handler: Callable[..., Awaitable[str]]
    terminal: bool = True       # True — результат уже показан в чате, follow-up не нужен


TOOLS: dict[str, Tool] = {}


def _tool(name, description, handler, properties=None, required=None, terminal=True):
    TOOLS[name] = Tool(name, description, properties or {}, required or [], handler, terminal)


def _s(desc: str) -> dict:
    return {"type": "STRING", "description": desc}


def _clean(value, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


# ══════════════════════════════════════════════════════════════════════════════
# Функции-обёртки над существующими хэндлерами.
# ══════════════════════════════════════════════════════════════════════════════

# ── Instagram ────────────────────────────────────────────────────────────────
async def _tool_instagram_profile(ctx: ToolContext, username: str = "") -> str:
    from instagram_handlers import _clean_username, _show_profile
    user = _clean_username(_clean(username))
    if not user:
        return "ОШИБКА: не указан username Instagram. Попроси пользователя его назвать."
    ok = await _show_profile(ctx.message, user, ctx.user_id)
    if ok is False:
        return (f"ОШИБКА: профиль @{user} не удалось получить (нет такого ника или Instagram временно "
                "не отвечает). Пользователю уже показано сообщение с кнопками (повторить / поиск по имени), "
                "не дублируй его.")
    return f"Карточка профиля @{user} отправлена в чат (с кнопками сторис, постов, подписки)."


_PERSON_CACHE: dict[str, tuple[float, str]] = {}
_PERSON_TTL = 24 * 3600
_IG_RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "share", "accounts", "none", "hashtag"}


def _parse_handles(text: str) -> list[str]:
    text = text or ""
    raw: list[str] = []
    m = re.search(r"HANDLES:\s*(.+)", text, re.I)
    if m:
        raw = re.split(r"[,;\s]+", m.group(1).strip())
    if not raw:
        raw = re.findall(r"instagram\.com/([A-Za-z0-9_.]+)", text, re.I)
    out: list[str] = []
    for h in raw:
        h = h.strip().lstrip("@").rstrip("./").lower()
        h = re.sub(r"^https?://(www\.)?instagram\.com/", "", h).split("/")[0].split("?")[0]
        if (re.fullmatch(r"[a-z0-9_.]{2,30}", h) and re.search(r"[a-z]", h)
                and h not in _IG_RESERVED and h not in out):
            out.append(h)
    return out[:3]


async def _resolve_instagram_handles(name: str) -> list[str]:
    from ai_handlers import get_gemini
    from google.genai import types as gt
    client = get_gemini()
    if not client:
        return []
    prompt = (
        f"Найди в интернете официальный Instagram-аккаунт публичной персоны: «{name}» "
        "(актёр, певец, блогер, стример, спортсмен, модель, создатель контента).\n"
        "Формат ответа — СТРОГО одна строка: HANDLES: ник1, ник2, ник3\n"
        "Правила: до 3 username через запятую, основной (самый крупный, верифицированный) первым. "
        "Только username без @ и без ссылок. Не возвращай фан-страницы, пародии, двойников и "
        "резервные аккаунты, если есть основной. Не ищи несовершеннолетних и частных лиц. "
        "Если персона непубличная, неоднозначная или аккаунт не найден — верни ровно: HANDLES: NONE"
    )
    resp = await client.aio.models.generate_content(
        model=MODEL, contents=prompt,
        config=gt.GenerateContentConfig(tools=[{"google_search": {}}]),
    )
    return _parse_handles(resp.text or "")


async def _resolve_person(person: str) -> dict:
    from instagram_viewer import get_user_status, search_accounts
    key = person.lower()
    hit = _PERSON_CACHE.get(key)
    if hit and time.time() - hit[0] < _PERSON_TTL:
        handles = [hit[1]]
    else:
        try:
            handles = await _resolve_instagram_handles(person)
        except Exception as e:
            logger.warning(f"find_person {person!r}: поиск не удался: {e}")
            handles = []

    if not handles:
        try:
            res = await search_accounts(person, 5)
        except Exception:
            res = None
        return {"kind": "search", "cands": res} if res else {"kind": "none"}

    evaluated: list[tuple[str, dict]] = []
    any_error = False
    dominant = None
    for h in handles:
        status, info = await get_user_status(h, allow_stale=True)
        if status in ("ok", "stale") and info:
            evaluated.append((h, info))
            if info.get("is_verified") or int(info.get("followers") or 0) >= 100_000:
                dominant = (h, info)
                break
        elif status == "error":
            any_error = True

    pick = dominant
    if not pick and len(evaluated) == 1:
        pick = evaluated[0]
    if not pick and len(evaluated) >= 2:
        ranked = sorted(evaluated, key=lambda x: (1 if x[1].get("is_verified") else 0,
                                                  int(x[1].get("followers") or 0)), reverse=True)
        top = int(ranked[0][1].get("followers") or 0)
        second = int(ranked[1][1].get("followers") or 0)
        if top >= 10_000 and top >= 5 * max(second, 1):
            pick = ranked[0]
        else:
            return {"kind": "ambiguous", "cands": [
                {"username": h, "full_name": i.get("full_name", ""), "followers": i.get("followers"),
                 "is_verified": bool(i.get("is_verified"))} for h, i in ranked]}
    if pick:
        _PERSON_CACHE[key] = (time.time(), pick[0])
        return {"kind": "found", "handle": pick[0], "info": pick[1]}
    if any_error:
        return {"kind": "api_down", "handle": handles[0]}
    return {"kind": "notinig", "handles": handles}


def _pick_keyboard(cands: list[dict], limit: int = 5):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from instagram_handlers import _fmt_count
    rows = []
    for c in cands[:limit]:
        extra = f" · {_fmt_count(c['followers'])}" if c.get("followers") else ""
        mark = " ✅" if c.get("is_verified") else ""
        rows.append([InlineKeyboardButton(text=f"👤 @{c['username']}{extra}{mark}",
                                          callback_data=f"ig_profile:{c['username']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _deliver_person(ctx: ToolContext, person: str, r: dict) -> str:
    from instagram_handlers import _show_profile
    esc = html.escape(person)
    kind = r["kind"]
    if kind == "found":
        h = r["handle"]
        await ctx.message.answer(f"🔎 «{esc}» → <b>@{h}</b>", parse_mode="HTML")
        await _show_profile(ctx.message, h, ctx.user_id)
        return (f"Instagram «{person}» найден через интернет: @{h}. "
                "Карточка профиля отправлена в чат (с кнопками сторис, постов, подписки).")
    if kind == "ambiguous":
        await ctx.message.answer(f"🤔 по «{esc}» нашлось несколько аккаунтов — какой нужен?",
                                 parse_mode="HTML", reply_markup=_pick_keyboard(r["cands"]))
        return ("Нашлось несколько подходящих аккаунтов, пользователю показаны кнопки для выбора. "
                "Больше ничего не пиши — он выберет сам.")
    if kind == "search":
        await ctx.message.answer(
            f"🔎 официальный ник «{esc}» в интернете не определился. вот что даёт поиск инсты по имени "
            "— проверь, что это тот человек:", parse_mode="HTML",
            reply_markup=_pick_keyboard(r["cands"]))
        return ("Официальный ник определить не удалось, пользователю показаны результаты поиска "
                "Instagram кнопками. Больше ничего не пиши — он выберет сам.")
    if kind == "api_down":
        h = r["handle"]
        await ctx.message.answer(
            f"🔎 по имени «{esc}» нашёл <a href=\"https://instagram.com/{h}\">@{h}</a>, "
            "но сама инста сейчас не отвечает, карточку показать не могу",
            parse_mode="HTML", disable_web_page_preview=True)
        return (f"Найден ник @{h} (через интернет), но Instagram не ответил — карточки нет, "
                "ссылка отправлена в чат.")
    if kind == "notinig":
        return (f"ОШИБКА: по имени «{person}» в интернете нашлись ники {', '.join(r['handles'])}, "
                "но в Instagram таких аккаунтов нет. Попроси пользователя прислать ник.")
    return (f"ОШИБКА: не нашёл официальный Instagram для «{person}» (имя неоднозначное или это не "
            "публичная персона). Попроси пользователя прислать ник или ссылку.")


async def _tool_instagram_find_person(ctx: ToolContext, name: str = "") -> str:
    person = _clean(name, 80)
    if not person:
        return "ОШИБКА: не указано имя человека. Попроси пользователя его назвать."
    return await _deliver_person(ctx, person, await _resolve_person(person))


def _split_names(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|\n|&|\s+и\s+|\s+and\s+)\s*", text or "", flags=re.I)
    out: list[str] = []
    for p in parts:
        p = re.sub(r"^(?:найди\s+)?(?:инст\w*|аккаунт\w*|профил\w*|instagram)\s+(?:у\s+|для\s+|от\s+)?", "", p.strip(), flags=re.I)
        p = p.strip(" .\"«»")
        if len(p) >= 2 and p.lower() not in [o.lower() for o in out]:
            out.append(p[:80])
    return out


async def _tool_instagram_find_people(ctx: ToolContext, names: str = "") -> str:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from instagram_handlers import _fmt_count
    parts = _split_names(names)[:4]
    if not parts:
        return "ОШИБКА: не указаны имена. Попроси пользователя назвать людей."
    if len(parts) == 1:
        return await _tool_instagram_find_person(ctx, parts[0])

    results = await asyncio.gather(*(_resolve_person(p) for p in parts), return_exceptions=True)
    lines = [f"🔎 нашёл {len(parts)} имён:\n"]
    rows: list[list] = []

    def btn(u: str, label: str = "") -> list:
        return [InlineKeyboardButton(text=f"👤 @{u}{label}", callback_data=f"ig_profile:{u}")]

    summary = []
    for p, r in zip(parts, results):
        if isinstance(r, Exception):
            r = {"kind": "none"}
        esc = html.escape(p)
        k = r["kind"]
        if k == "found":
            f = int(r["info"].get("followers") or 0)
            lines.append(f"✅ <b>{esc}</b> → @{r['handle']}" + (f" · {_fmt_count(f)}" if f else ""))
            rows.append(btn(r["handle"]))
            summary.append(f"{p}: @{r['handle']}")
        elif k in ("ambiguous", "search"):
            lines.append(f"🤔 <b>{esc}</b> — "
                         + ("несколько вариантов:" if k == "ambiguous" else "ник не определился, вот поиск инсты:"))
            for c in r["cands"][:2]:
                rows.append(btn(c["username"], " ✅" if c.get("is_verified") else ""))
            summary.append(f"{p}: варианты на выбор")
        elif k == "api_down":
            lines.append(f"⚠️ <b>{esc}</b> → @{r['handle']} (инста не отвечает, не проверил)")
            rows.append([InlineKeyboardButton(text=f"↗️ @{r['handle']}",
                                              url=f"https://instagram.com/{r['handle']}")])
            summary.append(f"{p}: @{r['handle']} (не проверен)")
        else:
            lines.append(f"❌ <b>{esc}</b> — не нашёл")
            summary.append(f"{p}: не найден")
    lines.append("\n<i>нажми на ник — покажу карточку</i>")
    await ctx.message.answer("\n".join(lines), parse_mode="HTML",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[:8]),
                             disable_web_page_preview=True)
    return ("Список Instagram-аккаунтов отправлен в чат одним сообщением с кнопками: "
            + "; ".join(summary) + ". Больше ничего не пиши.")


async def _tool_instagram_download(ctx: ToolContext, url: str = "") -> str:
    from instagram_handlers import _dispatch_ig_link, _parse_ig_link
    parsed = _parse_ig_link(_clean(url, 500))
    if not parsed:
        return "ОШИБКА: это не ссылка на пост/рилс/сторис/хайлайт Instagram."
    ok = await _dispatch_ig_link(ctx.message, parsed)
    return ("Медиа из Instagram отправлено в чат." if ok
            else "ОШИБКА: не удалось достать (приватный аккаунт, удалено или Instagram капризничает).")


# ── Скачивание видео ─────────────────────────────────────────────────────────
async def _tool_download_video(ctx: ToolContext, url: str = "") -> str:
    from media_downloader import detect_platform, handle_media_link
    platform, link = detect_platform(_clean(url, 500))
    if not platform:
        return ("ОШИБКА: поддерживаются ссылки YouTube, TikTok, X/Twitter, Reddit, Pinterest, "
                "VK Видео/Клипы, Twitch-клипы, Threads, Pornhub и XVideos.")
    await handle_media_link(ctx.message, custom_url=link, custom_platform=platform)
    return f"Запрос на скачивание с {platform} обработан, результат (или ошибка) показан в чате."


# ── Случайное 18+ видео (со спойлером, кэшем file_id, превью и альбомом) ─────
async def _tool_random_adult_video(
    ctx: ToolContext,
    query: str = "",
    source: str = "any",
    preview: bool = False,
) -> str:
    from media_downloader import send_random_adult_video
    q = _clean(query, 80)
    src = _clean(source, 15).lower()
    if src not in ("pornhub", "xvideos", "any"):
        src = "any"
    ok = await send_random_adult_video(
        ctx.message,
        query=q,
        source=src,
        quality=480,
        preview=bool(preview),
    )
    if not ok:
        return "ОШИБКА: не удалось найти или скачать 18+ видео (возможно, сервер блокирует доступ без ADULT_PROXY)."
    if preview:
        return "Карточка предпросмотра 18+ видео (со спойлером и кнопками 480p/720p/Другое) отправлена в чат."
    return ("Случайное 18+ видео отправлено в чат под спойлером "
            "(если оно было больше 50 МБ — автоматически разрезано и собрано в единый альбом).")


# ── Музыка ───────────────────────────────────────────────────────────────────
async def _tool_music_search(ctx: ToolContext, query: str = "") -> str:
    from music_handlers import show_music_page
    q = _clean(query, 100)
    if not q:
        return "ОШИБКА: пустой поисковый запрос."
    await show_music_page(ctx.message, "search", q, 0)
    return f"Список найденных треков по «{q}» с кнопками скачивания отправлен в чат."


async def _tool_music_send_track(ctx: ToolContext, query: str = "") -> str:
    from music_handlers import send_track_to_user
    from music_engine import music_engine
    q = _clean(query, 100)
    if not q:
        return "ОШИБКА: не указано название трека или исполнителя."
    tracks = await music_engine.search_multi(q, limit=3)
    if not tracks:
        return f"ОШИБКА: по запросу «{q}» ничего не найдено."
    best = tracks[0]
    await send_track_to_user(ctx.message, best["id"])
    return f"Трек «{best.get('artist', '?')} — {best.get('title', '?')}» отправлен в чат."


async def _tool_music_charts(ctx: ToolContext) -> str:
    from music_handlers import show_music_page
    await show_music_page(ctx.message, "chart", "", 0)
    return "Чарт отправлен в чат."


# ── Аниме ────────────────────────────────────────────────────────────────────
async def _tool_anime_search(ctx: ToolContext, query: str = "") -> str:
    from anixart_handler import _do_search
    q = _clean(query, 100)
    if not q:
        return "ОШИБКА: не указано название аниме."
    await _do_search(ctx.message, q, offset=0)
    return f"Результаты поиска аниме «{q}» отправлены в чат (с кнопками карточек)."


async def _tool_anime_random(ctx: ToolContext) -> str:
    from anixart_handler import _do_random
    await _do_random(ctx.message)
    return "Случайное аниме (карточка) отправлено в чат."


async def _tool_anime_recommend(ctx: ToolContext, vibe: str = "") -> str:
    from anixart_handler import _do_ai_recommend
    v = _clean(vibe, 300)
    if not v:
        return "ОШИБКА: не описано, какое аниме хочется."
    await _do_ai_recommend(ctx.message, v)
    return "ИИ-подборка аниме по вайбу отправлена в чат."


async def _tool_anime_top(ctx: ToolContext) -> str:
    from anixart_handler import _do_top
    await _do_top(ctx.message)
    return "Топ аниме группы отправлен в чат."


async def _tool_anime_schedule(ctx: ToolContext) -> str:
    import datetime
    import config
    from anixart_handler import _render_schedule_view
    today_idx = datetime.datetime.now(config.TIMEZONE).weekday()
    await _render_schedule_view(ctx.message, ctx.user_id, today_idx, only_my_subs=False, edit=False)
    return "Календарь выхода серий отправлен в чат."


async def _anime_pick(title: str, alt: str = "", min_score: float = 0.0):
    from anixart_handler import _best_release_match, _get_release
    t = _clean(title, 120)
    if not t:
        return None, "ОШИБКА: не указано название аниме."
    rid, score, shown = await _best_release_match(t, alt)
    if not rid:
        return None, f"ОШИБКА: в Anixart не нашлось аниме «{t}»."
    if score < min_score:
        return None, (f"ОШИБКА: не уверен, что «{t}» — это «{shown}». "
                      "Уточни у пользователя точное название.")
    rel = await _get_release(rid)
    if not rel:
        return None, "ОШИБКА: не удалось загрузить карточку релиза."
    return rel, ""


async def _tool_anime_ost(ctx: ToolContext, title: str = "", kind: str = "any", number: int = 1) -> str:
    from anixart_handler import _fetch_oped_themes, _send_oped_audio
    rel, err = await _anime_pick(title)
    if err:
        return err
    themes = await _fetch_oped_themes(rel)
    if not themes:
        return "ОШИБКА: для этого аниме не нашлось опенингов и эндингов."
    kind = _clean(kind, 5).lower()
    want = {"op": "OP", "ed": "ED"}.get(kind)
    matching = [i for i, t in enumerate(themes)
                if not want or want in f"{t.get('type', '')} {t.get('label', '')}".upper()]
    if not matching:
        matching = list(range(len(themes)))
    try:
        n = max(1, int(number or 1))
    except (TypeError, ValueError):
        n = 1
    idx = matching[min(n - 1, len(matching) - 1)]
    await _send_oped_audio(ctx.message, int(rel["id"]), idx, themes[idx], rel)
    return f"Трек «{themes[idx].get('label', 'OST')}» из «{rel.get('title_ru') or rel.get('title_original')}» отправлен в чат."


async def _tool_anime_profile(ctx: ToolContext) -> str:
    from anixart_handler import _do_profile
    await _do_profile(ctx.message)
    return "Карточка Anime Wrapped пользователя отправлена в чат."


async def _tool_anime_mark_watched(ctx: ToolContext, title: str = "", episode: int = 1) -> str:
    from anixart_handler import sync_rate_to_shikimori_and_anixart
    rel, err = await _anime_pick(title, min_score=0.5)
    if err:
        return err
    try:
        ep = max(1, int(episode))
    except (TypeError, ValueError):
        return "ОШИБКА: номер серии не понятен."
    total = int(rel.get("episodes_total") or 0)
    if total and ep > total:
        return f"ОШИБКА: в «{rel.get('title_ru')}» всего {total} серий, а названа {ep}-я."
    status = "completed" if (total and ep >= total) else "watching"
    res = await sync_rate_to_shikimori_and_anixart(ctx.user_id, rel, status, episodes=ep)
    synced = [n for n, f in (("Shikimori", res.get("shiki")), ("Anixart", res.get("anixart"))) if f]
    return (f"Отмечено: «{rel.get('title_ru') or rel.get('title_original')}», серий просмотрено {res.get('episodes', ep)}"
            f"{f'/{total}' if total else ''}, статус «{status}». "
            + (f"Синхронизировано: {', '.join(synced)}." if synced else "Сохранено только в профиле бота."))


async def _tool_anime_franchise(ctx: ToolContext, title: str = "") -> str:
    from anixart_handler import _send_franchise
    rel, err = await _anime_pick(title)
    if err:
        return err
    await _send_franchise(ctx.message, rel, ctx.user_id)
    return f"Порядок просмотра франшизы «{rel.get('title_ru') or rel.get('title_original')}» отправлен в чат."


# ── Товары ───────────────────────────────────────────────────────────────────
async def _tool_product_search(ctx: ToolContext, query: str = "") -> str:
    from ozon_handler import ozon_search
    q = _clean(query, 150)
    if not q:
        return "ОШИБКА: не указано, какой товар искать."
    found = await ozon_search(ctx.message, q)
    if not found:
        return f"По запросу «{q}» товары не найдены, но в чат отправлены прямые ссылки на поиск WB и Ozon."
    return f"Результаты поиска товара «{q}» отправлены в чат."


# ── Наряды и дембель ─────────────────────────────────────────────────────────
async def _tool_duty_schedule(ctx: ToolContext, when: str = "today") -> str:
    from datetime import date, timedelta
    from duty_handlers import auto_delete_later, build_duty_message, build_week_schedule
    when = _clean(when, 20).lower()
    if when == "week":
        text = build_week_schedule()
    elif when == "tomorrow":
        text = build_duty_message(date.today() + timedelta(days=1), "завтра")
    else:
        text = build_duty_message(date.today(), "сегодня")
    sent = await ctx.message.answer(text, parse_mode="Markdown")
    auto_delete_later(ctx.message.bot, ctx.chat_id, sent.message_id, 300)
    return "График нарядов отправлен в чат."


async def _tool_dembel_timer(ctx: ToolContext, person: str = "") -> str:
    from duty_handlers import _send_dembel_card, _send_dembel_summary
    from dembel_generator import DEMBEL_DATES
    person = _clean(person, 40)
    if person:
        match = next((p for p in DEMBEL_DATES if p.lower() == person.lower()), None)
        if not match:
            return f"ОШИБКА: «{person}» нет в таймере. Доступны: {', '.join(DEMBEL_DATES)}."
        await _send_dembel_card(ctx.message, match, edit_in_place=False)
        return f"Дембель-карточка {match} отправлена в чат."
    await _send_dembel_summary(ctx.message, edit_in_place=False)
    return "Сводка дембель-таймеров отправлена в чат."


# ── Лента Reddit ─────────────────────────────────────────────────────────────
async def _tool_reddit_feed(ctx: ToolContext) -> str:
    from feed_handlers import cmd_feed
    await cmd_feed(ctx.message)
    return "Пост из ленты (мемы/картинки) отправлен в чат."


# ── Генерация картинки (Pollinations) ────────────────────────────────────────
async def _tool_generate_image(ctx: ToolContext, prompt: str = "") -> str:
    import urllib.parse as up
    import aiohttp
    from aiogram.types import BufferedInputFile
    p = _clean(prompt, 400)
    if not p:
        return "ОШИБКА: нет описания картинки."
    url = (f"https://image.pollinations.ai/prompt/{up.quote(p)}"
           f"?width=1024&height=1024&nologo=true&seed={abs(hash(p)) % 99999}")
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                return f"ОШИБКА: генератор картинок вернул HTTP {r.status}."
            data = await r.read()
    await ctx.message.answer_photo(
        BufferedInputFile(data, "image.jpg"),
        caption=f"🎨 <i>{html.escape(p)}</i>", parse_mode="HTML",
    )
    return "Картинка сгенерирована и отправлена в чат."


# ── Интернет (погода, новости, факты) ────────────────────────────────────────
async def _tool_web_search(ctx: ToolContext, query: str = "") -> str:
    from ai_handlers import get_gemini
    from google.genai import types as gt
    client = get_gemini()
    q = _clean(query, 300)
    if not client or not q:
        return "ОШИБКА: поиск недоступен."
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=f"Найди актуальную информацию и коротко (до 1200 символов, только факты) ответь: {q}",
        config=gt.GenerateContentConfig(tools=[{"google_search": {}}]),
    )
    return (resp.text or "").strip()[:3000] or "Ничего не нашлось."


# ── Память ───────────────────────────────────────────────────────────────────
async def _tool_remember_fact(ctx: ToolContext, fact: str = "") -> str:
    from ai_handlers import save_user_fact
    f = _clean(fact, 120)
    if not f:
        return "ОШИБКА: нечего запоминать."
    save_user_fact(ctx.user_id, f)
    return "Запомнил."


# ══════════════════════════════════════════════════════════════════════════════
# Новые модули: мемы, рулетки, места рядом, разбор файлов, фильмы, раздачи, музыкальные ссылки
# ══════════════════════════════════════════════════════════════════════════════
async def _tool_make_meme(ctx: ToolContext, topic: str = "", style: str = "demotivator") -> str:
    from vision_handlers import extract_media, make_meme_for
    src = ctx.message.reply_to_message
    media = extract_media(src) or extract_media(ctx.message)
    if not media or not (media.is_image or media.kind in ("photo", "sticker")):
        return "ОШИБКА: мем делается из картинки — пусть пользователь ответит на фото (или пришлёт его) с этой просьбой."
    ok = await make_meme_for(ctx.message, media, _clean(topic, 150), _clean(style, 12).lower() == "impact",
                             reply_to=ctx.message)
    return "Мем отправлен в чат." if ok else "ОШИБКА: мем не получился."


async def _tool_analyze_media(ctx: ToolContext, task: str = "") -> str:
    from vision_handlers import answer_about_media, detect_intent, extract_media
    src = ctx.message.reply_to_message
    media = extract_media(src, allow_text=True) or extract_media(ctx.message)
    if not media:
        return "ОШИБКА: нет файла — пусть пользователь ответит на фото/PDF/видео/голосовое с вопросом."
    text = _clean(task, 500) or (ctx.message.text or "")
    ok = await answer_about_media(ctx.message, media, detect_intent(text) or "other", text, reply_to=ctx.message)
    return "Разбор файла отправлен в чат." if ok else "ОШИБКА: не удалось разобрать файл (подробности уже показаны в чате)."


async def _tool_random_choice(ctx: ToolContext, options: str = "", question: str = "") -> str:
    from fun_handlers import parse_options, run_pick
    opts = parse_options(options)
    if len(opts) < 2:
        return "ОШИБКА: нужно минимум 2 варианта через запятую."
    ok = await run_pick(ctx.message, opts, _clean(question, 80))
    return "Жребий брошен, результат с анимацией показан в чате." if ok else "ОШИБКА: жребий не удался."


async def _tool_who_picks(ctx: ToolContext, question: str = "", candidates: str = "") -> str:
    from fun_handlers import open_lobby, parse_options, run_pick
    q = _clean(question, 100) or "Кого выбираем?"
    names = parse_options(candidates)
    if len(names) >= 2:
        await run_pick(ctx.message, names, q if q.endswith("?") else q + "?")
        return "Жребий среди названных людей проведён и показан в чате."
    await open_lobby(ctx.message, q)
    return "Открыл лобби с кнопкой «Участвую» — участники нажмут её, потом будет жребий."


async def _tool_nearby_places(ctx: ToolContext, category: str = "", address: str = "") -> str:
    from places_handlers import detect_category, geocode, recall_location, request_location, show_nearby
    cat = detect_category(_clean(category, 30))
    addr = _clean(address, 150)
    if addr:
        geo = await geocode(addr)
        if not geo:
            return f"ОШИБКА: не нашёл адрес «{addr}». Попроси точнее или геопозицию."
        ok = await show_nearby(ctx.message, geo[0], geo[1], cat, label=geo[2], user_id=ctx.user_id)
        return "Места рядом (со ссылками на карту) отправлены в чат." if ok else "ОШИБКА: карты не ответили."
    last = recall_location(ctx.user_id)
    if last:
        ok = await show_nearby(ctx.message, last[0], last[1], cat, user_id=ctx.user_id)
        return "Места рядом с последней геопозицией отправлены в чат." if ok else "ОШИБКА: карты не ответили."
    await request_location(ctx.message)
    return "Геопозиции нет — попросил пользователя прислать её (кнопка/ответ); после этого места покажутся."


async def _tool_film_search(ctx: ToolContext, query: str = "") -> str:
    from film_handler import film_search_flow, tmdb_configured
    if not tmdb_configured():
        return "ОШИБКА: TMDB не настроен (нет TMDB_API_KEY). Скажи об этом администратору бота."
    q = _clean(query, 120)
    if not q:
        return "ОШИБКА: не указано название фильма или сериала."
    ok = await film_search_flow(ctx.message, q)
    return f"Карточка «{q}» (рейтинг, где смотреть, трейлер) отправлена в чат." if ok else f"ОШИБКА: «{q}» не найден."


async def _tool_film_trending(ctx: ToolContext) -> str:
    from film_handler import show_trending
    ok = await show_trending(ctx.message)
    return "Тренды фильмов и сериалов отправлены в чат." if ok else "ОШИБКА: тренды недоступны."


async def _tool_film_recommend(ctx: ToolContext, vibe: str = "") -> str:
    from film_handler import ai_recommend
    v = _clean(vibe, 300)
    if not v:
        return "ОШИБКА: не описано, какой фильм/сериал хочется."
    ok = await ai_recommend(ctx.message, v)
    return "ИИ-подборка фильмов/сериалов отправлена в чат." if ok else "ОШИБКА: подборка не получилась."


async def _tool_free_games(ctx: ToolContext, store: str = "all") -> str:
    from freebies import show_free
    store = _clean(store, 10).lower()
    ok = await show_free(ctx.message, store if store in ("epic", "steam", "other") else "all")
    return "Список бесплатных раздач отправлен в чат." if ok else "ОШИБКА: источники раздач не отвечают."


async def _tool_music_link(ctx: ToolContext, url: str = "") -> str:
    from music_links import find_music_url, process_music_link
    service, link = find_music_url(_clean(url, 500))
    if not link:
        return "ОШИБКА: это не ссылка Spotify, Яндекс.Музыки или VK Музыки."
    ok = await process_music_link(ctx.message, service, link)
    return "Трек по ссылке определён, результаты поиска отправлены в чат." if ok else \
        "ОШИБКА: не удалось определить трек по ссылке (пользователю уже объяснили, что делать)."


# ══════════════════════════════════════════════════════════════════════════════
# Реестр функций.
# ══════════════════════════════════════════════════════════════════════════════
_tool("instagram_profile",
      "Показать профиль Instagram ПО ГОТОВОМУ НИКУ или ссылке (карточка: аватар, подписчики, кнопки "
      "сторис/постов/подписки). Вызывай, когда просят найти/показать/глянуть аккаунт в Instagram "
      "(инста, инсте, инстаграм) и ник уже известен. Если назвали ИМЯ человека, а не ник — "
      "вызывай instagram_find_person, ник не выдумывай.",
      _tool_instagram_profile,
      {"username": _s("Username без @, либо ссылка на профиль")}, ["username"])

_tool("instagram_find_people",
      "Найти Instagram СРАЗУ НЕСКОЛЬКИХ известных людей (2–4 имени), например «инста свити фокс и "
      "эммы уотсон», «аккаунты Дженны Ортеги, Билли Айлиш». Ищет всех параллельно и присылает один "
      "список с кнопками. Для ОДНОГО человека используй instagram_find_person.",
      _tool_instagram_find_people,
      {"names": _s("Имена через запятую или «и», как написал пользователь, например «Emma Watson, Jenna Ortega»")},
      ["names"])

_tool("instagram_find_person",
      "Найти Instagram известного человека по ИМЕНИ (актёр, певец, блогер, стример, спортсмен, модель): "
      "«найди инстаграм Эммы Уотсон», «инста свити фокс», «аккаунт Дженны Ортеги». Сама ищет ник "
      "в интернете, проверяет его в Instagram и показывает карточку. Только для публичных персон; "
      "если ник/ссылка уже есть — используй instagram_profile.",
      _tool_instagram_find_person,
      {"name": _s("Имя человека как написал пользователь (на любом языке), например «Emma Watson»")},
      ["name"])

_tool("instagram_download",
      "Скачать пост, рилс, сторис или хайлайт Instagram по ссылке. Вызывай, когда пользователь "
      "просит скачать/достать/скинуть медиа и есть ссылка instagram.com (в сообщении или в реплае).",
      _tool_instagram_download,
      {"url": _s("Полная ссылка instagram.com")}, ["url"])

_tool("download_video",
      "Скачать видео/медиа по ссылке с YouTube, TikTok, X (Twitter), Reddit, Pinterest, VK Видео/Клипов, "
      "Twitch-клипов, Threads, Pornhub или XVideos.",
      _tool_download_video,
      {"url": _s("Ссылка на видео")}, ["url"])

_tool("random_adult_video",
      "Найти и отправить случайное 18+ видео (прон, порно) с Pornhub или XVideos прямо медиафайлом в чат "
      "(со спойлером; если видео весит больше 50 МБ — оно автоматически режется на части и собирается в альбом). "
      "Если пользователь просит сначала превью/карточку на выбор — передай preview=True.",
      _tool_random_adult_video,
      {"query": _s("Категория или поисковый запрос на русском или английском (необязательно)"),
       "source": {"type": "STRING", "description": "pornhub, xvideos или any", "enum": ["pornhub", "xvideos", "any"]},
       "preview": {"type": "BOOLEAN", "description": "True — сначала показать карточку превью с выбором качества; False — сразу скачать видео"}})

_tool("music_send_track",
      "Найти песню и СРАЗУ отправить её аудиофайлом. Вызывай на «скинь/кинь/пришли/включи песню X», "
      "когда понятно, какой именно трек нужен (исполнитель и/или название).",
      _tool_music_send_track,
      {"query": _s("Исполнитель и название, например «Rammstein Du Hast»")}, ["query"])

_tool("music_search",
      "Показать список найденных треков с кнопками на выбор. Вызывай на «найди песни/треки …», "
      "когда запрос расплывчатый (жанр, исполнитель целиком, «что-нибудь вроде …»).",
      _tool_music_search,
      {"query": _s("Поисковый запрос по музыке")}, ["query"])

_tool("music_charts", "Показать музыкальный чарт (популярные треки сейчас).", _tool_music_charts)

_tool("anime_search",
      "Найти аниме по названию (список с кнопками карточек).",
      _tool_anime_search,
      {"query": _s("Название аниме")}, ["query"])

_tool("anime_random", "Показать случайное аниме.", _tool_anime_random)

_tool("anime_recommend",
      "Подобрать аниме под настроение/вайб/описание (ИИ-подборка из 4 тайтлов). "
      "Вызывай на «посоветуй аниме», «что посмотреть из аниме».",
      _tool_anime_recommend,
      {"vibe": _s("Пожелания: жанр, настроение, что нравится/не нравится")}, ["vibe"])

_tool("anime_top", "Показать топ аниме, которое смотрят в этой группе.", _tool_anime_top)

_tool("anime_schedule", "Показать календарь выхода новых серий аниме на неделю.", _tool_anime_schedule)

_tool("anime_ost",
      "Скинуть опенинг или эндинг аниме аудиофайлом (MP3 с обложкой). Вызывай на «скинь опенинг/эндинг/OST из …».",
      _tool_anime_ost,
      {"title": _s("Название аниме"),
       "kind": {"type": "STRING", "description": "op — опенинг, ed — эндинг, any — любой", "enum": ["op", "ed", "any"]},
       "number": {"type": "INTEGER", "description": "Номер опенинга/эндинга (1 по умолчанию)"}},
      ["title"])

_tool("anime_profile",
      "Показать аниме-профиль пользователя: карточка Anime Wrapped (часы просмотра, жанры, средняя оценка).",
      _tool_anime_profile)

_tool("anime_mark_watched",
      "Отметить серию аниме просмотренной (синхронизация с Shikimori и Anixart). "
      "Вызывай на «отметь 5 серию … просмотренной», «досмотрел … до 8 серии».",
      _tool_anime_mark_watched,
      {"title": _s("Название аниме"),
       "episode": {"type": "INTEGER", "description": "Номер последней просмотренной серии"}},
      ["title", "episode"], terminal=False)

_tool("anime_franchise",
      "Показать порядок просмотра франшизы аниме (сезоны, фильмы, приквелы). "
      "Вызывай на «в каком порядке смотреть …».",
      _tool_anime_franchise,
      {"title": _s("Название аниме или франшизы")}, ["title"])

_tool("product_search",
      "Найти товар в магазине (Wildberries) с ценами и рейтингами. Вызывай на «найди/посмотри на озоне/"
      "вайлдберриз/где купить …». Можно использовать слова-фильтры в запросе: «до 3000», «от 50000», "
      "«дешёвые», «рейтинг», «скидки», «новинки».",
      _tool_product_search,
      {"query": _s("Что искать, вместе с ценовыми фильтрами, если они названы")}, ["query"])

_tool("duty_schedule",
      "Показать график нарядов (кто на сводках и процедурке) на сегодня, завтра или неделю.",
      _tool_duty_schedule,
      {"when": {"type": "STRING", "description": "today, tomorrow или week", "enum": ["today", "tomorrow", "week"]}})

_tool("dembel_timer",
      "Показать дембель-таймер: общую сводку или карточку одного человека.",
      _tool_dembel_timer,
      {"person": _s("Имя из таймера; пусто — общая сводка")})

_tool("reddit_feed", "Прислать пост из ленты (мемы, картинки, видео из Reddit).", _tool_reddit_feed)

_tool("generate_image",
      "Нарисовать/сгенерировать картинку по описанию. Описание переведи на английский.",
      _tool_generate_image,
      {"prompt": _s("Подробное описание картинки на английском")}, ["prompt"])

_tool("web_search",
      "Найти информацию в интернете: погода, новости, курсы, факты, расписания, всё актуальное. "
      "Результат вернётся тебе — на его основе ответь пользователю сам.",
      _tool_web_search,
      {"query": _s("Поисковый запрос")}, ["query"], terminal=False)

_tool("remember_fact",
      "Запомнить факт о пользователе на будущее, если он явно просит «запомни …».",
      _tool_remember_fact,
      {"fact": _s("Короткая формулировка факта")}, ["fact"], terminal=False)

_tool("make_meme",
      "Сделать мем (демотиватор) из картинки, на которую пользователь ответил, или прикрепил. Вызывай на "
      "«сделай мем про …» / «сделай мем из этого».",
      _tool_make_meme,
      {"topic": _s("Тема/идея мема, если названа; иначе пусто"),
       "style": {"type": "STRING", "description": "demotivator (по умолчанию) или impact (верх/низ белым шрифтом)",
                 "enum": ["demotivator", "impact"]}})

_tool("analyze_media",
      "Разобрать файл, на который пользователь ответил (фото, скриншот, стикер, PDF, DOCX, видео, голосовое) "
      "или текст-реплай: перевести, прочитать текст, объяснить мем, пересказать документ, ответить на вопрос по нему.",
      _tool_analyze_media,
      {"task": _s("Что именно сделать: «переведи», «что написано», «объясни мем», вопрос пользователя")})

_tool("random_choice",
      "Выбрать случайный вариант из названных с анимацией-жребием. «Кофе или чай?», «что заказать: пиццу, суши».",
      _tool_random_choice,
      {"options": _s("Варианты через запятую"), "question": _s("Заголовок жребия (необязательно)")}, ["options"])

_tool("who_picks",
      "Честный жребий «кто …?»: кто моет посуду, кто идёт за едой. Если имена названы — выберет из них; "
      "если нет — откроет лобби, где люди жмут «Участвую».",
      _tool_who_picks,
      {"question": _s("Вопрос, например «Кто сегодня моет посуду»"),
       "candidates": _s("Имена через запятую, если пользователь их назвал; иначе пусто")}, ["question"])

_tool("nearby_places",
      "Найти ближайшие кафе, аптеки и магазины со ссылками на карту. Вызывай на «где ближайшая аптека», "
      "«что поесть рядом», «магазин рядом». Если геопозиции нет — сам попросит её у пользователя.",
      _tool_nearby_places,
      {"category": {"type": "STRING", "description": "cafe (кафе/еда), pharmacy (аптека), shop (магазин) или пусто — всё"},
       "address": _s("Адрес/место, если назван вместо геопозиции")})

_tool("film_search",
      "Найти фильм или сериал и показать карточку: постер, рейтинг, жанры, где смотреть, трейлер. "
      "Вызывай на «найди фильм …», «что за сериал …», «где посмотреть …».",
      _tool_film_search,
      {"query": _s("Название фильма или сериала (можно с годом)")}, ["query"])

_tool("film_trending", "Показать фильмы и сериалы, которые сейчас в тренде.", _tool_film_trending)

_tool("film_recommend",
      "Подобрать фильмы/сериалы под настроение или описание (ИИ-подборка с карточками). "
      "Вызывай на «посоветуй фильм/сериал».",
      _tool_film_recommend,
      {"vibe": _s("Пожелания: жанр, настроение, что нравилось")}, ["vibe"])

_tool("free_games",
      "Показать, какие игры сейчас раздают бесплатно (Epic, Steam, GOG и др.) и когда закончится раздача. "
      "Вызывай на «что раздают бесплатно», «есть бесплатные игры в эпике».",
      _tool_free_games,
      {"store": {"type": "STRING", "description": "all, epic, steam или other", "enum": ["all", "epic", "steam", "other"]}})

_tool("music_link",
      "Открыть трек/альбом/плейлист по ссылке Spotify, Яндекс.Музыки или VK Музыки: определит название и "
      "покажет поиск для скачивания.",
      _tool_music_link,
      {"url": _s("Ссылка на музыку")}, ["url"])


TOOLS_HINT = (
    "\n\nИНСТРУМЕНТЫ: у тебя есть функции бота (Instagram, музыка и музыкальные ссылки, аниме, фильмы и сериалы, "
    "товары, скачивание видео, случайное 18+ видео, наряды, дембель, картинки, мемы, жребий и выбор, места рядом, бесплатные игры, "
    "разбор фото/файлов реплаем, поиск в интернете). Если просьба — это действие, которое покрывает "
    "функция, ВЫЗОВИ её, а не отвечай «я не умею» или «воспользуйся командой». Для обычного разговора, "
    "шуток и советов функции не вызывай. Не выдумывай параметры: если не хватает username, названия "
    "или ссылки — коротко спроси. Если просят Instagram знаменитости/блогера по ИМЕНИ — вызови "
    "instagram_find_person (один человек) или instagram_find_people (несколько имён сразу), а не угадывай ник сам. Функции сами отправляют результат в чат, поэтому не пересказывай его. "
    "Если функция вернула ОШИБКА — объясни пользователю своими словами и предложи вариант. "
    "Ответы после функций — 1 короткая фраза в твоём характере."
)


# ══════════════════════════════════════════════════════════════════════════════
# Выполнение и основной цикл
# ══════════════════════════════════════════════════════════════════════════════
def _rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    q = _RATE[user_id]
    while q and now - q[0] > RATE_LIMIT_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT_CALLS:
        return True
    q.append(now)
    return False


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> tuple[bool, str]:
    """Возвращает (ok, текст результата для модели)."""
    tool = TOOLS.get(name)
    if not tool:
        return False, f"ОШИБКА: функции {name} не существует."
    if _rate_limited(ctx.user_id):
        return False, "ОШИБКА: слишком много вызовов функций подряд, попроси пользователя подождать минуту."
    clean_args = {k: v for k, v in (args or {}).items() if k in tool.properties}
    try:
        result = await tool.handler(ctx, **clean_args)
    except Exception as e:
        logger.exception(f"AI tool {name} failed: {e}")
        return False, f"ОШИБКА при выполнении {name}: {type(e).__name__}: {e}"
    logger.info(f"🛠 AI tool {name}({clean_args}) → {result[:120]!r}")
    return (not result.startswith("ОШИБКА")), result


def _build_gemini_tool(gt):
    decls = []
    for t in TOOLS.values():
        params = None
        if t.properties:
            params = {"type": "OBJECT", "properties": t.properties, "required": t.required}
        decls.append(gt.FunctionDeclaration(name=t.name, description=t.description, parameters=params))
    return gt.Tool(function_declarations=decls)


async def _stream_step(client, gt, message, contents, config):
    from ai_handlers import _md_to_tg_html

    text = ""
    call_parts = []
    status = None
    last_edit = 0.0

    stream = await client.aio.models.generate_content_stream(
        model=MODEL, contents=contents, config=config,
    )
    async for chunk in stream:
        cands = getattr(chunk, "candidates", None) or []
        if not cands or not cands[0].content or not cands[0].content.parts:
            continue
        for part in cands[0].content.parts:
            if getattr(part, "function_call", None):
                call_parts.append(part)
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                text += part.text
        if text.strip():
            now = asyncio.get_running_loop().time()
            if status is None:
                status = await message.reply("▌")
                last_edit = now
            elif now - last_edit > 1.1:
                try:
                    await status.edit_text(text[:3900] + " ▌")
                    last_edit = now
                except Exception:
                    pass

    final = text.strip()[:4000]
    if status is not None:
        if final:
            try:
                await status.edit_text(_md_to_tg_html(final), parse_mode="HTML")
            except Exception:
                try:
                    await status.edit_text(final)
                except Exception:
                    pass
        else:
            try:
                await status.delete()
            except Exception:
                pass
    elif final:
        try:
            await message.reply(_md_to_tg_html(final), parse_mode="HTML")
        except Exception:
            await message.reply(final)
    return final, call_parts


async def run_ai_with_tools(
    message: types.Message,
    prompt: str,
    system_instruction: str,
    user_id: int,
) -> tuple[bool, str | None]:
    from ai_handlers import get_gemini

    client = get_gemini()
    if not client or not TOOLS_ENABLED:
        return False, None

    from google.genai import types as gt

    config = gt.GenerateContentConfig(
        system_instruction=system_instruction + TOOLS_HINT,
        tools=[_build_gemini_tool(gt)],
        automatic_function_calling=gt.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = [gt.Content(role="user", parts=[gt.Part(text=prompt)])]
    ctx = ToolContext(message=message, user_id=user_id, chat_id=message.chat.id)

    executed_any = False
    last_text = ""

    for _ in range(MAX_STEPS):
        try:
            text, call_parts = await _stream_step(client, gt, message, contents, config)
        except Exception as e:
            logger.warning(f"AI tools: ошибка запроса к Gemini: {e}")
            return (executed_any, last_text or None)

        if text:
            last_text = text
        if not call_parts:
            return True, last_text or None

        model_parts = ([gt.Part(text=text)] if text else []) + list(call_parts)
        contents.append(gt.Content(role="model", parts=model_parts))

        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
        except Exception:
            pass

        response_parts = []
        all_ok_terminal = True
        for part in call_parts[:MAX_CALLS_PER_STEP]:
            fc = part.function_call
            ok, result = await execute_tool(fc.name, dict(fc.args or {}), ctx)
            executed_any = True
            tool = TOOLS.get(fc.name)
            if not (ok and tool and tool.terminal):
                all_ok_terminal = False
            response_parts.append(gt.Part.from_function_response(
                name=fc.name, response={"result": result}))
        for part in call_parts[MAX_CALLS_PER_STEP:]:
            response_parts.append(gt.Part.from_function_response(
                name=part.function_call.name,
                response={"result": "ОШИБКА: слишком много функций за раз, не выполнено."}))
            all_ok_terminal = False

        contents.append(gt.Content(role="user", parts=response_parts))

        if all_ok_terminal:
            return True, last_text or None

    return True, last_text or None
