"""
ozon_handler.py — Поиск товаров через Google Shopping для Telegram-бота на aiogram 3.x

Команды:
  /ozon <запрос>          — поиск (до 5 товаров)
  /ozon <запрос> до 5000  — с максимальной ценой
  /ozon <запрос> от 1000  — с минимальной ценой
  /ozon <запрос> дешёвые  — сортировка по цене

Подключение в main.py:
    from ozon_handler import cmd_ozon
    dp.message.register(cmd_ozon, Command("ozon"))
"""

import asyncio
import logging
import re
from html import unescape
from typing import List, Optional
from urllib.parse import quote_plus, urlencode

import aiohttp
from aiogram import Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logger = logging.getLogger(__name__)

MAX_RESULTS = 5
TIMEOUT     = 20

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Парсер запроса ─────────────────────────────────────────────────────────────
import re as _re

NUM_RE = _re.compile(r"^(\d+(?:[.,]\d+)?)$")

class ParsedQuery:
    __slots__ = ("text", "price_min", "price_max", "sort")
    def __init__(self):
        self.text      = ""
        self.price_min = None
        self.price_max = None
        self.sort      = None   # "price_asc" | "price_desc" | None

SORT_WORDS = {
    "дешёвые": "price_asc", "дешевые": "price_asc", "дешево": "price_asc",
    "дёшево":  "price_asc", "подешевле": "price_asc",
    "дорогие": "price_desc","подороже":  "price_desc",
}

def _to_int(s: str) -> Optional[int]:
    s = s.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    m = NUM_RE.match(s)
    if not m: return None
    try:
        v = float(m.group(1))
        return int(round(v * (1000 if v < 1000 and s.endswith(("к","k")) else 1)))
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
            if v is not None:
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
    if p.sort == "price_asc":  parts.append("по цене ↑")
    if p.sort == "price_desc": parts.append("по цене ↓")
    return " · " + ", ".join(parts) if parts else ""


# ── Google Shopping парсер ────────────────────────────────────────────────────

def _google_shopping_url(query: str) -> str:
    """URL поиска Google Shopping на русском."""
    return (
        "https://www.google.com/search?"
        + urlencode({
            "q":    query,
            "tbm":  "shop",
            "hl":   "ru",
            "gl":   "ru",
            "num":  "20",
        })
    )

def _google_headers() -> dict:
    return {
        "User-Agent":      UA,
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT":             "1",
        "Sec-Fetch-Site":  "none",
        "Sec-Fetch-Mode":  "navigate",
    }

def _parse_price(text: str) -> Optional[int]:
    """Извлекает целое число рублей из строки типа '1 234 ₽' или '1234 руб'."""
    text = text.replace("\u00a0", "").replace(" ", "").replace("\xa0", "")
    m = re.search(r"(\d[\d,\.]*\d|\d)\s*(?:₽|руб)", text, re.IGNORECASE)
    if not m: return None
    digits = re.sub(r"[,\.]", "", m.group(1))
    try:
        v = int(digits)
        return v if 10 < v < 100_000_000 else None
    except Exception:
        return None

def _parse_google_shopping(html: str) -> List[dict]:
    """Парсит HTML Google Shopping и возвращает список товаров."""
    items = []
    seen  = set()

    # Google Shopping рендерит товары в блоках <div class="sh-dgr__content"> или похожих
    # Ищем паттерны: название + цена + магазин + ссылка

    # Метод 1: JSON-LD структурированные данные
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
        try:
            import json
            blob = json.loads(m.group(1))
            items_raw = blob if isinstance(blob, list) else [blob]
            for item in items_raw:
                if item.get("@type") not in ("Product", "Offer"): continue
                name  = item.get("name", "")
                price = None
                offer = item.get("offers") or item.get("Offers")
                if isinstance(offer, dict):
                    price = _parse_price(str(offer.get("price", "")))
                    if not price:
                        price = _parse_price(str(offer.get("lowPrice", "")))
                url = item.get("url", "") or item.get("@id", "")
                if name and price and url and url not in seen:
                    seen.add(url)
                    items.append({"title": name[:120], "price": price, "old_price": None,
                                  "shop": "", "link": url})
        except Exception:
            pass

    # Метод 2: Регулярки по HTML-блокам Google Shopping
    # Блок товара содержит: название в <h3> или data-title, цену рядом с ₽
    block_re = re.compile(
        r'<(?:div|a)[^>]+(?:class|data-)[^>]*(?:sh-dgr|sh-pr|pla-unit|commercial-unit)[^>]*>'
        r'(.*?)</(?:div|a)>',
        re.DOTALL | re.IGNORECASE
    )

    # Более простой и надёжный подход — ищем пары (заголовок, цена) в HTML
    # Google Shopping использует aria-label и title атрибуты
    title_candidates = re.findall(
        r'aria-label="([^"]{10,120})"[^>]*>|'
        r'<h3[^>]*class="[^"]*(?:title|name)[^"]*"[^>]*>([^<]{5,120})</h3>|'
        r'data-title="([^"]{5,120})"',
        html
    )
    price_candidates = re.findall(
        r'(\d[\d\s]{1,9}\d)\s*(?:₽|руб\.?)',
        unescape(html)
    )

    prices = []
    for p in price_candidates:
        v = _parse_price(p + "₽")
        if v and v not in prices:
            prices.append(v)

    titles = []
    for groups in title_candidates:
        t = next((g.strip() for g in groups if g.strip()), "")
        if t and len(t) > 8 and t not in titles:
            titles.append(t)

    # Метод 3: Парсим структуру Google Shopping более точно
    # Ищем блоки с классом sh-dgr__grid-result или i0X6df
    result_blocks = re.findall(
        r'<div[^>]+class="[^"]*(?:sh-dgr__grid-result|i0X6df|KZmu8e|Qlx7of)[^"]*"[^>]*>'
        r'(.*?)(?=<div[^>]+class="[^"]*(?:sh-dgr__grid-result|i0X6df|KZmu8e|Qlx7of)[^"]*"|$)',
        html, re.DOTALL
    )

    for block in result_blocks[:20]:
        # Заголовок
        t_match = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.DOTALL)
        if not t_match:
            t_match = re.search(r'(?:aria-label|title)="([^"]{8,120})"', block)
        title = ""
        if t_match:
            title = re.sub(r'<[^>]+>', '', t_match.group(1)).strip()
            title = unescape(title)[:120]

        if not title or len(title) < 5:
            continue

        # Цена
        price_match = re.search(r'(\d[\d\u00a0\s]{1,8}\d)\s*₽', unescape(block))
        price = None
        if price_match:
            price = _parse_price(price_match.group(0))

        if not price:
            continue

        # Зачёркнутая цена
        old_price = None
        old_matches = re.findall(r'<s[^>]*>.*?(\d[\d\u00a0\s]{1,8}\d)\s*₽.*?</s>', block, re.DOTALL)
        if old_matches:
            old_price = _parse_price(old_matches[-1] + "₽")
            if old_price and old_price <= price:
                old_price = None

        # Магазин
        shop = ""
        shop_m = re.search(r'(?:class="[^"]*(?:merchant|shop|store|aULzUe)[^"]*"[^>]*>)(.*?)(?:</)', block, re.DOTALL)
        if shop_m:
            shop = re.sub(r'<[^>]+>', '', shop_m.group(1)).strip()[:40]

        # Ссылка
        link = ""
        link_m = re.search(r'href="(/shopping/product/[^"]+|https?://[^"]+)"', block)
        if link_m:
            href = link_m.group(1)
            if href.startswith("/"):
                link = "https://www.google.com" + href
            else:
                link = href

        key = title[:40]
        if key not in seen:
            seen.add(key)
            items.append({
                "title":     title,
                "price":     price,
                "old_price": old_price,
                "shop":      shop,
                "link":      link,
            })

    # Если методы 2/3 не дали результатов — используем собранные titles+prices
    if not items and titles and prices:
        for i, title in enumerate(titles[:MAX_RESULTS]):
            price = prices[i] if i < len(prices) else None
            if price:
                items.append({
                    "title": title, "price": price,
                    "old_price": None, "shop": "", "link": "",
                })

    return items[:20]


async def _fetch_google_shopping(query: str) -> List[dict]:
    url = _google_shopping_url(query)
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                url,
                headers=_google_headers(),
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                allow_redirects=True,
            ) as resp:
                logger.info(f"Google Shopping статус: {resp.status}")
                if resp.status != 200:
                    return []
                html = await resp.text()
                logger.info(f"Google Shopping HTML длина: {len(html)}")
                items = _parse_google_shopping(html)
                logger.info(f"Google Shopping нашёл: {len(items)} товаров")
                return items
    except Exception as e:
        logger.error(f"Google Shopping ошибка: {e}", exc_info=True)
        return []


def _filter_items(items: List[dict], p: ParsedQuery) -> List[dict]:
    out = []
    for it in items:
        price = it.get("price")
        if not price: continue
        if p.price_min and price < p.price_min: continue
        if p.price_max and price > p.price_max: continue
        out.append(it)

    if p.sort == "price_asc":
        out.sort(key=lambda x: x.get("price") or 0)
    elif p.sort == "price_desc":
        out.sort(key=lambda x: x.get("price") or 0, reverse=True)

    return out


# ── Форматирование ────────────────────────────────────────────────────────────

def _fmt_item(i: int, it: dict) -> str:
    title = it.get("title", "")[:80]
    price = it.get("price")
    old   = it.get("old_price")
    shop  = it.get("shop", "")
    link  = it.get("link", "")

    price_str = _fmt_money(price) if price else "—"
    if old and old > price:
        try:    pct = int(round((1 - price / old) * 100))
        except: pct = 0
        price_str += f"  <s>{_fmt_money(old)}</s>"
        if pct > 0: price_str += f" −{pct}%"

    lines = [f"<b>{i}. {title}</b>", f"💰 {price_str}"]
    if shop: lines.append(f"🏪 {shop}")
    if link: lines.append(f"🔗 {link}")
    return "\n".join(lines)


def _format_results(p: ParsedQuery, items: List[dict]) -> str:
    query_str = quote_plus(p.text + " купить")
    google_link = f"https://www.google.com/search?q={query_str}&tbm=shop&hl=ru&gl=ru"

    head   = f"🔍 <b>{p.text}</b>{_filters_suffix(p)}\n"
    sep    = "━━━━━━━━━━━━"
    blocks = [_fmt_item(i, it) for i, it in enumerate(items, 1)]
    body   = f"\n{sep}\n".join(blocks)
    footer = f"\n\n🛒 <a href=\"{google_link}\">Все результаты в Google Shopping</a>"
    return head + "\n" + body + footer


# ── Хэндлер ──────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "<b>🛒 Поиск товаров</b>\n\n"
    "<code>/ozon &lt;запрос&gt;</code> — поиск\n\n"
    "<b>Фильтры:</b>\n"
    "• <code>до 5000</code> / <code>от 1000</code> — цена\n"
    "• <code>дешёвые</code> / <code>дорогие</code> — сортировка\n\n"
    "<b>Примеры:</b>\n"
    "<code>/ozon наушники до 3000 дешёвые</code>\n"
    "<code>/ozon ноутбук от 50000</code>"
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
        f"🔍 Ищу <b>{p.text}</b>{_filters_suffix(p)}…",
        parse_mode="HTML",
    )

    try:
        raw = await asyncio.wait_for(_fetch_google_shopping(p.text), timeout=25)
    except asyncio.TimeoutError:
        raw = []
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}", exc_info=True)
        raw = []

    items = _filter_items(raw, p)[:MAX_RESULTS]

    if not items:
        query_str   = quote_plus(p.text + " купить")
        google_link = f"https://www.google.com/search?q={query_str}&tbm=shop&hl=ru&gl=ru"
        await wait.edit_text(
            f"😔 Ничего не нашёл по запросу «{p.text}».\n\n"
            f"🔗 <a href=\"{google_link}\">Поискать в Google Shopping</a>",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        return

    text = _format_results(p, items)
    query_str   = quote_plus(p.text + " купить")
    google_link = f"https://www.google.com/search?q={query_str}&tbm=shop&hl=ru&gl=ru"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Google Shopping", url=google_link),
    ]])

    await wait.edit_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb,
    )


def register_ozon_handlers(dp: Dispatcher):
    logger.info("🛒 ozon_handler зарегистрирован")
