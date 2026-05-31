from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, date, timedelta
import sqlite3
import json
import logging
import asyncio

# ================= НАСТРОЙКИ =================
TOKEN = "8874089866:AAEoGd63Dm2DC6YNSQ29oO2zcUlVNI5zY1Y"  # Замените на свой токен
CHAT_ID = -1001234567890          # ← Замените на ID вашей группы
ADMIN_IDS = [123456789]           # ← Ваш Telegram ID

# ================= БАЗА ДАННЫХ =================
def init_db():
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    # Таблица настроек
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    # Таблица статистики
    c.execute('''CREATE TABLE IF NOT EXISTS stats
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_name TEXT,
                  task_type TEXT,
                  date TEXT,
                  completed INTEGER)''')
    # Таблица для отметок о выполнении (чтобы не дублировать за день)
    c.execute('''CREATE TABLE IF NOT EXISTS daily_done
                 (date TEXT, task_type TEXT, user_name TEXT,
                  PRIMARY KEY (date, task_type))''')

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
    people = get_setting(list_key)
    if not people:
        return False, "❌ Список пустой."
    if person_name not in people:
        return False, f"❌ Человек '{person_name}' не найден в списке."
    index = people.index(person_name)
    today = datetime.now().date()
    new_start_date = today - timedelta(days=index)
    update_setting(start_date_key, new_start_date.isoformat())
    return True, f"✅ <b>{person_name}</b> поставлен на сегодня!\nПозиция в очереди: {index+1}/{len(people)}"

# ================= ФОРМИРОВАНИЕ СООБЩЕНИЯ /today =================
def get_today_info():
    today = datetime.now().date()
    svodki_start = date.fromisoformat(get_setting('svodki_start_date'))
    procedurka_start = date.fromisoformat(get_setting('procedurka_start_date'))
    days_svodki = (today - svodki_start).days
    days_procedurka = (today - procedurka_start).days
    svodki = get_setting('svodki')
    procedurka = get_setting('procedurka')
    svodki_name = svodki[days_svodki % len(svodki)]
    proc_name = procedurka[days_procedurka % len(procedurka)]
    return svodki_name, proc_name

def get_message():
    svodki_name, proc_name = get_today_info()
    today = datetime.now().date()
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

# ================= СТАТИСТИКА =================
def mark_done(date_str: str, task_type: str, user_name: str):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    # Проверяем, не отмечали ли уже сегодня
    c.execute("SELECT 1 FROM daily_done WHERE date=? AND task_type=?", (date_str, task_type))
    if c.fetchone():
        conn.close()
        return False
    # Записываем в статистику
    c.execute("INSERT INTO stats (user_name, task_type, date, completed) VALUES (?, ?, ?, 1)",
              (user_name, task_type, date_str))
    c.execute("INSERT INTO daily_done (date, task_type, user_name) VALUES (?, ?, ?)",
              (date_str, task_type, user_name))
    conn.commit()
    conn.close()
    return True

def get_monthly_stats(year, month):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    month_str = f"{year}-{month:02d}"
    c.execute("SELECT user_name, task_type, COUNT(*) FROM stats WHERE strftime('%Y-%m', date)=? GROUP BY user_name, task_type",
              (month_str,))
    rows = c.fetchall()
    conn.close()
    svodki_stats = {}
    procedurka_stats = {}
    for user, task, cnt in rows:
        if task == 'svodki':
            svodki_stats[user] = cnt
        else:
            procedurka_stats[user] = cnt
    return svodki_stats, procedurka_stats

async def send_monthly_report(app):
    today = datetime.now()
    if today.day != 1:
        return
    # Отправляем отчёт за прошлый месяц
    last_month = today.replace(day=1) - timedelta(days=1)
    year, month = last_month.year, last_month.month
    svodki_stats, procedurka_stats = get_monthly_stats(year, month)
    if not svodki_stats and not procedurka_stats:
        return
    report = f"📊 <b>Отчёт героев за {last_month.strftime('%B %Y')}</b>\n\n"
    if svodki_stats:
        best_svodki = max(svodki_stats.items(), key=lambda x: x[1])
        report += "📦 Сводки:\n"
        for user, cnt in sorted(svodki_stats.items(), key=lambda x: x[1], reverse=True):
            medal = " 🏅" if user == best_svodki[0] else ""
            report += f"  {user}: {cnt} раз{medal}\n"
        report += "\n"
    if procedurka_stats:
        best_proc = max(procedurka_stats.items(), key=lambda x: x[1])
        report += "🧹 Уборка:\n"
        for user, cnt in sorted(procedurka_stats.items(), key=lambda x: x[1], reverse=True):
            medal = " 🏅" if user == best_proc[0] else ""
            report += f"  {user}: {cnt} раз{medal}\n"
    await app.bot.send_message(chat_id=CHAT_ID, text=report, parse_mode='HTML')

# ================= КЛАВИАТУРЫ =================
def main_menu():
    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today"),
         InlineKeyboardButton("❓ Кто сегодня?", callback_data="quick_today")],
        [InlineKeyboardButton("📋 Показать списки", callback_data="list")],
        [InlineKeyboardButton("✏️ Изменить сводки", callback_data="edit_svodki")],
        [InlineKeyboardButton("🧹 Изменить уборку", callback_data="edit_procedurka")],
        [InlineKeyboardButton("🎯 Сводки → на человека", callback_data="set_current_svodki")],
        [InlineKeyboardButton("🎯 Уборка → на человека", callback_data="set_current_procedurka")],
        [InlineKeyboardButton("📊 Статистика за месяц", callback_data="stats")],
    ]
    return InlineKeyboardMarkup(keyboard)

# Пагинация для списков
list_cache = {}  # временное хранилище: user_id -> (список, тип, страница)

def get_list_page(list_data, page, per_page=7):
    start = page * per_page
    end = start + per_page
    page_items = list_data[start:end]
    total_pages = (len(list_data) + per_page - 1) // per_page
    return page_items, total_pages

async def send_list_page(update_or_query, context, list_type, page=0):
    svodki = get_setting('svodki')
    procedurka = get_setting('procedurka')
    if list_type == 'svodki':
        data = svodki
        title = "📦 Список сводок"
    else:
        data = procedurka
        title = "🧹 Список уборки"
    today_svodki, today_proc = get_today_info()
    per_page = 7
    start = page * per_page
    page_items = data[start:start+per_page]
    total_pages = (len(data) + per_page - 1) // per_page
    text = f"{title} (страница {page+1}/{total_pages})\n\n"
    for i, name in enumerate(page_items, start=start+1):
        # подсветка текущего
        if list_type == 'svodki' and name == today_svodki:
            text += f"🟢 {i}. <b>{name}</b> (сегодня)\n"
        elif list_type == 'procedurka' and name == today_proc:
            text += f"🟢 {i}. <b>{name}</b> (сегодня)\n"
        else:
            text += f"{i}. {name}\n"
    keyboard = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"list_{list_type}_{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"list_{list_type}_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
    else:
        await update_or_query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

# ================= ОБРАБОТЧИКИ =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Бот готов!\nВыберите действие:", reply_markup=main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "today":
        await query.edit_message_text(get_message(), parse_mode='HTML', reply_markup=main_menu())
    elif data == "quick_today":
        svodki_name, proc_name = get_today_info()
        text = f"📅 Сегодня:\n📦 Сводки: {svodki_name}\n🧹 Уборка: {proc_name}"
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=main_menu())
    elif data == "list":
        # Выбор типа списка
        keyboard = [
            [InlineKeyboardButton("📦 Сводки", callback_data="list_svodki_0")],
            [InlineKeyboardButton("🧹 Уборка", callback_data="list_procedurka_0")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
        ]
        await query.edit_message_text("Выберите список:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("list_"):
        parts = data.split('_')
        if len(parts) == 3:
            list_type = parts[1]
            page = int(parts[2])
            await send_list_page(query, context, list_type, page)
    elif data == "stats":
        today = datetime.now()
        svodki_stats, procedurka_stats = get_monthly_stats(today.year, today.month)
        if not svodki_stats and not procedurka_stats:
            text = "📊 За этот месяц пока нет отметок о выполнении."
        else:
            text = f"📊 <b>Статистика за {today.strftime('%B %Y')}</b>\n\n"
            if svodki_stats:
                text += "📦 Сводки:\n"
                for user, cnt in sorted(svodki_stats.items(), key=lambda x: x[1], reverse=True):
                    text += f"  {user}: {cnt} раз\n"
                text += "\n"
            if procedurka_stats:
                text += "🧹 Уборка:\n"
                for user, cnt in sorted(procedurka_stats.items(), key=lambda x: x[1], reverse=True):
                    text += f"  {user}: {cnt} раз\n"
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=main_menu())
    elif data in ["edit_svodki", "edit_procedurka", "set_current_svodki", "set_current_procedurka"]:
        if data == "edit_svodki":
            msg = "Отправьте команду:\n`/set_svodki Саша, Олег, Максим, ...`"
        elif data == "edit_procedurka":
            msg = "Отправьте команду:\n`/set_procedurka Илья, Слава, Саша, ...`"
        elif data == "set_current_svodki":
            msg = "Отправьте команду:\n`/set_current_svodki Максим`"
        else:
            msg = "Отправьте команду:\n`/set_current_procedurka Игоооорь`"
        await query.edit_message_text(msg, parse_mode='Markdown')
    elif data == "back_to_menu":
        await query.edit_message_text("Главное меню:", reply_markup=main_menu())

# Обработка кнопок выполнения задач
async def task_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    # Формат: "done_tasktype"
    if data.startswith("done_"):
        task_type = data.split("_")[1]  # svodki или procedurka
        today_str = datetime.now().date().isoformat()
        # Получаем имя человека, который должен был выполнить
        svodki_name, proc_name = get_today_info()
        user_name = svodki_name if task_type == "svodki" else proc_name
        if mark_done(today_str, task_type, user_name):
            await query.edit_message_text(f"✅ Спасибо, {user_name}! Выполнение отмечено.")
            # Можно также удалить клавиатуру у исходного сообщения, но для простоты оставим
        else:
            await query.edit_message_text(f"⚠️ За сегодня уже отмечено выполнение по {task_type}.")

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

async def set_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_svodki Саша, Олег, Максим")
        return
    text = ' '.join(context.args)
    names = [name.strip() for name in text.split(',') if name.strip()]
    if not names:
        await update.message.reply_text("Список не может быть пустым.")
        return
    update_setting('svodki', names)
    today = datetime.now().date()
    update_setting('svodki_start_date', today.isoformat())
    await update.message.reply_text(f"✅ Список сводок обновлён: {', '.join(names)}")

async def set_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_procedurka Илья, Слава, Саша")
        return
    text = ' '.join(context.args)
    names = [name.strip() for name in text.split(',') if name.strip()]
    if not names:
        await update.message.reply_text("Список не может быть пустым.")
        return
    update_setting('procedurka', names)
    today = datetime.now().date()
    update_setting('procedurka_start_date', today.isoformat())
    await update.message.reply_text(f"✅ Список уборки обновлён: {', '.join(names)}")

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_message(), parse_mode='HTML', reply_markup=main_menu())

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now()
    svodki_stats, procedurka_stats = get_monthly_stats(today.year, today.month)
    if not svodki_stats and not procedurka_stats:
        text = "📊 За этот месяц пока нет отметок о выполнении."
    else:
        text = f"📊 <b>Статистика за {today.strftime('%B %Y')}</b>\n\n"
        if svodki_stats:
            text += "📦 Сводки:\n"
            for user, cnt in sorted(svodki_stats.items(), key=lambda x: x[1], reverse=True):
                text += f"  {user}: {cnt} раз\n"
            text += "\n"
        if procedurka_stats:
            text += "🧹 Уборка:\n"
            for user, cnt in sorted(procedurka_stats.items(), key=lambda x: x[1], reverse=True):
                text += f"  {user}: {cnt} раз\n"
    await update.message.reply_text(text, parse_mode='HTML')

# ================= ТАЙМЕРЫ НА ВЫПОЛНЕНИЕ =================
async def ask_for_task_completion(app, task_type: str, delay_minutes: int = 0):
    """Отправить сообщение с кнопкой выполнения через заданное время"""
    await asyncio.sleep(delay_minutes * 60)
    svodki_name, proc_name = get_today_info()
    if task_type == "svodki":
        user = svodki_name
        text = f"⏰ Напоминание: {user}, вы уже отнесли сводки? Нажмите кнопку ниже."
        callback_data = "done_svodki"
    else:
        user = proc_name
        text = f"⏰ Напоминание: {user}, вы уже убрали процедурку? Нажмите кнопку ниже."
        callback_data = "done_procedurka"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=callback_data)]])
    await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=keyboard)

# ================= ПЛАНИРОВЩИК =================
async def daily_reminder_and_timers(app):
    """Отправляет дневное напоминание и планирует два таймера"""
    await app.bot.send_message(chat_id=CHAT_ID, text=get_message(), parse_mode='HTML')
    # Запускаем асинхронные задачи: через 1 час спросить про сводки, в 23:00 про уборку
    now = datetime.now()
    # Время до 23:00 сегодня
    target_23 = datetime(now.year, now.month, now.day, 23, 0, 0)
    if now >= target_23:
        target_23 += timedelta(days=1)
    delay_until_23 = (target_23 - now).total_seconds() / 60
    # Через 60 минут спросить про сводки
    asyncio.create_task(ask_for_task_completion(app, "svodki", delay_minutes=60))
    # В 23:00 спросить про уборку
    asyncio.create_task(ask_for_task_completion(app, "procedurka", delay_minutes=delay_until_23))

# ================= ЗАПУСК =================
def main():
    init_db()
    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("set_current_svodki", set_current_svodki))
    app.add_handler(CommandHandler("set_current_procedurka", set_current_procedurka))
    app.add_handler(CommandHandler("set_svodki", set_svodki))
    app.add_handler(CommandHandler("set_procedurka", set_procedurka))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(?!done_).*"))  # все кроме done_
    app.add_handler(CallbackQueryHandler(task_done_callback, pattern="^done_"))

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    # Ежедневное напоминание в 18:00
    scheduler.add_job(
        lambda: asyncio.create_task(daily_reminder_and_timers(app)),
        "cron", hour=18, minute=0
    )
    # Ежемесячный отчёт 1-го числа в 10:00
    scheduler.add_job(
        lambda: asyncio.create_task(send_monthly_report(app)),
        "cron", day=1, hour=10, minute=0
    )
    scheduler.start()

    print("✅ Бот запущен со всеми улучшениями (пагинация, подсветка, статистика, таймеры выполнения)")
    app.run_polling()

if __name__ == "__main__":
    main()
