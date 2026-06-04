import asyncio
import logging
import json
import os
import random
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ══════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════
TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID   = os.getenv("CHAT_ID",   "YOUR_CHAT_ID_HERE")
DATA_FILE = "duty_data.json"
TIMEZONE  = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#  СПИСКИ ДЕЖУРНЫХ
# ══════════════════════════════════════════════
SVODKI_LIST    = ["Саша", "Олег", "Максим", "Игорь", "Илья", "Глеба", "Слава", "Ильнар"]
PROCEDURA_LIST = ["Илья", "Слава", "Саша", "Игорь", "Глеба", "Ильнар"]

# 2 — уникальные эмодзи-аватары каждому
PERSON_EMOJI = {
    "Саша":   "🦊",
    "Олег":   "🐻",
    "Максим": "🦁",
    "Игорь":  "🐺",
    "Илья":   "🦅",
    "Глеба":  "🐯",
    "Слава":  "🦝",
    "Ильнар": "🐉",
}

# 3 — рамка по дню недели: (имя, иконка, символ рамки, символ рамки конец)
WEEKDAY_STYLE = {
    0: ("понедельник", "😵",  "▸▸", "◂◂"),   # пн — начало страданий
    1: ("вторник",     "😐",  "◈",  "◈"),
    2: ("среда",       "⬡",   "⬡",  "⬡"),
    3: ("четверг",     "✦",   "✦",  "✦"),
    4: ("пятница",     "🔥",  "★",  "★"),
    5: ("суббота",     "🥳",  "🎉", "🎉"),
    6: ("воскресенье", "💀",  "🌙", "🌙"),
}

# ══════════════════════════════════════════════
#  ПУЛЫ ФРАЗ (зумерский стиль, умеренно)
# ══════════════════════════════════════════════

# 6 — случайные заголовки напоминания о сводках
SVODKI_HEADERS = [
    "📋 йоу, время сводок. не облажайся",
    "📋 сводки сами себя не потащат, очнись",
    "📋 18:00 — расслабляться рано, братан",
    "📋 наряд вышел, деваться некуда лол",
    "📋 внимание, избранный! сводки зовут",
    "📋 бип-буп, активирую дежурного 🤖",
    "📋 время икс. сводки. go go go",
    "📋 ну всё, твой выход чемпион",
]

# 6 — случайные заголовки напоминания о процедурке
PROC_HEADERS = [
    "🧹 22:00 — процедурка не помоет себя сама",
    "🧹 уборка — это медитация. почти",
    "🧹 йоу, пора убраться в процедурке",
    "🧹 вечерний наряд активирован 🌙",
    "🧹 процедурка ждёт. она терпеливая, но не бесконечно",
    "🧹 22:00 и тебе ещё предстоит подвиг",
    "🧹 эй, уборка сама себя не сделает",
    "🧹 наряд по процедурке. это буквально твоя судьба",
]

# 7 — случайные финальные фразы
ENDINGS = [
    "давай, не подведи команду 💪",
    "ты справишься, не ной 🫡",
    "вперёд, легенда 🚀",
    "команда верит. ну или типа того 👊",
    "не тупи, сделай и иди отдыхай 🛋",
    "всё в твоих руках, чемпион 🤝",
    "удачи. хотя тут и удача не нужна, просто сделай 😐",
    "погнали, чё стоишь 💨",
]

# 12 — реакция на «Сделано»
DONE_REPLIES = [
    "✅ красава! записал, можешь выдыхать 🛋",
    "✅ зафиксировал. команда тобой гордится (наверное)",
    "✅ чётко отработал, уважение 🫡",
    "✅ выполнено. ты молодец, иди отдыхай",
    "✅ записал в журнал. так держать, братан 💪",
    "✅ сделано! статистика не врёт — ты топ",
]

# повторное напоминание
RETRY_PREFIX = [
    "⏰ эй, ты там живой? повторяю:",
    "⏰ второй звонок, не игнорируй:",
    "⏰ окей окей, напоминаю ещё раз:",
    "⏰ хм, всё ещё не сделано? ладно:",
]

# личные уведомления сводки
PERSONAL_SVODKI = [
    "эй {name} 👋 сегодня твои сводки. не забудь, ладно?",
    "yo {name}, наряд по сводкам — это ты сегодня. погнали 📋",
    "{name}, привет. сводки сами себя не потащат, ты понял",
    "внимание, {name}! сводки сегодня на тебе. удачи 🫡",
]

# личные уведомления процедурка
PERSONAL_PROC = [
    "эй {name} 🧹 вечером процедурка на тебе. не забей",
    "yo {name}, уборка процедурки сегодня твоя. справишься",
    "{name}, привет. процедурка ждёт тебя в 22:00 👀",
    "внимание, {name}! процедурка сегодня твоя. это судьба",
]

# понедельничная рассылка
MONDAY_INTROS = [
    "☀️ доброе утро, страдальцы. новая неделя — новые наряды:",
    "☀️ понедельник, все мы немного умерли. но наряды никто не отменял:",
    "☀️ йоу, начинаем неделю. вот кто пашет:",
    "☀️ пн 9:00 — самое то узнать кому досталось на этой неделе:",
]

# воскресный итог
SUNDAY_INTROS = [
    "🏆 итоги недели. кто вообще старался:",
    "🏆 неделя прошла, подводим итоги нарядов:",
    "🏆 воскресенье — время чтить героев:",
    "🏆 статистика недели. цифры не врут:",
]

# ══════════════════════════════════════════════
#  ХРАНИЛИЩЕ ДАННЫХ
# ══════════════════════════════════════════════
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    data = {
        "start_date":            date.today().isoformat(),
        "svodki_start_index":    0,
        "procedura_start_index": 0,
        "chat_id":               CHAT_ID,
        "reminders_enabled":     True,
        "personal_ids":          {},
        "log":                   {},
        "pending_retry":         {},
        "duty_counts":           {},   # 11 — {"Саша": {"svodki": 3, "proc": 2}}
    }
    save_data(data)
    return data

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def increment_count(name: str, duty_type: str):
    """11 — плюсуем счётчик нарядов"""
    data = load_data()
    counts = data.setdefault("duty_counts", {})
    person = counts.setdefault(name, {"svodki": 0, "proc": 0})
    person[duty_type] = person.get(duty_type, 0) + 1
    data["duty_counts"] = counts
    save_data(data)

def get_total_duties(name: str) -> int:
    data = load_data()
    counts = data.get("duty_counts", {}).get(name, {})
    return counts.get("svodki", 0) + counts.get("proc", 0)

# ══════════════════════════════════════════════
#  ЛОГИКА НАРЯДОВ
# ══════════════════════════════════════════════
def get_svodki_idx(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    days_passed = (target_date - start).days
    return (data["svodki_start_index"] + days_passed) % len(SVODKI_LIST)

def get_svodki_person(target_date: date) -> str:
    return SVODKI_LIST[get_svodki_idx(target_date)]

def get_procedura_idx(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    days_passed = (target_date - start).days
    return (data["procedura_start_index"] + days_passed // 2) % len(PROCEDURA_LIST)

def get_procedura_person(target_date: date) -> str:
    return PROCEDURA_LIST[get_procedura_idx(target_date)]

def get_duty_day_number(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    days_passed = (target_date - start).days
    return (days_passed % 2) + 1

def days_until_svodki(name: str, from_date: date) -> int:
    for i in range(1, len(SVODKI_LIST) + 1):
        if get_svodki_person(from_date + timedelta(days=i)) == name:
            return i
    return len(SVODKI_LIST)

def days_until_procedura(name: str, from_date: date) -> int:
    for i in range(1, len(PROCEDURA_LIST) * 2 + 1):
        if get_procedura_person(from_date + timedelta(days=i)) == name:
            return i
    return len(PROCEDURA_LIST) * 2

# ══════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════
class AdminStates(StatesGroup):
    waiting_chat_id  = State()
    waiting_register = State()

# ══════════════════════════════════════════════
#  ОФОРМЛЕНИЕ
# ══════════════════════════════════════════════
def is_weekend(d: date) -> bool:
    return d.weekday() >= 5

def person_tag(name: str) -> str:
    """2 — эмодзи-аватар + имя"""
    return f"{PERSON_EMOJI.get(name, '👤')} *{name}*"

def progress_bar(current: int, total: int) -> str:
    """1 — прогресс-бар наряда"""
    filled = round((current / total) * 8)
    bar = "▓" * filled + "░" * (8 - filled)
    return f"`[{bar}]` {current}/{total}"

def weekday_divider(d: date) -> str:
    """3 — рамка по дню недели"""
    _, _, sym_l, sym_r = WEEKDAY_STYLE[d.weekday()]
    return f"{sym_l}━━━━━━━━━━━━━━━━{sym_r}"

def build_date_header(d: date, label: str) -> str:
    """3 + 14 — заголовок с иконкой дня и особым стилем выходных"""
    day_name, icon, sym_l, sym_r = WEEKDAY_STYLE[d.weekday()]
    date_str = d.strftime("%d.%m.%Y")
    if is_weekend(d):
        return f"{icon} *{label} — {day_name.upper()}, {date_str}* {icon}"
    return f"{icon} *{label} — {day_name}, {date_str}*"

def svodki_num_label(name: str) -> str:
    """19 — позиция в списке"""
    return f"Дежурный №{SVODKI_LIST.index(name)+1} по сводкам"

def proc_num_label(name: str) -> str:
    return f"Дежурный №{PROCEDURA_LIST.index(name)+1} по процедурке"

def next_person_svodki(name: str, from_date: date) -> str:
    """13 — кто следующий после name в ротации сводок"""
    idx = SVODKI_LIST.index(name)
    return SVODKI_LIST[(idx + 1) % len(SVODKI_LIST)]

def next_person_proc(name: str, from_date: date) -> str:
    idx = PROCEDURA_LIST.index(name)
    return PROCEDURA_LIST[(idx + 1) % len(PROCEDURA_LIST)]

# ══════════════════════════════════════════════
#  ПОСТРОЕНИЕ СООБЩЕНИЙ
# ══════════════════════════════════════════════
def build_duty_message(target_date: date, label: str) -> str:
    svodki   = get_svodki_person(target_date)
    proc     = get_procedura_person(target_date)
    proc_day = get_duty_day_number(target_date)
    div      = weekday_divider(target_date)
    header   = build_date_header(target_date, label)
    weekend_note = "\n🥳 *выходной, но наряды никто не отменял*" if is_weekend(target_date) else ""

    next_sv_days = days_until_svodki(svodki, target_date)
    next_pr_days = days_until_procedura(proc, target_date)
    next_sv_name = next_person_svodki(svodki, target_date)
    next_pr_name = next_person_proc(proc, target_date)

    total_sv = get_total_duties(svodki)
    total_pr = get_total_duties(proc)

    lines = [
        f"{header}{weekend_note}",
        div,
        "",
        f"🤷‍♀️ *{svodki_num_label(svodki)}:*",
        f"    {person_tag(svodki)}",
        f"    📊 нарядов всего: *{total_sv}*",
        f"    ⏭ следующий: {person_tag(next_sv_name)} через *{next_sv_days} д.*",
        "",
        f"⚡️ *{proc_num_label(proc)}:*",
        f"    {person_tag(proc)}",
        f"    {progress_bar(proc_day, 2)}",
        f"    📊 нарядов всего: *{total_pr}*",
        f"    ⏭ следующий: {person_tag(next_pr_name)} через *{next_pr_days} д.*",
        "",
        div,
    ]
    return "\n".join(lines)

def build_week_schedule() -> str:
    today = date.today()
    short = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    lines = ["*📊 расписание нарядов — 7 дней вперёд*\n_кто следующий страдалец? смотри:_\n"]
    for i in range(7):
        d        = today + timedelta(days=i)
        sv       = get_svodki_person(d)
        pr       = get_procedura_person(d)
        pd       = get_duty_day_number(d)
        sv_num   = SVODKI_LIST.index(sv) + 1
        pr_num   = PROCEDURA_LIST.index(pr) + 1
        wk_icon  = WEEKDAY_STYLE[d.weekday()][1]
        lbl      = "сегодня" if i == 0 else ("завтра" if i == 1 else f"{short[d.weekday()]} {d.strftime('%d.%m')}")
        lines.append(
            f"{wk_icon} *{lbl}*\n"
            f"  {PERSON_EMOJI.get(sv,'👤')} №{sv_num} {sv} — сводки\n"
            f"  {PERSON_EMOJI.get(pr,'👤')} №{pr_num} {pr} — процедурка {progress_bar(pd,2)}\n"
        )
    lines.append(weekday_divider(today))
    return "\n".join(lines)

def build_log_message() -> str:
    """4 — журнал с зумерскими подписями"""
    data = load_data()
    log  = data.get("log", {})
    lines = ["*📓 журнал за 7 дней*\n_кто делал, кто забил — всё тут:_\n"]
    for i in range(6, -1, -1):
        d     = date.today() - timedelta(days=i)
        key   = d.isoformat()
        entry = log.get(key, {})
        sv    = get_svodki_person(d)
        pr    = get_procedura_person(d)
        sv_ok = "✅" if entry.get("svodki") else "❌"
        pr_ok = "✅" if entry.get("proc")   else "❌"
        lbl   = "сегодня" if i == 0 else d.strftime("%d.%m")
        sv_em = PERSON_EMOJI.get(sv, "👤")
        pr_em = PERSON_EMOJI.get(pr, "👤")
        lines.append(f"📅 *{lbl}*  {sv_ok}{sv_em}{sv}  |  {pr_ok}{pr_em}{pr}")
    lines.append("\n_✅ = сделано, ❌ = либо сделано без отметки, либо нет 🤷_")
    return "\n".join(lines)

def build_monday_briefing() -> str:
    """14 — понедельничная рассылка на всю неделю"""
    today = date.today()
    intro = random.choice(MONDAY_INTROS)
    short = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    lines = [f"{intro}\n"]
    for i in range(7):
        d       = today + timedelta(days=i)
        sv      = get_svodki_person(d)
        pr      = get_procedura_person(d)
        pd      = get_duty_day_number(d)
        wk_icon = WEEKDAY_STYLE[d.weekday()][1]
        lbl     = f"{short[d.weekday()]} {d.strftime('%d.%m')}"
        sv_em   = PERSON_EMOJI.get(sv, "👤")
        pr_em   = PERSON_EMOJI.get(pr, "👤")
        lines.append(
            f"{wk_icon} *{lbl}* — {sv_em}{sv} / {pr_em}{pr} {progress_bar(pd,2)}"
        )
    lines.append(f"\n{weekday_divider(today)}")
    lines.append("_удачи всем. она вам понадобится_ 💀")
    return "\n".join(lines)

def build_sunday_summary() -> str:
    """15 — воскресный итог с MVP"""
    data   = load_data()
    log    = data.get("log", {})
    counts = {}   # {"Саша": {"svodki":2,"proc":1}}
    intro  = random.choice(SUNDAY_INTROS)

    for i in range(6, -1, -1):
        d     = date.today() - timedelta(days=i)
        entry = log.get(d.isoformat(), {})
        for field in ("svodki", "proc"):
            name = entry.get(field)
            if name:
                counts.setdefault(name, {"svodki": 0, "proc": 0})
                counts[name][field] += 1

    if not counts:
        return (
            f"{intro}\n\n"
            "😶 за эту неделю никто не нажимал «сделано»\n"
            "либо все реально пахали молча, либо… ну ты понял"
        )

    # MVP — у кого больше всего подтверждений
    mvp = max(counts, key=lambda n: counts[n]["svodki"] + counts[n]["proc"])
    mvp_em = PERSON_EMOJI.get(mvp, "👤")
    lines = [f"{intro}\n"]
    for name, c in sorted(counts.items(), key=lambda x: -(x[1]["svodki"]+x[1]["proc"])):
        em = PERSON_EMOJI.get(name, "👤")
        total = c["svodki"] + c["proc"]
        lines.append(
            f"  {em} *{name}* — сводки: {c['svodki']}, процедурка: {c['proc']} | итого: {total}"
        )
    lines.append(f"\n🏆 *mvp недели: {mvp_em} {mvp}*")
    lines.append("_уважение и почёт. заслужил_ 🫡")
    return "\n".join(lines)

# ══════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Наряд сегодня"),       KeyboardButton(text="📅 Наряд завтра")],
            [KeyboardButton(text="📊 Расписание на неделю"), KeyboardButton(text="📓 Журнал")],
            [KeyboardButton(text="🔔 Напомнить сейчас"),    KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True
    )

def confirmation_keyboard(duty_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ сделано, всё чётко",   callback_data=f"done:{duty_type}"),
        InlineKeyboardButton(text="⏰ напомни через 15м",    callback_data=f"retry:{duty_type}"),
    ]])

def settings_keyboard(data: dict) -> InlineKeyboardMarkup:
    enabled     = data.get("reminders_enabled", True)
    toggle_text = "🔕 вырубить напоминания" if enabled else "🔔 включить напоминания"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text,                           callback_data="toggle_reminders")],
        [InlineKeyboardButton(text="📍 установить чат",                   callback_data="set_chat")],
        [InlineKeyboardButton(text="👤 кто сегодня — сводки",             callback_data="pick_svodki")],
        [InlineKeyboardButton(text="👤 кто сегодня — процедурка",         callback_data="pick_procedura")],
        [InlineKeyboardButton(text="📝 список сводок",                    callback_data="show_svodki_list")],
        [InlineKeyboardButton(text="📝 список процедурки",                callback_data="show_procedura_list")],
        [InlineKeyboardButton(text="🪪 зарегистрировать личный id",       callback_data="register_personal")],
        [InlineKeyboardButton(text="◀️ назад",                            callback_data="back_main")],
    ])

def svodki_pick_keyboard(actual_idx: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, name in enumerate(SVODKI_LIST):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = "✅ " if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{mark}{em}{name}", callback_data=f"set_svodki_idx:{i}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def procedura_pick_keyboard(actual_idx: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, name in enumerate(PROCEDURA_LIST):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = "✅ " if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{mark}{em}{name}", callback_data=f"set_proc_idx:{i}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ══════════════════════════════════════════════
#  ОТПРАВКА НАПОМИНАНИЙ
# ══════════════════════════════════════════════
async def _send_reminder(bot: Bot, duty_type: str, retry: bool = False):
    data    = load_data()
    if not data.get("reminders_enabled", True):
        return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        logger.warning("CHAT_ID не настроен!")
        return

    today   = datetime.now(TIMEZONE).date()
    div     = weekday_divider(today)
    ending  = random.choice(ENDINGS)

    if duty_type == "svodki":
        person   = get_svodki_person(today)
        sv_num   = SVODKI_LIST.index(person) + 1
        next_d   = days_until_svodki(person, today)
        next_n   = next_person_svodki(person, today)
        total    = get_total_duties(person)
        header   = random.choice(RETRY_PREFIX) if retry else random.choice(SVODKI_HEADERS)
        msg_text = (
            f"*{header}*\n{div}\n\n"
            f"🤷‍♀️ *{svodki_num_label(person)}:*\n"
            f"    {person_tag(person)}\n"
            f"    📊 нарядов за всё время: *{total}*\n"
            f"    ⏭ после — {person_tag(next_n)} через *{next_d} д.*\n\n"
            f"_{ending}_\n{div}"
        )
    else:
        person     = get_procedura_person(today)
        proc_day   = get_duty_day_number(today)
        tomorrow   = today + timedelta(days=1)
        proc_next  = get_procedura_person(tomorrow)
        proc_day_t = get_duty_day_number(tomorrow)
        next_d     = days_until_procedura(person, today)
        next_n     = next_person_proc(person, today)
        total      = get_total_duties(person)
        header     = random.choice(RETRY_PREFIX) if retry else random.choice(PROC_HEADERS)
        msg_text   = (
            f"*{header}*\n{div}\n\n"
            f"⚡️ *{proc_num_label(person)}:*\n"
            f"    {person_tag(person)}\n"
            f"    {progress_bar(proc_day, 2)}\n"
            f"    📊 нарядов за всё время: *{total}*\n"
            f"    ⏭ после — {person_tag(next_n)} через *{next_d} д.*\n\n"
            f"⏭ *завтра пашет:*\n"
            f"    {person_tag(proc_next)} {progress_bar(proc_day_t, 2)}\n\n"
            f"_{ending}_\n{div}"
        )

    try:
        sent = await bot.send_message(
            chat_id, msg_text,
            parse_mode="Markdown",
            reply_markup=confirmation_keyboard(duty_type)
        )
        try:
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except Exception:
            pass

        # личное уведомление
        personal_ids = data.get("personal_ids", {})
        uid = personal_ids.get(person)
        if uid:
            tmpl = random.choice(PERSONAL_SVODKI if duty_type == "svodki" else PERSONAL_PROC)
            personal_text = tmpl.format(name=person)
            try:
                await bot.send_message(uid, personal_text)
            except Exception as e:
                logger.warning(f"личное сообщение {person}: {e}")

        logger.info(f"напоминание [{duty_type}] → {person}")
    except Exception as e:
        logger.error(f"ошибка отправки [{duty_type}]: {e}")


async def send_svodki_reminder(bot: Bot):
    await _send_reminder(bot, "svodki")

async def send_procedura_reminder(bot: Bot):
    await _send_reminder(bot, "proc")

async def send_retry_reminder(bot: Bot, duty_type: str):
    data    = load_data()
    pending = data.get("pending_retry", {})
    today   = date.today().isoformat()
    if pending.get(duty_type) == today:
        await _send_reminder(bot, duty_type, retry=True)
        pending.pop(duty_type, None)
        data["pending_retry"] = pending
        save_data(data)

async def send_monday_briefing(bot: Bot):
    """14 — рассылка в понедельник в 9:00"""
    data    = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try:
        sent = await bot.send_message(chat_id, build_monday_briefing(), parse_mode="Markdown")
        try: await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except: pass
    except Exception as e:
        logger.error(f"понедельничная рассылка: {e}")

async def send_sunday_summary(bot: Bot):
    """15 — итог недели в воскресенье в 20:00"""
    data    = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try:
        await bot.send_message(chat_id, build_sunday_summary(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"воскресный итог: {e}")

# ══════════════════════════════════════════════
#  ХЕНДЛЕРЫ
# ══════════════════════════════════════════════
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 *йоу! я бот-напоминалка нарядов*\n\n"
        "каждый день шлю:\n"
        "• *18:00* — кто тащит сводки\n"
        "• *22:00* — кто убирает процедурку\n"
        "• пн *9:00* — расписание на всю неделю\n"
        "• вс *20:00* — итоги недели + mvp\n\n"
        "нажал *«✅ сделано»* — попал в журнал.\n"
        "не нажал — ну и ладно, но статистика всё знает 👀\n\n"
        "поехали 👇",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def cmd_today(message: types.Message):
    await message.answer(build_duty_message(date.today(), "сегодня"), parse_mode="Markdown")

async def cmd_tomorrow(message: types.Message):
    await message.answer(build_duty_message(date.today() + timedelta(days=1), "завтра"), parse_mode="Markdown")

async def cmd_week(message: types.Message):
    await message.answer(build_week_schedule(), parse_mode="Markdown")

async def cmd_log(message: types.Message):
    await message.answer(build_log_message(), parse_mode="Markdown")

async def cmd_remind_now(message: types.Message):
    await message.answer("🔔 *погнали, отправляю в чат...*", parse_mode="Markdown")
    await _send_reminder(message.bot, "svodki")
    await _send_reminder(message.bot, "proc")

async def cmd_settings(message: types.Message):
    data      = load_data()
    enabled   = "✅ включены" if data.get("reminders_enabled", True) else "❌ выключены"
    chat_txt  = data.get("chat_id", "не настроен")
    sv_now    = get_svodki_person(date.today())
    pr_now    = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em     = PERSON_EMOJI.get(sv_now, "👤")
    pr_em     = PERSON_EMOJI.get(pr_now, "👤")
    await message.answer(
        f"⚙️ *настройки*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 чат: `{chat_txt}`\n"
        f"🔔 напоминания: {enabled}\n"
        f"🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n"
        f"⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n"
        f"🪪 зарегистрировано: *{reg_count}* чел.\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=settings_keyboard(data)
    )

async def cmd_getchatid(message: types.Message):
    await message.answer(
        f"📍 *id этого чата:*\n`{message.chat.id}`\n\nскопируй и вставь в настройки",
        parse_mode="Markdown"
    )

# ── ПОДТВЕРЖДЕНИЕ ──
async def callback_done(callback: types.CallbackQuery):
    duty_type = callback.data.split(":")[1]
    today     = date.today()
    data      = load_data()
    log       = data.setdefault("log", {})
    entry     = log.setdefault(today.isoformat(), {})
    person    = get_svodki_person(today) if duty_type == "svodki" else get_procedura_person(today)
    field     = "svodki" if duty_type == "svodki" else "proc"
    entry[field] = person
    data["log"]  = log
    data.get("pending_retry", {}).pop(duty_type, None)
    save_data(data)
    increment_count(person, field)
    reply = random.choice(DONE_REPLIES)
    await callback.answer(reply, show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)

async def callback_retry(callback: types.CallbackQuery):
    duty_type = callback.data.split(":")[1]
    data      = load_data()
    data.setdefault("pending_retry", {})[duty_type] = date.today().isoformat()
    save_data(data)
    await callback.answer("⏰ окей, напомню через 15 минут", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)

# ── НАСТРОЙКИ ──
async def callback_toggle_reminders(callback: types.CallbackQuery):
    data = load_data()
    data["reminders_enabled"] = not data.get("reminders_enabled", True)
    save_data(data)
    status = "✅ включены" if data["reminders_enabled"] else "❌ выключены"
    await callback.answer(f"напоминания {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(data))

async def callback_set_chat(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📍 *напиши id чата* куда кидать напоминания:\n"
        "узнать: напиши `/chatid` в нужном чате",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_chat_id)
    await callback.answer()

async def process_chat_id(message: types.Message, state: FSMContext):
    chat_id = message.text.strip()
    data    = load_data()
    data["chat_id"] = chat_id
    save_data(data)
    await state.clear()
    await message.answer(f"✅ *чат установлен!*\nid: `{chat_id}`", parse_mode="Markdown", reply_markup=main_keyboard())

# ── РЕГИСТРАЦИЯ ──
async def callback_register_personal(callback: types.CallbackQuery, state: FSMContext):
    names = " / ".join(SVODKI_LIST)
    await callback.message.answer(
        "🪪 *регистрация личных уведомлений*\n\n"
        f"напиши своё имя *точно как в списке*:\n{names}\n\n"
        "буду писать тебе лично в день дежурства 👀",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_register)
    await callback.answer()

async def process_register(message: types.Message, state: FSMContext):
    name      = message.text.strip()
    all_names = list(set(SVODKI_LIST + PROCEDURA_LIST))
    if name not in all_names:
        await message.answer(
            f"❌ *{name}* не найдено в списках\nпроверь написание и попробуй снова",
            parse_mode="Markdown"
        )
        return
    data = load_data()
    data.setdefault("personal_ids", {})[name] = message.from_user.id
    save_data(data)
    em = PERSON_EMOJI.get(name, "👤")
    await state.clear()
    await message.answer(
        f"✅ {em} *{name}* зарегистрирован!\nбуду писать тебе лично в день наряда 🫡",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ── ВЫБОР ДЕЖУРНОГО ──
async def callback_pick_svodki(callback: types.CallbackQuery):
    data        = load_data()
    today       = date.today()
    start       = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days
    actual_idx  = (data["svodki_start_index"] + days_passed) % len(SVODKI_LIST)
    await callback.message.edit_text(
        f"🤷‍♀️ *кто сегодня по сводкам?*\n"
        f"сейчас: {PERSON_EMOJI.get(SVODKI_LIST[actual_idx],'👤')} *{SVODKI_LIST[actual_idx]}*\n\n"
        f"нажми на нужное имя:",
        parse_mode="Markdown",
        reply_markup=svodki_pick_keyboard(actual_idx)
    )
    await callback.answer()

async def callback_pick_procedura(callback: types.CallbackQuery):
    data        = load_data()
    today       = date.today()
    start       = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days
    actual_idx  = (data["procedura_start_index"] + days_passed // 2) % len(PROCEDURA_LIST)
    await callback.message.edit_text(
        f"⚡️ *кто сегодня по процедурке?*\n"
        f"сейчас: {PERSON_EMOJI.get(PROCEDURA_LIST[actual_idx],'👤')} *{PROCEDURA_LIST[actual_idx]}*\n\n"
        f"нажми на нужное имя:",
        parse_mode="Markdown",
        reply_markup=procedura_pick_keyboard(actual_idx)
    )
    await callback.answer()

async def callback_set_svodki_idx(callback: types.CallbackQuery):
    chosen_idx  = int(callback.data.split(":")[1])
    chosen_name = SVODKI_LIST[chosen_idx]
    data        = load_data()
    today       = date.today()
    start       = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days
    data["svodki_start_index"] = (chosen_idx - days_passed) % len(SVODKI_LIST)
    save_data(data)
    em = PERSON_EMOJI.get(chosen_name, "👤")
    await callback.answer(f"✅ сводки сегодня: {em} {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=svodki_pick_keyboard(chosen_idx))

async def callback_set_proc_idx(callback: types.CallbackQuery):
    chosen_idx  = int(callback.data.split(":")[1])
    chosen_name = PROCEDURA_LIST[chosen_idx]
    data        = load_data()
    today       = date.today()
    start       = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days
    data["procedura_start_index"] = (chosen_idx - days_passed // 2) % len(PROCEDURA_LIST)
    save_data(data)
    em = PERSON_EMOJI.get(chosen_name, "👤")
    await callback.answer(f"✅ процедурка сегодня: {em} {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=procedura_pick_keyboard(chosen_idx))

async def callback_show_svodki_list(callback: types.CallbackQuery):
    today   = date.today()
    current = get_svodki_person(today)
    lines   = [f"*👥 наряд по сводкам:*\nсейчас: {PERSON_EMOJI.get(current,'👤')} *{current}*\n"]
    for i, name in enumerate(SVODKI_LIST, 1):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {em} {name}{mark}")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await callback.answer()

async def callback_show_procedura_list(callback: types.CallbackQuery):
    today    = date.today()
    current  = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    lines    = [f"*👥 наряд по процедурке:*\nсейчас: {PERSON_EMOJI.get(current,'👤')} *{current}* (день {proc_day}/2)\n"]
    for i, name in enumerate(PROCEDURA_LIST, 1):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {em} {name} *(2 дня)*{mark}")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await callback.answer()

async def callback_back_settings(callback: types.CallbackQuery):
    data      = load_data()
    enabled   = "✅ включены" if data.get("reminders_enabled", True) else "❌ выключены"
    chat_txt  = data.get("chat_id", "не настроен")
    sv_now    = get_svodki_person(date.today())
    pr_now    = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em     = PERSON_EMOJI.get(sv_now, "👤")
    pr_em     = PERSON_EMOJI.get(pr_now, "👤")
    await callback.message.edit_text(
        f"⚙️ *настройки*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 чат: `{chat_txt}`\n"
        f"🔔 напоминания: {enabled}\n"
        f"🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n"
        f"⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n"
        f"🪪 зарегистрировано: *{reg_count}* чел.\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=settings_keyboard(data)
    )
    await callback.answer()

async def callback_back_main(callback: types.CallbackQuery):
    await callback.message.answer("🏠 главное меню", reply_markup=main_keyboard())
    await callback.answer()

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
async def main():
    bot     = Bot(token=TOKEN)
    storage = MemoryStorage()
    dp      = Dispatcher(storage=storage)

    dp.message.register(cmd_start,       Command("start"))
    dp.message.register(cmd_getchatid,   Command("chatid"))
    dp.message.register(cmd_today,       F.text == "📋 Наряд сегодня")
    dp.message.register(cmd_tomorrow,    F.text == "📅 Наряд завтра")
    dp.message.register(cmd_week,        F.text == "📊 Расписание на неделю")
    dp.message.register(cmd_log,         F.text == "📓 Журнал")
    dp.message.register(cmd_remind_now,  F.text == "🔔 Напомнить сейчас")
    dp.message.register(cmd_settings,    F.text == "⚙️ Настройки")
    dp.message.register(process_chat_id, AdminStates.waiting_chat_id)
    dp.message.register(process_register, AdminStates.waiting_register)

    dp.callback_query.register(callback_done,                F.data.startswith("done:"))
    dp.callback_query.register(callback_retry,               F.data.startswith("retry:"))
    dp.callback_query.register(callback_toggle_reminders,    F.data == "toggle_reminders")
    dp.callback_query.register(callback_set_chat,            F.data == "set_chat")
    dp.callback_query.register(callback_register_personal,   F.data == "register_personal")
    dp.callback_query.register(callback_pick_svodki,         F.data == "pick_svodki")
    dp.callback_query.register(callback_pick_procedura,      F.data == "pick_procedura")
    dp.callback_query.register(callback_set_svodki_idx,      F.data.startswith("set_svodki_idx:"))
    dp.callback_query.register(callback_set_proc_idx,        F.data.startswith("set_proc_idx:"))
    dp.callback_query.register(callback_show_svodki_list,    F.data == "show_svodki_list")
    dp.callback_query.register(callback_show_procedura_list, F.data == "show_procedura_list")
    dp.callback_query.register(callback_back_settings,       F.data == "back_settings")
    dp.callback_query.register(callback_back_main,           F.data == "back_main")

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_svodki_reminder,   CronTrigger(hour=18, minute=0,  timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_procedura_reminder, CronTrigger(hour=22, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_monday_briefing,   CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_sunday_summary,    CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_retry_reminder, "interval", minutes=15, args=[bot, "svodki"])
    scheduler.add_job(send_retry_reminder, "interval", minutes=15, args=[bot, "proc"])
    scheduler.start()

    logger.info("✅ бот запущен. наряды под контролем 🫡")
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        scheduler.shutdown()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
