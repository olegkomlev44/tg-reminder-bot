"""
duty_handlers.py — всё что связано с нарядами, расписанием, настройками,
клавиатурами, планировщиком и cmd_start/cmd_getchatid.

Обновления v3.0 (пункты 1, 2, 3, 4, 6, 9 + врачи на октябрь 2026):
  - Подключено расписание врачей на октябрь 2026 (OCTOBER_DOCTORS)
  - Пункт 1: Передача дежурной бригады врачей напрямую на карточку сводок (make_reminder_card)
  - Пункт 2: Разделение контекста Сводок (маршрут + врачи + погода) и Процедурки (этап смены 1/2 или 2/2)
  - Пункт 3: Расчёт цепочки очереди из 3 следующих дежурных (get_queue_chain) для карточки и текста
  - Пункт 4: Интеграция личного прогресса ДМБ дежурного и подсчёт оставшихся нарядов до дома
  - Пункт 6: Единая информативная подпись (caption) к карточке наряда с живым тегом, очередью, ДМБ, врачами и погодой без спама вторым сообщением
  - Пункт 9: Контекстная генерация ИИ-заголовков (get_ai_header) с учётом дня недели, этапа смены, фамилии отв. врача, погоды и дней до ДМБ
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
    InputMediaPhoto, KeyboardButton, ReplyKeyboardMarkup,
)

from config import (
    BASE_DIR, CHAT_ID, DATA_FILE, MOODS, PERSON_EMOJI, PROCEDURA_LIST,
    SVODKI_LIST, TEMP_DIR, TIMEZONE, WEEKDAY_STYLE,
    SVODKI_HEADERS_BY_DAY, PROC_HEADERS_BY_DAY,
    GAMING_HEADERS_SVODKI, GAMING_HEADERS_PROC, GAMING_ENDINGS,
    DONE_REPLIES, DONE_REPLIES_GAMING, RETRY_PREFIX, RETRY_PREFIX_GAMING,
    PERSONAL_SVODKI, PERSONAL_SVODKI_GAMING, PERSONAL_PROC, PERSONAL_PROC_GAMING,
    MONDAY_INTROS, SUNDAY_INTROS,
    JULY_DOCTORS, AUGUST_DOCTORS, SEPTEMBER_DOCTORS, OCTOBER_DOCTORS,
    EASTER_EGGS,
)

logger = logging.getLogger(__name__)

# Импортируем генераторы карточек
try:
    from card_generator import make_reminder_card, THEME_KEYS
    logger.info(f"🟢 card_generator v3.0 импортирован, тем: {len(THEME_KEYS)}")
except Exception:
    logger.error("🔴 card_generator недоступен:\n" + traceback.format_exc())
    make_reminder_card = None
    THEME_KEYS = ["default"]

try:
    from dembel_generator import (
        make_dembel_card,
        make_dembel_summary_card,
        get_all_timers,
        get_today_milestones,
        get_next_milestone,
        format_dembel_caption,
        DEMBEL_DATES,
        _time_left as _dembel_time_left,
    )
    logger.info(f"🟢 dembel_generator v3.0 импортирован, людей: {len(DEMBEL_DATES)}")
except Exception:
    logger.error("🔴 dembel_generator недоступен:\n" + traceback.format_exc())
    make_dembel_card = None
    make_dembel_summary_card = None
    get_all_timers = None
    get_today_milestones = None
    get_next_milestone = None
    format_dembel_caption = None
    DEMBEL_DATES = {}
    _dembel_time_left = None


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
        data.setdefault("dembel_milestones_sent", [])
        return data
    data = {
        "start_date": date.today().isoformat(), "svodki_start_index": 0,
        "procedura_start_index": 0, "chat_id": CHAT_ID,
        "reminders_enabled": True, "ai_random_replies_enabled": True,
        "personal_ids": {}, "log": {}, "pending_retry": {}, "duty_counts": {},
        "pinned_msg_id": None, "daily_mood": {}, "track_cache": {}, "user_history": {},
        "dembel_milestones_sent": [],
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
    subs = data.setdefault("artist_subs", {}).get(str(user_id), [])
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

def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]

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

def format_person_mention(name: str, data: dict | None = None) -> str:
    """
    Пункт 6: Возвращает живой кликабельный тег [Имя](tg://user?id=...), если боец
    зарегистрировал свой ID в настройках, либо жирный тег с эмодзи.
    """
    if data is None:
        data = load_data()
    em = PERSON_EMOJI.get(name, "👤")
    uid = data.get("personal_ids", {}).get(name)
    if uid:
        return f"{em} [{name}](tg://user?id={uid})"
    return f"{em} *{name}*"

def progress_bar(current: int, total: int) -> str:
    filled = round((current / total) * 8)
    return f"`[{'▓'*filled}{'░'*(8-filled)}]` {current}/{total}"


# ── Пункт 3: Цепочка очереди («Кто за тобой») ─────────────────────────────────
def get_queue_chain(duty_type: str, from_date: date, count: int = 3) -> list[dict]:
    """
    Возвращает список из `count` следующих дежурных (после текущего на `from_date`)
    с точным количеством дней до начала их смены:
    [{"name": "Глеб", "days": 1, "emoji": "💣"}, ...]
    """
    current_person = get_svodki_person(from_date) if duty_type == "svodki" else get_procedura_person(from_date)
    chain: list[dict] = []
    seen = {current_person}
    max_scan = 30

    for d_offset in range(1, max_scan + 1):
        check_date = from_date + timedelta(days=d_offset)
        p = get_svodki_person(check_date) if duty_type == "svodki" else get_procedura_person(check_date)
        if p not in seen:
            seen.add(p)
            chain.append({
                "name": p,
                "days": d_offset,
                "emoji": PERSON_EMOJI.get(p, "👤"),
            })
            if len(chain) >= count:
                break
    return chain


def format_queue_chain_text(queue_chain: list[dict]) -> str:
    """Форматирует цепочку очереди со стрелками: 💣 Глеб (завтра) ➔ 🫡 Дима (+2д) ➔ ⚡ Денис (+3д)"""
    if not queue_chain:
        return "—"
    parts = []
    for item in queue_chain:
        d = item["days"]
        d_str = "завтра" if d == 1 else f"+{d}д"
        parts.append(f"{item.get('emoji', '👤')} {item['name']} (`{d_str}`)")
    return " ➔ ".join(parts)


# ── Пункт 4: Мини-прогресс ДМБ дежурного + остаток нарядов до дома ────────────
def get_person_dembel_brief(person: str, duty_type: str, from_date: date) -> dict | None:
    """
    Рассчитывает для дежурного его личный прогресс ДМБ и точное число оставшихся
    нарядов (сводок или дежурных дней процедурки) до даты его ДМБ.
    """
    target = DEMBEL_DATES.get(person)
    if not target:
        return None

    if _dembel_time_left:
        tl = _dembel_time_left(target)
    else:
        days_left = max(0, (target - from_date).days)
        done = days_left <= 0
        prog = min(1.0, max(0.0, (365 - days_left) / 365.0))
        tl = {
            "done": done,
            "days": days_left,
            "hours": 0,
            "progress": prog,
            "rank": {"short": "ДОМА" if done else "В СТРОЮ"},
        }

    duties_left = 0
    if not tl["done"]:
        rem_days = max(0, (target - from_date).days)
        for d in range(1, rem_days + 1):
            future_d = from_date + timedelta(days=d)
            if duty_type == "svodki":
                if get_svodki_person(future_d) == person:
                    duties_left += 1
            else:
                if get_procedura_person(future_d) == person:
                    duties_left += 1

    pct_str = f"{tl['progress'] * 100:.2f}%"
    target_str = target.strftime("%d.%m.%Y")
    if tl["done"]:
        caption_line = f"🏠 *ДМБ ({target_str}):* `100%` — *ГРАЖДАНСКИЙ (дома!)*"
    else:
        if duties_left > 0:
            forms = ("сводка", "сводки", "сводок") if duty_type == "svodki" else ("дежурство", "дежурства", "дежурств")
            tail = f"ещё `~{duties_left}` {_plural_ru(duties_left, forms)} до дома"
        else:
            tail = "*это крайний наряд перед ДМБ!*"
        caption_line = (
            f"🎖 *До ДМБ ({target_str}):* `{tl['days']} дн. {tl['hours']} ч.` (`{pct_str}`) · {tail}"
        )

    return {
        "has_dembel": True,
        "done": tl["done"],
        "days": tl["days"],
        "hours": tl["hours"],
        "progress": tl["progress"],
        "target_str": target_str,
        "rank_short": tl.get("rank", {}).get("short", ""),
        "duties_left": duties_left,
        "caption_line": caption_line,
    }


# ── Message builders ──────────────────────────────────────────────────────────
_DOCTORS_BY_MONTH = {
    7:  JULY_DOCTORS,
    8:  AUGUST_DOCTORS,
    9:  SEPTEMBER_DOCTORS,
    10: OCTOBER_DOCTORS,
}


def get_doctors_for_date(target_date: date) -> tuple | None:
    return _DOCTORS_BY_MONTH.get(target_date.month, {}).get(target_date.day)


def format_doctors_block(target_date: date) -> str:
    docs = get_doctors_for_date(target_date)
    if not docs:
        return ""
    date_str = target_date.strftime("%d.%m.%Y")
    roles = ("Отв. врач", "I хирург", "II хирург")
    lines = [f"🏥 *Дежурные врачи на {date_str}:*"]
    for idx, doc in enumerate(docs):
        role_lbl = f"{roles[idx]}: " if idx < len(roles) else ""
        lines.append(f"  • {role_lbl}*{doc}*")
    return "\n".join(lines)


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
    sv_days = days_until_svodki(svodki, target_date)
    pr_days = days_until_procedura(proc, target_date)

    sv_chain = format_queue_chain_text(get_queue_chain("svodki", target_date, 3))
    pr_chain = format_queue_chain_text(get_queue_chain("proc", target_date, 3))

    sv_dmb = get_person_dembel_brief(svodki, "svodki", target_date)
    pr_dmb = get_person_dembel_brief(proc, "proc", target_date)
    sv_dmb_line = f"\n  {sv_dmb['caption_line']}" if sv_dmb else ""
    pr_dmb_line = f"\n  {pr_dmb['caption_line']}" if pr_dmb else ""

    doc_block = format_doctors_block(target_date)
    doc_line = f"\n\n{doc_block}" if doc_block else ""

    return (
        f"📋 *Наряд {label}* — {icon} {day_name}, {date_str}\n"
        f"{div}\n\n"
        f"🤷‍♀️ *Дежурный №{SVODKI_LIST.index(svodki)+1} по сводкам (18:00):*\n"
        f"  {sv_em} *{svodki}*\n"
        f"  📊 нарядов всего: *{get_total_duties(svodki)}* (свой цикл через {sv_days} д.)\n"
        f"  ⏭ очередь: {sv_chain}{sv_dmb_line}\n\n"
        f"⚡️ *Дежурный №{PROCEDURA_LIST.index(proc)+1} по процедурке (22:00):*\n"
        f"  {pr_em} *{proc}*\n"
        f"  {bar} ({'экватор смены' if proc_day == 1 else 'сдача поста'})\n"
        f"  📊 нарядов всего: *{get_total_duties(proc)}* (свой цикл через {pr_days} д.)\n"
        f"  ⏭ очередь: {pr_chain}{pr_dmb_line}\n"
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
        docs = get_doctors_for_date(d)
        doc_short = f"\n  🏥 Врачи: {', '.join(docs)}" if docs else ""
        lines.append(
            f"{icon} *{d.strftime('%d.%m')} {day_name}*\n"
            f"  🤷‍♀️ {sv_em} {sv}  ⚡️ {pr_em} {pr} (д.{pday}/2){doc_short}\n"
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
    doc_block = format_doctors_block(today)
    doc_section = f"\n{doc_block}\n" if doc_block else ""
    return (
        f"📌 *АКТУАЛЬНЫЙ НАРЯД*\n{icon} {day_name}, {date_str}\n"
        f"_настроение бота: {mood_lbl}_\n{div}\n\n"
        f"🤷‍♀️ *Дежурный №{SVODKI_LIST.index(svodki)+1} по сводкам:*\n  {sv_em} *{svodki}*\n\n"
        f"⚡️ *Дежурный №{PROCEDURA_LIST.index(proc)+1} по процедурке:*\n  {pr_em} *{proc}*\n  `[{bar}]` день {proc_day}/2\n"
        f"{doc_section}\n"
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


# ── Пункт 9: Контекстная генерация ИИ-заголовка (Gemini) ──────────────────────
async def get_ai_header(
    person: str,
    duty_type: str,
    weekday_name: str = "",
    proc_day: int = 1,
    doctors: tuple | None = None,
    dembel_info: dict | None = None,
    weather_hint: str | None = None,
    total_duties: int = 0,
) -> str | None:
    """
    Генерирует короткую, меткую и смешную фразу-заголовок для карточки наряда
    с учётом реального контекста сегодняшнего вечера:
      - день недели и тип наряда (сводки к 18:00 или процедурка к 22:00)
      - день двухдневной смены для процедурки (1/2 или 2/2)
      - фамилия сегодняшнего ответственного врача (для сводок)
      - сколько дней и нарядов осталось бойцу до ДМБ
      - погода на улице
    """
    try:
        from google import genai
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            return None
        client = genai.Client(api_key=key)

        ctx_parts = []
        if weekday_name:
            ctx_parts.append(f"День недели: {weekday_name}")

        if duty_type == "svodki":
            duty_desc = "собрать и отнести сводки по отделениям к 18:00"
            if doctors and len(doctors) > 0:
                ctx_parts.append(f"Ответственный дежурный врач сегодня: {doctors[0]}")
            if weather_hint:
                ctx_parts.append(f"Погода на улице: {weather_hint[:90]}")
        else:
            stage_desc = (
                "день 1 из 2 (первый вечер смены, экватор)"
                if proc_day == 1
                else "день 2 из 2 (финальный вечер смены, сдача поста)"
            )
            duty_desc = f"уборка процедурки в 22:00 ({stage_desc})"

        if dembel_info and dembel_info.get("has_dembel"):
            if dembel_info.get("done"):
                ctx_parts.append(f"У бойца {person} уже наступил ДМБ (100% выслуги)!")
            else:
                d_left = dembel_info.get("days", 0)
                n_left = dembel_info.get("duties_left", 0)
                ctx_parts.append(f"До дембеля бойцу осталось {d_left} дн. (осталось примерно {n_left} таких нарядов)")

        if total_duties > 0:
            ctx_parts.append(f"Уже выполнено нарядов: {total_duties}")

        context_str = "; ".join(ctx_parts) if ctx_parts else "обычный вечерний наряд"

        prompt = (
            f"Напиши ОДНУ короткую, смешную и дерзкую фразу-заголовок (максимум 8–10 слов), "
            f"чтобы напомнить дежурному по имени {person} выполнить наряд: {duty_desc}.\n"
            f"Контекст вечера (обыграй 1–2 детали отсюда): {context_str}.\n"
            "Начни с подходящего эмодзи. Без кавычек. Можно сленг CS2, Dota 2, JDM/авто или Genshin."
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
        if not key:
            return None
        client = genai.Client(api_key=key)
        prompt = (
            f"Узнай актуальную погоду в {city} прямо сейчас. "
            "В начале укажи температуру и осадки (например: +8°C, пасмурно), а затем 1 короткое "
            "саркастичное предложение в геймерском стиле, как одеться дежурному для похода со сводками."
        )
        r = await client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
            config=gt.GenerateContentConfig(tools=[{"google_search": {}}])
        )
        return r.text.strip() if r.text else None
    except Exception as e:
        logger.error(f"Ошибка погоды в reminder: {e}")
        return None


def _extract_short_weather(weather_text: str | None) -> str | None:
    """Выделяет краткую сводку погоды (до 34 символов) для плашки на самой картинке."""
    if not weather_text:
        return None
    first_line = weather_text.replace("*", "").replace("_", "").split("\n")[0].strip()
    for sep in (". ", " — ", " - ", "! "):
        if sep in first_line:
            first_line = first_line.split(sep)[0].strip()
            break
    return first_line[:36]


# ── Reminders (Пункт 6: единая подпись к карточке без спама 2-м сообщением) ───
async def _send_personal(bot, person: str, duty_type: str):
    data = load_data()
    uid  = data.get("personal_ids", {}).get(person)
    if not uid: return
    pool = (PERSONAL_SVODKI + PERSONAL_SVODKI_GAMING) if duty_type == "svodki" else (PERSONAL_PROC + PERSONAL_PROC_GAMING)
    try: await bot.send_message(uid, random.choice(pool).format(name=person))
    except Exception as e: logger.warning(f"личное сообщение {person}: {e}")


def _build_unified_reminder_caption(
    duty_type: str,
    person: str,
    position_label: str,
    time_label: str,
    header: str,
    ending: str,
    total_duties: int,
    next_days: int,
    proc_day: int,
    queue_chain: list[dict],
    dembel_info: dict | None,
    doctors: tuple | None,
    weather_text: str | None,
    today: date,
    retry: bool,
    data: dict,
) -> str:
    """
    Пункт 6: Собирает всю важную информацию вечернего напоминания в единую
    структурированную подпись (caption) к картинке (с контролем лимита 1024 символа).
    """
    mention = format_person_mention(person, data)
    chain_str = format_queue_chain_text(queue_chain)
    retry_tag = "⚠️ *ПОВТОРНОЕ НАПОМИНАНИЕ (RETRY)*\n" if retry else ""

    if duty_type == "svodki":
        role_line = f"📋 *{position_label}* · сдать до `{time_label}`"
    else:
        bar = progress_bar(proc_day, 2)
        stage_txt = "экватор смены" if proc_day == 1 else "сдача поста"
        role_line = f"🧹 *{position_label}* · до `{time_label}`\n📈 Смена: {bar} ({stage_txt})"

    lines = [
        f"{retry_tag}*{header}*",
        "",
        f"👤 Дежурный: {mention}",
        role_line,
        f"📊 В зачёте: *{total_duties}* {_plural_ru(total_duties, ('наряд', 'наряда', 'нарядов'))} · свой след. цикл через `{next_days} дн.`",
        f"⏭ Очередь следом: {chain_str}",
    ]

    if dembel_info and dembel_info.get("caption_line"):
        lines.append(dembel_info["caption_line"])

    if duty_type == "svodki" and doctors:
        doc_block = format_doctors_block(today)
        if doc_block:
            lines.append("")
            lines.append(doc_block)

    if duty_type == "svodki" and weather_text:
        w_clean = weather_text.strip()
        lines.append("")
        lines.append(f"☁️ *Погода на улице:* {w_clean}")

    lines.append("")
    lines.append(f"_{ending}_")

    caption = "\n".join(lines)
    if len(caption) > 1015:
        caption = caption[:1010].rsplit("\n", 1)[0] + "\n…"
    return caption


async def _send_reminder(bot, duty_type: str, retry: bool = False):
    data = load_data()
    if not data.get("reminders_enabled", True):
        return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        logger.warning("CHAT_ID не настроен!")
        return

    today = datetime.now(TIMEZONE).date()
    wd    = today.weekday()
    weekday_name = WEEKDAY_STYLE[wd][0]
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
        doctors        = get_doctors_for_date(today)
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
        doctors        = None

    # Пункты 3 и 4: цепочка из 3 следующих дежурных и мини-прогресс ДМБ бойца
    queue_chain = get_queue_chain(duty_type, today, count=3)
    dembel_info = get_person_dembel_brief(person, duty_type, today)

    # Для сводок заранее получаем метео-сводку, чтобы встроить её в ИИ-заголовок, карточку и единую подпись (Пункт 6)
    weather_text = None
    if duty_type == "svodki" and not retry:
        weather_text = await get_weather_advice_for_reminder()
    weather_short = _extract_short_weather(weather_text)

    # Пункт 9: Контекстный ИИ-заголовок
    retry_pool = RETRY_PREFIX + RETRY_PREFIX_GAMING
    header = None
    if not retry:
        header = await get_ai_header(
            person=person,
            duty_type=duty_type,
            weekday_name=weekday_name,
            proc_day=proc_day,
            doctors=doctors,
            dembel_info=dembel_info,
            weather_hint=weather_short,
            total_duties=total,
        )
    if not header:
        header = random.choice(retry_pool) if retry else random.choice(headers_pool)
    ending     = get_ending()
    mood_label = get_mood_label()

    caption = _build_unified_reminder_caption(
        duty_type=duty_type,
        person=person,
        position_label=position_label,
        time_label=time_label,
        header=header,
        ending=ending,
        total_duties=total,
        next_days=next_d,
        proc_day=proc_day,
        queue_chain=queue_chain,
        dembel_info=dembel_info,
        doctors=doctors,
        weather_text=weather_text,
        today=today,
        retry=retry,
        data=data,
    )

    if easter:
        msg_text = random.choice(EASTER_EGGS)(person, duty_type) + "\n\n" + caption
        prev_id  = data.get(prev_key)
        if prev_id:
            try: await bot.delete_message(chat_id, prev_id)
            except: pass
        sent = await bot.send_message(
            chat_id, msg_text, parse_mode="Markdown",
            reply_markup=confirmation_keyboard(duty_type),
        )
        data[prev_key] = sent.message_id
        save_data(data)
        await update_pinned_message(bot)
        await _send_personal(bot, person, duty_type)
        return

    date_label = today.strftime("%d.%m.%Y") + " • " + weekday_name
    theme_key  = random.choice(THEME_KEYS)
    img_path   = os.path.join(TEMP_DIR, f"card_{duty_type}_{int(time.time())}.jpg")

    prev_id = data.get(prev_key)
    if prev_id:
        try: await bot.delete_message(chat_id, prev_id)
        except: pass

    if make_reminder_card:
        try:
            make_reminder_card(
                duty_type=duty_type,
                person=person,
                position_label=position_label,
                time_label=time_label,
                header_text=header,
                ending_text=ending,
                date_label=date_label,
                mood_label=mood_label,
                proc_day=proc_day if duty_type == "proc" else 1,
                next_person=next_n,
                next_days=next_d,
                total_duties=total,
                theme_key=theme_key,
                output_path=img_path,
                doctors=doctors,
                queue_chain=queue_chain,
                dembel_info=dembel_info,
                weather_short=weather_short,
            )
            photo = FSInputFile(img_path)
            sent  = await bot.send_photo(
                chat_id, photo=photo, caption=caption,
                parse_mode="Markdown", reply_markup=confirmation_keyboard(duty_type),
            )
            data[prev_key] = sent.message_id
            save_data(data)
            try: os.remove(img_path)
            except: pass
            await update_pinned_message(bot)
            await _send_personal(bot, person, duty_type)
            return
        except Exception as e:
            logger.error(f"Ошибка карточки: {e}")

    # Текстовый fallback (также использует единую подпись)
    sent = await bot.send_message(
        chat_id, caption, parse_mode="Markdown",
        reply_markup=confirmation_keyboard(duty_type),
    )
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


# ── Автоматические утренние оповещения о круглых датах ДМБ ───────────────────
async def check_dembel_milestones(bot):
    """
    Проверяет, наступила ли сегодня у кого-то из DEMBEL_DATES круглая дата
    (ДМБ, Светофор 3д, Космос 10д, 30д, Полтинник 50д, Стодневка 100д, Экватор 182д, 200д),
    и отправляет поздравление с карточкой в рабочий чат ровно 1 раз.
    """
    if not get_today_milestones:
        return
    data = load_data()
    if not data.get("reminders_enabled", True):
        return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        return

    sent_keys: list[str] = data.setdefault("dembel_milestones_sent", [])
    events = get_today_milestones()
    changed = False

    for ev in events:
        if ev["key"] in sent_keys:
            continue
        person = ev["person"]
        caption = (
            f"🎉 *РУБЕЖ ДМБ — {person.upper()}!*\n"
            f"{ev['title']}\n\n"
            + (format_dembel_caption(person) if format_dembel_caption else "")
        )
        img_path = os.path.join(TEMP_DIR, f"milestone_{int(time.time())}.jpg")
        try:
            if make_dembel_card:
                make_dembel_card(person, output_path=img_path, theme_key=random.choice(THEME_KEYS))
                photo = FSInputFile(img_path)
                await bot.send_photo(
                    chat_id, photo=photo, caption=caption,
                    parse_mode="Markdown", reply_markup=_dembel_list_keyboard(active_person=person),
                )
            else:
                await bot.send_message(chat_id, caption, parse_mode="Markdown")
            sent_keys.append(ev["key"])
            changed = True
        except Exception as e:
            logger.error(f"Ошибка отправки вехи ДМБ ({person}): {e}")
        finally:
            try:
                if os.path.exists(img_path):
                    os.remove(img_path)
            except Exception:
                pass

    if changed:
        data["dembel_milestones_sent"] = sent_keys[-120:]
        save_data(data)


async def daily_pinned_update(bot):
    await update_pinned_message(bot)
    await check_dembel_milestones(bot)


# ── Selftest ──────────────────────────────────────────────────────────────────
def _startup_selftest():
    if not make_reminder_card: return
    test_path = os.path.join(TEMP_DIR, "_selftest.jpg")
    try:
        today = datetime.now(TIMEZONE).date()
        make_reminder_card(
            duty_type="svodki",
            person="Игорь",
            position_label="самопроверка",
            time_label="18:00",
            header_text="проверка генератора карточек v3.0",
            ending_text="всё ок",
            date_label=today.strftime("%d.%m.%Y"),
            mood_label="проверка",
            doctors=get_doctors_for_date(today),
            queue_chain=[{"name": "Ильнар", "days": 1}, {"name": "Арсений", "days": 2}, {"name": "Глеб", "days": 3}],
            theme_key=random.choice(THEME_KEYS),
            output_path=test_path,
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
        "🧠 *ИИ (Gemini 2.5)* — пиши мне в личку, упомяни или ответь на моё сообщение. "
        "Я сам пойму просьбу и запущу нужную функцию:\n"
        "«найди профиль natgeo в инсте», «скинь Du Hast», «посоветуй аниме», "
        "«найди наушники до 3000», «скачай видео по ссылке», «какая погода в Москве»\n"
        "`/ai` — ask, roast, debate, story, memory, forget\n"
        "`/persona` — сменить характер бота\n"
        "`/tldr` `/meme` `/poll` `/imagen` — выжимка, мемы, картинки\n"
        "Ответь на фото/PDF/видео/ГС: «переведи», «что написано», «объясни мем»\n\n"
        "🎲 *Выбор и жребий:* `/pick кофе или чай`, `/who кто моет посуду`, `/roll`, `/coin`\n\n"
        "📍 *Рядом:* `/nearby` — кафе, аптеки, магазины (скинь геопозицию или адрес: "
        "`/nearby аптека Тверская 7`)\n\n"
        "🍿 *Фильмы и сериалы:* `/film <название>`; `trending`, `random`, `ai <вайб>`\n\n"
        "🎁 *Бесплатные игры:* `/free` — раздачи сейчас, `/free sale` — скидки, `/free on|off` — уведомления\n\n"
        "📋 *Наряды:* кнопки внизу, `/svodki` — напомнить сейчас\n"
        "🎖 *Дембель:* `/dembel` — карточки и общая таблица\n\n"
        "🎵 *Музыка:*\n"
        "`/find <название>` — поиск, `/charts` — чарт, `/music` — дашборд\n"
        "`/my_music` `/playlists` `/queue` `/dj` `/wrapped`\n\n"
        "📸 *Instagram:*\n"
        "`/ig <username>` — профиль, `/igdl <ссылка>` — пост, рилс, сторис\n"
        "`/ig_subs` — мои подписки, `/ig_popular` — популярные\n\n"
        "📥 *Медиа:* кинь ссылку YouTube, TikTok, X, Reddit, Instagram, Pinterest, VK Видео, "
        "Twitch-клип или Threads — скачаю. Или `/dl <ссылка>`. "
        "Ссылка Spotify / Яндекс.Музыки / VK Музыки — найду трек\n\n"
        "🎬 *Аниме:* `/anime <название>`; `random`, `ai <вайб>`, `top`, `schedule`, `profile`. "
        "Скрин или гифка с `/anime` — найду, что за аниме. Inline: `@bot a:<запрос>`\n\n"
        "🔥 *Лента:* `/feed` — мемы и посты, `/saved` — сохранённое, `/feed_settings`\n\n"
        "🛒 *Товары:* `/ozon <что искать>` — цены и рейтинг (Wildberries)\n"
        "🌦 *Прочее:* `/pogoda` `/chatid` `/help`\n\n"
        "Всё вижу. Всего не прощаю. Погнали 👇"
    )
    sent = await message.answer(start_text, parse_mode="Markdown", reply_markup=main_keyboard())
    auto_delete_later(message.bot, message.chat.id, sent.message_id, 180)

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


# ── Dembel (HUD v3.0: обновление на месте, общая таблица, вехи и звания) ─────
def _dembel_list_keyboard(active_person: str | None = None, is_summary: bool = False) -> InlineKeyboardMarkup:
    """
    Строит клавиатуру дембель-таймера.
    Все кнопки используют префикс dembel_card:, поэтому main.py менять не нужно:
      - dembel_card:__refresh__:<person> (или __all__) — перерисовать текущую карточку
      - dembel_card:__all__ — сводный дашборд всех бойцов
      - dembel_card:<person> — карточка конкретного бойца
    """
    buttons: list[list[InlineKeyboardButton]] = []

    top_row: list[InlineKeyboardButton] = []
    if active_person:
        top_row.append(
            InlineKeyboardButton(
                text="🔄 Обновить таймер",
                callback_data=f"dembel_card:__refresh__:{active_person}",
            )
        )
    elif is_summary:
        top_row.append(
            InlineKeyboardButton(
                text="🔄 Обновить таблицу",
                callback_data="dembel_card:__refresh__:__all__",
            )
        )
    top_row.append(
        InlineKeyboardButton(
            text="📊 Общая таблица ДМБ",
            callback_data="dembel_card:__all__",
        )
    )
    buttons.append(top_row)

    timers_map = {t["person"]: t for t in get_all_timers()} if get_all_timers else {}
    row: list[InlineKeyboardButton] = []
    for person in DEMBEL_DATES:
        t = timers_map.get(person)
        if t and t.get("done"):
            icon = "🏠"
            suffix = " (дома)"
        else:
            icon = "🟢" if person == active_person else "🎖"
            suffix = f" ({t['days']}д)" if t else ""
        row.append(
            InlineKeyboardButton(
                text=f"{icon} {person}{suffix}",
                callback_data=f"dembel_card:{person}",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def cmd_dembel(message: types.Message):
    if not get_all_timers:
        await message.answer("❌ Дембель-таймер недоступен.")
        return

    await check_dembel_milestones(message.bot)

    raw = (message.text or "").replace("/dembel", "").strip()
    if raw:
        if raw.lower() in ("all", "все", "общий", "таблица", "дашборд"):
            await _send_dembel_summary(message, edit_in_place=False)
            return
        match = next((p for p in DEMBEL_DATES if p.lower() == raw.lower()), None)
        if not match:
            await message.answer(f"❌ Не нашёл «{raw}».\nЕсть: {', '.join(DEMBEL_DATES)}")
            return
        await _send_dembel_card(message, match, edit_in_place=False)
        return

    timers = get_all_timers()
    done_cnt = sum(1 for t in timers if t["done"])
    avg_pct = sum(t["progress"] for t in timers) / max(1, len(timers)) * 100.0
    lines = [
        f"🎖 *ДЕМБЕЛЬ-ТАЙМЕР (1 ГОД · 365 ДНЕЙ)*",
        f"📊 Средний прогресс: *{avg_pct:.2f}%*  |  🏠 Дома: *{done_cnt}/{len(timers)}*\n",
    ]
    for idx, t in enumerate(timers, 1):
        rank_short = t.get("rank", {}).get("short", "")
        if t["done"]:
            lines.append(f"🏆 *{t['person']}* — *ГРАЖДАНСКИЙ (дома с {t['target'].strftime('%d.%m.%Y')})*")
        else:
            pct_str = f"{t['progress'] * 100:.2f}%"
            lines.append(
                f"`#{idx}` *{t['person']}* ({rank_short}, `{pct_str}`) — "
                f"*{t['days']} дн. {t['hours']} ч.* (до `{t['target'].strftime('%d.%m.%Y')}`)"
            )
    lines.append("\n_Нажми на имя для карточки HUD v3.0 или открой общую таблицу:_")
    await message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_dembel_list_keyboard(),
    )


async def _send_dembel_card(source: types.Message, person: str, edit_in_place: bool = False):
    chat_id = source.chat.id
    bot     = source.bot
    _map = str.maketrans({
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z",
        "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
        "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch",
        "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    })
    translit = person.lower().translate(_map)
    safe_slug = "".join(c for c in translit if c.isalnum()) or "person"
    img_path  = os.path.join(TEMP_DIR, f"dembel_{safe_slug}_{int(time.time()*1000)}.jpg")

    status = None
    if not (edit_in_place and source.photo):
        status = await bot.send_message(chat_id, f"🎨 Рисую карточку для *{person}*...", parse_mode="Markdown")

    try:
        if make_dembel_card:
            make_dembel_card(person, output_path=img_path, theme_key=random.choice(THEME_KEYS))
        else:
            raise RuntimeError("make_dembel_card недоступен")
    except Exception as e:
        logger.error(f"Ошибка дембель-карточки {person}: {e}")
        if status:
            try: await status.delete()
            except: pass
        timers = {t["person"]: t for t in get_all_timers()}
        t = timers.get(person, {})
        text = f"🏠 *{person}* — дома!" if t.get("done") else f"⏳ *{person}* — {t.get('days','?')} дн. {t.get('hours','?')} ч."
        await bot.send_message(chat_id, text, parse_mode="Markdown")
        return

    if status:
        try: await status.delete()
        except: pass

    caption = format_dembel_caption(person) if format_dembel_caption else f"🎖 Дембель-таймер · *{person}*"
    kb = _dembel_list_keyboard(active_person=person)

    try:
        photo = FSInputFile(img_path)
        if edit_in_place and source.photo:
            media = InputMediaPhoto(media=photo, caption=caption, parse_mode="Markdown")
            await source.edit_media(media=media, reply_markup=kb)
        else:
            await bot.send_photo(
                chat_id, photo=photo, caption=caption,
                parse_mode="Markdown", reply_markup=kb,
            )
    except Exception as e:
        logger.error(f"Ошибка отправки/обновления дембель-карточки: {e}")
        try:
            photo = FSInputFile(img_path)
            await bot.send_photo(
                chat_id, photo=photo, caption=caption,
                parse_mode="Markdown", reply_markup=kb,
            )
        except Exception:
            await bot.send_message(chat_id, f"❌ Не получилось отправить карточку для {person}.")
    finally:
        try: os.remove(img_path)
        except: pass


async def _send_dembel_summary(source: types.Message, edit_in_place: bool = False):
    chat_id = source.chat.id
    bot     = source.bot
    img_path = os.path.join(TEMP_DIR, f"dembel_summary_{int(time.time()*1000)}.jpg")

    status = None
    if not (edit_in_place and source.photo):
        status = await bot.send_message(chat_id, "📊 Собираю общую таблицу ДМБ...", parse_mode="Markdown")

    try:
        if make_dembel_summary_card:
            make_dembel_summary_card(output_path=img_path, theme_key=random.choice(THEME_KEYS))
        else:
            raise RuntimeError("make_dembel_summary_card недоступен")
    except Exception as e:
        logger.error(f"Ошибка общей таблицы ДМБ: {e}")
        if status:
            try: await status.delete()
            except: pass
        await bot.send_message(chat_id, "❌ Не удалось сгенерировать общую таблицу ДМБ.")
        return

    if status:
        try: await status.delete()
        except: pass

    timers = get_all_timers()
    done_cnt = sum(1 for t in timers if t["done"])
    avg_pct = sum(t["progress"] for t in timers) / max(1, len(timers)) * 100.0
    next_up = next((t for t in timers if not t["done"]), None)
    next_line = (
        f"⏳ Ближайший ДМБ: *{next_up['person']}* — осталось `{next_up['days']} дн. {next_up['hours']} ч.` (`{next_up['target'].strftime('%d.%m.%Y')}`)"
        if next_up else "🎉 Все бойцы дома!"
    )
    caption = (
        f"📊 *ОБЩАЯ ТАБЛИЦА ДМБ (1 ГОД)*\n"
        f"📈 Средний прогресс: *{avg_pct:.4f}%*  |  🏠 Дома: *{done_cnt}/{len(timers)}*\n"
        f"{next_line}"
    )
    kb = _dembel_list_keyboard(is_summary=True)

    try:
        photo = FSInputFile(img_path)
        if edit_in_place and source.photo:
            media = InputMediaPhoto(media=photo, caption=caption, parse_mode="Markdown")
            await source.edit_media(media=media, reply_markup=kb)
        else:
            await bot.send_photo(
                chat_id, photo=photo, caption=caption,
                parse_mode="Markdown", reply_markup=kb,
            )
    except Exception as e:
        logger.error(f"Ошибка отправки общей таблицы ДМБ: {e}")
    finally:
        try: os.remove(img_path)
        except: pass


async def callback_dembel_card(callback: types.CallbackQuery):
    payload = callback.data.split(":", 1)[1]
    is_photo_msg = bool(callback.message and callback.message.photo)

    if payload == "__all__" or payload == "__refresh__:__all__":
        await callback.answer("🔄 Обновляю общую таблицу ДМБ...")
        await _send_dembel_summary(callback.message, edit_in_place=is_photo_msg)
        return

    if payload.startswith("__refresh__:"):
        person = payload.split(":", 1)[1]
        await callback.answer(f"🔄 Обновляю таймер: {person}...")
        await _send_dembel_card(callback.message, person, edit_in_place=is_photo_msg)
        return

    person = payload
    await callback.answer(f"🎖 {person}")
    await _send_dembel_card(callback.message, person, edit_in_place=is_photo_msg)

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