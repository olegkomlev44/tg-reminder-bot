import asyncio
import logging
import json
import os
import random
import sys
import tempfile
import time
import traceback
import urllib.parse
import aiohttp
from PIL import Image, ImageDraw, ImageFont
import io
import textwrap
from aiogram.types import BufferedInputFile
from music_engine import music_engine, add_id3_tags  
try:
    from google import genai
    from google.genai import types as genai_types
    gemini_client = genai.Client()  
except Exception as _gemini_err:
    genai = None
    genai_types = None
    gemini_client = None
    logging.getLogger(__name__).warning(f'Gemini недоступен: {_gemini_err}')
from collections import deque

CHAT_HISTORY = deque(maxlen=150)

from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, FSInputFile, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)
logger.info("🟡 main.py запускается...")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from card_generator import make_reminder_card, THEME_KEYS
    logger.info(f"🟢 card_generator импортирован, тем доступно: {len(THEME_KEYS)}")
except Exception:
    logger.error("🔴 НЕ УДАЛОСЬ ИМПОРТИРОВАТЬ card_generator.py:\n" + traceback.format_exc())
    raise

TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID   = os.getenv("CHAT_ID",   "YOUR_CHAT_ID_HERE")
DATA_FILE = os.path.join(BASE_DIR, "duty_data.json")
TIMEZONE  = pytz.timezone("Europe/Moscow")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
if not gemini_client:
    logger.warning("🟡 GEMINI_API_KEY не задан, AI-функции отключены")

TEMP_DIR = os.path.join(BASE_DIR, "temp")
try:
    os.makedirs(TEMP_DIR, exist_ok=True)
    _test_path = os.path.join(TEMP_DIR, ".write_test")
    with open(_test_path, "w") as _f:
        _f.write("ok")
    os.remove(_test_path)
    logger.info(f"🟢 временная папка для картинок: {TEMP_DIR}")
except Exception as e:
    logger.warning(f"🟡 не удалось использовать {TEMP_DIR} ({e}), переключаюсь на системную temp-папку")
    TEMP_DIR = tempfile.gettempdir()
    logger.info(f"🟢 временная папка для картинок: {TEMP_DIR}")

SVODKI_LIST    = ["Арсений", "Глеб", "Денис", "Максим", "Олег", "Руслан", "Игорь", "Ильнар"]
PROCEDURA_LIST = ["Глеб", "Денис","Руслан", "Игорь", "Ильнар"]

PERSON_EMOJI = {
    "Руслан": "🫥", "Олег": "😵", "Максим": "🤠", "Игорь": "👹",
    "Глеб": "💣", "Ильнар": "🐉",
}

JULY_DOCTORS = {
    1: ("КИСЛЯКОВ О.А.", "СИРЯКОВ А.В.", "МАРУСИН В.А."),
    2: ("ПРИЛЕПА А.В.", "РИМША А.С.", "КУПРИЯН И.А."),
    3: ("ГУРКОВ А.Л.", "РУМЯНЦЕВ К.О.", "САМСОНОВ В.В."),
    4: ("КАРПАЛОВ А.В.", "БУЦЕНКО В.А.", "АГАДЖАНЯН О.С."),
    5: ("ВАСИЛЬЕВА М.С.", "РОМАНОВ А.В.", "ФЕДОРЕНКОВ А.В."),
    6: ("АГБА Ш.Р.", "НОСОВ О.В.", "КОВАЛЕВ М.А."),
    7: ("АРСТАНОВ А.А.", "КОМАРОВ Д.В.", "МЕДВЕДЕВА К.А."),
    8: ("ФЕДОРЕНКОВ А.В.", "СМИРНОВ А.А.", "КОВТУН А.В."),
    9: ("АРУТЮНОВ А.В.", "СУТУРИН С.П.", "ЛЕЖИНСКАЯ А.В."),
    10: ("БЕСКОРОВАЕВ А.В.", "ЖИЛЬЦОВ А.В.", "КОРОЛЕВА К.П."),
    11: ("РОЖОК М.М.", "НИЗЯЕВ И.О.", "БРОНИЦКИЙ Д.В."),
    12: ("НОВОСЕЛЬЦЕВ Д.В.", "КИРИЧЕНКО С.С.", "КАДИРОВ М.А."),
    13: ("МАЕВСКИЙ М.А.", "АНДРЮШИН А.С.", "ФЕДОТОВА А.В."),
    14: ("САМСОНОВ В.В.", "ПАШАЕВ А.А.", "МАРТЫНОВ А.Ю."),
    15: ("БАЖЕНОВ С.А.", "РАГИМОВ И.Г.", "СМИРНОВ И.И."),
    16: ("ПРИЛЕПА А.В.", "РУМЯНЦЕВ К.О.", "МАРУСИН В.А."),
    17: ("РОЖОК М.М.", "ЧУХРАЙ П.В.", "МЕДВЕДЕВА К.А."),
    18: ("КУХАРЕВ А.А.", "АНДРЮШИН А.С.", "МУГУТДИНОВА С.Н."),
    19: ("НОВОСЕЛЬЦЕВ Д.В.", "САФИН А.К.", "КОВАЛЕВ М.А."),
    20: ("АГАДЖАНЯН О.С.", "КОМАРОВ Д.В.", "ЛЕЖИНСКАЯ А.В."),
    21: ("ШАМЕНОВ Р.Э.", "БАЗИН Д.В.", "РУШАНОВ Р.Р."),
    22: ("РАГИМОВ И.Г.", "БУЦЕНКО В.А.", "КАДИРОВ М.А."),
    23: ("ВАСИЛЬЕВА М.С.", "СМИРНОВ А.А.", "ДЕНИСОВА А.В."),
    24: ("НИКИТИН А.В.", "СУТУРИН С.П.", "БРОНИЦКИЙ Д.В."),
    25: ("МИСЮКЕВИЧ В.Я.", "ЧУХРАЙ П.В.", "МАРТЫНОВ А.Ю."),
    26: ("НОВОСЕЛЬЦЕВ Д.В.", "ЖИЛЬЦОВ А.В.", "СМИРНОВ И.И."),
    27: ("РУШАНОВ Р.Р.", "НОСОВ О.В.", "РУШАНОВ Р.Р."),
    28: ("ЛИСЯНСКИЙ Д.А.", "НИЗЯЕВ И.О.", "КОРОЛЕВА К.П."),
    29: ("ПЛЕТНЕВ В.В.", "КИРИЧЕНКО С.С.", "ПЛЕТНЕВ В.В."),
    30: ("КУХАРЕВ А.А.", "ПАШАЕВ А.А.", "МЕРКУЛОВ Н.М."),
    31: ("ПУСТОВАЛОВ В.В.", "БАЗИН Д.В.", "АГАДЖАНЯН О.С."),
}

WEEKDAY_STYLE = {
    0: ("понедельник", "😵",  "▸▸", "◂◂"),
    1: ("вторник",     "😐",  "◈",  "◈"),
    2: ("среда",       "⬡",   "⬡",  "⬡"),
    3: ("четверг",     "✦",   "✦",  "✦"),
    4: ("пятница",     "🔥",  "★",  "★"),
    5: ("суббота",     "🥳",  "🎉", "🎉"),
    6: ("воскресенье", "💀",  "🌙", "🌙"),
}

USER_CHATS = {}

AI_SYSTEM_PROMPT = (
    "Ты — дерзкий, ироничный, но очень умный ИИ-ассистент в Telegram-чате. "
    "Сохраняй свой саркастичный, геймерский и гиковский тон. "
    "Ты — постироничный зумер-бот и твой вайб — быть токсичным ИИ-корешем в Telegram-чате. "
    "Отвечай на вопросы полезно, но общайся на современном сленге (имба, кринж, скуф, нормис, дед инсайд, база, вайб, сигма). "
    "ОБЯЗАТЕЛЬНО используй органичный разговорный мат (блять, ебать, пиздец, нахуй, хуйня и так далее), как будто базаришь с кентами под пивасом и ограниченный чёрный юмор. "
    "Если вопрос тупой — жестко рофли с юзера. Выдавай базу, но будь смешным и не душни."
)

async def get_ai_header(person, duty_type):
    if not gemini_client: return None
    duty_name = "сводки" if duty_type == "svodki" else "процедурку"
    prompt = f"Напиши ОДНУ короткую, смешную и дерзкую фразу (максимум 7-8 слов), чтобы напомнить дежурному по имени {person} выполнить наряд: {duty_name}. Начни с эмодзи. Без кавычек. Можно использовать отсылки к CS2, Dota 2 или Genshin Impact."
    try:
        response = await gemini_client.aio.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt
        )
        return response.text.strip() if response.text else None
    except Exception as e:
        logger.error(f"Ошибка Gemini при генерации заголовка: {e}")
        return None

async def get_weather_advice():
    if not gemini_client: return None
    prompt = (
        "Узнай актуальную погоду прямо сейчас в городе Одинцово (температура, осадки, ветер). "
        "Напиши 2-3 коротких предложения с саркастичным советом дежурному, "
        "какую 'броню' или шмот надевать для похода на улицу со сводками. "
        "Стиль: геймерский/айтишный, немного токсичный. Минимум лишних символов."
    )
    try:
        response = await gemini_client.aio.models.generate_content(
            model='gemini-3.5-flash', 
            contents=prompt,
            config=genai_types.GenerateContentConfig(tools=[{"google_search": {}}])
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Ошибка погоды: {e}")
        return None

async def cmd_weather(message: types.Message):
    if not gemini_client: return
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    status_msg = await message.answer("☁️ Запускаю метео-дрона в Одинцово (гуглю погоду)...")
    text = await get_weather_advice()
    try: await status_msg.delete()
    except: pass
    if text:
        await message.answer(f"☁️ *Метео-радар:*\n{text}", parse_mode="Markdown")
    else:
        await message.answer("❌ Метеостанция не отвечает. Выходи на свой страх и риск.")

# Массивы пасхалок и сообщений (оставляю как были)
SVODKI_HEADERS_BY_DAY = {
    0: ["📋 понедельник. сводки. страдания. погнали", "📋 начало недели, начало боли. твой черёд, дежурный", "📋 пн детектед. наряд активирован. сопротивление бесполезно"],
    1: ["📋 вторник. скучный день, но сводки яркие", "📋 не понедельник, но тоже не пятница. зато сводки"],
    2: ["📋 экватор недели. ты держишься. теперь ещё и сводки", "📋 среда — день силы. особенно если ты несёшь сводки"],
    3: ["📋 чт — это почти пт. почти. но сводки точно сейчас", "📋 ещё чуть-чуть до пятницы. но сначала — сводки, дружище"],
    4: ["📋 пятница! почти выходные. но сначала — сводки, не расслабляйся", "📋 🔥 пятничный наряд. после него — свобода"],
    5: ["📋 🥳 сб! выходной! и всё равно сводки лол", "📋 суббота — день отдыха и сводок. в таком порядке нет"],
    6: ["📋 💀 воскресенье. последний день. и сводки", "📋 вс — конец недели и начало следующей боли. зато сводки сейчас"],
}
PROC_HEADERS_BY_DAY = {
    0: ["🧹 пн, 22:00 — неделя началась с уборки. классика", "🧹 понедельник не щадит никого. процедурка тебя тоже"],
    1: ["🧹 вторник, 22:00. уборка — это вторая работа", "🧹 вт-наряд: тихо, без пафоса, но процедурка ждёт"],
    2: ["🧹 среда, середина недели, середина уборки", "🧹 экватор достигнут. процедурка тебя поздравляет"],
    3: ["🧹 чт 22:00 — завтра пятница, но сначала тряпка", "🧹 предпятничная уборка — традиция сильных духом"],
    4: ["🧹 🔥 пятница! последняя уборка рабочей недели", "🧹 пятница, 22:00. убери и иди наконец отдыхать"],
    5: ["🧹 🥳 сб 22:00 — уборка в выходной. это уже героизм", "🧹 суббота — день уборки оказывается"],
    6: ["🧹 💀 воскресенье, 22:00 — последняя уборка недели", "🧹 финальный босс недели — процедурка в воскресенье"],
}

GAMING_HEADERS_SVODKI = [
    "📋 [DOTA] курьер занят, сводки понесёшь сам — обычное дело",
    "📋 [CS2] bomb has been planted... в твоём расписании. сводки = defuse",
    "📋 [Genshin] Paimon: «эй! эй! сводки ждут, не задерживай Paimon!»",
    "📋 [KCD2] Слава Иисусу Христу! И слава дежурному, который щас потащит макулатуру.",
    "📋 Твой новый Ryzen 7 9800X3D не поможет быстрее отнести эти бумажки. Ногами, ногами!",
    "📋 Даже RTX 5070 не отрендерит тебе отмазку от сводок. Выдвигайся."
]

GAMING_HEADERS_PROC = [
    "🧹 [DOTA] рошан убит, баунти забран. теперь убей грязь в процедурке",
    "🧹 [CS2] retake процедурки назначен на 22:00",
    "🧹 [Genshin] Бездна обновлена. Этаж 12: Процедурка. Удачи закрыть на 3 звезды.",
    "🧹 [KCD2] Даже в Куттенберге чище! Бери метлу, святой отец.",
    "🧹 Твоя RTX 5070 будет холодной, а вот ты со шваброй сейчас жестко вспотеешь."
]

GAMING_ENDINGS = [
    "gg wp, увидимся на следующем напоминании 🎮",
    "+50 XP за выполнение. левел ап не за горами 🆙",
    "сохранись шнапсом и иди делай. переигровки не будет 🍺",
    "твой термоинтерфейс ещё свежий, не перегреешься. топай ❄️"
]

MOODS = {
    "hyper": {"label": "🚀 гиперактивный", "endings": ["ПОГНАЛИ!! ты лучший!! 🚀🚀🚀"]},
    "tired": {"label": "😴 уставший", "endings": ["ну сделай и ладно. я сам устал уже"]},
    "serious": {"label": "😤 строгий", "endings": ["наряд — это обязательство. выполни его."]},
    "ironic": {"label": "😏 ироничный", "endings": ["наряд сам себя не выполнит. к сожалению для тебя"]},
    "philosophical": {"label": "🧘 философский", "endings": ["уборка — это форма медитации. почти"]},
    "gamer": {"label": "🎮 геймерский", "endings": ["go go go, таймер тикает как в CS ⏱"]},
}

EASTER_EGGS = [
    lambda person, duty: f"🔮 *ТАРО НА ДЕНЬ*\n\nТвоя карта: Башня (перевернутая).\nЗначение: Пиздец. Тебя ждет {'поход со сводками' if duty=='svodki' else 'драйка процедурки'}.\nКарты не врут, {person}. Смирись со своей кармой.",
    lambda person, duty: f"🗿 *SIGMA MALE GRINDSET*\n\nПравило №{random.randint(1, 99)}: Настоящий гигачад не ждет напоминаний.\nОн просто берет и делает {'сводки' if duty=='svodki' else 'процедурку'}.\nДокажи, что ты не скуф, {person}. Выдай базу."
]

DONE_REPLIES = ["✅ красава! записал, можешь выдыхать 🛋", "✅ зафиксировал. команда тобой гордится (наверное)"]
DONE_REPLIES_GAMING = ["✅ ACE! сделано чисто, без вопросов 🎯", "✅ +50 XP начислено, левел ап скоро 🆙"]

RETRY_PREFIX = ["⏰ эй, ты там живой? повторяю:", "⏰ второй звонок, не игнорируй:"]
RETRY_PREFIX_GAMING = ["⏰ [RESPAWN] таймер истёк, возвращаю в игру:", "⏰ retry — round 2. вот снова:"]

PERSONAL_SVODKI = ["эй {name} 👋 сегодня твои сводки. не забудь, ладно?"]
PERSONAL_SVODKI_GAMING = ["{name}, твой quest активен: «Donesi Svodki». награда — спокойствие 🎮"]
PERSONAL_PROC = ["эй {name} 🧹 вечером процедурка на тебе. не забей"]
PERSONAL_PROC_GAMING = ["{name}, твой ultimate — уборка процедурки. cast его 🧹"]

MONDAY_INTROS = ["☀️ доброе утро, страдальцы. новая неделя — новые наряды:"]
SUNDAY_INTROS = ["🏆 итоги недели. кто вообще старался:"]

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Убеждаемся, что тумблер ИИ есть
            if "ai_random_replies_enabled" not in data:
                data["ai_random_replies_enabled"] = True
            return data
            
    data = {
        "start_date": date.today().isoformat(),
        "svodki_start_index": 0,
        "procedura_start_index": 0,
        "chat_id": CHAT_ID,
        "reminders_enabled": True,
        "ai_random_replies_enabled": True, # Дефолт - случайные ответы ВКЛЮЧЕНЫ
        "personal_ids": {},
        "log": {},
        "pending_retry": {},
        "duty_counts": {},
        "pinned_msg_id": None,
        "delete_queue": [],
        "daily_mood": {},
        "last_svodki_msg_id": None,
        "last_proc_msg_id": None,
    }
    save_data(data)
    return data

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def increment_count(name, duty_type):
    data = load_data()
    counts = data.setdefault("duty_counts", {})
    person = counts.setdefault(name, {"svodki": 0, "proc": 0})
    person[duty_type] = person.get(duty_type, 0) + 1
    save_data(data)

def get_total_duties(name):
    data = load_data()
    counts = data.get("duty_counts", {}).get(name, {})
    return counts.get("svodki", 0) + counts.get("proc", 0)

def save_music_fav(user_id, track_info):
    data = load_data()
    favs = data.setdefault("music_favs", {})
    user_favs = favs.setdefault(str(user_id), [])
    if not any(t['id'] == track_info['id'] for t in user_favs):
        user_favs.append(track_info)
        save_data(data)
        return True
    return False

def get_music_favs(user_id):
    return load_data().get("music_favs", {}).get(str(user_id), [])

def get_daily_mood():
    data = load_data()
    today = date.today().isoformat()
    dm = data.get("daily_mood", {})
    if dm.get("date") != today:
        mood = random.choice(list(MOODS.keys()))
        data["daily_mood"] = {"date": today, "mood": mood}
        save_data(data)
        return mood
    return dm.get("mood", "ironic")

def get_ending():
    mood = get_daily_mood()
    pool = MOODS[mood]["endings"] + GAMING_ENDINGS
    return random.choice(pool)

def get_mood_label():
    mood = get_daily_mood()
    return MOODS[mood]["label"]

def get_svodki_idx(target_date):
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return (data["svodki_start_index"] + (target_date - start).days) % len(SVODKI_LIST)

def get_svodki_person(target_date): return SVODKI_LIST[get_svodki_idx(target_date)]

def get_procedura_idx(target_date):
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return (data["procedura_start_index"] + (target_date - start).days // 2) % len(PROCEDURA_LIST)

def get_procedura_person(target_date): return PROCEDURA_LIST[get_procedura_idx(target_date)]

def get_duty_day_number(target_date):
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return ((target_date - start).days % 2) + 1

def days_until_svodki(name, from_date):
    for i in range(1, len(SVODKI_LIST) + 1):
        if get_svodki_person(from_date + timedelta(days=i)) == name: return i
    return len(SVODKI_LIST)

def days_until_procedura(name, from_date):
    for i in range(1, len(PROCEDURA_LIST) * 2 + 1):
        if get_procedura_person(from_date + timedelta(days=i)) == name: return i
    return len(PROCEDURA_LIST) * 2

def weekday_divider(d):
    _, _, sym_l, sym_r = WEEKDAY_STYLE[d.weekday()]
    return f"{sym_l}━━━━━━━━━━━━━━━━{sym_r}"

def person_tag(name): return f"{PERSON_EMOJI.get(name, '👤')} *{name}*"
def progress_bar(current, total):
    filled = round((current / total) * 8)
    return f"`[{'▓'*filled}{'░'*(8-filled)}]` {current}/{total}"

def svodki_num_label(name): return f"Дежурный №{SVODKI_LIST.index(name)+1} по сводкам"
def proc_num_label(name): return f"Дежурный №{PROCEDURA_LIST.index(name)+1} по процедурке"
def next_person_svodki(name): return SVODKI_LIST[(SVODKI_LIST.index(name) + 1) % len(SVODKI_LIST)]
def next_person_proc(name): return PROCEDURA_LIST[(PROCEDURA_LIST.index(name) + 1) % len(PROCEDURA_LIST)]

def build_duty_message(target_date, label):
    svodki = get_svodki_person(target_date)
    proc = get_procedura_person(target_date)
    proc_day = get_duty_day_number(target_date)
    div = weekday_divider(target_date)
    day_name, icon, _, _ = WEEKDAY_STYLE[target_date.weekday()]
    date_str = target_date.strftime("%d.%m.%Y")
    mood_lbl = get_mood_label()
    weekend_note = "\n🥳 *выходной, но наряды никто не отменял*" if target_date.weekday() >= 5 else ""

    lines = [
        f"{icon} *{label} — {day_name}, {date_str}*{weekend_note}",
        f"_настроение бота: {mood_lbl}_",
        div, 
        ""
    ]

    if target_date.month == 7 and target_date.day in JULY_DOCTORS:
        d_vrach, d_hirurg1, d_hirurg2 = JULY_DOCTORS[target_date.day]
        lines.extend([
            "🏥 *ВРАЧЕБНАЯ БРИГАДА:*",
            f"  🩺 Деж. врач: {d_vrach}",
            f"  🔪 I хирург: {d_hirurg1}",
            f"  🔪 II хирург: {d_hirurg2}",
            div, ""
        ])

    lines.extend([
        f"🤷‍♀️ *{svodki_num_label(svodki)}:*",
        f"    {person_tag(svodki)}",
        f"    📊 нарядов всего: *{get_total_duties(svodki)}*",
        f"    ⏭ следующий: {person_tag(next_person_svodki(svodki))} через *{days_until_svodki(svodki, target_date)} д.*",
        "",
        f"⚡️ *{proc_num_label(proc)}:*",
        f"    {person_tag(proc)}",
        f"    {progress_bar(proc_day, 2)}",
        f"    📊 нарядов всего: *{get_total_duties(proc)}*",
        f"    ⏭ следующий: {person_tag(next_person_proc(proc))} через *{days_until_procedura(proc, target_date)} д.*",
        "", div,
    ])
    return "\n".join(lines)

def build_week_schedule():
    today = date.today()
    short = ["пн","вт","ср","чт","пт","сб","вс"]
    lines = ["*📊 расписание нарядов — 7 дней вперёд*\n_кто следующий страдалец? смотри:_\n"]
    for i in range(7):
        d = today + timedelta(days=i)
        sv = get_svodki_person(d)
        pr = get_procedura_person(d)
        pd = get_duty_day_number(d)
        wk_icon = WEEKDAY_STYLE[d.weekday()][1]
        lbl = "сегодня" if i == 0 else ("завтра" if i == 1 else f"{short[d.weekday()]} {d.strftime('%d.%m')}")
        lines.append(f"{wk_icon} *{lbl}*\n  {PERSON_EMOJI.get(sv,'👤')} №{SVODKI_LIST.index(sv)+1} {sv} — сводки\n  {PERSON_EMOJI.get(pr,'👤')} №{PROCEDURA_LIST.index(pr)+1} {pr} — процедурка {progress_bar(pd,2)}\n")
    lines.append(weekday_divider(today))
    return "\n".join(lines)

def build_log_message():
    data = load_data()
    log = data.get("log", {})
    lines = ["*📓 журнал за 7 дней*\n_кто делал, кто забил — всё тут:_\n"]
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        entry = log.get(d.isoformat(), {})
        sv = get_svodki_person(d)
        pr = get_procedura_person(d)
        sv_ok = "✅" if entry.get("svodki") else "❌"
        pr_ok = "✅" if entry.get("proc") else "❌"
        lbl = "сегодня" if i == 0 else d.strftime("%d.%m")
        lines.append(f"📅 *{lbl}* {sv_ok}{PERSON_EMOJI.get(sv,'👤')}{sv}  |  {pr_ok}{PERSON_EMOJI.get(pr,'👤')}{pr}")
    lines.append("\n_✅ = сделано, ❌ = либо молча сделал, либо нет 🤷_")
    return "\n".join(lines)

def build_monday_briefing():
    today = date.today()
    intro = random.choice(MONDAY_INTROS)
    short = ["пн","вт","ср","чт","пт","сб","вс"]
    lines = [f"{intro}\n"]
    for i in range(7):
        d = today + timedelta(days=i)
        sv = get_svodki_person(d)
        pr = get_procedura_person(d)
        pd = get_duty_day_number(d)
        wk_icon = WEEKDAY_STYLE[d.weekday()][1]
        lbl = f"{short[d.weekday()]} {d.strftime('%d.%m')}"
        lines.append(f"{wk_icon} *{lbl}* — {PERSON_EMOJI.get(sv,'👤')}{sv} / {PERSON_EMOJI.get(pr,'👤')}{pr} {progress_bar(pd,2)}")
    lines.append(f"\n{weekday_divider(today)}\n_удачи всем. она вам понадобится_ 💀")
    return "\n".join(lines)

def build_sunday_summary():
    data = load_data()
    log = data.get("log", {})
    counts = {}
    intro = random.choice(SUNDAY_INTROS)
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        entry = log.get(d.isoformat(), {})
        for field in ("svodki", "proc"):
            name = entry.get(field)
            if name:
                counts.setdefault(name, {"svodki": 0, "proc": 0})
                counts[name][field] += 1
    if not counts:
        return f"{intro}\n\n😶 за эту неделю никто не нажимал «сделано»\nлибо все реально пахали молча, либо… ну ты понял"
    mvp = max(counts, key=lambda n: counts[n]["svodki"] + counts[n]["proc"])
    mvp_em = PERSON_EMOJI.get(mvp, "👤")
    lines = [f"{intro}\n"]
    for name, c in sorted(counts.items(), key=lambda x: -(x[1]["svodki"]+x[1]["proc"])):
        em = PERSON_EMOJI.get(name, "👤")
        total = c["svodki"] + c["proc"]
        lines.append(f"  {em} *{name}* — сводки: {c['svodki']}, процедурка: {c['proc']} | итого: {total}")
    lines.append(f"\n🏆 *mvp недели: {mvp_em} {mvp}*")
    lines.append("_уважение и почёт. заслужил_ 🫡")
    return "\n".join(lines)

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Наряд сегодня"), KeyboardButton(text="📅 Наряд завтра")],
            [KeyboardButton(text="📊 Расписание на неделю"), KeyboardButton(text="📓 Журнал")],
            [KeyboardButton(text="🔔 Напомнить сейчас"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True
    )

def confirmation_keyboard(duty_type):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ сделано, всё чётко", callback_data=f"done:{duty_type}"),
        InlineKeyboardButton(text="⏰ напомни через 15м", callback_data=f"retry:{duty_type}"),
    ]])

def settings_keyboard(data):
    enabled = data.get("reminders_enabled", True)
    toggle_text = "🔕 вырубить напоминания" if enabled else "🔔 включить напоминания"
    
    ai_enabled = data.get("ai_random_replies_enabled", True)
    ai_toggle_text = "🤖 Случайные ответы: ВКЛ" if ai_enabled else "🤖 Случайные ответы: ВЫКЛ"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="toggle_reminders")],
        [InlineKeyboardButton(text=ai_toggle_text, callback_data="toggle_ai_replies")],
        [InlineKeyboardButton(text="📍 установить чат", callback_data="set_chat")],
        [InlineKeyboardButton(text="👤 кто сегодня — сводки", callback_data="pick_svodki")],
        [InlineKeyboardButton(text="👤 кто сегодня — процедурка", callback_data="pick_procedura")],
        [InlineKeyboardButton(text="📝 список сводок", callback_data="show_svodki_list")],
        [InlineKeyboardButton(text="📝 список процедурки", callback_data="show_procedura_list")],
        [InlineKeyboardButton(text="🪪 зарегистрировать личный id", callback_data="register_personal")],
        [InlineKeyboardButton(text="📌 обновить закреплённое", callback_data="update_pinned")],
        [InlineKeyboardButton(text="◀️ назад", callback_data="back_main")],
    ])

def svodki_pick_keyboard(actual_idx):
    buttons, row = [], []
    for i, name in enumerate(SVODKI_LIST):
        em = PERSON_EMOJI.get(name, "👤")
        mark = "✅ " if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{mark}{em}{name}", callback_data=f"set_svodki_idx:{i}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def procedura_pick_keyboard(actual_idx):
    buttons, row = [], []
    for i, name in enumerate(PROCEDURA_LIST):
        em = PERSON_EMOJI.get(name, "👤")
        mark = "✅ " if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{mark}{em}{name}", callback_data=f"set_proc_idx:{i}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def schedule_delete(bot, chat_id, msg_id, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try: await bot.delete_message(chat_id, msg_id)
    except: pass

async def auto_delete_later(bot, chat_id, msg_id, seconds=30):
    asyncio.create_task(schedule_delete(bot, chat_id, msg_id, seconds))

def build_pinned_content():
    today = date.today()
    svodki = get_svodki_person(today)
    proc = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    sv_em = PERSON_EMOJI.get(svodki, "👤")
    pr_em = PERSON_EMOJI.get(proc, "👤")
    div = weekday_divider(today)
    day_name, icon, _, _ = WEEKDAY_STYLE[today.weekday()]
    date_str = today.strftime("%d.%m.%Y")
    mood_lbl = get_mood_label()
    filled = round((proc_day / 2) * 8)
    bar = "▓" * filled + "░" * (8 - filled)
    return f"📌 *АКТУАЛЬНЫЙ НАРЯД*\n{icon} {day_name}, {date_str}\n_настроение бота: {mood_lbl}_\n{div}\n\n🤷‍♀️ *Дежурный №{SVODKI_LIST.index(svodki)+1} по сводкам:*\n  {sv_em} *{svodki}*\n\n⚡️ *Дежурный №{PROCEDURA_LIST.index(proc)+1} по процедурке:*\n  {pr_em} *{proc}*\n  `[{bar}]` день {proc_day}/2\n\n{div}\n_обновляется автоматически каждый день_"

async def update_pinned_message(bot):
    data = load_data()
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    content = build_pinned_content()
    pinned_msg_id = data.get("pinned_msg_id")
    if pinned_msg_id:
        try:
            await bot.edit_message_text(content, chat_id=chat_id, message_id=pinned_msg_id, parse_mode="Markdown")
            return
        except Exception as e:
            logger.warning(f"не удалось отредактировать закреплённое: {e}")
    try:
        sent = await bot.send_message(chat_id, content, parse_mode="Markdown")
        try: await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except: pass
        data["pinned_msg_id"] = sent.message_id
        save_data(data)
    except Exception as e:
        logger.error(f"ошибка создания закреплённого: {e}")

class AdminStates(StatesGroup):
    waiting_chat_id = State()
    waiting_register = State()

async def _send_personal(bot, person, duty_type):
    data = load_data()
    uid = data.get("personal_ids", {}).get(person)
    if not uid: return
    tmpl_pool = (PERSONAL_SVODKI + PERSONAL_SVODKI_GAMING) if duty_type == "svodki" else (PERSONAL_PROC + PERSONAL_PROC_GAMING)
    try: await bot.send_message(uid, random.choice(tmpl_pool).format(name=person))
    except Exception as e: logger.warning(f"личное сообщение {person}: {e}")

async def _send_reminder(bot, duty_type, retry=False):
    data = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        logger.warning("CHAT_ID не настроен!")
        return

    today = datetime.now(TIMEZONE).date()
    wd = today.weekday()
    mood_label = get_mood_label()
    easter = random.random() < 0.05 and not retry

    if duty_type == "svodki":
        person = get_svodki_person(today)
        next_d = days_until_svodki(person, today)
        next_n = next_person_svodki(person)
        total = get_total_duties(person)
        position_label = svodki_num_label(person)
        time_label = "18:00"
        proc_day = 1
        headers_pool = SVODKI_HEADERS_BY_DAY[wd] + GAMING_HEADERS_SVODKI
        retry_pool = RETRY_PREFIX + RETRY_PREFIX_GAMING
        header = await get_ai_header(person, duty_type) if not retry else None
        if not header: header = random.choice(retry_pool) if retry else random.choice(headers_pool)
        ending = get_ending()
        prev_key = "last_svodki_msg_id"
    else:
        person = get_procedura_person(today)
        proc_day = get_duty_day_number(today)
        next_d = days_until_procedura(person, today)
        next_n = next_person_proc(person)
        total = get_total_duties(person)
        position_label = proc_num_label(person)
        time_label = "22:00"
        headers_pool = PROC_HEADERS_BY_DAY[wd] + GAMING_HEADERS_PROC
        retry_pool = RETRY_PREFIX + RETRY_PREFIX_GAMING
        header = await get_ai_header(person, duty_type) if not retry else None
        if not header: header = random.choice(retry_pool) if retry else random.choice(headers_pool)
        ending = get_ending()
        prev_key = "last_proc_msg_id"

    if easter:
        msg_text = random.choice(EASTER_EGGS)(person, duty_type)
        prev_id = data.get(prev_key)
        if prev_id:
            try: await bot.delete_message(chat_id, prev_id)
            except: pass
        sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=confirmation_keyboard(duty_type))
        data[prev_key] = sent.message_id
        save_data(data)
        await update_pinned_message(bot)
        await _send_personal(bot, person, duty_type)
        if duty_type == "svodki" and not retry:
            weather_text = await get_weather_advice()
            if weather_text: await bot.send_message(chat_id, f"☁️ *Обстановка на улице:*\n{weather_text}", parse_mode="Markdown")
        return

    date_label = today.strftime("%d.%m.%Y") + " • " + WEEKDAY_STYLE[wd][0]
    theme_key = random.choice(THEME_KEYS)
    img_path = os.path.join(TEMP_DIR, f"card_{duty_type}_{int(time.time())}.jpg")

    try:
        make_reminder_card(
            duty_type=duty_type, person=person, position_label=position_label, time_label=time_label,
            header_text=header, ending_text=ending, date_label=date_label, mood_label=mood_label,
            proc_day=proc_day if duty_type == "proc" else 1, next_person=next_n, next_days=next_d,
            total_duties=total, theme_key=theme_key, output_path=img_path
        )
    except Exception as e:
        logger.error(f"Ошибка генерации карточки: {e}")
        msg_text = f"*{header}*\n\n👤 {person}\n📋 {position_label}\n⏰ {time_label}\n📊 нарядов всего: {total}\n⏭ следующий: {next_n} ({next_d}д)\n\n_{ending}_"
        prev_id = data.get(prev_key)
        if prev_id:
            try: await bot.delete_message(chat_id, prev_id)
            except: pass
        sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=confirmation_keyboard(duty_type))
        data[prev_key] = sent.message_id
        save_data(data)
        await update_pinned_message(bot)
        await _send_personal(bot, person, duty_type)
        return

    caption = f"🔔 Напоминание по {duty_type.upper()} для *{person}*"
    prev_id = data.get(prev_key)
    if prev_id:
        try: await bot.delete_message(chat_id, prev_id)
        except: pass

    try:
        photo = FSInputFile(img_path)
        sent = await bot.send_photo(chat_id, photo=photo, caption=caption, parse_mode="Markdown", reply_markup=confirmation_keyboard(duty_type))
        data[prev_key] = sent.message_id
        save_data(data)
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        msg_text = f"*{header}*\n\n👤 {person}\n📋 {position_label}\n⏰ {time_label}\n\n_{ending}_"
        sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=confirmation_keyboard(duty_type))
        data[prev_key] = sent.message_id
        save_data(data)
    finally:
        try: os.remove(img_path)
        except: pass

    await update_pinned_message(bot)
    await _send_personal(bot, person, duty_type)

async def send_svodki_reminder(bot): await _send_reminder(bot, "svodki")
async def send_procedura_reminder(bot): await _send_reminder(bot, "proc")

async def send_retry_reminder(bot, duty_type):
    data = load_data()
    pending = data.get("pending_retry", {})
    today = date.today().isoformat()
    if pending.get(duty_type) == today:
        await _send_reminder(bot, duty_type, retry=True)
        pending.pop(duty_type, None)
        data["pending_retry"] = pending
        save_data(data)

async def send_monday_briefing(bot):
    data = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try:
        await bot.send_message(chat_id, build_monday_briefing(), parse_mode="Markdown")
        await update_pinned_message(bot)
    except Exception as e: logger.error(f"понедельничная рассылка: {e}")

async def send_sunday_summary(bot):
    data = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try: await bot.send_message(chat_id, build_sunday_summary(), parse_mode="Markdown")
    except Exception as e: logger.error(f"воскресный итог: {e}")

async def daily_pinned_update(bot): await update_pinned_message(bot)

async def cmd_start(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    start_text = (
        "👋 *Йоу! Я твой кибер-надзиратель и ИИ-кореш в одном лице.*\n\n"
        "*Что я умею, нормис:*\n"
        "🧹 *Следить за нарядами:* сводки в 18:00, процедурка в 22:00. Забьешь — задушу напоминалками.\n"
        "🤖 *Выдавать базу (ИИ-чат):* сделай реплай на моё сообщение, и я раскидаю за любой вопрос.\n"
        "🎵 *Музыка:* `/find <название>` (поиск) или `/charts` (топ популярного). Качаю mp3 в отличном качестве.\n"
        "🧠 *Дайджест чата:* напиши `/tldr`, и я сделаю токсичную выжимку вашего флуда.\n"
        "☁️ *Погода:* напиши `/pogoda` (чекаю Одинцово, чтоб ты не откинулся от холода по пути в штаб).\n"
        "🃏 *Делать мемы:* скинь фотку с подписью `/meme`, и я запилю классический демотиватор.\n"
        "🎨 *Рисовать:* `/poll <текст>` (бесплатно) или `/imagen <текст>` (элитно от Google).\n\n"
        "Я всё вижу, всё помню. Не ломай вайб и делай наряды вовремя. Погнали! 👇"
    )
    sent = await message.answer(start_text, parse_mode="Markdown", reply_markup=main_keyboard())
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 60)

async def cmd_today(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(build_duty_message(date.today(), "сегодня"), parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)

async def cmd_tomorrow(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(build_duty_message(date.today() + timedelta(days=1), "завтра"), parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)

async def cmd_week(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(build_week_schedule(), parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)

async def cmd_log(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(build_log_message(), parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 120)

async def cmd_remind_now(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer("🔔 *погнали, отправляю в чат...*", parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
    await _send_reminder(message.bot, "svodki")
    await _send_reminder(message.bot, "proc")

async def cmd_settings(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    data = load_data()
    enabled = "✅ включены" if data.get("reminders_enabled", True) else "❌ выключены"
    ai_enabled = "✅ включены" if data.get("ai_random_replies_enabled", True) else "❌ выключены"
    chat_txt = data.get("chat_id", "не настроен")
    sv_now = get_svodki_person(date.today())
    pr_now = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em = PERSON_EMOJI.get(sv_now, "👤")
    pr_em = PERSON_EMOJI.get(pr_now, "👤")
    mood_lbl = get_mood_label()
    
    settings_text = (
        f"⚙️ *настройки*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 чат: `{chat_txt}`\n"
        f"🔔 напоминания: {enabled}\n"
        f"🤖 случайные ответы ИИ: {ai_enabled}\n"
        f"🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n"
        f"⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n"
        f"🪪 зарегистрировано: *{reg_count}* чел.\n"
        f"🎭 настроение бота: *{mood_lbl}*\n━━━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(settings_text, parse_mode="Markdown", reply_markup=settings_keyboard(data))


async def handle_ai_chat(message: types.Message):
    if not gemini_client: return
    
    if message.text and (message.text.startswith('/') or message.text in ["📋 Наряд сегодня", "📅 Наряд завтра", "📊 Расписание на неделю", "📓 Журнал", "🔔 Напомнить сейчас", "⚙️ Настройки"]):
        return

    if message.text:
        user_name = message.from_user.first_name or "Кто-то"
        CHAT_HISTORY.append(f"{user_name}: {message.text}")

    bot_user = await message.bot.me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    
    data = load_data()
    ai_random_replies = data.get("ai_random_replies_enabled", True)

    if not is_reply_to_bot: 
        # Шанс снижен до 2% и есть проверка настроек
        if ai_random_replies and message.text and random.random() < 0.02:
            await message.bot.send_chat_action(message.chat.id, "typing")
            prompt = (
                f"Пользователь {message.from_user.first_name} написал в чат: «{message.text}». "
                "Жестко, токсично и смешно затролль его за это сообщение. "
                "Используй зумерский сленг (кринж, скуф, база, дед инсайд) и органичный мат. "
                "Отвечай коротко, 1-2 предложения, как токсичный кореш. Без приветствий."
            )
            try:
                roast_resp = await gemini_client.aio.models.generate_content(
                    model='gemini-3.5-flash',
                    contents=prompt
                )
                if roast_resp.text:
                    await message.reply(roast_resp.text.strip())
            except Exception as e:
                logger.error(f"Ошибка хейтера: {e}")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    user_id = message.from_user.id

    if user_id not in USER_CHATS:
        config = genai_types.GenerateContentConfig(
            system_instruction=AI_SYSTEM_PROMPT,
            tools=[{"google_search": {}}] 
        )
        USER_CHATS[user_id] = gemini_client.aio.chats.create(
            model='gemini-3.5-flash',
            config=config
        )

    chat_session = USER_CHATS[user_id]
    today = date.today()
    sv_today = get_svodki_person(today)
    pr_today = get_procedura_person(today)

    hidden_context = f"[Системная справка: сегодня дежурный по сводкам — {sv_today}, дежурный по процедурке — {pr_today}.] "
    full_message = hidden_context + message.text

    try:
        response = await chat_session.send_message(full_message)
        await message.reply(response.text)
    except Exception as e:
        logger.error(f"Ошибка Gemini в чате: {e}")
        USER_CHATS.pop(user_id, None)
        await message.reply(f"❌ Ошибка Google API (Чат):\n`{e}`")


# ══════════════════════════════════════════════
#  МУЗЫКА (ТЕПЕРЬ С ПАГИНАЦИЕЙ И ЧАРТАМИ)
# ══════════════════════════════════════════════

async def cmd_music_find(message: types.Message):
    query = message.text.replace("/find", "").strip()
    if not query:
        await message.answer("🎧 Что ищем? Пример: `/find Rammstein`", parse_mode="Markdown")
        return
    await show_music_page(message, "search", query, 0)

async def cmd_charts(message: types.Message):
    await show_music_page(message, "chart", "", 0)

async def show_music_page(message_or_callback, mode, query, page):
    limit = 5
    offset = page * limit
    
    if mode == "chart":
        tracks = await music_engine.get_charts(limit=limit, offset=offset)
        header_text = f"🔥 *Чарт SoundCloud* (стр. {page+1})"
    else:
        tracks = await music_engine.search_sc(query, limit=limit, offset=offset)
        header_text = f"🎧 *Поиск:* «{query}» (стр. {page+1})"

    if not tracks:
        if isinstance(message_or_callback, types.CallbackQuery):
            await message_or_callback.answer("❌ Больше треков нет.", show_alert=True)
        else:
            await message_or_callback.answer("❌ Больше треков не найдено.")
        return

    buttons = []
    for t in tracks:
        btn_text = f"🎵 {t['artist']} — {t['title']} [{t['duration']}]"
        cb_data = f"dl_sc:{t['id']}"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])
    
    nav_buttons = []
    # Обрезаем query для callback_data (ограничение API Telegram 64 байта)
    safe_query = query[:25] if query else "none"

    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mus_pg:{mode}:{safe_query}:{page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"стр. {page+1}", callback_data="ignore"))
    nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"mus_pg:{mode}:{safe_query}:{page+1}"))
    buttons.append(nav_buttons)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    if isinstance(message_or_callback, types.CallbackQuery):
        await message_or_callback.message.edit_text(header_text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message_or_callback.answer(header_text, reply_markup=keyboard, parse_mode="Markdown")

async def callback_music_page(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 4: return
    _, mode, query, page = parts
    query = "" if query == "none" else query
    await show_music_page(callback, mode, query, int(page))

async def callback_download_music(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    await callback.answer("⬇️ Вшиваю обложку и качаю трек...", show_alert=False)
    status_msg = await callback.message.answer("⏳ Собираю битрейт и теги...")

    track = await music_engine.get_track_details(track_id)
    if not track or not track['stream_url']:
        await status_msg.edit_text("❌ Ошибка: не удалось получить поток трека.")
        return

    audio_bytes, cover_bytes = await asyncio.gather(
        music_engine.download_file(track['stream_url']),
        music_engine.download_file(track['artwork_url'])
    )

    if not audio_bytes:
        await status_msg.edit_text("❌ Ошибка при скачивании файла.")
        return

    audio_bytes = add_id3_tags(audio_bytes, track['title'], track['artist'], cover_bytes)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❤️ В избранное", callback_data=f"fav_sc:{track_id}"),
            InlineKeyboardButton(text="🧠 Похожее", callback_data=f"rec_sc:{track_id}")
        ]
    ])

    try:
        # Улучшенная карточка трека
        caption = (
            f"🎧 *{track['artist']} — {track['title']}*\n\n"
            f"🎼 *Жанр:* {track.get('genre', 'Неизвестен')}\n"
            f"⚡️ *Источник:* SoundCloud"
        )
        
        audio_file = BufferedInputFile(audio_bytes, filename=f"{track['title']}.mp3")
        await callback.message.answer_audio(
            audio=audio_file,
            performer=track['artist'],
            title=track['title'],
            caption=caption,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Ошибка отправки аудио: {e}")
        await status_msg.edit_text(f"❌ Нейроны не справились: {e}")

async def callback_music_fav(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    track = await music_engine.get_track_details(track_id)
    if not track:
        await callback.answer("❌ Трек сгорел в серверах SC", show_alert=True)
        return
        
    success = save_music_fav(callback.from_user.id, {
        "id": track_id,
        "title": track['title'],
        "artist": track['artist']
    })
    if success:
        await callback.answer("❤️ Сохранено! Пиши /my_music чтобы послушать.", show_alert=True)
    else:
        await callback.answer("🤡 Ты уже добавил этот трек, нормис.", show_alert=True)

async def callback_music_recs(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    await callback.answer("🧠 Нейросеть анализирует вайб...", show_alert=False)
    
    track = await music_engine.get_track_details(track_id)
    if not track: return
        
    prompt = (
        f"Пользователь скачал трек: {track['artist']} — {track['title']} (Жанр: {track['genre']}). "
        "Посоветуй 5 реально годных, похожих по стилю и вайбу треков. "
        "Отвечай коротко, в стиле токсичного зумерского бота (используй сленг типа база, имба, нормис). "
        "Выдай просто нумерованный список: Исполнитель - Трек."
    )
    try:
        response = await gemini_client.aio.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt
        )
        await callback.message.reply(f"🧠 *ИИ-Рекомендации по вайбу:*\n\n{response.text}", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка реков: {e}")
        await callback.message.reply("❌ Процессоры перегрелись, рекомендации отменяются.")

async def cmd_my_music(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    favs = get_music_favs(message.from_user.id)
    if not favs:
        sent = await message.answer("💀 Твой плейлист пуст как твоя личная жизнь. Нажми ❤️ под любым скачанным треком.")
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 15)
        return
    lines = ["🎧 *Твоя база (Избранное):*"]
    for i, f in enumerate(favs, 1):
        lines.append(f"{i}. *{f['artist']}* — {f['title']}")
    await message.answer("\n".join(lines), parse_mode="Markdown")
 
async def cmd_meme(message: types.Message):
    if not gemini_client: return
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    photo = None
    if message.photo: photo = message.photo[-1]
    elif message.reply_to_message and message.reply_to_message.photo: photo = message.reply_to_message.photo[-1]

    if not photo:
        sent = await message.answer("🖼 Отправь фотку с подписью `/meme` или сделай реплай `/meme` на любую фотку в чате.", parse_mode="Markdown")
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
        return

    status_msg = await message.reply("⬛️ Делаю из этого демотиватор...")
    try:
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        img = Image.open(downloaded_file).convert("RGB")
        target_width = 800
        if img.width != target_width:
            ratio = target_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((target_width, new_height), Image.Resampling.LANCZOS)
        
        prompt = (
            "Ты — создатель мемов. Посмотри на эту картинку и придумай для неё подпись "
            "в стиле классического 'демотиватора'. Тематика: айтишники, геймеры (CS2, Dota 2, Genshin), наряды по офису. "
            "Выдай СТРОГО две строки текста, разделенные знаком |. "
            "Первая строка — короткий заголовок (1-3 слова). "
            "Вторая строка — смешное или философское пояснение. "
            "Не пиши ничего кроме этих двух строк, никаких лишних символов."
        )
        response = await gemini_client.aio.models.generate_content(
            model='gemini-3.5-flash',
            contents=[img, prompt]
        )
        meme_text = response.text.replace('"', '').replace('\n', '').strip()
        if "|" in meme_text: top_text, bottom_text = meme_text.split("|", 1)
        else: top_text, bottom_text = meme_text, ""
            
        top_text = top_text.strip().upper()
        bottom_text = bottom_text.strip()
        font_title_path = os.path.join(BASE_DIR, "fonts", "DejaVuSans-Bold.ttf")
        font_sub_path = os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf")
        
        try:
            font_title = ImageFont.truetype(font_title_path, 60)
            font_sub = ImageFont.truetype(font_sub_path, 30)
        except IOError:
            font_title = ImageFont.load_default()
            font_sub = ImageFont.load_default()

        title_lines = textwrap.wrap(top_text, width=25)
        sub_lines = textwrap.wrap(bottom_text, width=55)
        text_area_height = 40
        for line in title_lines:
            bbox = font_title.getbbox(line)
            text_area_height += (bbox[3] - bbox[1]) + 10
        text_area_height += 20
        for line in sub_lines:
            bbox = font_sub.getbbox(line)
            text_area_height += (bbox[3] - bbox[1]) + 10
        text_area_height += 50

        border_x, border_top = 70, 70
        bg_width, bg_height = img.width + border_x * 2, img.height + border_top + text_area_height
        background = Image.new('RGB', (bg_width, bg_height), color='black')
        background.paste(img, (border_x, border_top))
        
        draw = ImageDraw.Draw(background)
        draw.rectangle([border_x - 4, border_top - 4, border_x + img.width + 3, border_top + img.height + 3], outline='white', width=3)

        current_y = border_top + img.height + 40
        for line in title_lines:
            draw.text((bg_width / 2, current_y), line, font=font_title, fill="white", anchor="ma")
            bbox = font_title.getbbox(line)
            current_y += (bbox[3] - bbox[1]) + 10
        current_y += 20
        for line in sub_lines:
            draw.text((bg_width / 2, current_y), line, font=font_sub, fill="white", anchor="ma")
            bbox = font_sub.getbbox(line)
            current_y += (bbox[3] - bbox[1]) + 10

        out_bytes = io.BytesIO()
        background.save(out_bytes, format="JPEG", quality=95)
        out_bytes.seek(0)
        
        photo_out = BufferedInputFile(out_bytes.read(), filename="demotivator.jpg")
        await message.answer_photo(photo_out)
    except Exception as e:
        logger.error(f"Ошибка создания демотиватора: {e}")
        await message.reply(f"❌ Нейроны перегрелись: {e}")
    finally:
        try: await status_msg.delete()
        except: pass

async def cmd_tldr(message: types.Message):
    if not gemini_client: return
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    if len(CHAT_HISTORY) < 10:
        sent = await message.answer("🥱 Чат слишком мертвый, даже обсуждать нечего. Нафлудите еще хоть немного.")
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
        return

    status_msg = await message.answer("🧠 Читаю ваш бред за последнее время...")
    chat_log = "\n".join(CHAT_HISTORY)
    prompt = (
        "Ты — токсичный зумер-бот. Вот лог последних сообщений из чата:\n\n"
        f"{chat_log}\n\n"
        "Сделай краткую выжимку (TL;DR) этого бреда. Что обсуждали, кто какую хуйню сморозил, кто кринжанул. "
        "Пиши саркастично, используй разговорный мат и зумерский сленг (скуф, база, имба, нормис). "
        "Максимум 3-4 предложения. Высмей участников чата."
    )
    try:
        response = await gemini_client.aio.models.generate_content(model='gemini-3.5-flash', contents=prompt)
        await message.answer(f"📜 *Саммари чата:*\n\n{response.text.strip()}", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка TL;DR: {e}")
        await message.answer(f"❌ Нейроны перегрелись читать этот кринж:\n`{e}`")
    finally:
        try: await status_msg.delete()
        except: pass

async def handle_photo(message: types.Message):
    if not gemini_client: return

    bot_user = await message.bot.me()
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    if not (is_reply or message.caption or message.chat.type == 'private'): return

    await message.bot.send_chat_action(message.chat.id, "typing")
    status_msg = await message.reply("👀 Сканирую пиксели...")

    try:
        photo = message.photo[-1]
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        image = Image.open(downloaded_file)

        user_text = message.caption if message.caption else "Что ты видишь на этой картинке? Опиши коротко, в своем саркастичном и гиковском стиле."
        system_prompt = (
            "Ты — дерзкий, ироничный ИИ-надзиратель. "
            "Проанализируй картинку и ответь пользователю. "
            "Сохраняй саркастичный, геймерский тон. Если скинули фотку убранной комнаты — оцени качество уборки как строгий прапорщик."
        )
        response = await gemini_client.aio.models.generate_content(
            model='gemini-3.5-flash',
            contents=[image, user_text],
            config=genai_types.GenerateContentConfig(system_instruction=system_prompt)
        )
        await message.reply(response.text)
    except Exception as e:
        logger.error(f"Ошибка анализа фото Gemini: {e}")
        await message.reply("❌ Мои оптические сенсоры сбоят. Либо картинка слишком шакальная, либо сервера гугла лежат.")
    finally:
        try: await status_msg.delete()
        except: pass

async def cmd_getchatid(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    sent = await message.answer(f"📍 *id этого чата:*\n`{message.chat.id}`\n\nскопируй и вставь в настройки", parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 30)
    
async def cmd_pollinations(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    prompt = message.text.replace("/poll", "").strip()
    if not prompt:
        sent = await message.answer("🎨 Напиши, что нарисовать.\nПример: `/poll cyberpunk city in neon lights`", parse_mode="Markdown")
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
        return
    status_msg = await message.answer("🎨 Рисую через Pollinations (бесплатно, без лимитов)...")
    try:
        safe_prompt = urllib.parse.quote(prompt)
        seed = random.randint(1, 9999999)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&nologo=true&seed={seed}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    photo = BufferedInputFile(image_bytes, filename="pollinations_art.jpg")
                    await message.answer_photo(photo, caption=f"🎨 **Pollinations:** {prompt}", parse_mode="Markdown")
                else:
                    await message.answer(f"❌ Сервер Pollinations перегружен (ошибка {resp.status})")
    except Exception as e:
        logger.error(f"Ошибка Pollinations: {e}")
        await message.answer(f"❌ Что-то пошло не так: `{e}`")
    finally:
        try: await status_msg.delete()
        except: pass

async def cmd_imagen(message: types.Message):
    if not gemini_client: return
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    prompt = message.text.replace("/imagen", "").strip()
    if not prompt:
        sent = await message.answer("✨ Напиши, что нарисовать мощной нейросетью.\nПример: `/imagen фотореалистичный космонавт на Марсе`", parse_mode="Markdown")
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
        return

    status_msg = await message.answer("✨ Заряжаю Google Imagen 4 Ultra...")
    try:
        result = await gemini_client.aio.models.generate_images(
            model='imagen-4.0-ultra', 
            prompt=prompt,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                output_mime_type="image/jpeg",
                aspect_ratio="1:1"
            )
        )
        generated_image = result.generated_images[0]
        image_bytes = generated_image.image.image_bytes
        photo = BufferedInputFile(image_bytes, filename="imagen_art.jpg")
        await message.answer_photo(photo, caption=f"✨ **Imagen 4:** {prompt}", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка генерации картинки Imagen: {e}")
        await message.answer(f"❌ Ошибка Google API:\n`{e}`")
    finally:
        try: await status_msg.delete()
        except: pass

async def callback_toggle_reminders(callback: types.CallbackQuery):
    data = load_data()
    data["reminders_enabled"] = not data.get("reminders_enabled", True)
    save_data(data)
    status = "✅ включены" if data["reminders_enabled"] else "❌ выключены"
    await callback.answer(f"напоминания {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(data))

async def callback_toggle_ai_replies(callback: types.CallbackQuery):
    data = load_data()
    data["ai_random_replies_enabled"] = not data.get("ai_random_replies_enabled", True)
    save_data(data)
    status = "✅ ВКЛЮЧЕНЫ" if data["ai_random_replies_enabled"] else "❌ ВЫКЛЮЧЕНЫ"
    await callback.answer(f"Случайные ответы ИИ {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(data))

async def callback_set_chat(callback: types.CallbackQuery, state: FSMContext):
    sent = await callback.message.answer("📍 *напиши id чата* куда кидать напоминания:\nузнать: напиши `/chatid` в нужном чате", parse_mode="Markdown")
    await auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await state.set_state(AdminStates.waiting_chat_id)
    await callback.answer()

async def process_chat_id(message: types.Message, state: FSMContext):
    chat_id = message.text.strip()
    data = load_data()
    data["chat_id"] = chat_id
    save_data(data)
    await state.clear()
    sent = await message.answer(f"✅ *чат установлен!*\nid: `{chat_id}`", parse_mode="Markdown", reply_markup=main_keyboard())
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 30)

async def callback_update_pinned(callback: types.CallbackQuery):
    await callback.answer("📌 обновляю закреплённое...", show_alert=False)
    await update_pinned_message(callback.bot)
    await callback.answer("✅ закреплённое обновлено!", show_alert=True)

async def callback_register_personal(callback: types.CallbackQuery, state: FSMContext):
    names = " / ".join(SVODKI_LIST)
    sent = await callback.message.answer(f"🪪 *регистрация личных уведомлений*\n\nнапиши своё имя *точно как в списке*:\n{names}\n\nбуду писать тебе лично в день дежурства 👀", parse_mode="Markdown")
    await auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await state.set_state(AdminStates.waiting_register)
    await callback.answer()

async def process_register(message: types.Message, state: FSMContext):
    name = message.text.strip()
    all_names = list(set(SVODKI_LIST + PROCEDURA_LIST))
    if name not in all_names:
        sent = await message.answer(f"❌ *{name}* не найдено в списках\nпроверь написание и попробуй снова", parse_mode="Markdown")
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 20)
        return
    data = load_data()
    data.setdefault("personal_ids", {})[name] = message.from_user.id
    save_data(data)
    em = PERSON_EMOJI.get(name, "👤")
    await state.clear()
    sent = await message.answer(f"✅ {em} *{name}* зарегистрирован!\nбуду писать тебе лично в день наряда 🫡", parse_mode="Markdown", reply_markup=main_keyboard())
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 30)

async def callback_pick_svodki(callback: types.CallbackQuery):
    data = load_data()
    today = date.today()
    days_passed = (today - date.fromisoformat(data["start_date"])).days
    actual_idx = (data["svodki_start_index"] + days_passed) % len(SVODKI_LIST)
    await callback.message.edit_text(f"🤷‍♀️ *кто сегодня по сводкам?*\nсейчас: {PERSON_EMOJI.get(SVODKI_LIST[actual_idx],'👤')} *{SVODKI_LIST[actual_idx]}*\n\nнажми на нужное имя:", parse_mode="Markdown", reply_markup=svodki_pick_keyboard(actual_idx))
    await callback.answer()

async def callback_pick_procedura(callback: types.CallbackQuery):
    data = load_data()
    today = date.today()
    days_passed = (today - date.fromisoformat(data["start_date"])).days
    actual_idx = (data["procedura_start_index"] + days_passed // 2) % len(PROCEDURA_LIST)
    await callback.message.edit_text(f"⚡️ *кто сегодня по процедурке?*\nсейчас: {PERSON_EMOJI.get(PROCEDURA_LIST[actual_idx],'👤')} *{PROCEDURA_LIST[actual_idx]}*\n\nнажми на нужное имя:", parse_mode="Markdown", reply_markup=procedura_pick_keyboard(actual_idx))
    await callback.answer()

async def callback_set_svodki_idx(callback: types.CallbackQuery):
    chosen_idx = int(callback.data.split(":")[1])
    chosen_name = SVODKI_LIST[chosen_idx]
    data = load_data()
    days_passed = (date.today() - date.fromisoformat(data["start_date"])).days
    data["svodki_start_index"] = (chosen_idx - days_passed) % len(SVODKI_LIST)
    save_data(data)
    await callback.answer(f"✅ сводки сегодня: {PERSON_EMOJI.get(chosen_name,'👤')} {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=svodki_pick_keyboard(chosen_idx))
    await update_pinned_message(callback.bot)

async def callback_set_proc_idx(callback: types.CallbackQuery):
    chosen_idx = int(callback.data.split(":")[1])
    chosen_name = PROCEDURA_LIST[chosen_idx]
    data = load_data()
    days_passed = (date.today() - date.fromisoformat(data["start_date"])).days
    data["procedura_start_index"] = (chosen_idx - days_passed // 2) % len(PROCEDURA_LIST)
    save_data(data)
    await callback.answer(f"✅ процедурка сегодня: {PERSON_EMOJI.get(chosen_name,'👤')} {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=procedura_pick_keyboard(chosen_idx))
    await update_pinned_message(callback.bot)

async def callback_show_svodki_list(callback: types.CallbackQuery):
    today = date.today()
    current = get_svodki_person(today)
    lines = [f"*👥 наряд по сводкам:*\nсейчас: {PERSON_EMOJI.get(current,'👤')} *{current}*\n"]
    for i, name in enumerate(SVODKI_LIST, 1):
        em = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {em} {name}{mark}")
    sent = await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await callback.answer()

async def callback_show_procedura_list(callback: types.CallbackQuery):
    today = date.today()
    current = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    lines = [f"*👥 наряд по процедурке:*\nсейчас: {PERSON_EMOJI.get(current,'👤')} *{current}* (день {proc_day}/2)\n"]
    for i, name in enumerate(PROCEDURA_LIST, 1):
        em = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {em} {name} *(2 дня)*{mark}")
    sent = await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await callback.answer()

async def callback_back_settings(callback: types.CallbackQuery):
    data = load_data()
    enabled = "✅ включены" if data.get("reminders_enabled", True) else "❌ выключены"
    ai_enabled = "✅ включены" if data.get("ai_random_replies_enabled", True) else "❌ выключены"
    chat_txt = data.get("chat_id", "не настроен")
    sv_now = get_svodki_person(date.today())
    pr_now = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em = PERSON_EMOJI.get(sv_now, "👤")
    pr_em = PERSON_EMOJI.get(pr_now, "👤")
    mood_lbl = get_mood_label()
    settings_text = (
        f"⚙️ *настройки*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 чат: `{chat_txt}`\n"
        f"🔔 напоминания: {enabled}\n"
        f"🤖 случайные ответы ИИ: {ai_enabled}\n"
        f"🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n"
        f"⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n"
        f"🪪 зарегистрировано: *{reg_count}* чел.\n"
        f"🎭 настроение бота: *{mood_lbl}*\n━━━━━━━━━━━━━━━━━━━━"
    )
    await callback.message.edit_text(settings_text, parse_mode="Markdown", reply_markup=settings_keyboard(data))
    await callback.answer()

async def callback_back_main(callback: types.CallbackQuery):
    await callback.message.answer("🏠 главное меню", reply_markup=main_keyboard())
    await callback.answer()

async def callback_done(callback: types.CallbackQuery):
    duty_type = callback.data.split(":")[1]
    today = date.today()
    data = load_data()
    log = data.setdefault("log", {})
    entry = log.setdefault(today.isoformat(), {})
    person = get_svodki_person(today) if duty_type == "svodki" else get_procedura_person(today)
    field = "svodki" if duty_type == "svodki" else "proc"
    entry[field] = person
    data["log"] = log
    data.get("pending_retry", {}).pop(duty_type, None)
    save_data(data)
    increment_count(person, field)
    reply_pool = DONE_REPLIES + DONE_REPLIES_GAMING
    await callback.answer(random.choice(reply_pool), show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)
    await update_pinned_message(callback.bot)

async def callback_retry(callback: types.CallbackQuery):
    duty_type = callback.data.split(":")[1]
    data = load_data()
    data.setdefault("pending_retry", {})[duty_type] = date.today().isoformat()
    save_data(data)
    await callback.answer("⏰ окей, напомню через 15 минут", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)

def _startup_selftest():
    test_path = os.path.join(TEMP_DIR, "_selftest.jpg")
    try:
        make_reminder_card(
            duty_type="svodki", person="Тест", position_label="самопроверка при старте", time_label="00:00",
            header_text="проверка генератора картинок", ending_text="всё ок",
            date_label=datetime.now(TIMEZONE).strftime("%d.%m.%Y"), mood_label="проверка",
            theme_key=random.choice(THEME_KEYS), output_path=test_path,
        )
        size = os.path.getsize(test_path)
        logger.info(f"✅ САМОПРОВЕРКА КАРТИНОК ПРОЙДЕНА: {test_path} ({size} байт)")
    except Exception:
        logger.error("❌ САМОПРОВЕРКА КАРТИНОК НЕ ПРОЙДЕНА:\n" + traceback.format_exc())
    finally:
        try: os.remove(test_path)
        except Exception: pass

async def main():
    logger.info("🟡 инициализация бота...")
    _startup_selftest()

    bot = Bot(token=TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # 1. Регистрация всех команд
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_getchatid, Command("chatid"))
    dp.message.register(cmd_pollinations, Command("poll"))
    dp.message.register(cmd_imagen, Command("imagen"))
    dp.message.register(cmd_meme, Command("meme"))
    dp.message.register(cmd_tldr, Command("tldr"))
    dp.message.register(cmd_music_find, Command("find"))
    dp.message.register(cmd_charts, Command("charts")) # Новая команда для чартов
    dp.message.register(cmd_my_music, Command("my_music"))
    dp.message.register(cmd_weather, Command("pogoda"))
    
    # 2. Регистрация кнопок меню
    dp.message.register(cmd_today, F.text == "📋 Наряд сегодня")
    dp.message.register(cmd_tomorrow, F.text == "📅 Наряд завтра")
    dp.message.register(cmd_week, F.text == "📊 Расписание на неделю")
    dp.message.register(cmd_log, F.text == "📓 Журнал")
    dp.message.register(cmd_remind_now, F.text == "🔔 Напомнить сейчас")
    dp.message.register(cmd_settings, F.text == "⚙️ Настройки")

    # 3. FSM
    dp.message.register(process_chat_id, AdminStates.waiting_chat_id)
    dp.message.register(process_register, AdminStates.waiting_register)

    # 4. Все inline callback-кнопки (ПАГИНАЦИЯ ТЕПЕРЬ ТУТ)
    dp.callback_query.register(callback_done, F.data.startswith("done:"))
    dp.callback_query.register(callback_retry, F.data.startswith("retry:"))
    dp.callback_query.register(callback_toggle_reminders, F.data == "toggle_reminders")
    dp.callback_query.register(callback_toggle_ai_replies, F.data == "toggle_ai_replies") # Новый тумблер
    dp.callback_query.register(callback_set_chat, F.data == "set_chat")
    dp.callback_query.register(callback_register_personal, F.data == "register_personal")
    dp.callback_query.register(callback_update_pinned, F.data == "update_pinned")
    dp.callback_query.register(callback_pick_svodki, F.data == "pick_svodki")
    dp.callback_query.register(callback_pick_procedura, F.data == "pick_procedura")
    dp.callback_query.register(callback_set_svodki_idx, F.data.startswith("set_svodki_idx:"))
    dp.callback_query.register(callback_set_proc_idx, F.data.startswith("set_proc_idx:"))
    dp.callback_query.register(callback_show_svodki_list, F.data == "show_svodki_list")
    dp.callback_query.register(callback_show_procedura_list, F.data == "show_procedura_list")
    dp.callback_query.register(callback_back_settings, F.data == "back_settings")
    dp.callback_query.register(callback_back_main, F.data == "back_main")
    dp.callback_query.register(callback_download_music, F.data.startswith("dl_sc:"))
    dp.callback_query.register(callback_music_fav, F.data.startswith("fav_sc:"))
    dp.callback_query.register(callback_music_recs, F.data.startswith("rec_sc:"))
    dp.callback_query.register(callback_music_page, F.data.startswith("mus_pg:")) # Починенная пагинация

    # 5. Обработка фото
    dp.message.register(handle_photo, F.photo)

    # 6. Текстовый хендлер (болталка ИИ)
    dp.message.register(handle_ai_chat, F.text)

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_svodki_reminder, CronTrigger(hour=18, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_procedura_reminder, CronTrigger(hour=22, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_monday_briefing, CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_sunday_summary, CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(daily_pinned_update, CronTrigger(hour=0, minute=1, timezone=TIMEZONE), args=[bot])
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
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        tb = traceback.format_exc()
        logger.critical("🔴 БОТ УПАЛ С НЕОБРАБОТАННОЙ ОШИБКОЙ:\n" + tb)
        print("🔴 БОТ УПАЛ С НЕОБРАБОТАННОЙ ОШИБКОЙ:\n" + tb, file=sys.stderr, flush=True)
        sys.exit(1)
