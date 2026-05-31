from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, date, timedelta
import sqlite3
import json
import logging

# ================= НАСТРОЙКИ =================
TOKEN = "8874089866:AAEoGd63Dm2DC6YNSQ29oO2zcUlVNI5zY1Y"  # Замените на свой токен
CHAT_ID = -1003782926765          # ← Замените на ID вашей группы
ADMIN_IDS = [891298064]           # ← Ваш Telegram ID

# ================= БАЗА ДАННЫХ =================
def init_db():
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')

    default_svodki = ["Саша", "Олег", "Максим", "Игорь", "Илья", "Глеба", "Слава", "Ильнар"]
    default_procedurka = ["Илья", "Слава", "Саша", "Игоооорь", "Глеба", "Ильнар"]

    c.execute("INSERT OR IGNORE INTO settings VALUES ('svodki', ?)", (json.dumps(default_svodki),))
    c.execute("INSERT OR IGNORE INTO settings VALUES ('procedurka', ?)", (json.dumps(default_procedurka),))
    c.execute("INSERT OR IGNORE INTO settings VALUES ('svodki_start_date', ?)", ("2026-06-01",))
    c.execute("INSERT OR IGNORE INTO settings VALUES ('procedurka_start_date', ?)", ("2026-06-01",))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    result = c.fetchone()
    conn.close()
    if key in ['svodki', 'procedurka']:
        return json.loads(result[0]) if result else []
    return result[0] if result else None

def update_setting(key, value):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    if key in ['svodki', 'procedurka']:
        value = json.dumps(value)
    c.execute("REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# ================= ЛОГИКА УСТАНОВКИ КОНКРЕТНОГО ЧЕЛОВЕКА =================
def set_current_person(list_key: str, start_date_key: str, person_name: str):
    """Ставит выбранного человека на сегодня для конкретного списка"""
    people = get_setting(list_key)
    if not people:
        return False, "❌ Список пустой."
    if person_name not in people:
        return False, f"❌ Человек '{person_name}' не найден в списке."

    index = people.index(person_name)           # позиция в списке (0-based)
    today = datetime.now().date()
    # Новый start_date = сегодня минус index дней
    new_start_date = today - timedelta(days=index)

    update_setting(start_date_key, new_start_date.isoformat())

    # Проверка (опционально)
    days_passed = (today - new_start_date).days
    check_index = days_passed % len(people)
    return True, f"✅ <b>{person_name}</b> поставлен на сегодня!\nПозиция в очереди: {index+1}/{len(people)}"

# ================= ФОРМИРОВАНИЕ СООБЩЕНИЯ /today =================
def get_message():
    today = datetime.now().date()

    svodki_start = date.fromisoformat(get_setting('svodki_start_date'))
    procedurka_start = date.fromisoformat(get_setting('procedurka_start_date'))

    days_svodki = (today - svodki_start).days
    days_procedurka = (today - procedurka_start).days

    svodki = get_setting('svodki')
    procedurka = get_setting('procedurka')

    svodki_name = svodki[days_svodki % len(svodki)]
    proc_name = procedurka[days_procedurka % len(procedurka)]   # больше нет //2

    return f"""
🚨 <b>НАПОМИНАНИЕ НА СЕГОДНЯ</b> 🚨

📦 <b>Относить сводки</b>
🎖 {svodki_name}

━━━━━━━━━━━━━━━

🧹 <b>Уборка процедурки</b>
🎖 {proc_name}

━━━━━━━━━━━━━━━
📅 {today.strftime('%d.%m.%Y')}
"""

# ================= КЛАВИАТУРЫ =================
def main_menu():
    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("📋 Показать списки", callback_data="list")],
        [InlineKeyboardButton("✏️ Изменить сводки", callback_data="edit_svodki")],
        [InlineKeyboardButton("🧹 Изменить уборку", callback_data="edit_procedurka")],
        [InlineKeyboardButton("🎯 Сводки → на человека", callback_data="set_current_svodki")],
        [InlineKeyboardButton("🎯 Уборка → на человека", callback_data="set_current_procedurka")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ================= ОБРАБОТЧИКИ =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Бот готов!\nВыберите действие:", reply_markup=main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "today":
        await query.edit_message_text(get_message(), parse_mode='HTML', reply_markup=main_menu())

    elif query.data == "list":
        svodki = get_setting('svodki')
        procedurka = get_setting('procedurka')
        text = f"""<b>Текущие списки:</b>

📦 Сводки:
{chr(10).join([f"{i+1}. {name}" for i, name in enumerate(svodki)])}

🧹 Уборка:
{chr(10).join([f"{i+1}. {name}" for i, name in enumerate(procedurka)])}"""
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=main_menu())

    elif query.data in ["edit_svodki", "edit_procedurka", "set_current_svodki", "set_current_procedurka"]:
        if query.data == "edit_svodki":
            msg = "Отправьте команду:\n`/set_svodki Саша, Олег, Максим, ...`"
        elif query.data == "edit_procedurka":
            msg = "Отправьте команду:\n`/set_procedurka Илья, Слава, Саша, ...`"
        elif query.data == "set_current_svodki":
            msg = "Отправьте команду:\n`/set_current_svodki Максим`"
        else:  # set_current_procedurka
            msg = "Отправьте команду:\n`/set_current_procedurka Игоооорь`"
        await query.edit_message_text(msg, parse_mode='Markdown')

# ================= КОМАНДЫ =================
async def set_current_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_current_svodki Максим")
        return
    name = ' '.join(context.args).strip()
    success, msg = set_current_person('svodki', 'svodki_start_date', name)
    await update.message.reply_text(msg, parse_mode='HTML')

async def set_current_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_current_procedurka Игоооорь")
        return
    name = ' '.join(context.args).strip()
    success, msg = set_current_person('procedurka', 'procedurka_start_date', name)
    await update.message.reply_text(msg, parse_mode='HTML')

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_message(), parse_mode='HTML', reply_markup=main_menu())

# Опциональные команды для редактирования списков (если хотите)
async def set_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_svodki Саша, Олег, Максим, Игорь")
        return
    text = ' '.join(context.args)
    names = [name.strip() for name in text.split(',') if name.strip()]
    if not names:
        await update.message.reply_text("Список не может быть пустым.")
        return
    update_setting('svodki', names)
    # Сбросим start_date для сводок, чтобы сегодняшний день соответствовал первому в списке
    today = datetime.now().date()
    update_setting('svodki_start_date', today.isoformat())
    await update.message.reply_text(f"✅ Список сводок обновлён: {', '.join(names)}")

async def set_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_procedurka Илья, Слава, Саша, ...")
        return
    text = ' '.join(context.args)
    names = [name.strip() for name in text.split(',') if name.strip()]
    if not names:
        await update.message.reply_text("Список не может быть пустым.")
        return
    update_setting('procedurka', names)
    # Сбросим start_date для уборки
    today = datetime.now().date()
    update_setting('procedurka_start_date', today.isoformat())
    await update.message.reply_text(f"✅ Список уборки обновлён: {', '.join(names)}")

# ================= ЗАПУСК =================
def main():
    init_db()
    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("set_current_svodki", set_current_svodki))
    app.add_handler(CommandHandler("set_current_procedurka", set_current_procedurka))
    app.add_handler(CommandHandler("set_svodki", set_svodki))          # опционально
    app.add_handler(CommandHandler("set_procedurka", set_procedurka))  # опционально
    app.add_handler(CallbackQueryHandler(button_handler))

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        lambda: app.bot.send_message(chat_id=CHAT_ID, text=get_message(), parse_mode='HTML'),
        "cron", hour=18, minute=0
    )
    scheduler.start()

    print("✅ Бот запущен (исправленная логика: независимые очереди и установка конкретного человека)")
    app.run_polling()

if __name__ == "__main__":
    main()
