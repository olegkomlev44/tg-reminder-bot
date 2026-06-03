import asyncio
import logging
import json
import os
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

# ─────────────────────────────────────────────
# КОНФИГУРАЦИЯ
# ─────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.getenv("CHAT_ID", "YOUR_CHAT_ID_HERE")
DATA_FILE = "duty_data.json"
TIMEZONE = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# СПИСКИ ДЕЖУРНЫХ
# ─────────────────────────────────────────────
SVODKI_LIST = ["Саша", "Олег", "Максим", "Игорь", "Илья", "Глеба", "Слава", "Ильнар"]
PROCEDURA_LIST = ["Илья", "Слава", "Саша", "Игорь", "Глеба", "Ильнар"]

# ─────────────────────────────────────────────
# ХРАНИЛИЩЕ ДАННЫХ
# ─────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    today_str = date.today().isoformat()
    data = {
        "start_date": today_str,
        "svodki_start_index": 0,
        "procedura_start_index": 0,
        "chat_id": CHAT_ID,
        "reminders_enabled": True
    }
    save_data(data)
    return data

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────────
# ЛОГИКА НАРЯДОВ
# ─────────────────────────────────────────────
def get_svodki_person(target_date: date) -> str:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    days_passed = (target_date - start).days
    idx = (data["svodki_start_index"] + days_passed) % len(SVODKI_LIST)
    return SVODKI_LIST[idx]

def get_procedura_person(target_date: date) -> str:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    days_passed = (target_date - start).days
    slot = (data["procedura_start_index"] + days_passed // 2) % len(PROCEDURA_LIST)
    return PROCEDURA_LIST[slot]

def get_duty_day_number(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    days_passed = (target_date - start).days
    return (days_passed % 2) + 1

# ─────────────────────────────────────────────
# FSM STATES
# ─────────────────────────────────────────────
class AdminStates(StatesGroup):
    waiting_chat_id = State()

# ─────────────────────────────────────────────
# КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Наряд сегодня"), KeyboardButton(text="📅 Наряд завтра")],
            [KeyboardButton(text="📊 Расписание на неделю"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True
    )

def settings_keyboard(data):
    enabled = data.get("reminders_enabled", True)
    toggle_text = "🔕 Выключить напоминания" if enabled else "🔔 Включить напоминания"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="toggle_reminders")],
        [InlineKeyboardButton(text="📍 Установить чат для напоминаний", callback_data="set_chat")],
        [InlineKeyboardButton(text="👤 Выбрать, кто сейчас дежурит сводки", callback_data="pick_svodki")],
        [InlineKeyboardButton(text="👤 Выбрать, кто сейчас дежурит процедурку", callback_data="pick_procedura")],
        [InlineKeyboardButton(text="📝 Сводки — список", callback_data="show_svodki_list")],
        [InlineKeyboardButton(text="📝 Процедурка — список", callback_data="show_procedura_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])

def svodki_pick_keyboard(current_idx: int):
    """Клавиатура выбора стартового человека для сводок"""
    buttons = []
    row = []
    for i, name in enumerate(SVODKI_LIST):
        mark = "✅ " if i == current_idx else ""
        btn = InlineKeyboardButton(
            text=f"{mark}{name}",
            callback_data=f"set_svodki_idx:{i}"
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def procedura_pick_keyboard(current_idx: int):
    """Клавиатура выбора стартового человека для процедурки"""
    buttons = []
    row = []
    for i, name in enumerate(PROCEDURA_LIST):
        mark = "✅ " if i == current_idx else ""
        btn = InlineKeyboardButton(
            text=f"{mark}{name}",
            callback_data=f"set_proc_idx:{i}"
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─────────────────────────────────────────────
# ФОРМИРОВАНИЕ СООБЩЕНИЙ
# ─────────────────────────────────────────────
def build_duty_message(target_date: date, label: str) -> str:
    svodki = get_svodki_person(target_date)
    procedura = get_procedura_person(target_date)
    proc_day = get_duty_day_number(target_date)
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    day_name = day_names[target_date.weekday()]
    date_str = target_date.strftime("%d.%m.%Y")
    return (
        f"📅 *{label} — {day_name}, {date_str}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤷‍♀️ *Относит сводки:*\n"
        f"    👤 {svodki}\n\n"
        f"⚡️ *Уборка процедурки:*\n"
        f"    🧹 {procedura}\n"
        f"    📆 День наряда: *{proc_day}/2*\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

def build_week_schedule() -> str:
    today = date.today()
    lines = ["*📊 Расписание на 7 дней:*\n━━━━━━━━━━━━━━━━━━━━\n"]
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for i in range(7):
        d = today + timedelta(days=i)
        svodki = get_svodki_person(d)
        procedura = get_procedura_person(d)
        proc_day = get_duty_day_number(d)
        label = "Сегодня" if i == 0 else ("Завтра" if i == 1 else f"{day_names[d.weekday()]} {d.strftime('%d.%m')}")
        lines.append(
            f"📌 *{label}*\n"
            f"  🤷 Сводки: {svodki}\n"
            f"  ⚡ Процедурка: {procedura} ({proc_day}/2)\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)

# ─────────────────────────────────────────────
# НАПОМИНАНИЯ
# ─────────────────────────────────────────────
async def send_svodki_reminder(bot: Bot):
    data = load_data()
    if not data.get("reminders_enabled", True):
        return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        return
    today = datetime.now(TIMEZONE).date()
    svodki = get_svodki_person(today)
    msg = (
        f"🔔 *НАПОМИНАНИЕ — 18:00*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤷‍♀️ *Сводки несёт:*\n"
        f"    👤 *{svodki}*\n\n"
        f"📋 Не забудь отнести сводки!\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        await bot.send_message(chat_id, msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка отправки сводки: {e}")

async def send_procedura_reminder(bot: Bot):
    data = load_data()
    if not data.get("reminders_enabled", True):
        return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        return
    today = datetime.now(TIMEZONE).date()
    procedura = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    tomorrow = today + timedelta(days=1)
    procedura_tomorrow = get_procedura_person(tomorrow)
    proc_day_tomorrow = get_duty_day_number(tomorrow)
    msg = (
        f"🔔 *НАПОМИНАНИЕ — 22:00*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡️ *Уборка процедурки сегодня:*\n"
        f"    🧹 *{procedura}* (день {proc_day}/2)\n\n"
        f"⏭ *Завтра уберёт:*\n"
        f"    🧹 *{procedura_tomorrow}* (день {proc_day_tomorrow}/2)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        await bot.send_message(chat_id, msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка отправки процедурки: {e}")

# ─────────────────────────────────────────────
# ХЕНДЛЕРЫ — КОМАНДЫ
# ─────────────────────────────────────────────
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 *Привет! Я бот-напоминалка о нарядах!*\n\n"
        "⏰ Каждый день я буду напоминать:\n"
        "• В *18:00* — кто несёт сводки\n"
        "• В *22:00* — кто убирает процедурку\n\n"
        "Выбери действие на клавиатуре ниже 👇",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def cmd_today(message: types.Message):
    await message.answer(build_duty_message(date.today(), "Сегодня"), parse_mode="Markdown")

async def cmd_tomorrow(message: types.Message):
    await message.answer(build_duty_message(date.today() + timedelta(days=1), "Завтра"), parse_mode="Markdown")

async def cmd_week(message: types.Message):
    await message.answer(build_week_schedule(), parse_mode="Markdown")

async def cmd_settings(message: types.Message):
    data = load_data()
    chat_id_display = data.get("chat_id", "не настроен")
    enabled = "✅ Включены" if data.get("reminders_enabled", True) else "❌ Выключены"
    svodki_now = get_svodki_person(date.today())
    proc_now = get_procedura_person(date.today())
    await message.answer(
        f"⚙️ *Настройки бота*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Чат: `{chat_id_display}`\n"
        f"🔔 Напоминания: {enabled}\n"
        f"🤷‍♀️ Сводки сегодня: *{svodki_now}*\n"
        f"⚡️ Процедурка сегодня: *{proc_now}*\n"
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

# ─────────────────────────────────────────────
# ХЕНДЛЕРЫ — CALLBACK (НАСТРОЙКИ)
# ─────────────────────────────────────────────
async def callback_toggle_reminders(callback: types.CallbackQuery):
    data = load_data()
    data["reminders_enabled"] = not data.get("reminders_enabled", True)
    save_data(data)
    status = "✅ включены" if data["reminders_enabled"] else "❌ выключены"
    await callback.answer(f"Напоминания {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(data))

async def callback_set_chat(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📍 *Введи ID чата* куда слать напоминания:\n\n"
        "Чтобы узнать ID — напиши боту `/chatid` в нужном чате.",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_chat_id)
    await callback.answer()

async def process_chat_id(message: types.Message, state: FSMContext):
    chat_id = message.text.strip()
    data = load_data()
    data["chat_id"] = chat_id
    save_data(data)
    await state.clear()
    await message.answer(
        f"✅ *Чат установлен!*\nID: `{chat_id}`",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ─────────────────────────────────────────────
# ХЕНДЛЕРЫ — ВЫБОР СТАРТОВОГО ЧЕЛОВЕКА
# ─────────────────────────────────────────────
async def callback_pick_svodki(callback: types.CallbackQuery):
    """Показать меню выбора — кто сейчас дежурит по сводкам"""
    data = load_data()
    current_idx = data.get("svodki_start_index", 0)
    # Вычисляем, кто реально сейчас на очереди
    today = date.today()
    start = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days
    actual_idx = (current_idx + days_passed) % len(SVODKI_LIST)

    await callback.message.edit_text(
        f"🤷‍♀️ *Выбери, кто дежурит по сводкам СЕГОДНЯ:*\n\n"
        f"Сейчас выбран: *{SVODKI_LIST[actual_idx]}*\n\n"
        f"Нажми на имя чтобы сделать его дежурным сегодня — "
        f"остальные дни пересчитаются автоматически.",
        parse_mode="Markdown",
        reply_markup=svodki_pick_keyboard(actual_idx)
    )
    await callback.answer()

async def callback_pick_procedura(callback: types.CallbackQuery):
    """Показать меню выбора — кто сейчас дежурит по процедурке"""
    data = load_data()
    today = date.today()
    start = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days
    current_slot_idx = (data.get("procedura_start_index", 0) + days_passed // 2) % len(PROCEDURA_LIST)

    await callback.message.edit_text(
        f"⚡️ *Выбери, кто дежурит по процедурке СЕГОДНЯ:*\n\n"
        f"Сейчас выбран: *{PROCEDURA_LIST[current_slot_idx]}*\n\n"
        f"Нажми на имя — он станет дежурным с сегодняшнего дня. "
        f"Каждый дежурит 2 дня подряд.",
        parse_mode="Markdown",
        reply_markup=procedura_pick_keyboard(current_slot_idx)
    )
    await callback.answer()

async def callback_set_svodki_idx(callback: types.CallbackQuery):
    """Установить выбранного человека дежурным по сводкам сегодня"""
    chosen_idx = int(callback.data.split(":")[1])
    chosen_name = SVODKI_LIST[chosen_idx]

    data = load_data()
    today = date.today()
    start = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days

    # Вычисляем нужный start_index так, чтобы сегодня был chosen_idx
    # (start_index + days_passed) % len = chosen_idx
    # start_index = (chosen_idx - days_passed) % len
    new_start = (chosen_idx - days_passed) % len(SVODKI_LIST)
    data["svodki_start_index"] = new_start
    save_data(data)

    await callback.answer(f"✅ Сегодня сводки несёт {chosen_name}!", show_alert=True)
    # Обновляем клавиатуру — галочка переедет
    await callback.message.edit_reply_markup(reply_markup=svodki_pick_keyboard(chosen_idx))

async def callback_set_proc_idx(callback: types.CallbackQuery):
    """Установить выбранного человека дежурным по процедурке сегодня"""
    chosen_idx = int(callback.data.split(":")[1])
    chosen_name = PROCEDURA_LIST[chosen_idx]

    data = load_data()
    today = date.today()
    start = date.fromisoformat(data["start_date"])
    days_passed = (today - start).days

    # slot = (proc_start + days_passed // 2) % len = chosen_idx
    # proc_start = (chosen_idx - days_passed // 2) % len
    new_start = (chosen_idx - days_passed // 2) % len(PROCEDURA_LIST)
    data["procedura_start_index"] = new_start
    save_data(data)

    await callback.answer(f"✅ Сегодня процедурку убирает {chosen_name}!", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=procedura_pick_keyboard(chosen_idx))

async def callback_show_svodki_list(callback: types.CallbackQuery):
    today = date.today()
    current = get_svodki_person(today)
    lines = [f"*👥 Наряд по сводкам:*\nСейчас дежурит: *{current}*\n"]
    for i, name in enumerate(SVODKI_LIST, 1):
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {name}{mark}")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await callback.answer()

async def callback_show_procedura_list(callback: types.CallbackQuery):
    today = date.today()
    current = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    lines = [f"*👥 Наряд по процедурке:*\nСейчас дежурит: *{current}* (день {proc_day}/2)\n"]
    for i, name in enumerate(PROCEDURA_LIST, 1):
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {name} *(2 дня подряд)*{mark}")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await callback.answer()

async def callback_back_settings(callback: types.CallbackQuery):
    data = load_data()
    chat_id_display = data.get("chat_id", "не настроен")
    enabled = "✅ Включены" if data.get("reminders_enabled", True) else "❌ Выключены"
    svodki_now = get_svodki_person(date.today())
    proc_now = get_procedura_person(date.today())
    await callback.message.edit_text(
        f"⚙️ *Настройки бота*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Чат: `{chat_id_display}`\n"
        f"🔔 Напоминания: {enabled}\n"
        f"🤷‍♀️ Сводки сегодня: *{svodki_now}*\n"
        f"⚡️ Процедурка сегодня: *{proc_now}*\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=settings_keyboard(data)
    )
    await callback.answer()

async def callback_back_main(callback: types.CallbackQuery):
    await callback.message.answer("🏠 Главное меню", reply_markup=main_keyboard())
    await callback.answer()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    bot = Bot(token=TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Команды
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_getchatid, Command("chatid"))
    dp.message.register(cmd_today, F.text == "📋 Наряд сегодня")
    dp.message.register(cmd_tomorrow, F.text == "📅 Наряд завтра")
    dp.message.register(cmd_week, F.text == "📊 Расписание на неделю")
    dp.message.register(cmd_settings, F.text == "⚙️ Настройки")
    dp.message.register(process_chat_id, AdminStates.waiting_chat_id)

    # Callbacks — настройки
    dp.callback_query.register(callback_toggle_reminders, F.data == "toggle_reminders")
    dp.callback_query.register(callback_set_chat, F.data == "set_chat")
    dp.callback_query.register(callback_show_svodki_list, F.data == "show_svodki_list")
    dp.callback_query.register(callback_show_procedura_list, F.data == "show_procedura_list")
    dp.callback_query.register(callback_back_main, F.data == "back_main")
    dp.callback_query.register(callback_back_settings, F.data == "back_settings")

    # Callbacks — выбор дежурного
    dp.callback_query.register(callback_pick_svodki, F.data == "pick_svodki")
    dp.callback_query.register(callback_pick_procedura, F.data == "pick_procedura")
    dp.callback_query.register(callback_set_svodki_idx, F.data.startswith("set_svodki_idx:"))
    dp.callback_query.register(callback_set_proc_idx, F.data.startswith("set_proc_idx:"))

    # Планировщик
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_svodki_reminder, CronTrigger(hour=18, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_procedura_reminder, CronTrigger(hour=22, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.start()
    logger.info("✅ Бот запущен! Напоминания: 18:00 и 22:00 МСК")

    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        scheduler.shutdown()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
