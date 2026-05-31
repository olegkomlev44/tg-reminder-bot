from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, date, timedelta
import sqlite3
import json
import logging

TOKEN = "8874089866:AAEoGd63Dm2DC6YNSQ29oO2zcUlVNI5zY1Y"

CHAT_ID = -1001234567890          # ← Замени!
ADMIN_IDS = [123456789]           # ← Добавь свой ID

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
        [InlineKeyboardButton("📆 Сдвинуть очередь", callback_data="offset")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ================= ОБРАБОТЧИКИ =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Бот-напоминалка готов!\nВыбери действие:", reply_markup=main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "today":
        await query.edit_message_text(get_message(), parse_mode='HTML', reply_markup=main_menu())
    
    elif query.data == "list":
        svodki = get_setting('svodki')
        procedurka = get_setting('procedurka')
        start_date = get_setting('start_date')
        
        text = f"""<b>Текущие списки:</b>

📦 <b>Сводки</b> ({len(svodki)} чел.):
{chr(10).join([f"{i+1}. {name}" for i, name in enumerate(svodki)])}

🧹 <b>Уборка</b> ({len(procedurka)} чел.):
{chr(10).join([f"{i+1}. {name}" for i, name in enumerate(procedurka)])}

📅 Начало отсчёта: {start_date}"""
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=main_menu())

    elif query.data == "edit_svodki":
        await query.edit_message_text("Отправь команду:\n`/set_svodki Саша, Олег, Максим, ...`", parse_mode='Markdown')
    
    elif query.data == "edit_procedurka":
        await query.edit_message_text("Отправь команду:\n`/set_procedurka Илья, Слава, Саша, ...`", parse_mode='Markdown')
    
    elif query.data == "offset":
        await query.edit_message_text("Отправь:\n`/set_offset 3` — чтобы сдвинуть очередь на 3 дня", parse_mode='Markdown')

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_message(), parse_mode='HTML', reply_markup=main_menu())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку «Меню» ниже или используй команды", reply_markup=main_menu())

# ================= ЗАПУСК =================
def main():
    init_db()
    logging.basicConfig(level=logging.INFO)
    
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Планировщик на 18:00
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        lambda: app.bot.send_message(chat_id=CHAT_ID, text=get_message(), parse_mode='HTML'),
        "cron", hour=18, minute=0
    )
    scheduler.start()

    print("✅ Бот с кнопками запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
