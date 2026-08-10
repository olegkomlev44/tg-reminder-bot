"""
ozon_handler.py — Поиск товаров на Wildberries для Telegram-бота на aiogram 3.x

WB API: 429 = rate limit (слишком частые запросы без куков).
Решение: сначала получаем куки с главной страницы, затем ищем.
"""

import asyncio
import logging
import re
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
TIMEOUT     = 20

WB_MAIN   = "https://www.wildberries.ru/"
WB_SEARCH = "https://search.wb.ru/exactmatch/ru/common/v7/search"
WB_ITEM   = "https://www.wildberries.ru/catalog/{}/detail.aspx"

# Полный браузерный UA + заголовки
WB_HEADERS_MAIN = {
    "User-Agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language":           "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding":           "gzip, deflate, br",
    "Connection":                "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
}

WB_HEADERS_API = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin":          "https://www.wildberries.ru",
    "Referer":         "https://www.wildberries.ru/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "cross-site",
    "Connection":      "keep-alive",
}


# ── Парсер запроса ─────────────────────────────────────────────────────────────
class ParsedQuery:
    __slots__ = ("text", "price_min", "price_max", "sort")
    def __init__(self):
        self.text      = ""
        self.price_min = None
        self.price_max = None
        self.sort      = "popular"

SORT_WORDS = {
    "дешёвые": "priceup",  "дешевые": "priceup",  "дешево": "priceup",
    "дёшево":  "priceup",  "подешевле": "priceup",
    "дорогие": "pricedown","подороже": "pricedown",
    "новинки": "newly",    "новые":    "newly",
    "рейтинг": "rate",     "скидки":   "sale",
}

def _to_int(s: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", s)
    try: return int(digits) if 10 < int(digits) < 100_000_000 else None
    except: return None

def parse_query(raw: str) -> ParsedQuery:
    p = ParsedQuery()
    tokens = raw.split()
    used   = [False] * len(tokens)

    def nxt_int(i):
        for j in range(i+1, min(i+3, len(tokens))):
            if used[j]: continue
            v = _to_int(tokens[j])
            if v: used[j] = True; return v
        return None

    for i, tok in enumerate(tokens):
        if used[i]: continue
        low = tok.lower().replace("ё","е")
        if low in ("до","макс","<=","<"):
            v = nxt_int(i)
            if v: p.price_max = v; used[i] = True; continue
        if low in ("от","мин",">=",">"):
            v = nxt_int(i)
            if v: p.price_min = v; used[i] = True; continue
        if low in SORT_WORDS:
            p.sort = SORT_WORDS[low]; used[i] = True; continue

    p.text = " ".join(t for i,t in enumerate(tokens) if not used[i]).strip()
    return p


def _fmt_money(v: int) -> str:
    return f"{v:,}".replace(",", "\u00a0") + " ₽"

def _filters_suffix(p: ParsedQuery) -> str:
    parts = []
    if p.price_min: parts.append(f"от {_fmt_money(p.price_min)}")
    if p.price_max: parts.append(f"до {_fmt_money(p.price_max)}")
    labels = {"priceup":"по цене ↑","pricedown":"по цене ↓","newly":"новинки","rate":"рейтинг","sale":"скидки"}
    if p.sort in labels: parts.append(labels[p.sort])
    return " · " + ", ".join(parts) if parts else ""


# ── WB поиск с куками ─────────────────────────────────────────────────────────
async def _wb_search(p: ParsedQuery) -> List[dict]:
    jar = aiohttp.CookieJar(unsafe=True)
    conn = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(cookie_jar=jar, connector=conn) as session:
        # Шаг 1: прогрев — получаем куки с главной
        try:
            async with session.get(
                WB_MAIN,
                headers=WB_HEADERS_MAIN,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=True,
            ) as r:
                logger.info(f"WB прогрев: {r.status}, куки: {len(jar)}")
        except Exception as e:
            logger.warning(f"WB прогрев не удался: {e}")

        await asyncio.sleep(0.5)

        # Шаг 2: поиск
        params = {
            "appType":   "1",
            "curr":      "rub",
            "dest":      "-1257786",
            "resultset": "catalog",
            "limit":     "20",
            "sort":      p.sort,
            "query":     p.text,
        }
        if p.price_min or p.price_max:
            pmin = p.price_min * 100 if p.price_min else 0
            pmax = p.price_max * 100 if p.price_max else 0
            if pmin: params["priceU"] = f"{pmin};"
            if pmax:
                params["priceU"] = f"{pmin};{pmax}"

        url = WB_SEARCH + "?" + urlencode(params)
        try:
            async with session.get(
                url,
                headers=WB_HEADERS_API,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ) as r:
                logger.info(f"WB поиск: статус {r.status}")
                if r.status == 429:
                    logger.warning("WB: rate limit (429) — слишком много запросов")
                    return []
                if r.status != 200:
                    logger.warning(f"WB: статус {r.status}")
                    return []
                data = await r.json(content_type=None)
                # Дамп структуры для отладки
                top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                logger.info(f"WB JSON top keys: {top_keys}")
                if isinstance(data, dict):
                    for k in list(data.keys())[:5]:
                        v = data[k]
                        if isinstance(v, dict):
                            logger.info(f"  WB[{k}] keys: {list(v.keys())[:8]}")
                        elif isinstance(v, list):
                            logger.info(f"  WB[{k}] list len={len(v)}, first={str(v[0])[:100] if v else 'empty'}")
                        else:
                            logger.info(f"  WB[{k}] = {str(v)[:80]}")
                products = (
                    data.get("data", {}).get("products") or
                    data.get("products") or
                    data.get("catalog", {}).get("products") or
                    data.get("value", {}).get("data", {}).get("products") or
                    []
                )
                logger.info(f"WB: {len(products)} товаров")
                return _parse_wb(products, p)
        except Exception as e:
            logger.error(f"WB поиск ошибка: {e}", exc_info=True)
            return []


def _parse_wb(products: list, p: ParsedQuery) -> List[dict]:
    items = []
    for prod in products:
        name  = (prod.get("name") or "").strip()
        brand = (prod.get("brand") or "").strip()
        title = f"{brand} {name}".strip() if brand else name
        if not title: continue

        sale_u = prod.get("salePriceU") or 0
        orig_u = prod.get("priceU") or 0
        price     = sale_u // 100 if sale_u else (orig_u // 100 if orig_u else None)
        old_price = orig_u // 100 if orig_u and orig_u > sale_u else None

        if not price or price < 10: continue
        if p.price_min and price < p.price_min: continue
        if p.price_max and price > p.price_max: continue

        nm_id   = prod.get("id") or 0
        rating  = prod.get("rating") or 0
        reviews = prod.get("feedbacks") or 0

        items.append({
            "title":     title[:100],
            "price":     price,
            "old_price": old_price,
            "rating":    round(float(rating), 1) if rating else None,
            "reviews":   int(reviews) if reviews else None,
            "link":      WB_ITEM.format(nm_id) if nm_id else "",
        })
    return items


# ── Форматирование ─────────────────────────────────────────────────────────────
def _fmt_item(i: int, it: dict) -> str:
    title = it["title"]
    price = it["price"]
    old   = it.get("old_price")
    rat   = it.get("rating")
    rev   = it.get("reviews")
    link  = it.get("link","")

    price_str = _fmt_money(price)
    if old and old > price:
        pct = int(round((1 - price/old)*100))
        price_str += f"  <s>{_fmt_money(old)}</s>"
        if pct > 0: price_str += f" −{pct}%"

    lines = [f"<b>{i}. {title}</b>", f"💰 {price_str}"]
    if rat:
        rev_str = f" ({rev:,} отз.)".replace(",","\u00a0") if rev else ""
        lines.append(f"⭐ {rat}{rev_str}")
    if link: lines.append(f"🔗 {link}")
    return "\n".join(lines)

def _format_results(p: ParsedQuery, items: List[dict]) -> str:
    wb_link = f"https://www.wildberries.ru/catalog/0/search.aspx?sort={p.sort}&search={quote_plus(p.text)}"
    head   = f"🔍 <b>WB: {p.text}</b>{_filters_suffix(p)}\n"
    blocks = [_fmt_item(i, it) for i, it in enumerate(items, 1)]
    body   = "\n━━━━━━━━━━━━\n".join(blocks)
    footer = f"\n\n🛒 <a href=\"{wb_link}\">Все результаты на Wildberries</a>"
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

    p = parse_query(query)
    if not p.text:
        await msg.answer("❌ Укажите название товара.", parse_mode="HTML")
        return

    wait = await msg.answer(
        f"🔍 Ищу <b>{p.text}</b> на Wildberries{_filters_suffix(p)}…",
        parse_mode="HTML",
    )

    try:
        items = await asyncio.wait_for(_wb_search(p), timeout=30)
    except asyncio.TimeoutError:
        items = []
    except Exception as e:
        logger.error(f"WB error: {e}", exc_info=True)
        items = []

    items = items[:MAX_RESULTS]
    wb_link = f"https://www.wildberries.ru/catalog/0/search.aspx?sort={p.sort}&search={quote_plus(p.text)}"

    if not items:
        await wait.edit_text(
            f"😔 Ничего не нашёл по запросу «{p.text}».\n\n"
            f"🔗 <a href=\"{wb_link}\">Поискать на Wildberries</a>",
            parse_mode="HTML", disable_web_page_preview=False,
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Wildberries", url=wb_link),
    ]])
    await wait.edit_text(
        _format_results(p, items),
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb,
    )

def register_ozon_handlers(dp: Dispatcher):
    logger.info("🛒 ozon_handler зарегистрирован")
