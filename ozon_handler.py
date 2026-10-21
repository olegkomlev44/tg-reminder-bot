"""
ozon_handler.py — Поиск товаров на Wildberries для Telegram-бота на aiogram 3.x
С защитой от BadStatusLine (WAF WB) и автоматическим фоллбэком через Gemini Google Search.
"""

import asyncio
import html
import json
import logging
import re
import ssl
from typing import List, Optional
from urllib.parse import quote_plus, urlencode

import aiohttp
from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logger = logging.getLogger(__name__)

MAX_RESULTS = 5
TIMEOUT     = 15

WB_MAIN = "https://www.wildberries.ru/"
WB_SEARCH_ENDPOINTS = [
    "https://search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v9/search",
    "https://u-search.wb.ru/exactmatch/ru/common/v9/search",
]
WB_ITEM = "https://www.wildberries.ru/catalog/{}/detail.aspx"

# Заголовки без 'br' (чтобы aiohttp не требовал brotli)
WB_HEADERS_MAIN = {
    "User-Agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language":           "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding":           "gzip, deflate",
    "Connection":                "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
}

WB_HEADERS_API = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Origin":          "https://www.wildberries.ru",
    "Referer":         "https://www.wildberries.ru/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "cross-site",
    "Connection":      "keep-alive",
}


def _make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers(
            "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
            "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
            "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305"
        )
    except ssl.SSLError:
        pass
    return ctx


# ── Парсер запроса ─────────────────────────────────────────────────────────────
class ParsedQuery:
    __slots__ = ("text", "price_min", "price_max", "sort")

    def __init__(self):
        self.text      = ""
        self.price_min = None
        self.price_max = None
        self.sort      = "popular"


SORT_WORDS = {
    "дешёвые": "priceup",   "дешевые": "priceup",   "дешево": "priceup",
    "дёшево":  "priceup",   "подешевле": "priceup",
    "дорогие": "pricedown", "подороже": "pricedown",
    "новинки": "newly",     "новые":    "newly",
    "рейтинг": "rate",      "скидки":   "sale",
}


def _to_int(s: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", str(s))
    try:
        val = int(digits)
        return val if 10 < val < 100_000_000 else None
    except Exception:
        return None


def parse_query(raw: str) -> ParsedQuery:
    p = ParsedQuery()
    tokens = raw.split()
    used   = [False] * len(tokens)

    def nxt_int(i):
        for j in range(i + 1, min(i + 3, len(tokens))):
            if used[j]:
                continue
            v = _to_int(tokens[j])
            if v:
                used[j] = True
                return v
        return None

    for i, tok in enumerate(tokens):
        if used[i]:
            continue
        low = tok.lower().replace("ё", "е")
        if low in ("до", "макс", "<=", "<"):
            v = nxt_int(i)
            if v:
                p.price_max = v
                used[i] = True
                continue
        if low in ("от", "мин", ">=", ">"):
            v = nxt_int(i)
            if v:
                p.price_min = v
                used[i] = True
                continue
        if low in SORT_WORDS:
            p.sort = SORT_WORDS[low]
            used[i] = True
            continue

    p.text = " ".join(t for i, t in enumerate(tokens) if not used[i]).strip()
    return p


def _fmt_money(v: int) -> str:
    return f"{v:,}".replace(",", "\u00a0") + " ₽"


def _filters_suffix(p: ParsedQuery) -> str:
    parts = []
    if p.price_min:
        parts.append(f"от {_fmt_money(p.price_min)}")
    if p.price_max:
        parts.append(f"до {_fmt_money(p.price_max)}")
    labels = {
        "priceup":   "по цене ↑",
        "pricedown": "по цене ↓",
        "newly":     "новинки",
        "rate":      "рейтинг",
        "sale":      "скидки",
    }
    if p.sort in labels:
        parts.append(labels[p.sort])
    return " · " + ", ".join(parts) if parts else ""


# ── Прямой поиск по API Wildberries ───────────────────────────────────────────
async def _wb_direct_search(p: ParsedQuery) -> List[dict]:
    jar = aiohttp.CookieJar(unsafe=True)
    conn = aiohttp.TCPConnector(ssl=_make_ssl_context())

    params = {
        "ab_testing": "false",
        "appType":    "1",
        "curr":       "rub",
        "dest":       "-1257786",
        "lang":       "ru",
        "resultset":  "catalog",
        "limit":      "25",
        "sort":       p.sort,
        "query":      p.text,
    }
    if p.price_min or p.price_max:
        pmin = (p.price_min or 0) * 100
        pmax = (p.price_max or 10_000_000) * 100
        params["priceU"] = f"{pmin};{pmax}"

    async with aiohttp.ClientSession(cookie_jar=jar, connector=conn) as session:
        try:
            async with session.get(
                WB_MAIN,
                headers=WB_HEADERS_MAIN,
                timeout=aiohttp.ClientTimeout(total=6),
                allow_redirects=True,
            ) as r:
                logger.info(f"WB прогрев: {r.status}, куки: {len(jar)}")
        except Exception as e:
            logger.debug(f"WB прогрев пропущен: {type(e).__name__}: {e}")

        for endpoint in WB_SEARCH_ENDPOINTS:
            url = f"{endpoint}?{urlencode(params)}"
            try:
                async with session.get(
                    url,
                    headers=WB_HEADERS_API,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                ) as r:
                    if r.status != 200:
                        logger.warning(f"WB API ({endpoint}): HTTP {r.status}")
                        continue
                    data = await r.json(content_type=None)
                    if not isinstance(data, dict):
                        continue
                    products = (
                        data.get("data", {}).get("products")
                        or data.get("products")
                        or data.get("catalog", {}).get("products")
                        or data.get("value", {}).get("data", {}).get("products")
                        or []
                    )
                    items = _parse_wb(products, p)
                    if items:
                        logger.info(f"WB API: найдено {len(items)} товаров через {endpoint}")
                        return items
            except aiohttp.ClientError as e:
                # WAF WB может вернуть сырой <html> без статуса (BadStatusLine)
                logger.warning(f"WB WAF заблокировал запрос к {endpoint}: {type(e).__name__}")
            except Exception as e:
                logger.warning(f"WB ошибка на {endpoint}: {type(e).__name__}: {e}")

    return []


def _parse_wb(products: list, p: ParsedQuery) -> List[dict]:
    items = []
    for prod in products:
        if not isinstance(prod, dict):
            continue
        name  = (prod.get("name") or "").strip()
        brand = (prod.get("brand") or "").strip()
        title = f"{brand} / {name}".strip(" /") if brand else name
        if not title:
            continue

        # В новых версиях WB API цены лежат в sizes[0].price (в копейках)
        sale_u = prod.get("salePriceU") or 0
        orig_u = prod.get("priceU") or 0
        sizes = prod.get("sizes")
        if isinstance(sizes, list) and sizes:
            for sz in sizes:
                pr = sz.get("price") if isinstance(sz, dict) else None
                if isinstance(pr, dict):
                    sale_u = pr.get("product") or pr.get("total") or sale_u
                    orig_u = pr.get("basic") or orig_u
                    if sale_u:
                        break

        price     = sale_u // 100 if sale_u else (orig_u // 100 if orig_u else None)
        old_price = orig_u // 100 if orig_u and sale_u and orig_u > sale_u else None

        if not price or price < 10:
            continue
        if p.price_min and price < p.price_min:
            continue
        if p.price_max and price > p.price_max:
            continue

        nm_id   = prod.get("id") or prod.get("nmId") or 0
        rating  = prod.get("reviewRating") or prod.get("rating") or 0
        reviews = prod.get("feedbacks") or prod.get("nmFeedbacks") or 0

        items.append({
            "title":     title[:100],
            "price":     int(price),
            "old_price": int(old_price) if old_price else None,
            "rating":    round(float(rating), 1) if rating else None,
            "reviews":   int(reviews) if reviews else None,
            "link":      WB_ITEM.format(nm_id) if nm_id else "",
        })
    return items


# ── Резервный поиск через Gemini + Google Search (обход блокировки IP) ────────
async def _gemini_wb_fallback(p: ParsedQuery) -> List[dict]:
    try:
        from ai_handlers import get_gemini
        from google.genai import types as gt
    except Exception as e:
        logger.warning(f"WB fallback: не удалось импортировать Gemini: {e}")
        return []

    client = get_gemini()
    if not client:
        return []

    price_cond = ""
    if p.price_min and p.price_max:
        price_cond = f"Диапазон цены: от {p.price_min} до {p.price_max} руб."
    elif p.price_max:
        price_cond = f"Цена строго до {p.price_max} руб."
    elif p.price_min:
        price_cond = f"Цена от {p.price_min} руб."

    sort_map = {
        "priceup":   "сначала дешёвые",
        "pricedown": "сначала дорогие",
        "rate":      "с высоким рейтингом",
        "sale":      "со скидками",
        "newly":     "новинки",
    }
    sort_cond = f"Сортировка: {sort_map.get(p.sort, 'популярные')}."

    prompt = (
        f"Найди через поиск реальные товары на Wildberries или Ozon по запросу: «{p.text}».\n"
        f"{price_cond} {sort_cond}\n"
        "Верни ТОЛЬКО валидный JSON-массив (до 5 товаров) без комментариев и пояснений в формате:\n"
        '[{"title": "Бренд Название товара", "price": 1200, "old_price": 1800, '
        '"rating": 4.8, "reviews": 150, "nm_id": 123456789}]\n'
        "Правила:\n"
        "- price и old_price — целые числа в рублях (если скидки нет, old_price: null).\n"
        "- nm_id — реальный артикул Wildberries из ссылки wildberries.ru/catalog/<nm_id>/detail.aspx, "
        "если он есть в результатах поиска, иначе null."
    )

    try:
        resp = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=gt.GenerateContentConfig(tools=[{"google_search": {}}]),
        )
        raw_text = (resp.text or "").strip()
        match = re.search(r"\[.*\]", raw_text, flags=re.DOTALL)
        if not match:
            return []

        parsed = json.loads(match.group(0))
        if not isinstance(parsed, list):
            return []

        items: List[dict] = []
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            price = _to_int(entry.get("price"))
            if not title or not price:
                continue
            if p.price_min and price < p.price_min:
                continue
            if p.price_max and price > p.price_max:
                continue

            old_price = _to_int(entry.get("old_price"))
            if old_price and old_price <= price:
                old_price = None

            rating_val = entry.get("rating")
            try:
                rating = round(float(rating_val), 1) if rating_val else None
                if rating and (rating <= 0 or rating > 5):
                    rating = None
            except Exception:
                rating = None

            reviews = _to_int(entry.get("reviews"))

            nm_id = entry.get("nm_id")
            if nm_id and str(nm_id).isdigit() and 5 <= len(str(nm_id)) <= 12:
                link = WB_ITEM.format(nm_id)
            else:
                link = f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote_plus(title[:60])}"

            items.append({
                "title":     title[:100],
                "price":     price,
                "old_price": old_price,
                "rating":    rating,
                "reviews":   reviews,
                "link":      link,
            })

        logger.info(f"WB Gemini fallback: найдено {len(items)} товаров по запросу «{p.text}»")
        return items
    except Exception as e:
        logger.warning(f"WB Gemini fallback ошибка: {e}")
        return []


async def _wb_search(p: ParsedQuery) -> List[dict]:
    items = await _wb_direct_search(p)
    if items:
        return items
    logger.info(f"Прямой поиск WB недоступен с IP хостинга, включаю поиск через Gemini для «{p.text}»")
    return await _gemini_wb_fallback(p)


# ── Форматирование ─────────────────────────────────────────────────────────────
def _fmt_item(i: int, it: dict) -> str:
    title = html.escape(it["title"])
    price = it["price"]
    old   = it.get("old_price")
    rat   = it.get("rating")
    rev   = it.get("reviews")
    link  = it.get("link", "")

    price_str = _fmt_money(price)
    if old and old > price:
        pct = int(round((1 - price / old) * 100))
        price_str += f"  <s>{_fmt_money(old)}</s>"
        if pct > 0:
            price_str += f" −{pct}%"

    lines = [f"<b>{i}. {title}</b>", f"💰 {price_str}"]
    if rat:
        rev_str = f" ({rev:,} отз.)".replace(",", "\u00a0") if rev else ""
        lines.append(f"⭐ {rat}{rev_str}")
    if link:
        lines.append(f"🔗 <a href=\"{html.escape(link, quote=True)}\">Открыть на Wildberries</a>")
    return "\n".join(lines)


def _format_results(p: ParsedQuery, items: List[dict]) -> str:
    wb_link = f"https://www.wildberries.ru/catalog/0/search.aspx?sort={p.sort}&search={quote_plus(p.text)}"
    head    = f"🔍 <b>WB: {html.escape(p.text)}</b>{html.escape(_filters_suffix(p))}\n"
    blocks  = [_fmt_item(i, it) for i, it in enumerate(items, 1)]
    body    = "\n━━━━━━━━━━━━\n".join(blocks)
    footer  = f"\n\n🛒 <a href=\"{wb_link}\">Все результаты на Wildberries</a>"
    return head + "\n" + body + footer


# ── Хэндлер ───────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "<b>🛒 Поиск на Wildberries</b>\n\n"
    "<code>/ozon &lt;запрос&gt;</code> — поиск товаров\n\n"
    "<b>Фильтры:</b>\n"
    "• <code>до 5000</code> / <code>от 1000</code> — цена\n"
    "• <code>дешёвые</code> / <code>дорогие</code> — сортировка\n"
    "• <code>новинки</code>, <code>рейтинг</code>, <code>скидки</code>\n\n"
    "<b>Примеры:</b>\n"
    "<code>/ozon наушники до 3000 дешёвые</code>\n"
    "<code>/ozon ноутбук от 50000 рейтинг</code>"
)


async def cmd_ozon(msg: Message):
    args  = (msg.text or "").split(maxsplit=1)
    query = args[1].strip() if len(args) > 1 else ""

    if not query:
        await msg.answer(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
        return

    await ozon_search(msg, query)


async def ozon_search(msg: Message, query: str) -> bool:
    """Сам поиск — вынесен из cmd_ozon, чтобы его мог вызывать ИИ (ai_tools.py)."""
    p = parse_query(query)
    if not p.text:
        await msg.answer("❌ Укажите название товара.", parse_mode="HTML")
        return False

    wait = await msg.answer(
        f"🔍 Ищу <b>{html.escape(p.text)}</b> на Wildberries{html.escape(_filters_suffix(p))}…",
        parse_mode="HTML",
    )

    try:
        items = await asyncio.wait_for(_wb_search(p), timeout=35)
    except asyncio.TimeoutError:
        items = []
    except Exception as e:
        logger.error(f"WB error: {e}", exc_info=True)
        items = []

    items = items[:MAX_RESULTS]
    wb_link = f"https://www.wildberries.ru/catalog/0/search.aspx?sort={p.sort}&search={quote_plus(p.text)}"
    ozon_link = f"https://www.ozon.ru/search/?text={quote_plus(p.text)}"

    if not items:
        await wait.edit_text(
            f"😔 Ничего не нашёл по запросу «{html.escape(p.text)}».\n\n"
            f"🔗 <a href=\"{wb_link}\">Искать на Wildberries</a> · "
            f"<a href=\"{ozon_link}\">Искать на Ozon</a>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return False

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟣 Wildberries", url=wb_link),
        InlineKeyboardButton(text="🔵 Ozon", url=ozon_link),
    ]])
    await wait.edit_text(
        _format_results(p, items),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb,
    )
    return True


def register_ozon_handlers(dp: Dispatcher):
    dp.message.register(cmd_ozon, Command("ozon"))
    logger.info("🛒 ozon_handler зарегистрирован")
