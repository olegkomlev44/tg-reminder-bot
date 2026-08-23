"""
duty_handlers.py — всё что связано с нарядами, расписанием, настройками,
клавиатурами, планировщиком и cmd_start/cmd_getchatid.
"""

import asyncio
import json
import logging
import os
import random
import time
import traceback
from datetime import date, datetime, timedelta

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
)

from config import (
    BASE_DIR, CHAT_ID, DATA_FILE, MOODS, PERSON_EMOJI, PROCEDURA_LIST,
    SVODKI_LIST, TEMP_DIR, TIMEZONE, WEEKDAY_STYLE,
    SVODKI_HEADERS_BY_DAY, PROC_HEADERS_BY_DAY,
    GAMING_HEADERS_SVODKI, GAMING_HEADERS_PROC, GAMING_ENDINGS,
    DONE_REPLIES, DONE_REPLIES_GAMING, RETRY_PREFIX, RETRY_PREFIX_GAMING,
    PERSONAL_SVODKI, PERSONAL_SVODKI_GAMING, PERSONAL_PROC, PERSONAL_PROC_GAMING,
    MONDAY_INTROS, SUNDAY_INTROS, JULY_DOCTORS, AUGUST_DOCTORS, EASTER_EGGS,
)

logger = logging.getLogger(__name__)

# Импортируем генераторы карточек
try:
    from card_generator import make_reminder_card, THEME_KEYS
    logger.info(f"🟢 card_generator импортирован, тем: {len(THEME_KEYS)}")
except Exception:
    logger.error("🔴 card_generator недоступен:\n" + traceback.format_exc())
    make_reminder_card = None
    THEME_KEYS = ["default"]

try:
    from dembel_generator import make_dembel_card, get_all_timers, DEMBEL_DATES
    logger.info(f"🟢 dembel_generator импортирован, людей: {len(DEMBEL_DATES)}")
except Exception:
    logger.error("🔴 dembel_generator недоступен")
    make_dembel_card = None
    get_all_timers   = None
    DEMBEL_DATES     = {}


# ── FSM ───────────────────────────────────────────────────────────────────────
class AdminStates(StatesGroup):
    waiting_chat_id  = State()
    waiting_register = State()


# ── Data helpers ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("ai_random_replies_enabled", True)
        data.setdefault("track_cache", {})
        data.setdefault("user_history", {})
        return data
    data = {
        "start_date": date.today().isoformat(), "svodki_start_index": 0,
        "procedura_start_index": 0, "chat_id": CHAT_ID,
        "reminders_enabled": True, "ai_random_replies_enabled": True,
        "personal_ids": {}, "log": {}, "pending_retry": {}, "duty_counts": {},
        "pinned_msg_id": None, "daily_mood": {}, "track_cache": {}, "user_history": {}
    }
    save_data(data)
    return data

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def increment_count(name: str, duty_type: str):
    data = load_data()
    person = data.setdefault("duty_counts", {}).setdefault(name, {"svodki": 0, "proc": 0})
    person[duty_type] = person.get(duty_type, 0) + 1
    save_data(data)

def get_total_duties(name: str) -> int:
    counts = load_data().get("duty_counts", {}).get(name, {})
    return counts.get("svodki", 0) + counts.get("proc", 0)

# ── Плейлисты / очередь / артисты ─────────────────────────────────────────────
def get_playlists(user_id) -> dict:
    return load_data().get("playlists", {}).get(str(user_id), {})

def save_playlist_track(user_id, pl_name: str, track: dict) -> bool:
    data = load_data()
    tracks = data.setdefault("playlists", {}).setdefault(str(user_id), {}).setdefault(pl_name, [])
    if not any(t["id"] == track["id"] for t in tracks):
        tracks.append(track); save_data(data); return True
    return False

def delete_playlist(user_id, pl_name: str):
    data = load_data()
    data.get("playlists", {}).get(str(user_id), {}).pop(pl_name, None)
    save_data(data)

def remove_track_from_playlist(user_id, pl_name: str, track_id: str):
    data = load_data()
    pl = data.get("playlists", {}).get(str(user_id), {})
    if pl_name in pl:
        pl[pl_name] = [t for t in pl[pl_name] if str(t["id"]) != str(track_id)]
        save_data(data)

def queue_push(user_id, track: dict):
    data = load_data()
    q = data.setdefault("queues", {}).setdefault(str(user_id), [])
    if not any(t["id"] == track["id"] for t in q):
        q.append(track); save_data(data)

def queue_pop(user_id) -> dict | None:
    data = load_data()
    q = data.setdefault("queues", {}).setdefault(str(user_id), [])
    if not q: return None
    track = q.pop(0); save_data(data); return track

def queue_list(user_id) -> list:
    return load_data().get("queues", {}).get(str(user_id), [])

def queue_clear(user_id):
    data = load_data()
    data.setdefault("queues", {})[str(user_id)] = []
    save_data(data)

def get_artist_subs(user_id) -> list:
    return load_data().get("artist_subs", {}).get(str(user_id), [])

def add_artist_sub(user_id, artist: str) -> bool:
    data = load_data()
    subs = data.setdefault("artist_subs", {}).setdefault(str(user_id), [])
    if artist not in subs:
        subs.append(artist); save_data(data); return True
    return False

def remove_artist_sub(user_id, artist: str):
    data = load_data()
    subs = data.setdefault("artist_subs", {}).setdefault(str(user_id), [])
    if artist in subs:
        subs.remove(artist); save_data(data)

# ── Mood / misc helpers ───────────────────────────────────────────────────────
def get_explicit_tag(title: str) -> str:
    bad = ["fuck", "bitch", "shit", "nigga", "hoe", "explicit", "18+"]
    return " 🔞[E]" if any(w in title.lower() for w in bad) else ""

def get_daily_mood() -> str:
    data = load_data()
    today = date.today().isoformat()
    dm = data.get("daily_mood", {})
    if dm.get("date") != today:
        mood = random.choice(list(MOODS.keys()))
        data["daily_mood"] = {"date": today, "mood": mood}
        save_data(data); return mood
    return dm.get("mood", "ironic")

def get_mood_label() -> str:
    return MOODS[get_daily_mood()]["label"]

def get_ending() -> str:
    mood = get_daily_mood()
    return random.choice(MOODS[mood]["endings"] + GAMING_ENDINGS)

# ── Duty schedule ─────────────────────────────────────────────────────────────
def get_svodki_idx(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return (data["svodki_start_index"] + (target_date - start).days) % len(SVODKI_LIST)

def get_svodki_person(target_date: date) -> str:
    return SVODKI_LIST[get_svodki_idx(target_date)]

def get_procedura_idx(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return (data["procedura_start_index"] + (target_date - start).days // 2) % len(PROCEDURA_LIST)

def get_procedura_person(target_date: date) -> str:
    return PROCEDURA_LIST[get_procedura_idx(target_date)]

def get_duty_day_number(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return ((target_date - start).days % 2) + 1

def days_until_svodki(name: str, from_date: date) -> int:
    for i in range(1, len(SVODKI_LIST) + 1):
        if get_svodki_person(from_date + timedelta(days=i)) == name: return i
    return len(SVODKI_LIST)

def days_until_procedura(name: str, from_date: date) -> int:
    for i in range(1, len(PROCEDURA_LIST) * 2 + 1):
        if get_procedura_person(from_date + timedelta(days=i)) == name: return i
    return len(PROCEDURA_LIST) * 2

def next_person_svodki(name: str) -> str:
    return SVODKI_LIST[(SVODKI_LIST.index(name) + 1) % len(SVODKI_LIST)]

def next_person_proc(name: str) -> str:
    return PROCEDURA_LIST[(PROCEDURA_LIST.index(name) + 1) % len(PROCEDURA_LIST)]

def svodki_num_label(name: str) -> str:
    return f"Дежурный №{SVODKI_LIST.index(name)+1} по сводкам"

def proc_num_label(name: str) -> str:
    return f"Дежурный №{PROCEDURA_LIST.index(name)+1} по процедурке"

def weekday_divider(d: date) -> str:
    _, _, sym_l, sym_r = WEEKDAY_STYLE[d.weekday()]
    return f"{sym_l}━━━━━━━━━━━━━━━━{sym_r}"

def person_tag(name: str) -> str:
    return f"{PERSON_EMOJI.get(name,'👤')} *{name}*"

def progress_bar(current: int, total: int) -> str:
    filled = round((current / total) * 8)
    return f"`[{'▓'*filled}{'░'*(8-filled)}]` {current}/{total}"

# ── Message builders ──────────────────────────────────────────────────────────
def build_duty_message(target_date: date, label: str) -> str:
    svodki   = get_svodki_person(target_date)
    proc     = get_procedura_person(target_date)
    proc_day = get_duty_day_number(target_date)
    div      = weekday_divider(target_date)
    day_name, icon, _, _ = WEEKDAY_STYLE[target_date.weekday()]
    date_str = target_date.strftime("%d.%m.%Y")
    sv_em = PERSON_EMOJI.get(svodki, "👤")
    pr_em = PERSON_EMOJI.get(proc, "👤")
    bar = progress_bar(proc_day, 2)
    sv_next = next_person_svodki(svodki)
    pr_next = next_person_proc(proc)
    sv_days = days_until_svodki(svodki, target_date)
    pr_days = days_until_procedura(proc, target_date)

    # Врачи
    month = target_date.month
    day   = target_date.day
    docs  = JULY_DOCTORS.get(day) if month == 7 else AUGUST_DOCTORS.get(day) if month == 8 else None
    doc_line = f"\n\n🏥 *Врачи на {date_str}:*\n" + "\n".join(f"  • {d}" for d in docs) if docs else ""

    return (
        f"📋 *Наряд {label}* — {icon} {day_name}, {date_str}\n"
        f"{div}\n\n"
        f"🤷‍♀️ *Дежурный №{SVODKI_LIST.index(svodki)+1} по сводкам:*\n"
        f"  {sv_em} *{svodki}*\n"
        f"  📊 нарядов всего: {get_total_duties(svodki)}\n"
        f"  ⏭ следующий: {PERSON_EMOJI.get(sv_next,'👤')} {sv_next} через {sv_days} д.\n\n"
        f"⚡️ *Дежурный №{PROCEDURA_LIST.index(proc)+1} по процедурке:*\n"
        f"  {pr_em} *{proc}*\n"
        f"  {bar} день {proc_day}/2\n"
        f"  📊 нарядов всего: {get_total_duties(proc)}\n"
        f"  ⏭ следующий: {PERSON_EMOJI.get(pr_next,'👤')} {pr_next} через {pr_days} д.\n"
        f"{div}{doc_line}"
    )

def build_week_schedule() -> str:
    today  = date.today()
    lines  = [f"📊 *Расписание на неделю* (с {today.strftime('%d.%m')}):\n"]
    for i in range(7):
        d = today + timedelta(days=i)
        sv = get_svodki_person(d)
        pr = get_procedura_person(d)
        pday = get_duty_day_number(d)
        day_name, icon, _, _ = WEEKDAY_STYLE[d.weekday()]
        sv_em = PERSON_EMOJI.get(sv, "👤")
        pr_em = PERSON_EMOJI.get(pr, "👤")
        lines.append(
            f"{icon} *{d.strftime('%d.%m')} {day_name}*\n"
            f"  🤷‍♀️ {sv_em} {sv}  ⚡️ {pr_em} {pr} (д.{pday}/2)\n"
        )
    return "\n".join(lines)

def build_log_message() -> str:
    data = load_data()
    log  = data.get("log", {})
    if not log: return "📓 *Журнал пуст.* Пока никто ничего не выполнил."
    lines = ["📓 *Журнал выполнения:*\n"]
    for d_str in sorted(log.keys(), reverse=True)[:14]:
        entry = log[d_str]
        sv = entry.get("svodki", "—")
        pr = entry.get("proc", "—")
        sv_em = PERSON_EMOJI.get(sv, "👤") if sv != "—" else ""
        pr_em = PERSON_EMOJI.get(pr, "👤") if pr != "—" else ""
        lines.append(f"📅 *{d_str}*: 🤷‍♀️ {sv_em}{sv}  ⚡️ {pr_em}{pr}")
    return "\n".join(lines)

def build_monday_briefing() -> str:
    today   = date.today()
    lines   = [f"{random.choice(MONDAY_INTROS)}\n"]
    for i in range(7):
        d = today + timedelta(days=i)
        sv = get_svodki_person(d)
        pr = get_procedura_person(d)
        day_name, icon, _, _ = WEEKDAY_STYLE[d.weekday()]
        lines.append(f"{icon} {day_name} {d.strftime('%d.%m')}: {PERSON_EMOJI.get(sv,'👤')}{sv} / {PERSON_EMOJI.get(pr,'👤')}{pr}")
    return "\n".join(lines)

def build_sunday_summary() -> str:
    today = date.today()
    data  = load_data()
    log   = data.get("log", {})
    lines = [f"{random.choice(SUNDAY_INTROS)}\n"]
    counts: dict[str, int] = {}
    for i in range(7):
        d = (today - timedelta(days=6-i)).isoformat()
        e = log.get(d, {})
        for v in e.values():
            counts[v] = counts.get(v, 0) + 1
    if counts:
        sorted_c = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        for name, cnt in sorted_c:
            em = PERSON_EMOJI.get(name, "👤")
            lines.append(f"{em} *{name}* — {cnt} наряд(ов)")
    else:
        lines.append("_никто ничего не подтвердил. классика._")
    return "\n".join(lines)

def build_pinned_content() -> str:
    today    = date.today()
    svodki   = get_svodki_person(today)
    proc     = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    sv_em    = PERSON_EMOJI.get(svodki, "👤")
    pr_em    = PERSON_EMOJI.get(proc, "👤")
    div      = weekday_divider(today)
    day_name, icon, _, _ = WEEKDAY_STYLE[today.weekday()]
    date_str = today.strftime("%d.%m.%Y")
    mood_lbl = get_mood_label()
    filled   = round((proc_day / 2) * 8)
    bar      = "▓" * filled + "░" * (8 - filled)
    return (
        f"📌 *АКТУАЛЬНЫЙ НАРЯД*\n{icon} {day_name}, {date_str}\n"
        f"_настроение бота: {mood_lbl}_\n{div}\n\n"
        f"🤷‍♀️ *Дежурный №{SVODKI_LIST.index(svodki)+1} по сводкам:*\n  {sv_em} *{svodki}*\n\n"
        f"⚡️ *Дежурный №{PROCEDURA_LIST.index(proc)+1} по процедурке:*\n  {pr_em} *{proc}*\n  `[{bar}]` день {proc_day}/2\n\n"
        f"{div}\n_обновляется автоматически каждый день_"
    )

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Наряд сегодня"), KeyboardButton(text="📅 Наряд завтра")],
        [KeyboardButton(text="📊 Расписание на неделю"), KeyboardButton(text="📓 Журнал")],
        [KeyboardButton(text="🔔 Напомнить сейчас"), KeyboardButton(text="⚙️ Настройки")],
    ], resize_keyboard=True)

def confirmation_keyboard(duty_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнено!", callback_data=f"done:{duty_type}"),
        InlineKeyboardButton(text="⏰ Напомни позже", callback_data=f"retry:{duty_type}"),
    ]])

def settings_keyboard(data: dict) -> InlineKeyboardMarkup:
    rem_icon = "✅" if data.get("reminders_enabled", True) else "❌"
    ai_icon  = "✅" if data.get("ai_random_replies_enabled", True) else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{rem_icon} Напоминания", callback_data="toggle_reminders"),
         InlineKeyboardButton(text=f"{ai_icon} ИИ-ответы", callback_data="toggle_ai_replies")],
        [InlineKeyboardButton(text="📍 Установить чат", callback_data="set_chat"),
         InlineKeyboardButton(text="📌 Обновить закреп", callback_data="update_pinned")],
        [InlineKeyboardButton(text="🤷‍♀️ Кто по сводкам?", callback_data="pick_svodki"),
         InlineKeyboardButton(text="⚡️ Кто по процедурке?", callback_data="pick_procedura")],
        [InlineKeyboardButton(text="👥 Список сводки", callback_data="show_svodki_list"),
         InlineKeyboardButton(text="👥 Список процедурки", callback_data="show_procedura_list")],
        [InlineKeyboardButton(text="🪪 Зарегистрировать ID", callback_data="register_personal")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
    ])

def svodki_pick_keyboard(actual_idx: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, name in enumerate(SVODKI_LIST):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️" if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{em} {name}{mark}", callback_data=f"set_svodki_idx:{i}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def procedura_pick_keyboard(actual_idx: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, name in enumerate(PROCEDURA_LIST):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️" if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{em} {name}{mark}", callback_data=f"set_proc_idx:{i}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ── Helpers ───────────────────────────────────────────────────────────────────
async def schedule_delete(bot, chat_id, msg_id, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try: await bot.delete_message(chat_id, msg_id)
    except: pass

def auto_delete_later(bot, chat_id, msg_id, seconds=30):
    asyncio.create_task(schedule_delete(bot, chat_id, msg_id, seconds))

async def update_pinned_message(bot):
    data    = load_data()
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    content       = build_pinned_content()
    pinned_msg_id = data.get("pinned_msg_id")
    if pinned_msg_id:
        try:
            await bot.edit_message_text(content, chat_id=chat_id, message_id=pinned_msg_id, parse_mode="Markdown")
            return
        except Exception as e:
            logger.warning(f"не удалось обновить закреп: {e}")
    try:
        sent = await bot.send_message(chat_id, content, parse_mode="Markdown")
        try: await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except: pass
        data["pinned_msg_id"] = sent.message_id
        save_data(data)
    except Exception as e:
        logger.error(f"ошибка создания закрепа: {e}")

async def get_ai_header(person: str, duty_type: str):
    try:
        from google import genai
        key = os.getenv("GEMINI_API_KEY")
        if not key: return None
        client   = genai.Client(api_key=key)
        duty_name = "сводки" if duty_type == "svodki" else "процедурку"
        prompt = (
            f"Напиши ОДНУ короткую, смешную и дерзкую фразу (максимум 7-8 слов), "
            f"чтобы напомнить дежурному по имени {person} выполнить наряд: {duty_name}. "
            "Начни с эмодзи. Без кавычек. Можно отсылки к CS2, Dota 2 или Genshin Impact."
        )
        response = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text.strip() if response.text else None
    except Exception as e:
        logger.error(f"Ошибка Gemini header: {e}")
        return None

async def get_weather_advice_for_reminder(city: str = "Одинцово") -> str | None:
    try:
        from google import genai
        from google.genai import types as gt
        key = os.getenv("GEMINI_API_KEY")
        if not key: return None
        client = genai.Client(api_key=key)
        prompt = (
            f"Узнай актуальную погоду в {city}. Напиши 1-2 предложения с саркастичным советом "
            "как одеться дежурному. Стиль: геймерский, немного токсичный."
        )
        r = await client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
            config=gt.GenerateContentConfig(tools=[{"google_search": {}}])
        )
        return r.text.strip()
    except Exception as e:
        logger.error(f"Ошибка погоды в reminder: {e}")
        return None

# ── Reminders ─────────────────────────────────────────────────────────────────
async def _send_personal(bot, person: str, duty_type: str):
    data = load_data()
    uid  = data.get("personal_ids", {}).get(person)
    if not uid: return
    pool = (PERSONAL_SVODKI + PERSONAL_SVODKI_GAMING) if duty_type == "svodki" else (PERSONAL_PROC + PERSONAL_PROC_GAMING)
    try: await bot.send_message(uid, random.choice(pool).format(name=person))
    except Exception as e: logger.warning(f"личное сообщение {person}: {e}")

async def _send_reminder(bot, duty_type: str, retry: bool = False):
    data = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        logger.warning("CHAT_ID не настроен!"); return

    today = datetime.now(TIMEZONE).date()
    wd    = today.weekday()
    easter = random.random() < 0.05 and not retry

    if duty_type == "svodki":
        person         = get_svodki_person(today)
        next_d         = days_until_svodki(person, today)
        next_n         = next_person_svodki(person)
        total          = get_total_duties(person)
        position_label = svodki_num_label(person)
        time_label     = "18:00"
        proc_day       = 1
        headers_pool   = SVODKI_HEADERS_BY_DAY[wd] + GAMING_HEADERS_SVODKI
        prev_key       = "last_svodki_msg_id"
    else:
        person         = get_procedura_person(today)
        proc_day       = get_duty_day_number(today)
        next_d         = days_until_procedura(person, today)
        next_n         = next_person_proc(person)
        total          = get_total_duties(person)
        position_label = proc_num_label(person)
        time_label     = "22:00"
        headers_pool   = PROC_HEADERS_BY_DAY[wd] + GAMING_HEADERS_PROC
        prev_key       = "last_proc_msg_id"

    retry_pool = RETRY_PREFIX + RETRY_PREFIX_GAMING
    header = await get_ai_header(person, duty_type) if not retry else None
    if not header:
        header = random.choice(retry_pool) if retry else random.choice(headers_pool)
    ending    = get_ending()
    mood_label = get_mood_label()

    if easter:
        msg_text = random.choice(EASTER_EGGS)(person, duty_type)
        prev_id  = data.get(prev_key)
        if prev_id:
            try: await bot.delete_message(chat_id, prev_id)
            except: pass
        sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown",
                                      reply_markup=confirmation_keyboard(duty_type))
        data[prev_key] = sent.message_id
        save_data(data)
        await update_pinned_message(bot)
        await _send_personal(bot, person, duty_type)
        if duty_type == "svodki" and not retry:
            wt = await get_weather_advice_for_reminder()
            if wt: await bot.send_message(chat_id, f"☁️ *Обстановка на улице:*\n{wt}", parse_mode="Markdown")
        return

    date_label = today.strftime("%d.%m.%Y") + " • " + WEEKDAY_STYLE[wd][0]
    theme_key  = random.choice(THEME_KEYS)
    img_path   = os.path.join(TEMP_DIR, f"card_{duty_type}_{int(time.time())}.jpg")

    caption  = f"🔔 Напоминание по {duty_type.upper()} для *{person}*"
    prev_id  = data.get(prev_key)
    if prev_id:
        try: await bot.delete_message(chat_id, prev_id)
        except: pass

    if make_reminder_card:
        try:
            make_reminder_card(
                duty_type=duty_type, person=person, position_label=position_label,
                time_label=time_label, header_text=header, ending_text=ending,
                date_label=date_label, mood_label=mood_label,
                proc_day=proc_day if duty_type == "proc" else 1,
                next_person=next_n, next_days=next_d, total_duties=total,
                theme_key=theme_key, output_path=img_path
            )
            photo = FSInputFile(img_path)
            sent  = await bot.send_photo(chat_id, photo=photo, caption=caption,
                                         parse_mode="Markdown", reply_markup=confirmation_keyboard(duty_type))
            data[prev_key] = sent.message_id
            save_data(data)
            try: os.remove(img_path)
            except: pass
            await update_pinned_message(bot)
            await _send_personal(bot, person, duty_type)
            if duty_type == "svodki" and not retry:
                wt = await get_weather_advice_for_reminder()
                if wt: await bot.send_message(chat_id, f"☁️ *Обстановка на улице:*\n{wt}", parse_mode="Markdown")
            return
        except Exception as e:
            logger.error(f"Ошибка карточки: {e}")

    # Текстовый fallback
    msg_text = (
        f"*{header}*\n\n👤 {person}\n📋 {position_label}\n"
        f"⏰ {time_label}\n📊 нарядов всего: {total}\n"
        f"⏭ следующий: {next_n} через {next_d} д.\n\n_{ending}_"
    )
    sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown",
                                  reply_markup=confirmation_keyboard(duty_type))
    data[prev_key] = sent.message_id
    save_data(data)
    await update_pinned_message(bot)
    await _send_personal(bot, person, duty_type)

async def send_svodki_reminder(bot):   await _send_reminder(bot, "svodki")
async def send_procedura_reminder(bot): await _send_reminder(bot, "proc")

async def send_retry_reminder(bot, duty_type: str):
    data    = load_data()
    pending = data.get("pending_retry", {})
    today   = date.today().isoformat()
    if pending.get(duty_type) == today:
        await _send_reminder(bot, duty_type, retry=True)
        pending.pop(duty_type, None)
        data["pending_retry"] = pending
        save_data(data)

async def send_monday_briefing(bot):
    data = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try:
        await bot.send_message(chat_id, build_monday_briefing(), parse_mode="Markdown")
        await update_pinned_message(bot)
    except Exception as e: logger.error(f"понедельничная рассылка: {e}")

async def send_sunday_summary(bot):
    data = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try: await bot.send_message(chat_id, build_sunday_summary(), parse_mode="Markdown")
    except Exception as e: logger.error(f"воскресный итог: {e}")

async def daily_pinned_update(bot): await update_pinned_message(bot)

# ── Selftest ──────────────────────────────────────────────────────────────────
def _startup_selftest():
    if not make_reminder_card: return
    test_path = os.path.join(TEMP_DIR, "_selftest.jpg")
    try:
        make_reminder_card(
            duty_type="svodki", person="Тест", position_label="самопроверка",
            time_label="00:00", header_text="проверка", ending_text="всё ок",
            date_label=datetime.now(TIMEZONE).strftime("%d.%m.%Y"), mood_label="проверка",
            theme_key=random.choice(THEME_KEYS), output_path=test_path,
        )
        size = os.path.getsize(test_path)
        logger.info(f"✅ САМОПРОВЕРКА КАРТИНОК ПРОЙДЕНА: {test_path} ({size} байт)")
    except Exception:
        logger.error("❌ САМОПРОВЕРКА КАРТИНОК НЕ ПРОЙДЕНА:\n" + traceback.format_exc())
    finally:
        try: os.remove(test_path)
        except: pass

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(message: types.Message):
    args = (message.text or "").split()
    if len(args) > 1 and args[1].startswith("track_"):
        track_id = args[1].replace("track_", "")
        auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
        from music_handlers import send_track_to_user
        await send_track_to_user(message, track_id)
        return
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    start_text = (
        "👋 *Йоу. Я здесь. Слушаю.*\n\n"
        "📋 *Наряды* — слежу за сводками и процедуркой. 18:00 и 22:00, каждый день.\n\n"
        "🤖 *ИИ-чат* — реплай на моё сообщение, отвечу на всё.\n\n"
        "🎵 *Музыка:*\n"
        "`/find <название>` — поиск треков\n"
        "`/charts` — топ\n`/music` — дашборд\n`/dj` — DJ-голосование\n\n"
        "📸 *Instagram:* `/ig <username>`\n\n"
        "📥 *Медиа:* ссылка YouTube/TikTok/X — скачаю\n\n"
        "🎬 *Аниме:* `/anime <запрос>` или `@bot a:<запрос>`\n\n"
        "🛠 *Прочее:*\n"
        "`/tldr` `/pogoda` `/meme` `/poll` `/imagen` `/ozon` `/chatid`\n\n"
        "Всё вижу. Всего не прощаю. Погнали 👇"
    )
    sent = await message.answer(start_text, parse_mode="Markdown", reply_markup=main_keyboard())
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 60)

async def cmd_getchatid(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    await message.answer(f"🆔 ID этого чата: `{message.chat.id}`", parse_mode="Markdown")

async def cmd_today(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(build_duty_message(date.today(), "сегодня"), parse_mode="Markdown")
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)

async def cmd_tomorrow(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(build_duty_message(date.today() + timedelta(days=1), "завтра"), parse_mode="Markdown")
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)

async def cmd_week(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(build_week_schedule(), parse_mode="Markdown")
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)

async def cmd_log(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(build_log_message(), parse_mode="Markdown")
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 120)

async def cmd_remind_now(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer("🔔 *погнали, отправляю в чат...*", parse_mode="Markdown")
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
    await _send_reminder(message.bot, "svodki")
    await _send_reminder(message.bot, "proc")

async def cmd_settings(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    data      = load_data()
    enabled   = "✅ включены" if data.get("reminders_enabled", True) else "❌ выключены"
    ai_en     = "✅ включены" if data.get("ai_random_replies_enabled", True) else "❌ выключены"
    chat_txt  = data.get("chat_id", "не настроен")
    sv_now    = get_svodki_person(date.today())
    pr_now    = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em = PERSON_EMOJI.get(sv_now, "👤")
    pr_em = PERSON_EMOJI.get(pr_now, "👤")
    text = (
        f"⚙️ *настройки*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 чат: `{chat_txt}`\n🔔 напоминания: {enabled}\n"
        f"🤖 случайные ответы ИИ: {ai_en}\n"
        f"🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n"
        f"⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n"
        f"🪪 зарегистрировано: *{reg_count}* чел.\n"
        f"🎭 настроение бота: *{get_mood_label()}*\n━━━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=settings_keyboard(data))

# ── Dembel ────────────────────────────────────────────────────────────────────
def _dembel_list_keyboard() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for person in DEMBEL_DATES:
        row.append(InlineKeyboardButton(text=f"🎖 {person}", callback_data=f"dembel_card:{person}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def cmd_dembel(message: types.Message):
    if not get_all_timers:
        await message.answer("❌ Дембель-таймер недоступен."); return
    raw = (message.text or "").replace("/dembel", "").strip()
    if raw:
        match = next((p for p in DEMBEL_DATES if p.lower() == raw.lower()), None)
        if not match:
            await message.answer(f"❌ Не нашёл «{raw}».\nЕсть: {', '.join(DEMBEL_DATES)}"); return
        await _send_dembel_card(message, match); return
    timers = get_all_timers()
    lines  = ["🎖 *ДЕМБЕЛЬ-ТАЙМЕР*\n"]
    for t in timers:
        if t["done"]: lines.append(f"🏠 *{t['person']}* — дома!")
        else: lines.append(f"⏳ *{t['person']}* — {t['days']} дн. {t['hours']} ч. (дембель {t['target'].strftime('%d.%m.%Y')})")
    lines.append("\n_Жми на имя — пришлю карточку_")
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=_dembel_list_keyboard())

async def _send_dembel_card(source, person: str):
    chat_id = source.chat.id
    bot     = source.bot
    status  = await bot.send_message(chat_id, f"🎨 Рисую карточку для *{person}*...", parse_mode="Markdown")
    _map = str.maketrans({"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"})
    translit = person.lower().translate(_map)
    safe_slug = "".join(c for c in translit if c.isalnum()) or "person"
    img_path  = os.path.join(TEMP_DIR, f"dembel_{safe_slug}_{int(time.time())}.jpg")
    try:
        if make_dembel_card:
            make_dembel_card(person, output_path=img_path, theme_key=random.choice(THEME_KEYS))
        else:
            raise RuntimeError("make_dembel_card недоступен")
    except Exception as e:
        logger.error(f"Ошибка дембель-карточки {person}: {e}")
        try: await status.delete()
        except: pass
        timers = {t["person"]: t for t in get_all_timers()}
        t = timers.get(person, {})
        text = f"🏠 *{person}* — дома!" if t.get("done") else f"⏳ *{person}* — {t.get('days','?')} дн. {t.get('hours','?')} ч."
        await bot.send_message(chat_id, text, parse_mode="Markdown")
        return
    try: await status.delete()
    except: pass
    try:
        photo = FSInputFile(img_path)
        await bot.send_photo(chat_id, photo=photo, caption=f"🎖 Дембель-таймер · *{person}*",
                             parse_mode="Markdown", reply_markup=_dembel_list_keyboard())
    except Exception as e:
        logger.error(f"Ошибка отправки дембель-карточки: {e}")
        await bot.send_message(chat_id, f"❌ Не получилось отправить карточку для {person}.")
    finally:
        try: os.remove(img_path)
        except: pass

async def callback_dembel_card(callback: types.CallbackQuery):
    person = callback.data.split(":", 1)[1]
    await callback.answer()
    await _send_dembel_card(callback.message, person)

# ── Settings callbacks ─────────────────────────────────────────────────────────
async def callback_toggle_reminders(callback: types.CallbackQuery):
    data = load_data()
    data["reminders_enabled"] = not data.get("reminders_enabled", True)
    save_data(data)
    status = "✅ включены" if data["reminders_enabled"] else "❌ выключены"
    await callback.answer(f"напоминания {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(data))

async def callback_toggle_ai_replies(callback: types.CallbackQuery):
    data = load_data()
    data["ai_random_replies_enabled"] = not data.get("ai_random_replies_enabled", True)
    save_data(data)
    status = "✅ ВКЛЮЧЕНЫ" if data["ai_random_replies_enabled"] else "❌ ВЫКЛЮЧЕНЫ"
    await callback.answer(f"Случайные ответы ИИ {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(data))

async def callback_set_chat(callback: types.CallbackQuery, state: FSMContext):
    sent = await callback.message.answer("📍 *напиши id чата* куда кидать напоминания:", parse_mode="Markdown")
    auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await state.set_state(AdminStates.waiting_chat_id)
    await callback.answer()

async def process_chat_id(message: types.Message, state: FSMContext):
    chat_id = message.text.strip()
    data = load_data(); data["chat_id"] = chat_id; save_data(data)
    await state.clear()
    sent = await message.answer(f"✅ *чат установлен!*\nid: `{chat_id}`", parse_mode="Markdown", reply_markup=main_keyboard())
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 30)

async def callback_update_pinned(callback: types.CallbackQuery):
    await callback.answer("📌 обновляю...")
    await update_pinned_message(callback.bot)
    await callback.answer("✅ закреплённое обновлено!", show_alert=True)

async def callback_register_personal(callback: types.CallbackQuery, state: FSMContext):
    names = " / ".join(SVODKI_LIST)
    sent  = await callback.message.answer(
        f"🪪 *регистрация личных уведомлений*\n\nнапиши своё имя точно как в списке:\n{names}",
        parse_mode="Markdown"
    )
    auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await state.set_state(AdminStates.waiting_register)
    await callback.answer()

async def process_register(message: types.Message, state: FSMContext):
    name      = message.text.strip()
    all_names = list(set(SVODKI_LIST + PROCEDURA_LIST))
    if name not in all_names:
        sent = await message.answer(f"❌ *{name}* не найдено. Проверь написание.", parse_mode="Markdown")
        auto_delete_later(message.bot, message.chat.id, sent.message_id, 20)
        return
    data = load_data()
    data.setdefault("personal_ids", {})[name] = message.from_user.id
    save_data(data)
    em = PERSON_EMOJI.get(name, "👤")
    await state.clear()
    sent = await message.answer(f"✅ {em} *{name}* зарегистрирован!", parse_mode="Markdown", reply_markup=main_keyboard())
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 30)

async def callback_pick_svodki(callback: types.CallbackQuery):
    data = load_data(); today = date.today()
    days_passed = (today - date.fromisoformat(data["start_date"])).days
    actual_idx  = (data["svodki_start_index"] + days_passed) % len(SVODKI_LIST)
    await callback.message.edit_text(
        f"🤷‍♀️ *кто сегодня по сводкам?*\nсейчас: {PERSON_EMOJI.get(SVODKI_LIST[actual_idx],'👤')} *{SVODKI_LIST[actual_idx]}*\n\nнажми на нужное имя:",
        parse_mode="Markdown", reply_markup=svodki_pick_keyboard(actual_idx)
    )
    await callback.answer()

async def callback_pick_procedura(callback: types.CallbackQuery):
    data = load_data(); today = date.today()
    days_passed = (today - date.fromisoformat(data["start_date"])).days
    actual_idx  = (data["procedura_start_index"] + days_passed // 2) % len(PROCEDURA_LIST)
    await callback.message.edit_text(
        f"⚡️ *кто сегодня по процедурке?*\nсейчас: {PERSON_EMOJI.get(PROCEDURA_LIST[actual_idx],'👤')} *{PROCEDURA_LIST[actual_idx]}*\n\nнажми на нужное имя:",
        parse_mode="Markdown", reply_markup=procedura_pick_keyboard(actual_idx)
    )
    await callback.answer()

async def callback_set_svodki_idx(callback: types.CallbackQuery):
    chosen_idx  = int(callback.data.split(":")[1])
    chosen_name = SVODKI_LIST[chosen_idx]
    data = load_data()
    days_passed = (date.today() - date.fromisoformat(data["start_date"])).days
    data["svodki_start_index"] = (chosen_idx - days_passed) % len(SVODKI_LIST)
    save_data(data)
    await callback.answer(f"✅ сводки сегодня: {PERSON_EMOJI.get(chosen_name,'👤')} {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=svodki_pick_keyboard(chosen_idx))
    await update_pinned_message(callback.bot)

async def callback_set_proc_idx(callback: types.CallbackQuery):
    chosen_idx  = int(callback.data.split(":")[1])
    chosen_name = PROCEDURA_LIST[chosen_idx]
    data = load_data()
    days_passed = (date.today() - date.fromisoformat(data["start_date"])).days
    data["procedura_start_index"] = (chosen_idx - days_passed // 2) % len(PROCEDURA_LIST)
    save_data(data)
    await callback.answer(f"✅ процедурка сегодня: {PERSON_EMOJI.get(chosen_name,'👤')} {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=procedura_pick_keyboard(chosen_idx))
    await update_pinned_message(callback.bot)

async def callback_show_svodki_list(callback: types.CallbackQuery):
    today   = date.today(); current = get_svodki_person(today)
    lines   = [f"*👥 наряд по сводкам:*\nсейчас: {PERSON_EMOJI.get(current,'👤')} *{current}*\n"]
    for i, name in enumerate(SVODKI_LIST, 1):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {em} {name}{mark}")
    sent = await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await callback.answer()

async def callback_show_procedura_list(callback: types.CallbackQuery):
    today    = date.today(); current = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    lines    = [f"*👥 наряд по процедурке:*\nсейчас: {PERSON_EMOJI.get(current,'👤')} *{current}* (день {proc_day}/2)\n"]
    for i, name in enumerate(PROCEDURA_LIST, 1):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {em} {name} *(2 дня)*{mark}")
    sent = await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await callback.answer()

async def callback_back_settings(callback: types.CallbackQuery):
    data = load_data()
    enabled  = "✅ включены" if data.get("reminders_enabled", True) else "❌ выключены"
    ai_en    = "✅ включены" if data.get("ai_random_replies_enabled", True) else "❌ выключены"
    chat_txt = data.get("chat_id", "не настроен")
    sv_now   = get_svodki_person(date.today()); pr_now = get_procedura_person(date.today())
    text = (
        f"⚙️ *настройки*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 чат: `{chat_txt}`\n🔔 напоминания: {enabled}\n🤖 ИИ-ответы: {ai_en}\n"
        f"🤷‍♀️ сводки: {PERSON_EMOJI.get(sv_now,'👤')} *{sv_now}*\n"
        f"⚡️ процедурка: {PERSON_EMOJI.get(pr_now,'👤')} *{pr_now}*\n"
        f"🪪 зарегистрировано: *{len(data.get('personal_ids',{}))}* чел.\n"
        f"🎭 настроение: *{get_mood_label()}*\n━━━━━━━━━━━━━━━━━━━━"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=settings_keyboard(data))
    await callback.answer()

async def callback_back_main(callback: types.CallbackQuery):
    await callback.message.answer("🏠 главное меню", reply_markup=main_keyboard())
    await callback.answer()

async def callback_done(callback: types.CallbackQuery):
    duty_type = callback.data.split(":")[1]
    today = date.today(); data = load_data()
    entry  = data.setdefault("log", {}).setdefault(today.isoformat(), {})
    person = get_svodki_person(today) if duty_type == "svodki" else get_procedura_person(today)
    field  = "svodki" if duty_type == "svodki" else "proc"
    entry[field] = person
    data.get("pending_retry", {}).pop(duty_type, None)
    save_data(data)
    increment_count(person, field)
    await callback.answer(random.choice(DONE_REPLIES + DONE_REPLIES_GAMING), show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)
    await update_pinned_message(callback.bot)

async def callback_retry(callback: types.CallbackQuery):
    duty_type = callback.data.split(":")[1]
    data = load_data()
    data.setdefault("pending_retry", {})[duty_type] = date.today().isoformat()
    save_data(data)
    await callback.answer("⏰ окей, напомню через 15 минут", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)
