"""
cases_game.py — 🎰 КЕЙС-КЛУБ: игра с кейсами, монетами и соревнованием между игроками.

Игровой цикл:
  • У каждого игрока монеты 🪙 (старт 1000) и инвентарь предметов (скины CS, машины, ценные вещи, хлам).
  • Кейсы платные: Стартовый 100 · Оружейный CS 400 · Гараж 1500 · Люкс 5000 · Мифик 20 000.
    Шансы посчитаны так, чтобы в среднем кейс возвращал ~90% цены (RTP) — монеты медленно утекают,
    а удача и умение вовремя продавать решают, кто в топе.
  • Выпавший предмет можно продать за его цену (кнопка под дропом или в инвентаре) либо копить.
  • 🎁 Раз в день — бонус монет (серия дней даёт больше). 🆘 Если игрок «на нуле» (монет и стоимости
    инвентаря меньше цены самого дешёвого кейса) — гарантированное пособие (раз в 2 часа).
  • 🏆 Топ по капиталу (монеты + инвентарь) и ⚔️ БАТЛЫ: двое платят за один кейс, у кого дороже дроп —
    забирает оба предмета.

Дополнительно:
  • 🚀 АПГРЕЙД: рискнуть предметом ради более дорогого (шанс = 90% × цена/цена_цели — честный).
  • 🧾 КОНТРАКТ: 3–10 предметов → один (в среднем ~90% суммарной стоимости, с шансом на большой куш).
  • 🎟 Бесплатный кейс раз в день и 💥 «предложение дня» (−15% на один кейс, 1 раз в день).
  • 🎁 /give — подарки монет (комиссия 5%) и предметов, 🤝 /trade — обмен предмет на предмет.
  • 📅 Сезоны по 14 дней: топ-3 по ПРИРОСТУ капитала получают кубки (прогресс не сбрасывается),
    топ отдельно по чату. 🏅 Достижения и титулы.

Команды: /case (меню), /daily, /inv, /ctop, /give, /trade. Всё остальное — кнопками.
Данные: SQLite-файл (CASES_DB_PATH, по умолчанию cases_game.db рядом с модулем).
Подключение: register_cases_handlers(dp) в main.py.
"""

import asyncio
import html
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.getenv("CASES_TZ", "Europe/Moscow"))
except Exception:                                    # нет tzdata — фиксированный МСК
    _TZ = timezone(timedelta(hours=3))

logger = logging.getLogger(__name__)
_RNG = secrets.SystemRandom()

# ── Настройки экономики ───────────────────────────────────────────────────────
START_COINS = 1000
DAILY_BASE = 500            # ежедневный бонус
DAILY_STEP = 100            # +100 за каждый день серии подряд
DAILY_STREAK_MAX = 6        # максимум +600 (итого 1100 на 7-й день серии)
BAILOUT = 300               # пособие «на нуле»
BAILOUT_COOLDOWN = 2 * 3600
RTP = 0.90                  # какую долю цены кейс возвращает в среднем
CONFIRM_SELL_AT = 5000      # дороже этого — продажа с подтверждением
BATTLE_TTL = 20 * 60        # открытый батл «живёт» 20 минут, потом возврат ставки
INV_PER_PAGE = 6
SEASON_DAYS = 14            # длина сезона
DEAL_DISCOUNT = 0.15        # скидка «предложения дня» (1 раз в день на игрока)
UPGRADE_MULTS = (1.5, 2, 3, 5, 10, 25)
UPGRADE_MIN_CHANCE, UPGRADE_MAX_CHANCE = 0.01, 0.75
CONTRACT_MIN, CONTRACT_MAX = 3, 10
# (множитель к сумме, вероятность): матожидание ≈ 0.9 — контракт не «печатает деньги»
CONTRACT_OUTCOMES = [(0.4, 0.42), (0.8, 0.31), (1.2, 0.17), (2.0, 0.07), (4.0, 0.025), (8.0, 0.005)]
GIVE_MIN, GIVE_FEE, GIVE_DAILY_CAP = 10, 0.05, 20000     # подарки: минимум, комиссия, лимит в сутки (в 🪙-стоимости)
TRADE_TTL = 30 * 60
COMMANDS = {"menu": ("case", "cases"), "daily": ("daily",), "inv": ("inv", "inventory"), "top": ("ctop",),
            "give": ("give",), "trade": ("trade",)}

# ── Каталог ───────────────────────────────────────────────────────────────────
RARITY = {
    "junk":   ("🗑", "Хлам"),
    "common": ("⚪", "Обычный"),
    "rare":   ("🔵", "Редкий"),
    "epic":   ("🟣", "Эпический"),
    "legend": ("🔴", "Легендарный"),
    "mythic": ("🟡", "Мифический"),
}
RARITY_ORDER = list(RARITY)

# (категория, редкость, ключ, название, цена продажи в 🪙). Ключ НЕ менять — он лежит в инвентарях.
_ITEMS_RAW = [
 ("cs","junk","cs_p250_sand","P250 | Sand Dune",22),("cs","junk","cs_nova_walnut","Nova | Walnut",30),("cs","junk","cs_sg_sheen","SG 553 | Army Sheen",38),
 ("cs","common","cs_mp9_bull","MP9 | Bulldozer",90),("cs","common","cs_famas_col","FAMAS | Colony",120),("cs","common","cs_dual_con","Dual Berettas | Contractor",150),("cs","common","cs_glock_water","Glock-18 | Water Elemental",190),
 ("cs","rare","cs_m4s_guard","M4A1-S | Guardian",450),("cs","rare","cs_usp_orion","USP-S | Orion",650),("cs","rare","cs_ak_redline","AK-47 | Redline",800),("cs","rare","cs_deagle_blaze","Desert Eagle | Blaze",950),
 ("cs","epic","cs_awp_asiimov","AWP | Asiimov",1800),("cs","epic","cs_ak_vulcan","AK-47 | Vulcan",2600),("cs","epic","cs_m4a4_neo","M4A4 | Neo-Noir",3200),("cs","epic","cs_glock_fade","Glock-18 | Fade",4200),
 ("cs","legend","cs_ak_fire","AK-47 | Fire Serpent",9000),("cs","legend","cs_kara_doppler","★ Karambit | Doppler",14000),("cs","legend","cs_awp_lore","AWP | Dragon Lore",28000),
 ("cs","mythic","cs_m4_howl","M4A4 | Howl",90000),("cs","mythic","cs_butterfly","★ Butterfly Knife | Fade",120000),("cs","mythic","cs_gloves","★ Sport Gloves | Pandora's Box",180000),

 ("car","junk","car_pine","Ёлочка-ароматизатор",12),("car","junk","car_wheelbike","Колесо от велосипеда",20),("car","junk","car_sign","Знак «Шипы»",32),
 ("car","common","car_vac","Автомобильный пылесос",110),("car","common","car_dvr","Видеорегистратор",160),("car","common","car_scooter","Самокат (б/у)",210),("car","common","car_tires","Комплект зимней резины",240),
 ("car","rare","car_granta","Lada Granta (б/у)",600),("car","rare","car_niva","Lada Niva",900),("car","rare","car_rapid","Skoda Rapid",1150),("car","rare","car_moto","Мотоцикл Yamaha",1300),
 ("car","epic","car_camry","Toyota Camry",4500),("car","epic","car_bmw3","BMW 3 Series",6000),("car","epic","car_tesla","Tesla Model 3",8500),
 ("car","legend","car_g63","Mercedes-AMG G63",24000),("car","legend","car_911","Porsche 911",38000),("car","legend","car_urus","Lamborghini Urus",45000),
 ("car","mythic","car_aventador","Lamborghini Aventador",150000),("car","mythic","car_laferrari","Ferrari LaFerrari",280000),("car","mythic","car_chiron","Bugatti Chiron",450000),

 ("lux","junk","lux_magnet","Магнит на холодильник",12),("lux","junk","lux_ticket","Просроченный билет в кино",18),("lux","junk","lux_piggy","Пустая копилка",25),
 ("lux","common","lux_pbank","Powerbank 20 000 мАч",130),("lux","common","lux_buds","Беспроводные наушники",180),("lux","common","lux_watch","Умные часы",260),("lux","common","lux_kb","Механическая клавиатура",300),
 ("lux","rare","lux_switch","Nintendo Switch",500),("lux","rare","lux_ps5","PlayStation 5",700),("lux","rare","lux_iphone","iPhone 16",850),("lux","rare","lux_gpu","Видеокарта RTX 4070",1100),
 ("lux","epic","lux_mac","MacBook Pro",3500),("lux","epic","lux_gold100","Золотой слиток 100 г",5500),("lux","epic","lux_rolex","Rolex Submariner",7000),
 ("lux","legend","lux_diamond","Бриллиант 5 карат",26000),("lux","legend","lux_flat","Квартира-студия",38000),("lux","legend","lux_bar","Золотой слиток 1 кг",42000),
 ("lux","mythic","lux_penthouse","Пентхаус в Москва-Сити",300000),("lux","mythic","lux_yacht","Яхта",420000),("lux","mythic","lux_island","Частный остров",600000),
]

ITEMS: dict[str, dict] = {k: {"name": n, "price": p, "rarity": r, "cat": c} for c, r, k, n, p in _ITEMS_RAW}

# Кейсы: тиры выше «sink» задаются относительными весами (shape); sink (самый частый тир) добирает остаток,
# а общий масштаб подбирается автоматически так, чтобы средняя выплата = RTP × цена.
CASES = {
    "starter": dict(icon="🎁", name="Стартовый", price=100, cats=("cs", "car", "lux"), sink="junk",
                    shape={"common": 1.0, "rare": 0.2},
                    desc="всё понемногу: хлам, обычное, иногда редкое"),
    "cs":      dict(icon="🔫", name="Оружейный CS", price=400, cats=("cs",), sink="junk",
                    shape={"common": 1.0, "rare": 0.55, "epic": 0.15, "legend": 0.015, "mythic": 0.0015},
                    desc="скины из CS: от Sand Dune до Howl и перчаток"),
    "garage":  dict(icon="🚗", name="Гараж", price=1500, cats=("car",), sink="junk",
                    shape={"common": 1.0, "rare": 0.6, "epic": 0.18, "legend": 0.02, "mythic": 0.002},
                    desc="от ёлочки до Bugatti Chiron"),
    "lux":     dict(icon="💎", name="Люкс", price=5000, cats=("lux",), sink="common",
                    shape={"rare": 1.0, "epic": 0.5, "legend": 0.07, "mythic": 0.006},
                    desc="гаджеты, золото, бриллианты, недвижимость"),
    "mythic":  dict(icon="🌌", name="Мифик", price=20000, cats=("cs", "car", "lux"), sink="rare",
                    shape={"epic": 1.0, "legend": 0.9, "mythic": 0.06},
                    desc="только дорогое: эпик, легенда, миф. Для смелых"),
    # бесплатный кейс дня: цена 0, средняя выплата задана напрямую (ev_target), в магазине не продаётся
    "free":    dict(icon="🎟", name="Бесплатный кейс дня", price=0, cats=("cs", "car", "lux"), sink="junk",
                    shape={"common": 1.0, "rare": 0.25, "epic": 0.03}, ev_target=70, free=True,
                    desc="раз в день бесплатно — хлам, обычное, иногда редкое"),
}
SHOP = [k for k, c in CASES.items() if not c.get("free")]
MIN_PRICE = min(CASES[k]["price"] for k in SHOP)


def _avg_price(cats, rarity) -> float | None:
    ps = [v["price"] for v in ITEMS.values() if v["cat"] in cats and v["rarity"] == rarity]
    return sum(ps) / len(ps) if ps else None


def _build_cases():
    for key, c in CASES.items():
        pool = {r: [k for k, v in ITEMS.items() if v["cat"] in c["cats"] and v["rarity"] == r] for r in RARITY_ORDER}
        sink, shape = c["sink"], c["shape"]
        p_sink = _avg_price(c["cats"], sink)
        gain = sum(w * (_avg_price(c["cats"], t) - p_sink) for t, w in shape.items())
        alpha = (c.get("ev_target", RTP * c["price"]) - p_sink) / gain
        weights = {t: alpha * w for t, w in shape.items()}
        weights[sink] = 1 - sum(weights.values())
        assert weights[sink] > 0 and all(pool[t] for t in weights), f"кейс {key}: не сходится экономика"
        c["key"], c["pool"], c["weights"] = key, pool, weights
        c["ev"] = sum(weights[t] * _avg_price(c["cats"], t) for t in weights)


_build_cases()


def roll_item(case_key: str) -> str:
    """Криптостойкий ГСЧ: сначала редкость по весам кейса, затем предмет внутри редкости."""
    c = CASES[case_key]
    tiers = list(c["weights"])
    r = _RNG.random()
    acc = 0.0
    for t in tiers:
        acc += c["weights"][t]
        if r < acc:
            return _RNG.choice(c["pool"][t])
    return _RNG.choice(c["pool"][tiers[-1]])


_TROPHY = {1: ("mythic", 30000), 2: ("legend", 15000), 3: ("epic", 8000)}


def item_info(key: str) -> dict:
    it = ITEMS.get(key)
    if it:
        return it
    if key.startswith("trophy:"):                      # кубок сезона: trophy:<сезон>:<место>
        try:
            _, season, place = key.split(":")
            r, p = _TROPHY[int(place)]
            return {"name": f"Кубок сезона {season} · {place} место", "price": p, "rarity": r, "cat": "trophy"}
        except (ValueError, KeyError):
            pass
    return {"name": "Неизвестный предмет", "price": 0, "rarity": "junk", "cat": "?"}


def _fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _e(s) -> str:
    return html.escape(str(s))


def _it(key: str) -> str:
    i = item_info(key)
    return f"{RARITY[i['rarity']][0]} <b>{_e(i['name'])}</b>"


# ── SQLite ────────────────────────────────────────────────────────────────────
_DB_PATH = os.getenv("CASES_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_game.db")
_conn: sqlite3.Connection | None = None
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, name TEXT, coins INTEGER NOT NULL, last_daily TEXT DEFAULT '', streak INTEGER DEFAULT 0,
  last_bailout REAL DEFAULT 0, opened INTEGER DEFAULT 0, spent INTEGER DEFAULT 0, earned INTEGER DEFAULT 0,
  best_item TEXT DEFAULT '', best_price INTEGER DEFAULT 0, bw INTEGER DEFAULT 0, bl INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS inventory(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, item TEXT NOT NULL, case_key TEXT, got_at REAL);
CREATE INDEX IF NOT EXISTS inv_user ON inventory(user_id);
CREATE TABLE IF NOT EXISTS battles(
  id TEXT PRIMARY KEY, chat_id INTEGER, message_id INTEGER, creator INTEGER, creator_name TEXT,
  case_key TEXT, price INTEGER, created REAL, status TEXT);
CREATE TABLE IF NOT EXISTS achievements(user_id INTEGER, key TEXT, got_at REAL, PRIMARY KEY(user_id, key));
CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS basket(user_id INTEGER, inv_id INTEGER, PRIMARY KEY(user_id, inv_id));
CREATE TABLE IF NOT EXISTS chat_members(chat_id INTEGER, user_id INTEGER, seen REAL, PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, to_id INTEGER,
  kind TEXT, value INTEGER, ts REAL);
CREATE INDEX IF NOT EXISTS tr_from ON transfers(from_id, ts);
CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, chat_id INTEGER, message_id INTEGER, a INTEGER, a_name TEXT,
  b INTEGER, b_name TEXT, a_inv INTEGER, b_inv INTEGER, created REAL, status TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS season_snap(season INTEGER, user_id INTEGER, base INTEGER, PRIMARY KEY(season, user_id));
CREATE TABLE IF NOT EXISTS season_results(season INTEGER, place INTEGER, user_id INTEGER, name TEXT, growth INTEGER,
  PRIMARY KEY(season, place));
"""

# колонки, добавленные после первой версии: старые базы мигрируют на лету (данные сохраняются)
_NEW_USER_COLS = {
    "username": "TEXT DEFAULT ''", "last_free": "TEXT DEFAULT ''", "last_deal": "TEXT DEFAULT ''",
    "title": "TEXT DEFAULT ''", "contracts": "INTEGER DEFAULT 0", "up_win": "INTEGER DEFAULT 0",
    "up_try": "INTEGER DEFAULT 0", "lucky_up": "INTEGER DEFAULT 0", "bstreak": "INTEGER DEFAULT 0",
    "bstreak_best": "INTEGER DEFAULT 0", "trades": "INTEGER DEFAULT 0", "gifts": "INTEGER DEFAULT 0",
    "bailouts": "INTEGER DEFAULT 0", "best_sale": "INTEGER DEFAULT 0", "seasons_top": "INTEGER DEFAULT 0",
    "seasons_win": "INTEGER DEFAULT 0",
}


def _migrate(conn: sqlite3.Connection):
    have = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col, decl in _NEW_USER_COLS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
    return _conn


@contextmanager
def _tx():
    c = _db()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _ensure(c, uid: int, name: str | None = None, now: float | None = None) -> dict:
    row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        c.execute("INSERT INTO users(user_id,name,coins,created) VALUES(?,?,?,?)",
                  (uid, name or str(uid), START_COINS, now or time.time()))
        no = _meta_get(c, "season_no")
        if no is not None:                                   # новичок стартует сезон с чистого листа
            c.execute("INSERT OR IGNORE INTO season_snap VALUES(?,?,?)", (int(no), uid, START_COINS))
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    elif name and row["name"] != name:
        c.execute("UPDATE users SET name=? WHERE user_id=?", (name, uid))
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return dict(row)


def _inv_stats(c, uid: int) -> tuple[int, int]:
    cnt = val = 0
    for r in c.execute("SELECT item, COUNT(*) n FROM inventory WHERE user_id=? GROUP BY item", (uid,)):
        cnt += r["n"]
        val += r["n"] * item_info(r["item"])["price"]
    return cnt, val


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, _TZ).date().isoformat()


def _secs_to_midnight(ts: float) -> int:
    d = datetime.fromtimestamp(ts, _TZ)
    nxt = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - d).total_seconds()))


def _human(secs: int) -> str:
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h} ч {m} мин" if h else f"{max(m, 1)} мин"


def get_profile(uid: int, name: str | None = None, now: float | None = None, username: str | None = None) -> dict:
    """Профиль + возврат ставок по просроченным батлам + ленивое закрытие сезона. Создаёт игрока при первом обращении."""
    now = now or time.time()
    with _tx() as c:
        _rollover(c, now)
        _ensure(c, uid, name, now)
        if username is not None:
            c.execute("UPDATE users SET username=? WHERE user_id=?", (username.lower().lstrip("@"), uid))
        refunded = 0
        for b in c.execute("SELECT * FROM battles WHERE creator=? AND status='open' AND created<?",
                           (uid, now - BATTLE_TTL)).fetchall():
            c.execute("UPDATE battles SET status='expired' WHERE id=?", (b["id"],))
            c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (b["price"], uid))
            refunded += b["price"]
        u = _ensure(c, uid)
        cnt, val = _inv_stats(c, uid)
    u.update(inv_count=cnt, inv_value=val, networth=u["coins"] + val, refunded=refunded)
    return u


def daily_status(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
    if u["last_daily"] == _day(now):
        return {"ready": False, "wait": _secs_to_midnight(now), "streak": u["streak"]}
    return {"ready": True, "wait": 0, "streak": u["streak"]}


def claim_daily(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    today = _day(now)
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        if u["last_daily"] == today:
            return {"ok": False, "wait": _secs_to_midnight(now), "streak": u["streak"]}
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        streak = u["streak"] + 1 if u["last_daily"] == yesterday else 1
        amount = DAILY_BASE + DAILY_STEP * min(streak - 1, DAILY_STREAK_MAX)
        c.execute("UPDATE users SET coins=coins+?, last_daily=?, streak=? WHERE user_id=?", (amount, today, streak, uid))
        _check_ach(c, uid, now)
    return {"ok": True, "amount": amount, "streak": streak}


def maybe_bailout(uid: int, now: float | None = None) -> dict:
    """Гарантия «на нуле»: монет и стоимости инвентаря меньше самого дешёвого кейса → пособие (раз в 2 часа)."""
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        _, val = _inv_stats(c, uid)
        if u["coins"] >= MIN_PRICE or val >= MIN_PRICE:
            return {"given": 0, "broke": False}
        since = now - u["last_bailout"]
        if since < BAILOUT_COOLDOWN:
            return {"given": 0, "broke": True, "wait": int(BAILOUT_COOLDOWN - since)}
        c.execute("UPDATE users SET coins=coins+?, last_bailout=?, bailouts=bailouts+1 WHERE user_id=?", (BAILOUT, now, uid))
        _check_ach(c, uid, now)
    return {"given": BAILOUT, "broke": True}


def _record_drop(c, uid: int, case_key: str, item: str, now: float) -> int:
    cur = c.execute("INSERT INTO inventory(user_id,item,case_key,got_at) VALUES(?,?,?,?)", (uid, item, case_key, now))
    return cur.lastrowid


def _update_best(c, uid: int, item: str):
    p = item_info(item)["price"]
    c.execute("UPDATE users SET best_item=?, best_price=? WHERE user_id=? AND best_price<?", (item, p, uid, p))


def open_case(uid: int, case_key: str, times: int = 1, now: float | None = None, deal: bool = False) -> dict:
    """Покупка и открытие. deal=True — «предложение дня»: скидка, 1 раз в день, только ×1."""
    now = now or time.time()
    case = CASES.get(case_key)
    if case is None or case.get("free"):
        return {"ok": False, "reason": "bad", "need": 0}
    today = _day(now)
    if deal:
        if case_key != deal_case(now) or times != 1:
            return {"ok": False, "reason": "bad", "need": 0}
        unit = deal_price(case_key)
    else:
        unit = case["price"]
    price = unit * times
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        if deal and u["last_deal"] == today:
            return {"ok": False, "reason": "deal_used", "need": 0}
        if u["coins"] < price:
            return {"ok": False, "reason": "money", "need": price - u["coins"], "coins": u["coins"]}
        c.execute("UPDATE users SET coins=coins-?, spent=spent+?, opened=opened+? WHERE user_id=?",
                  (price, price, times, uid))
        if deal:
            c.execute("UPDATE users SET last_deal=? WHERE user_id=?", (today, uid))
        drops = []
        for _ in range(times):
            item = roll_item(case_key)
            drops.append((item, _record_drop(c, uid, case_key, item, now)))
            _update_best(c, uid, item)
        _check_ach(c, uid, now)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "drops": drops, "coins": coins, "cost": price}


def sell_item(uid: int, inv_id: int) -> dict:
    with _tx() as c:
        row = c.execute("SELECT * FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None:
            return {"ok": False}
        price = item_info(row["item"])["price"]
        _drop_inv(c, inv_id)
        c.execute("UPDATE users SET coins=coins+?, earned=earned+?, best_sale=MAX(best_sale, ?) WHERE user_id=?",
                  (price, price, price, uid))
        _check_ach(c, uid)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "price": price, "item": row["item"], "coins": coins}


_BULK = {
    "junk": ("🗑 Хлам", {"junk"}),
    "cheap": ("⚪🔵 Всё до редкого", {"junk", "common", "rare"}),
    "all": ("💎 Вообще всё", set(RARITY_ORDER)),
}


def bulk_preview(uid: int, mode: str) -> tuple[int, int]:
    rs = _BULK[mode][1]
    rows = [r for r in _db().execute("SELECT item FROM inventory WHERE user_id=?", (uid,)) if item_info(r["item"])["rarity"] in rs]
    return len(rows), sum(item_info(r["item"])["price"] for r in rows)


def sell_bulk(uid: int, mode: str) -> dict:
    rs = _BULK[mode][1]
    with _tx() as c:
        rows = [r for r in c.execute("SELECT id, item FROM inventory WHERE user_id=?", (uid,)).fetchall()
                if item_info(r["item"])["rarity"] in rs]
        total = sum(item_info(r["item"])["price"] for r in rows)
        for r in rows:
            _drop_inv(c, r["id"])
        if total:
            c.execute("UPDATE users SET coins=coins+?, earned=earned+? WHERE user_id=?", (total, total, uid))
            _check_ach(c, uid)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "count": len(rows), "total": total, "coins": coins}


def inventory_page(uid: int, page: int, asc: bool = False, no_trophy: bool = False) -> dict:
    rows = _db().execute("SELECT id, item FROM inventory WHERE user_id=?", (uid,)).fetchall()
    if no_trophy:
        rows = [r for r in rows if not _is_trophy(r["item"])]
    sign = 1 if asc else -1
    rows = sorted(rows, key=lambda r: (sign * item_info(r["item"])["price"], item_info(r["item"])["name"], r["id"]))
    pages = max(1, -(-len(rows) // INV_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * INV_PER_PAGE:(page + 1) * INV_PER_PAGE]
    return {"items": [(r["id"], r["item"]) for r in chunk], "page": page, "pages": pages, "total": len(rows)}


def leaderboard(limit: int | None = 10, chat_id: int | None = None) -> list[dict]:
    """Топ по капиталу (монеты + инвентарь). chat_id — только игроки, замеченные в этом групповом чате."""
    c = _db()
    worth = _all_networth(c)
    members = None
    if chat_id is not None:
        members = {r["user_id"] for r in c.execute("SELECT user_id FROM chat_members WHERE chat_id=?", (chat_id,))}
    out = [{"user_id": u["user_id"], "name": u["name"], "title": u["title"], "coins": u["coins"],
            "networth": worth.get(u["user_id"], u["coins"]), "bw": u["bw"], "bl": u["bl"]}
           for u in c.execute("SELECT * FROM users") if members is None or u["user_id"] in members]
    out.sort(key=lambda d: -d["networth"])
    for i, d in enumerate(out, 1):
        d["rank"] = i
    return out if limit is None else out[:limit]




# ── Батлы ─────────────────────────────────────────────────────────────────────
def create_battle(uid: int, name: str, case_key: str, chat_id: int, msg_id: int, now: float | None = None) -> dict:
    now = now or time.time()
    price = CASES[case_key]["price"]
    with _tx() as c:
        u = _ensure(c, uid, name, now)
        if u["coins"] < price:
            return {"ok": False, "need": price - u["coins"]}
        c.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (price, uid))     # ставка в «эскроу»
        bid = secrets.token_hex(4)
        c.execute("INSERT INTO battles VALUES(?,?,?,?,?,?,?,?,'open')",
                  (bid, chat_id, msg_id, uid, name, case_key, price, now))
    return {"ok": True, "id": bid, "price": price}


def cancel_battle(bid: str, uid: int) -> dict:
    with _tx() as c:
        b = c.execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
        if b is None or b["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if b["creator"] != uid:
            return {"ok": False, "reason": "not_owner"}
        c.execute("UPDATE battles SET status='cancelled' WHERE id=?", (bid,))
        c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (b["price"], uid))
    return {"ok": True, "price": b["price"]}


def accept_battle(bid: str, opp: int, opp_name: str, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        b = c.execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
        if b is None or b["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if b["creator"] == opp:
            return {"ok": False, "reason": "self"}
        if now - b["created"] > BATTLE_TTL:
            c.execute("UPDATE battles SET status='expired' WHERE id=?", (bid,))
            c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (b["price"], b["creator"]))
            return {"ok": False, "reason": "expired"}
        o = _ensure(c, opp, opp_name, now)
        price, ck, a = b["price"], b["case_key"], b["creator"]
        if o["coins"] < price:
            return {"ok": False, "reason": "money", "need": price - o["coins"]}
        c.execute("UPDATE users SET coins=coins-?, spent=spent+?, opened=opened+1 WHERE user_id=?", (price, price, opp))
        c.execute("UPDATE users SET spent=spent+?, opened=opened+1 WHERE user_id=?", (price, a))
        ia, ib = roll_item(ck), roll_item(ck)
        pa, pb = item_info(ia)["price"], item_info(ib)["price"]
        _update_best(c, a, ia)
        _update_best(c, opp, ib)
        if pa == pb:
            winner = None
            _record_drop(c, a, ck, ia, now)
            _record_drop(c, opp, ck, ib, now)
        else:
            winner = a if pa > pb else opp
            loser = opp if winner == a else a
            _record_drop(c, winner, ck, ia, now)
            _record_drop(c, winner, ck, ib, now)
            c.execute("UPDATE users SET bw=bw+1, bstreak=bstreak+1, bstreak_best=MAX(bstreak_best, bstreak+1) "
                      "WHERE user_id=?", (winner,))
            c.execute("UPDATE users SET bl=bl+1, bstreak=0 WHERE user_id=?", (loser,))
        c.execute("UPDATE battles SET status='done' WHERE id=?", (bid,))
        _check_ach(c, a, now)
        _check_ach(c, opp, now)
    return {"ok": True, "case": ck, "price": price, "a": a, "a_name": b["creator_name"], "a_item": ia,
            "b": opp, "b_name": opp_name, "b_item": ib, "winner": winner}


def get_battle(bid: str) -> dict | None:
    r = _db().execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
    return dict(r) if r else None


# ══════════════════════════════════════════════════════════════════════════════
#  v2: уведомления, достижения, сезоны, апгрейды, контракты, подарки, обмен
# ══════════════════════════════════════════════════════════════════════════════
def _notice(c, uid: int, text: str, now: float):
    c.execute("INSERT INTO notices(user_id,text,ts) VALUES(?,?,?)", (uid, text, now))


def pop_notices(uid: int) -> str:
    with _tx() as c:
        rows = c.execute("SELECT id, text FROM notices WHERE user_id=? ORDER BY id", (uid,)).fetchall()
        c.execute("DELETE FROM notices WHERE user_id=?", (uid,))
    return "\n".join(r["text"] for r in rows[-6:])


def _drop_inv(c, inv_id: int):
    """Убрать предмет из инвентаря (и из корзины контракта, если он там лежал)."""
    c.execute("DELETE FROM inventory WHERE id=?", (inv_id,))
    c.execute("DELETE FROM basket WHERE inv_id=?", (inv_id,))


def _is_trophy(item: str) -> bool:
    return item.startswith("trophy:")


# ── достижения и титулы ───────────────────────────────────────────────────────
ACH = {
    "first_open": ("🎰", "Новичок", "открыть первый кейс", lambda u, x: u["opened"] >= 1),
    "open_50":    ("📦", "Кейсовод", "открыть 50 кейсов", lambda u, x: u["opened"] >= 50),
    "open_500":   ("🔥", "Одержимый", "открыть 500 кейсов", lambda u, x: u["opened"] >= 500),
    "epic":       ("🟣", "Фиолетовый", "выбить эпик 🟣 или лучше", lambda u, x: u["best_price"] >= 1800),
    "legend":     ("🔴", "Красная нить", "выбить легендарный дроп 🔴", lambda u, x: u["best_price"] >= 9000),
    "mythic":     ("🟡", "Золотая жила", "выбить мифический дроп 🟡", lambda u, x: u["best_price"] >= 90000),
    "rich_10k":   ("💵", "Состоятельный", "капитал 10 000 🪙", lambda u, x: x["networth"] >= 10_000),
    "rich_100k":  ("💰", "Магнат", "капитал 100 000 🪙", lambda u, x: x["networth"] >= 100_000),
    "rich_1m":    ("🏦", "Миллионер", "капитал 1 000 000 🪙", lambda u, x: x["networth"] >= 1_000_000),
    "bw_1":       ("⚔️", "Боец", "выиграть первый батл", lambda u, x: u["bw"] >= 1),
    "bstreak_5":  ("🛡", "Непобедимый", "5 побед в батлах подряд", lambda u, x: u["bstreak_best"] >= 5),
    "bw_25":      ("🗡", "Гладиатор", "25 побед в батлах", lambda u, x: u["bw"] >= 25),
    "up_win":     ("🚀", "Рисковый", "успешный апгрейд", lambda u, x: u["up_win"] >= 1),
    "up_lucky":   ("🌠", "Ва-банк", "удачный апгрейд с шансом ≤10%", lambda u, x: u["lucky_up"] >= 1),
    "contract":   ("🧾", "Делец", "подписать первый контракт", lambda u, x: u["contracts"] >= 1),
    "trade":      ("🤝", "Меняла", "завершить обмен с игроком", lambda u, x: u["trades"] >= 1),
    "gift":       ("🎁", "Щедрая душа", "сделать первый подарок", lambda u, x: u["gifts"] >= 1),
    "daily_7":    ("📅", "Постоянный клиент", "серия бонуса 7 дней", lambda u, x: u["streak"] >= 7),
    "broke":      ("🆘", "На дне", "получить пособие", lambda u, x: u["bailouts"] >= 1),
    "sale_10k":   ("🏷", "Продажа века", "продать предмет за 10 000+ 🪙", lambda u, x: u["best_sale"] >= 10_000),
    "season_top": ("🏅", "Призёр сезона", "войти в топ-3 сезона", lambda u, x: u["seasons_top"] >= 1),
    "season_win": ("🏆", "Чемпион сезона", "выиграть сезон", lambda u, x: u["seasons_win"] >= 1),
}


def _check_ach(c, uid: int, now: float | None = None) -> list[str]:
    """Выдаёт новые достижения (внутри текущей транзакции) и кладёт уведомления."""
    now = now or time.time()
    row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        return []
    u = dict(row)
    _, val = _inv_stats(c, uid)
    x = {"networth": u["coins"] + val}
    have = {r["key"] for r in c.execute("SELECT key FROM achievements WHERE user_id=?", (uid,))}
    new = []
    for key, (icon, name, desc, check) in ACH.items():
        if key not in have and check(u, x):
            c.execute("INSERT OR IGNORE INTO achievements VALUES(?,?,?)", (uid, key, now))
            _notice(c, uid, f"🏅 Достижение: {icon} <b>{name}</b> — {desc}", now)
            new.append(key)
    return new


def get_achievements(uid: int) -> dict[str, float]:
    return {r["key"]: r["got_at"] for r in _db().execute("SELECT key, got_at FROM achievements WHERE user_id=?", (uid,))}


def set_title(uid: int, key: str) -> bool:
    with _tx() as c:
        if key == "-":
            c.execute("UPDATE users SET title='' WHERE user_id=?", (uid,))
            return True
        if key not in ACH or not c.execute("SELECT 1 FROM achievements WHERE user_id=? AND key=?", (uid, key)).fetchone():
            return False
        c.execute("UPDATE users SET title=? WHERE user_id=?", (key, uid))
    return True


def user_exists(uid: int) -> bool:
    return _db().execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone() is not None


def user_name(uid: int) -> str | None:
    r = _db().execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
    return r["name"] if r else None


def find_user_by_username(username: str) -> int | None:
    r = _db().execute("SELECT user_id FROM users WHERE LOWER(username)=?", (username.lower().lstrip("@"),)).fetchone()
    return r["user_id"] if r else None


def touch_chat(uid: int, chat_id: int | None, now: float | None = None):
    """Запоминаем, что игрок активен в этом групповом чате (для чат-топа)."""
    if chat_id is None or chat_id >= 0:
        return
    _db().execute("INSERT INTO chat_members VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET seen=excluded.seen",
                  (chat_id, uid, now or time.time()))


# ── сезоны ────────────────────────────────────────────────────────────────────
def _meta_get(c, k: str, default=None):
    r = c.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r["value"] if r else default


def _meta_set(c, k: str, v):
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


def _all_networth(c) -> dict[int, int]:
    worth = {r["user_id"]: r["coins"] for r in c.execute("SELECT user_id, coins FROM users")}
    for r in c.execute("SELECT user_id, item, COUNT(*) n FROM inventory GROUP BY user_id, item"):
        if r["user_id"] in worth:
            worth[r["user_id"]] += r["n"] * item_info(r["item"])["price"]
    return worth


def _snapshot_all(c, season: int):
    c.executemany("INSERT OR REPLACE INTO season_snap VALUES(?,?,?)", [(season, u, w) for u, w in _all_networth(c).items()])


def _season_state(c, now: float) -> tuple[int, float]:
    no = _meta_get(c, "season_no")
    if no is None:
        _meta_set(c, "season_no", 1)
        _meta_set(c, "season_start", now)
        _snapshot_all(c, 1)
        return 1, now
    return int(no), float(_meta_get(c, "season_start"))


def _rollover(c, now: float):
    """Лениво закрывает сезон: топ-3 по ПРИРОСТУ капитала получают кубки; прогресс остальных не сбрасывается."""
    no, start = _season_state(c, now)
    end = start + SEASON_DAYS * 86400
    if now < end:
        return
    worth = _all_networth(c)
    base = {r["user_id"]: r["base"] for r in c.execute("SELECT user_id, base FROM season_snap WHERE season=?", (no,))}
    names = {r["user_id"]: r["name"] for r in c.execute("SELECT user_id, name FROM users")}
    growth = sorted(((worth[u] - base.get(u, worth[u]), u) for u in worth), reverse=True)
    for place, (g, u) in enumerate([x for x in growth if x[0] > 0][:3], 1):
        key = f"trophy:{no}:{place}"
        _record_drop(c, u, "season", key, now)
        c.execute("INSERT OR REPLACE INTO season_results VALUES(?,?,?,?,?)", (no, place, u, names.get(u, str(u)), g))
        c.execute("UPDATE users SET seasons_top=seasons_top+1" + (", seasons_win=seasons_win+1" if place == 1 else "")
                  + " WHERE user_id=?", (u,))
        _notice(c, u, f"🏆 <b>Сезон {no} завершён!</b> Ты занял <b>{place}</b> место "
                      f"(+{g:,} 🪙 за сезон).\nНаграда в инвентаре: {item_info(key)['name']} "
                      f"({item_info(key)['price']:,} 🪙)".replace(",", " "), now)
        _check_ach(c, u, now)
    no += 1
    new_start = end if now < end + SEASON_DAYS * 86400 else now
    _meta_set(c, "season_no", no)
    _meta_set(c, "season_start", new_start)
    _snapshot_all(c, no)


def season_info(now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _rollover(c, now)
        no, start = _season_state(c, now)
        last = [dict(r) for r in c.execute("SELECT * FROM season_results WHERE season=? ORDER BY place", (no - 1,))]
    end = start + SEASON_DAYS * 86400
    return {"no": no, "start": start, "end": end, "left": max(0, int(end - now)), "last": last}


def season_growth(uid: int, now: float | None = None) -> int:
    info = season_info(now)
    c = _db()
    base = c.execute("SELECT base FROM season_snap WHERE season=? AND user_id=?", (info["no"], uid)).fetchone()
    worth = _all_networth(c).get(uid, 0)
    return worth - (base["base"] if base else worth)


def season_leaderboard(limit: int | None = 10, chat_id: int | None = None, now: float | None = None) -> list[dict]:
    info = season_info(now)
    c = _db()
    worth = _all_networth(c)
    base = {r["user_id"]: r["base"] for r in c.execute("SELECT user_id, base FROM season_snap WHERE season=?", (info["no"],))}
    members = None
    if chat_id is not None:
        members = {r["user_id"] for r in c.execute("SELECT user_id FROM chat_members WHERE chat_id=?", (chat_id,))}
    out = []
    for u in c.execute("SELECT user_id, name, title FROM users"):
        if members is not None and u["user_id"] not in members:
            continue
        w = worth.get(u["user_id"], 0)
        out.append({"user_id": u["user_id"], "name": u["name"], "title": u["title"],
                    "growth": w - base.get(u["user_id"], w), "networth": w})
    out.sort(key=lambda d: -d["growth"])
    for i, d in enumerate(out, 1):
        d["rank"] = i
    return out if limit is None else out[:limit]


# ── бесплатный кейс дня и предложение дня ─────────────────────────────────────
def free_status(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
    ready = u["last_free"] != _day(now)
    return {"ready": ready, "wait": 0 if ready else _secs_to_midnight(now)}


def open_free(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    today = _day(now)
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        if u["last_free"] == today:
            return {"ok": False, "wait": _secs_to_midnight(now)}
        item = roll_item("free")
        inv_id = _record_drop(c, uid, "free", item, now)
        c.execute("UPDATE users SET last_free=?, opened=opened+1 WHERE user_id=?", (today, uid))
        _update_best(c, uid, item)
        _check_ach(c, uid, now)
    return {"ok": True, "item": item, "inv_id": inv_id}


def deal_case(now: float | None = None) -> str:
    """Кейс дня со скидкой: вращается по дням детерминированно."""
    keys = list(SHOP)
    return keys[date.fromisoformat(_day(now or time.time())).toordinal() % len(keys)]


def deal_price(case_key: str) -> int:
    return max(1, round(CASES[case_key]["price"] * (1 - DEAL_DISCOUNT)))


def deal_available(uid: int, now: float | None = None) -> bool:
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
    return u["last_deal"] != _day(now)


# ── апгрейд ───────────────────────────────────────────────────────────────────
def upgrade_options(src_item: str) -> list[dict]:
    """Варианты апгрейда: для каждого множителя — случайная цель из каталога около цены×множитель.
    Шанс = RTP × цена_предмета / цена_цели, так что среднее остаётся 90% — апгрейд не «печатает деньги»."""
    p0 = item_info(src_item)["price"]
    if p0 <= 0 or _is_trophy(src_item):
        return []
    out, seen = [], set()
    for m in UPGRADE_MULTS:
        t = p0 * m
        cands = [k for k, v in ITEMS.items() if 0.85 * t <= v["price"] <= 1.25 * t and v["price"] > p0]
        if not cands:
            continue
        key = _RNG.choice(cands)
        chance = RTP * p0 / ITEMS[key]["price"]
        if key in seen or not (UPGRADE_MIN_CHANCE <= chance <= UPGRADE_MAX_CHANCE):
            continue
        seen.add(key)
        out.append({"mult": ITEMS[key]["price"] / p0, "target": key, "chance": chance})
    return out


def upgrade_item(uid: int, inv_id: int, target: str, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        row = c.execute("SELECT * FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None:
            return {"ok": False, "reason": "gone"}
        src = row["item"]
        if _is_trophy(src) or target not in ITEMS:
            return {"ok": False, "reason": "bad"}
        p0, pt = item_info(src)["price"], ITEMS[target]["price"]
        chance = RTP * p0 / pt
        if pt <= p0 or not (UPGRADE_MIN_CHANCE <= chance <= UPGRADE_MAX_CHANCE):
            return {"ok": False, "reason": "bad"}
        r = _RNG.random()
        success = r < chance
        _drop_inv(c, inv_id)
        new_id = _record_drop(c, uid, "upgrade", target, now) if success else None
        c.execute("UPDATE users SET up_try=up_try+1" + (", up_win=up_win+1" if success else "")
                  + (", lucky_up=lucky_up+1" if success and chance <= 0.10 else "") + " WHERE user_id=?", (uid,))
        _check_ach(c, uid, now)
    return {"ok": True, "success": success, "chance": chance, "r": r, "src": src, "target": target, "inv_id": new_id}


# ── контракты ─────────────────────────────────────────────────────────────────
def basket_items(uid: int) -> list[tuple[int, str]]:
    rows = _db().execute("SELECT i.id, i.item FROM basket b JOIN inventory i ON i.id=b.inv_id AND i.user_id=b.user_id "
                         "WHERE b.user_id=? ORDER BY i.id", (uid,)).fetchall()
    return [(r["id"], r["item"]) for r in rows]


def basket_toggle(uid: int, inv_id: int) -> dict:
    with _tx() as c:
        row = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None or _is_trophy(row["item"]):
            return {"ok": False, "reason": "bad"}
        if c.execute("SELECT 1 FROM basket WHERE user_id=? AND inv_id=?", (uid, inv_id)).fetchone():
            c.execute("DELETE FROM basket WHERE user_id=? AND inv_id=?", (uid, inv_id))
            return {"ok": True, "added": False}
        n = c.execute("SELECT COUNT(*) n FROM basket WHERE user_id=?", (uid,)).fetchone()["n"]
        if n >= CONTRACT_MAX:
            return {"ok": False, "reason": "full"}
        c.execute("INSERT INTO basket VALUES(?,?)", (uid, inv_id))
    return {"ok": True, "added": True}


def basket_clear(uid: int):
    _db().execute("DELETE FROM basket WHERE user_id=?", (uid,))


def contract_target_pool(total: int, mult: float) -> list[str]:
    t = total * mult
    cands = [k for k, v in ITEMS.items() if 0.75 * t <= v["price"] <= 1.33 * t]
    if cands:
        return cands
    best = min(ITEMS, key=lambda k: abs(ITEMS[k]["price"] - t))
    return [best]


def contract_expected_ratio(total: int) -> float:
    """Точное матожидание выплаты/стоимости для суммы total (используется в тестах и в UI)."""
    e = 0.0
    for m, w in CONTRACT_OUTCOMES:
        pool = contract_target_pool(total, m)
        e += w * sum(ITEMS[k]["price"] for k in pool) / len(pool)
    return e / total


def sign_contract(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        rows = c.execute("SELECT i.id, i.item FROM basket b JOIN inventory i ON i.id=b.inv_id AND i.user_id=b.user_id "
                         "WHERE b.user_id=?", (uid,)).fetchall()
        if not (CONTRACT_MIN <= len(rows) <= CONTRACT_MAX) or any(_is_trophy(r["item"]) for r in rows):
            return {"ok": False, "reason": "count", "n": len(rows)}
        total = sum(item_info(r["item"])["price"] for r in rows)
        r = _RNG.random()
        acc, mult = 0.0, CONTRACT_OUTCOMES[-1][0]
        for m, w in CONTRACT_OUTCOMES:
            acc += w
            if r < acc:
                mult = m
                break
        item = _RNG.choice(contract_target_pool(total, mult))
        for row in rows:
            _drop_inv(c, row["id"])
        inv_id = _record_drop(c, uid, "contract", item, now)
        c.execute("UPDATE users SET contracts=contracts+1 WHERE user_id=?", (uid,))
        _check_ach(c, uid, now)
    return {"ok": True, "n": len(rows), "total": total, "mult": mult, "item": item, "inv_id": inv_id,
            "src": [r["item"] for r in rows]}


# ── подарки ───────────────────────────────────────────────────────────────────
def _sent_last_day(c, uid: int, now: float) -> int:
    return c.execute("SELECT COALESCE(SUM(value),0) s FROM transfers WHERE from_id=? AND ts>?", (uid, now - 86400)).fetchone()["s"]


def give_left(uid: int, now: float | None = None) -> int:
    now = now or time.time()
    return max(0, GIVE_DAILY_CAP - _sent_last_day(_db(), uid, now))


def give_coins(uid: int, to: int, amount: int, now: float | None = None) -> dict:
    now = now or time.time()
    if uid == to:
        return {"ok": False, "reason": "self"}
    if amount < GIVE_MIN:
        return {"ok": False, "reason": "min"}
    with _tx() as c:
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (to,)).fetchone() is None:
            return {"ok": False, "reason": "no_target"}
        u = _ensure(c, uid, None, now)
        if u["coins"] < amount:
            return {"ok": False, "reason": "money", "need": amount - u["coins"]}
        left = GIVE_DAILY_CAP - _sent_last_day(c, uid, now)
        if amount > left:
            return {"ok": False, "reason": "cap", "left": max(0, left)}
        fee = int(amount * GIVE_FEE)
        c.execute("UPDATE users SET coins=coins-?, gifts=gifts+1 WHERE user_id=?", (amount, uid))
        c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (amount - fee, to))
        c.execute("INSERT INTO transfers(from_id,to_id,kind,value,ts) VALUES(?,?,?,?,?)", (uid, to, "coins", amount, now))
        _check_ach(c, uid, now)
        _check_ach(c, to, now)
    return {"ok": True, "amount": amount, "fee": fee, "got": amount - fee}


def give_item(uid: int, to: int, inv_id: int, now: float | None = None) -> dict:
    now = now or time.time()
    if uid == to:
        return {"ok": False, "reason": "self"}
    with _tx() as c:
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (to,)).fetchone() is None:
            return {"ok": False, "reason": "no_target"}
        row = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None:
            return {"ok": False, "reason": "gone"}
        if _is_trophy(row["item"]):
            return {"ok": False, "reason": "trophy"}
        price = item_info(row["item"])["price"]
        left = GIVE_DAILY_CAP - _sent_last_day(c, uid, now)
        if price > left:
            return {"ok": False, "reason": "cap", "left": max(0, left)}
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (to, inv_id))
        c.execute("DELETE FROM basket WHERE inv_id=?", (inv_id,))
        c.execute("UPDATE users SET gifts=gifts+1 WHERE user_id=?", (uid,))
        c.execute("INSERT INTO transfers(from_id,to_id,kind,value,ts) VALUES(?,?,?,?,?)", (uid, to, "item", price, now))
        _check_ach(c, uid, now)
    return {"ok": True, "item": row["item"], "price": price}


# ── обмен предметами между игроками ───────────────────────────────────────────
def create_trade(a: int, a_name: str, b: int, b_name: str, a_inv: int, b_inv: int, chat_id: int, msg_id: int,
                 now: float | None = None) -> dict:
    now = now or time.time()
    if a == b:
        return {"ok": False, "reason": "self"}
    with _tx() as c:
        ra = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (a_inv, a)).fetchone()
        rb = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (b_inv, b)).fetchone()
        if ra is None or rb is None:
            return {"ok": False, "reason": "gone"}
        if _is_trophy(ra["item"]) or _is_trophy(rb["item"]):
            return {"ok": False, "reason": "trophy"}
        tid = secrets.token_hex(4)
        c.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,'open')",
                  (tid, chat_id, msg_id, a, a_name, b, b_name, a_inv, b_inv, now))
    return {"ok": True, "id": tid, "a_item": ra["item"], "b_item": rb["item"]}


def get_trade(tid: str) -> dict | None:
    r = _db().execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def close_trade(tid: str, uid: int, kind: str) -> dict:
    """kind: 'cancel' (только инициатор) или 'decline' (только адресат)."""
    with _tx() as c:
        t = c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if t is None or t["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if uid != (t["a"] if kind == "cancel" else t["b"]):
            return {"ok": False, "reason": "not_yours"}
        c.execute("UPDATE trades SET status=? WHERE id=?", ("cancelled" if kind == "cancel" else "declined", tid))
    return {"ok": True}


def accept_trade(tid: str, uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        t = c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if t is None or t["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if uid != t["b"]:
            return {"ok": False, "reason": "not_yours"}
        if now - t["created"] > TRADE_TTL:
            c.execute("UPDATE trades SET status='expired' WHERE id=?", (tid,))
            return {"ok": False, "reason": "expired"}
        ra = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (t["a_inv"], t["a"])).fetchone()
        rb = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (t["b_inv"], t["b"])).fetchone()
        if ra is None or rb is None:
            c.execute("UPDATE trades SET status='void' WHERE id=?", (tid,))
            return {"ok": False, "reason": "gone"}
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (t["b"], t["a_inv"]))
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (t["a"], t["b_inv"]))
        c.execute("DELETE FROM basket WHERE inv_id IN (?,?)", (t["a_inv"], t["b_inv"]))
        c.execute("UPDATE users SET trades=trades+1 WHERE user_id IN (?,?)", (t["a"], t["b"]))
        c.execute("UPDATE trades SET status='done' WHERE id=?", (tid,))
        _check_ach(c, t["a"], now)
        _check_ach(c, t["b"], now)
    return {"ok": True, "a": t["a"], "a_name": t["a_name"], "b": t["b"], "b_name": t["b_name"],
            "a_item": ra["item"], "b_item": rb["item"]}


def get_inv_item(uid: int, inv_id: int) -> str | None:
    r = _db().execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
    return r["item"] if r else None


# ══════════════════════════════════════════════════════════════════════════════
#  UI
# ══════════════════════════════════════════════════════════════════════════════
_BUSY: set[int] = set()          # игроки, у которых сейчас идёт анимация открытия


def _name_of(u) -> str:
    n = (getattr(u, "full_name", None) or "").strip() or (f"@{u.username}" if getattr(u, "username", None) else "")
    return (n or str(u.id))[:32]


async def _edit(msg, text: str, kb: Kb | None = None) -> bool:
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return True
    except Exception as e:
        err = str(e).lower()
        if "retry after" in err:
            import re
            m = re.search(r"retry after (\d+)", err)
            await asyncio.sleep(min(int(m.group(1)) if m else 2, 5))
        elif "not modified" not in err:
            logger.warning("edit не удался: %s", e)
        return False


def _kb(rows) -> Kb:
    return Kb(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row] for row in rows])


def _chances(c: dict) -> str:
    parts = []
    for t in RARITY_ORDER:
        w = c["weights"].get(t)
        if w:
            pct = w * 100
            parts.append(f"{RARITY[t][0]} {pct:.0f}%" if pct >= 1 else f"{RARITY[t][0]} {pct:.2f}%")
    return " · ".join(parts)


# ── экраны ────────────────────────────────────────────────────────────────────
_BANNER = {
    "junk": "🗑 Ну… хоть что-то",
    "common": "⚪ Обычный дроп",
    "rare": "🔵 Неплохо!",
    "epic": "🟣 <b>ЭПИЧЕСКИЙ ДРОП!</b>",
    "legend": "🔴🔥 <b>ЛЕГЕНДАРНЫЙ ДРОП!!</b> 🔥🔴",
    "mythic": "🟡✨ <b>МИФИЧЕСКИЙ ДРОП!!!</b> ✨🟡",
}


def drop_screen(uid: int, ckey: str, item: str, inv_id: int, coins: int, cost: int) -> tuple[str, Kb]:
    c, i = CASES[ckey], item_info(item)
    mult = i["price"] / cost
    delta = i["price"] - cost
    text = (
        f"{_BANNER[i['rarity']]}\n\n{_it(item)}\n"
        f"💰 {_fmt(i['price'])} 🪙 · ×{mult:.2f} от цены кейса "
        f"({'+' if delta >= 0 else '−'}{_fmt(abs(delta))})\n\n"
        f"🪙 Баланс: <b>{_fmt(coins)}</b>"
    )
    kb = _kb([
        [(f"💰 Продать за {_fmt(i['price'])}", f"cs:sell:{uid}:{inv_id}:0:-1")],
        [(f"🔁 Ещё раз · {_fmt(c['price'])}🪙", f"cs:open:{uid}:{ckey}"), ("📦 Кейсы", f"cs:cases:{uid}")],
        [("🎒 Инвентарь", f"cs:inv:{uid}:0"), ("🏠 Меню", f"cs:menu:{uid}")],
    ])
    return text, kb


def open5_screen(uid: int, ckey: str, drops: list, coins: int, cost: int) -> tuple[str, Kb]:
    c = CASES[ckey]
    drops = sorted(drops, key=lambda d: -item_info(d[0])["price"])
    total = sum(item_info(d[0])["price"] for d in drops)
    delta = total - cost
    text = (
        f"📦 <b>×{len(drops)} · {c['icon']} {c['name']}</b>\n\n"
        + "\n".join(f"{_it(i)} — {_fmt(item_info(i)['price'])}" for i, _ in drops)
        + f"\n\n💰 Выпало на <b>{_fmt(total)}</b> 🪙 при ставке {_fmt(cost)} "
        f"({'+' if delta >= 0 else '−'}{_fmt(abs(delta))})\n🪙 Баланс: <b>{_fmt(coins)}</b>"
    )
    kb = _kb([
        [(f"🔁 ×5 ещё · {_fmt(c['price'] * 5)}🪙", f"cs:open5:{uid}:{ckey}"), ("🎰 ×1", f"cs:open:{uid}:{ckey}")],
        [("🎒 Инвентарь", f"cs:inv:{uid}:0"), ("📦 Кейсы", f"cs:cases:{uid}")],
        [("🏠 Меню", f"cs:menu:{uid}")],
    ])
    return text, kb


def inv_screen(uid: int, page: int = 0, notice: str = "") -> tuple[str, Kb]:
    pg = inventory_page(uid, page)
    p = get_profile(uid)
    if not pg["total"]:
        return ((notice + "\n\n" if notice else "") + "🎒 <b>Инвентарь пуст</b>\nОткрой кейс — и сюда что-нибудь выпадет!",
                _kb([[("📦 Кейсы", f"cs:cases:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")]]))
    lines = [f"🎒 <b>ИНВЕНТАРЬ</b> · {pg['total']} шт. на {_fmt(p['inv_value'])} 🪙 · стр. {pg['page'] + 1}/{pg['pages']}"]
    if notice:
        lines.insert(0, notice + "\n")
    sell_row, line_no = [], 0
    for n, (inv_id, item) in enumerate(pg["items"], 1):
        lines.append(f"{n}. {_it(item)} — {_fmt(item_info(item)['price'])} 🪙")
        sell_row.append((f"💰 {n}", f"cs:sell:{uid}:{inv_id}:0:{pg['page']}"))
    rows = [sell_row[i:i + 3] for i in range(0, len(sell_row), 3)]
    nav = []
    if pg["page"] > 0:
        nav.append(("◀️", f"cs:inv:{uid}:{pg['page'] - 1}"))
    nav.append((f"{pg['page'] + 1}/{pg['pages']}", f"cs:inv:{uid}:{pg['page']}"))
    if pg["page"] < pg["pages"] - 1:
        nav.append(("▶️", f"cs:inv:{uid}:{pg['page'] + 1}"))
    rows.append(nav)
    rows.append([("🗑 Хлам", f"cs:bulk:{uid}:junk:0"), ("⚪🔵 До редкого", f"cs:bulk:{uid}:cheap:0"),
                 ("💎 Всё", f"cs:bulk:{uid}:all:0")])
    rows.append([("📦 Кейсы", f"cs:cases:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")])
    lines.append("\n<i>Кнопка «💰 N» продаёт предмет под этим номером</i>")
    return "\n".join(lines), _kb(rows)


# ── анимации ──────────────────────────────────────────────────────────────────
def _strip(ckey: str, center_item: str | None = None, width: int = 9) -> str:
    cells = [RARITY[item_info(roll_item(ckey))["rarity"]][0] for _ in range(width)]
    if center_item:
        cells[width // 2] = RARITY[item_info(center_item)["rarity"]][0]
        cells[width // 2] = f"【{cells[width // 2]}】"
    return "".join(cells)


async def _anim_battle(msg, res: dict):
    c = CASES[res["case"]]
    an, bn = _e(res["a_name"]), _e(res["b_name"])
    head = f"⚔️ <b>БАТЛ</b> · {c['icon']} {c['name']} · {_fmt(c['price'])} 🪙\n\n"
    pa, pb = item_info(res["a_item"])["price"], item_info(res["b_item"])["price"]
    for k in range(6):
        await _edit(msg, head + f"{an}\n{_strip(res['case'])}  ❓\n\n{bn}\n{_strip(res['case'])}  ❓\n\n"
                              f"<i>{'◐◓◑◒'[k % 4]} кейсы крутятся…</i>")
        await asyncio.sleep(0.6 + 0.1 * k)
    await _edit(msg, head + f"{an}\n{_it(res['a_item'])} — <b>{_fmt(pa)}</b> 🪙\n\n{bn}\n"
                            f"{_strip(res['case'])}  ❓\n\n<i>◓ у соперника ещё крутится…</i>")
    await asyncio.sleep(1.3)


def battle_result_text(res: dict) -> str:
    c = CASES[res["case"]]
    an, bn = _e(res["a_name"]), _e(res["b_name"])
    pa, pb = item_info(res["a_item"])["price"], item_info(res["b_item"])["price"]
    text = (f"⚔️ <b>БАТЛ</b> · {c['icon']} {c['name']} · {_fmt(c['price'])} 🪙\n\n"
            f"{an}\n{_it(res['a_item'])} — <b>{_fmt(pa)}</b> 🪙\n\n"
            f"{bn}\n{_it(res['b_item'])} — <b>{_fmt(pb)}</b> 🪙\n\n")
    if res["winner"] is None:
        return text + "🤝 <b>Ничья!</b> Каждый оставляет свой предмет."
    wn = an if res["winner"] == res["a"] else bn
    return text + f"🏆 <b>Победил {wn}!</b> Забирает оба предмета на <b>{_fmt(pa + pb)}</b> 🪙"


# ── обработчики ───────────────────────────────────────────────────────────────
async def _do_open5(cb: types.CallbackQuery, uid: int, name: str, ckey: str):
    if uid in _BUSY:
        await cb.answer("Погоди, кейс ещё открывается 🎰")
        return
    get_profile(uid, name)
    res = open_case(uid, ckey, times=5)
    if not res["ok"]:
        await cb.answer(f"Не хватает {_fmt(res['need'])} 🪙", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        c = CASES[ckey]
        for k in range(3):
            await _edit(cb.message, f"{c['icon']} <b>×5 · {c['name']}</b>\n\n"
                                    + "\n".join(_strip(ckey) for _ in range(5)) + f"\n\n<i>{'◐◓◑◒'[k]} открываем…</i>")
            await asyncio.sleep(0.6)
        text, kb = open5_screen(uid, ckey, res["drops"], res["coins"], res["cost"])
        await _edit(cb.message, _with_notices(uid, text), kb)
    finally:
        _BUSY.discard(uid)


async def _do_sell(cb, uid: int, inv_id: int, ok: int, page: int):
    item = get_inv_item(uid, inv_id)
    if item is None:
        await cb.answer("Этот предмет уже продан", show_alert=True)
        if page >= 0:
            t, k = inv_screen(uid, page)
            await _edit(cb.message, t, k)
        return
    price = item_info(item)["price"]
    if price >= CONFIRM_SELL_AT and not ok:
        await cb.answer()
        back = f"cs:inv:{uid}:{page}" if page >= 0 else f"cs:menu:{uid}"
        await _edit(cb.message, f"⚠️ Продать {_it(item)} за <b>{_fmt(price)}</b> 🪙?\nПредмет дорогой — точно?",
                    _kb([[("✅ Да, продать", f"cs:sell:{uid}:{inv_id}:1:{page}"), ("❌ Нет", back)]]))
        return
    r = sell_item(uid, inv_id)
    if not r["ok"]:
        await cb.answer("Этот предмет уже продан", show_alert=True)
        return
    await cb.answer(f"+{_fmt(r['price'])} 🪙")
    note = f"💰 Продано: {_it(item)} за <b>{_fmt(r['price'])}</b> 🪙"
    if page >= 0:
        t, k = inv_screen(uid, page, note)
    else:
        t = note + f"\n\n🪙 Баланс: <b>{_fmt(r['coins'])}</b>"
        k = _kb([[("📦 Кейсы", f"cs:cases:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")], [("🏠 Меню", f"cs:menu:{uid}")]])
    await _edit(cb.message, t, k)


async def _do_bulk(cb, uid: int, mode: str, ok: int):
    if mode not in _BULK:
        await cb.answer("Неизвестный режим", show_alert=True)
        return
    cnt, total = bulk_preview(uid, mode)
    if not cnt:
        await cb.answer("Нечего продавать в этой категории", show_alert=True)
        return
    if not ok:
        await cb.answer()
        await _edit(cb.message, f"⚠️ Продать <b>{cnt}</b> предм. ({_BULK[mode][0]}) за <b>{_fmt(total)}</b> 🪙?",
                    _kb([[("✅ Продать", f"cs:bulk:{uid}:{mode}:1"), ("❌ Отмена", f"cs:inv:{uid}:0")]]))
        return
    r = sell_bulk(uid, mode)
    await cb.answer(f"+{_fmt(r['total'])} 🪙")
    t, k = inv_screen(uid, 0, f"💰 Продано <b>{r['count']}</b> предм. за <b>{_fmt(r['total'])}</b> 🪙 · баланс {_fmt(r['coins'])}")
    await _edit(cb.message, t, k)


async def _do_daily(cb, uid: int, name: str):
    get_profile(uid, name)
    r = claim_daily(uid)
    if not r["ok"]:
        await cb.answer(f"Бонус уже получен. Следующий через {_human(r['wait'])}", show_alert=True)
        return
    await cb.answer(f"+{_fmt(r['amount'])} 🪙")
    t, k = menu_screen(uid, name, f"🎁 Бонус дня: +<b>{_fmt(r['amount'])}</b> 🪙 · серия {r['streak']} дн. 🔥")
    await _edit(cb.message, t, k)


def _battle_invite(uid: int, name: str, ckey: str, bid: str) -> tuple[str, Kb]:
    c = CASES[ckey]
    text = (f"⚔️ <b>БАТЛ!</b>\n{_e(name)} ставит <b>{_fmt(c['price'])}</b> 🪙 на кейс {c['icon']} <b>{c['name']}</b>\n\n"
            "Каждому выпадет предмет из этого кейса — <b>у кого дороже, забирает оба</b>.\n"
            f"Кто примет вызов? ⏳ {BATTLE_TTL // 60} мин")
    return text, _kb([[(f"⚔️ Принять · {_fmt(c['price'])}🪙", f"cs:bacc:{bid}"), ("✖️ Отмена", f"cs:bcan:{bid}")]])


async def _do_battle_new(cb, uid: int, name: str, ckey: str, fresh_message: bool = False):
    if ckey not in CASES:
        await cb.answer("Нет такого кейса", show_alert=True)
        return
    get_profile(uid, name)
    target = cb.message
    if fresh_message:
        target = await cb.message.reply("⚔️ Готовлю батл…")
    r = create_battle(uid, name, ckey, target.chat.id, target.message_id)
    if not r["ok"]:
        await cb.answer(f"Не хватает {_fmt(r['need'])} 🪙", show_alert=True)
        if fresh_message:
            try:
                await target.delete()
            except Exception:
                pass
        return
    await cb.answer("Батл создан")
    t, k = _battle_invite(uid, name, ckey, r["id"])
    await _edit(target, t, k)


async def _do_battle_accept(cb, bid: str):
    uid, name = cb.from_user.id, _name_of(cb.from_user)
    res = accept_battle(bid, uid, _dn(uid, name))
    if not res["ok"]:
        reason = res["reason"]
        if reason == "closed":
            await cb.answer("Этот батл уже закрыт", show_alert=True)
        elif reason == "self":
            await cb.answer("Свой же вызов принять нельзя 😄 Жди соперника", show_alert=True)
        elif reason == "expired":
            await cb.answer("Вызов истёк, ставка вернулась создателю", show_alert=True)
            await _edit(cb.message, "⌛ Вызов истёк — ставка возвращена.", None)
        else:
            await cb.answer(f"Не хватает {_fmt(res['need'])} 🪙", show_alert=True)
        return
    await cb.answer("Батл начался!")
    try:
        await _anim_battle(cb.message, res)
    except Exception:
        logger.exception("анимация батла упала")
    kb = _kb([[("🔁 Свой батл на этот кейс", f"cs:brev:{res['case']}")]])
    await _edit(cb.message, battle_result_text(res), kb)


async def _do_battle_cancel(cb, bid: str):
    r = cancel_battle(bid, cb.from_user.id)
    if r["ok"]:
        await cb.answer("Батл отменён, ставка возвращена")
        await _edit(cb.message, "✖️ Батл отменён — ставка возвращена.", None)
    elif r["reason"] == "not_owner":
        await cb.answer("Отменить может только тот, кто создал батл", show_alert=True)
    else:
        await cb.answer("Этот батл уже закрыт", show_alert=True)


async def on_callback(cb: types.CallbackQuery):
    try:
        await _dispatch(cb)
    except Exception:
        logger.exception("cases_game: ошибка в callback %r", getattr(cb, "data", None))
        try:
            await cb.answer("Что-то пошло не так 😵 Попробуй /case", show_alert=True)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  v2 UI: экраны меню/кейсов/топа, апгрейд, контракт, подарки, обмен, достижения
# ══════════════════════════════════════════════════════════════════════════════
def _title_of(uid: int) -> str:
    r = _db().execute("SELECT title FROM users WHERE user_id=?", (uid,)).fetchone()
    return r["title"] if r else ""


def _tt(title_key: str) -> str:
    return f" «{ACH[title_key][1]}»" if title_key in ACH else ""


def _dn(uid: int, name: str) -> str:
    """Имя с выбранным титулом (для топа, батлов, обмена)."""
    return f"{name}{_tt(_title_of(uid))}"


def _mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{_e(name)}</a>'


def _human_long(secs: int) -> str:
    d, h = secs // 86400, (secs % 86400) // 3600
    return f"{d} д {h} ч" if d else _human(secs)


def _pct(p: float) -> str:
    return f"{p * 100:.1f}%" if p < 0.1 else f"{p * 100:.0f}%"


def _with_notices(uid: int, text: str) -> str:
    n = pop_notices(uid)
    return f"{text}\n\n{n}" if n else text


# ── экраны ────────────────────────────────────────────────────────────────────
def menu_screen(uid: int, name: str, notice: str = "") -> tuple[str, Kb]:
    p = get_profile(uid, name)
    notes = [notice] if notice else []
    if p["refunded"]:
        notes.append(f"↩️ Батл никто не принял — ставка <b>{_fmt(p['refunded'])}</b> 🪙 возвращена")
    b = maybe_bailout(uid)
    if b["given"]:
        notes.append(f"🆘 Ты был на нуле — держи пособие <b>+{BAILOUT}</b> 🪙")
        p = get_profile(uid, name)
    elif b.get("broke"):
        notes.append(f"🆘 Ты на нуле. Пособие через <b>{_human(b['wait'])}</b> — или забери бонус дня")
    pn = pop_notices(uid)
    if pn:
        notes.append(pn)
    lb = leaderboard(None)
    rank = next((d["rank"] for d in lb if d["user_id"] == uid), len(lb))
    ds, fs = daily_status(uid), free_status(uid)
    se = season_info()
    sg = season_growth(uid)
    text = (
        "🎰 <b>КЕЙС-КЛУБ</b>\n"
        f"👤 {_e(name)}{_tt(p['title'])}\n\n"
        f"🪙 Баланс: <b>{_fmt(p['coins'])}</b>\n"
        f"🎒 Инвентарь: {p['inv_count']} шт. на {_fmt(p['inv_value'])} 🪙\n"
        f"📈 Капитал: <b>{_fmt(p['networth'])}</b> 🪙 · место <b>#{rank}</b> из {len(lb)}\n"
        f"📅 Сезон {se['no']}: ещё {_human_long(se['left'])} · твой прирост <b>{'+' if sg >= 0 else '−'}{_fmt(abs(sg))}</b>\n\n"
        + ("🎁 <b>Бонус дня готов!</b>" if ds["ready"] else f"🎁 Бонус дня через {_human(ds['wait'])}")
        + (f" (серия {ds['streak']} дн.)" if ds["streak"] else "")
        + "\n" + ("🎟 <b>Бесплатный кейс готов!</b>" if fs["ready"] else f"🎟 Бесплатный кейс через {_human(fs['wait'])}")
        + ("\n\n" + "\n".join(notes) if notes else "")
    )
    kb = _kb([
        [("📦 Кейсы", f"cs:cases:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")],
        [("🎟 Бесплатный ✅" if fs["ready"] else "🎟 Бесплатный ⏳", f"cs:free:{uid}"),
         ("🎁 Бонус дня ✅" if ds["ready"] else "🎁 Бонус дня ⏳", f"cs:daily:{uid}")],
        [("🚀 Апгрейд", f"cs:up:{uid}:0"), ("🧾 Контракт", f"cs:ct:{uid}")],
        [("⚔️ Батл", f"cs:battle:{uid}"), ("🏆 Топ", f"cs:top:{uid}:all")],
        [("🏅 Достижения", f"cs:ach:{uid}"), ("📊 Профиль", f"cs:prof:{uid}")],
    ])
    return text, kb


def cases_screen(uid: int) -> tuple[str, Kb]:
    p = get_profile(uid)
    fs = free_status(uid)
    deal = deal_case()
    dc, dprice, dav = CASES[deal], deal_price(deal), deal_available(uid)
    lines = ["📦 <b>КЕЙСЫ</b>", f"🪙 Баланс: <b>{_fmt(p['coins'])}</b>\n",
             "🎟 <b>Бесплатный кейс дня:</b> " + ("готов!" if fs["ready"] else f"через {_human(fs['wait'])}"),
             f"💥 <b>Предложение дня:</b> {dc['icon']} {dc['name']} за <b>{_fmt(dprice)}</b> 🪙 "
             f"(−{round(DEAL_DISCOUNT * 100)}%)" + ("" if dav else " — уже использовано"), ""]
    rows = [[("🎟 Бесплатный кейс ✅" if fs["ready"] else "🎟 Бесплатный кейс ⏳", f"cs:free:{uid}")]]
    for k in SHOP:
        c = CASES[k]
        best = max(item_info(i)["price"] for t in c["weights"] for i in c["pool"][t])
        lines.append(f"{c['icon']} <b>{c['name']}</b> — {_fmt(c['price'])} 🪙 · до {_fmt(best)} 🪙\n   <i>{c['desc']}</i>")
        mark = "💥 " if (k == deal and dav) else ""
        rows.append([(f"{mark}{c['icon']} {c['name']} · {_fmt(c['price'])}🪙", f"cs:case:{uid}:{k}")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines), _kb(rows)


def case_screen(uid: int, ckey: str) -> tuple[str, Kb]:
    c = CASES[ckey]
    p = get_profile(uid)
    items = sorted({i for t in c["weights"] for i in c["pool"][t]}, key=lambda i: -item_info(i)["price"])[:5]
    text = (
        f"{c['icon']} <b>{c['name']}</b> — {_fmt(c['price'])} 🪙\n<i>{c['desc']}</i>\n\n"
        f"🎲 Шансы: {_chances(c)}\n"
        f"📊 Средняя выплата ≈ {_fmt(c['ev'])} 🪙 ({c['ev'] / c['price'] * 100:.0f}% цены)\n\n"
        "🔝 Самое ценное:\n" + "\n".join(f"{_it(i)} — {_fmt(item_info(i)['price'])} 🪙" for i in items)
    )
    rows = []
    if ckey == deal_case():
        dp = deal_price(ckey)
        if deal_available(uid):
            text += f"\n\n💥 <b>Предложение дня:</b> −{round(DEAL_DISCOUNT * 100)}% → <b>{_fmt(dp)}</b> 🪙 (1 раз в день)"
            rows.append([(f"💥 Со скидкой · {_fmt(dp)}🪙", f"cs:opend:{uid}:{ckey}")])
        else:
            text += "\n\n💥 Предложение дня на этот кейс сегодня уже использовано"
    text += f"\n\n🪙 Баланс: <b>{_fmt(p['coins'])}</b>"
    rows += [
        [(f"🎰 Открыть · {_fmt(c['price'])}🪙", f"cs:open:{uid}:{ckey}")],
        [(f"📦 ×5 сразу · {_fmt(c['price'] * 5)}🪙", f"cs:open5:{uid}:{ckey}")],
        [("⬅️ Кейсы", f"cs:cases:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")],
    ]
    return text, _kb(rows)


def free_drop_screen(uid: int, item: str, inv_id: int) -> tuple[str, Kb]:
    i = item_info(item)
    text = (f"🎟 <b>Бесплатный кейс дня</b>\n\n{_BANNER[i['rarity']]}\n\n{_it(item)}\n"
            f"💰 {_fmt(i['price'])} 🪙 — на халяву! Следующий кейс — завтра.")
    kb = _kb([
        [(f"💰 Продать за {_fmt(i['price'])}", f"cs:sell:{uid}:{inv_id}:0:-1")],
        [("📦 Кейсы", f"cs:cases:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")],
        [("🏠 Меню", f"cs:menu:{uid}")],
    ])
    return _with_notices(uid, text), kb


def top_screen(uid: int, mode: str = "all", chat_id: int | None = None) -> tuple[str, Kb]:
    group = chat_id is not None and chat_id < 0
    note = ""
    if mode == "chat" and not group:
        mode, note = "all", "<i>Чат-топ работает в группах — показываю общий.</i>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    if mode == "season":
        se = season_info()
        lb = season_leaderboard(None)
        lines = [f"📅 <b>СЕЗОН {se['no']}</b> · осталось <b>{_human_long(se['left'])}</b>",
                 "<i>рейтинг по ПРИРОСТУ капитала — стартуют все с нуля, прогресс не сбрасывается</i>",
                 "🏆 Призы: Кубки сезона топ-3 (30 000 / 15 000 / 8 000 🪙)\n"]
        for d in lb[:10]:
            mark = medals[d["rank"] - 1] if d["rank"] <= 3 else f"{d['rank']}."
            me = " ← ты" if d["user_id"] == uid else ""
            lines.append(f"{mark} {_e(d['name'])}{_tt(d['title'])} — <b>{'+' if d['growth'] >= 0 else '−'}{_fmt(abs(d['growth']))}</b> 🪙{me}")
        mine = next((d for d in lb if d["user_id"] == uid), None)
        if mine and mine["rank"] > 10:
            lines.append(f"…\n{mine['rank']}. {_e(mine['name'])} — <b>{'+' if mine['growth'] >= 0 else '−'}{_fmt(abs(mine['growth']))}</b> 🪙 ← ты")
        if se["last"]:
            lines.append(f"\n🏁 <b>Сезон {se['no'] - 1}:</b> " + " · ".join(
                f"{medals[r['place'] - 1]} {_e(r['name'])} (+{_fmt(r['growth'])})" for r in se["last"]))
    else:
        lb = leaderboard(None, chat_id if mode == "chat" else None)
        title = "💬 <b>ТОП ЭТОГО ЧАТА</b>" if mode == "chat" else "🏆 <b>ТОП КЕЙС-КЛУБА</b>"
        lines = [f"{title}\n<i>капитал = монеты + стоимость инвентаря</i>\n"]
        for d in lb[:10]:
            mark = medals[d["rank"] - 1] if d["rank"] <= 3 else f"{d['rank']}."
            me = " ← ты" if d["user_id"] == uid else ""
            lines.append(f"{mark} {_e(d['name'])}{_tt(d['title'])} — <b>{_fmt(d['networth'])}</b> 🪙 · ⚔️ {d['bw']}:{d['bl']}{me}")
        mine = next((d for d in lb if d["user_id"] == uid), None)
        if mine and mine["rank"] > 10:
            lines.append(f"…\n{mine['rank']}. {_e(mine['name'])} — <b>{_fmt(mine['networth'])}</b> 🪙 ← ты")
        if not lb:
            lines.append("Пока никого. Будь первым — /case")
    tabs = [("🌍 Все", "all"), ("💬 Чат", "chat"), ("📅 Сезон", "season")]
    row = [(f"✔️ {t}" if m == mode else t, f"cs:top:{uid}:{m}") for t, m in tabs]
    return note + "\n".join(lines), _kb([row, [("🏠 Меню", f"cs:menu:{uid}")]])


def profile_screen(uid: int, name: str) -> tuple[str, Kb]:
    p = get_profile(uid, name)
    best = (f"{_it(p['best_item'])} — {_fmt(p['best_price'])} 🪙" if p["best_item"] else "—")
    have = get_achievements(uid)
    text = (
        f"📊 <b>ПРОФИЛЬ</b> · {_e(name)}{_tt(p['title'])}\n\n"
        f"🪙 Баланс: <b>{_fmt(p['coins'])}</b>\n"
        f"📈 Капитал: <b>{_fmt(p['networth'])}</b> 🪙\n"
        f"📦 Открыто кейсов: {p['opened']}\n"
        f"💸 Потрачено: {_fmt(p['spent'])} · 💰 Продано на: {_fmt(p['earned'])}\n"
        f"🏅 Лучший дроп: {best}\n"
        f"⚔️ Батлы: {p['bw']} побед / {p['bl']} поражений (лучшая серия {p['bstreak_best']})\n"
        f"🚀 Апгрейды: {p['up_win']} из {p['up_try']} успешных · 🧾 контрактов: {p['contracts']}\n"
        f"🤝 Обменов: {p['trades']} · 🎁 подарков: {p['gifts']}\n"
        f"🏅 Достижения: {len(have)}/{len(ACH)} · 🔥 серия бонуса: {p['streak']} дн.\n\n"
        f"<i>Бонус дня {DAILY_BASE}–{DAILY_BASE + DAILY_STEP * DAILY_STREAK_MAX} 🪙, "
        f"пособие {BAILOUT} 🪙 если на нуле (раз в {BAILOUT_COOLDOWN // 3600} ч)</i>"
    )
    return text, _kb([[("🏅 Достижения", f"cs:ach:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")]])


def battle_pick_screen(uid: int) -> tuple[str, Kb]:
    p = get_profile(uid)
    rows = [[(f"{CASES[k]['icon']} {CASES[k]['name']} · {_fmt(CASES[k]['price'])}🪙", f"cs:bnew:{uid}:{k}")] for k in SHOP]
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    text = ("⚔️ <b>БАТЛ</b>\nВыбери кейс: ты ставишь его цену, соперник — столько же. Каждому выпадает предмет "
            f"из этого кейса — <b>у кого дороже, забирает оба</b>.\n\n🪙 Баланс: <b>{_fmt(p['coins'])}</b>")
    return text, _kb(rows)


def ach_screen(uid: int) -> tuple[str, Kb]:
    have = get_achievements(uid)
    cur = _title_of(uid)
    lines = [f"🏅 <b>ДОСТИЖЕНИЯ</b> · {len(have)}/{len(ACH)}",
             "<i>Выбранное достижение показывается титулом рядом с именем в топе, батлах и обмене.</i>\n"]
    btns = []
    for k, (icon, name, desc, _) in ACH.items():
        if k in have:
            lines.append(f"✅ {icon} <b>{name}</b> — {desc}" + (" ← титул" if k == cur else ""))
            btns.append((f"{'✔️ ' if k == cur else ''}{icon} {name}", f"cs:title:{uid}:{k}"))
        else:
            lines.append(f"🔒 {icon} {name} — {desc}")
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if cur:
        rows.append([("❎ Снять титул", f"cs:title:{uid}:-")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines), _kb(rows)


# ── универсальный экран выбора предмета ───────────────────────────────────────
def _pick_screen(owner: int, page: int, title: str, item_cb, page_cb, back_cb, *, asc: bool = False,
                 marked=frozenset(), footer: str = "", extra=(), empty: str = "🎒 Пусто") -> tuple[str, Kb]:
    pg = inventory_page(owner, page, asc=asc, no_trophy=True)
    if not pg["total"]:
        return f"{title}\n\n{empty}", _kb([[("⬅️ Назад", back_cb)]])
    lines = [f"{title} · стр. {pg['page'] + 1}/{pg['pages']}"]
    btns = []
    for n, (inv_id, item) in enumerate(pg["items"], 1):
        on = inv_id in marked
        lines.append(f"{n}. {'✅ ' if on else ''}{_it(item)} — {_fmt(item_info(item)['price'])} 🪙")
        btns.append((f"{'✅' if on else '👉'} {n}", item_cb(inv_id, pg["page"])))
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    nav = []
    if pg["page"] > 0:
        nav.append(("◀️", page_cb(pg["page"] - 1)))
    nav.append((f"{pg['page'] + 1}/{pg['pages']}", page_cb(pg["page"])))
    if pg["page"] < pg["pages"] - 1:
        nav.append(("▶️", page_cb(pg["page"] + 1)))
    rows.append(nav)
    rows.extend(extra)
    rows.append([("⬅️ Назад", back_cb)])
    if footer:
        lines.append("\n" + footer)
    return "\n".join(lines), _kb(rows)


# ── апгрейд ───────────────────────────────────────────────────────────────────
def up_pick_screen(uid: int, page: int = 0) -> tuple[str, Kb]:
    return _pick_screen(uid, page, "🚀 <b>АПГРЕЙД</b>: выбери предмет", lambda i, pg: f"cs:upi:{uid}:{i}",
                        lambda pg: f"cs:up:{uid}:{pg}", f"cs:menu:{uid}",
                        footer="<i>Шанс честный: 90% × цена предмета / цена цели. Не повезёт — предмет сгорит.</i>",
                        empty="🎒 Нечем рисковать. Открой кейс!")


def up_options_screen(uid: int, inv_id: int) -> tuple[str, Kb]:
    item = get_inv_item(uid, inv_id)
    if item is None:
        return "Этот предмет уже не в инвентаре.", _kb([[("🚀 К апгрейду", f"cs:up:{uid}:0")]])
    opts = upgrade_options(item)
    if not opts:
        return (f"🚀 {_it(item)}\n\nДля этого предмета нет вариантов апгрейда.",
                _kb([[("🚀 Выбрать другой", f"cs:up:{uid}:0")]]))
    p0 = item_info(item)["price"]
    lines = [f"🚀 <b>АПГРЕЙД</b>\nРискуешь: {_it(item)} — {_fmt(p0)} 🪙\n"]
    rows = []
    for o in opts:
        t = item_info(o["target"])
        lines.append(f"×{o['mult']:.1f} → {_it(o['target'])} — {_fmt(t['price'])} 🪙 · шанс <b>{_pct(o['chance'])}</b>")
        label = f"🚀 ×{o['mult']:.1f} · {_pct(o['chance'])} → {_cut_name(t['name'])}"
        rows.append([(label, f"cs:upgo:{uid}:{inv_id}:{o['target']}")])
    rows.append([("⬅️ Другой предмет", f"cs:up:{uid}:0")])
    lines.append("\n<i>Не повезёт — предмет сгорит. Цели выбираются случайно при каждом открытии экрана.</i>")
    return "\n".join(lines), _kb(rows)


def _cut_name(s: str, n: int = 24) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


async def _anim_upgrade(msg, res: dict):
    cells = 10
    p, r, ok = res["chance"], res["r"], res["success"]
    wc = max(1, min(cells - 1, round(p * cells)))
    if ok:
        final = min(wc - 1, int(r / p * wc))
    else:
        final = wc + min(cells - wc - 1, int((r - p) / (1 - p) * (cells - wc)))
    bar = "🟩" * wc + "🟥" * (cells - wc)
    offs = [None, -31, -24, -18, -13, -9, -6, -3, -2, 2, -1, 0]
    delays = [0.8, 0.45, 0.45, 0.5, 0.6, 0.75, 0.9, 1.0, 1.0, 0.9, 0.9, 0.8]
    notes = ["заряжаем…", "набираем ход", "набираем ход", "летит", "летит", "замедляется", "замедляется",
             "почти…", "ну же…", "ещё чуть-чуть…", "дрожит…", "СТОП!"]
    head = (f"🚀 <b>АПГРЕЙД</b>\n{_it(res['src'])} → {_it(res['target'])}\n"
            f"шанс <b>{_pct(p)}</b> <i>(шкала условная)</i>\n\n")
    for k, off in enumerate(offs):
        pos = final if off is None and k == len(offs) - 1 else (final + (off or 0)) % cells
        if k == len(offs) - 1:
            pos = final
        pointer = "▫️" * pos + "🔻" + "▫️" * (cells - 1 - pos)
        await _edit(msg, head + f"{pointer}\n{bar}\n\n<i>{'◐◓◑◒'[k % 4]} {notes[k]}</i>")
        await asyncio.sleep(delays[k])


async def _do_upgrade(cb, uid: int, inv_id: int, target: str):
    if uid in _BUSY:
        await cb.answer("Погоди, ещё идёт действие 🎰")
        return
    res = upgrade_item(uid, inv_id, target)
    if not res["ok"]:
        await cb.answer("Не получилось: предмет уже не доступен для апгрейда", show_alert=True)
        t, k = up_pick_screen(uid, 0)
        await _edit(cb.message, t, k)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        try:
            await _anim_upgrade(cb.message, res)
        except Exception:
            logger.exception("анимация апгрейда упала")
        if res["success"]:
            price = item_info(res["target"])["price"]
            text = (f"🟢 <b>УСПЕХ!</b> (шанс был {_pct(res['chance'])})\n\n{_it(res['target'])}\n"
                    f"💰 {_fmt(price)} 🪙 · ×{price / item_info(res['src'])['price']:.1f} к цене {_it(res['src'])}")
            rows = [[(f"💰 Продать за {_fmt(price)}", f"cs:sell:{uid}:{res['inv_id']}:0:-1")]]
        else:
            text = (f"🔴 <b>Не повезло…</b> (шанс был {_pct(res['chance'])})\n\n"
                    f"{_it(res['src'])} сгорел 🔥\nЦель была: {_it(res['target'])}")
            rows = []
        rows += [[("🚀 Ещё апгрейд", f"cs:up:{uid}:0"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")], [("🏠 Меню", f"cs:menu:{uid}")]]
        await _edit(cb.message, _with_notices(uid, text), _kb(rows))
    finally:
        _BUSY.discard(uid)


# ── контракт ──────────────────────────────────────────────────────────────────
def ct_screen(uid: int) -> tuple[str, Kb]:
    items = basket_items(uid)
    total = sum(item_info(i)["price"] for _, i in items)
    outcomes = " · ".join(f"×{m:g} — {_pct(w)}" for m, w in CONTRACT_OUTCOMES)
    lines = [f"🧾 <b>КОНТРАКТ</b>\nОбменяй <b>{CONTRACT_MIN}–{CONTRACT_MAX}</b> предметов на один случайный.\n",
             f"В контракте: <b>{len(items)}</b>/{CONTRACT_MAX} на <b>{_fmt(total)}</b> 🪙"]
    for n, (_, i) in enumerate(items, 1):
        lines.append(f"{n}. {_it(i)} — {_fmt(item_info(i)['price'])} 🪙")
    lines.append(f"\n🎲 Итоговая цена = сумма × случайный множитель:\n{outcomes}")
    if items:
        lines.append(f"📊 Средний возврат при такой сумме ≈ <b>{contract_expected_ratio(total) * 100:.0f}%</b>")
    rows = [[("➕ Выбрать предметы", f"cs:ctp:{uid}:0")]]
    if len(items) >= CONTRACT_MIN:
        rows.append([(f"🧾 Подписать · {len(items)} шт.", f"cs:ctg:{uid}")])
    if items:
        rows.append([("🧹 Очистить", f"cs:ctc:{uid}")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines), _kb(rows)


def ct_pick_screen(uid: int, page: int = 0) -> tuple[str, Kb]:
    items = basket_items(uid)
    total = sum(item_info(i)["price"] for _, i in items)
    return _pick_screen(uid, page, "🧾 <b>КОНТРАКТ</b>: отметь предметы", lambda i, pg: f"cs:ctt:{uid}:{i}:{pg}",
                        lambda pg: f"cs:ctp:{uid}:{pg}", f"cs:ct:{uid}", asc=True, marked={i for i, _ in items},
                        footer=f"🧾 Выбрано: <b>{len(items)}</b>/{CONTRACT_MAX} на {_fmt(total)} 🪙",
                        extra=[[("✅ Готово → к контракту", f"cs:ct:{uid}")]], empty="🎒 Пусто — нечего вкладывать")


async def _do_contract_sign(cb, uid: int):
    if uid in _BUSY:
        await cb.answer("Погоди, ещё идёт действие 🎰")
        return
    res = sign_contract(uid)
    if not res["ok"]:
        await cb.answer(f"Нужно от {CONTRACT_MIN} до {CONTRACT_MAX} предметов (сейчас {res.get('n', 0)})", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        try:
            for k in range(4):
                await _edit(cb.message, f"🧾 <b>КОНТРАКТ</b>\n\n{res['n']} предметов на <b>{_fmt(res['total'])}</b> 🪙\n\n"
                                        f"{'📄' * (k + 1)}{'▫️' * (3 - k)}\n<i>{'◐◓◑◒'[k]} подписываем и считаем…</i>")
                await asyncio.sleep(0.8)
        except Exception:
            logger.exception("анимация контракта упала")
        price = item_info(res["item"])["price"]
        ratio = price / res["total"]
        verdict = "🟢 в плюс!" if ratio > 1 else ("🟡 почти вернул" if ratio > 0.75 else "🔴 в минус")
        text = (f"🧾 <b>КОНТРАКТ ПОДПИСАН</b>\n{res['n']} предметов на {_fmt(res['total'])} 🪙 →\n\n"
                f"{_BANNER[item_info(res['item'])['rarity']]}\n{_it(res['item'])}\n"
                f"💰 {_fmt(price)} 🪙 · ×{ratio:.2f} от вложенного · {verdict}")
        rows = [[(f"💰 Продать за {_fmt(price)}", f"cs:sell:{uid}:{res['inv_id']}:0:-1")],
                [("🧾 Новый контракт", f"cs:ct:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")], [("🏠 Меню", f"cs:menu:{uid}")]]
        await _edit(cb.message, _with_notices(uid, text), _kb(rows))
    finally:
        _BUSY.discard(uid)


# ── подарки ───────────────────────────────────────────────────────────────────
def _give_err(r: dict) -> str:
    return {
        "self": "Себе нельзя 😄",
        "min": f"Минимальный перевод — {GIVE_MIN} 🪙",
        "no_target": "Этот игрок ещё не играл — пусть откроет /case",
        "money": f"Не хватает {_fmt(r.get('need', 0))} 🪙",
        "cap": f"Лимит подарков на сутки: осталось {_fmt(r.get('left', 0))} 🪙-стоимости",
        "gone": "Этого предмета уже нет в инвентаре",
        "trophy": "Кубки сезона нельзя передавать — это память о победе 🏆",
    }.get(r.get("reason"), "Не получилось")


def give_coin_screen(uid: int, tid: int, amount: int) -> tuple[str, Kb]:
    tname = user_name(tid) or str(tid)
    fee = int(amount * GIVE_FEE)
    text = (f"🎁 <b>Перевод монет</b>\n{_mention(tid, tname)} получит <b>{_fmt(amount - fee)}</b> 🪙 "
            f"(ты отдаёшь <b>{_fmt(amount)}</b>, комиссия {round(GIVE_FEE * 100)}% = {_fmt(fee)})\n"
            f"Лимит подарков на сутки: осталось {_fmt(give_left(uid))} 🪙")
    return text, _kb([[("✅ Перевести", f"cs:gc:{uid}:{tid}:{amount}:1"), ("❌ Отмена", f"cs:gx:{uid}")]])


def give_item_pick_screen(uid: int, tid: int, page: int = 0) -> tuple[str, Kb]:
    tname = user_name(tid) or str(tid)
    return _pick_screen(uid, page, f"🎁 <b>Что подарить</b> → {_e(tname)}", lambda i, pg: f"cs:gi:{uid}:{tid}:{i}:0",
                        lambda pg: f"cs:gip:{uid}:{tid}:{pg}", f"cs:gx:{uid}",
                        footer="<i>Монетами: <code>/give 500</code> ответом на сообщение игрока</i>",
                        empty="🎒 Дарить нечего")


async def _do_give_coins(cb, uid: int, tid: int, amount: int, ok: int):
    tname = user_name(tid)
    if tname is None:
        await cb.answer(_give_err({"reason": "no_target"}), show_alert=True)
        return
    if not ok:
        await cb.answer()
        t, k = give_coin_screen(uid, tid, amount)
        await _edit(cb.message, t, k)
        return
    r = give_coins(uid, tid, amount)
    if not r["ok"]:
        await cb.answer(_give_err(r), show_alert=True)
        return
    await cb.answer("Переведено!")
    me = user_name(uid) or str(uid)
    await _edit(cb.message, f"🎁 {_e(_dn(uid, me))} перевёл {_mention(tid, tname)} "
                            f"<b>{_fmt(r['got'])}</b> 🪙 (комиссия {_fmt(r['fee'])})", None)


async def _do_give_item(cb, uid: int, tid: int, inv_id: int, ok: int):
    tname = user_name(tid)
    item = get_inv_item(uid, inv_id)
    if tname is None or item is None:
        await cb.answer(_give_err({"reason": "no_target" if tname is None else "gone"}), show_alert=True)
        return
    if not ok:
        await cb.answer()
        price = item_info(item)["price"]
        await _edit(cb.message, f"🎁 Подарить {_it(item)} ({_fmt(price)} 🪙) → {_mention(tid, tname)}?\n"
                                f"Лимит подарков на сутки: осталось {_fmt(give_left(uid))} 🪙",
                    _kb([[("✅ Подарить", f"cs:gi:{uid}:{tid}:{inv_id}:1"), ("❌ Отмена", f"cs:gx:{uid}")]]))
        return
    r = give_item(uid, tid, inv_id)
    if not r["ok"]:
        await cb.answer(_give_err(r), show_alert=True)
        return
    await cb.answer("Подарено!")
    me = user_name(uid) or str(uid)
    await _edit(cb.message, f"🎁 {_e(_dn(uid, me))} подарил {_mention(tid, tname)}: {_it(r['item'])} "
                            f"({_fmt(r['price'])} 🪙)", None)


# ── обмен ─────────────────────────────────────────────────────────────────────
def trade_mine_screen(uid: int, tid: int, page: int = 0) -> tuple[str, Kb]:
    tname = user_name(tid) or str(tid)
    return _pick_screen(uid, page, f"🤝 <b>ОБМЕН</b> с {_e(tname)}: выбери <b>свой</b> предмет",
                        lambda i, pg: f"cs:trs:{uid}:{tid}:{i}", lambda pg: f"cs:trp:{uid}:{tid}:{pg}",
                        f"cs:gx:{uid}", empty="🎒 Тебе нечего предложить")


def trade_theirs_screen(uid: int, tid: int, myinv: int, page: int = 0) -> tuple[str, Kb]:
    tname = user_name(tid) or str(tid)
    mine = get_inv_item(uid, myinv)
    return _pick_screen(tid, page, f"🤝 Ты отдаёшь {_it(mine) if mine else '—'}. Что хочешь у <b>{_e(tname)}</b>?",
                        lambda i, pg: f"cs:trc:{uid}:{tid}:{myinv}:{i}",
                        lambda pg: f"cs:trq:{uid}:{tid}:{myinv}:{pg}", f"cs:trp:{uid}:{tid}:0",
                        empty="У игрока нет подходящих предметов")


def trade_offer(t: dict, a_item: str, b_item: str) -> tuple[str, Kb]:
    pa, pb = item_info(a_item)["price"], item_info(b_item)["price"]
    text = (f"🤝 <b>ОБМЕН</b>\n{_mention(t['a'], t['a_name'])} предлагает:\n{_it(a_item)} — {_fmt(pa)} 🪙\n\n"
            f"на предмет {_mention(t['b'], t['b_name'])}:\n{_it(b_item)} — {_fmt(pb)} 🪙\n\n"
            f"{_mention(t['b'], t['b_name'])}, принимаешь? ⏳ {TRADE_TTL // 60} мин")
    return text, _kb([[("✅ Принять", f"cs:tra:{t['id']}"), ("❌ Отклонить", f"cs:trd:{t['id']}"),
                       ("✖️ Отмена", f"cs:trx:{t['id']}")]])


async def _do_trade_create(cb, uid: int, tid: int, myinv: int, theirinv: int):
    me, tname = user_name(uid) or str(uid), user_name(tid)
    if tname is None:
        await cb.answer(_give_err({"reason": "no_target"}), show_alert=True)
        return
    r = create_trade(uid, _dn(uid, me), tid, _dn(tid, tname), myinv, theirinv, cb.message.chat.id, cb.message.message_id)
    if not r["ok"]:
        await cb.answer("Предмет уже изменился — выбери заново" if r["reason"] == "gone"
                        else _give_err(r), show_alert=True)
        return
    await cb.answer("Предложение отправлено")
    t = get_trade(r["id"])
    text, kb = trade_offer(t, r["a_item"], r["b_item"])
    await _edit(cb.message, text, kb)


async def _do_trade_answer(cb, tid_: str, kind: str):
    uid = cb.from_user.id
    if kind == "accept":
        r = accept_trade(tid_, uid)
        if r["ok"]:
            await cb.answer("Обмен состоялся!")
            await _edit(cb.message, f"🤝 <b>ОБМЕН СОСТОЯЛСЯ</b>\n{_mention(r['a'], r['a_name'])} получил {_it(r['b_item'])}\n"
                                    f"{_mention(r['b'], r['b_name'])} получил {_it(r['a_item'])}", None)
            return
        msg = {"closed": "Это предложение уже закрыто", "not_yours": "Принять может только адресат обмена",
               "expired": "Предложение истекло", "gone": "Один из предметов уже продан или передан — обмен отменён"}
        await cb.answer(msg.get(r["reason"], "Не получилось"), show_alert=True)
        if r["reason"] in ("expired", "gone"):
            await _edit(cb.message, "⌛ Обмен недействителен." if r["reason"] == "gone" else "⌛ Предложение истекло.", None)
        return
    r = close_trade(tid_, uid, "cancel" if kind == "cancel" else "decline")
    if r["ok"]:
        await cb.answer("Готово")
        await _edit(cb.message, "✖️ Обмен отменён инициатором." if kind == "cancel" else "❌ Обмен отклонён.", None)
    elif r["reason"] == "not_yours":
        await cb.answer("Отменить может только инициатор, отклонить — только адресат", show_alert=True)
    else:
        await cb.answer("Это предложение уже закрыто", show_alert=True)


# ── анимация открытия (обновлена: бесплатный кейс) ────────────────────────────
async def _anim_open(msg, ckey: str, drop: str):
    c = CASES[ckey]
    price_txt = "бесплатно" if c["price"] == 0 else f"{_fmt(c['price'])} 🪙"
    track = [roll_item(ckey) for _ in range(64)]
    center = 56
    track[center] = drop
    if item_info(drop)["rarity"] in ("junk", "common") and _RNG.random() < 0.5:      # «почти выпало»
        top_tier = max(c["weights"], key=lambda t: RARITY_ORDER.index(t))
        track[center + 1] = _RNG.choice(c["pool"][top_tier])
    positions = [8, 17, 26, 34, 41, 47, 51, 54, 55, 56]
    delays = [0.45, 0.45, 0.5, 0.55, 0.65, 0.8, 0.95, 1.1, 1.2, 0.9]
    notes = ["щёлк…", "крутится", "крутится", "крутится", "замедляется", "замедляется", "почти…", "тихо-тихо…",
             "ещё чуть-чуть…", "СТОП!"]
    for k, p in enumerate(positions):
        cells = []
        for d in range(-4, 5):
            em = RARITY[item_info(track[p + d])["rarity"]][0]
            cells.append(f"【{em}】" if d == 0 else em)
        cur = item_info(track[p])
        text = (f"{c['icon']} <b>{c['name']}</b> · {price_txt}\n\n"
                f"{''.join(cells)}\n"
                f"{'🔽' if k < len(positions) - 1 else '✅'} <b>{_e(cur['name'])}</b>\n\n"
                f"<i>{'◐◓◑◒'[k % 4]} {notes[k]}</i>")
        await _edit(msg, text)
        await asyncio.sleep(delays[k])
    r = item_info(drop)["rarity"]
    if r in ("epic", "legend", "mythic"):
        em = RARITY[r][0]
        for k in range(2):
            flash = (em * 5) if k == 0 else ("✨" + em) * 3
            await _edit(msg, f"{flash}\n\n{_it(drop)}\n\n{flash}")
            await asyncio.sleep(0.55)


async def _do_open(cb, uid: int, name: str, ckey: str, deal: bool = False):
    if ckey not in SHOP:
        await cb.answer("Нет такого кейса", show_alert=True)
        return
    if uid in _BUSY:
        await cb.answer("Погоди, кейс ещё открывается 🎰")
        return
    get_profile(uid, name)
    res = open_case(uid, ckey, 1, None, deal)
    if not res["ok"]:
        reason = res.get("reason")
        if reason == "deal_used":
            await cb.answer("Предложение дня уже использовано сегодня", show_alert=True)
        elif reason == "bad":
            await cb.answer("Это предложение уже не действует — кейс дня сменился", show_alert=True)
        else:
            b = maybe_bailout(uid)
            extra = "\n🆘 Но ты на нуле — загляни в меню за пособием." if b["given"] else ""
            await cb.answer(f"Не хватает {_fmt(res['need'])} 🪙{extra}", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        item, inv_id = res["drops"][0]
        try:
            await _anim_open(cb.message, ckey, item)
        except Exception:
            logger.exception("анимация кейса упала — показываю дроп")
        text, kb = drop_screen(uid, ckey, item, inv_id, res["coins"], res["cost"])
        await _edit(cb.message, _with_notices(uid, text), kb)
    finally:
        _BUSY.discard(uid)


async def _do_free(cb, uid: int, name: str):
    if uid in _BUSY:
        await cb.answer("Погоди, кейс ещё открывается 🎰")
        return
    get_profile(uid, name)
    res = open_free(uid)
    if not res["ok"]:
        await cb.answer(f"Бесплатный кейс уже открыт. Следующий через {_human(res['wait'])}", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        try:
            await _anim_open(cb.message, "free", res["item"])
        except Exception:
            logger.exception("анимация бесплатного кейса упала")
        text, kb = free_drop_screen(uid, res["item"], res["inv_id"])
        await _edit(cb.message, text, kb)
    finally:
        _BUSY.discard(uid)


# ── команды ───────────────────────────────────────────────────────────────────
def _touch(user, chat_id) -> tuple[int, str]:
    uid, name = user.id, _name_of(user)
    get_profile(uid, name, None, getattr(user, "username", None) or "")
    touch_chat(uid, chat_id)
    return uid, name


async def cmd_case(message: types.Message):
    if not message.from_user:
        return
    uid, name = _touch(message.from_user, message.chat.id)
    text, kb = menu_screen(uid, name)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def cmd_daily(message: types.Message):
    if not message.from_user:
        return
    uid, name = _touch(message.from_user, message.chat.id)
    r = claim_daily(uid)
    if r["ok"]:
        txt = (f"🎁 <b>Бонус дня:</b> +<b>{_fmt(r['amount'])}</b> 🪙\n🔥 Серия: {r['streak']} дн. "
               f"(завтра будет больше, максимум на {DAILY_STREAK_MAX + 1}-й день)")
    else:
        txt = f"🎁 Бонус уже получен. Следующий через <b>{_human(r['wait'])}</b>."
    fs = free_status(uid)
    txt += "\n🎟 Бесплатный кейс дня ждёт тебя!" if fs["ready"] else ""
    txt = _with_notices(uid, txt)
    await message.answer(txt, parse_mode="HTML", reply_markup=_kb([[("🏠 Меню", f"cs:menu:{uid}"),
                                                                   ("📦 Кейсы", f"cs:cases:{uid}")]]))


async def cmd_inv(message: types.Message):
    if not message.from_user:
        return
    uid, _ = _touch(message.from_user, message.chat.id)
    text, kb = inv_screen(uid, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def cmd_ctop(message: types.Message):
    if not message.from_user:
        return
    uid, _ = _touch(message.from_user, message.chat.id)
    text, kb = top_screen(uid, "chat" if message.chat.id < 0 else "all", message.chat.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


def _resolve_target(message, args: list[str]) -> tuple[int | None, str]:
    uid = message.from_user.id
    tid = None
    rt = getattr(message, "reply_to_message", None)
    if rt is not None and rt.from_user is not None and not getattr(rt.from_user, "is_bot", False):
        tid = rt.from_user.id
    else:
        for a in args:
            if a.startswith("@") and len(a) > 1:
                tid = find_user_by_username(a)
                break
    if tid is None:
        return None, "Ответь командой на сообщение игрока или укажи его @username"
    if tid == uid:
        return None, "Себе нельзя 😄"
    if not user_exists(tid):
        return None, "Этот игрок ещё не играл — пусть откроет /case"
    return tid, ""


async def cmd_give(message: types.Message):
    if not message.from_user:
        return
    uid, _ = _touch(message.from_user, message.chat.id)
    args = (message.text or "").split()[1:]
    tid, err = _resolve_target(message, args)
    if err:
        await message.answer(f"🎁 {err}\n\nМонеты: <code>/give 500</code> · предмет: <code>/give</code> "
                             "(ответом на сообщение игрока)", parse_mode="HTML")
        return
    amount = next((int(a) for a in args if a.isdigit()), None)
    if amount is not None:
        pre = give_coins.__wrapped__ if False else None                    # (проверка без списания ниже)
        if amount < GIVE_MIN:
            await message.answer(_give_err({"reason": "min"}))
            return
        p = get_profile(uid)
        if p["coins"] < amount:
            await message.answer(_give_err({"reason": "money", "need": amount - p["coins"]}))
            return
        if amount > give_left(uid):
            await message.answer(_give_err({"reason": "cap", "left": give_left(uid)}))
            return
        text, kb = give_coin_screen(uid, tid, amount)
    else:
        text, kb = give_item_pick_screen(uid, tid, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def cmd_trade(message: types.Message):
    if not message.from_user:
        return
    uid, _ = _touch(message.from_user, message.chat.id)
    tid, err = _resolve_target(message, (message.text or "").split()[1:])
    if err:
        await message.answer(f"🤝 {err}", parse_mode="HTML")
        return
    text, kb = trade_mine_screen(uid, tid, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── диспетчер callback ────────────────────────────────────────────────────────
async def _dispatch(cb: types.CallbackQuery):
    parts = (cb.data or "").split(":")
    if len(parts) < 3 or cb.message is None:
        await cb.answer("Эта кнопка устарела", show_alert=True)
        return
    act = parts[1]
    chat_id = cb.message.chat.id
    # кнопки без владельца: нажать может любой (внутри проверяется роль)
    if act in ("bacc", "bcan", "brev", "tra", "trd", "trx"):
        touch_chat(cb.from_user.id, chat_id)
    if act == "bacc":
        return await _do_battle_accept(cb, parts[2])
    if act == "bcan":
        return await _do_battle_cancel(cb, parts[2])
    if act == "brev":                                    # реванш/свой батл — для любого нажавшего
        return await _do_battle_new(cb, cb.from_user.id, _dn(cb.from_user.id, _name_of(cb.from_user)), parts[2], fresh_message=True)
    if act == "tra":
        return await _do_trade_answer(cb, parts[2], "accept")
    if act == "trd":
        return await _do_trade_answer(cb, parts[2], "decline")
    if act == "trx":
        return await _do_trade_answer(cb, parts[2], "cancel")
    uid = int(parts[2])
    if cb.from_user.id != uid:
        await cb.answer("Это меню другого игрока. Своё — /case", show_alert=True)
        return
    name = _name_of(cb.from_user)
    get_profile(uid, name, None, getattr(cb.from_user, "username", None) or "")
    touch_chat(uid, chat_id)
    msg = cb.message
    if act == "menu":
        await cb.answer()
        t, k = menu_screen(uid, name)
    elif act == "cases":
        await cb.answer()
        t, k = cases_screen(uid)
    elif act == "case":
        if parts[3] not in SHOP:
            await cb.answer("Нет такого кейса", show_alert=True)
            return
        await cb.answer()
        t, k = case_screen(uid, parts[3])
    elif act == "open":
        return await _do_open(cb, uid, name, parts[3])
    elif act == "opend":
        return await _do_open(cb, uid, name, parts[3], deal=True)
    elif act == "open5":
        if parts[3] not in SHOP:
            await cb.answer("Нет такого кейса", show_alert=True)
            return
        return await _do_open5(cb, uid, name, parts[3])
    elif act == "free":
        return await _do_free(cb, uid, name)
    elif act == "inv":
        await cb.answer()
        t, k = inv_screen(uid, int(parts[3]))
    elif act == "sell":
        return await _do_sell(cb, uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act == "bulk":
        return await _do_bulk(cb, uid, parts[3], int(parts[4]))
    elif act == "daily":
        return await _do_daily(cb, uid, name)
    elif act == "top":
        await cb.answer()
        t, k = top_screen(uid, parts[3] if len(parts) > 3 else "all", chat_id)
    elif act == "prof":
        await cb.answer()
        t, k = profile_screen(uid, name)
    elif act == "battle":
        await cb.answer()
        t, k = battle_pick_screen(uid)
    elif act == "bnew":
        if parts[3] not in SHOP:
            await cb.answer("Нет такого кейса", show_alert=True)
            return
        return await _do_battle_new(cb, uid, _dn(uid, name), parts[3])
    elif act == "ach":
        await cb.answer()
        t, k = ach_screen(uid)
    elif act == "title":
        if set_title(uid, parts[3]):
            await cb.answer("Титул выбран" if parts[3] != "-" else "Титул снят")
        else:
            await cb.answer("Это достижение ещё не получено", show_alert=True)
        t, k = ach_screen(uid)
    elif act == "up":
        await cb.answer()
        t, k = up_pick_screen(uid, int(parts[3]))
    elif act == "upi":
        await cb.answer()
        t, k = up_options_screen(uid, int(parts[3]))
    elif act == "upgo":
        return await _do_upgrade(cb, uid, int(parts[3]), parts[4])
    elif act == "ct":
        await cb.answer()
        t, k = ct_screen(uid)
    elif act == "ctp":
        await cb.answer()
        t, k = ct_pick_screen(uid, int(parts[3]))
    elif act == "ctt":
        r = basket_toggle(uid, int(parts[3]))
        if not r["ok"]:
            await cb.answer("В контракт можно положить не больше 10 предметов" if r["reason"] == "full"
                            else "Этот предмет нельзя вложить", show_alert=True)
        else:
            await cb.answer("Добавлено" if r["added"] else "Убрано")
        t, k = ct_pick_screen(uid, int(parts[4]))
    elif act == "ctc":
        basket_clear(uid)
        await cb.answer("Корзина очищена")
        t, k = ct_screen(uid)
    elif act == "ctg":
        return await _do_contract_sign(cb, uid)
    elif act == "gx":
        await cb.answer("Отменено")
        t, k = "❌ Отменено.", None
    elif act == "gc":
        return await _do_give_coins(cb, uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act == "gip":
        await cb.answer()
        t, k = give_item_pick_screen(uid, int(parts[3]), int(parts[4]))
    elif act == "gi":
        return await _do_give_item(cb, uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act == "trp":
        await cb.answer()
        t, k = trade_mine_screen(uid, int(parts[3]), int(parts[4]))
    elif act == "trs":
        if get_inv_item(uid, int(parts[4])) is None:
            await cb.answer("Предмет уже не в инвентаре", show_alert=True)
            return
        await cb.answer()
        t, k = trade_theirs_screen(uid, int(parts[3]), int(parts[4]), 0)
    elif act == "trq":
        await cb.answer()
        t, k = trade_theirs_screen(uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act == "trc":
        return await _do_trade_create(cb, uid, int(parts[3]), int(parts[4]), int(parts[5]))
    else:
        await cb.answer("Неизвестная кнопка", show_alert=True)
        return
    await _edit(msg, t, k)


CASES_HELP = ("🎰 <b>кейс-клуб</b>\n/case — меню: кейсы, инвентарь, бонус, апгрейд, контракты, топ, батлы\n"
              "/daily — бонус · /inv — инвентарь · /ctop — топ (в группе — по чату)\n"
              "/give 500 и /trade — ответом на сообщение игрока: подарок и обмен")


def register_cases_handlers(dp):
    _db()                                               # создаём БД/таблицы и мигрируем при старте
    dp.message.register(cmd_case, Command(*COMMANDS["menu"]))
    dp.message.register(cmd_daily, Command(*COMMANDS["daily"]))
    dp.message.register(cmd_inv, Command(*COMMANDS["inv"]))
    dp.message.register(cmd_ctop, Command(*COMMANDS["top"]))
    dp.message.register(cmd_give, Command(*COMMANDS["give"]))
    dp.message.register(cmd_trade, Command(*COMMANDS["trade"]))
    dp.callback_query.register(on_callback, F.data.startswith("cs:"))
    logger.info("🎰 cases_game зарегистрирован (/case, /daily, /inv, /ctop, /give, /trade), БД: %s", _DB_PATH)
