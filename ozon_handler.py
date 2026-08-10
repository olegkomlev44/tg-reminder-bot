"""
ozon_handler.py — Поиск товаров на Ozon для Telegram-бота на aiogram 3.x
Переработан из плагина OzonSearch для exteraGram.

⚠️  У Ozon нет публичного API. Плагин работает с теми же JSON-эндпоинтами,
    которые использует сайт. Ozon активно защищается от ботов, поэтому:
    - запросы идут с браузерными заголовками
    - реализован каскад из 4 API-эндпоинтов + HTML-fallback
    - при блокировке присылается ссылка на ручной поиск

Команды:
  /ozon <запрос>          — поиск (до 5 товаров)
  /ozon <запрос> до 5000  — с максимальной ценой
  /ozon <запрос> от 1000  — с минимальной ценой
  /ozon <запрос> дешёвые  — сортировка по цене
  /ozon <запрос> новинки  — сортировка по новизне
  /ozon <запрос> скидки   — сортировка по скидке

Подключение в main.py:
    from ozon_handler import register_ozon_handlers
    register_ozon_handlers(dp)
"""

import asyncio
import json
import logging
import re
import time
from html import unescape as html_unescape
from typing import Any, List, Optional
from urllib.parse import urlencode

import aiohttp
from aiogram import Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────
OZON_BASE = "https://www.ozon.ru"

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

API_ENDPOINTS = [
    ("entry-www",  "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"),
    ("comp-www",   "https://www.ozon.ru/api/composer-api.bx/page/json/v2"),
    ("entry-api",  "https://api.ozon.ru/entrypoint-api.bx/page/json/v2"),
    ("comp-api",   "https://api.ozon.ru/composer-api.bx/page/json/v2"),
]

SORT_WORDS = {
    "дешёвые": "price",  "дешевые": "price",  "дешево": "price",
    "дёшево": "price",   "подешевле": "price", "cheap": "price",
    "дорогие": "price_desc", "подороже": "price_desc",
    "новинки": "new",    "новые": "new",       "new": "new",
    "рейтинг": "rating", "рейтингу": "rating",
    "скидки":  "discount", "скидкой": "discount",
    "популярные": None,
}

MAX_RESULTS = 5   # товаров в ответе по умолчанию
MAX_PAGES   = 2   # страниц выдачи максимум
TIMEOUT     = 15  # секунд

NUM_RE   = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(к|k|тыс|т)?$", re.I)
RANGE_RE = re.compile(r"^(\d+)\s*[-–—]\s*(\d+)$")


# ── Парсер запроса ────────────────────────────────────────────────────────────
class ParsedQuery:
    __slots__ = ("text", "price_min", "price_max", "sort", "min_rating", "min_reviews")

    def __init__(self):
        self.text        = ""
        self.price_min   = None
        self.price_max   = None
        self.sort        = None
        self.min_rating  = None
        self.min_reviews = None


def _to_amount(token: str) -> Optional[int]:
    t = token.replace("\u00a0", "").replace(" ", "").lower()
    m = NUM_RE.match(t)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    if m.group(2):
        val *= 1000
    val = int(round(val))
    return val if 0 < val < 100_000_000 else None


def parse_query(raw: str) -> ParsedQuery:
    p      = ParsedQuery()
    tokens = raw.split()
    used   = [False] * len(tokens)

    def nxt_amount(i: int) -> Optional[int]:
        for j in range(i + 1, min(i + 3, len(tokens))):
            if used[j]:
                continue
            val = _to_amount(tokens[j])
            if val is not None:
                used[j] = True
                return val
            return None
        return None

    for i, tok in enumerate(tokens):
        if used[i]:
            continue
        low = tok.lower().replace("ё", "е")

        if low in ("до", "макс", "максимум", "<", "<="):
            val = nxt_amount(i)
            if val is not None:
                p.price_max = val
                used[i] = True
                continue

        if low in ("от", "мин", "минимум", ">", ">="):
            val = nxt_amount(i)
            if val is not None:
                p.price_min = val
                used[i] = True
                continue

        m = RANGE_RE.match(low)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a < b and b > 100:
                p.price_min, p.price_max = a, b
                used[i] = True
                continue

        if low in ("оценка", "оценкой", "звезд", "звезд", "rating"):
            for j in range(i + 1, min(i + 3, len(tokens))):
                if used[j]:
                    continue
                try:
                    val = float(tokens[j].replace(",", "."))
                except ValueError:
                    break
                if 0 < val <= 5:
                    p.min_rating = val
                    used[i] = used[j] = True
                break
            if used[i]:
                continue

        if low in ("отзывов", "отзывы", "отзыва", "reviews"):
            for j in range(i + 1, min(i + 3, len(tokens))):
                if used[j]:
                    continue
                digits = re.sub(r"\D", "", tokens[j])
                if digits:
                    p.min_reviews = int(digits)
                    used[i] = used[j] = True
                break
            if used[i]:
                continue

        if low in SORT_WORDS:
            p.sort = SORT_WORDS[low]
            used[i] = True
            continue

    p.text = " ".join(tokens[i] for i in range(len(tokens)) if not used[i]).strip()
    return p


# ── URL построитель ───────────────────────────────────────────────────────────
def _build_url(p: ParsedQuery, page: int) -> str:
    params = [("text", p.text), ("page", str(page)), ("from_global", "true")]
    if p.sort:
        params.append(("sorting", p.sort))
    if p.price_min:
        params.append(("pricefrom", str(p.price_min)))
    if p.price_max:
        params.append(("priceto", str(p.price_max)))
    return "/search?" + urlencode(params)


# ── Сетевой слой ──────────────────────────────────────────────────────────────
def _base_headers(ua: str) -> dict:
    return {
        "User-Agent":      ua,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer":         OZON_BASE + "/",
        "Origin":          OZON_BASE,
        "DNT":             "1",
    }


async def _fetch_json(session: aiohttp.ClientSession, api_url: str, page_url: str, ua: str) -> Optional[Any]:
    params  = {"url": page_url}
    headers = _base_headers(ua)
    try:
        async with session.get(
            api_url,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ssl=False,
        ) as resp:
            if resp.status != 200:
                return None
            ct = resp.content_type or ""
            if "json" in ct:
                return await resp.json(content_type=None)
            text = await resp.text()
            return json.loads(text)
    except Exception:
        return None


async def _fetch_html(session: aiohttp.ClientSession, page_url: str) -> List[dict]:
    """Фолбэк: парсим data-state из HTML страницы поиска."""
    url = OZON_BASE + page_url
    try:
        async with session.get(
            url,
            headers=_base_headers(UA_DESKTOP),
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ssl=False,
        ) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
    except Exception:
        return []

    # ищем data-state JSON в тегах скриптов
    items = []
    for m in re.finditer(r'data-state="([^"]+)"', text):
        try:
            blob = json.loads(html_unescape(m.group(1)))
            items.extend(_extract_products(blob))
        except Exception:
            pass
    return items


# ── Извлечение товаров из JSON ────────────────────────────────────────────────
def _iter_dicts(node, depth=0):
    if depth > 25:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v, depth + 1)
    elif isinstance(node, list):
        for el in node:
            yield from _iter_dicts(el, depth + 1)


def _collect_strings(node, limit=200) -> List[str]:
    out = []
    for d in _iter_dicts(node):
        for v in d.values():
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
                if len(out) >= limit:
                    return out
    return out


def _price_to_int(v) -> Optional[int]:
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"\D", "", v)
        return int(digits) if digits else None
    return None


def _fmt_money(val: int) -> str:
    return f"{val:,}".replace(",", "\u00a0") + " ₽"


def _item_link(node: dict) -> str:
    for key in ("link", "url", "action", "deepLink"):
        v = node.get(key)
        if isinstance(v, str) and v.startswith("/"):
            return OZON_BASE + v.split("?")[0]
        if isinstance(v, str) and v.startswith("http"):
            return v.split("?")[0]
        if isinstance(v, dict):
            href = v.get("link") or v.get("url") or ""
            if href.startswith("/"):
                return OZON_BASE + href.split("?")[0]
    return ""


def _slug_title(slug: str) -> str:
    parts = slug.split("-")
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(p.capitalize() for p in parts if p)


def _pick_title(strings: List[str], fallback: str) -> str:
    for s in strings:
        if 8 < len(s) < 140 and not re.search(r"\d{5,}|\bhttp\b|₽|\$", s):
            return s
    return fallback


def _prices_from_node(node: dict, strings: List[str]):
    price = old_price = None
    for key in ("price", "cardPrice", "finalPrice", "salePrice", "offerPrice",
                "originalPrice", "pricePerItem"):
        v = _price_to_int(node.get(key))
        if v and 50 < v < 50_000_000:
            price = v
            break
    for key in ("originalPrice", "crossedPrice", "oldPrice", "strikethroughPrice",
                "marketplaceSellerId"):
        v = _price_to_int(node.get(key))
        if v and 50 < v < 50_000_000 and (not price or v > price):
            old_price = v
            break
    if not price:
        # ищем в строках вида "1 234 ₽"
        for s in strings:
            m = re.search(r"(\d[\d\s]{1,8}\d)\s*₽", s)
            if m:
                v = _price_to_int(m.group(1))
                if v and 50 < v < 50_000_000:
                    price = v
                    break
    return price, old_price


def _rating_from_strings(strings: List[str]) -> Optional[float]:
    for s in strings:
        m = re.match(r"^([1-5](?:[.,]\d)?)$", s.strip())
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                pass
    return None


def _is_ad(node: dict) -> bool:
    return bool(node.get("isAdult") or node.get("advertisingCode") or node.get("adv"))


def _extract_products(data: Any) -> List[dict]:
    items = []
    seen  = set()
    for node in _iter_dicts(data):
        link = _item_link(node)
        if not link or link in seen:
            continue
        m = re.search(r"/product/([^/?#]+)", link)
        if not m:
            continue
        slug = m.group(1)
        strings = _collect_strings(node)
        price, old_price = _prices_from_node(node, strings)
        title = _pick_title(strings, _slug_title(slug))
        if len(title) < 5:
            continue
        nid = node.get("id") or node.get("skuId") or node.get("itemId") or slug
        seen.add(link)
        items.append({
            "id":        str(nid),
            "title":     title,
            "price":     price,
            "old_price": old_price,
            "rating":    _rating_from_strings(strings),
            "reviews":   None,
            "link":      link,
            "is_ad":     _is_ad(node),
        })
    return items


# ── Основной поиск ────────────────────────────────────────────────────────────
async def search_ozon(p: ParsedQuery, want: int = MAX_RESULTS) -> List[dict]:
    raw_items: List[dict] = []
    seen_ids: set = set()

    connector = aiohttp.TCPConnector(ssl=False, limit=4)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Прогрев (главная страница)
        try:
            async with session.get(
                OZON_BASE,
                headers=_base_headers(UA_DESKTOP),
                timeout=aiohttp.ClientTimeout(total=8),
                ssl=False,
            ) as _:
                pass
        except Exception:
            pass

        for page in range(1, MAX_PAGES + 1):
            page_url = _build_url(p, page)
            page_items: List[dict] = []

            # Пробуем API-эндпоинты по очереди
            for name, api_url in API_ENDPOINTS:
                data = await _fetch_json(session, api_url, page_url, UA_DESKTOP)
                if data:
                    page_items = _extract_products(data)
                    if page_items:
                        logger.debug(f"Ozon [{name}] page={page}: {len(page_items)} товаров")
                        break

            # HTML-фолбэк
            if not page_items:
                page_items = await _fetch_html(session, page_url)
                logger.debug(f"Ozon [html] page={page}: {len(page_items)} товаров")

            if not page_items:
                break

            for it in page_items:
                if it["id"] not in seen_ids:
                    seen_ids.add(it["id"])
                    raw_items.append(it)

            if len(raw_items) >= want * 2:
                break

    return _filter_items(raw_items, p)[:want]


def _filter_items(items: List[dict], p: ParsedQuery) -> List[dict]:
    out = []
    for it in items:
        if it.get("is_ad"):
            continue
        if not it.get("title"):
            continue
        price = it.get("price")
        if not price:
            continue
        if p.price_min and price < p.price_min:
            continue
        if p.price_max and price > p.price_max:
            continue
        if p.min_rating:
            r = it.get("rating")
            if not r or r < p.min_rating - 1e-9:
                continue
        if p.min_reviews:
            rv = it.get("reviews") or 0
            if rv < p.min_reviews:
                continue
        out.append(it)
    return out


# ── Форматирование ────────────────────────────────────────────────────────────
def _fmt_item(i: int, it: dict, title_len: int = 70) -> str:
    title = it.get("title", "")[:title_len]
    price = it.get("price")
    old   = it.get("old_price")
    rating = it.get("rating")
    link  = it.get("link", "")

    lines = [f"<b>{i}. {title}</b>"]

    price_str = _fmt_money(price) if price else "—"
    if old and old > price:
        try:
            pct = int(round((1 - price / old) * 100))
        except (ZeroDivisionError, TypeError):
            pct = 0
        price_str += f" <s>{_fmt_money(old)}</s>"
        if pct > 0:
            price_str += f" −{pct}%"
    lines.append(f"💰 {price_str}")

    if rating:
        lines.append(f"⭐ {rating:g}")

    if link:
        lines.append(f"🔗 {link}")

    return "\n".join(lines)


def _filters_suffix(p: ParsedQuery) -> str:
    parts = []
    if p.price_min:
        parts.append(f"от {_fmt_money(p.price_min)}")
    if p.price_max:
        parts.append(f"до {_fmt_money(p.price_max)}")
    sort_labels = {
        "price": "по цене ↑", "price_desc": "по цене ↓",
        "new": "новинки", "rating": "по рейтингу", "discount": "скидки",
    }
    if p.sort and p.sort in sort_labels:
        parts.append(sort_labels[p.sort])
    if p.min_rating:
        parts.append(f"оценка ≥{p.min_rating:g}")
    if parts:
        return " · " + ", ".join(parts)
    return ""


def format_results(p: ParsedQuery, items: List[dict]) -> str:
    search_link = OZON_BASE + _build_url(p, 1)
    head = f"🔍 <b>Ozon: {p.text}</b>{_filters_suffix(p)}\n"
    sep  = "━━━━━━━━━━━━"
    blocks = [_fmt_item(i, it) for i, it in enumerate(items, 1)]
    body = f"\n{sep}\n".join(blocks)
    footer = f"\n\n🛒 <a href=\"{search_link}\">Все результаты на Ozon</a>"
    return head + "\n" + body + footer


# ── Роутер ────────────────────────────────────────────────────────────────────
router = Router(name="ozon")

HELP_TEXT = (
    "<b>🛒 Поиск на Ozon</b>\n\n"
    "<code>/ozon &lt;запрос&gt;</code> — поиск товаров\n\n"
    "<b>Фильтры:</b>\n"
    "• <code>до 5000</code> / <code>от 1000</code> — диапазон цен\n"
    "• <code>1000-5000</code> — диапазон\n"
    "• <code>дешёвые</code> / <code>дорогие</code> — сортировка\n"
    "• <code>новинки</code> — по дате\n"
    "• <code>скидки</code> — по размеру скидки\n"
    "• <code>рейтинг</code> — по рейтингу\n"
    "• <code>оценка 4.5</code> — минимальная оценка\n\n"
    "<b>Примеры:</b>\n"
    "<code>/ozon наушники до 3000 скидки</code>\n"
    "<code>/ozon ноутбук от 50000 рейтинг оценка 4</code>"
)


@router.message(Command("ozon"))
async def cmd_ozon(msg: Message):
    print(f"✅ CMD_OZON ВЫЗВАН: {msg.text}", flush=True)
    logger.info(f"✅ CMD_OZON ВЫЗВАН от chat_id={msg.chat.id}: {msg.text}")
    try:
        await msg.answer("🔍 Тест: ozon_handler работает!")
        logger.info("✅ CMD_OZON: ответ отправлен")
    except Exception as e:
        logger.error(f"❌ CMD_OZON: не могу ответить: {e}", exc_info=True)
        print(f"❌ CMD_OZON ERROR: {e}", flush=True)
        return

    args  = (msg.text or "").split(maxsplit=1)
    query = args[1].strip() if len(args) > 1 else ""

    if not query:
        await msg.answer(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
        return

    p = parse_query(query)
    if not p.text:
        await msg.answer("❌ Укажите название товара.", parse_mode="HTML")
        return

    search_link = OZON_BASE + _build_url(p, 1)
    wait = await msg.answer(
        f"🔍 Ищу <b>{p.text}</b> на Ozon{_filters_suffix(p)}…",
        parse_mode="HTML",
    )

    try:
        items = await asyncio.wait_for(search_ozon(p), timeout=45)
    except asyncio.TimeoutError:
        await wait.edit_text(
            f"⏱ Ozon не ответил вовремя.\n\n"
            f"🔗 <a href=\"{search_link}\">Поискать вручную</a>",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        return
    except Exception as e:
        logger.error(f"Ozon search error: {e}", exc_info=True)
        await wait.edit_text(
            f"❌ Ошибка при поиске.\n\n"
            f"🔗 <a href=\"{search_link}\">Попробуйте вручную</a>",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        return

    if not items:
        await wait.edit_text(
            f"😔 По запросу «{p.text}» ничего не найдено"
            + (f" с фильтрами{_filters_suffix(p)}" if _filters_suffix(p) else "")
            + f".\n\n🔗 <a href=\"{search_link}\">Поискать на сайте</a>",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        return

    text = format_results(p, items)

    # Кнопка «Ещё» — новый поиск с той же строкой но большим числом результатов
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Открыть на Ozon", url=search_link),
    ]])

    await wait.edit_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb,
    )


def register_ozon_handlers(dp: Dispatcher):
    dp.include_router(router)
    logger.info("🛒 ozon_handler зарегистрирован")
