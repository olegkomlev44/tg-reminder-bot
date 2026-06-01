from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, date, timedelta
from io import BytesIO
import sqlite3
import json
import logging
import asyncio
from PIL import Image, ImageDraw, ImageFont
import pytz

# ================= НАСТРОЙКИ =================
TOKEN = "8874089866:AAEoGd63Dm2DC6YNSQ29oO2zcUlVNI5zY1Y"
CHAT_ID = -1003782926765          # ← Замените на ID вашей группы
ADMIN_IDS = [891298064,715554757]           # ← ID админов (можно несколько через запятую)

MSK = pytz.timezone('Europe/Moscow')

def now_msk():
    return datetime.now(MSK)

# ================= БАЗА ДАННЫХ =================
def init_db():
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_name TEXT,
                  task_type TEXT,
                  date TEXT,
                  completed INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_done
                 (date TEXT, task_type TEXT, user_name TEXT,
                  PRIMARY KEY (date, task_type))''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_state
                 (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reminders
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id INTEGER,
                  message_id INTEGER,
                  task_type TEXT,
                  date TEXT)''')

    default_svodki = ["Саша", "Олег", "Максим", "Игорь", "Илья", "Глеба", "Слава", "Ильнар"]
    default_procedurka = ["Илья", "Слава", "Саша", "Игоооорь", "Глеба", "Ильнар"]

    c.execute("INSERT OR IGNORE INTO settings VALUES ('svodki', ?)", (json.dumps(default_svodki),))
    c.execute("INSERT OR IGNORE INTO settings VALUES ('procedurka', ?)", (json.dumps(default_procedurka),))
    c.execute("INSERT OR IGNORE INTO settings VALUES ('svodki_start_date', ?)", ("2026-06-01",))
    c.execute("INSERT OR IGNORE INTO settings VALUES ('procedurka_start_date', ?)", ("2026-06-01",))
    c.execute("INSERT OR IGNORE INTO settings VALUES ('shave_enabled', ?)", ("1",))
    # Настройка времени напоминаний (по умолчанию: сводки в 18:00, уборка в 23:00)
    c.execute("INSERT OR IGNORE INTO settings VALUES ('svodki_remind_hour', ?)", ("20",))
    c.execute("INSERT OR IGNORE INTO settings VALUES ('procedurka_remind_hour', ?)", ("23",))
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

def save_last_message_id(message_id: int):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute("REPLACE INTO bot_state VALUES ('last_msg', ?)", (str(message_id),))
    conn.commit()
    conn.close()

def get_last_message_id():
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute("SELECT value FROM bot_state WHERE key='last_msg'")
    row = c.fetchone()
    conn.close()
    return int(row[0]) if row else None

# FIX: принимает bot, а не app
async def delete_previous_message(bot, chat_id: int):
    prev_id = get_last_message_id()
    if prev_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=prev_id)
        except Exception:
            pass
        finally:
            save_last_message_id(0)

def save_reminder_message(chat_id: int, message_id: int, task_type: str, date_str: str):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    # Удаляем старый reminder того же типа за тот же день
    c.execute("DELETE FROM reminders WHERE task_type=? AND date=?", (task_type, date_str))
    c.execute("INSERT INTO reminders (chat_id, message_id, task_type, date) VALUES (?, ?, ?, ?)",
              (chat_id, message_id, task_type, date_str))
    conn.commit()
    conn.close()

def get_reminder_message(task_type: str, date_str: str):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute("SELECT message_id FROM reminders WHERE task_type=? AND date=?", (task_type, date_str))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# ================= ЛОГИКА УСТАНОВКИ КОНКРЕТНОГО ЧЕЛОВЕКА =================
def set_current_person(list_key: str, start_date_key: str, person_name: str):
    people = get_setting(list_key)
    if not people:
        return False, "❌ Список пустой."
    if person_name not in people:
        return False, f"❌ Человек '{person_name}' не найден в списке."
    index = people.index(person_name)
    today = now_msk().date()
    new_start_date = today - timedelta(days=index)
    update_setting(start_date_key, new_start_date.isoformat())
    return True, f"✅ <b>{person_name}</b> поставлен на сегодня!\nПозиция в очереди: {index+1}/{len(people)}"

# ================= ФОРМИРОВАНИЕ СООБЩЕНИЯ =================
def get_today_info():
    today = now_msk().date()
    svodki_start = date.fromisoformat(get_setting('svodki_start_date'))
    procedurka_start = date.fromisoformat(get_setting('procedurka_start_date'))
    days_svodki = (today - svodki_start).days
    days_procedurka = (today - procedurka_start).days
    svodki = get_setting('svodki')
    procedurka = get_setting('procedurka')
    svodki_name = svodki[days_svodki % len(svodki)]
    proc_name = procedurka[days_procedurka % len(procedurka)]
    return svodki_name, proc_name

def is_task_done(task_type: str) -> bool:
    today_str = now_msk().date().isoformat()
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM daily_done WHERE date=? AND task_type=?", (today_str, task_type))
    result = c.fetchone() is not None
    conn.close()
    return result

def get_text_message():
    svodki_name, proc_name = get_today_info()
    today = now_msk().date()
    svodki_done = "✅" if is_task_done("svodki") else "⏳"
    proc_done = "✅" if is_task_done("procedurka") else "⏳"
    return (
        f"🚨 <b>НАПОМИНАНИЕ НА СЕГОДНЯ</b> 🚨\n\n"
        f"📦 <b>Относить сводки</b> {svodki_done}\n"
        f"🎖 {svodki_name}\n\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🧹 <b>Уборка процедурки</b> {proc_done}\n"
        f"🎖 {proc_name}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📅 {today.strftime('%d.%m.%Y')}"
    )

# ================= ГЕНЕРАЦИЯ ДЕМОТИВАТОРА (улучшенный) =================
def generate_demotivator(svodki_name: str, procedurka_name: str, date_str: str) -> BytesIO:
    W, H = 900, 650
    # Тёмный фон с градиентом
    img = Image.new('RGB', (W, H), color='#0a0a0a')
    draw = ImageDraw.Draw(img)

    # Фоновый градиент (имитация через полосы)
    for y in range(H):
        ratio = y / H
        r = int(10 + ratio * 20)
        g = int(10 + ratio * 15)
        b = int(10 + ratio * 35)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Внешняя рамка (двойная)
    draw.rectangle([(12, 12), (W-12, H-12)], outline='#c8a96e', width=3)
    draw.rectangle([(18, 18), (W-18, H-18)], outline='#8a6a2e', width=1)

    # Декоративные угловые элементы
    corner_size = 30
    corners = [(20, 20), (W-20-corner_size, 20), (20, H-20-corner_size), (W-20-corner_size, H-20-corner_size)]
    for cx, cy in corners:
        draw.rectangle([(cx, cy), (cx+corner_size, cy+corner_size)], outline='#c8a96e', width=2)

    # Загрузка шрифтов (fallback на дефолтный)
    def load_font(size):
        for font_path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                           "arial.ttf", "Arial.ttf"]:
            try:
                return ImageFont.truetype(font_path, size)
            except:
                continue
        return ImageFont.load_default()

    font_header = load_font(22)
    font_title = load_font(32)
    font_emoji_label = load_font(18)
    font_name = load_font(54)
    font_date = load_font(20)
    font_small = load_font(16)

    def centered_text(text, y, font, color):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((W - w) // 2, y), text, fill=color, font=font)

    def shadow_text(text, y, font, color, shadow_color='#000000'):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        x = (W - w) // 2
        # Тень
        draw.text((x+2, y+2), text, fill=shadow_color, font=font)
        draw.text((x, y), text, fill=color, font=font)

    # Заголовок блока
    centered_text("⚡ ПРИКАЗ ДНЯ ⚡", 38, font_header, '#c8a96e')

    # Разделитель
    draw.line([(60, 72), (W-60, 72)], fill='#c8a96e', width=1)
    draw.line([(60, 75), (W-60, 75)], fill='#8a6a2e', width=1)

    # Блок СВОДКИ
    # Подзаголовок
    centered_text("📦  ОТНОСИТЬ СВОДКИ", 95, font_emoji_label, '#aaaaaa')
    # Имя с золотым свечением
    shadow_text(svodki_name, 125, font_name, '#f5d78e', '#3a2800')
    # Подчёркивание имени
    bbox = draw.textbbox((0, 0), svodki_name, font=font_name)
    name_w = bbox[2] - bbox[0]
    line_x = (W - name_w) // 2
    draw.line([(line_x, 125+bbox[3]+4), (line_x+name_w, 125+bbox[3]+4)], fill='#c8a96e', width=2)

    # Разделитель между блоками
    mid_y = 280
    draw.line([(80, mid_y), (W-80, mid_y)], fill='#333333', width=1)
    centered_text("✦", mid_y - 8, font_small, '#c8a96e')

    # Блок УБОРКА
    centered_text("🧹  УБОРКА ПРОЦЕДУРКИ", mid_y + 20, font_emoji_label, '#aaaaaa')
    shadow_text(procedurka_name, mid_y + 48, font_name, '#f5d78e', '#3a2800')
    bbox2 = draw.textbbox((0, 0), procedurka_name, font=font_name)
    name_w2 = bbox2[2] - bbox2[0]
    line_x2 = (W - name_w2) // 2
    draw.line([(line_x2, mid_y+48+bbox2[3]+4), (line_x2+name_w2, mid_y+48+bbox2[3]+4)], fill='#c8a96e', width=2)

    # Нижний разделитель
    draw.line([(60, H-80), (W-60, H-80)], fill='#c8a96e', width=1)
    draw.line([(60, H-77), (W-60, H-77)], fill='#8a6a2e', width=1)

    # Дата и слоган
    centered_text(f"📅  {date_str}", H-62, font_date, '#888888')
    centered_text("Каждый день — новый герой", H-38, font_small, '#555555')

    bio = BytesIO()
    bio.name = 'demotivator.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

# ================= СТАТИСТИКА =================
def mark_done(date_str: str, task_type: str, user_name: str):
    conn = sqlite3.connect('reminder.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM daily_done WHERE date=? AND task_type=?", (date_str, task_type))
    if c.fetchone():
        conn.close()
        return False
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
    today = now_msk()
    if today.day != 1:
        return
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

# ================= НАПОМИНАНИЕ "ПОБРИТЬСЯ" =================
async def send_shave_reminder(app):
    enabled = get_setting('shave_enabled')
    if enabled == '1':
        await app.bot.send_message(chat_id=CHAT_ID, text="🧔‍♂️ <b>Напоминание:</b> всем побриться! Будьте опрятными.", parse_mode='HTML')

# ================= КЛАВИАТУРЫ =================
def main_menu():
    shave_status = get_setting('shave_enabled')
    status_text = "🪒 Вкл" if shave_status == '1' else "🪒 Выкл"
    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today"),
         InlineKeyboardButton("❓ Кто сегодня?", callback_data="quick_today")],
        [InlineKeyboardButton("📋 Показать списки", callback_data="list")],
        [InlineKeyboardButton("✏️ Изменить сводки", callback_data="edit_svodki")],
        [InlineKeyboardButton("🧹 Изменить уборку", callback_data="edit_procedurka")],
        [InlineKeyboardButton("🎯 Сводки → на человека", callback_data="set_current_svodki")],
        [InlineKeyboardButton("🎯 Уборка → на человека", callback_data="set_current_procedurka")],
        [InlineKeyboardButton("📊 Статистика за месяц", callback_data="stats")],
        [InlineKeyboardButton("⏰ Время напоминаний", callback_data="remind_times")],
        [InlineKeyboardButton(f"{status_text} (напом. о бритье)", callback_data="toggle_shave_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ================= ОБРАБОТЧИК КНОПКИ "ВЫПОЛНЕНО" =================
# FIX: вынесен отдельно и регистрируется ПЕРВЫМ
async def task_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if not data.startswith("done_"):
        return

    task_type = data.split("_")[1]
    today_str = now_msk().date().isoformat()
    svodki_name, proc_name = get_today_info()
    user_name = svodki_name if task_type == "svodki" else proc_name

    if mark_done(today_str, task_type, user_name):
        task_label = "сводки" if task_type == "svodki" else "уборку процедурки"
        await query.edit_message_text(
            f"✅ <b>Отлично, {user_name}!</b>\n\n"
            f"Выполнение отмечено: {task_label}\n"
            f"📅 {now_msk().strftime('%d.%m.%Y %H:%M')}",
            parse_mode='HTML'
        )
    else:
        task_label = "сводки" if task_type == "svodki" else "уборку"
        await query.edit_message_text(
            f"⚠️ <b>Уже отмечено!</b>\n\n"
            f"За сегодня выполнение по «{task_label}» уже зафиксировано.",
            parse_mode='HTML'
        )

    # Удаляем сообщение через 8 секунд
    async def delete_later():
        await asyncio.sleep(8)
        try:
            await query.message.delete()
        except Exception:
            pass
    asyncio.create_task(delete_later())

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
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await update_or_query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        save_last_message_id(sent.message_id)
    else:
        await update_or_query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

# ================= ОСНОВНОЙ ОБРАБОТЧИК КНОПОК =================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "today":
        svodki_name, proc_name = get_today_info()
        today_str = now_msk().strftime('%d.%m.%Y')
        img_bio = generate_demotivator(svodki_name, proc_name, today_str)
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await query.message.reply_photo(
            photo=img_bio, caption=get_text_message(),
            parse_mode='HTML', reply_markup=main_menu()
        )
        save_last_message_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

    elif data == "quick_today":
        svodki_name, proc_name = get_today_info()
        svodki_done = "✅" if is_task_done("svodki") else "⏳ не выполнено"
        proc_done = "✅" if is_task_done("procedurka") else "⏳ не выполнено"
        text = (
            f"📅 <b>Сегодня:</b>\n\n"
            f"📦 Сводки: <b>{svodki_name}</b> {svodki_done}\n"
            f"🧹 Уборка: <b>{proc_name}</b> {proc_done}"
        )
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await query.message.reply_text(text, parse_mode='HTML', reply_markup=main_menu())
        save_last_message_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

    elif data == "list":
        keyboard = [
            [InlineKeyboardButton("📦 Сводки", callback_data="list_svodki_0")],
            [InlineKeyboardButton("🧹 Уборка", callback_data="list_procedurka_0")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
        ]
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await query.message.reply_text("Выберите список:", reply_markup=InlineKeyboardMarkup(keyboard))
        save_last_message_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

    elif data.startswith("list_"):
        parts = data.split('_')
        if len(parts) == 3:
            list_type = parts[1]
            page = int(parts[2])
            await send_list_page(query, context, list_type, page)

    elif data == "stats":
        today = now_msk()
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
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await query.message.reply_text(text, parse_mode='HTML', reply_markup=main_menu())
        save_last_message_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

    elif data == "remind_times":
        svodki_h = get_setting('svodki_remind_hour') or '20'
        proc_h = get_setting('procedurka_remind_hour') or '23'
        text = (
            f"⏰ <b>Текущее время напоминаний (МСК):</b>\n\n"
            f"📦 Сводки — проверка в <b>{svodki_h}:00</b>\n"
            f"🧹 Уборка — проверка в <b>{proc_h}:00</b>\n\n"
            f"Для изменения отправьте:\n"
            f"<code>/set_remind_times 20 23</code>\n"
            f"(первое — час для сводок, второе — для уборки)"
        )
        keyboard = [[InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_menu")]]
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await query.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        save_last_message_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

    elif data == "toggle_shave_menu":
        if update.effective_user.id not in ADMIN_IDS:
            await query.answer("⛔️ Только администраторы!", show_alert=True)
            return
        current = get_setting('shave_enabled')
        new_value = '0' if current == '1' else '1'
        update_setting('shave_enabled', new_value)
        status_text = "включено ✅" if new_value == '1' else "выключено ❌"
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await query.message.reply_text(
            f"🪒 Напоминание о бритье {status_text}.",
            reply_markup=main_menu()
        )
        save_last_message_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

    elif data in ["edit_svodki", "edit_procedurka", "set_current_svodki", "set_current_procedurka"]:
        msgs = {
            "edit_svodki": "Отправьте команду:\n`/set_svodki Саша, Олег, Максим, ...`",
            "edit_procedurka": "Отправьте команду:\n`/set_procedurka Илья, Слава, Саша, ...`",
            "set_current_svodki": "Отправьте команду:\n`/set_current_svodki Максим`",
            "set_current_procedurka": "Отправьте команду:\n`/set_current_procedurka Игоооорь`",
        }
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await query.message.reply_text(msgs[data], parse_mode='Markdown')
        save_last_message_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

    elif data == "back_to_menu":
        await delete_previous_message(context.bot, CHAT_ID)
        sent = await query.message.reply_text("Главное меню:", reply_markup=main_menu())
        save_last_message_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

# ================= КОМАНДЫ =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_previous_message(context.bot, CHAT_ID)
    sent = await update.message.reply_text("👋 Бот готов!\nВыберите действие:", reply_markup=main_menu())
    save_last_message_id(sent.message_id)

async def set_current_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_current_svodki Максим")
        return
    name = ' '.join(context.args).strip()
    success, msg = set_current_person('svodki', 'svodki_start_date', name)
    await delete_previous_message(context.bot, CHAT_ID)
    sent = await update.message.reply_text(msg, parse_mode='HTML')
    save_last_message_id(sent.message_id)

async def set_current_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args:
        await update.message.reply_text("Пример: /set_current_procedurka Игоооорь")
        return
    name = ' '.join(context.args).strip()
    success, msg = set_current_person('procedurka', 'procedurka_start_date', name)
    await delete_previous_message(context.bot, CHAT_ID)
    sent = await update.message.reply_text(msg, parse_mode='HTML')
    save_last_message_id(sent.message_id)

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
    today = now_msk().date()
    update_setting('svodki_start_date', today.isoformat())
    await delete_previous_message(context.bot, CHAT_ID)
    sent = await update.message.reply_text(f"✅ Список сводок обновлён: {', '.join(names)}")
    save_last_message_id(sent.message_id)

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
    today = now_msk().date()
    update_setting('procedurka_start_date', today.isoformat())
    await delete_previous_message(context.bot, CHAT_ID)
    sent = await update.message.reply_text(f"✅ Список уборки обновлён: {', '.join(names)}")
    save_last_message_id(sent.message_id)

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    svodki_name, proc_name = get_today_info()
    today_str = now_msk().strftime('%d.%m.%Y')
    img_bio = generate_demotivator(svodki_name, proc_name, today_str)
    await delete_previous_message(context.bot, CHAT_ID)
    sent = await update.message.reply_photo(
        photo=img_bio, caption=get_text_message(),
        parse_mode='HTML', reply_markup=main_menu()
    )
    save_last_message_id(sent.message_id)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = now_msk()
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
    await delete_previous_message(context.bot, CHAT_ID)
    sent = await update.message.reply_text(text, parse_mode='HTML')
    save_last_message_id(sent.message_id)

async def toggle_shave_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    current = get_setting('shave_enabled')
    new_value = '0' if current == '1' else '1'
    update_setting('shave_enabled', new_value)
    status_text = "включено" if new_value == '1' else "выключено"
    await delete_previous_message(context.bot, CHAT_ID)
    sent = await update.message.reply_text(f"🪒 Напоминание о бритье {status_text}.")
    save_last_message_id(sent.message_id)

async def set_remind_times_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /set_remind_times 20 23 — установить час напоминаний для сводок и уборки"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только администраторам.")
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Пример: /set_remind_times 20 23\n"
            "(первое — час для сводок, второе — для уборки, 0-23 по МСК)"
        )
        return
    try:
        h1 = int(context.args[0])
        h2 = int(context.args[1])
        if not (0 <= h1 <= 23 and 0 <= h2 <= 23):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Некорректные часы. Укажите числа от 0 до 23.")
        return
    update_setting('svodki_remind_hour', str(h1))
    update_setting('procedurka_remind_hour', str(h2))
    await update.message.reply_text(
        f"✅ Время напоминаний обновлено!\n"
        f"📦 Сводки — {h1}:00 МСК\n"
        f"🧹 Уборка — {h2}:00 МСК\n\n"
        f"⚠️ Изменения применятся с <b>следующего перезапуска бота</b>.",
        parse_mode='HTML'
    )

# ================= ТАЙМЕРЫ НАПОМИНАНИЙ =================
# FIX: используем точный расчёт времени вместо hardcoded задержки
async def ask_for_task_completion(app, task_type: str, target_hour: int):
    """Ждёт до target_hour по МСК и отправляет запрос о выполнении."""
    now = now_msk()
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    delay_seconds = (target - now).total_seconds()
    logging.info(f"Задержка до {target_hour}:00 МСК для {task_type}: {delay_seconds:.0f}с")
    await asyncio.sleep(delay_seconds)

    # Не отправляем если уже выполнено
    if is_task_done(task_type):
        logging.info(f"{task_type} уже выполнено, напоминание пропущено")
        return

    svodki_name, proc_name = get_today_info()
    if task_type == "svodki":
        user = svodki_name
        text = f"⏰ <b>Напоминание!</b>\n\n{user}, вы уже отнесли сводки?\nНажмите кнопку ниже 👇"
        callback_data = "done_svodki"
    else:
        user = proc_name
        text = f"⏰ <b>Напоминание!</b>\n\n{user}, вы уже убрали процедурку?\nНажмите кнопку ниже 👇"
        callback_data = "done_procedurka"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выполнено!", callback_data=callback_data)
    ]])
    try:
        sent = await app.bot.send_message(
            chat_id=CHAT_ID, text=text,
            parse_mode='HTML', reply_markup=keyboard
        )
        today_str = now_msk().date().isoformat()
        save_reminder_message(CHAT_ID, sent.message_id, task_type, today_str)

        # Авто-удаление через 2 часа
        async def delete_after():
            await asyncio.sleep(7200)
            try:
                await sent.delete()
            except Exception:
                pass
        asyncio.create_task(delete_after())
    except Exception as e:
        logging.error(f"Ошибка отправки напоминания {task_type}: {e}")

async def daily_reminder_and_timers(app):
    """Ежедневное напоминание в 18:00 + запуск таймеров проверки выполнения."""
    svodki_name, proc_name = get_today_info()
    today_str = now_msk().strftime('%d.%m.%Y')
    img_bio = generate_demotivator(svodki_name, proc_name, today_str)
    # FIX: передаём app.bot, а не app
    await delete_previous_message(app.bot, CHAT_ID)
    sent = await app.bot.send_photo(
        chat_id=CHAT_ID, photo=img_bio,
        caption=get_text_message(), parse_mode='HTML'
    )
    save_last_message_id(sent.message_id)

    # Запускаем напоминания-проверки с привязкой к настраиваемому времени
    svodki_hour = int(get_setting('svodki_remind_hour') or '20')
    procedurka_hour = int(get_setting('procedurka_remind_hour') or '23')

    asyncio.create_task(ask_for_task_completion(app, "svodki", svodki_hour))
    asyncio.create_task(ask_for_task_completion(app, "procedurka", procedurka_hour))

# ================= ЗАПУСК =================
def main():
    init_db()
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("toggle_shave", toggle_shave_command))
    app.add_handler(CommandHandler("set_current_svodki", set_current_svodki))
    app.add_handler(CommandHandler("set_current_procedurka", set_current_procedurka))
    app.add_handler(CommandHandler("set_svodki", set_svodki))
    app.add_handler(CommandHandler("set_procedurka", set_procedurka))
    app.add_handler(CommandHandler("set_remind_times", set_remind_times_command))

    # FIX: done_ хендлер регистрируется ПЕРВЫМ, иначе перехватывается общим
    app.add_handler(CallbackQueryHandler(task_done_callback, pattern="^done_"))
    app.add_handler(CallbackQueryHandler(button_handler))

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    # Используем partial чтобы избежать проблем с lambda в цикле
    scheduler.add_job(
        lambda: asyncio.create_task(send_shave_reminder(app)),
        "cron", hour=17, minute=0
    )
    scheduler.add_job(
        lambda: asyncio.create_task(daily_reminder_and_timers(app)),
        "cron", hour=18, minute=0
    )
    scheduler.add_job(
        lambda: asyncio.create_task(send_monthly_report(app)),
        "cron", day=1, hour=10, minute=0
    )
    scheduler.start()

    print("✅ Бот запущен. Временная зона: Europe/Moscow")
    print(f"📦 Напоминание сводки: {get_setting('svodki_remind_hour')}:00 МСК")
    print(f"🧹 Напоминание уборки: {get_setting('procedurka_remind_hour')}:00 МСК")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
