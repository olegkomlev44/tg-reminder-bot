from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, date
import sqlite3
import json
import logging

TOKEN = "8874089866:AAEoGd63Dm2DC6YNSQ29oO2zcUlVNI5zY1Y"

# ================= НАСТРОЙКИ =================
CHAT_ID = -1001234567890          # ← Замени на реальный ID группы!
ADMIN_IDS = [123456789]           # ← Добавь сюда свои Telegram ID (через @userinfobot)

# Инициализация БД
def init_db():
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Значения по умолчанию
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
    return json.loads(result[0]) if key in ['svodki', 'procedurka'] else result[0] if result else None

def update_setting(key, value):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    if key in ['svodki', 'procedurka']:
        value = json.dumps(value)
    c.execute("REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# Получить сообщение
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
📅 Сегодня: {today.strftime('%d.%m.%Y')}
    """

# ================= КОМАНДЫ =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот-напоминалка запущен!\n\nНапиши /help для списка команд.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📋 <b>Доступные команды:</b>

/today — показать сегодняшнее назначение
/list — показать текущие списки
/set_svodki имя1, имя2, имя3... — изменить список сводок
/set_procedurka имя1, имя2... — изменить список уборки
/set_start_date ГГГГ-ММ-ДД — изменить дату начала отсчёта
/set_offset N — сдвинуть очередь на N дней (например /set_offset 3)

/help — это сообщение
    """
    await update.message.reply_text(text, parse_mode='HTML')

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_message(), parse_mode='HTML')

async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    svodki = get_setting('svodki')
    procedurka = get_setting('procedurka')
    start_date = get_setting('start_date')
    
    text = f"""<b>Текущие настройки:</b>

📦 Сводки ({len(svodki)} чел.):
{chr(10).join([f"{i+1}. {name}" for i, name in enumerate(svodki)])}

🧹 Уборка ({len(procedurka)} чел.):
{chr(10).join([f"{i+1}. {name}" for i, name in enumerate(procedurka)])}

📅 Дата начала отсчёта: {start_date}"""
    await update.message.reply_text(text, parse_mode='HTML')

# ================= РЕДАКТИРОВАНИЕ =================
async def set_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторы могут менять списки.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_svodki Саша, Олег, Максим")
        return
    names = [name.strip() for name in ' '.join(context.args).split(',')]
    update_setting('svodki', names)
    await update.message.reply_text(f"✅ Список сводок обновлён ({len(names)} человек)")

async def set_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторы могут менять списки.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_procedurka Илья, Слава, Саша")
        return
    names = [name.strip() for name in ' '.join(context.args).split(',')]
    update_setting('procedurka', names)
    await update.message.reply_text(f"✅ Список уборки обновлён ({len(names)} человек)")

async def set_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторы.")
        return
    try:
        date_str = context.args[0]
        datetime.strptime(date_str, "%Y-%m-%d")
        update_setting('start_date', date_str)
        await update.message.reply_text(f"✅ Дата начала отсчёта изменена на {date_str}")
    except:
        await update.message.reply_text("❌ Неверный формат! Используй ГГГГ-ММ-ДД\nПример: /set_start_date 2026-06-01")

async def set_offset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторы.")
        return
    try:
        offset = int(context.args[0])
        start_date = date.fromisoformat(get_setting('start_date'))
        new_date = start_date - timedelta(days=offset)
        update_setting('start_date', new_date.isoformat())
        await update.message.reply_text(f"✅ Очередь сдвинута на {offset} дней назад.")
    except:
        await update.message.reply_text("Пример: /set_offset 2")

# ================= ЗАПУСК =================
def main():
    init_db()
    logging.basicConfig(level=logging.INFO)
    
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("list", show_list))
    app.add_handler(CommandHandler("set_svodki", set_svodki))
    app.add_handler(CommandHandler("set_procedurka", set_procedurka))
    app.add_handler(CommandHandler("set_start_date", set_start_date))
    app.add_handler(CommandHandler("set_offset", set_offset))

    # Планировщик
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        lambda: app.bot.send_message(chat_id=CHAT_ID, text=get_message(), parse_mode='HTML'),
        "cron", hour=18, minute=0
    )
    scheduler.start()

    print("✅ Бот с базой данных успешно запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
