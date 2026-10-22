"""
freebies.py — бесплатные раздачи игр и крупные скидки.

Источники (ключи не нужны):
  • Epic Games Store — официальный endpoint freeGamesPromotions (с датами окончания и анонсом следующей раздачи)
  • GamerPower — раздачи Steam, GOG, Ubisoft, itch.io, Epic и др. (поиск «что раздают сегодня»)
  • CheapShark — крупные скидки в Steam на хорошие игры (рейтинг ≥ 80%)

Команды:
  /free            — что раздают прямо сейчас (с кнопками фильтра и подписки)
  /free epic|steam — только магазин
  /free sale       — крупные скидки в Steam
  /free on|off     — уведомления в этом чате (в группах — только админы)
  /free sale 70    — порог скидки для уведомлений о распродажах (по умолчанию 75%)

Фоновая проверка: check_freebies(bot) раз в 2 часа (подключается в main.py).
Первый запуск только запоминает текущие раздачи и ничего не шлёт — чтобы не было потока старого.
Основной чат из duty-настроек подписан по умолчанию (отключается /free off).
"""

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (NaryadBot freebies)"}
EPIC_URL = ("https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
            "?locale=ru&country=RU&allowCountries=RU")
GAMERPOWER_URL = "https://www.gamerpower.com/api/giveaways?type=game&sort-by=date"
CHEAPSHARK_URL = "https://www.cheapshark.com/api/1.0/deals"
DEFAULT_MIN_DISCOUNT = 75
CACHE_TTL = 600


@dataclass
class Deal:
    key: str
    kind: str                 # free | sale | upcoming
    title: str
    store: str
    url: str
    image: str = ""
    start: datetime | None = None
    end: datetime | None = None
    old_price: str = ""
    new_price: str = ""
    discount: int = 0
    note: str = ""

    @property
    def store_group(self) -> str:
        s = self.store.lower()
        return "epic" if "epic" in s else "steam" if "steam" in s else "other"


# ── Работа с БД (таблицы в общей sqlite из db.py) ─────────────────────────────
def _conn():
    from db import _db_connect
    return _db_connect()


def init_freebies_db():
    c = _conn()
    c.execute("CREATE TABLE IF NOT EXISTS free_seen (key TEXT PRIMARY KEY, first_seen INTEGER NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS free_subs (chat_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 1, "
              "min_discount INTEGER DEFAULT 75)")
    c.execute("CREATE TABLE IF NOT EXISTS free_meta (k TEXT PRIMARY KEY, v TEXT)")
    c.commit()
    c.close()


def _db(fn):
    c = _conn()
    try:
        r = fn(c)
        c.commit()
        return r
    finally:
        c.close()


def get_sub(chat_id: int) -> dict | None:
    row = _db(lambda c: c.execute("SELECT enabled, min_discount FROM free_subs WHERE chat_id=?", (chat_id,)).fetchone())
    return {"enabled": bool(row[0]), "min_discount": row[1]} if row else None


def set_sub(chat_id: int, enabled: bool | None = None, min_discount: int | None = None):
    def run(c):
        c.execute("INSERT OR IGNORE INTO free_subs(chat_id, enabled, min_discount) VALUES(?,1,?)",
                  (chat_id, DEFAULT_MIN_DISCOUNT))
        if enabled is not None:
            c.execute("UPDATE free_subs SET enabled=? WHERE chat_id=?", (int(enabled), chat_id))
        if min_discount is not None:
            c.execute("UPDATE free_subs SET min_discount=? WHERE chat_id=?", (min_discount, chat_id))
    _db(run)


def _is_seeded() -> bool:
    return bool(_db(lambda c: c.execute("SELECT 1 FROM free_meta WHERE k='seeded'").fetchone()))


def _mark_seeded():
    _db(lambda c: c.execute("INSERT OR REPLACE INTO free_meta(k,v) VALUES('seeded','1')"))


def _filter_unseen(deals: list[Deal]) -> list[Deal]:
    def run(c):
        out = []
        for d in deals:
            if not c.execute("SELECT 1 FROM free_seen WHERE key=?", (d.key,)).fetchone():
                out.append(d)
        return out
    return _db(run)


def _mark_seen(deals: list[Deal]):
    now = int(time.time())

    def run(c):
        c.executemany("INSERT OR IGNORE INTO free_seen(key, first_seen) VALUES(?,?)", [(d.key, now) for d in deals])
        c.execute("DELETE FROM free_seen WHERE key LIKE 'sale:%' AND first_seen < ?", (now - 45 * 86400,))
    _db(run)


# ── Загрузка источников ───────────────────────────────────────────────────────
async def _get_json(session: aiohttp.ClientSession, url: str, **params):
    try:
        async with session.get(url, params=params or None, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
            logger.info("freebies %s → HTTP %s", url.split("?")[0], r.status)
    except Exception as e:
        logger.info("freebies %s: %s: %s", url.split("?")[0], type(e).__name__, e)
    return None


def _parse_dt(s: str | None) -> datetime | None:
    if not s or s.upper() == "N/A":
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _epic_slug(el: dict) -> str:
    for m in (el.get("offerMappings") or []) + ((el.get("catalogNs") or {}).get("mappings") or []):
        if m.get("pageSlug"):
            return m["pageSlug"]
    slug = el.get("productSlug") or el.get("urlSlug") or ""
    return slug.split("/")[0] if slug and slug != "[]" else ""


def parse_epic(data: dict) -> list[Deal]:
    out: list[Deal] = []
    els = (((data or {}).get("data") or {}).get("Catalog") or {}).get("searchStore", {}).get("elements", []) or []
    for el in els:
        promos = el.get("promotions") or {}
        price = (el.get("price") or {}).get("totalPrice") or {}
        orig = price.get("originalPrice") or 0
        slug = _epic_slug(el)
        url = f"https://store.epicgames.com/ru/p/{slug}" if slug else "https://store.epicgames.com/ru/free-games"
        img = ""
        for ki in el.get("keyImages") or []:
            if ki.get("type") in ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail") and ki.get("url"):
                img = ki["url"]
                break
        fmt = price.get("fmtPrice") or {}

        def add(offer: dict, kind: str):
            ds = offer.get("discountSetting") or {}
            if ds.get("discountPercentage") not in (0, None) and kind == "free":
                return
            end = _parse_dt(offer.get("endDate"))
            out.append(Deal(
                key=f"epic:{el.get('id')}:{offer.get('endDate', '')}", kind=kind, title=el.get("title", "?"),
                store="Epic Games Store", url=url, image=img, start=_parse_dt(offer.get("startDate")), end=end,
                old_price=fmt.get("originalPrice", ""), new_price="0", discount=100))

        for grp in promos.get("promotionalOffers") or []:
            for offer in grp.get("promotionalOffers") or []:
                if orig > 0 and (price.get("discountPrice") == 0):
                    add(offer, "free")
        for grp in promos.get("upcomingPromotionalOffers") or []:
            for offer in grp.get("promotionalOffers") or []:
                ds = offer.get("discountSetting") or {}
                if orig > 0 and ds.get("discountPercentage") == 0:
                    add(offer, "upcoming")
    return out


def parse_gamerpower(data) -> list[Deal]:
    out: list[Deal] = []
    if not isinstance(data, list):
        return out
    for g in data:
        if g.get("status") not in (None, "Active"):
            continue
        title = re.sub(r"\s*\((?:Steam|Epic Games|GOG|Ubisoft|itch\.io)[^)]*\)\s*(?:Giveaway)?$", "",
                       g.get("title", "?")).replace(" Giveaway", "").strip()
        worth = g.get("worth") or ""
        out.append(Deal(
            key=f"gp:{g.get('id')}", kind="free", title=title or "?", store=g.get("platforms", "").split(",")[0].strip()
            or "PC", url=g.get("open_giveaway_url") or g.get("gamerpower_url") or "", image=g.get("image") or g.get("thumbnail") or "",
            start=_parse_dt(g.get("published_date")), end=_parse_dt(g.get("end_date")),
            old_price=worth if worth and worth != "N/A" else "", new_price="0", discount=100,
            note=("" if (g.get("type") or "Game") == "Game" else g.get("type", ""))))
    return out


def parse_cheapshark(data, min_discount: int = DEFAULT_MIN_DISCOUNT) -> list[Deal]:
    out: list[Deal] = []
    if not isinstance(data, list):
        return out
    for g in data:
        try:
            savings = int(float(g.get("savings", 0)))
            sale = float(g.get("salePrice", 0))
            normal = float(g.get("normalPrice", 0))
            rating = int(g.get("steamRatingPercent") or 0)
            votes = int(g.get("steamRatingCount") or 0)
        except (TypeError, ValueError):
            continue
        app = g.get("steamAppID")
        if not app or savings < min_discount or normal <= 0:
            continue
        free = sale == 0
        if not free and (rating < 80 or votes < 500):          # на распродажах — только проверенные игры
            continue
        out.append(Deal(
            key=f"sale:{app}:{sale:.2f}", kind="free" if free else "sale", title=g.get("title", "?"), store="Steam",
            url=f"https://store.steampowered.com/app/{app}", image=g.get("thumb", ""),
            old_price=f"${normal:.2f}", new_price="0" if free else f"${sale:.2f}", discount=savings,
            note=f"👍 {rating}% ({votes:,})".replace(",", " ") if rating else ""))
    return out


_CACHE: dict = {"ts": 0.0, "deals": []}


def _norm(t: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", t.lower())


async def fetch_all(force: bool = False) -> list[Deal]:
    """Все актуальные раздачи (free/upcoming) и скидки (sale) с дедупликацией по названию."""
    if not force and time.time() - _CACHE["ts"] < CACHE_TTL and _CACHE["deals"]:
        return _CACHE["deals"]
    async with aiohttp.ClientSession(headers=UA) as s:
        epic, gp, cs_free, cs_sale = await asyncio.gather(
            _get_json(s, EPIC_URL),
            _get_json(s, GAMERPOWER_URL),
            _get_json(s, CHEAPSHARK_URL, storeID=1, upperPrice=0, onSale=1, pageSize=20),
            _get_json(s, CHEAPSHARK_URL, storeID=1, onSale=1, sortBy="Savings", desc=1, steamRating=80,
                      pageSize=30, lowerPrice=1),
        )
    deals: list[Deal] = []
    seen_titles: set[str] = set()
    epic_deals = parse_epic(epic) if epic else []
    for d in epic_deals:                                  # Epic — приоритетный источник
        deals.append(d)
        if d.kind == "free":
            seen_titles.add(_norm(d.title))
    for d in parse_gamerpower(gp) + parse_cheapshark(cs_free, 100):
        n = _norm(d.title)
        if n in seen_titles or (epic_deals and "epic" in d.store.lower()):
            continue
        seen_titles.add(n)
        deals.append(d)
    deals += parse_cheapshark(cs_sale, 50)
    if deals:
        _CACHE.update(ts=time.time(), deals=deals)
    return deals


# ── Форматирование ────────────────────────────────────────────────────────────
def _left(end: datetime | None) -> str:
    if not end:
        return "без срока"
    sec = (end - datetime.now(timezone.utc)).total_seconds()
    if sec <= 0:
        return "заканчивается"
    d, rem = divmod(int(sec), 86400)
    h = rem // 3600
    return f"ещё {d} дн. {h} ч" if d else (f"ещё {h} ч" if h else "меньше часа!")


def _msk(dt: datetime | None) -> str:
    if not dt:
        return ""
    from config import TIMEZONE
    return dt.astimezone(TIMEZONE).strftime("%d.%m %H:%M")


def deal_line(d: Deal) -> str:
    e = html.escape
    urgent = d.end and (d.end - datetime.now(timezone.utc)).total_seconds() < 86400
    price = f" · было {e(d.old_price)}" if d.old_price and d.kind != "sale" else ""
    if d.kind == "sale":
        price = f" · <s>{e(d.old_price)}</s> → <b>{e(d.new_price)}</b> (−{d.discount}%)"
    when = f"\n   ⏳ до {_msk(d.end)} МСК ({_left(d.end)})" if d.end else ""
    flag = "🔥 " if urgent else ""
    note = f" · {e(d.note)}" if d.note else ""
    return f'{flag}<a href="{e(d.url)}"><b>{e(d.title)}</b></a> — {e(d.store)}{price}{note}{when}'


def build_list_text(deals: list[Deal], group: str = "all") -> str:
    free = [d for d in deals if d.kind == "free" and (group in ("all", d.store_group))]
    free.sort(key=lambda d: (d.end is None, d.end or datetime.max.replace(tzinfo=timezone.utc)))
    soon = [d for d in deals if d.kind == "upcoming" and group in ("all", "epic")]
    title = {"all": "Бесплатно сейчас", "epic": "Epic Games — раздача", "steam": "Steam — бесплатно",
             "other": "Другие магазины — бесплатно"}.get(group, "Бесплатно сейчас")
    out = [f"🎁 <b>{title}</b> ({len(free)})"]
    if not free:
        out.append("\nСейчас ничего не раздают 🤷")
    for d in free[:12]:
        out.append("\n" + deal_line(d))
    if len(free) > 12:
        out.append(f"\n…и ещё {len(free) - 12}")
    if soon:
        out.append("\n\n🔜 <b>Скоро (Epic)</b>")
        for d in soon[:3]:
            out.append(f"\n• <b>{html.escape(d.title)}</b> — с {_msk(d.start)} МСК")
    out.append("\n\n<i>🔥 — заканчивается меньше чем через сутки</i>")
    return "".join(out)[:4000]


def build_sale_text(deals: list[Deal]) -> str:
    sales = sorted((d for d in deals if d.kind == "sale"), key=lambda d: -d.discount)[:10]
    if not sales:
        return "💸 Крупных скидок на проверенные игры в Steam сейчас не нашёл."
    return "💸 <b>Крупные скидки в Steam</b> (рейтинг ≥ 80%)\n" + "".join("\n" + deal_line(d) for d in sales) \
        + "\n\n<i>Цены в USD по данным CheapShark; в РФ-магазине могут отличаться</i>"


def list_kb(group: str, subscribed: bool) -> InlineKeyboardMarkup:
    def b(label, g):
        return InlineKeyboardButton(text=("• " if g == group else "") + label, callback_data=f"free:f:{g}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("Все", "all"), b("Epic", "epic"), b("Steam", "steam"), b("Другие", "other")],
        [InlineKeyboardButton(text="💸 Скидки Steam", callback_data="free:sale"),
         InlineKeyboardButton(text="🔄", callback_data=f"free:f:{group}:r")],
        [InlineKeyboardButton(text="🔕 Выключить уведомления" if subscribed else "🔔 Включить уведомления",
                              callback_data="free:sub:off" if subscribed else "free:sub:on")],
    ])


# ── Команды ───────────────────────────────────────────────────────────────────
async def _can_manage(message: types.Message, user_id: int | None = None) -> bool:
    if message.chat.type == "private":
        return True
    uid = user_id or (message.from_user.id if message.from_user else 0)
    try:
        m = await message.bot.get_chat_member(message.chat.id, uid)
        return m.status in ("creator", "administrator")
    except Exception:
        return False


def _subscribed(chat_id: int) -> bool:
    s = get_sub(chat_id)
    return bool(s and s["enabled"])


async def show_free(message: types.Message, group: str = "all", force: bool = False) -> bool:
    status = await message.answer("🎁 Смотрю раздачи…")
    deals = await fetch_all(force)
    if not deals:
        await status.edit_text("❌ Источники раздач сейчас не отвечают. Попробуй позже.")
        return False
    await status.edit_text(build_list_text(deals, group), parse_mode="HTML",
                           reply_markup=list_kb(group, _subscribed(message.chat.id)),
                           disable_web_page_preview=True)
    return True


async def cmd_free(message: types.Message):
    parts = (message.text or "").split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    chat_id = message.chat.id
    if sub in ("on", "вкл"):
        if not await _can_manage(message):
            await message.reply("🔒 В группе уведомления включают только админы.")
            return
        set_sub(chat_id, True)
        await message.reply("🔔 Включил: пришлю, когда игра станет бесплатной в Epic/Steam и т.д. "
                            f"и при скидках от {(get_sub(chat_id) or {}).get('min_discount', DEFAULT_MIN_DISCOUNT)}% "
                            "на хорошие игры Steam.")
    elif sub in ("off", "выкл"):
        if not await _can_manage(message):
            await message.reply("🔒 В группе уведомления выключают только админы.")
            return
        set_sub(chat_id, False)
        await message.reply("🔕 Уведомления о раздачах выключены.")
    elif sub == "sale":
        if len(parts) > 2 and parts[2].isdigit():
            if not await _can_manage(message):
                await message.reply("🔒 Порог меняют только админы.")
                return
            pct = max(30, min(95, int(parts[2])))
            set_sub(chat_id, None, pct)
            await message.reply(f"💸 Порог скидки для уведомлений: <b>{pct}%</b>", parse_mode="HTML")
            return
        deals = await fetch_all()
        await message.answer(build_sale_text(deals), parse_mode="HTML", disable_web_page_preview=True)
    elif sub in ("epic", "steam", "other"):
        await show_free(message, sub)
    else:
        await show_free(message, "all")


async def callback_free(callback: types.CallbackQuery):
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    msg = callback.message
    try:
        if action == "f":
            group = parts[2] if len(parts) > 2 else "all"
            deals = await fetch_all(force=len(parts) > 3)
            await msg.edit_text(build_list_text(deals, group), parse_mode="HTML",
                                reply_markup=list_kb(group, _subscribed(msg.chat.id)),
                                disable_web_page_preview=True)
            await callback.answer()
        elif action == "sale":
            deals = await fetch_all()
            await msg.answer(build_sale_text(deals), parse_mode="HTML", disable_web_page_preview=True)
            await callback.answer()
        elif action == "sub":
            if not await _can_manage(msg, callback.from_user.id):
                await callback.answer("Только админы чата", show_alert=True)
                return
            on = parts[2] == "on"
            set_sub(msg.chat.id, on)
            await callback.answer("🔔 Включено" if on else "🔕 Выключено")
            try:
                await msg.edit_reply_markup(reply_markup=list_kb("all", on))
            except Exception:
                pass
        else:
            await callback.answer()
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning("callback_free: %s", e)
        await callback.answer()


# ── Фоновая проверка и рассылка ───────────────────────────────────────────────
def _main_chat() -> int | None:
    try:
        from config import CHAT_ID
        from duty_handlers import load_data
        cid = load_data().get("chat_id", CHAT_ID)
        return int(cid) if cid and str(cid) != "YOUR_CHAT_ID_HERE" else None
    except Exception:
        return None


def _recipients() -> list[tuple[int, int]]:
    """[(chat_id, min_discount)] — подписанные чаты + основной чат (если явно не выключен)."""
    rows = _db(lambda c: c.execute("SELECT chat_id, enabled, min_discount FROM free_subs").fetchall())
    res = {r[0]: (r[2], bool(r[1])) for r in rows}
    main = _main_chat()
    if main is not None and main not in res:
        res[main] = (DEFAULT_MIN_DISCOUNT, True)
    return [(cid, md) for cid, (md, en) in res.items() if en]


def _digest(new: list[Deal]) -> str:
    free = [d for d in new if d.kind == "free"]
    sales = [d for d in new if d.kind == "sale"]
    out = []
    if free:
        out.append("🎁 <b>Новая бесплатная раздача!</b>" if len(free) == 1 else f"🎁 <b>Новые раздачи ({len(free)})!</b>")
        out += ["\n" + deal_line(d) for d in free[:8]]
    if sales:
        out.append(("\n\n" if free else "") + "💸 <b>Большая скидка в Steam</b>")
        out += ["\n" + deal_line(d) for d in sales[:6]]
    out.append("\n\n<i>/free — все раздачи · /free off — отключить</i>")
    return "".join(out)[:4000]


async def check_freebies(bot) -> None:
    try:
        deals = [d for d in await fetch_all(force=True) if d.kind in ("free", "sale")]
        if not deals:
            return
        unseen = _filter_unseen(deals)
        if not _is_seeded():
            _mark_seen(deals)
            _mark_seeded()
            logger.info("🎁 freebies: первый запуск, запомнил %d текущих раздач/скидок без рассылки", len(deals))
            return
        if not unseen:
            return
        _mark_seen(unseen)
        for chat_id, min_disc in _recipients():
            mine = [d for d in unseen if d.kind == "free" or d.discount >= min_disc]
            if not mine:
                continue
            try:
                if len(mine) == 1 and mine[0].image:
                    d = mine[0]
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Забрать", url=d.url)]])
                    await bot.send_photo(chat_id, d.image, caption=_digest(mine)[:1024], parse_mode="HTML",
                                         reply_markup=kb)
                else:
                    await bot.send_message(chat_id, _digest(mine), parse_mode="HTML", disable_web_page_preview=True)
            except Exception as e:
                logger.warning("freebies: не отправил в %s: %s", chat_id, e)
                try:
                    await bot.send_message(chat_id, _digest(mine), parse_mode="HTML", disable_web_page_preview=True)
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        logger.info("🎁 freebies: новых %d, разослано", len(unseen))
    except Exception as e:
        logger.error("check_freebies: %s", e, exc_info=True)


def register_freebies_handlers(dp):
    init_freebies_db()
    dp.message.register(cmd_free, Command("free", "freegames", "giveaway"))
    dp.callback_query.register(callback_free, F.data.startswith("free:"))
    logger.info("🎁 freebies зарегистрирован (/free, фоновая проверка — check_freebies)")
