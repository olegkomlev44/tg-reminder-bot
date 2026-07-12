import asyncio
import logging
import json
import os
import re
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
from aiogram.types import BufferedInputFile, InlineQueryResultArticle, InputTextMessageContent, InlineQueryResultCachedAudio
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

# Хранилище для Дайджеста и ограничитель потоков скачивания
CHAT_HISTORY = deque(maxlen=150)
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(6)

from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
# --- НОВЫЕ ИМПОРТЫ ---
from db import (
    init_db, get_cached_file_id, save_cached_file_id,
    save_music_fav, get_music_favs, log_track_history, get_user_history
)
from web_server import start_web_server
from aiogram.types import WebAppInfo
# ----------------------

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
            model='gemini-2.5-flash',
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
            model='gemini-2.5-flash', 
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
            if "ai_random_replies_enabled" not in data: data["ai_random_replies_enabled"] = True
            if "track_cache" not in data: data["track_cache"] = {}
            if "user_history" not in data: data["user_history"] = {} # НОВОЕ: История
            return data
            
    data = {
        "start_date": date.today().isoformat(), "svodki_start_index": 0, "procedura_start_index": 0,
        "chat_id": CHAT_ID, "reminders_enabled": True, "ai_random_replies_enabled": True,
        "personal_ids": {}, "log": {}, "pending_retry": {}, "duty_counts": {}, "pinned_msg_id": None,
        "daily_mood": {}, "track_cache": {}, "user_history": {}
    }
    save_data(data)
    return data

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --- ФИЛЬТР ЯВНОГО КОНТЕНТА (Визуал 9) ---
def get_explicit_tag(title):
    bad_words = ['fuck', 'bitch', 'shit', 'nigga', 'hoe', 'explicit', '18+']
    return " 🔞[E]" if any(w in title.lower() for w in bad_words) else ""

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

# ── ПЛЕЙЛИСТЫ ──────────────────────────────────────────────────────────────
def get_playlists(user_id) -> dict:
    """{'playlist_name': [track_info, ...]}"""
    return load_data().get("playlists", {}).get(str(user_id), {})

def save_playlist_track(user_id, playlist_name: str, track_info: dict) -> bool:
    data = load_data()
    pl = data.setdefault("playlists", {}).setdefault(str(user_id), {})
    tracks = pl.setdefault(playlist_name, [])
    if not any(t["id"] == track_info["id"] for t in tracks):
        tracks.append(track_info)
        save_data(data)
        return True
    return False

def delete_playlist(user_id, playlist_name: str):
    data = load_data()
    data.get("playlists", {}).get(str(user_id), {}).pop(playlist_name, None)
    save_data(data)

def remove_track_from_playlist(user_id, playlist_name: str, track_id: str):
    data = load_data()
    pl = data.get("playlists", {}).get(str(user_id), {})
    if playlist_name in pl:
        pl[playlist_name] = [t for t in pl[playlist_name] if str(t["id"]) != str(track_id)]
        save_data(data)

# ── ОЧЕРЕДЬ ВОСПРОИЗВЕДЕНИЯ ────────────────────────────────────────────────
def queue_push(user_id, track_info: dict):
    data = load_data()
    q = data.setdefault("queues", {}).setdefault(str(user_id), [])
    if not any(t["id"] == track_info["id"] for t in q):
        q.append(track_info)
        save_data(data)

def queue_pop(user_id) -> dict | None:
    data = load_data()
    q = data.setdefault("queues", {}).setdefault(str(user_id), [])
    if not q:
        return None
    track = q.pop(0)
    save_data(data)
    return track

def queue_list(user_id) -> list:
    return load_data().get("queues", {}).get(str(user_id), [])

def queue_clear(user_id):
    data = load_data()
    data.setdefault("queues", {})[str(user_id)] = []
    save_data(data)

# ── ПОДПИСКИ НА АРТИСТОВ ───────────────────────────────────────────────────
def get_artist_subs(user_id) -> list:
    return load_data().get("artist_subs", {}).get(str(user_id), [])

def add_artist_sub(user_id, artist: str) -> bool:
    data = load_data()
    subs = data.setdefault("artist_subs", {}).setdefault(str(user_id), [])
    if artist not in subs:
        subs.append(artist)
        save_data(data)
        return True
    return False

def remove_artist_sub(user_id, artist: str):
    data = load_data()
    subs = data.setdefault("artist_subs", {}).setdefault(str(user_id), [])
    if artist in subs:
        subs.remove(artist)
        save_data(data)

# ── DJ-ГОЛОСОВАНИЕ (in-memory, не нужна персистентность) ──────────────────
DJ_SESSIONS: dict = {}  # chat_id → {tracks, votes, msg_id, initiator_id}

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

async def animate_loading(message: types.Message):
    frames = [
        "⏳ `[▓░░░░░░░░░] 10% — Поиск потока...`",
        "⏳ `[▓▓▓░░░░░░░] 30% — Извлечение битрейта...`",
        "⏳ `[▓▓▓▓▓▓░░░░] 60% — Скачивание...`",
        "⏳ `[▓▓▓▓▓▓▓▓░░] 80% — Вшиваем метаданные...`",
        "⏳ `[▓▓▓▓▓▓▓▓▓▓] 99% — Подготовка к отправке...`"
    ]
    for frame in frames:
        try:
            await message.edit_text(frame, parse_mode="Markdown")
            await asyncio.sleep(1.2)
        except: pass
            
async def animate_wave(message: types.Message):
    """Кастомная анимация Загрузки Волны"""
    frames = [
        "🎧 `[ ılı.lıllılı.ıllı. ] Настраиваемся на ваш вайб...`",
        "🎧 `[ .ıllı.lıllılı.ılı ] Анализ Избранного...`",
        "🎧 `[ ılı.lıllılı.ıllı. ] Подключение к нейронке...`",
        "🎧 `[ .ıllı.lıllılı.ılı ] Синтез бесконечного потока...`"
    ]
    for frame in frames:
        try:
            await message.edit_text(frame, parse_mode="Markdown")
            await asyncio.sleep(1.0)
        except: pass

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

class MusicStates(StatesGroup):
    waiting_playlist_name = State()

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
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("track_"):
        track_id = args[1].replace("track_", "")
        await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
        await send_track_to_user(message, track_id)
        return
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
                    model='gemini-2.5-flash',
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
            model='gemini-2.5-flash',
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


# ═════════════════════════════════════════════
#  МУЗЫКА
# ═════════════════════════════════════════════

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
        src_names = list(set(t.get("source", "SC") for t in tracks)) if tracks else ["SoundCloud"]
        header_text = f"🔥 *Чарт* ({', '.join(src_names)}) — стр. {page+1}"
    else:
        tracks = await music_engine.search_multi(query, limit=limit, offset=offset)
        src_names = list(set(t.get("source", "SC") for t in tracks)) if tracks else ["SoundCloud"]
        header_text = f"🎧 *Поиск:* «{query}» ({', '.join(src_names)}) — стр. {page+1}"

    if not tracks:
        if isinstance(message_or_callback, types.CallbackQuery):
            await message_or_callback.answer("❌ Больше треков нет.", show_alert=True)
        else:
            await message_or_callback.answer("❌ Ничего не нашли ни на одной площадке 💀")
        return

    _src_icons = {"SoundCloud": "🔊", "YouTube Music": "▶️"}
    buttons = []
    for t in tracks:
        icon = _src_icons.get(t.get("source", ""), "🎵")
        btn_text = f"{icon} {t['artist']} — {t['title']} [{t['duration']}]"
        cb_data = f"dl_sc:{t['id']}"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])
    
    nav_buttons = []
    safe_query = query[:25] if query else "none"

    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⏪", callback_data=f"mus_pg:{mode}:{safe_query}:{page-1}"))
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text=f"{page}", callback_data=f"mus_pg:{mode}:{safe_query}:{page-1}"))
        
    nav_buttons.append(InlineKeyboardButton(text=f"· {page+1} ·", callback_data="ignore"))
    
    nav_buttons.append(InlineKeyboardButton(text=f"{page+2}", callback_data=f"mus_pg:{mode}:{safe_query}:{page+1}"))
    nav_buttons.append(InlineKeyboardButton(text="⏩", callback_data=f"mus_pg:{mode}:{safe_query}:{page+1}"))
    
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
    
# ── ВИЗУАЛ 1+3: Карточка трека с доминирующим цветом обложки ─────────────
def _dominant_color(img_bytes: bytes) -> tuple[int, int, int]:
    """Возвращает доминирующий цвет картинки через простой квантайзинг Pillow."""
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((64, 64))
        quantized = img.quantize(colors=5, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette()[:15]  # первые 5 цветов × RGB
        # Берём самый насыщенный (не серый) из топ-5
        best = (80, 80, 80)
        best_sat = 0.0
        for i in range(5):
            r, g, b = palette[i*3], palette[i*3+1], palette[i*3+2]
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / mx if mx else 0
            if sat > best_sat:
                best_sat = sat
                best = (r, g, b)
        return best
    except Exception:
        return (30, 30, 40)

def make_track_card(
    artist: str, title: str, duration: str, source: str,
    cover_bytes: bytes | None, genre: str = ""
) -> bytes:
    """
    Рисует карточку 900×300 px:
    - Левая треть: обложка альбома
    - Правая часть: градиент от доминирующего цвета + текст
    Возвращает JPEG-байты.
    """
    W, H = 900, 300
    COVER_W = 300

    # ── фон: градиент от доминирующего цвета ──
    dom = _dominant_color(cover_bytes) if cover_bytes else (30, 30, 40)
    # тёмная версия для правого края
    dark = tuple(max(0, c - 80) for c in dom)

    card = Image.new("RGB", (W, H), dark)
    draw = ImageDraw.Draw(card)

    # горизонтальный градиент (левая часть светлее)
    for x in range(COVER_W, W):
        t = (x - COVER_W) / (W - COVER_W)
        r = int(dom[0] * (1 - t) + dark[0] * t)
        g = int(dom[1] * (1 - t) + dark[1] * t)
        b = int(dom[2] * (1 - t) + dark[2] * t)
        draw.line([(x, 0), (x, H)], fill=(r, g, b))

    # ── обложка ──
    if cover_bytes:
        try:
            cover_img = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
            cover_img = cover_img.resize((COVER_W, H), Image.Resampling.LANCZOS)
            card.paste(cover_img, (0, 0))
        except Exception:
            pass

    # мягкий градиент-оверлей на стыке обложки и текста
    for x in range(20):
        alpha = int(200 * (1 - x / 20))
        draw.line([(COVER_W + x, 0), (COVER_W + x, H)], fill=(*dom, alpha))  # type: ignore

    # ── шрифты ──
    font_dir = os.path.join(BASE_DIR, "fonts")
    try:
        f_big   = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 36)
        f_med   = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 22)
        f_small = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans.ttf"),      18)
    except Exception:
        f_big = f_med = f_small = ImageFont.load_default()

    # ── текст ──
    TX = COVER_W + 28
    # яркость фона в зоне текста
    luma = 0.299 * dom[0] + 0.587 * dom[1] + 0.114 * dom[2]
    fg = (255, 255, 255) if luma < 140 else (20, 20, 20)
    fg_dim = tuple(min(255, c + 60) for c in fg) if luma < 140 else (80, 80, 80)  # type: ignore

    # артист
    artist_short = artist[:32] + "…" if len(artist) > 32 else artist
    draw.text((TX, 55), artist_short, font=f_med, fill=fg_dim)

    # название (с переносом)
    title_short = title[:44] + "…" if len(title) > 44 else title
    lines = textwrap.wrap(title_short, width=22)[:2]
    y_title = 90
    for line in lines:
        draw.text((TX, y_title), line, font=f_big, fill=fg)
        y_title += 44

    # жанр
    if genre and genre.lower() not in ("неизвестен", "мультиплатформа"):
        clean = re.sub(r'[^a-zA-Zа-яА-Я0-9 ]', '', genre).strip()
        if clean:
            draw.text((TX, y_title + 10), f"#{clean.replace(' ', '_').lower()}", font=f_small, fill=fg_dim)

    # источник + длительность
    src_icon = {"SoundCloud": "🔊", "YouTube Music": "▶️"}.get(source, "🎵")
    info_line = f"{src_icon} {source}  ·  ⏱ {duration}"
    draw.text((TX, H - 42), info_line, font=f_small, fill=fg_dim)

    # тонкая полоска доминирующего цвета снизу
    draw.rectangle([(0, H - 4), (W, H)], fill=dom)

    out = io.BytesIO()
    card.save(out, format="JPEG", quality=88)
    return out.getvalue()


async def send_track_to_user(target_obj, track_id: str):
    """Универсальная функция отправки. Поддерживает кэш, очередь и умные обложки."""
    is_callback = isinstance(target_obj, types.CallbackQuery)
    message = target_obj.message if is_callback else target_obj

    if is_callback: await target_obj.answer("⏳ Проверяю базу...", show_alert=False)
    
    cached_file_id = get_cached_file_id(track_id)
    track = await music_engine.get_track_details(track_id)
    
    if not track or not track.get('stream_url'):
        err = "❌ Ошибка: не удалось получить поток трека (он удален или залочен)."
        if is_callback: await message.answer(err)
        else: await message.answer(err)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❤️ Избранное", callback_data=f"fav_sc:{track_id}"),
            InlineKeyboardButton(text="➕ В очередь", callback_data=f"queue_add:{track_id}"),
        ],
        [
            InlineKeyboardButton(text="🧠 Похожее", callback_data=f"rec_sc:{track_id}"),
            InlineKeyboardButton(text="📝 Текст", callback_data=f"lyrics:{track_id}"),
        ],
        [
            InlineKeyboardButton(text="📋 В плейлист", callback_data=f"pl_pick:{track_id}"),
            InlineKeyboardButton(text="👤 Артист", callback_data=f"artist_page:{track_id}"),
        ],
    ])

    genre_raw = track.get('genre', 'Неизвестен')
    clean_genre = re.sub(r'[^a-zA-Zа-яА-Я0-9\s]', '', genre_raw).strip().replace(" ", "_").lower()
    genre_tag = f"#{clean_genre}" if clean_genre and clean_genre != 'неизвестен' else "#music"

    explicit = get_explicit_tag(track['title'])
    
    # 2. ФОРМИРУЕМ текст с уже готовым хештегом и тегом Explicit
    _src = track.get("source", "SoundCloud")
    _src_icon = {"SoundCloud": "🔊", "YouTube Music": "▶️"}.get(_src, "🎵")
    caption = (
        f"🎧 *{track['artist']} — {track['title']}*{explicit}\n\n"
        f"🎼 *Жанр:* {genre_tag}\n"
        f"{_src_icon} *Источник:* {_src}"
    )


    if cached_file_id:
        try:
            await message.answer_audio(
                cached_file_id, performer=track['artist'], title=track['title'],
                caption=caption, parse_mode="Markdown", reply_markup=keyboard
            )
            return
        except Exception:
            pass 

    status_msg = await message.answer("⏳ `[░░░░░░░░░░] 0% — Инициализация...`", parse_mode="Markdown")
    anim_task = asyncio.create_task(animate_loading(status_msg))
    
    async with DOWNLOAD_SEMAPHORE:
        # УМНЫЕ ОБЛОЖКИ: Каскадный поиск
        # 1. Пробуем найти студийную обложку в iTunes
        final_cover_url = await music_engine.fetch_itunes_cover(track['artist'], track['title'])
        
        # 2. Если нет - берем родную из SoundCloud
        if not final_cover_url:
            final_cover_url = track.get('artwork_url')
            
        # 3. Если обложки нет вообще или это дефолтная заглушка — Генерим нейросетью!
        if not final_cover_url or "default_avatar" in final_cover_url:
            # Делаем промпт для ИИ, чтобы картинка подходила по вайбу (и добавляем рандомный seed)
            safe_prompt = urllib.parse.quote(f"cool abstract aesthetic music album cover for {track['artist']} genre {track.get('genre', 'music')} without any text")
            seed = random.randint(1, 999999)
            final_cover_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1000&height=1000&nologo=true&seed={seed}"

        audio_bytes, cover_bytes = await asyncio.gather(
            music_engine.download_file(track['stream_url']),
            music_engine.download_file(final_cover_url)
        )
        
        anim_task.cancel() 

        if not audio_bytes:
            await status_msg.edit_text("❌ Ошибка при скачивании файла.")
            return

        mb_size = round(len(audio_bytes) / (1024 * 1024), 2)
        geek_data = f"\n|| 💽 ID: {track_id} | 📦 {mb_size} MB | ⚙️ ~128 kbps ||"

        caption = (
            f"🎧 *{track['artist']} — {track['title']}*\n\n"
            f"🎼 *Жанр:* {genre_tag}\n"
            f"{_src_icon} *Источник:* {_src}{geek_data}"
        )

        audio_bytes = add_id3_tags(audio_bytes, track['title'], track['artist'], cover_bytes)
        audio_file = BufferedInputFile(audio_bytes, filename=f"{track['title']}.mp3")
        thumb_file = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None

        # ── Визуал 1+3: красивая карточка перед аудио ──
        try:
            dur_str = f"{track.get('duration', '?')}"
            card_bytes = await asyncio.to_thread(
                make_track_card,
                track['artist'], track['title'], dur_str,
                track.get('source', 'SoundCloud'), cover_bytes, track.get('genre', '')
            )
            await message.answer_photo(
                BufferedInputFile(card_bytes, filename="card.jpg"),
                caption=caption, parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Карточка не сгенерировалась: {e}")

        try:
            sent_audio = await message.answer_audio(
                audio=audio_file, performer=track['artist'], title=track['title'],
                reply_markup=keyboard, thumbnail=thumb_file
            )
            save_cached_file_id(track_id, sent_audio.audio.file_id)
            log_track_history(target_obj.from_user.id, {
                "id": track_id, "title": track['title'],
                "artist": track['artist'], "source": track.get("source", "SoundCloud")
            })
            await status_msg.delete()

        except Exception as e:
            logger.error(f"Ошибка отправки аудио: {e}")
            await status_msg.edit_text(f"❌ Телеграм отказался принимать файл: {e}")

async def callback_download_music(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    await send_track_to_user(callback, track_id)

async def callback_lyrics(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    await callback.answer("⏳ Ищу текст по базам...", show_alert=False)
    
    track = await music_engine.get_track_details(track_id)
    if not track: return
    
    lyrics = await music_engine.fetch_lyrics(track['artist'], track['title'])
    if not lyrics:
        await callback.answer("❌ Текст для этого трека не найден.", show_alert=True)
        return
        
    safe_lyrics = re.sub(r'(\[.*?\])', r'*\1*', lyrics)
    safe_lyrics = safe_lyrics.replace('\r\n\r\n', '\n\n').replace('\n\n', '\n\n❖ ❖ ❖\n\n')
    
    safe_lyrics = safe_lyrics[:3900] + ("\n\n[...]" if len(safe_lyrics) > 3900 else "")
    await callback.message.answer(f"📝 *{track['artist']} — {track['title']}*\n\n{safe_lyrics}", parse_mode="Markdown")

async def callback_music_recs(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    await callback.answer("🧠 ИИ подбирает вайб...", show_alert=False)
    status_msg = await callback.message.answer("⏳ Нейросеть генерирует плейлист...")
    
    track = await music_engine.get_track_details(track_id)
    if not track: return
        
    prompt = (
        f"Посоветуй 5 топовых треков, похожих по стилю и вайбу на: {track['artist']} — {track['title']}. "
        "Выдай ТОЛЬКО валидный JSON-массив строк. Пример: [\"Artist 1 - Title 1\", \"Artist 2 - Title 2\"]. "
        "Никакого лишнего текста, маркдауна или пояснений. Только чистый JSON."
    )
    
    try:
        response = await gemini_client.aio.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        recs_list = json.loads(clean_text)
        
        await status_msg.edit_text("🔍 Пробиваю треки ИИ по базе SoundCloud...")
        buttons = []
        
        for q in recs_list[:5]:
            search_res = await music_engine.search_multi(q, limit=1)
            if search_res:
                t = search_res[0]
                buttons.append([InlineKeyboardButton(text=f"🎵 {t['artist']} — {t['title']}", callback_data=f"dl_sc:{t['id']}")])
                
        if buttons:
            await status_msg.edit_text("🧠 *Умные рекомендации специально для тебя:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
        else:
            await status_msg.edit_text("❌ ИИ выдал классные треки, но в SoundCloud их не оказалось.")
            
    except Exception as e:
        logger.error(f"Ошибка реков 2.0: {e}")
        await status_msg.edit_text("❌ Процессоры перегрелись или ИИ выдал кривой формат.")

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

# --- ФУНКЦИОНАЛ 1: МОЯ ВОЛНА ---
async def callback_start_wave(callback: types.CallbackQuery):
    await callback.answer()
    favs = get_music_favs(callback.from_user.id)
    
    status_msg = await callback.message.answer("🌊 Запускаю Мою Волну...")
    anim_task = asyncio.create_task(animate_wave(status_msg))
    
    try:
        if favs:
            # Берем 3 случайных трека из избранного для контекста
            sample = random.sample(favs, min(3, len(favs)))
            context = ", ".join([f"{t['artist']} - {t['title']}" for t in sample])
            prompt = f"Юзер любит эти треки: {context}. Выдай 5 очень похожих треков других исполнителей. Верни СТРОГО JSON массив строк."
        else:
            prompt = "Выдай 5 популярных крутых треков (хип-хоп, поп, фонк). Верни СТРОГО JSON массив строк."
            
        response = await gemini_client.aio.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        recs_list = json.loads(clean_text)
        
        buttons = []
        for q in recs_list[:5]:
            search_res = await music_engine.search_multi(q, limit=1)
            if search_res:
                t = search_res[0]
                buttons.append([InlineKeyboardButton(text=f"🌊 {t['artist']} — {t['title']}", callback_data=f"dl_sc:{t['id']}")])
        
        buttons.append([InlineKeyboardButton(text="🔄 Следующая Волна", callback_data="start_wave")])
        
        anim_task.cancel()
        if buttons:
            await status_msg.edit_text("🌊 *Моя Волна*\nБесконечный поток под твой вкус:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
        else:
            await status_msg.edit_text("❌ Волна разбилась о скалы API. Попробуй еще раз.")
            
    except Exception as e:
        anim_task.cancel()
        logger.error(f"Wave error: {e}")
        await status_msg.edit_text("❌ Нейросеть не смогла сгенерировать волну.")

# --- ФУНКЦИОНАЛ 7: РАДАР РЕЛИЗОВ ---
async def callback_radar(callback: types.CallbackQuery):
    await callback.answer("🚀 Сканирую новинки твоих любимых артистов...", show_alert=False)
    favs = get_music_favs(callback.from_user.id)
    if not favs:
        await callback.message.answer("❌ Добавь треки в избранное, чтобы радар знал, кого искать!")
        return
        
    # Собираем уникальных артистов
    artists = list(set([f['artist'] for f in favs]))
    target_artist = random.choice(artists) # Ищем новинку для случайного артиста из базы
    
    tracks = await music_engine.search_multi(f"{target_artist} 2026", limit=3)
    if not tracks:
        await callback.message.answer(f"🔕 У *{target_artist}* пока нет свежих дропов.")
        return
        
    buttons = [[InlineKeyboardButton(text=f"🚀 {t['artist']} — {t['title']}", callback_data=f"dl_sc:{t['id']}")] for t in tracks]
    await callback.message.answer(f"🚀 *Радар релизов*\nСвежее для тебя от *{target_artist}*:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

# --- ВИЗУАЛ 5: ИТОГИ ГОДА (WRAPPED) ---
async def cmd_wrapped(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    hist = get_user_history(message.from_user.id)
    if len(hist) < 5:
        await message.answer("🤡 Какой тебе Wrapped, ты даже 5 треков не послушал. Иди слушай музыку.")
        return
    status_msg = await message.answer("🎁 Рендерю инфографику...")
    try:
        user_name = message.from_user.username or message.from_user.first_name or "анон"
        card_bytes = await asyncio.to_thread(_make_wrapped_card, hist, user_name)
        await message.answer_photo(
            BufferedInputFile(card_bytes, filename="wrapped.jpg"),
            caption=(
                f"🎁 *Твои музыкальные итоги*\n"
                f"🎵 {len(hist)} треков в истории\n"
                f"Поделись с корешами!"
            ),
            parse_mode="Markdown"
        )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Wrapped v2 error: {e}")
        await status_msg.edit_text(f"❌ Инфографика не сгенерировалась: {e}")

# --- ВИЗУАЛ 6: ГЛАВНЫЙ ДАШБОРД ---
from aiogram.types import WebAppInfo

async def cmd_my_music(message: types.Message):
    """Показывает избранное пользователя — аналог /music > Избранное, но одной командой."""
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    favs = get_music_favs(message.from_user.id)

    if not favs:
        await message.answer(
            "💀 *Твой плейлист пуст.*\n\n"
            "Нажимай ❤️ под треками чтобы сохранять их сюда. "
            "Ищи треки через `/find <название>`.",
            parse_mode="Markdown"
        )
        return

    limit = 6
    page = 0
    page_favs = favs[:limit]

    buttons = []
    for f in page_favs:
        explicit = get_explicit_tag(f['title'])
        source_icon = {"SoundCloud": "🔊", "YouTube Music": "▶️"}.get(f.get("source", ""), "🎵")
        buttons.append([InlineKeyboardButton(
            text=f"{source_icon} {f['artist']} — {f['title']}{explicit}",
            callback_data=f"dl_sc:{f['id']}"
        )])

    nav = []
    if limit < len(favs):
        nav.append(InlineKeyboardButton(text="⏩", callback_data="show_favs:1"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🎶 В дашборд", callback_data="back_to_dash")])

    await message.answer(
        f"❤️ *Твоя база — {len(favs)} трек(ов):*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )


async def cmd_music_dashboard(message: types.Message):
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    
    # Кнопка Web App (заглушка для Визуала 1)
    # Замени web_app_url на свой домен в BotHost (например, bot-12345.bothost.ru)
# Обязательно с https://
web_app_url = "https://bot-твой-id.bothost.ru" 
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌊 Моя Волна", callback_data="start_wave"),
            InlineKeyboardButton(text="🔥 Чарты", callback_data="mus_pg:chart:none:0")
        ],
        [
            InlineKeyboardButton(text="❤️ Избранное", callback_data="show_favs:0"),
            InlineKeyboardButton(text="🕒 История", callback_data="show_hist:0")
        ],
        [
            InlineKeyboardButton(text="📋 Плейлисты", callback_data="show_playlists"),
            InlineKeyboardButton(text="🎵 Очередь", callback_data="queue_show")
        ],
        [InlineKeyboardButton(text="🚀 Радар релизов", callback_data="radar_releases"),
        [InlineKeyboardButton(text="🌐 Открыть плеер", web_app=WebAppInfo(url=web_app_url))],
        ]
    ])
    
    text = (
        "🎶 *MUSIC DASHBOARD*\n\n"
        "Добро пожаловать в хаб. Выбирай, какой вайб тебе нужен сейчас.\n"
        "Или ищи треки напрямую: `@Betboomers_bot [название]`"
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

# --- ИЗБРАННОЕ ИНЛАЙН-КНОПКАМИ ---
async def callback_show_favs(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    favs = get_music_favs(callback.from_user.id)
    
    if not favs:
        await callback.answer("💀 Твой плейлист пуст. Нажми ❤️ под треком.", show_alert=True)
        return
        
    limit = 6
    offset = page * limit
    page_favs = favs[offset:offset+limit]
    
    buttons = []
    for f in page_favs:
        explicit = get_explicit_tag(f['title'])
        buttons.append([InlineKeyboardButton(text=f"🎵 {f['artist']} — {f['title']}{explicit}", callback_data=f"dl_sc:{f['id']}")])
        
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⏪", callback_data=f"show_favs:{page-1}"))
    if offset + limit < len(favs): nav.append(InlineKeyboardButton(text="⏩", callback_data=f"show_favs:{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ Назад в Дашборд", callback_data="back_to_dash")])
    
    await callback.message.edit_text("❤️ *Твоя база (Избранное):*", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

# --- ИСТОРИЯ ПРОСЛУШИВАНИЙ (Функционал 4) ---
async def callback_show_hist(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    hist = get_user_history(callback.from_user.id)
    
    if not hist:
        await callback.answer("🕳 Ты еще ничего не слушал.", show_alert=True)
        return
        
    limit = 6
    offset = page * limit
    page_hist = hist[offset:offset+limit]
    
    buttons = []
    for f in page_hist:
        explicit = get_explicit_tag(f['title'])
        buttons.append([InlineKeyboardButton(text=f"🕒 {f['artist']} — {f['title']}{explicit}", callback_data=f"dl_sc:{f['id']}")])
        
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⏪", callback_data=f"show_hist:{page-1}"))
    if offset + limit < len(hist): nav.append(InlineKeyboardButton(text="⏩", callback_data=f"show_hist:{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ Назад в Дашборд", callback_data="back_to_dash")])
    
    await callback.message.edit_text("🕒 *Недавно прослушанное:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

# ═══════════════════════════════════════════════════════════
#  ФУНКЦИОНАЛ 1 — ПЛЕЙЛИСТЫ
# ═══════════════════════════════════════════════════════════

async def cmd_playlists(message: types.Message):
    """Список плейлистов пользователя."""
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    pls = get_playlists(message.from_user.id)
    if not pls:
        await message.answer(
            "📋 *У тебя пока нет плейлистов.*\n\n"
            "Нажми *«📋 В плейлист»* под любым треком чтобы создать первый.",
            parse_mode="Markdown"
        )
        return
    buttons = []
    for name, tracks in pls.items():
        buttons.append([InlineKeyboardButton(
            text=f"📋 {name} ({len(tracks)} тр.)",
            callback_data=f"pl_open:{name[:30]}"
        )])
    buttons.append([InlineKeyboardButton(text="🗑 Удалить плейлист", callback_data="pl_delete_menu")])
    buttons.append([InlineKeyboardButton(text="◀️ Дашборд", callback_data="back_to_dash")])
    await message.answer("📋 *Твои плейлисты:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")


async def callback_pl_pick(callback: types.CallbackQuery):
    """Выбор плейлиста для добавления трека (или создание нового)."""
    track_id = callback.data.split(":", 1)[1]
    pls = get_playlists(callback.from_user.id)
    buttons = []
    for name in pls:
        buttons.append([InlineKeyboardButton(text=f"📋 {name}", callback_data=f"pl_add:{name[:30]}:{track_id}")])
    buttons.append([InlineKeyboardButton(text="➕ Новый плейлист", callback_data=f"pl_new:{track_id}")])
    buttons.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="pl_cancel")])
    await callback.message.answer(
        "📋 Выбери плейлист или создай новый:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


async def callback_pl_new(callback: types.CallbackQuery, state: FSMContext):
    track_id = callback.data.split(":", 1)[1]
    await state.set_state(MusicStates.waiting_playlist_name)
    await state.update_data(pending_track_id=track_id)
    await callback.message.answer("✏️ Напиши название нового плейлиста:")
    await callback.answer()


async def process_playlist_name(message: types.Message, state: FSMContext):
    data = await state.get_data()
    track_id = data.get("pending_track_id")
    name = message.text.strip()[:40]
    await state.clear()
    if not name:
        await message.answer("❌ Пустое имя — не катит.")
        return
    if track_id:
        # Получаем инфу о треке чтобы сохранить
        track = await music_engine.get_track_details(track_id)
        if track:
            save_playlist_track(message.from_user.id, name, {
                "id": track_id, "title": track["title"], "artist": track["artist"],
                "source": track.get("source", "SoundCloud")
            })
            await message.answer(f"✅ Плейлист *«{name}»* создан, трек добавлен!", parse_mode="Markdown")
        else:
            await message.answer("❌ Не удалось получить инфу о треке.")
    else:
        await message.answer("❌ Что-то пошло не так.")


async def callback_pl_add(callback: types.CallbackQuery):
    """Добавляет трек в существующий плейлист."""
    parts = callback.data.split(":", 2)
    _, pl_name, track_id = parts
    track = await music_engine.get_track_details(track_id)
    if not track:
        await callback.answer("❌ Трек не найден", show_alert=True)
        return
    ok = save_playlist_track(callback.from_user.id, pl_name, {
        "id": track_id, "title": track["title"], "artist": track["artist"],
        "source": track.get("source", "SoundCloud")
    })
    if ok:
        await callback.answer(f"✅ Добавлено в «{pl_name}»", show_alert=True)
    else:
        await callback.answer("🤡 Уже есть в этом плейлисте.", show_alert=True)


async def callback_pl_open(callback: types.CallbackQuery):
    """Открыть плейлист."""
    pl_name = callback.data.split(":", 1)[1]
    pls = get_playlists(callback.from_user.id)
    tracks = pls.get(pl_name, [])
    if not tracks:
        await callback.answer("💀 Плейлист пуст", show_alert=True)
        return
    buttons = []
    for t in tracks[:10]:
        src_icon = {"SoundCloud": "🔊", "YouTube Music": "▶️"}.get(t.get("source", ""), "🎵")
        buttons.append([InlineKeyboardButton(
            text=f"{src_icon} {t['artist']} — {t['title']}",
            callback_data=f"dl_sc:{t['id']}"
        )])
    buttons.append([
        InlineKeyboardButton(text="🔀 Шаффл", callback_data=f"pl_shuffle:{pl_name[:30]}"),
        InlineKeyboardButton(text="▶️ Всё в очередь", callback_data=f"pl_to_queue:{pl_name[:30]}")
    ])
    buttons.append([InlineKeyboardButton(text="◀️ Плейлисты", callback_data="show_playlists")])
    await callback.message.edit_text(
        f"📋 *{pl_name}* — {len(tracks)} тр.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )


async def callback_pl_shuffle(callback: types.CallbackQuery):
    """Шаффл — случайный трек из плейлиста."""
    pl_name = callback.data.split(":", 1)[1]
    pls = get_playlists(callback.from_user.id)
    tracks = pls.get(pl_name, [])
    if not tracks:
        await callback.answer("💀 Плейлист пуст", show_alert=True)
        return
    t = random.choice(tracks)
    await callback.answer("🔀 Кидаю случайный трек...")
    await send_track_to_user(callback, str(t["id"]))


async def callback_pl_to_queue(callback: types.CallbackQuery):
    """Добавить весь плейлист в очередь."""
    pl_name = callback.data.split(":", 1)[1]
    pls = get_playlists(callback.from_user.id)
    tracks = pls.get(pl_name, [])
    if not tracks:
        await callback.answer("💀 Пусто", show_alert=True)
        return
    for t in tracks:
        queue_push(callback.from_user.id, t)
    await callback.answer(f"➕ {len(tracks)} треков в очереди", show_alert=True)


async def callback_pl_cancel(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()


async def callback_pl_delete_menu(callback: types.CallbackQuery):
    pls = get_playlists(callback.from_user.id)
    if not pls:
        await callback.answer("Нет плейлистов", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {name}", callback_data=f"pl_delete:{name[:30]}")] for name in pls]
    buttons.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="pl_cancel")])
    await callback.message.answer("Какой плейлист удалить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


async def callback_pl_delete(callback: types.CallbackQuery):
    pl_name = callback.data.split(":", 1)[1]
    delete_playlist(callback.from_user.id, pl_name)
    await callback.answer(f"🗑 «{pl_name}» удалён", show_alert=True)
    await callback.message.delete()


async def callback_show_playlists(callback: types.CallbackQuery):
    pls = get_playlists(callback.from_user.id)
    if not pls:
        await callback.answer("📋 Нет плейлистов", show_alert=True)
        return
    buttons = []
    for name, tracks in pls.items():
        buttons.append([InlineKeyboardButton(text=f"📋 {name} ({len(tracks)} тр.)", callback_data=f"pl_open:{name[:30]}")])
    buttons.append([InlineKeyboardButton(text="◀️ Дашборд", callback_data="back_to_dash")])
    await callback.message.edit_text("📋 *Твои плейлисты:*", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
#  ФУНКЦИОНАЛ 2 — ОЧЕРЕДЬ ВОСПРОИЗВЕДЕНИЯ
# ═══════════════════════════════════════════════════════════

async def callback_queue_add(callback: types.CallbackQuery):
    track_id = callback.data.split(":", 1)[1]
    track = await music_engine.get_track_details(track_id)
    if not track:
        await callback.answer("❌ Трек не найден", show_alert=True)
        return
    queue_push(callback.from_user.id, {
        "id": track_id, "title": track["title"],
        "artist": track["artist"], "source": track.get("source", "SoundCloud")
    })
    q = queue_list(callback.from_user.id)
    await callback.answer(f"➕ В очередь! Позиция: {len(q)}", show_alert=False)


async def cmd_queue(message: types.Message):
    """Показать и управлять очередью."""
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    q = queue_list(message.from_user.id)
    if not q:
        await message.answer("🎵 Очередь пуста. Жми ➕ под треками чтобы добавлять.")
        return
    buttons = []
    for i, t in enumerate(q[:10]):
        src_icon = {"SoundCloud": "🔊", "YouTube Music": "▶️"}.get(t.get("source", ""), "🎵")
        buttons.append([InlineKeyboardButton(
            text=f"{i+1}. {src_icon} {t['artist']} — {t['title']}",
            callback_data=f"dl_sc:{t['id']}"
        )])
    buttons.append([
        InlineKeyboardButton(text="▶️ Следующий", callback_data="queue_next"),
        InlineKeyboardButton(text="🗑 Очистить", callback_data="queue_clear")
    ])
    await message.answer(
        f"🎵 *Очередь — {len(q)} треков:*\n_Нажми на трек чтобы воспроизвести_",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )


async def callback_queue_next(callback: types.CallbackQuery):
    track = queue_pop(callback.from_user.id)
    if not track:
        await callback.answer("🎵 Очередь пуста!", show_alert=True)
        return
    await callback.answer(f"▶️ Играю: {track['artist']} — {track['title']}")
    await send_track_to_user(callback, str(track["id"]))


async def callback_queue_clear(callback: types.CallbackQuery):
    queue_clear(callback.from_user.id)
    await callback.answer("🗑 Очередь очищена", show_alert=True)
    try:
        await callback.message.delete()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  ФУНКЦИОНАЛ 4 — СТРАНИЦА АРТИСТА + ПОДПИСКИ
# ═══════════════════════════════════════════════════════════

async def callback_artist_page(callback: types.CallbackQuery):
    """Страница артиста: топ треков + кнопка подписки."""
    track_id = callback.data.split(":", 1)[1]
    await callback.answer("👤 Загружаю профиль артиста...")
    track = await music_engine.get_track_details(track_id)
    if not track:
        await callback.answer("❌ Трек не найден", show_alert=True)
        return
    artist = track["artist"]
    status_msg = await callback.message.answer(f"🔍 Ищу треки артиста *{artist}*...", parse_mode="Markdown")
    top_tracks = await music_engine.search_multi(artist, limit=5)
    # фильтруем — только этого артиста
    artist_tracks = [t for t in top_tracks if artist.lower() in t["artist"].lower()][:5]
    if not artist_tracks:
        artist_tracks = top_tracks[:5]

    subs = get_artist_subs(callback.from_user.id)
    is_subbed = artist in subs
    sub_btn = InlineKeyboardButton(
        text="🔕 Отписаться" if is_subbed else "🔔 Следить за артистом",
        callback_data=f"artist_unsub:{artist[:40]}" if is_subbed else f"artist_sub:{artist[:40]}"
    )

    buttons = []
    for t in artist_tracks:
        src_icon = {"SoundCloud": "🔊", "YouTube Music": "▶️"}.get(t.get("source", ""), "🎵")
        buttons.append([InlineKeyboardButton(
            text=f"{src_icon} {t['title']} [{t['duration']}]",
            callback_data=f"dl_sc:{t['id']}"
        )])
    buttons.append([sub_btn])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_dash")])

    await status_msg.edit_text(
        f"👤 *{artist}*\n\n🎵 Топ треков:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )


async def callback_artist_sub(callback: types.CallbackQuery):
    artist = callback.data.split(":", 1)[1]
    ok = add_artist_sub(callback.from_user.id, artist)
    if ok:
        await callback.answer(f"🔔 Подписался на {artist}! Радар будет следить за новинками.", show_alert=True)
    else:
        await callback.answer("Уже подписан", show_alert=True)


async def callback_artist_unsub(callback: types.CallbackQuery):
    artist = callback.data.split(":", 1)[1]
    remove_artist_sub(callback.from_user.id, artist)
    await callback.answer(f"🔕 Отписался от {artist}", show_alert=True)


# ═══════════════════════════════════════════════════════════
#  ФУНКЦИОНАЛ 5 — DJ MODE (групповое голосование)
# ═══════════════════════════════════════════════════════════

async def cmd_dj(message: types.Message):
    """Запускает DJ-сессию в групповом чате."""
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    chat_id = message.chat.id
    if message.chat.type == "private":
        await message.answer("🎧 DJ Mode работает только в групповых чатах!")
        return
    query = message.text.replace("/dj", "").strip()
    if not query:
        await message.answer("🎧 Укажи что ищем: `/dj название трека`", parse_mode="Markdown")
        return
    status = await message.answer(f"🎧 DJ ищет варианты для голосования: *{query}*...", parse_mode="Markdown")
    tracks = await music_engine.search_multi(query, limit=4)
    if not tracks:
        await status.edit_text("❌ Ничего не нашли. DJ в шоке.")
        return

    DJ_SESSIONS[chat_id] = {
        "tracks": tracks,
        "votes": {str(t["id"]): set() for t in tracks},
        "msg_id": None,
        "initiator": message.from_user.id
    }

    buttons = []
    for i, t in enumerate(tracks):
        src_icon = {"SoundCloud": "🔊", "YouTube Music": "▶️"}.get(t.get("source", ""), "🎵")
        buttons.append([InlineKeyboardButton(
            text=f"{src_icon} {t['artist']} — {t['title']} [{t['duration']}]  👍 0",
            callback_data=f"dj_vote:{t['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🏁 Завершить голосование", callback_data="dj_finish")])

    sent = await status.edit_text(
        f"🎧 *DJ MODE*\n{message.from_user.first_name} запустил голосование!\nГолосуй за следующий трек:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    DJ_SESSIONS[chat_id]["msg_id"] = sent.message_id


async def callback_dj_vote(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    track_id = callback.data.split(":", 1)[1]
    session = DJ_SESSIONS.get(chat_id)
    if not session:
        await callback.answer("❌ Сессия уже закончилась", show_alert=True)
        return

    # Снимаем предыдущий голос этого юзера
    for tid, voters in session["votes"].items():
        voters.discard(user_id)

    session["votes"][track_id].add(user_id)

    # Обновляем кнопки с числом голосов
    buttons = []
    for t in session["tracks"]:
        tid = str(t["id"])
        count = len(session["votes"].get(tid, set()))
        src_icon = {"SoundCloud": "🔊", "YouTube Music": "▶️"}.get(t.get("source", ""), "🎵")
        marker = " ✅" if tid == track_id and user_id in session["votes"].get(tid, set()) else ""
        buttons.append([InlineKeyboardButton(
            text=f"{src_icon} {t['artist']} — {t['title']}  👍 {count}{marker}",
            callback_data=f"dj_vote:{tid}"
        )])
    buttons.append([InlineKeyboardButton(text="🏁 Завершить голосование", callback_data="dj_finish")])

    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception:
        pass
    await callback.answer("✅ Голос засчитан!")


async def callback_dj_finish(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    session = DJ_SESSIONS.get(chat_id)
    if not session:
        await callback.answer("❌ Сессия не найдена", show_alert=True)
        return

    # Победитель — максимум голосов
    winner_id = max(session["votes"], key=lambda tid: len(session["votes"][tid]))
    winner_count = len(session["votes"][winner_id])
    winner_track = next((t for t in session["tracks"] if str(t["id"]) == winner_id), None)

    DJ_SESSIONS.pop(chat_id, None)

    if not winner_track or winner_count == 0:
        await callback.message.edit_text("🎧 Никто не проголосовал. DJ уходит обиженным.")
        return

    await callback.message.edit_text(
        f"🏆 *Победитель голосования:*\n🎵 {winner_track['artist']} — {winner_track['title']}\n👍 {winner_count} голос(ов)\n\n⏳ Загружаю...",
        parse_mode="Markdown"
    )
    await send_track_to_user(callback, winner_id)


# ═══════════════════════════════════════════════════════════
#  ВИЗУАЛ 5 — WRAPPED v2 (полная инфографика)
# ═══════════════════════════════════════════════════════════

def _make_wrapped_card(hist: list, user_name: str) -> bytes:
    """Рисует полноценную Wrapped-карточку 900×1100 px."""
    W, H = 900, 1100
    # ── статистика ──
    artists: dict[str, int] = {}
    sources: dict[str, int] = {}
    for t in hist:
        artists[t.get("artist", "?")] = artists.get(t.get("artist", "?"), 0) + 1
        sources[t.get("source", "SoundCloud")] = sources.get(t.get("source", "SoundCloud"), 0) + 1

    top_artists = sorted(artists.items(), key=lambda x: x[1], reverse=True)[:3]
    top_tracks = hist[:5]
    total = len(hist)

    # «Музыкальная личность»
    if total >= 50:
        persona = "🔥 Марафонец"
    elif total >= 20:
        persona = "🎧 Меломан"
    elif total >= 10:
        persona = "🎵 Слушатель"
    else:
        persona = "🌱 Новичок"

    # Топ-источник
    top_src = max(sources, key=sources.get) if sources else "SoundCloud"

    font_dir = os.path.join(BASE_DIR, "fonts")
    try:
        f_hero  = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 52)
        f_big   = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 34)
        f_med   = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 24)
        f_small = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans.ttf"),      20)
    except Exception:
        f_hero = f_big = f_med = f_small = ImageFont.load_default()

    # ── фон: вертикальный градиент фиолетово-чёрный ──
    img = Image.new("RGB", (W, H), (10, 5, 20))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        r = int(80 * (1 - t) + 10 * t)
        g = int(0  * (1 - t) + 5  * t)
        b = int(120 * (1 - t) + 20 * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # декоративные круги
    for cx, cy, cr, alpha in [(750, 100, 180, 25), (100, 900, 220, 20), (500, 550, 100, 15)]:
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.ellipse([(cx-cr, cy-cr), (cx+cr, cy+cr)], fill=(180, 80, 255, alpha))
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))

    draw = ImageDraw.Draw(img)

    # ── заголовок ──
    draw.text((W//2, 55), "ТВОЯ МУЗЫКА", font=f_hero, fill=(220, 180, 255), anchor="mm")
    draw.text((W//2, 115), f"@{user_name}", font=f_med, fill=(160, 120, 210), anchor="mm")
    draw.line([(60, 145), (W-60, 145)], fill=(100, 50, 160), width=2)

    # ── всего треков + личность ──
    draw.text((60, 165), f"🎵 Прослушано треков:", font=f_med, fill=(180, 180, 180))
    draw.text((60, 198), str(total), font=f_hero, fill=(255, 255, 255))
    draw.text((200, 218), persona, font=f_big, fill=(220, 160, 255))

    draw.line([(60, 270), (W-60, 270)], fill=(70, 40, 110), width=1)

    # ── топ артисты ──
    draw.text((60, 290), "👑 Топ артисты", font=f_big, fill=(220, 180, 255))
    medals = ["🥇", "🥈", "🥉"]
    for i, (artist, cnt) in enumerate(top_artists):
        y = 335 + i * 52
        draw.text((60, y), f"{medals[i]} {artist[:30]}", font=f_med, fill=(255, 255, 255))
        draw.text((W-70, y), f"{cnt} тр.", font=f_small, fill=(160, 120, 210), anchor="ra")

    draw.line([(60, 500), (W-60, 500)], fill=(70, 40, 110), width=1)

    # ── топ треки ──
    draw.text((60, 520), "🎧 Последние 5 треков", font=f_big, fill=(220, 180, 255))
    for i, t in enumerate(top_tracks):
        y = 565 + i * 48
        num_col = (140, 100, 200)
        draw.text((60, y), f"{i+1}.", font=f_med, fill=num_col)
        line = f"{t.get('artist','?')} — {t.get('title','?')}"
        if len(line) > 42:
            line = line[:41] + "…"
        draw.text((100, y), line, font=f_small, fill=(230, 230, 230))

    draw.line([(60, 810), (W-60, 810)], fill=(70, 40, 110), width=1)

    # ── пайчарт источников (упрощённый барчарт) ──
    draw.text((60, 830), "📊 Источники", font=f_big, fill=(220, 180, 255))
    src_colors = {"SoundCloud": (255, 85, 0), "YouTube Music": (255, 0, 0)}
    bar_y = 878
    bar_h = 36
    bar_max_w = W - 120
    for src, cnt in sorted(sources.items(), key=lambda x: x[1], reverse=True):
        ratio = cnt / total if total else 0
        bar_w = max(4, int(bar_max_w * ratio))
        col = src_colors.get(src, (120, 80, 200))
        draw.rounded_rectangle([(60, bar_y), (60 + bar_w, bar_y + bar_h)], radius=8, fill=col)
        draw.text((70, bar_y + 8), f"{src}: {cnt} ({int(ratio*100)}%)", font=f_small, fill=(255, 255, 255))
        bar_y += bar_h + 14

    # ── футер ──
    draw.line([(60, H-60), (W-60, H-60)], fill=(100, 50, 160), width=2)
    draw.text((W//2, H-35), f"🤖 generated by бот • {datetime.now().strftime('%d.%m.%Y')}", font=f_small, fill=(100, 80, 140), anchor="mm")

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


async def callback_back_to_dash(callback: types.CallbackQuery):
    await callback.message.delete()
    await cmd_music_dashboard(callback.message)


async def inline_music_search(inline_query: types.InlineQuery):
    query = inline_query.query.strip()
    if not query:
        return

    # Telegram позволяет максимум 50 результатов за один ответ
    tracks = await music_engine.search_multi(query, limit=50)
    results = []
    bot_user = await inline_query.bot.me()
    fallback_img = "https://i.imgur.com/8mX1wGg.png"
    _src_icons = {"SoundCloud": "🔊", "YouTube Music": "▶️"}

    for t in tracks:
        track_id = str(t['id'])
        cached_file_id = get_cached_file_id(track_id)
        src_icon = _src_icons.get(t.get("source", ""), "🎵")
        thumb = t.get('artwork_url') or fallback_img

        if cached_file_id:
            # Трек уже скачан и лежит в TG — кидаем аудио прямо в чат
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❤️ В избранное", callback_data=f"fav_sc:{track_id}"),
                InlineKeyboardButton(text="🧠 Похожее", callback_data=f"rec_sc:{track_id}")
            ]])
            results.append(
                InlineQueryResultCachedAudio(
                    id=f"cached_{track_id}",
                    audio_file_id=cached_file_id,
                    caption=f"{src_icon} *{t['artist']} — {t['title']}* [{t['duration']}]",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            )
        else:
            # Трека нет в кэше — карточка с кнопкой «Слушать» (deep link → бот скачает и закэширует)
            deep_link = f"https://t.me/{bot_user.username}?start=track_{track_id}"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Слушать / скачать", url=deep_link)
            ]])
            results.append(
                InlineQueryResultArticle(
                    id=f"art_{track_id}",
                    title=f"{t['artist']} — {t['title']}",
                    description=f"⏱ {t['duration']}  {src_icon} {t.get('source', 'SC')}  •  нажми чтобы открыть",
                    thumbnail_url=thumb,
                    input_message_content=InputTextMessageContent(
                        message_text=(
                            f"🎧 *{t['artist']} — {t['title']}*\n"
                            f"⏱ {t['duration']}  {src_icon} {t.get('source', 'SoundCloud')}"
                        ),
                        parse_mode="Markdown"
                    ),
                    reply_markup=keyboard
                )
            )

    await inline_query.answer(results, cache_time=60, is_personal=True)

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
            model='gemini-2.5-flash',
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
        response = await gemini_client.aio.models.generate_content(model='gemini-2.5-flash', contents=prompt)
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
            model='gemini-2.5-flash',
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
            model='imagen-4.0-ultra-generate', 
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

    # --- ЗАПУСКАЕМ БД И ВЕБ-СЕРВЕР ---
    init_db()
    await start_web_server()
    # ---------------------------------

    bot = Bot(token=TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.inline_query.register(inline_music_search)
    
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_getchatid, Command("chatid"))
    dp.message.register(cmd_pollinations, Command("poll"))
    dp.message.register(cmd_imagen, Command("imagen"))
    dp.message.register(cmd_meme, Command("meme"))
    dp.message.register(cmd_tldr, Command("tldr"))
    dp.message.register(cmd_music_find, Command("find"))
    dp.message.register(cmd_charts, Command("charts"))
    dp.message.register(cmd_music_dashboard, Command("music"))
    dp.message.register(cmd_my_music, Command("my_music"))
    dp.message.register(cmd_playlists, Command("playlists"))
    dp.message.register(cmd_queue, Command("queue"))
    dp.message.register(cmd_dj, Command("dj"))
    dp.message.register(cmd_wrapped, Command("wrapped"))
    dp.message.register(cmd_weather, Command("pogoda"))
    
    dp.message.register(cmd_today, F.text == "📋 Наряд сегодня")
    dp.message.register(cmd_tomorrow, F.text == "📅 Наряд завтра")
    dp.message.register(cmd_week, F.text == "📊 Расписание на неделю")
    dp.message.register(cmd_log, F.text == "📓 Журнал")
    dp.message.register(cmd_remind_now, F.text == "🔔 Напомнить сейчас")
    dp.message.register(cmd_settings, F.text == "⚙️ Настройки")

    dp.message.register(process_chat_id, AdminStates.waiting_chat_id)
    dp.message.register(process_register, AdminStates.waiting_register)
    dp.message.register(process_playlist_name, MusicStates.waiting_playlist_name)

    dp.callback_query.register(callback_done, F.data.startswith("done:"))
    dp.callback_query.register(callback_retry, F.data.startswith("retry:"))
    dp.callback_query.register(callback_toggle_reminders, F.data == "toggle_reminders")
    dp.callback_query.register(callback_toggle_ai_replies, F.data == "toggle_ai_replies")
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
    dp.callback_query.register(callback_music_page, F.data.startswith("mus_pg:"))
    dp.callback_query.register(callback_lyrics, F.data.startswith("lyrics:"))
    dp.callback_query.register(callback_show_favs, F.data.startswith("show_favs:"))
    dp.callback_query.register(callback_show_hist, F.data.startswith("show_hist:"))
    dp.callback_query.register(callback_back_to_dash, F.data == "back_to_dash")
    dp.callback_query.register(callback_start_wave, F.data == "start_wave")
    dp.callback_query.register(callback_radar, F.data == "radar_releases")
    # Плейлисты
    dp.callback_query.register(callback_pl_pick, F.data.startswith("pl_pick:"))
    dp.callback_query.register(callback_pl_new, F.data.startswith("pl_new:"))
    dp.callback_query.register(callback_pl_add, F.data.startswith("pl_add:"))
    dp.callback_query.register(callback_pl_open, F.data.startswith("pl_open:"))
    dp.callback_query.register(callback_pl_shuffle, F.data.startswith("pl_shuffle:"))
    dp.callback_query.register(callback_pl_to_queue, F.data.startswith("pl_to_queue:"))
    dp.callback_query.register(callback_pl_cancel, F.data == "pl_cancel")
    dp.callback_query.register(callback_pl_delete_menu, F.data == "pl_delete_menu")
    dp.callback_query.register(callback_pl_delete, F.data.startswith("pl_delete:"))
    dp.callback_query.register(callback_show_playlists, F.data == "show_playlists")
    # Очередь
    dp.callback_query.register(callback_queue_add, F.data.startswith("queue_add:"))
    dp.callback_query.register(callback_queue_next, F.data == "queue_next")
    dp.callback_query.register(callback_queue_clear, F.data == "queue_clear")
    dp.callback_query.register(lambda cb: cmd_queue(cb.message), F.data == "queue_show")
    # Артист
    dp.callback_query.register(callback_artist_page, F.data.startswith("artist_page:"))
    dp.callback_query.register(callback_artist_sub, F.data.startswith("artist_sub:"))
    dp.callback_query.register(callback_artist_unsub, F.data.startswith("artist_unsub:"))
    # DJ
    dp.callback_query.register(callback_dj_vote, F.data.startswith("dj_vote:"))
    dp.callback_query.register(callback_dj_finish, F.data == "dj_finish")

    dp.message.register(handle_photo, F.photo)
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
