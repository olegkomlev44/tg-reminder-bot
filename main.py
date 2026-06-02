# ╔══════════════════════════════════════════════════════════╗
# ║           ДЕЖУРНЫЙ БОТ — ПОЛНАЯ ВЕРСИЯ v3.0             ║
# ║   Сводки · Уборка · Бритьё · Статистика · Расписание   ║
# ╚══════════════════════════════════════════════════════════╝

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, date, timedelta
from io import BytesIO
import sqlite3
import json
import logging
import asyncio
import random
from PIL import Image, ImageDraw, ImageFont
import pytz

# ═══════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════
TOKEN    = "8874089866:AAEoGd63Dm2DC6YNSQ29oO2zcUlVNI5zY1Y"
CHAT_ID = -1003782926765          # ← Замените на ID вашей группы
ADMIN_IDS = [891298064,715554757]           # ← ID админов (можно несколько через запятую)

MSK = pytz.timezone('Europe/Moscow')

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU   = [
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"
]
MONTHS_RU_GEN = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

# Мотивационные фразы для демотиватора (случайная каждый день)
MOTTOS = [
    "Каждый день — новый герой",
    "Порядок — основа дисциплины",
    "Честь дежурного священна",
    "Делай сегодня — не откладывай",
    "Сила в организованности",
    "Труд украшает человека",
    "Дежурный — это звучит гордо",
    "Сводки ждать не любят",
    "Чистота — залог успеха",
    "Вместе мы сила, по очереди — порядок",
]

def now_msk() -> datetime:
    return datetime.now(MSK)

def today_msk() -> date:
    return now_msk().date()

def fmt_date(d: date) -> str:
    return f"{d.day} {MONTHS_RU_GEN[d.month]} {d.year}"

# ═══════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════
DB = 'reminder.db'

def get_conn():
    return sqlite3.connect(DB)

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS daily_done
                 (date TEXT, task_type TEXT, user_name TEXT,
                  completed_at TEXT,
                  PRIMARY KEY (date, task_type))''')

    c.execute('''CREATE TABLE IF NOT EXISTS stats
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_name TEXT, task_type TEXT,
                  date TEXT, completed INTEGER)''')

    c.execute('''CREATE TABLE IF NOT EXISTS bot_state
                 (key TEXT PRIMARY KEY, value TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS reminders
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id INTEGER, message_id INTEGER,
                  task_type TEXT, date TEXT)''')

    defaults = {
        'svodki':               json.dumps(["Саша", "Олег", "Максим", "Игорь", "Илья", "Глеба", "Слава", "Ильнар"]),
        'procedurka':           json.dumps(["Илья", "Слава", "Саша", "Игоооорь", "Глеба", "Ильнар"]),
        'svodki_start_date':    '2026-06-01',
        'procedurka_start_date':'2026-06-01',
        'svodki_interval':      '1',
        'procedurka_interval':  '2',
        'svodki_remind_hour':   '20',
        'procedurka_remind_hour':'23',
        'shave_enabled':        '1',
        'daily_hour':           '18',
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

def gs(key: str):
    """get_setting — получить значение из БД."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return [] if key in ('svodki', 'procedurka') else None
    if key in ('svodki', 'procedurka'):
        return json.loads(row[0])
    return row[0]

def us(key: str, value):
    """update_setting — сохранить значение в БД."""
    conn = get_conn()
    c = conn.cursor()
    v = json.dumps(value) if key in ('svodki', 'procedurka') else str(value)
    c.execute("REPLACE INTO settings VALUES (?, ?)", (key, v))
    conn.commit()
    conn.close()

# ─── bot_state ────────────────────────────────────────────
def save_msg_id(message_id: int):
    conn = get_conn()
    conn.execute("REPLACE INTO bot_state VALUES ('last_msg', ?)", (str(message_id),))
    conn.commit(); conn.close()

def get_msg_id() -> int | None:
    conn = get_conn()
    row = conn.execute("SELECT value FROM bot_state WHERE key='last_msg'").fetchone()
    conn.close()
    return int(row[0]) if row and row[0] != '0' else None

async def del_prev(bot, chat_id: int):
    mid = get_msg_id()
    if mid:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
        save_msg_id(0)

# ─── reminders ────────────────────────────────────────────
def save_reminder(chat_id: int, message_id: int, task_type: str, date_str: str):
    conn = get_conn()
    conn.execute("DELETE FROM reminders WHERE task_type=? AND date=?", (task_type, date_str))
    conn.execute(
        "INSERT INTO reminders (chat_id,message_id,task_type,date) VALUES (?,?,?,?)",
        (chat_id, message_id, task_type, date_str)
    )
    conn.commit(); conn.close()

# ─── daily_done ───────────────────────────────────────────
def mark_done(date_str: str, task_type: str, user_name: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM daily_done WHERE date=? AND task_type=?", (date_str, task_type))
    if c.fetchone():
        conn.close()
        return False
    now_str = now_msk().strftime('%H:%M')
    c.execute(
        "INSERT INTO daily_done (date,task_type,user_name,completed_at) VALUES (?,?,?,?)",
        (date_str, task_type, user_name, now_str)
    )
    c.execute(
        "INSERT INTO stats (user_name,task_type,date,completed) VALUES (?,?,?,1)",
        (user_name, task_type, date_str)
    )
    conn.commit(); conn.close()
    return True

def unmark_done(date_str: str, task_type: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM daily_done WHERE date=? AND task_type=?", (date_str, task_type))
    if not c.fetchone():
        conn.close()
        return False
    c.execute("DELETE FROM daily_done WHERE date=? AND task_type=?", (date_str, task_type))
    c.execute(
        "DELETE FROM stats WHERE date=? AND task_type=? AND id=("
        "  SELECT id FROM stats WHERE date=? AND task_type=? ORDER BY id DESC LIMIT 1"
        ")",
        (date_str, task_type, date_str, task_type)
    )
    conn.commit(); conn.close()
    return True

def is_done(task_type: str, date_str: str = None) -> bool:
    if date_str is None:
        date_str = today_msk().isoformat()
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM daily_done WHERE date=? AND task_type=?", (date_str, task_type)
    ).fetchone()
    conn.close()
    return row is not None

def done_at(task_type: str) -> str | None:
    date_str = today_msk().isoformat()
    conn = get_conn()
    row = conn.execute(
        "SELECT completed_at FROM daily_done WHERE date=? AND task_type=?",
        (date_str, task_type)
    ).fetchone()
    conn.close()
    return row[0] if row else None

# ─── stats ────────────────────────────────────────────────
def get_monthly_stats(year: int, month: int):
    conn = get_conn()
    month_str = f"{year}-{month:02d}"
    rows = conn.execute(
        "SELECT user_name, task_type, COUNT(*) FROM stats "
        "WHERE strftime('%Y-%m', date)=? GROUP BY user_name, task_type",
        (month_str,)
    ).fetchall()
    conn.close()
    sv, pr = {}, {}
    for user, task, cnt in rows:
        (sv if task == 'svodki' else pr)[user] = cnt
    return sv, pr

# ═══════════════════════════════════════════════════════════
#  ЛОГИКА ОЧЕРЕДИ
# ═══════════════════════════════════════════════════════════
def get_person_for_date(list_key: str, target_date: date) -> str:
    people    = gs(list_key)
    start     = date.fromisoformat(gs(f'{list_key}_start_date'))
    interval  = int(gs(f'{list_key}_interval') or '1')
    days      = (target_date - start).days
    idx       = (days // interval) % len(people)
    return people[idx]

def get_today_info() -> tuple[str, str]:
    today = today_msk()
    return get_person_for_date('svodki', today), get_person_for_date('procedurka', today)

def get_schedule(days_ahead: int = 7) -> list[tuple[date, str, str]]:
    today = today_msk()
    result = []
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        result.append((d, get_person_for_date('svodki', d), get_person_for_date('procedurka', d)))
    return result

def set_current_person(list_key: str, person_name: str) -> tuple[bool, str]:
    people = gs(list_key)
    if not people:
        return False, "❌ Список пустой."
    if person_name not in people:
        return False, f"❌ «{person_name}» не найден в списке."
    idx      = people.index(person_name)
    interval = int(gs(f'{list_key}_interval') or '1')
    today    = today_msk()
    us(f'{list_key}_start_date', (today - timedelta(days=idx * interval)).isoformat())
    label    = "Сводки" if list_key == 'svodki' else "Уборка"
    return True, (
        f"✅ <b>{person_name}</b> назначен сегодня!\n"
        f"📋 {label} · позиция {idx+1} из {len(people)}"
    )

def skip_person(list_key: str) -> str:
    """Сдвинуть очередь на одну позицию вперёд (пропуск)."""
    interval = int(gs(f'{list_key}_interval') or '1')
    start    = date.fromisoformat(gs(f'{list_key}_start_date'))
    new_start = start - timedelta(days=interval)
    us(f'{list_key}_start_date', new_start.isoformat())
    name = get_person_for_date(list_key, today_msk())
    label = "сводки" if list_key == 'svodki' else "уборку"
    return f"⏭ Очередь сдвинута. Теперь на {label}: <b>{name}</b>"

# ═══════════════════════════════════════════════════════════
#  ТЕКСТЫ И ФОРМАТИРОВАНИЕ
# ═══════════════════════════════════════════════════════════
def status_icon(task_type: str) -> str:
    if is_done(task_type):
        t = done_at(task_type)
        return f"✅ выполнено в {t}" if t else "✅ выполнено"
    return "⏳ ожидает"

def get_main_caption() -> str:
    sv, pr = get_today_info()
    today  = today_msk()
    wd     = WEEKDAYS_RU[today.weekday()]
    return (
        f"📅 <b>{wd}, {fmt_date(today)}</b>\n"
        f"{'─' * 26}\n\n"
        f"📦 <b>Сводки</b>\n"
        f"   👤 {sv}\n"
        f"   {status_icon('svodki')}\n\n"
        f"🧹 <b>Уборка процедурки</b>\n"
        f"   👤 {pr}\n"
        f"   {status_icon('procedurka')}"
    )

def get_week_text() -> str:
    schedule = get_schedule(7)
    lines = ["📆 <b>Расписание на 7 дней</b>\n"]
    for i, (d, sv, pr) in enumerate(schedule):
        wd   = WEEKDAYS_RU[d.weekday()]
        pref = "🔹 <b>Сегодня</b>" if i == 0 else ("🔸 Завтра" if i == 1 else f"   {wd} {d.day:02d}.{d.month:02d}")
        sv_done = " ✅" if is_done('svodki', d.isoformat()) else ""
        pr_done = " ✅" if is_done('procedurka', d.isoformat()) else ""
        lines.append(f"{pref}\n   📦 {sv}{sv_done}  🧹 {pr}{pr_done}")
    return "\n\n".join(lines)

def get_stats_text(year: int, month: int) -> str:
    sv_stats, pr_stats = get_monthly_stats(year, month)
    header = f"📊 <b>Статистика — {MONTHS_RU[month].capitalize()} {year}</b>\n{'─' * 26}\n\n"
    if not sv_stats and not pr_stats:
        return header + "Пока нет отмеченных выполнений."

    def render(stats: dict, icon: str, label: str) -> str:
        if not stats:
            return ""
        best = max(stats, key=stats.get)
        lines = [f"{icon} <b>{label}</b>"]
        for user, cnt in sorted(stats.items(), key=lambda x: x[1], reverse=True):
            crown = " 👑" if user == best else ""
            bar   = "█" * cnt + "░" * max(0, 5 - cnt)
            lines.append(f"  {bar} {user} — {cnt}×{crown}")
        return "\n".join(lines)

    parts = []
    if sv_stats:
        parts.append(render(sv_stats, "📦", "Сводки"))
    if pr_stats:
        parts.append(render(pr_stats, "🧹", "Уборка"))
    return header + "\n\n".join(parts)

# ═══════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ КАРТОЧКИ (ДЕМОТИВАТОР)
# ═══════════════════════════════════════════════════════════
def load_font(size: int) -> ImageFont.FreeTypeFont:
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "arial.ttf", "Arial.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

def load_font_regular(size: int) -> ImageFont.FreeTypeFont:
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "arial.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

def draw_centered(draw, text: str, y: int, font, color, width: int, shadow=None):
    bb = draw.textbbox((0, 0), text, font=font)
    w  = bb[2] - bb[0]
    x  = (width - w) // 2
    if shadow:
        draw.text((x + 2, y + 2), text, fill=shadow, font=font)
    draw.text((x, y), text, fill=color, font=font)
    return w, x

def generate_card(svodki_name: str, proc_name: str, date_str: str) -> BytesIO:
    W, H = 960, 680
    img  = Image.new('RGB', (W, H))
    draw = ImageDraw.Draw(img)

    # ── Фон: вертикальный градиент тёмно-синий → почти чёрный ──
    for y in range(H):
        t = y / H
        r = int(8  + t * 4)
        g = int(12 + t * 6)
        b = int(28 + t * 10)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # ── Внешние рамки ──
    draw.rectangle([(10, 10), (W-10, H-10)], outline=(180, 150, 60), width=3)
    draw.rectangle([(16, 16), (W-16, H-16)], outline=(80,  65, 20), width=1)

    # ── Угловые декоры ──
    cs = 22
    for cx, cy in [(18, 18), (W-18-cs, 18), (18, H-18-cs), (W-18-cs, H-18-cs)]:
        draw.rectangle([(cx, cy), (cx+cs, cy+cs)], outline=(200, 170, 70), width=2)
        draw.line([(cx+cs//2, cy), (cx+cs//2, cy+cs)], fill=(200, 170, 70), width=1)
        draw.line([(cx, cy+cs//2), (cx+cs, cy+cs//2)], fill=(200, 170, 70), width=1)

    # ── Горизонтальные линии-акценты ──
    for ly, alpha in [(58, 120), (60, 200), (H-60, 200), (H-58, 120)]:
        draw.line([(50, ly), (W-50, ly)], fill=(180, 150, 60), width=1)

    # ── Фоновые буквы (watermark) ──
    wm_font = load_font(180)
    wm_bb   = draw.textbbox((0, 0), "ДЖ", font=wm_font)
    wm_w    = wm_bb[2] - wm_bb[0]
    draw.text(((W - wm_w) // 2, H // 2 - 110), "ДЖ", fill=(18, 24, 45), font=wm_font)

    # ── Заголовок ──
    f_header  = load_font(17)
    f_label   = load_font_regular(17)
    f_name    = load_font(58)
    f_name_sm = load_font(46)
    f_date    = load_font_regular(19)
    f_motto   = load_font_regular(16)
    f_badge   = load_font(13)

    header_txt = "⚡  ДЕЖУРНЫЙ ПРИКАЗ  ⚡"
    draw_centered(draw, header_txt, 26, f_header, (210, 180, 80), W)

    # ── Блок 1: СВОДКИ ──
    block1_y = 80
    draw_centered(draw, "📦  ОТНОСИТЬ СВОДКИ", block1_y, f_label, (140, 140, 160), W)

    name1_font = f_name if len(svodki_name) <= 10 else f_name_sm
    _, nx1 = draw_centered(draw, svodki_name, block1_y + 34, name1_font, (245, 215, 100), W, shadow=(40, 30, 0))
    bb1    = draw.textbbox((0, 0), svodki_name, font=name1_font)
    nw1    = bb1[2] - bb1[0]
    nh1    = bb1[3] - bb1[1]
    # Подчёркивание имени
    uly1   = block1_y + 34 + nh1 + 6
    draw.line([(nx1, uly1), (nx1 + nw1, uly1)], fill=(180, 150, 60), width=2)

    # Бейдж статуса
    sv_done = is_done('svodki')
    badge1_txt = "✓ ВЫПОЛНЕНО" if sv_done else "• ОЖИДАЕТ"
    badge1_col = (80, 200, 120) if sv_done else (200, 180, 80)
    draw_centered(draw, badge1_txt, uly1 + 10, f_badge, badge1_col, W)

    # ── Разделитель ──
    mid_y = 320
    draw.line([(60, mid_y - 1), (W - 60, mid_y - 1)], fill=(40, 40, 60), width=1)
    draw.line([(60, mid_y),     (W - 60, mid_y)],     fill=(80, 65, 20), width=1)
    draw.line([(60, mid_y + 1), (W - 60, mid_y + 1)], fill=(40, 40, 60), width=1)
    draw_centered(draw, "✦", mid_y - 9, load_font(18), (180, 150, 60), W)

    # ── Блок 2: УБОРКА ──
    block2_y = mid_y + 22
    draw_centered(draw, "🧹  УБОРКА ПРОЦЕДУРКИ", block2_y, f_label, (140, 140, 160), W)

    name2_font = f_name if len(proc_name) <= 10 else f_name_sm
    _, nx2 = draw_centered(draw, proc_name, block2_y + 34, name2_font, (245, 215, 100), W, shadow=(40, 30, 0))
    bb2    = draw.textbbox((0, 0), proc_name, font=name2_font)
    nw2    = bb2[2] - bb2[0]
    nh2    = bb2[3] - bb2[1]
    uly2   = block2_y + 34 + nh2 + 6
    draw.line([(nx2, uly2), (nx2 + nw2, uly2)], fill=(180, 150, 60), width=2)

    pr_done = is_done('procedurka')
    badge2_txt = "✓ ВЫПОЛНЕНО" if pr_done else "• ОЖИДАЕТ"
    badge2_col = (80, 200, 120) if pr_done else (200, 180, 80)
    draw_centered(draw, badge2_txt, uly2 + 10, f_badge, badge2_col, W)

    # ── Нижняя панель ──
    draw_centered(draw, f"📅  {date_str}", H - 52, f_date, (100, 100, 120), W)
    motto = random.choice(MOTTOS)
    draw_centered(draw, motto, H - 30, f_motto, (60, 60, 80), W)

    bio = BytesIO()
    bio.name = 'card.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

# ═══════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════
def kb_main() -> InlineKeyboardMarkup:
    sv_done = is_done('svodki')
    pr_done = is_done('procedurka')
    shave   = gs('shave_enabled') == '1'

    sv_icon = "✅" if sv_done else "📦"
    pr_icon = "✅" if pr_done else "🧹"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{sv_icon} Сводки выполнены" if sv_done else f"{sv_icon} Сводки сданы?", callback_data="done_svodki"),
            InlineKeyboardButton(f"{pr_icon} Уборка выполнена" if pr_done else f"{pr_icon} Уборка сделана?", callback_data="done_procedurka"),
        ],
        [
            InlineKeyboardButton("📆 Неделя", callback_data="week"),
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
            InlineKeyboardButton("📋 Очереди", callback_data="lists_menu"),
        ],
        [
            InlineKeyboardButton("⚙️ Управление", callback_data="admin_menu"),
        ],
    ])

def kb_lists_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Список сводок", callback_data="list_svodki_0"),
            InlineKeyboardButton("🧹 Список уборки", callback_data="list_procedurka_0"),
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_main")],
    ])

def kb_admin() -> InlineKeyboardMarkup:
    shave = gs('shave_enabled') == '1'
    sv_i  = gs('svodki_interval') or '1'
    pr_i  = gs('procedurka_interval') or '2'
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Пропустить сводки",  callback_data="skip_svodki"),
         InlineKeyboardButton("⏭ Пропустить уборку",  callback_data="skip_procedurka")],
        [InlineKeyboardButton("✏️ Изменить список сводок",   callback_data="edit_hint_svodki")],
        [InlineKeyboardButton("✏️ Изменить список уборки",   callback_data="edit_hint_procedurka")],
        [InlineKeyboardButton("🎯 Назначить на сводки",       callback_data="set_hint_svodki"),
         InlineKeyboardButton("🎯 Назначить на уборку",       callback_data="set_hint_procedurka")],
        [InlineKeyboardButton(
            f"🪒 Бритьё: {'ВКЛ ✅' if shave else 'ВЫКЛ ❌'}",
            callback_data="toggle_shave"
        )],
        [InlineKeyboardButton("⏰ Время напоминаний", callback_data="remind_info")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_main")],
    ])

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_main")]])

def kb_done(task_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выполнено!", callback_data=f"done_{task_type}"),
        InlineKeyboardButton("↩️ Отменить",  callback_data=f"undo_{task_type}"),
    ]])

# ═══════════════════════════════════════════════════════════
#  ОТПРАВКА ГЛАВНОГО СООБЩЕНИЯ
# ═══════════════════════════════════════════════════════════
async def send_main(bot, chat_id: int, edit_msg=None):
    sv, pr  = get_today_info()
    today   = today_msk()
    wd      = WEEKDAYS_RU[today.weekday()]
    img_bio = generate_card(sv, pr, f"{wd}, {fmt_date(today)}")
    caption = get_main_caption()
    markup  = kb_main()

    await del_prev(bot, chat_id)
    if edit_msg:
        try:
            await edit_msg.delete()
        except Exception:
            pass
    sent = await bot.send_photo(
        chat_id=chat_id, photo=img_bio,
        caption=caption, parse_mode='HTML',
        reply_markup=markup
    )
    save_msg_id(sent.message_id)
    return sent

# ═══════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ КОМАНД
# ═══════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main(context.bot, CHAT_ID, edit_msg=None)

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main(context.bot, CHAT_ID)

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await del_prev(context.bot, CHAT_ID)
    sent = await update.message.reply_text(
        get_week_text(), parse_mode='HTML', reply_markup=kb_back_main()
    )
    save_msg_id(sent.message_id)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = now_msk()
    await del_prev(context.bot, CHAT_ID)
    sent = await update.message.reply_text(
        get_stats_text(today.year, today.month),
        parse_mode='HTML', reply_markup=kb_back_main()
    )
    save_msg_id(sent.message_id)

async def cmd_skip_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    msg = skip_person('svodki')
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_skip_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    msg = skip_person('procedurka')
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_set_current_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    if not context.args:
        await update.message.reply_text("Пример: <code>/set_current_svodki Максим</code>", parse_mode='HTML'); return
    ok, msg = set_current_person('svodki', ' '.join(context.args).strip())
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_set_current_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    if not context.args:
        await update.message.reply_text("Пример: <code>/set_current_procedurka Игоооорь</code>", parse_mode='HTML'); return
    ok, msg = set_current_person('procedurka', ' '.join(context.args).strip())
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_set_svodki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    if not context.args:
        await update.message.reply_text("Пример: <code>/set_svodki Саша, Олег, Максим</code>", parse_mode='HTML'); return
    names = [n.strip() for n in ' '.join(context.args).split(',') if n.strip()]
    if not names:
        await update.message.reply_text("❌ Список не может быть пустым."); return
    us('svodki', names)
    us('svodki_start_date', today_msk().isoformat())
    await update.message.reply_text(
        f"✅ Список сводок обновлён ({len(names)} чел.):\n" + ", ".join(names)
    )

async def cmd_set_procedurka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    if not context.args:
        await update.message.reply_text("Пример: <code>/set_procedurka Илья, Слава, Саша</code>", parse_mode='HTML'); return
    names = [n.strip() for n in ' '.join(context.args).split(',') if n.strip()]
    if not names:
        await update.message.reply_text("❌ Список не может быть пустым."); return
    us('procedurka', names)
    us('procedurka_start_date', today_msk().isoformat())
    await update.message.reply_text(
        f"✅ Список уборки обновлён ({len(names)} чел.):\n" + ", ".join(names)
    )

async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    if not context.args or len(context.args) < 2:
        sv_i = gs('svodki_interval') or '1'
        pr_i = gs('procedurka_interval') or '2'
        await update.message.reply_text(
            f"⏱ <b>Интервалы ротации:</b>\n"
            f"📦 Сводки — каждые <b>{sv_i}</b> дн.\n"
            f"🧹 Уборка — каждые <b>{pr_i}</b> дн.\n\n"
            f"Изменить:\n"
            f"<code>/set_interval svodki 1</code>\n"
            f"<code>/set_interval procedurka 2</code>",
            parse_mode='HTML'
        ); return
    key = context.args[0].lower()
    if key not in ('svodki', 'procedurka'):
        await update.message.reply_text("❌ Укажите: svodki или procedurka"); return
    try:
        val = int(context.args[1])
        assert 1 <= val <= 30
    except Exception:
        await update.message.reply_text("❌ Интервал: от 1 до 30 дней."); return
    us(f'{key}_interval', str(val))
    label = "Сводки" if key == 'svodki' else "Уборка"
    await update.message.reply_text(
        f"✅ <b>{label}</b> — смена каждые <b>{val}</b> дн.", parse_mode='HTML'
    )

async def cmd_set_remind_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    if not context.args or len(context.args) < 2:
        h1 = gs('svodki_remind_hour') or '20'
        h2 = gs('procedurka_remind_hour') or '23'
        await update.message.reply_text(
            f"⏰ Сейчас: сводки {h1}:00, уборка {h2}:00 МСК\n"
            f"Изменить: <code>/set_remind_times 20 23</code>",
            parse_mode='HTML'
        ); return
    try:
        h1, h2 = int(context.args[0]), int(context.args[1])
        assert 0 <= h1 <= 23 and 0 <= h2 <= 23
    except Exception:
        await update.message.reply_text("❌ Часы от 0 до 23."); return
    us('svodki_remind_hour', str(h1))
    us('procedurka_remind_hour', str(h2))
    await update.message.reply_text(
        f"✅ Напоминания: сводки в <b>{h1}:00</b>, уборка в <b>{h2}:00</b> МСК\n"
        f"(вступит в силу завтра)",
        parse_mode='HTML'
    )

async def cmd_toggle_shave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Только для администраторов."); return
    cur = gs('shave_enabled')
    new = '0' if cur == '1' else '1'
    us('shave_enabled', new)
    await update.message.reply_text(
        f"🪒 Напоминание о бритье {'включено ✅' if new == '1' else 'выключено ❌'}"
    )

# ═══════════════════════════════════════════════════════════
#  ОБРАБОТЧИК КНОПОК
# ═══════════════════════════════════════════════════════════
async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик done_ и undo_ — регистрируется ПЕРВЫМ."""
    query = update.callback_query
    await query.answer()
    data = query.data
    today_str = today_msk().isoformat()

    if data.startswith("done_"):
        task_type = data[5:]  # svodki / procedurka
        sv, pr    = get_today_info()
        user_name = sv if task_type == 'svodki' else pr
        label     = "сводки" if task_type == 'svodki' else "уборку"

        if mark_done(today_str, task_type, user_name):
            t = done_at(task_type)
            await query.edit_message_text(
                f"✅ <b>{user_name}</b> отметил {label}!\n"
                f"Время выполнения: <b>{t}</b>\n\n"
                f"Можно отменить нажав кнопку ниже 👇",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Отменить отметку", callback_data=f"undo_{task_type}")
                ]])
            )
        else:
            t = done_at(task_type)
            await query.answer(f"Уже отмечено в {t} ✅", show_alert=True)

    elif data.startswith("undo_"):
        task_type = data[5:]
        label     = "сводки" if task_type == 'svodki' else "уборки"
        if unmark_done(today_str, task_type):
            await query.edit_message_text(
                f"↩️ Отметка о выполнении <b>{label}</b> отменена.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ Выполнено!", callback_data=f"done_{task_type}"
                    )
                ]])
            )
        else:
            await query.answer("Отметки и так нет.", show_alert=True)

    # Обновляем главное сообщение если оно есть
    mid = get_msg_id()
    if mid:
        sv, pr  = get_today_info()
        today   = today_msk()
        wd      = WEEKDAYS_RU[today.weekday()]
        img_bio = generate_card(sv, pr, f"{wd}, {fmt_date(today)}")
        try:
            await context.bot.edit_message_media(
                chat_id=CHAT_ID,
                message_id=mid,
                media=__import__('telegram').InputMediaPhoto(
                    media=img_bio,
                    caption=get_main_caption(),
                    parse_mode='HTML'
                ),
                reply_markup=kb_main()
            )
        except Exception:
            pass

    # Авто-удаление через 15 сек
    async def _del():
        await asyncio.sleep(15)
        try:
            await query.message.delete()
        except Exception:
            pass
    asyncio.create_task(_del())


async def cb_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    async def reply_text(text, markup=None, **kw):
        await del_prev(context.bot, CHAT_ID)
        sent = await query.message.reply_text(
            text, parse_mode='HTML',
            reply_markup=markup or kb_back_main(), **kw
        )
        save_msg_id(sent.message_id)
        try:
            await query.delete_message()
        except Exception:
            pass

    # ── Главное меню (обновить фото) ──
    if data == "back_main":
        await send_main(context.bot, CHAT_ID, edit_msg=query.message)
        return

    # ── Расписание на неделю ──
    elif data == "week":
        await reply_text(get_week_text())

    # ── Статистика ──
    elif data == "stats":
        today = now_msk()
        await reply_text(get_stats_text(today.year, today.month))

    # ── Меню списков ──
    elif data == "lists_menu":
        await reply_text("Выберите список:", markup=kb_lists_menu())

    # ── Просмотр списка ──
    elif data.startswith("list_"):
        parts     = data.split('_')
        list_type = parts[1]
        page      = int(parts[2])
        people    = gs(list_type)
        sv_today, pr_today = get_today_info()
        today_person = sv_today if list_type == 'svodki' else pr_today
        interval  = int(gs(f'{list_type}_interval') or '1')
        label_int = f"меняется каждые {interval} дн." if interval > 1 else "меняется каждый день"
        title     = "📦 Сводки" if list_type == 'svodki' else "🧹 Уборка"
        per_page  = 8
        start     = page * per_page
        total_p   = (len(people) + per_page - 1) // per_page
        lines     = [f"{title}  <i>({label_int})</i>\n"]
        for i, name in enumerate(people[start:start+per_page], start=start+1):
            marker = "🟢 <b>" if name == today_person else "   "
            suffix = "</b>  ← сегодня" if name == today_person else ""
            lines.append(f"{marker}{i}. {name}{suffix}")
        text = "\n".join(lines)
        nav  = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"list_{list_type}_{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_p}", callback_data="noop"))
        if page + 1 < total_p:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"list_{list_type}_{page+1}"))
        keyboard = []
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="lists_menu")])
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

    # ── Меню управления (только текст, без фото) ──
    elif data == "admin_menu":
        if update.effective_user.id not in ADMIN_IDS:
            await query.answer("⛔️ Только для администраторов!", show_alert=True)
            return
        await reply_text("⚙️ <b>Управление</b>", markup=kb_admin())

    # ── Пропуск человека ──
    elif data in ("skip_svodki", "skip_procedurka"):
        if update.effective_user.id not in ADMIN_IDS:
            await query.answer("⛔️ Только для администраторов!", show_alert=True); return
        key = data[5:]  # svodki / procedurka
        msg = skip_person(key)
        await query.answer(msg.replace('<b>', '').replace('</b>', ''), show_alert=True)
        await send_main(context.bot, CHAT_ID, edit_msg=query.message)

    # ── Переключить бритьё ──
    elif data == "toggle_shave":
        if update.effective_user.id not in ADMIN_IDS:
            await query.answer("⛔️ Только для администраторов!", show_alert=True); return
        cur = gs('shave_enabled')
        new = '0' if cur == '1' else '1'
        us('shave_enabled', new)
        status = "включено ✅" if new == '1' else "выключено ❌"
        await query.answer(f"🪒 Напоминание о бритье {status}", show_alert=True)
        # Обновляем меню
        await reply_text("⚙️ <b>Управление</b>", markup=kb_admin())

    # ── Подсказки для команд ──
    elif data == "edit_hint_svodki":
        await reply_text(
            "✏️ Отправьте команду:\n"
            "<code>/set_svodki Саша, Олег, Максим, Игорь, ...</code>\n\n"
            "Имена через запятую. Список будет сброшен с сегодня."
        )
    elif data == "edit_hint_procedurka":
        await reply_text(
            "✏️ Отправьте команду:\n"
            "<code>/set_procedurka Илья, Слава, Саша, ...</code>\n\n"
            "Имена через запятую. Список будет сброшен с сегодня."
        )
    elif data == "set_hint_svodki":
        people = gs('svodki')
        names_list = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(people))
        await reply_text(
            f"🎯 Назначить конкретного человека на сводки сегодня:\n"
            f"<code>/set_current_svodki ИМЯ</code>\n\n"
            f"Доступные имена:\n{names_list}"
        )
    elif data == "set_hint_procedurka":
        people = gs('procedurka')
        names_list = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(people))
        await reply_text(
            f"🎯 Назначить конкретного человека на уборку сегодня:\n"
            f"<code>/set_current_procedurka ИМЯ</code>\n\n"
            f"Доступные имена:\n{names_list}"
        )

    # ── Информация о напоминаниях ──
    elif data == "remind_info":
        h1 = gs('svodki_remind_hour') or '20'
        h2 = gs('procedurka_remind_hour') or '23'
        hd = gs('daily_hour') or '18'
        sv_i = gs('svodki_interval') or '1'
        pr_i = gs('procedurka_interval') or '2'
        await reply_text(
            f"⏰ <b>Настройки напоминаний</b>\n\n"
            f"📢 Ежедневная карточка: <b>{hd}:00</b> МСК\n"
            f"📦 Проверка сводок:      <b>{h1}:00</b> МСК\n"
            f"🧹 Проверка уборки:      <b>{h2}:00</b> МСК\n\n"
            f"🔄 Интервалы ротации:\n"
            f"   📦 Сводки — каждые <b>{sv_i}</b> дн.\n"
            f"   🧹 Уборка — каждые <b>{pr_i}</b> дн.\n\n"
            f"Изменить время:\n"
            f"<code>/set_remind_times 20 23</code>\n"
            f"Изменить интервал:\n"
            f"<code>/set_interval procedurka 2</code>"
        )

    elif data == "noop":
        pass

# ═══════════════════════════════════════════════════════════
#  ПЛАНИРОВЩИК
# ═══════════════════════════════════════════════════════════
async def job_daily(app):
    """Ежедневная карточка в 18:00."""
    sv, pr  = get_today_info()
    today   = today_msk()
    wd      = WEEKDAYS_RU[today.weekday()]
    img_bio = generate_card(sv, pr, f"{wd}, {fmt_date(today)}")
    await del_prev(app.bot, CHAT_ID)
    sent = await app.bot.send_photo(
        chat_id=CHAT_ID, photo=img_bio,
        caption=get_main_caption(), parse_mode='HTML',
        reply_markup=kb_main()
    )
    save_msg_id(sent.message_id)

    # Запуск таймеров проверки выполнения
    sv_h = int(gs('svodki_remind_hour') or '20')
    pr_h = int(gs('procedurka_remind_hour') or '23')
    asyncio.create_task(_remind_check(app, 'svodki', sv_h))
    asyncio.create_task(_remind_check(app, 'procedurka', pr_h))

async def _remind_check(app, task_type: str, target_hour: int):
    """Ждёт до target_hour, потом отправляет запрос если не выполнено."""
    now    = now_msk()
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())

    if is_done(task_type):
        logging.info(f"[remind] {task_type} уже выполнено, пропускаем")
        return

    sv, pr    = get_today_info()
    user      = sv if task_type == 'svodki' else pr
    label     = "сводки" if task_type == 'svodki' else "уборку процедурки"
    label_btn = "Сводки сданы!" if task_type == 'svodki' else "Уборка сделана!"

    text = (
        f"⏰ <b>Напоминание</b>\n\n"
        f"<b>{user}</b>, не забудьте отметить {label}!\n"
        f"Нажмите кнопку когда выполнено 👇"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ {label_btn}", callback_data=f"done_{task_type}")
    ]])
    try:
        sent = await app.bot.send_message(
            chat_id=CHAT_ID, text=text,
            parse_mode='HTML', reply_markup=keyboard
        )
        save_reminder(CHAT_ID, sent.message_id, task_type, today_msk().isoformat())

        async def _auto_del():
            await asyncio.sleep(7200)
            try: await sent.delete()
            except Exception: pass
        asyncio.create_task(_auto_del())
    except Exception as e:
        logging.error(f"[remind] ошибка отправки {task_type}: {e}")

async def job_shave(app):
    if gs('shave_enabled') == '1':
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="🧔‍♂️ <b>Время!</b> Не забудьте побриться и выглядеть опрятно.",
            parse_mode='HTML'
        )

async def job_monthly_report(app):
    today = now_msk()
    if today.day != 1:
        return
    last = today.replace(day=1) - timedelta(days=1)
    sv_s, pr_s = get_monthly_stats(last.year, last.month)
    if not sv_s and not pr_s:
        return
    text = get_stats_text(last.year, last.month).replace(
        f"{MONTHS_RU[today.month].capitalize()} {today.year}",
        f"{MONTHS_RU[last.month].capitalize()} {last.year} — итоги"
    )
    await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='HTML')

# ═══════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════
def main():
    init_db()
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
        datefmt='%d.%m %H:%M:%S'
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    app = Application.builder().token(TOKEN).build()

    # ── Команды ──
    for cmd, handler in [
        ("start",                   cmd_start),
        ("today",                   cmd_today),
        ("week",                    cmd_week),
        ("stats",                   cmd_stats),
        ("skip_svodki",             cmd_skip_svodki),
        ("skip_procedurka",         cmd_skip_procedurka),
        ("set_current_svodki",      cmd_set_current_svodki),
        ("set_current_procedurka",  cmd_set_current_procedurka),
        ("set_svodki",              cmd_set_svodki),
        ("set_procedurka",          cmd_set_procedurka),
        ("set_interval",            cmd_set_interval),
        ("set_remind_times",        cmd_set_remind_times),
        ("toggle_shave",            cmd_toggle_shave),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    # ── Кнопки (done/undo — ПЕРВЫМИ) ──
    app.add_handler(CallbackQueryHandler(cb_done,    pattern=r"^(done|undo)_"))
    app.add_handler(CallbackQueryHandler(cb_buttons))

    # ── Планировщик ──
    sched = AsyncIOScheduler(timezone="Europe/Moscow")
    daily_h = int(gs('daily_hour') or '18')
    sched.add_job(lambda: asyncio.create_task(job_daily(app)),          "cron", hour=daily_h, minute=0)
    sched.add_job(lambda: asyncio.create_task(job_shave(app)),          "cron", hour=17,      minute=0)
    sched.add_job(lambda: asyncio.create_task(job_monthly_report(app)), "cron", day=1,        hour=10, minute=0)
    sched.start()

    sv_i = gs('svodki_interval') or '1'
    pr_i = gs('procedurka_interval') or '2'
    sv, pr = get_today_info()
    print(f"\n{'═'*50}")
    print(f"  ✅ Дежурный бот запущен  |  MSK")
    print(f"  📦 Сводки:  {sv} (каждые {sv_i} дн.)")
    print(f"  🧹 Уборка:  {pr} (каждые {pr_i} дн.)")
    print(f"  📢 Карточка: {daily_h}:00  |  🪒 Бритьё: {'вкл' if gs('shave_enabled')=='1' else 'выкл'}")
    print(f"{'═'*50}\n")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
