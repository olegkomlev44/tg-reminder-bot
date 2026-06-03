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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#  СПИСКИ ДЕЖУРНЫХ  (позиция = порядковый №)
# ══════════════════════════════════════════════
SVODKI_LIST    = ["Саша", "Олег", "Максим", "Игорь", "Илья", "Глеба", "Слава", "Ильнар"]
PROCEDURA_LIST = ["Илья", "Слава", "Саша", "Игорь", "Глеба", "Ильнар"]

# Цвета/эмодзи дня недели (14 — оформление, буднии vs выходные)
WEEKDAY_HEADER = {
    0: ("понедельник", "📅"),
    1: ("вторник",     "📅"),
    2: ("среда",       "📅"),
    3: ("четверг",     "📅"),
    4: ("пятница",     "📅"),
    5: ("суббота",     "🌅"),   # выходной
    6: ("воскресенье", "🌅"),   # выходной
}

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
        # 5 — реестр Telegram user_id для личных уведомлений
        "personal_ids":          {},   # {"Саша": 123456789, ...}
        # 4 — журнал подтверждений: {"2024-06-01": {"svodki": "Саша", "proc": null}}
        "log":                   {},
        # 1 — ожидающие повторного напоминания (задачи добавляются планировщиком)
        "pending_retry":         {},   # {"svodki": "2024-06-01", "proc": "2024-06-01"}
    }
    save_data(data)
    return data

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
    """13 — через сколько дней снова очередь name по сводкам"""
    for i in range(1, len(SVODKI_LIST) + 1):
        if get_svodki_person(from_date + timedelta(days=i)) == name:
            return i
    return len(SVODKI_LIST)

def days_until_procedura(name: str, from_date: date) -> int:
    """13 — через сколько дней снова очередь name по процедурке"""
    for i in range(1, len(PROCEDURA_LIST) * 2 + 1):
        if get_procedura_person(from_date + timedelta(days=i)) == name:
            return i
    return len(PROCEDURA_LIST) * 2

# ══════════════════════════════════════════════
#  FSM STATES
# ══════════════════════════════════════════════
class AdminStates(StatesGroup):
    waiting_chat_id    = State()
    waiting_register   = State()   # 5 — регистрация личного ID

# ══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ОФОРМЛЕНИЯ
# ══════════════════════════════════════════════
def is_weekend(d: date) -> bool:
    return d.weekday() >= 5

def date_header(d: date, label: str) -> str:
    """14 — разное оформление для будней и выходных"""
    day_name, icon = WEEKDAY_HEADER[d.weekday()]
    date_str = d.strftime("%d.%m.%Y")
    if is_weekend(d):
        return f"{icon} *{label} — {day_name.upper()}, {date_str}* {icon}"
    return f"{icon} *{label} — {day_name}, {date_str}*"

def svodki_position_label(name: str) -> str:
    """19 — 'Дежурный №2 по сводкам'"""
    idx = SVODKI_LIST.index(name) + 1
    return f"Дежурный №{idx} по сводкам"

def proc_position_label(name: str) -> str:
    """19 — 'Дежурный №3 по процедурке'"""
    idx = PROCEDURA_LIST.index(name) + 1
    return f"Дежурный №{idx} по процедурке"

def divider(weekend: bool = False) -> str:
    return "🌸━━━━━━━━━━━━━━━━🌸" if weekend else "━━━━━━━━━━━━━━━━━━━━"

# ══════════════════════════════════════════════
#  ПОСТРОЕНИЕ СООБЩЕНИЙ
# ══════════════════════════════════════════════
def build_duty_message(target_date: date, label: str) -> str:
    svodki   = get_svodki_person(target_date)
    proc     = get_procedura_person(target_date)
    proc_day = get_duty_day_number(target_date)
    weekend  = is_weekend(target_date)
    div      = divider(weekend)

    # 13 — считаем следующую очередь
    next_sv  = days_until_svodki(svodki, target_date)
    next_pr  = days_until_procedura(proc, target_date)

    # 19 — позиция в списке
    sv_lbl  = svodki_position_label(svodki)
    pr_lbl  = proc_position_label(proc)

    # 14 — особый заголовок выходных
    header  = date_header(target_date, label)
    weekend_note = "\n🏖 *Выходной день*" if weekend else ""

    lines = [
        f"{header}{weekend_note}",
        div,
        "",
        f"🤷‍♀️ *{sv_lbl}:*",
        f"    👤 *{svodki}*",
        f"    🔁 Следующий раз через *{next_sv} д.*",
        "",
        f"⚡️ *{pr_lbl}:*",
        f"    🧹 *{proc}*",
        f"    📆 День наряда: *{proc_day}/2*",
        f"    🔁 Следующий раз через *{next_pr} д.*",
        "",
        div,
    ]
    return "\n".join(lines)

def build_week_schedule() -> str:
    today = date.today()
    lines = ["*📊 Расписание нарядов на 7 дней*\n"]
    short_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for i in range(7):
        d         = today + timedelta(days=i)
        svodki    = get_svodki_person(d)
        proc      = get_procedura_person(d)
        proc_day  = get_duty_day_number(d)
        sv_num    = SVODKI_LIST.index(svodki) + 1
        pr_num    = PROCEDURA_LIST.index(proc) + 1
        weekend   = is_weekend(d)
        wk_icon   = "🌅" if weekend else "📌"
        lbl       = "Сегодня" if i == 0 else ("Завтра" if i == 1 else f"{short_days[d.weekday()]} {d.strftime('%d.%m')}")
        lines.append(
            f"{wk_icon} *{lbl}*\n"
            f"  🤷 №{sv_num} {svodki}\n"
            f"  ⚡ №{pr_num} {proc} ({proc_day}/2)\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)

def build_log_message() -> str:
    """4 — журнал подтверждений за последние 7 дней"""
    data = load_data()
    log  = data.get("log", {})
    lines = ["*📓 Журнал за последние 7 дней*\n"]
    for i in range(6, -1, -1):
        d     = date.today() - timedelta(days=i)
        key   = d.isoformat()
        entry = log.get(key, {})
        sv    = get_svodki_person(d)
        pr    = get_procedura_person(d)
        sv_ok = "✅" if entry.get("svodki") else "❌"
        pr_ok = "✅" if entry.get("proc")   else "❌"
        lbl   = "Сегодня" if i == 0 else d.strftime("%d.%m")
        lines.append(f"📅 *{lbl}*  {sv_ok} {sv}  |  {pr_ok} {pr}")
    return "\n".join(lines)

# ══════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Наряд сегодня"),      KeyboardButton(text="📅 Наряд завтра")],
            [KeyboardButton(text="📊 Расписание на неделю"), KeyboardButton(text="📓 Журнал")],
            [KeyboardButton(text="🔔 Напомнить сейчас"),    KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True
    )

def confirmation_keyboard(duty_type: str) -> InlineKeyboardMarkup:
    """1 — кнопки подтверждения выполнения наряда"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сделано",          callback_data=f"done:{duty_type}"),
            InlineKeyboardButton(text="⏰ Напомни через 15м", callback_data=f"retry:{duty_type}"),
        ]
    ])

def settings_keyboard(data: dict) -> InlineKeyboardMarkup:
    enabled     = data.get("reminders_enabled", True)
    toggle_text = "🔕 Выключить напоминания" if enabled else "🔔 Включить напоминания"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text,                              callback_data="toggle_reminders")],
        [InlineKeyboardButton(text="📍 Установить чат для напоминаний",       callback_data="set_chat")],
        [InlineKeyboardButton(text="👤 Выбрать дежурного — сводки",          callback_data="pick_svodki")],
        [InlineKeyboardButton(text="👤 Выбрать дежурного — процедурка",      callback_data="pick_procedura")],
        [InlineKeyboardButton(text="📝 Сводки — список",                     callback_data="show_svodki_list")],
        [InlineKeyboardButton(text="📝 Процедурка — список",                 callback_data="show_procedura_list")],
        [InlineKeyboardButton(text="🪪 Зарегистрировать личный ID",          callback_data="register_personal")],
        [InlineKeyboardButton(text="◀️ Назад",                               callback_data="back_main")],
    ])

def svodki_pick_keyboard(actual_idx: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, name in enumerate(SVODKI_LIST):
        mark = "✅ " if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"set_svodki_idx:{i}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def procedura_pick_keyboard(actual_idx: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, name in enumerate(PROCEDURA_LIST):
        mark = "✅ " if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"set_proc_idx:{i}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ══════════════════════════════════════════════
#  ОТПРАВКА НАПОМИНАНИЙ
# ══════════════════════════════════════════════
async def _send_reminder(bot: Bot, duty_type: str, retry: bool = False):
    """Универсальная отправка напоминания (18:00 или 22:00, или по кнопке/retry)"""
    data    = load_data()
    if not data.get("reminders_enabled", True):
        return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        logger.warning("CHAT_ID не настроен!")
        return

    today   = datetime.now(TIMEZONE).date()
    weekend = is_weekend(today)
    div     = divider(weekend)

    if duty_type == "svodki":
        person   = get_svodki_person(today)
        sv_num   = SVODKI_LIST.index(person) + 1
        next_d   = days_until_svodki(person, today)
        prefix   = "⏰ *Повторное напоминание*\n" if retry else ""
        header   = f"🔔 *СВОДКИ — 18:00*" if not weekend else "🌅 *СВОДКИ — 18:00* 🌅"
        # 18 — сохраняем message_id для последующего закрепления
        msg_text = (
            f"{prefix}{header}\n{div}\n\n"
            f"🤷‍♀️ *{svodki_position_label(person)}:*\n"
            f"    👤 *{person}*\n"
            f"    🔁 Следующий раз через *{next_d} д.*\n\n"
            f"📋 Не забудь отнести сводки!\n{div}"
        )
    else:
        person      = get_procedura_person(today)
        proc_day    = get_duty_day_number(today)
        tomorrow    = today + timedelta(days=1)
        proc_next   = get_procedura_person(tomorrow)
        proc_day_t  = get_duty_day_number(tomorrow)
        pr_num      = PROCEDURA_LIST.index(person) + 1
        next_d      = days_until_procedura(person, today)
        prefix      = "⏰ *Повторное напоминание*\n" if retry else ""
        header      = f"🔔 *ПРОЦЕДУРКА — 22:00*" if not weekend else "🌅 *ПРОЦЕДУРКА — 22:00* 🌅"
        msg_text    = (
            f"{prefix}{header}\n{div}\n\n"
            f"⚡️ *{proc_position_label(person)}:*\n"
            f"    🧹 *{person}* — день *{proc_day}/2*\n"
            f"    🔁 Следующий раз через *{next_d} д.*\n\n"
            f"⏭ *Завтра уберёт:*\n"
            f"    🧹 *{proc_next}* — день *{proc_day_t}/2*\n{div}"
        )

    try:
        sent = await bot.send_message(
            chat_id, msg_text,
            parse_mode="Markdown",
            reply_markup=confirmation_keyboard(duty_type)   # 1
        )
        # 18 — пробуем закрепить сообщение (нужны права администратора)
        try:
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except Exception:
            pass  # нет прав — молча пропускаем

        # 5 — личное уведомление дежурному
        personal_ids = data.get("personal_ids", {})
        uid = personal_ids.get(person)
        if uid:
            personal_text = (
                f"👋 *{person}*, сегодня твой наряд!\n\n"
                + (f"🤷‍♀️ Сводки — не забудь отнести!\n" if duty_type == "svodki"
                   else f"⚡️ Процедурка — нужно убраться после 22:00\n")
            )
            try:
                await bot.send_message(uid, personal_text, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Не удалось отправить личное сообщение {person} ({uid}): {e}")

        logger.info(f"Напоминание [{duty_type}] отправлено. Дежурный: {person}")
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания [{duty_type}]: {e}")


async def send_svodki_reminder(bot: Bot):
    await _send_reminder(bot, "svodki")

async def send_procedura_reminder(bot: Bot):
    await _send_reminder(bot, "proc")

async def send_retry_reminder(bot: Bot, duty_type: str):
    """1 — повторное напоминание через 15 минут"""
    data = load_data()
    pending = data.get("pending_retry", {})
    today = date.today().isoformat()
    if pending.get(duty_type) == today:
        await _send_reminder(bot, duty_type, retry=True)
        pending.pop(duty_type, None)
        data["pending_retry"] = pending
        save_data(data)

# ══════════════════════════════════════════════
#  ХЕНДЛЕРЫ — КОМАНДЫ И КНОПКИ
# ══════════════════════════════════════════════
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 *Привет! Я бот-напоминалка о нарядах!*\n\n"
        "⏰ Каждый день отправляю:\n"
        "• *18:00* — кто несёт сводки\n"
        "• *22:00* — кто убирает процедурку\n\n"
        "После каждого напоминания можно нажать *«✅ Сделано»* — "
        "всё фиксируется в журнале.\n\n"
        "Выбери действие 👇",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def cmd_today(message: types.Message):
    await message.answer(build_duty_message(date.today(), "Сегодня"), parse_mode="Markdown")

async def cmd_tomorrow(message: types.Message):
    await message.answer(build_duty_message(date.today() + timedelta(days=1), "Завтра"), parse_mode="Markdown")

async def cmd_week(message: types.Message):
    await message.answer(build_week_schedule(), parse_mode="Markdown")

async def cmd_log(message: types.Message):
    """4 — журнал подтверждений"""
    await message.answer(build_log_message(), parse_mode="Markdown")

async def cmd_remind_now(message: types.Message):
    """7 — ручная отправка напоминания прямо сейчас"""
    await message.answer("🔔 Отправляю напоминание в чат...", parse_mode="Markdown")
    bot = message.bot
    await _send_reminder(bot, "svodki")
    await _send_reminder(bot, "proc")

async def cmd_settings(message: types.Message):
    data = load_data()
    enabled  = "✅ Включены" if data.get("reminders_enabled", True) else "❌ Выключены"
    chat_txt = data.get("chat_id", "не настроен")
    sv_now   = get_svodki_person(date.today())
    pr_now   = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    await message.answer(
        f"⚙️ *Настройки бота*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Чат: `{chat_txt}`\n"
        f"🔔 Напоминания: {enabled}\n"
        f"🤷‍♀️ Сводки сегодня: *{sv_now}*\n"
        f"⚡️ Процедурка сегодня: *{pr_now}*\n"
        f"🪪 Зарегистрировано: *{reg_count}* чел.\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=settings_keyboard(data)
    )

async def cmd_getchatid(message: types.Message):
    await message.answer(
        f"📍 *ID этого чата:*\n`{message.chat.id}`\n\n"
        f"Скопируй и вставь в настройки бота.",
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════
#  CALLBACK — ПОДТВЕРЖДЕНИЕ (улучшение 1)
# ══════════════════════════════════════════════
async def callback_done(callback: types.CallbackQuery):
    """1 — пользователь нажал «Сделано»"""
    duty_type = callback.data.split(":")[1]   # svodki | proc
    today     = date.today()
    data      = load_data()
    log       = data.setdefault("log", {})
    entry     = log.setdefault(today.isoformat(), {})

    person = get_svodki_person(today) if duty_type == "svodki" else get_procedura_person(today)
    field  = "svodki" if duty_type == "svodki" else "proc"
    entry[field] = person
    data["log"]  = log

    # убираем pending retry если был
    data.get("pending_retry", {}).pop(duty_type, None)
    save_data(data)

    label = "Сводки отнесены" if duty_type == "svodki" else "Процедурка убрана"
    await callback.answer(f"✅ {label}! Записал в журнал.", show_alert=False)
    # Убираем кнопки с сообщения
    await callback.message.edit_reply_markup(reply_markup=None)

async def callback_retry(callback: types.CallbackQuery):
    """1 — запросить повторное напоминание через 15 минут"""
    duty_type = callback.data.split(":")[1]
    data      = load_data()
    data.setdefault("pending_retry", {})[duty_type] = date.today().isoformat()
    save_data(data)
    await callback.answer("⏰ Напомню через 15 минут!", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)

# ══════════════════════════════════════════════
#  CALLBACK — НАСТРОЙКИ
# ══════════════════════════════════════════════
async def callback_toggle_reminders(callback: types.CallbackQuery):
    data = load_data()
    data["reminders_enabled"] = not data.get("reminders_enabled", True)
    save_data(data)
    status = "✅ включены" if data["reminders_enabled"] else "❌ выключены"
    await callback.answer(f"Напоминания {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(data))

async def callback_set_chat(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📍 *Введи ID чата* куда слать напоминания:\n"
        "Узнать ID: напиши `/chatid` в нужном чате.",
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
    await message.answer(f"✅ *Чат установлен!*\nID: `{chat_id}`", parse_mode="Markdown", reply_markup=main_keyboard())

# ══════════════════════════════════════════════
#  CALLBACK — РЕГИСТРАЦИЯ ЛИЧНОГО ID (улучшение 5)
# ══════════════════════════════════════════════
async def callback_register_personal(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🪪 *Регистрация личного уведомления*\n\n"
        "Напиши своё имя *точно как в списке*:\n"
        "Саша / Олег / Максим / Игорь / Илья / Глеба / Слава / Ильнар\n\n"
        "После этого бот будет писать тебе лично в день дежурства.",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_register)
    await callback.answer()

async def process_register(message: types.Message, state: FSMContext):
    name    = message.text.strip()
    all_names = SVODKI_LIST + [n for n in PROCEDURA_LIST if n not in SVODKI_LIST]
    if name not in all_names:
        await message.answer(
            f"❌ Имя *{name}* не найдено в списках.\n"
            f"Проверь написание и попробуй снова.",
            parse_mode="Markdown"
        )
        return
    data = load_data()
    data.setdefault("personal_ids", {})[name] = message.from_user.id
    save_data(data)
    await state.clear()
    await message.answer(
        f"✅ *{name}*, ты зарегистрирован!\n"
        f"Буду писать тебе лично в день дежурства 🫡",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ══════════════════════════════════════════════
#  CALLBACK — ВЫБОР ДЕЖУРНОГО
# ══════════════════════════════════════════════
async def callback_pick_svodki(callback: types.CallbackQuery):
    data        = load_data()
    today       = date.today()
    start       = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days
    actual_idx  = (data["svodki_start_index"] + days_passed) % len(SVODKI_LIST)
    await callback.message.edit_text(
        f"🤷‍♀️ *Кто дежурит по сводкам СЕГОДНЯ?*\n\n"
        f"Сейчас: *{SVODKI_LIST[actual_idx]}* — нажми на нужное имя:",
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
        f"⚡️ *Кто дежурит по процедурке СЕГОДНЯ?*\n\n"
        f"Сейчас: *{PROCEDURA_LIST[actual_idx]}* — нажми на нужное имя:",
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
    await callback.answer(f"✅ Сводки сегодня: {chosen_name}", show_alert=True)
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
    await callback.answer(f"✅ Процедурка сегодня: {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=procedura_pick_keyboard(chosen_idx))

async def callback_show_svodki_list(callback: types.CallbackQuery):
    today   = date.today()
    current = get_svodki_person(today)
    lines   = [f"*👥 Наряд по сводкам:*\nСейчас: *{current}*\n"]
    for i, name in enumerate(SVODKI_LIST, 1):
        mark = " ◀️" if name == current else ""
        lines.append(f"{i}️⃣ {name}{mark}")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await callback.answer()

async def callback_show_procedura_list(callback: types.CallbackQuery):
    today    = date.today()
    current  = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    lines    = [f"*👥 Наряд по процедурке:*\nСейчас: *{current}* (день {proc_day}/2)\n"]
    for i, name in enumerate(PROCEDURA_LIST, 1):
        mark = " ◀️" if name == current else ""
        lines.append(f"{i}️⃣ {name} *(2 дня подряд)*{mark}")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await callback.answer()

async def callback_back_settings(callback: types.CallbackQuery):
    data     = load_data()
    enabled  = "✅ Включены" if data.get("reminders_enabled", True) else "❌ Выключены"
    chat_txt = data.get("chat_id", "не настроен")
    sv_now   = get_svodki_person(date.today())
    pr_now   = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    await callback.message.edit_text(
        f"⚙️ *Настройки бота*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Чат: `{chat_txt}`\n"
        f"🔔 Напоминания: {enabled}\n"
        f"🤷‍♀️ Сводки сегодня: *{sv_now}*\n"
        f"⚡️ Процедурка сегодня: *{pr_now}*\n"
        f"🪪 Зарегистрировано: *{reg_count}* чел.\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=settings_keyboard(data)
    )
    await callback.answer()

async def callback_back_main(callback: types.CallbackQuery):
    await callback.message.answer("🏠 Главное меню", reply_markup=main_keyboard())
    await callback.answer()

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
async def main():
    bot     = Bot(token=TOKEN)
    storage = MemoryStorage()
    dp      = Dispatcher(storage=storage)

    # ── Команды / кнопки ──
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

    # ── Callbacks ──
    dp.callback_query.register(callback_done,                 F.data.startswith("done:"))
    dp.callback_query.register(callback_retry,                F.data.startswith("retry:"))
    dp.callback_query.register(callback_toggle_reminders,     F.data == "toggle_reminders")
    dp.callback_query.register(callback_set_chat,             F.data == "set_chat")
    dp.callback_query.register(callback_register_personal,    F.data == "register_personal")
    dp.callback_query.register(callback_pick_svodki,          F.data == "pick_svodki")
    dp.callback_query.register(callback_pick_procedura,       F.data == "pick_procedura")
    dp.callback_query.register(callback_set_svodki_idx,       F.data.startswith("set_svodki_idx:"))
    dp.callback_query.register(callback_set_proc_idx,         F.data.startswith("set_proc_idx:"))
    dp.callback_query.register(callback_show_svodki_list,     F.data == "show_svodki_list")
    dp.callback_query.register(callback_show_procedura_list,  F.data == "show_procedura_list")
    dp.callback_query.register(callback_back_settings,        F.data == "back_settings")
    dp.callback_query.register(callback_back_main,            F.data == "back_main")

    # ── Планировщик ──
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # Основные напоминания
    scheduler.add_job(send_svodki_reminder,   CronTrigger(hour=18, minute=0,  timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_procedura_reminder, CronTrigger(hour=22, minute=0, timezone=TIMEZONE), args=[bot])

    # 1 — повторные напоминания: каждые 15 минут проверяем очередь
    scheduler.add_job(
        send_retry_reminder,
        "interval", minutes=15,
        args=[bot, "svodki"]
    )
    scheduler.add_job(
        send_retry_reminder,
        "interval", minutes=15,
        args=[bot, "proc"]
    )

    scheduler.start()
    logger.info("✅ Бот запущен! Напоминания: 18:00 и 22:00 МСК")

    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        scheduler.shutdown()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
