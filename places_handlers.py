"""
places_handlers.py — поиск мест рядом: кафе, аптеки, магазины.

Как пользоваться:
  • /nearby                       — бот попросит геопозицию (кнопкой в личке / ответом в группе)
  • скинуть геопозицию боту в личку (или ответом на его сообщение в группе)
  • /nearby аптека Тверская 7     — категория + адрес (геокодинг через Nominatim)
  • ответить командой /nearby на сообщение с геопозицией

Данные — OpenStreetMap (Overpass API, несколько зеркал), геокодинг — Nominatim/Photon.
Ключи API не нужны. Подключение: register_places_handlers(dp) в main.py.
"""

import asyncio
import html
import logging
import math
import re
import secrets
import time
from collections import OrderedDict

import aiohttp
from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import (
    ForceReply, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

logger = logging.getLogger(__name__)

USER_AGENT = "NaryadBot/1.0 (telegram bot; places search)"
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
RADIUS_START = 1000
RADIUS_WIDE = 3000
PER_CAT_SHORT = 4
PER_CAT_FULL = 8

CATS: dict[str, dict] = {
    "cafe":  {"emoji": "☕", "label": "Кафе и еда"},
    "pharm": {"emoji": "💊", "label": "Аптеки"},
    "shop":  {"emoji": "🛒", "label": "Магазины"},
}
_CAFE_AMENITIES = {"cafe", "restaurant", "fast_food"}
_SHOP_TYPES = {"supermarket", "convenience", "mall", "department_store", "general", "greengrocer"}

# синонимы для /nearby <категория> и ИИ-инструмента
CAT_SYNONYMS = {
    "cafe": ("кафе", "кофе", "поесть", "еда", "ресторан", "столовая", "пекарня", "cafe", "food"),
    "pharm": ("аптека", "аптеки", "аптеку", "лекарства", "pharmacy"),
    "shop": ("магазин", "магазины", "магазина", "продукты", "супермаркет", "продуктовый", "shop", "store"),
}


def detect_category(word: str) -> str | None:
    w = (word or "").lower().strip(" ,.!?")
    for key, syns in CAT_SYNONYMS.items():
        if w in syns:
            return key
    return None


# ── Запомненные геопозиции и результаты ───────────────────────────────────────
_LAST_LOC: dict[int, tuple[float, float, float]] = {}    # user_id → (lat, lon, ts)
LOC_TTL = 3 * 3600
_SEARCHES: "OrderedDict[str, dict]" = OrderedDict()      # token → результаты


def remember_location(user_id: int, lat: float, lon: float):
    _LAST_LOC[user_id] = (lat, lon, time.time())


def recall_location(user_id: int) -> tuple[float, float] | None:
    v = _LAST_LOC.get(user_id)
    if v and time.time() - v[2] < LOC_TTL:
        return v[0], v[1]
    return None


# ── Геометрия и ссылки ────────────────────────────────────────────────────────
def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_arrow(lat1, lon1, lat2, lon2) -> str:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️"][int((deg + 22.5) // 45) % 8]


def fmt_dist(m: float) -> str:
    return f"{int(round(m, -1))} м" if m < 1000 else f"{m / 1000:.1f} км".replace(".", ",")


def yandex_pin(lat, lon) -> str:
    return f"https://yandex.ru/maps/?pt={lon:.6f},{lat:.6f}&z=17&l=map"


def yandex_route(lat0, lon0, lat, lon) -> str:
    return f"https://yandex.ru/maps/?rtext={lat0:.6f},{lon0:.6f}~{lat:.6f},{lon:.6f}&rtt=pd"


def google_pin(lat, lon) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def yandex_multi(points: list[tuple[float, float]], center: tuple[float, float]) -> str:
    pts = "~".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points[:15])
    return f"https://yandex.ru/maps/?pt={pts}&ll={center[1]:.6f},{center[0]:.6f}&z=16&l=map"


# ── Overpass ──────────────────────────────────────────────────────────────────
def build_query(lat: float, lon: float, radius: int) -> str:
    a = f"(around:{radius},{lat:.6f},{lon:.6f})"
    return (
        "[out:json][timeout:25];(\n"
        f'  nwr["amenity"~"^(cafe|restaurant|fast_food)$"]{a};\n'
        f'  nwr["amenity"="pharmacy"]{a};\n'
        f'  nwr["shop"~"^(supermarket|convenience|mall|department_store|general|greengrocer)$"]{a};\n'
        ");out tags center 800;"
    )


def classify(tags: dict) -> str | None:
    am, shop = tags.get("amenity"), tags.get("shop")
    if am == "pharmacy" or tags.get("healthcare") == "pharmacy":
        return "pharm"
    if am in _CAFE_AMENITIES:
        return "cafe"
    if shop in _SHOP_TYPES:
        return "shop"
    return None


def parse_overpass(data: dict, lat0: float, lon0: float) -> dict[str, list[dict]]:
    """Overpass JSON → {категория: [места по возрастанию расстояния]}."""
    res: dict[str, list[dict]] = {k: [] for k in CATS}
    seen: set[tuple] = set()
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        cat = classify(tags)
        if not cat:
            continue
        lat = el.get("lat") if "lat" in el else (el.get("center") or {}).get("lat")
        lon = el.get("lon") if "lon" in el else (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        name = (tags.get("name:ru") or tags.get("name") or tags.get("brand") or tags.get("operator") or "").strip()
        if not name:
            if cat != "pharm":
                continue                      # безымянные кафе/магазины — шум
            name = "Аптека"
        dist = haversine_m(lat0, lon0, lat, lon)
        key = (name.lower(), round(dist / 15))
        if key in seen:
            continue
        seen.add(key)
        street = tags.get("addr:street", "")
        house = tags.get("addr:housenumber", "")
        addr = (f"{street}, {house}" if street and house else street or "").strip(", ")
        res[cat].append({
            "name": name[:60], "lat": lat, "lon": lon, "dist": dist, "addr": addr[:60],
            "hours": (tags.get("opening_hours") or "")[:40],
            "kind": tags.get("cuisine", "").split(";")[0].replace("_", " ")[:20] if cat == "cafe" else "",
        })
    for k in res:
        res[k].sort(key=lambda p: p["dist"])
    return res


async def _overpass(query: str) -> dict | None:
    timeout = aiohttp.ClientTimeout(total=28)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
        for url in OVERPASS_MIRRORS:
            try:
                async with s.post(url, data={"data": query}, timeout=timeout) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    logger.info(f"overpass {url} → HTTP {r.status}")
            except Exception as e:
                logger.info(f"overpass {url}: {type(e).__name__}: {e}")
    return None


async def search_places(lat: float, lon: float) -> tuple[dict[str, list[dict]], int] | None:
    """Ищет на 1 км; если где-то меньше 3 мест — расширяет до 3 км. Возвращает (места, радиус)."""
    radius = RADIUS_START
    data = await _overpass(build_query(lat, lon, radius))
    if data is None:
        return None
    places = parse_overpass(data, lat, lon)
    if min(len(v) for v in places.values()) < 3:
        data2 = await _overpass(build_query(lat, lon, RADIUS_WIDE))
        if data2 is not None:
            places, radius = parse_overpass(data2, lat, lon), RADIUS_WIDE
    return places, radius


# ── Геокодинг адреса ──────────────────────────────────────────────────────────
async def geocode(query: str) -> tuple[float, float, str] | None:
    q = query.strip()
    if not q:
        return None
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
        try:
            async with s.get("https://nominatim.openstreetmap.org/search",
                             params={"q": q, "format": "json", "limit": 1, "accept-language": "ru"},
                             timeout=timeout) as r:
                if r.status == 200:
                    js = await r.json(content_type=None)
                    if js:
                        return float(js[0]["lat"]), float(js[0]["lon"]), js[0].get("display_name", q)[:80]
        except Exception as e:
            logger.info(f"nominatim: {e}")
        try:
            async with s.get("https://photon.komoot.io/api/", params={"q": q, "limit": 1, "lang": "default"},
                             timeout=timeout) as r:
                if r.status == 200:
                    js = await r.json(content_type=None)
                    feats = js.get("features") or []
                    if feats:
                        lon, lat = feats[0]["geometry"]["coordinates"]
                        p = feats[0].get("properties", {})
                        return float(lat), float(lon), ", ".join(
                            x for x in (p.get("name"), p.get("city")) if x) or q
        except Exception as e:
            logger.info(f"photon: {e}")
    return None


# ── Форматирование ────────────────────────────────────────────────────────────
def _place_line(i: int, p: dict, lat0: float, lon0: float) -> str:
    e = html.escape
    arrow = bearing_arrow(lat0, lon0, p["lat"], p["lon"])
    head = (f'{i}. <a href="{yandex_pin(p["lat"], p["lon"])}">{e(p["name"])}</a>'
            f' — {fmt_dist(p["dist"])} {arrow}')
    bits = []
    if p.get("kind"):
        bits.append(e(p["kind"]))
    if p["addr"]:
        bits.append(e(p["addr"]))
    if p["hours"]:
        bits.append("🕐 " + e(p["hours"]))
    bits.append(f'<a href="{yandex_route(lat0, lon0, p["lat"], p["lon"])}">🧭 маршрут</a>'
                f' · <a href="{google_pin(p["lat"], p["lon"])}">Google</a>')
    return head + "\n   " + " · ".join(bits)


def _summary_text(sr: dict, only: str | None = None) -> str:
    lat0, lon0 = sr["lat"], sr["lon"]
    cats = [only] if only else list(CATS)
    limit = PER_CAT_FULL if only else PER_CAT_SHORT
    out = [f"📍 <b>Рядом с тобой</b> (радиус до {sr['radius']} м){' — ' + html.escape(sr['label']) if sr.get('label') else ''}"]
    for c in cats:
        items = sr["places"].get(c, [])[:limit]
        out.append(f"\n{CATS[c]['emoji']} <b>{CATS[c]['label']}</b>")
        if not items:
            out.append("   ничего не нашлось поблизости 🤷")
            continue
        out.extend(_place_line(i, p, lat0, lon0) for i, p in enumerate(items, 1))
    out.append("\n<i>Данные OpenStreetMap — часы работы и адреса могут быть неполными</i>")
    return "\n".join(out)[:4000]


def _kb(tok: str, sr: dict, only: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if only:
        rows.append([InlineKeyboardButton(text="⬅️ Всё рядом", callback_data=f"pl:all:{tok}")])
    else:
        rows.append([InlineKeyboardButton(text=f"{v['emoji']} {v['label'].split()[0]}", callback_data=f"pl:c:{k}:{tok}")
                     for k, v in CATS.items()])
    cats = [only] if only else list(CATS)
    pts = [(p["lat"], p["lon"]) for c in cats for p in sr["places"].get(c, [])[:(PER_CAT_FULL if only else PER_CAT_SHORT)]]
    if pts:
        rows.append([InlineKeyboardButton(text="🗺 Все точки на карте",
                                          url=yandex_multi(pts, (sr["lat"], sr["lon"])))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Основной сценарий ─────────────────────────────────────────────────────────
async def show_nearby(message: types.Message, lat: float, lon: float,
                      only: str | None = None, label: str = "", user_id: int | None = None) -> bool:
    uid = user_id or (message.from_user.id if message.from_user else 0)
    if uid:
        remember_location(uid, lat, lon)
    status = await message.reply("🔎 Ищу кафе, аптеки и магазины рядом…", reply_markup=ReplyKeyboardRemove())
    found = await search_places(lat, lon)
    if found is None:
        await status.edit_text("❌ Карты сейчас не отвечают (Overpass). Попробуй через минуту.")
        return False
    places, radius = found
    sr = {"lat": lat, "lon": lon, "radius": radius, "places": places, "label": label}
    tok = secrets.token_hex(4)
    _SEARCHES[tok] = sr
    while len(_SEARCHES) > 100:
        _SEARCHES.popitem(last=False)
    if not any(places.values()):
        await status.edit_text("🤷 В радиусе 3 км по картам ничего не нашлось.")
        return False
    await status.edit_text(_summary_text(sr, only), parse_mode="HTML",
                           reply_markup=_kb(tok, sr, only), disable_web_page_preview=True)
    return True


async def request_location(message: types.Message, hint: str = ""):
    """Просит геопозицию: кнопка в личке, ForceReply в группах."""
    text = (hint + "\n\n" if hint else "") + "📍 Скинь геопозицию — найду кафе, аптеки и магазины рядом."
    if message.chat.type == "private":
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True)
        await message.answer(text, reply_markup=kb)
    else:
        await message.reply(
            text + "\n<i>Ответь на это сообщение геопозицией (📎 → Геопозиция) или адресом: "
                   "<code>/nearby Тверская 7, Москва</code></i>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="📎 → Геопозиция"))


async def cmd_nearby(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    only = None
    if arg:
        first, _, rest = arg.partition(" ")
        cat = detect_category(first)
        if cat:
            only, arg = cat, rest.strip()

    # 1) команда ответом на сообщение с геопозицией
    rep = message.reply_to_message
    if rep and rep.location:
        await show_nearby(message, rep.location.latitude, rep.location.longitude, only)
        return
    # 2) адрес текстом
    if arg:
        geo = await geocode(arg)
        if not geo:
            await message.reply("🤷 Не нашёл такой адрес. Попробуй точнее или скинь геопозицию.")
            return
        await show_nearby(message, geo[0], geo[1], only, label=geo[2])
        return
    # 3) недавно присланная геопозиция
    uid = message.from_user.id if message.from_user else 0
    last = recall_location(uid)
    if last:
        await show_nearby(message, last[0], last[1], only)
        return
    await request_location(message)


async def handle_location(message: types.Message):
    loc = message.location
    if not loc:
        return
    await show_nearby(message, loc.latitude, loc.longitude)


async def _location_filter(message: types.Message) -> bool:
    """В личке — любая геопозиция; в группах — только ответ на сообщение бота."""
    if not message.location:
        return False
    if message.chat.type == "private":
        return True
    rep = message.reply_to_message
    if rep and rep.from_user:
        me = await message.bot.me()
        return rep.from_user.id == me.id
    return False


async def callback_places(callback: types.CallbackQuery):
    parts = (callback.data or "").split(":")
    # pl:all:<tok>  |  pl:c:<cat>:<tok>
    if len(parts) < 3:
        await callback.answer()
        return
    tok = parts[-1]
    sr = _SEARCHES.get(tok)
    if not sr:
        await callback.answer("Результаты устарели — скинь геопозицию заново", show_alert=True)
        return
    only = parts[2] if parts[1] == "c" and len(parts) == 4 and parts[2] in CATS else None
    try:
        await callback.message.edit_text(_summary_text(sr, only), parse_mode="HTML",
                                         reply_markup=_kb(tok, sr, only), disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"places edit: {e}")
    await callback.answer()


def register_places_handlers(dp):
    dp.message.register(cmd_nearby, Command("nearby", "near", "around"))
    dp.message.register(handle_location, _location_filter)
    dp.callback_query.register(callback_places, F.data.startswith("pl:"))
    logger.info("📍 places_handlers зарегистрированы (/nearby + геопозиция)")
