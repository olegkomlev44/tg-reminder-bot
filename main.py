from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, date, timedelta
import sqlite3
import json
import logging

TOKEN = "8874089866:AAEoGd63Dm2DC6YNSQ29oO2zcUlVNI5zY1Y"

CHAT_ID = -1003782926765          # ← Замени!
ADMIN_IDS = [891298064]           # ← Твой ID

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
    c.execute("INSERT OR IGNORE INTO settings VALUES ('start_date', ?)", ("2026-06-01",))
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

# ================= ИСПРАВЛЕННАЯ ФУНКЦИЯ =================
def set_current_person(list_key: str, person_name: str):
    """Точно ставит выбранного человека на СЕГОДНЯ"""
    people = get_setting(list_key)
    if not people:
        return False, "❌ Список пустой."
    if person_name not in people:
        return False, f"❌ Человек '{person_name}' не найден в списке."
    
    index = people.index(person_name)           # Позиция человека в списке
    today = datetime.now().date()
    
    # Делаем так, чтобы сегодня был именно этот индекс
    # new_start_date = today - index дней
    new_start_date = today - timedelta(days=index)
    
    update_setting('start_date', new_start_date.isoformat())
    
    # Проверка
    days_passed = (today - new_start_date).days
    check_index = days_passed % len(people)
    
    return True, f"✅ <b>{person_name}</b> поставлен на сегодня!\nПозиция в очереди: {index+1}/{len(people)}"

# ================= СООБЩЕНИЕ =================
def get_message():
    today = datetime.now().date()
    start_date = date.fromisoformat(get_setting('start_date'))
    days_passed = (today - start_date).days
    
    svodki = get_setting('svodki')
    procedurka = get_setting('procedurka')
    
    svodki_name = svodki[days_passed % len(svodki)]
    proc_name = procedurka[(days_passed // 2) % len(procedurka)]
    
    return f"""
🚨 <b>НАПОМИНАНИЕ НА СЕГОДНЯ</b> 🚨

📦 <b>Относить сводки</b>
🎖 {svodki_name}

━━━━━━━━━━━━━━━

🧹 <b>Уборка процедурки</b>
🎖 {proc_name}

━━━━━━━━━━━━━━━
📅 {today.strftime('%d.%m.%Y')} | День {days_passed+1}
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
    await update.message.reply_text("👋 Бот готов!\nВыбери действие:", reply_markup=main_menu())

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
            msg = "Отправь:\n`/set_svodki Саша, Олег, Максим, ...`"
        elif query.data == "edit_procedurka":
            msg = "Отправь:\n`/set_procedurka Илья, Слава, Саша, ...`"
        elif query.data == "set_current_svodki":
            msg = "Отправь:\n`/set_current_svodki Максим`"
        else:
            msg = "Отправь:\n`/set_current_procedurka Слава`"
        await query.edit_message_text(msg, parse_mode='Markdown')

# ================= КОМАНДЫ =================
async def set_current_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: `/set_current_svodki Максим`")
        return
    name = ' '.join(context.args).strip()
    success, msg = set_current_person('svodki', name)
    await update.message.reply_text(msg, parse_mode='HTML')

async def set_current_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: `/set_current_procedurka Слава`")
        return
    name = ' '.join(context.args).strip()
    success, msg = set_current_person('procedurka', name)
    await update.message.reply_text(msg, parse_mode='HTML')

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_message(), parse_mode='HTML', reply_markup=main_menu())

# ================= ЗАПУСК =================
def main():
    init_db()
    logging.basicConfig(level=logging.INFO)
    
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("set_current_svodki", set_current_svodki))
    app.add_handler(CommandHandler("set_current_procedurka", set_current_procedurka))
    app.add_handler(CallbackQueryHandler(button_handler))

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        lambda: app.bot.send_message(chat_id=CHAT_ID, text=get_message(), parse_mode='HTML'),
        "cron", hour=18, minute=0
    )
    scheduler.start()

    print("✅ Бот запущен (исправленная логика сдвига)")
    app.run_polling()

if __name__ == "__main__":
    main()
