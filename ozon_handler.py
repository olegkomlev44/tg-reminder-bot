"""
ozon_handler.py — Поиск товаров на Wildberries для Telegram-бота на aiogram 3.x

Wildberries имеет публичный JSON API поиска без авторизации.
Команды:
  /ozon <запрос>          — поиск (до 5 товаров)
  /ozon <запрос> до 5000  — с максимальной ценой
  /ozon <запрос> от 1000  — с минимальной ценой
  /ozon <запрос> дешёвые  — сортировка по цене
  /ozon <запрос> дорогие  — сортировка по цене (убывание)
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

# ── WB API ────────────────────────────────────────────────────────────────────
WB_SEARCH  = "https://search.wb.ru/exactmatch/ru/common/v7/search"
WB_CATALOG = "https://catalog.wb.ru/catalog/search"
WB_ITEM    = "https://www.wildberries.ru/catalog/{}/detail.aspx"

WB_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin":          "https://www.wildberries.ru",
    "Referer":         "https://www.wildberries.ru/",
    "x-queryid":       "qid1234567890",
}

WB_PARAMS_BASE = {
    "appType":    "1",
    "curr":       "rub",
    "dest":       "-1257786",
    "resultset":  "catalog",
    "limit":      "20",
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
    "дешёвые":   "priceup",   "дешевые":   "priceup",
    "дешево":    "priceup",   "дёшево":    "priceup",
    "подешевле": "priceup",
    "дорогие":   "pricedown", "подороже":  "pricedown",
    "новинки":   "newly",     "новые":     "newly",
    "рейтинг":   "rate",      "популярные":"popular",
    "скидки":    "sale",
}

def _to_int(s: str) -> Optional[int]:
    s = s.replace("\u00a0","").replace(" ","").replace(",",".")
    try:
        v = float(re.sub(r"[^\d.]","",s))
        return int(v * 1000) if v < 500 and any(c in s.lower() for c in ("к","k")) else int(v)
    except Exception:
        return None

def parse_query(raw: str) -> ParsedQuery:
    p = ParsedQuery()
    tokens = raw.split()
    used   = [False] * len(tokens)

    def nxt_int(i):
        for j in range(i+1, min(i+3, len(tokens))):
            if used[j]: continue
            v = _to_int(tokens[j])
            if v and 10 < v < 100_000_000:
                used[j] = True
                return v
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
    sort_labels = {
        "priceup":   "по цене ↑", "pricedown": "по цене ↓",
        "newly":     "новинки",   "rate":      "по рейтингу",
        "sale":      "скидки",
    }
    if p.sort in sort_labels: parts.append(sort_labels[p.sort])
    return " · " + ", ".join(parts) if parts else ""


# ── WB поиск ──────────────────────────────────────────────────────────────────
async def _wb_search(p: ParsedQuery) -> List[dict]:
    params = dict(WB_PARAMS_BASE)
    params["query"] = p.text
    params["sort"]  = p.sort
    if p.price_min: params["priceU"] = f"{p.price_min*100};"
    if p.price_max:
        pmin = params.get("priceU", ";").split(";")[0]
        params["priceU"] = f"{pmin};{p.price_max*100}"

    endpoints = [
        WB_SEARCH + "?" + urlencode(params),
        WB_CATALOG + "?" + urlencode(params),
    ]

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector, headers=WB_HEADERS) as session:
        for url in endpoints:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                    logger.info(f"WB [{url[:60]}] статус: {resp.status}")
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                    products = (
                        data.get("data", {}).get("products") or
                        data.get("products") or
                        data.get("catalog", {}).get("products") or
                        []
                    )
                    logger.info(f"WB вернул {len(products)} товаров")
                    if products:
                        return _parse_wb_products(products, p)
            except Exception as e:
                logger.warning(f"WB endpoint error: {e}")
                continue
    return []


def _parse_wb_products(products: list, p: ParsedQuery) -> List[dict]:
    items = []
    for prod in products:
        name  = prod.get("name") or prod.get("title") or ""
        brand = prod.get("brand") or ""
        full_name = f"{brand} {name}".strip() if brand else name

        # Цена в копейках: salePriceU или priceU
        sale_u = prod.get("salePriceU") or prod.get("sale_price_u")
        orig_u = prod.get("priceU")     or prod.get("price_u")
        price     = (sale_u // 100) if sale_u else None
        old_price = (orig_u // 100) if orig_u and orig_u != sale_u else None

        if not price or price < 10:
            continue

        # Фильтр по цене
        if p.price_min and price < p.price_min: continue
        if p.price_max and price > p.price_max: continue

        nm_id = prod.get("id") or prod.get("nmId") or 0
        link  = WB_ITEM.format(nm_id) if nm_id else ""

        rating = prod.get("rating") or prod.get("suppliersRating") or 0
        reviews = prod.get("feedbacks") or prod.get("feedbackCount") or 0

        items.append({
            "title":     full_name[:100],
            "price":     price,
            "old_price": old_price if old_price and old_price > price else None,
            "rating":    round(float(rating), 1) if rating else None,
            "reviews":   int(reviews) if reviews else None,
            "link":      link,
        })

    return items


# ── Форматирование ─────────────────────────────────────────────────────────────
def _fmt_item(i: int, it: dict) -> str:
    title   = it.get("title", "")
    price   = it.get("price")
    old     = it.get("old_price")
    rating  = it.get("rating")
    reviews = it.get("reviews")
    link    = it.get("link", "")

    price_str = _fmt_money(price) if price else "—"
    if old and old > price:
        try:    pct = int(round((1 - price / old) * 100))
        except: pct = 0
        price_str += f"  <s>{_fmt_money(old)}</s>"
        if pct > 0: price_str += f" −{pct}%"

    lines = [f"<b>{i}. {title}</b>", f"💰 {price_str}"]
    if rating:
        rev_str = f" ({reviews:,} отз.)".replace(",","\u00a0") if reviews else ""
        lines.append(f"⭐ {rating}{rev_str}")
    if link:
        lines.append(f"🔗 {link}")
    return "\n".join(lines)


def _format_results(p: ParsedQuery, items: List[dict]) -> str:
    wb_link = "https://www.wildberries.ru/catalog/0/search.aspx?sort=" + p.sort + "&search=" + quote_plus(p.text)
    head   = f"🔍 <b>WB: {p.text}</b>{_filters_suffix(p)}\n"
    sep    = "━━━━━━━━━━━━"
    blocks = [_fmt_item(i, it) for i, it in enumerate(items, 1)]
    body   = f"\n{sep}\n".join(blocks)
    footer = f"\n\n🛒 <a href=\"{wb_link}\">Все результаты на Wildberries</a>"
    return head + "\n" + body + footer


# ── Хэндлер ───────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "<b>🛒 Поиск на Wildberries</b>\n\n"
    "<code>/ozon &lt;запрос&gt;</code> — поиск товаров\n\n"
    "<b>Фильтры:</b>\n"
    "• <code>до 5000</code> / <code>от 1000</code> — цена\n"
    "• <code>дешёвые</code> / <code>дорогие</code> — сортировка\n"
    "• <code>новинки</code> — по дате добавления\n"
    "• <code>рейтинг</code> — по оценке\n"
    "• <code>скидки</code> — по размеру скидки\n\n"
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
        items = await asyncio.wait_for(_wb_search(p), timeout=25)
    except asyncio.TimeoutError:
        items = []
    except Exception as e:
        logger.error(f"WB search error: {e}", exc_info=True)
        items = []

    items = items[:MAX_RESULTS]

    if not items:
        wb_link = "https://www.wildberries.ru/catalog/0/search.aspx?search=" + quote_plus(p.text)
        await wait.edit_text(
            f"😔 Ничего не нашёл по запросу «{p.text}».\n\n"
            f"🔗 <a href=\"{wb_link}\">Поискать на Wildberries</a>",
            parse_mode="HTML", disable_web_page_preview=False,
        )
        return

    wb_link = "https://www.wildberries.ru/catalog/0/search.aspx?sort=" + p.sort + "&search=" + quote_plus(p.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Wildberries", url=wb_link),
    ]])
    await wait.edit_text(
        _format_results(p, items),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb,
    )


def register_ozon_handlers(dp: Dispatcher):
    logger.info("🛒 ozon_handler зарегистрирован")
