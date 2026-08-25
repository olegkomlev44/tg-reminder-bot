"""
anixart_handler.py — Anixart для Telegram-бота на aiogram 3.x

v3:
  - Избранное, история просмотров, подписка на новые серии (свои таблицы в db.py)
  - Привязка личного аккаунта Anixart (best-effort — см. _anixart_login)
  - Фильтр по статусу/году поверх жанра (клиентская фильтрация над проверенным
    эндпоинтом /genre/{id}/releases — без гадания об именах query-параметров)
  - Кэш постеров через file_id (переиспользует таблицу cache из db.py)
  - AI-вайб релиза через Gemini (ai_handlers.get_gemini)
  - Брендированные Pillow-карточки вместо голого фото+подпись
  - Кнопка switch_inline_query_current_chat — не нужно объяснять префикс "a:"

Команды:
  /anime <запрос>   — поиск с пагинацией
  /anime random     — случайный релиз
  /anime id <id>    — инфо по ID
  /anime genres     — жанры (+ фильтр по статусу/году)
  /anime favs       — закладки
  /anime history    — недавно просмотренное
  /anime login      — привязать аккаунт Anixart
Инлайн: @bot a:<запрос> — маршрутизируется в main.py (inline_query_router)
"""

import asyncio
import hashlib
import io
import logging
import time
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFont
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, FSInputFile, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, InlineQueryResultArticle,
    InlineQueryResultCachedPhoto, InlineQueryResultPhoto,
    InputTextMessageContent, Message,
)

from db import (
    get_cached_file_id, save_cached_file_id,
    save_anime_fav, remove_anime_fav, is_anime_fav, get_anime_favs,
    log_anime_history, get_anime_history,
    subscribe_anime, unsubscribe_anime, is_anime_subscribed, get_anime_subscriptions,
    get_all_anime_subscriptions, update_anime_sub_episodes,
    save_anixart_token, get_anixart_token, remove_anixart_token,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.anixart.tv"
USER_AGENT = (
    "AnixartApp/9.0 BETA 7-25082901 "
    "(Android 9; SDK 28; x86_64; ROG ASUS AI2201_B; ru)"
)


# ── FSM: привязка аккаунта Anixart ─────────────────────────────────────────────
class AnimeStates(StatesGroup):
    waiting_login    = State()
    waiting_password = State()


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
                logger.warning(f"Постер {url} вернул статус {r.status}")
    except Exception as e:
        logger.warning(f"Не удалось скачать постер {url}: {e}")
    return None

def _img_url(raw: str) -> str:
    if not raw: return ""
    if raw.startswith("http"): return raw
    if raw.startswith("//"): return "https:" + raw
    if "." not in raw: raw += ".jpg"
    return f"https://s.anixstatic.org/posters/{raw}"

# Поле с постером в ответах Anixart на практике встречается под разными именами
# и в разных формах — то плоская строка, то вложенный объект с несколькими
# размерами. Раньше код брал только rel.get("poster") or rel.get("image") как
# строку — если поле называется иначе или пришло объектом, _img_url тихо
# получал "" и постер никогда не прикреплялся (падало в текстовый фолбэк).
# Здесь перебираем правдоподобные варианты и логируем сырые ключи релиза, если
# вообще ничего не нашли — это покажет в логах BotHost, как поле называется
# на самом деле, и точечный фикс потом займёт одну строку.
def _extract_poster_url(rel: dict) -> str:
    for key in ("poster", "image", "image_original", "poster_image", "cover", "picture", "img"):
        val = rel.get(key)
        if not val:
            continue
        if isinstance(val, str):
            return _img_url(val)
        if isinstance(val, dict):
            for subkey in ("original", "medium", "small", "url", "path", "src", "image"):
                sub = val.get(subkey)
                if isinstance(sub, str) and sub:
                    return _img_url(sub)
    logger.warning(
        f"Не нашёл постер в релизе #{rel.get('id')} «{rel.get('title_ru')}»: "
        f"доступные ключи={sorted(rel.keys())}"
    )
    return ""


# ── Anixart: вход в личный аккаунт (best-effort) ──────────────────────────────
# У Anixart нет официального публичного API — это реверс-инжиниринг (то, что уже
# используется выше для поиска/жанров, тоже неофициальное, но проверено самим
# ботом на практике). Эндпоинт логина не задокументирован нигде публично, поэтому
# пробуем пару правдоподобных вариантов и логируем сырой ответ при неудаче.
# Если ни один не сработает — смотри лог BotHost на "Anixart login" и пришли мне
# то, что там появится, поправлю за один шаг.
async def _anixart_login(login: str, password: str) -> Optional[dict]:
    attempts = [
        ("/user/login", {"login": login, "password": password}),
        ("/auth/login", {"login": login, "password": password}),
    ]
    for path, body in attempts:
        try:
            data  = await _api_post(path, body)
            token = (
                data.get("token")
                or (data.get("profile") or {}).get("token")
                or (data.get("profileToken") or {}).get("token")
            )
            if token:
                return {"token": token}
            logger.warning(f"Anixart login {path}: ответ без поля token: {data}")
        except Exception as e:
            logger.warning(f"Anixart login через {path} не сработал: {e}")
    return None


# ── Текстовые виджеты: звёзды и прогресс-бар ──────────────────────────────────
def _stars_text(rating) -> str:
    try:
        r = float(rating or 0)
    except (TypeError, ValueError):
        r = 0.0
    n = max(0, min(5, round(r / 2)))   # рейтинг Anixart — по шкале 0-10
    return "★" * n + "☆" * (5 - n)

def _progress_bar_text(cur, total, width: int = 10) -> str:
    try:
        cur = int(cur or 0)
    except (TypeError, ValueError):
        cur = 0
    try:
        total = int(total) if total else 0
    except (TypeError, ValueError):
        total = 0
    frac = (max(0.0, min(1.0, cur / total)) if total > 0 else (1.0 if cur else 0.0))
    filled = round(frac * width)
    return "▓" * filled + "░" * (width - filled)


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
        f"⭐ <b>Рейтинг:</b> {rating}  {_stars_text(rating)}",
        f"🎬 <b>Серии:</b> {ep_rel}/{ep_tot}  {_progress_bar_text(ep_rel, rel.get('episodes_total'))}",
        f"🏷 <b>Жанры:</b> {genres}",
    ]
    if desc: lines += ["", desc]
    lines += ["", f'🔗 <a href="https://anixart.tv/release/{rid}">Открыть на сайте</a>']
    return "\n".join(lines)


# ── Брендированная карточка (Pillow) ──────────────────────────────────────────
_FONT_DIR = "/usr/share/fonts/truetype/dejavu/"

def _font(size: int, bold: bool = False):
    path = _FONT_DIR + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

_STATUS_COLORS = {
    "онгоинг": (66, 209, 122), "выходит": (66, 209, 122),
    "завершен": (90, 156, 245), "завершён": (90, 156, 245),
    "анонс": (186, 120, 245), "анонсирован": (186, 120, 245),
    "приостановлен": (245, 176, 66), "заморожен": (245, 176, 66),
}
_DEFAULT_STATUS_COLOR = (150, 150, 160)
_ACCENT = (255, 200, 60)
_CARD_W, _POSTER_H, _INFO_H = 1000, 1000, 340

def _dominant_color(img: Image.Image):
    return img.convert("RGB").resize((1, 1)).getpixel((0, 0))

def _darken(rgb, factor=0.30):
    return tuple(int(c * factor) for c in rgb)

def _wrap_lines(draw, text, font, max_width, max_lines=2):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur: lines.append(cur)
            cur = w
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    consumed = sum(len(l) + 1 for l in lines)
    if len(lines) == max_lines and consumed < len(text or ""):
        last = lines[-1]
        while draw.textlength(last + "…", font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines or [""]

def _render_anime_card_sync(poster_bytes: bytes, rel: dict) -> bytes:
    title  = rel.get("title_ru") or rel.get("title_original") or "Без названия"
    year   = rel.get("year") or "?"
    status_obj = rel.get("status")
    status = status_obj.get("name") if isinstance(status_obj, dict) else str(status_obj or "?")
    ep_rel = rel.get("episodes_released", 0) or 0
    ep_tot = rel.get("episodes_total") or None
    rating = rel.get("rating", 0) or 0

    poster = Image.open(io.BytesIO(poster_bytes)).convert("RGB")
    src_w, src_h = poster.size
    target_ratio = _CARD_W / _POSTER_H
    src_ratio = (src_w / src_h) if src_h else target_ratio
    if src_ratio > target_ratio:
        new_h, new_w = src_h, int(src_h * target_ratio)
    else:
        new_w, new_h = src_w, int(src_w / target_ratio)
    left = max(0, (src_w - new_w) // 2)
    top  = max(0, (src_h - new_h) // 2)
    poster = poster.crop((left, top, left + new_w, top + new_h)).resize((_CARD_W, _POSTER_H), Image.LANCZOS)

    panel_bg = _darken(_dominant_color(poster))
    card = Image.new("RGB", (_CARD_W, _POSTER_H + _INFO_H), panel_bg)
    card.paste(poster, (0, 0))

    # плавный переход постера в информационную панель
    fade_h = 160
    gradient = Image.new("L", (1, fade_h), 0)
    for yy in range(fade_h):
        gradient.putpixel((0, yy), int(255 * (yy / fade_h)))
    gradient = gradient.resize((_CARD_W, fade_h))
    overlay = Image.new("RGB", (_CARD_W, fade_h), panel_bg)
    region  = card.crop((0, _POSTER_H - fade_h, _CARD_W, _POSTER_H))
    card.paste(Image.composite(overlay, region, gradient), (0, _POSTER_H - fade_h))

    draw = ImageDraw.Draw(card)
    pad = 44
    y = _POSTER_H + 28

    title_font = _font(50, bold=True)
    for line in _wrap_lines(draw, title, title_font, _CARD_W - pad * 2, max_lines=2):
        draw.text((pad, y), line, font=title_font, fill=(255, 255, 255))
        y += 58

    y += 10
    meta_font = _font(30)
    dot_color = _STATUS_COLORS.get(str(status).strip().lower(), _DEFAULT_STATUS_COLOR)
    draw.ellipse((pad, y + 9, pad + 20, y + 29), fill=dot_color)
    draw.text((pad + 34, y), f"{status}  •  {year}", font=meta_font, fill=(225, 225, 230))
    y += 52

    bar_x, bar_w, bar_h = pad, _CARD_W - pad * 2 - 140, 22
    frac = 0.0
    if ep_tot:
        frac = max(0.0, min(1.0, ep_rel / ep_tot))
    elif ep_rel:
        frac = 1.0
    draw.rounded_rectangle((bar_x, y, bar_x + bar_w, y + bar_h), radius=bar_h // 2, fill=(60, 60, 70))
    if frac > 0:
        draw.rounded_rectangle((bar_x, y, bar_x + max(bar_h, int(bar_w * frac)), y + bar_h),
                                radius=bar_h // 2, fill=_ACCENT)
    draw.text((bar_x + bar_w + 16, y - 4), f"{ep_rel}/{ep_tot or '?'} эп.", font=_font(24), fill=(230, 230, 235))
    y += bar_h + 24

    star_font = _font(36)
    n = max(0, min(5, round((rating or 0) / 2)))
    star_str = "★" * n + "☆" * (5 - n)
    draw.text((pad, y), star_str, font=star_font, fill=(255, 205, 60))
    sw = draw.textlength(star_str, font=star_font)
    draw.text((pad + sw + 18, y + 4), f"{rating}/10" if rating else "—", font=_font(26), fill=(210, 210, 215))

    buf = io.BytesIO()
    card.save(buf, format="JPEG", quality=90)
    return buf.getvalue()

async def _render_anime_card(poster_bytes: bytes, rel: dict) -> Optional[bytes]:
    """Рендер идёт в отдельном потоке — Pillow синхронный и не должен блокировать event loop."""
    try:
        return await asyncio.to_thread(_render_anime_card_sync, poster_bytes, rel)
    except Exception as e:
        logger.warning(f"Не удалось собрать карточку аниме: {e}")
        return None


# ── Трейлер ───────────────────────────────────────────────────────────────────
# У Anixart нет задокументированного поля с трейлером (в разных версиях API
# видели то "trailer", то вложенный объект с youtube id — и то и другое
# ненадёжно). Поэтому: сначала пробуем достать прямую ссылку из релиза (если
# она там есть), а если нет — ищем официальный трейлер на YouTube по названию
# и качаем через тот же yt-dlp пайплайн, что уже используется в
# media_downloader.py (лимит на размер файла общий — MAX_VIDEO_BYTES).
def _trailer_url_from_release(rel: dict) -> Optional[str]:
    direct = rel.get("trailer") or rel.get("trailer_url") or rel.get("video_trailer")
    if isinstance(direct, str) and direct.startswith("http"):
        return direct
    if isinstance(direct, dict):
        yt_id = direct.get("youtube_id") or direct.get("id") or direct.get("video_id")
        url = direct.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
        if yt_id:
            return f"https://www.youtube.com/watch?v={yt_id}"
    return None

async def _fetch_trailer(rel: dict) -> tuple[Optional[str], dict]:
    """Возвращает (путь_к_файлу, info) как media_downloader._download_media,
    или (None, {...}) с описанием причины неудачи."""
    from media_downloader import _download_media  # переиспользуем yt-dlp пайплайн

    title = rel.get("title_ru") or rel.get("title_original") or ""
    direct_url = _trailer_url_from_release(rel)
    query_url = direct_url or f"ytsearch1:{title} трейлер аниме"
    try:
        return await _download_media(query_url, "youtube")
    except Exception as e:
        logger.warning(f"Не удалось скачать трейлер для «{title}»: {e}")
        return None, {"_error": str(e)[:200]}


# ── Ссылки на конкретные серии ─────────────────────────────────────────────────
# У anixart.tv нет задокументированного deep-link'а на конкретную серию — сайт
# сам решает, как открыть плеер после клика по номеру серии в приложении.
# Ниже — самый распространённый шаблон среди подобных сайтов (release/{id}/episode/{n}).
# Если у тебя он не сработает (просто откроет страницу релиза без конкретной
# серии) — пришли мне реальную ссылку на серию, скопированную из браузера или
# из "Поделиться" в приложении, и я поправлю константу за один шаг, без
# переписывания остальной логики.
EPISODE_URL_TEMPLATE = "https://anixart.tv/release/{rid}/episode/{ep}"

def _episode_url(rid: int, ep: int) -> str:
    return EPISODE_URL_TEMPLATE.format(rid=rid, ep=ep)

_EP_PAGE_SIZE = 30

def _episode_keyboard(rid: int, ep_total: int, offset: int = 0) -> InlineKeyboardMarkup:
    page = list(range(offset + 1, min(ep_total, offset + _EP_PAGE_SIZE) + 1))
    rows, row = [], []
    for ep in page:
        row.append(InlineKeyboardButton(text=str(ep), url=_episode_url(rid, ep)))
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Назад", callback_data=f"anix:eplist:{rid}:{max(0, offset - _EP_PAGE_SIZE)}"
        ))
    if offset + _EP_PAGE_SIZE < ep_total:
        nav.append(InlineKeyboardButton(
            text="Ещё ➡️", callback_data=f"anix:eplist:{rid}:{offset + _EP_PAGE_SIZE}"
        ))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Клавиатуры ────────────────────────────────────────────────────────────────
def _release_keyboard(rel: dict, user_id: int) -> InlineKeyboardMarkup:
    rid = rel.get("id", 0)
    fav = is_anime_fav(user_id, rid)
    sub = is_anime_subscribed(user_id, rid)
    rows = [
        [
            InlineKeyboardButton(text=("💔 Убрать" if fav else "🤍 В закладки"), callback_data=f"anix:fav:{rid}"),
            InlineKeyboardButton(text=("🔕 Отписаться" if sub else "🔔 Новые серии"), callback_data=f"anix:sub:{rid}"),
        ],
        [
            InlineKeyboardButton(text="🤖 Вайб-чек (AI)", callback_data=f"anix:vibe:{rid}"),
            InlineKeyboardButton(text="🎬 Трейлер", callback_data=f"anix:trailer:{rid}"),
        ],
        [InlineKeyboardButton(text="▶️ Серии", callback_data=f"anix:eplist:{rid}:0")],
        [
            InlineKeyboardButton(text="🌐 Открыть", url=f"https://anixart.tv/release/{rid}"),
            InlineKeyboardButton(text="🔀 Ещё случайный", callback_data="anix:random"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔎 Искать аниме инлайн", switch_inline_query_current_chat="a:")
    ]])


async def _send_release(ctx, rel: dict):
    rid     = rel.get("id", 0)
    user_id = ctx.from_user.id
    caption = _fmt_release(rel)
    kb      = _release_keyboard(rel, user_id)
    chat_id = ctx.message.chat.id if isinstance(ctx, CallbackQuery) else ctx.chat.id
    bot     = ctx.bot

    log_anime_history(user_id, rel)

    poster_url = _extract_poster_url(rel)
    if poster_url:
        data = await _download_poster(poster_url)
        if data:
            card_bytes = await _render_anime_card(data, rel)
            fname = f"anix_{hashlib.md5(poster_url.encode()).hexdigest()[:8]}.jpg"
            try:
                sent = await bot.send_photo(
                    chat_id, BufferedInputFile(card_bytes or data, fname),
                    caption=caption, parse_mode="HTML", reply_markup=kb
                )
                # прогреваем кэш file_id для инлайна (переиспользует таблицу cache)
                if sent.photo:
                    save_cached_file_id(f"anix_poster_{rid}", sent.photo[-1].file_id)
                return
            except Exception as e:
                logger.warning(f"Не удалось отправить фото: {e}")
    await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)


# ── Кэшированные запросы к API ─────────────────────────────────────────────────
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


# ── Жанры + фильтр по статусу/году ────────────────────────────────────────────
# У Anixart, судя по всему, есть отдельный эндпоинт /filter с параметрами —
# но он нигде публично не задокументирован, а гадать с именами параметров
# рискованно (вернёт нефильтрованные результаты молча, без ошибки — хуже, чем
# просто не работать). Поэтому фильтруем на своей стороне поверх уже проверенного
# /genre/{id}/releases: тянем несколько страниц, кэшируем пул на 10 минут, и сами
# выводим варианты статуса/года ИЗ РЕАЛЬНЫХ данных — так фильтр не может «соврать».
async def _fetch_genre_pool(gid: int) -> list:
    key = f"genre_pool:{gid}"
    cached = await _cache_get(key)
    if cached is not None:
        return cached
    pool = []
    for offset in (0, 10, 20, 30):
        try:
            data  = await _api_get(f"/genre/{gid}/releases/{offset}")
            chunk = data.get("content") or []
        except Exception:
            break
        if not chunk:
            break
        pool.extend(chunk)
        if len(chunk) < 10:
            break
    await _cache_set(key, pool, ttl=600)
    return pool

def _distinct_statuses(pool: list) -> list:
    seen, out = set(), []
    for item in pool:
        st = item.get("status")
        name = st.get("name") if isinstance(st, dict) else str(st or "")
        if name and name not in seen:
            seen.add(name); out.append(name)
    return out

def _distinct_years(pool: list) -> list:
    return sorted({item.get("year") for item in pool if item.get("year")}, reverse=True)

def _filter_pool(pool: list, status_idx: int, year: int, statuses: list) -> list:
    out = pool
    if 0 < status_idx <= len(statuses):
        target = statuses[status_idx - 1]
        out = [i for i in out if (
            (i.get("status").get("name") if isinstance(i.get("status"), dict) else str(i.get("status") or ""))
            == target
        )]
    if year:
        out = [i for i in out if i.get("year") == year]
    return out

async def _render_genre_page(msg: Message, gid: int, status_idx: int, year: int, offset: int):
    try:
        pool = await _fetch_genre_pool(gid)
        if not pool:
            await msg.edit_text("😔 По этому жанру ничего не найдено."); return

        statuses = _distinct_statuses(pool)
        years    = _distinct_years(pool)
        filtered = _filter_pool(pool, status_idx, year, statuses)
        page     = filtered[offset:offset + 10]

        buttons = []
        for item in page:
            title = (item.get("title_ru") or item.get("title_original") or "Без названия")[:40]
            y_    = item.get("year") or "?"
            rid   = item.get("id", 0)
            buttons.append([InlineKeyboardButton(text=f"{title} ({y_})", callback_data=f"anix:info:{rid}")])

        status_row = [InlineKeyboardButton(
            text=("• Все статусы" if status_idx == 0 else "Все статусы"),
            callback_data=f"anix:gf:{gid}:0:{year}:0"
        )]
        for i, s in enumerate(statuses[:3], start=1):
            label = f"• {s}" if i == status_idx else s
            status_row.append(InlineKeyboardButton(text=label[:16], callback_data=f"anix:gf:{gid}:{i}:{year}:0"))
        buttons.append(status_row)

        if years:
            year_row = [InlineKeyboardButton(
                text=("• Любой год" if year == 0 else "Любой год"),
                callback_data=f"anix:gf:{gid}:{status_idx}:0:0"
            )]
            for y_ in years[:4]:
                label = f"• {y_}" if y_ == year else str(y_)
                year_row.append(InlineKeyboardButton(text=label, callback_data=f"anix:gf:{gid}:{status_idx}:{y_}:0"))
            buttons.append(year_row)

        if len(filtered) > offset + 10:
            buttons.append([InlineKeyboardButton(
                text="⏩ Ещё", callback_data=f"anix:gf:{gid}:{status_idx}:{year}:{offset + 10}"
            )])
        buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])

        suffix = f" (стр. {offset // 10 + 1})" if offset else ""
        text = f"🎭 Найдено {len(filtered)} аниме{suffix}:" if filtered else "😔 Ничего не подходит под этот фильтр."
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        try:
            await msg.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        except Exception:
            pass


# ── Хэндлеры команд ──────────────────────────────────────────────────────────
router = Router(name="anixart")

HELP_TEXT = (
    "<b>🎬 Anixart — поиск аниме</b>\n\n"
    "<code>/anime &lt;запрос&gt;</code> — поиск\n"
    "<code>/anime random</code> — случайный релиз\n"
    "<code>/anime id &lt;id&gt;</code> — инфо по ID\n"
    "<code>/anime genres</code> — жанры (+ фильтр по статусу/году)\n"
    "<code>/anime favs</code> — твои закладки\n"
    "<code>/anime history</code> — недавно смотрел(а)\n"
    "<code>/anime login</code> — привязать аккаунт Anixart\n\n"
    "<i>Инлайн:</i> @botname <code>a:&lt;запрос&gt;</code> — или кнопка ниже 👇"
)

@router.message(Command("anime"))
async def cmd_anime(msg: Message, state: FSMContext):
    args  = (msg.text or "").split(maxsplit=1)
    query = args[1].strip() if len(args) > 1 else ""
    if not query:
        await msg.answer(HELP_TEXT, parse_mode="HTML", reply_markup=_help_keyboard()); return
    parts = query.split(maxsplit=1)
    sub   = parts[0].lower()
    if sub == "random":              await _do_random(msg)
    elif sub == "id" and len(parts) > 1 and parts[1].strip().isdigit():
        await _do_info(msg, int(parts[1].strip()))
    elif sub == "genres":            await _do_genres(msg)
    elif sub in ("favs", "favorites", "избранное", "закладки"):  await _do_favs(msg)
    elif sub in ("history", "история"):                          await _do_history(msg)
    elif sub in ("login", "аккаунт", "account"):                 await _start_anime_login(msg, state)
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


async def _do_favs(ctx):
    user_id = ctx.from_user.id
    favs = get_anime_favs(user_id)
    if not favs:
        await _reply(ctx, "🤍 Пока нет закладок. Открой любой релиз и нажми «В закладки»."); return
    buttons = [[InlineKeyboardButton(text=f['title'][:40] or f"#{f['id']}", callback_data=f"anix:info:{f['id']}")]
               for f in favs[:20]]
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    await _reply_kb(ctx, f"<b>❤️ Твои закладки ({len(favs)}):</b>",
                    InlineKeyboardMarkup(inline_keyboard=buttons))


async def _do_history(ctx):
    user_id = ctx.from_user.id
    hist = get_anime_history(user_id, limit=20)
    if not hist:
        await _reply(ctx, "👀 История пока пуста."); return
    buttons = [[InlineKeyboardButton(text=h['title'][:40] or f"#{h['id']}", callback_data=f"anix:info:{h['id']}")]
               for h in hist]
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="anix:close")])
    await _reply_kb(ctx, "<b>👀 Недавно смотрел(а):</b>",
                    InlineKeyboardMarkup(inline_keyboard=buttons))


# ── Привязка аккаунта Anixart ──────────────────────────────────────────────────
async def _start_anime_login(ctx, state: FSMContext):
    existing = get_anixart_token(ctx.from_user.id)
    if existing:
        text = (
            f"🔗 Уже привязан аккаунт <b>{existing['login']}</b>.\n"
            "Напиши /anime login ещё раз, чтобы перепривязать, "
            "или используй /anime_unlink, чтобы отвязать."
        )
        await ctx.answer(text, parse_mode="HTML"); return
    text = (
        "🔗 <b>Привязка аккаунта Anixart</b>\n\n"
        "Это неофициальная функция — у Anixart нет публичного API, вход идёт "
        "напрямую логином/паролем, как в самом приложении.\n"
        "Пароль <b>нигде не сохраняется</b>: бот обменивает его на токен и сразу "
        "забывает, хранится только токен. Сообщение с паролем бот удалит сам.\n\n"
        "Напиши логин или e-mail от Anixart (или «отмена»):"
    )
    await ctx.answer(text, parse_mode="HTML")
    await state.set_state(AnimeStates.waiting_login)

@router.message(AnimeStates.waiting_login)
async def process_anime_login(message: Message, state: FSMContext):
    login = (message.text or "").strip()
    if login.lower() in ("отмена", "/cancel", "cancel"):
        await state.clear(); await message.answer("Отменено."); return
    await state.update_data(login=login)
    await state.set_state(AnimeStates.waiting_password)
    await message.answer("Теперь напиши пароль (сообщение с ним я сразу удалю):")

@router.message(AnimeStates.waiting_password)
async def process_anime_password(message: Message, state: FSMContext):
    data     = await state.get_data()
    login    = data.get("login", "")
    password = (message.text or "").strip()
    user_id  = message.from_user.id
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    if password.lower() in ("отмена", "/cancel", "cancel"):
        await message.answer("Отменено."); return

    wait = await message.answer("🔐 Пробую войти…")
    result = await _anixart_login(login, password)
    password = None  # дальше не используется и никуда не сохраняется
    if result and result.get("token"):
        save_anixart_token(user_id, result["token"], login)
        await wait.edit_text(f"✅ Аккаунт <b>{login}</b> привязан.", parse_mode="HTML")
    else:
        await wait.edit_text(
            "❌ Не получилось войти. Формат запроса — предположение по открытым "
            "обёрткам Anixart API (официальной документации нет). Если логин и "
            "пароль точно верные, глянь в логах BotHost строку «Anixart login» "
            "и пришли её мне — поправлю запрос за один шаг."
        )

@router.message(Command("anime_unlink"))
async def cmd_anime_unlink(message: Message):
    remove_anixart_token(message.from_user.id)
    await message.answer("🔓 Аккаунт Anixart отвязан.")


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
    gid = int(cq.data.split(":")[2])
    await cq.answer("🔍 Ищу…")
    wait = await cq.message.answer("🔍 Загружаю аниме по жанру…")
    await _render_genre_page(wait, gid, status_idx=0, year=0, offset=0)

@router.callback_query(F.data.startswith("anix:gf:"))
async def cb_genre_filtered(cq: CallbackQuery):
    # anix:gf:{gid}:{status_idx}:{year}:{offset}
    _, _, gid, status_idx, year, offset = cq.data.split(":")
    await cq.answer()
    await _render_genre_page(cq.message, int(gid), int(status_idx), int(year), int(offset))

@router.callback_query(F.data.startswith("anix:fav:"))
async def cb_fav_toggle(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    user_id = cq.from_user.id
    if is_anime_fav(user_id, rid):
        remove_anime_fav(user_id, rid)
        await cq.answer("💔 Убрано из закладок")
    else:
        rel = await _get_release(rid) or {"id": rid, "title": ""}
        save_anime_fav(user_id, rel)
        await cq.answer("❤️ В закладках!")
    rel = await _get_release(rid) or {"id": rid}
    try:
        await cq.message.edit_reply_markup(reply_markup=_release_keyboard(rel, user_id))
    except Exception:
        pass

@router.callback_query(F.data.startswith("anix:sub:"))
async def cb_sub_toggle(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    user_id = cq.from_user.id
    if is_anime_subscribed(user_id, rid):
        unsubscribe_anime(user_id, rid)
        await cq.answer("🔕 Подписка на новые серии отменена")
    else:
        rel = await _get_release(rid) or {"id": rid, "title": ""}
        subscribe_anime(user_id, rel)
        await cq.answer("🔔 Подписался! Напишу, когда выйдет новая серия")
    rel = await _get_release(rid) or {"id": rid}
    try:
        await cq.message.edit_reply_markup(reply_markup=_release_keyboard(rel, user_id))
    except Exception:
        pass

@router.callback_query(F.data.startswith("anix:vibe:"))
async def cb_vibe(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer("🤖 Думаю…")
    rel = await _get_release(rid)
    if not rel:
        await cq.message.answer("❌ Не нашёл этот релиз."); return

    from ai_handlers import get_gemini
    client = get_gemini()
    if not client:
        await cq.message.answer("🤖 AI сейчас недоступен."); return

    title = rel.get("title_ru") or rel.get("title_original") or "?"
    desc  = (rel.get("description") or "")[:600]
    genres_raw = rel.get("genres") or []
    genres = ", ".join((g.get("name") if isinstance(g, dict) else str(g)) for g in genres_raw[:5])
    try:
        prompt = (
            f"Аниме «{title}». Жанры: {genres or '—'}.\n"
            f"Официальное описание: {desc or 'нет описания'}\n\n"
            "Напиши короткую вайб-характеристику (2-3 предложения, БЕЗ СПОЙЛЕРОВ, "
            "без вступлений вроде «это аниме про»): атмосфера, кому зайдёт, на что похоже. "
            "Стиль неформальный, зумерский, без смайликов через слово."
        )
        response = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = (response.text or "").strip()
        if text:
            await cq.message.answer(f"🤖 <b>Вайб-чек:</b>\n{text}", parse_mode="HTML")
        else:
            await cq.message.answer("🤖 AI не смог сформулировать ответ, попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Ошибка AI-вайба: {e}")
        await cq.message.answer("❌ AI сейчас недоступен, попробуй позже.")

@router.callback_query(F.data.startswith("anix:trailer:"))
async def cb_trailer(cq: CallbackQuery):
    rid = int(cq.data.split(":")[2])
    await cq.answer("🎬 Ищу трейлер…")
    rel = await _get_release(rid)
    if not rel:
        await cq.message.answer("❌ Не нашёл этот релиз."); return

    title = rel.get("title_ru") or rel.get("title_original") or "?"
    status = await cq.message.answer(f"🎬 Ищу трейлер «{title}»…")

    from media_downloader import MAX_VIDEO_MB, _cleanup  # общие константы/утилита очистки
    filepath, info = await _fetch_trailer(rel)

    if not filepath:
        if info.get("_too_large"):
            text = f"❌ Трейлер нашёлся, но весит больше {MAX_VIDEO_MB} МБ — Telegram не пропустит."
        else:
            text = "😔 Не нашёл трейлер для этого релиза. Попробуй поискать вручную на YouTube."
        try:
            await status.edit_text(text)
        except Exception:
            await cq.message.answer(text)
        return

    try:
        await status.edit_text("🎬 Загружаю трейлер в Telegram…")
    except Exception:
        pass

    try:
        video_file = FSInputFile(filepath, filename="trailer.mp4")
        await cq.message.answer_video(
            video_file,
            caption=f"🎬 Трейлер: <b>{title}</b>",
            parse_mode="HTML",
            width=info.get("width") or None,
            height=info.get("height") or None,
            duration=int(info.get("duration") or 0) or None,
            supports_streaming=True,
        )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Не удалось отправить трейлер: {e}")
        try:
            await status.edit_text(f"❌ Не удалось отправить трейлер: <code>{str(e)[:150]}</code>", parse_mode="HTML")
        except Exception:
            pass
    finally:
        _cleanup(filepath, info)

@router.callback_query(F.data.startswith("anix:eplist:"))
async def cb_episodes(cq: CallbackQuery):
    # anix:eplist:{rid}:{offset}
    _, _, rid_s, offset_s = cq.data.split(":")
    rid, offset = int(rid_s), int(offset_s)
    await cq.answer()

    rel = await _get_release(rid)
    if not rel:
        await cq.message.answer("❌ Не нашёл этот релиз."); return

    ep_total = rel.get("episodes_released") or 0
    if not ep_total:
        await cq.message.answer("😔 Пока нет вышедших серий."); return

    title = rel.get("title_ru") or rel.get("title_original") or "?"
    kb = _episode_keyboard(rid, ep_total, offset)
    text = f"▶️ <b>{title}</b>\nВыбери серию ({ep_total} доступно):"

    # Если это уже клавиатура серий — редактируем (листание), иначе шлём новым сообщением
    if cq.message.text and cq.message.text.startswith("▶️"):
        try:
            await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await cq.message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "anix:close")
async def cb_close(cq: CallbackQuery):
    await cq.answer()
    try: await cq.message.delete()
    except: pass


# ── Inline с фото ─────────────────────────────────────────────────────────────
async def inline_anime(iq: InlineQuery):
    """Срабатывает на запросы вида 'a:<название>'. Вызывается напрямую из
    inline_query_router в main.py — НЕ регистрируй это через @router.inline_query,
    иначе вернётся конфликт с музыкальным инлайном, который чинили отдельно."""
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
        poster_url = _extract_poster_url(item)

        text = (
            f"<b>{title_ru}</b>\n"
            + (f"<i>{title_orig}</i>\n\n" if title_orig and title_orig != title_ru else "\n")
            + f"📅 {year} | 📊 {status}\n"
            f"⭐ {rating} {_stars_text(rating)} | 🎬 {ep_rel}/{ep_tot} эп.\n\n"
            f"🔗 https://anixart.tv/release/{rid}"
        )
        desc = f"{status} • {ep_rel}/{ep_tot} эп. • ⭐{rating}"

        # переиспользуем таблицу cache: если постер этого релиза уже когда-то
        # уходил в чат через _send_release, у нас есть его file_id — так быстрее
        # и не зависит от доступности anixstatic на каждый инлайн-показ.
        cached_fid = get_cached_file_id(f"anix_poster_{rid}")
        if cached_fid:
            results.append(InlineQueryResultCachedPhoto(
                id=str(rid), photo_file_id=cached_fid,
                title=f"{title_ru} ({year})", description=desc,
                caption=text, parse_mode="HTML",
            ))
        elif poster_url:
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


# ── Крон: новые серии по подпискам ────────────────────────────────────────────
async def check_anime_episodes(bot):
    """Раз в несколько часов (регистрируется в main.py через scheduler) проверяет
    подписки всех пользователей и пишет тем, у кого вышла новая серия."""
    subs = get_all_anime_subscriptions()
    for sub in subs:
        rid = sub["id"]
        rel = await _get_release(rid)
        if not rel:
            continue
        new_ep = rel.get("episodes_released", 0) or 0
        if new_ep > (sub["last_episodes"] or 0):
            title = rel.get("title_ru") or sub["title"] or "?"
            try:
                await bot.send_message(
                    sub["user_id"],
                    f"🔔 <b>{title}</b>\nВышла новая серия! Теперь доступно: {new_ep} эп.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="👀 Открыть", url=f"https://anixart.tv/release/{rid}")
                    ]])
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить {sub['user_id']} о «{title}»: {e}")
        update_anime_sub_episodes(sub["user_id"], rid, new_ep)


# ── Утилиты ───────────────────────────────────────────────────────────────────
async def _reply(ctx, text: str) -> Message:
    if isinstance(ctx, CallbackQuery):
        return await ctx.message.answer(text)
    return await ctx.answer(text)

async def _reply_kb(ctx, text: str, kb: InlineKeyboardMarkup) -> Message:
    if isinstance(ctx, CallbackQuery):
        return await ctx.message.answer(text, parse_mode="HTML", reply_markup=kb)
    return await ctx.answer(text, parse_mode="HTML", reply_markup=kb)


def register_anixart_handlers(dp):
    dp.include_router(router)
    logger.info("🎬 anixart_handler зарегистрирован (v3: закладки, история, подписки, AI, карточки)")
