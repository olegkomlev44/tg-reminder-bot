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
from google import genai
from google.genai import types as genai_types  # Импортируем типы ИИ безопасно
# Инициализируем клиента (ключ автоматически подтянется из переменных окружения, если назвать его GEMINI_API_KEY)
gemini_client = genai.Client()
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

# ══════════════════════════════════════════════
#  ЛОГИРОВАНИЕ — настраиваем САМЫМ ПЕРВЫМ ДЕЛОМ,
#  до любого кода, который может упасть. Иначе при ошибке на старте
#  (например, нет прав на создание папки) в логах хостинга не видно
#  вообще ничего — именно это и было причиной "тишины в логах".
# ══════════════════════════════════════════════
try:
    # На некоторых хостингах stdout оборачивается в блочную буферизацию,
    # из-за которой print/logger.info может не долетать до панели логов
    # часами — принудительно включаем построчную буферизацию.
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

# Папка, где реально лежит этот файл — на неё ВСЕГДА есть права на запись,
# независимо от того, как именно хостинг развернул проект (Docker/native).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Импорт генератора карточек — оборачиваем в try/except, чтобы при сбое
# (например, не скопировалась папка fonts/) сразу было видно конкретную
# причину в логах, а не молчаливое падение всего бота.
try:
    from card_generator import make_reminder_card, THEME_KEYS
    logger.info(f"🟢 card_generator импортирован, тем доступно: {len(THEME_KEYS)}")
except Exception:
    logger.error("🔴 НЕ УДАЛОСЬ ИМПОРТИРОВАТЬ card_generator.py:\n" + traceback.format_exc())
    raise

# ══════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════
TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID   = os.getenv("CHAT_ID",   "YOUR_CHAT_ID_HERE")
DATA_FILE = os.path.join(BASE_DIR, "duty_data.json")
TIMEZONE  = pytz.timezone("Europe/Moscow")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
if not gemini_client:
    logger.warning("🟡 GEMINI_API_KEY не задан, AI-функции отключены")


# Папка для временных картинок. Раньше тут было захардкожено "/app/shared/temp" —
# это путь, который существует только при ОЧЕНЬ конкретной Docker-сборке
# (WORKDIR /app + смонтированный shared-volume). На многих хостингах, включая
# нативный (без Dockerfile) режим BotHost, такой папки просто нет, и создать
# её нельзя (нет прав на запись в корень файловой системы) — из-за этого
# процесс падал на старте ДО логирования, и казалось, что бот вообще не работает.
# Теперь используем папку рядом со скриптом, а если и туда вдруг нельзя
# писать — откатываемся на системную temp-папку, которая есть всегда.
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

# ══════════════════════════════════════════════
#  СПИСКИ ДЕЖУРНЫХ
# ══════════════════════════════════════════════
SVODKI_LIST    = ["Арсений", "Олег", "Максим", "Руслан", "Игорь", "Илья", "Глеба", "Ильнар"]
PROCEDURA_LIST = ["Илья", "Руслан", "Игорь", "Глеба", "Ильнар"]

PERSON_EMOJI = {
    "Руслан": "🫥", "Олег": "😵", "Максим": "🤠", "Игорь": "👹",
    "Илья": "🤖", "Глеба": "💣", "Ильнар": "🐉",
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

# ══════════════════════════════════════════════
#  GEMINI AI ИНТЕГРАЦИЯ
# ══════════════════════════════════════════════
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
            model='gemini-2.5-flash', # <--- Обновили модель
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
        # Разрешаем нейросети сходить в реальный интернет
        response = await gemini_client.aio.models.generate_content(
            model='gemini-1.5-flash', 
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                tools=[{"google_search": {}}]
            )
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Ошибка погоды: {e}")
        return None

async def cmd_weather(message: types.Message):
    if not gemini_client: return
    # Удаляем команду юзера
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    status_msg = await message.answer("☁️ Запускаю метео-дрона в Одинцово (гуглю погоду)...")

    text = await get_weather_advice()
    
    try:
        await status_msg.delete()
    except:
        pass

    if text:
        await message.answer(f"☁️ *Метео-радар:*\n{text}", parse_mode="Markdown")
    else:
        await message.answer("❌ Метеостанция не отвечает. Выходи на свой страх и риск.")

async def handle_ai_chat(message: types.Message):
    if not gemini_client: return
    
    if message.text.startswith('/') or message.text in ["📋 Наряд сегодня", "📅 Наряд завтра", "📊 Расписание на неделю", "📓 Журнал", "🔔 Напомнить сейчас", "⚙️ Настройки"]:
        return

    bot_user = await message.bot.me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    
    if not is_reply_to_bot: return

    await message.bot.send_chat_action(message.chat.id, "typing")

    user_id = message.from_user.id

    if user_id not in USER_CHATS:
        config = genai_types.GenerateContentConfig(
            system_instruction=AI_SYSTEM_PROMPT,
            tools=[{"google_search": {}}] 
        )
        USER_CHATS[user_id] = gemini_client.aio.chats.create(
            model='gemini-2.5-flash', # <--- Ставим твою 3.5 Flash!
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
        # Выводим РЕАЛЬНУЮ причину сбоя прямо в чат!
        await message.reply(f"❌ Ошибка Google API (Чат):\n`{e}`")

# ══════════════════════════════════════════════
#  ФРАЗЫ И ПРОЧИЕ ДАННЫЕ (сохранены без изменений)
# ══════════════════════════════════════════════
SVODKI_HEADERS_BY_DAY = {
    0: ["📋 понедельник. сводки. страдания. погнали", "📋 начало недели, начало боли. твой черёд, дежурный", "📋 пн детектед. наряд активирован. сопротивление бесполезно", "📋 понедельник говорит: подъём. сводки говорят: иди", "📋 новая неделя — те же сводки. ты знал на что шёл"],
    1: ["📋 вторник. скучный день, но сводки яркие", "📋 не понедельник, но тоже не пятница. зато сводки", "📋 вт детектед. уже не начало, ещё не конец. но сводки — вот они", "📋 вторник шепчет: сходи со сводками. ты слышишь?", "📋 второй день недели, первый раз сходишь со сводками"],
    2: ["📋 экватор недели. ты держишься. теперь ещё и сводки", "📋 среда — день силы. особенно если ты несёшь сводки", "📋 горб недели. сводки — твоя ноша сегодня", "📋 среда, половина ада позади. впереди — сводки", "📋 ср детектед. времени на нытьё нет. сводки ждут"],
    3: ["📋 чт — это почти пт. почти. но сводки точно сейчас", "📋 ещё чуть-чуть до пятницы. но сначала — сводки, дружище", "📋 четверг мотивирует. не тебя. сводки — тебя", "📋 предпятничный наряд активирован", "📋 четверг: завтра пятница, сегодня сводки"],
    4: ["📋 пятница! почти выходные. но сначала — сводки, не расслабляйся", "📋 🔥 пятничный наряд. после него — свобода", "📋 последний рывок недели: сводки и на покой", "📋 пт = праздник. но сначала сводки донеси, потом праздник", "📋 финишная прямая недели. сводки — последний барьер"],
    5: ["📋 🥳 сб! выходной! и всё равно сводки лол", "📋 суббота — день отдыха и сводок. в таком порядке нет", "📋 выходной? да. наряд отменён? нет 😂", "📋 сб детектед. сводки не знают что такое выходные", "📋 поздравляем! сегодня суббота и сводки одновременно"],
    6: ["📋 💀 воскресенье. последний день. и сводки", "📋 вс — конец недели и начало следующей боли. зато сводки сейчас", "📋 солнечный воскресный наряд. почти поэзия", "📋 воскресенье говорит: расслабься. сводки говорят: нет", "📋 последний шанс отличиться на этой неделе. сводки. вперёд"],
}
PROC_HEADERS_BY_DAY = {
    0: ["🧹 пн, 22:00 — неделя началась с уборки. классика", "🧹 понедельник не щадит никого. процедурка тебя тоже", "🧹 начало недели, начало страданий с тряпкой", "🧹 пн-детокс: убираешь процедурку — очищаешь карму", "🧹 новая неделя требует чистой процедурки. логично"],
    1: ["🧹 вторник, 22:00. уборка — это вторая работа", "🧹 вт-наряд: тихо, без пафоса, но процедурка ждёт", "🧹 второй день — второе дыхание для уборки", "🧹 вторник скучный? не с уборкой процедурки", "🧹 вт детектед. швабра в руки, погнали"],
    2: ["🧹 среда, середина недели, середина уборки", "🧹 экватор достигнут. процедурка тебя поздравляет", "🧹 срединный наряд. философски. убирай", "🧹 ср 22:00 — самое время почистить процедурку", "🧹 горб недели взят. теперь горб уборки"],
    3: ["🧹 чт 22:00 — завтра пятница, но сначала тряпка", "🧹 предпятничная уборка — традиция сильных духом", "🧹 четверг: последний бой перед выходными. процедурка", "🧹 чт-детектед. финальный наряд рабочей недели", "🧹 завтра пятница — мотивация убраться сегодня"],
    4: ["🧹 🔥 пятница! последняя уборка рабочей недели", "🧹 пятница, 22:00. убери и иди наконец отдыхать", "🧹 финальная процедурка рабочей недели. заслуженно", "🧹 пт-наряд. после — выходные. держись", "🧹 последний рывок: процедурка и свобода"],
    5: ["🧹 🥳 сб 22:00 — уборка в выходной. это уже героизм", "🧹 суббота — день уборки оказывается", "🧹 субботний вечер, процедурка. почти романтика", "🧹 сб-детектед. выходной наряд — особая честь", "🧹 уборка в выходной? ну ты силён, дежурный"],
    6: ["🧹 💀 воскресенье, 22:00 — последняя уборка недели", "🧹 финальный босс недели — процедурка в воскресенье", "🧹 вс-наряд: убери и встречай новую неделю чистым", "🧹 последнее испытание недели. процедурка. справишься", "🧹 воскресный финал. процедурка прощается до следующей недели"],
}

GAMING_HEADERS_SVODKI = [
    "📋 [DOTA] курьер занят, сводки понесёшь сам — обычное дело",
    "📋 First Blood дня: ты первый увидел это сообщение. сводки твои",
    "📋 ward placed 👁 мы видим тебя. иди со сводками",
    "📋 respawn через 5...4...3... успей со сводками",
    "📋 [CS2] bomb has been planted... в твоём расписании. сводки = defuse",
    "📋 rush B! но сначала сводки, потом раш",
    "📋 эко-раунд отменяется, сводки — это must buy",
    "📋 clutch 1v1 vs прокрастинация. сводки — твой ace",
    "📋 ежедневное поручение получено: «Доставка сводок» 📜",
    "📋 [Genshin] Paimon: «эй! эй! сводки ждут, не задерживай Paimon!»",
    "📋 traveler, открыт telepoint к сводкам. resin не тратится 🌟",
    "📋 commission «Сводки» — лёгкая сложность, награда: спокойствие команды",
    "📋 git commit -m 'отнёс сводки' && git push",
    "📋 TODO: сводки [priority: HIGH, deadline: 18:00]",
    "📋 функция donesi_svodki() ещё не вызвана. вызови сейчас",
    "📋 [WARNING] uncommitted сводки detected — push now",
]
GAMING_HEADERS_PROC = [
    "🧹 [DOTA] рошан убит, баунти забран. теперь убей грязь в процедурке",
    "🧹 ночной дозор начался — время вечерней уборки 🌙",
    "🧹 это твой ultimate на сегодня: cast «Уборка Процедурки» 🔥",
    "🧹 backdoor protection не спасёт от швабры. иди убирайся",
    "🧹 [CS2] retake процедурки назначен на 22:00",
    "🧹 smoke вышел, дым рассеялся — пора убирать процедурку, метафорично, но факт",
    "🧹 t-side выиграл день, ct-side выигрывает процедурку. твой ход",
    "🧹 раунд начался: цель — обезвредить грязь до таймаута",
    "🧹 ежедневное поручение: «Чистота домена Процедурка» 📜",
    "🧹 [Genshin] Paimon: «эй! эй! тут грязно! Paimon не пройдёт мимо!»",
    "🧹 domain challenge «Процедурка» открыт. готов к испытанию?",
    "🧹 resin потрачен на день — осталась только процедурка, traveler",
    "🧹 git clean -fd ./процедурка --force",
    "🧹 [CRON 22:00] задача cleanup_procedure() запущена",
    "🧹 build failed: процедурка не убрана. fix and retry",
    "🧹 rm -rf грязь/* && echo 'процедурка чистая'",
]
GAMING_ENDINGS = [
    "gg wp, увидимся на следующем напоминании 🎮",
    "+50 XP за выполнение. левел ап не за горами 🆙",
    "ачивка «надёжный напарник» на расстоянии одного клика 🏆",
    "пуш мид и иди делай наряд, союзники на тебя надеются",
    "это не баг, это фича: наряд существует пока его не выполнят",
    "commit сделан, тесты зелёные — осталось задеплоить себя в процедурку",
    "respawn через 5 сек. время на подготовку — сейчас",
    "критический урон по прокрастинации нанесён ⚔️",
    "ранг «дежурный-про» ждёт подтверждения",
    "сложность: лёгкая. награда: мир в команде 🕊",
    "Paimon одобряет. Paimon редко кого-то одобряет",
    "rampage! но только если сделаешь без задержек 🔥",
]

MOODS = {
    "hyper": {
        "label": "🚀 гиперактивный",
        "endings": ["ПОГНАЛИ!! ты лучший!! всё получится!! 🚀🚀🚀", "ВСЁ БУДЕТ ЧЁТКО!! верим!! не сомневаемся!! 💪💪", "ТЫ СУПЕРЗВЕЗДА НАРЯДОВ!! УДАЧИ!! 🌟🌟🌟", "НАРЯД ПРИНЯТ!! ВЫПОЛНЯЙ!! МЫ С ТОБОЙ!! 🎉🎉"],
    },
    "tired": {
        "label": "😴 уставший",
        "endings": ["ну сделай и ладно. я сам устал уже напоминать", "всё. иди. я полежу пока", "давай. я верю. хотя сил уже нет верить", "сделаешь — хорошо. не сделаешь — ну и бог с ним"],
    },
    "serious": {
        "label": "😤 строгий",
        "endings": ["наряд — это обязательство. выполни его.", "не нужно слов. просто сделай.", "ты дежурный. это всё что нужно знать.", "отступления нет. выполняй."],
    },
    "ironic": {
        "label": "😏 ироничный",
        "endings": ["удачи. хотя тут не нужна удача, просто встань и сделай 😐", "ты конечно можешь не идти. но лучше иди", "наряд сам себя не выполнит. к сожалению для тебя", "ну не знаю, может само рассосётся? нет? тогда иди"],
    },
    "philosophical": {
        "label": "🧘 философский",
        "endings": ["дежурный, как и квант — существует пока его не наблюдают", "уборка — это форма медитации. почти", "каждый наряд приближает тебя к просветлению", "сводки — это метафора ответственности. неси её буквально"],
    },
    "gamer": {
        "label": "🎮 геймерский",
        "endings": ["go go go, таймер тикает как в CS ⏱", "это твой daily quest, easy mode — не ной", "respawn через 5 сек, успей подготовиться", "ranked матч жизни начался, не фидь наряд"],
    },
}

EASTER_EGGS = [
    lambda person, duty: f"📎 *ОФИЦИАЛЬНЫЙ ПРОТОКОЛ №{random.randint(100,999)}/А*\n\nНастоящим уведомляем гражданина *{person}* о необходимости исполнения обязанностей по {'сводкам' if duty=='svodki' else 'процедурке'}. Уклонение от исполнения влечёт последствия.\n\n_Подписано: Комитет по нарядам_",
    lambda person, duty: f"👽 *ВХОДЯЩИЙ СИГНАЛ ИЗ ГЛУБОКОГО КОСМОСА*\n\nЗемляни-ин *{person}*!\nВа-аша планета назначи-ила тебя дежурным по {'сводкам' if duty=='svodki' else 'процедурке'}.\nНе подводи-и Землю-у. Мы наблюдаем-м.\n\n_— цивилизация Зоргов_",
    lambda person, duty: f"🎤 *ДРОП*\n\nйо, {person}, слышишь зов?\n{'сводки' if duty=='svodki' else 'процедурка'} — это не прикол\nвстань с дивана, хватит спать\nвремя наряд выполнять\nхоп-хоп, не тупи\nпогнал, до свидания 🎤",
    lambda person, duty: f"📜 *Ода дежурному*\n\nО, {person}, герой без плаща,\n{'Сводки несёт, душа трепеща' if duty=='svodki' else 'Процедурку убрав, не спеша'}.\nНаряд как судьба — не избегнуть его,\nИди же скорее, не медли ничё.\n\n_— анонимный поэт нарядов_",
    lambda person, duty: f"⚠️ *ОБНАРУЖЕН БАГ В МАТРИЦЕ*\n\n```\nERROR: duty_avoidance detected\nUSER: {person}\nTASK: {'svodki' if duty=='svodki' else 'procedura'}\nSTATUS: PENDING\n```\n\nПерезагрузка невозможна. Выполни наряд для продолжения реальности.",
    lambda person, duty: f"🎮 *DOTA 2 — MATCH RESULT*\n━━━━━━━━━━━━━━━━━━━━\nRadiant 34 : Dire 21\n🏆 MVP матча: {PERSON_EMOJI.get(person,'👤')} *{person}*\n📊 KDA: 14/2/9 · GPM: 720\n🔥 Killing Spree x5\n\nGG WP! теперь твой ход — {'сводки' if duty=='svodki' else 'процедурка'} 🫡",
    lambda person, duty: f"💣 *CS2 — ROUND_OVER*\n━━━━━━━━━━━━━━━━━━━━\nCounter-Terrorists Win!\n⭐ MVP: {PERSON_EMOJI.get(person,'👤')} *{person}*\n🎯 3 Headshot Kills · ACE\n💰 +3400$ за раунд\n\nследующий раунд: {'сводки' if duty=='svodki' else 'процедурка'}. defuse it 🔧",
    lambda person, duty: f"🌟 *GENSHIN IMPACT — DAILY COMMISSION*\n━━━━━━━━━━━━━━━━━━━━\nTraveler: {PERSON_EMOJI.get(person,'👤')} *{person}*\n📜 Поручение: {'«Доставка сводок»' if duty=='svodki' else '«Уборка домена Процедурка»'}\n🎁 Награда: 60 Primogems + Mora\n\nPaimon: «эй! эй! {person}! не забудь забрать награду!» 🍞",
    lambda person, duty: f"💻 *TERMINAL*\n```\n$ ssh duty@server\n$ whoami\n{person}\n$ cat /etc/duty/today.conf\nTASK={'svodki' if duty=='svodki' else 'procedura'}\nSTATUS=pending\nPRIORITY=high\nDEADLINE={'18:00' if duty=='svodki' else '22:00'}\n$ _\n```",
]

DONE_REPLIES = ["✅ красава! записал, можешь выдыхать 🛋", "✅ зафиксировал. команда тобой гордится (наверное)", "✅ чётко отработал, уважение 🫡", "✅ выполнено. ты молодец, иди отдыхай", "✅ записал в журнал. так держать, братан 💪", "✅ сделано! статистика не врёт — ты топ", "✅ зачёт. командная работа — это святое 🤝", "✅ принято! ты буквально держишь этот коллектив 🙌"]
DONE_REPLIES_GAMING = ["✅ ACE! сделано чисто, без вопросов 🎯", "✅ +50 XP начислено, левел ап скоро 🆙", "✅ commit successful — ветка наряда смержена в main 🌿", "✅ quest completed! получено: уважение команды 🏆", "✅ killing spree x3 — три дня подряд без пропусков? 🔥", "✅ build passed ✅ все тесты зелёные", "✅ First Blood дня записан на твой счёт 🩸", "✅ Paimon одобряет! редкий случай 🌟"]

RETRY_PREFIX = ["⏰ эй, ты там живой? повторяю:", "⏰ второй звонок, не игнорируй:", "⏰ окей окей, напоминаю ещё раз:", "⏰ хм, всё ещё не сделано? ладно:", "⏰ я просто делаю своё дело. напоминаю:", "⏰ настойчивость — это я. вот снова:"]
RETRY_PREFIX_GAMING = ["⏰ [RESPAWN] таймер истёк, возвращаю в игру:", "⏰ retry — round 2. вот снова:", "⏰ [RECONNECTING...] напоминание восстановлено:", "⏰ ping подскочил, повторяю пакет данных:"]

PERSONAL_SVODKI = ["эй {name} 👋 сегодня твои сводки. не забудь, ладно?", "yo {name}, наряд по сводкам — это ты сегодня. погнали 📋", "{name}, привет. сводки сами себя не потащат, ты понял", "внимание, {name}! сводки сегодня на тебе. удачи 🫡", "псс, {name} 👀 напоминаю тихонько — сводки твои сегодня", "{name} сегодня несёт сводки. это ты. это не я. это ты 😐"]
PERSONAL_SVODKI_GAMING = ["{name}, твой quest активен: «Donesi Svodki». награда — спокойствие 🎮", "respawn, {name}! пора нести сводки, таймер пошёл ⏱", "{name}, commission «Сводки» открыта. Paimon ждёт у выхода 🌟", "git push {name} → сводки. без force, по-нормальному 👀"]
PERSONAL_PROC = ["эй {name} 🧹 вечером процедурка на тебе. не забей", "yo {name}, уборка процедурки сегодня твоя. справишься", "{name}, привет. процедурка ждёт тебя в 22:00 👀", "внимание, {name}! процедурка сегодня твоя. это судьба", "псс, {name} — убраться в процедурке это ты сегодня 🧹", "{name}, 22:00 приближается. процедурка уже скучает по тебе"]
PERSONAL_PROC_GAMING = ["{name}, твой ultimate — уборка процедурки. cast его 🧹", "domain «Процедурка» доступен для {name}. удачи в challenge", "{name}, cron-задача cleanup() назначена на тебя сегодня", "rush B, {name}! но в процедурку, и со шваброй 🧹"]

MONDAY_INTROS = ["☀️ доброе утро, страдальцы. новая неделя — новые наряды:", "☀️ понедельник, все мы немного умерли. но наряды никто не отменял:", "☀️ йоу, начинаем неделю. вот кто пашет:", "☀️ пн 9:00 — самое то узнать кому досталось на этой неделе:", "☀️ новая неделя ворвалась без предупреждения. вот расписание:", "☀️ понедельник не спросил хотите ли вы. вот кто дежурит:"]
SUNDAY_INTROS = ["🏆 итоги недели. кто вообще старался:", "🏆 неделя прошла, подводим итоги нарядов:", "🏆 воскресенье — время чтить героев:", "🏆 статистика недели. цифры не врут:", "🏆 занавес опускается. подсчитываем потери и победы:", "🏆 всё, конец недели. смотрим кто реально работал:"]

# ══════════════════════════════════════════════
#  ХРАНИЛИЩЕ ДАННЫХ
# ══════════════════════════════════════════════
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    data = {
        "start_date": date.today().isoformat(),
        "svodki_start_index": 0,
        "procedura_start_index": 0,
        "chat_id": CHAT_ID,
        "reminders_enabled": True,
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

# ══════════════════════════════════════════════
#  НАСТРОЕНИЕ ДНЯ
# ══════════════════════════════════════════════
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

# ══════════════════════════════════════════════
#  ЛОГИКА НАРЯДОВ
# ══════════════════════════════════════════════
def get_svodki_idx(target_date):
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return (data["svodki_start_index"] + (target_date - start).days) % len(SVODKI_LIST)

def get_svodki_person(target_date):
    return SVODKI_LIST[get_svodki_idx(target_date)]

def get_procedura_idx(target_date):
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return (data["procedura_start_index"] + (target_date - start).days // 2) % len(PROCEDURA_LIST)

def get_procedura_person(target_date):
    return PROCEDURA_LIST[get_procedura_idx(target_date)]

def get_duty_day_number(target_date):
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return ((target_date - start).days % 2) + 1

def days_until_svodki(name, from_date):
    for i in range(1, len(SVODKI_LIST) + 1):
        if get_svodki_person(from_date + timedelta(days=i)) == name:
            return i
    return len(SVODKI_LIST)

def days_until_procedura(name, from_date):
    for i in range(1, len(PROCEDURA_LIST) * 2 + 1):
        if get_procedura_person(from_date + timedelta(days=i)) == name:
            return i
    return len(PROCEDURA_LIST) * 2

def weekday_divider(d):
    _, _, sym_l, sym_r = WEEKDAY_STYLE[d.weekday()]
    return f"{sym_l}━━━━━━━━━━━━━━━━{sym_r}"

def person_tag(name):
    return f"{PERSON_EMOJI.get(name, '👤')} *{name}*"

def progress_bar(current, total):
    filled = round((current / total) * 8)
    return f"`[{'▓'*filled}{'░'*(8-filled)}]` {current}/{total}"

def svodki_num_label(name):
    return f"Дежурный №{SVODKI_LIST.index(name)+1} по сводкам"

def proc_num_label(name):
    return f"Дежурный №{PROCEDURA_LIST.index(name)+1} по процедурке"

def next_person_svodki(name):
    return SVODKI_LIST[(SVODKI_LIST.index(name) + 1) % len(SVODKI_LIST)]

def next_person_proc(name):
    return PROCEDURA_LIST[(PROCEDURA_LIST.index(name) + 1) % len(PROCEDURA_LIST)]

# ══════════════════════════════════════════════
#  ПОСТРОЕНИЕ СООБЩЕНИЙ (для команд и рассылок)
# ══════════════════════════════════════════════
def build_duty_message(target_date, label):
    svodki = get_svodki_person(target_date)
    proc = get_procedura_person(target_date)
    proc_day = get_duty_day_number(target_date)
    div = weekday_divider(target_date)
    day_name, icon, _, _ = WEEKDAY_STYLE[target_date.weekday()]
    date_str = target_date.strftime("%d.%m.%Y")
    mood_lbl = get_mood_label()
    weekend_note = "\n🥳 *выходной, но наряды никто не отменял*" if target_date.weekday() >= 5 else ""
    return "\n".join([
        f"{icon} *{label} — {day_name}, {date_str}*{weekend_note}",
        f"_настроение бота: {mood_lbl}_",
        div, "",
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
        lines.append(f"📅 *{lbl}*  {sv_ok}{PERSON_EMOJI.get(sv,'👤')}{sv}  |  {pr_ok}{PERSON_EMOJI.get(pr,'👤')}{pr}")
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

# ══════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="toggle_reminders")],
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

# ══════════════════════════════════════════════
#  АВТОУДАЛЕНИЕ
# ══════════════════════════════════════════════
async def schedule_delete(bot, chat_id, msg_id, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await bot.delete_message(chat_id, msg_id)
    except:
        pass

async def auto_delete_later(bot, chat_id, msg_id, seconds=30):
    asyncio.create_task(schedule_delete(bot, chat_id, msg_id, seconds))

# ══════════════════════════════════════════════
#  ЗАКРЕПЛЁННОЕ
# ══════════════════════════════════════════════
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
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        return
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
        try:
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except:
            pass
        data["pinned_msg_id"] = sent.message_id
        save_data(data)
    except Exception as e:
        logger.error(f"ошибка создания закреплённого: {e}")

# ══════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════
class AdminStates(StatesGroup):
    waiting_chat_id = State()
    waiting_register = State()

# ══════════════════════════════════════════════
#  ОТПРАВКА НАПОМИНАНИЙ (С КАРТИНКОЙ)
# ══════════════════════════════════════════════
async def _send_personal(bot, person, duty_type):
    data = load_data()
    uid = data.get("personal_ids", {}).get(person)
    if not uid: return
    tmpl_pool = (PERSONAL_SVODKI + PERSONAL_SVODKI_GAMING) if duty_type == "svodki" else (PERSONAL_PROC + PERSONAL_PROC_GAMING)
    try:
        await bot.send_message(uid, random.choice(tmpl_pool).format(name=person))
    except Exception as e:
        logger.warning(f"личное сообщение {person}: {e}")

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
        # Генерируем фразу через ИИ, если это не повторное напоминание
        header = None
        if not retry:
            header = await get_ai_header(person, duty_type)
        # Если ИИ сломался, ключа нет или это повтор — берём из старых списков
        if not header:
            header = random.choice(retry_pool) if retry else random.choice(headers_pool)

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
        # Генерируем фразу через ИИ, если это не повторное напоминание
        header = None
        if not retry:
            header = await get_ai_header(person, duty_type)
        # Если ИИ сломался, ключа нет или это повтор — берём из старых списков
        if not header:
            header = random.choice(retry_pool) if retry else random.choice(headers_pool)

        ending = get_ending()
        prev_key = "last_proc_msg_id"

    # Пасхалка – текст
    if easter:
        msg_text = random.choice(EASTER_EGGS)(person, duty_type)
        prev_id = data.get(prev_key)
        if prev_id:
            try: await bot.delete_message(chat_id, prev_id)
            except: pass
        sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown",
                                      reply_markup=confirmation_keyboard(duty_type))
        data[prev_key] = sent.message_id
        save_data(data)
        await update_pinned_message(bot)
        await _send_personal(bot, person, duty_type)
            # ☁️ АВТОМАТИЧЕСКАЯ ПОГОДА ДЛЯ СВОДОК
    if duty_type == "svodki" and not retry:
        weather_text = await get_weather_advice()
        if weather_text:
            await bot.send_message(chat_id, f"☁️ *Обстановка на улице:*\n{weather_text}", parse_mode="Markdown")
        return

    # Генерация картинки
    date_label = today.strftime("%d.%m.%Y") + " • " + WEEKDAY_STYLE[wd][0]
    theme_key = random.choice(THEME_KEYS)
    img_path = os.path.join(TEMP_DIR, f"card_{duty_type}_{int(time.time())}.jpg")

    try:
        logger.info(f"Генерация карточки для {person} ({duty_type})")
        make_reminder_card(
            duty_type=duty_type,
            person=person,
            position_label=position_label,
            time_label=time_label,
            header_text=header,
            ending_text=ending,
            date_label=date_label,
            mood_label=mood_label,
            proc_day=proc_day if duty_type == "proc" else 1,
            next_person=next_n,
            next_days=next_d,
            total_duties=total,
            theme_key=theme_key,
            output_path=img_path
        )
        if not os.path.exists(img_path):
            raise FileNotFoundError(f"Файл {img_path} не создан")
        logger.info(f"Карточка создана: {img_path} (размер {os.path.getsize(img_path)} байт)")
    except Exception as e:
        logger.error(f"Ошибка генерации карточки: {e}", exc_info=True)
        # Отправляем текст
        msg_text = f"*{header}*\n\n👤 {person}\n📋 {position_label}\n⏰ {time_label}\n📊 нарядов всего: {total}\n⏭ следующий: {next_n} ({next_d}д)\n\n_{ending}_"
        prev_id = data.get(prev_key)
        if prev_id:
            try: await bot.delete_message(chat_id, prev_id)
            except: pass
        sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown",
                                      reply_markup=confirmation_keyboard(duty_type))
        data[prev_key] = sent.message_id
        save_data(data)
        await update_pinned_message(bot)
        await _send_personal(bot, person, duty_type)
        return

    # Отправляем фото
    caption = f"🔔 Напоминание по {duty_type.upper()} для *{person}*"
    prev_id = data.get(prev_key)
    if prev_id:
        try: await bot.delete_message(chat_id, prev_id)
        except: pass

    try:
        photo = FSInputFile(img_path)
        sent = await bot.send_photo(
            chat_id,
            photo=photo,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=confirmation_keyboard(duty_type)
        )
        data[prev_key] = sent.message_id
        save_data(data)
        logger.info(f"Фото отправлено для {person} ({duty_type})")
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}", exc_info=True)
        # Фолбэк текст
        msg_text = f"*{header}*\n\n👤 {person}\n📋 {position_label}\n⏰ {time_label}\n\n_{ending}_"
        sent = await bot.send_message(chat_id, msg_text, parse_mode="Markdown",
                                      reply_markup=confirmation_keyboard(duty_type))
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
    except Exception as e:
        logger.error(f"понедельничная рассылка: {e}")

async def send_sunday_summary(bot):
    data = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try:
        await bot.send_message(chat_id, build_sunday_summary(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"воскресный итог: {e}")

async def daily_pinned_update(bot):
    await update_pinned_message(bot)

# ══════════════════════════════════════════════
#  ХЕНДЛЕРЫ
# ══════════════════════════════════════════════
async def cmd_start(message: types.Message):
    # Удаляем команду пользователя
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    
    start_text = (
        "👋 *Йоу! Я твой кибер-надзиратель и ИИ-бро в одном лице.*\n\n"
        "*Что я умею:*\n"
        "🧹 *Следить за нарядами:* сводки в 18:00, процедурка в 22:00.\n"
        "🤖 *Быть умным:* сделай реплай на моё сообщение, и я отвечу на любой вопрос (от сборки ПК до меты в Доте).\n"
        "🃏 *Делать мемы:* скинь фотку с подписью `/meme`, и я наложу смешной текст.\n"
        "🎨 *Рисовать:* напиши `/img <что нарисовать>`, и я сгенерирую картинку.\n\n"
        "Я всё вижу, всё помню. Погнали! 👇"
    )
    
    sent = await message.answer(start_text, parse_mode="Markdown", reply_markup=main_keyboard())
    
    # Удаляем стартовое сообщение бота через 60 секунд, чтобы не висело в чате
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
    chat_txt = data.get("chat_id", "не настроен")
    sv_now = get_svodki_person(date.today())
    pr_now = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em = PERSON_EMOJI.get(sv_now, "👤")
    pr_em = PERSON_EMOJI.get(pr_now, "👤")
    mood_lbl = get_mood_label()
    await message.answer(f"⚙️ *настройки*\n━━━━━━━━━━━━━━━━━━━━\n📍 чат: `{chat_txt}`\n🔔 напоминания: {enabled}\n🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n🪪 зарегистрировано: *{reg_count}* чел.\n🎭 настроение бота: *{mood_lbl}*\n━━━━━━━━━━━━━━━━━━━━", parse_mode="Markdown", reply_markup=settings_keyboard(data))

async def cmd_meme(message: types.Message):
    if not gemini_client: return

    # Удаляем команду юзера
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    photo = None
    if message.photo:
        photo = message.photo[-1]
    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]

    if not photo:
        sent = await message.answer("🖼 Отправь фотку с подписью `/meme` или сделай реплай `/meme` на любую фотку в чате.", parse_mode="Markdown")
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
        return

    status_msg = await message.reply("⬛️ Делаю из этого демотиватор...")

    try:
        # 1. Скачиваем фото 
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        img = Image.open(downloaded_file).convert("RGB")
        
        # Приводим картинку к единой ширине 800px, чтобы шрифты всегда смотрелись пропорционально
        target_width = 800
        if img.width != target_width:
            ratio = target_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((target_width, new_height), Image.Resampling.LANCZOS)
        
        # 2. Просим Gemini придумать текст для демотиватора
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
        
        # Парсим текст
        meme_text = response.text.replace('"', '').replace('\n', '').strip()
        if "|" in meme_text:
            top_text, bottom_text = meme_text.split("|", 1)
        else:
            top_text = meme_text
            bottom_text = ""
            
        top_text = top_text.strip().upper()  # Заголовок всегда капсом
        bottom_text = bottom_text.strip()

        # 3. Настраиваем шрифты
        font_title_path = os.path.join(BASE_DIR, "fonts", "DejaVuSans-Bold.ttf")
        font_sub_path = os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf")
        
        try:
            font_title = ImageFont.truetype(font_title_path, 60)
            font_sub = ImageFont.truetype(font_sub_path, 30)
        except IOError:
            font_title = ImageFont.load_default()
            font_sub = ImageFont.load_default()

        # Разбиваем текст на строки, если он слишком длинный
        title_lines = textwrap.wrap(top_text, width=25)
        sub_lines = textwrap.wrap(bottom_text, width=55)

        # Вычисляем высоту текста, чтобы понять, какого размера делать черный фон
        dummy_draw = ImageDraw.Draw(Image.new("RGB", (1,1)))
        
        text_area_height = 40 # Верхний отступ до текста
        for line in title_lines:
            bbox = font_title.getbbox(line)
            text_area_height += (bbox[3] - bbox[1]) + 10
            
        text_area_height += 20 # Расстояние между заголовком и подписью
        
        for line in sub_lines:
            bbox = font_sub.getbbox(line)
            text_area_height += (bbox[3] - bbox[1]) + 10
            
        text_area_height += 50 # Нижний отступ (padding)

        # 4. Создаем черное полотно (Демотиватор)
        border_x = 70
        border_top = 70
        
        bg_width = img.width + border_x * 2
        bg_height = img.height + border_top + text_area_height
        
        background = Image.new('RGB', (bg_width, bg_height), color='black')
        
        # Вставляем фотку
        background.paste(img, (border_x, border_top))
        
        # Рисуем белую рамку вокруг фотки (классика демотиваторов)
        draw = ImageDraw.Draw(background)
        draw.rectangle(
            [border_x - 4, border_top - 4, border_x + img.width + 3, border_top + img.height + 3],
            outline='white', width=3
        )

        # 5. Печатаем текст
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

        # 6. Отправляем в чат
        out_bytes = io.BytesIO()
        background.save(out_bytes, format="JPEG", quality=95)
        out_bytes.seek(0)
        
        photo_out = BufferedInputFile(out_bytes.read(), filename="demotivator.jpg")
        await message.answer_photo(photo_out)
        
    except Exception as e:
        logger.error(f"Ошибка создания демотиватора: {e}")
        await message.reply(f"❌ Нейроны перегрелись: {e}")
    finally:
        try:
            await status_msg.delete()
        except:
            pass


async def handle_photo(message: types.Message):
    if not gemini_client: return

    bot_user = await message.bot.me()
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    
    # Чтобы бот не спамил в общих чатах, реагируем только если:
    # 1. Это реплай на сообщение бота
    # 2. К фотке добавили текстовую подпись
    # 3. Это личная переписка с ботом
    if not (is_reply or message.caption or message.chat.type == 'private'):
        return

    # Показываем статус, что бот "печатает"
    await message.bot.send_chat_action(message.chat.id, "typing")
    status_msg = await message.reply("👀 Сканирую пиксели...")

    try:
        # Телеграм присылает несколько размеров фото, берем самое большое (последнее в списке)
        photo = message.photo[-1]
        
        # Скачиваем файл прямо в оперативную память, не засоряя жесткий диск хостинга
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        
        # Превращаем байты в объект картинки, который понимает Google
        image = Image.open(downloaded_file)

        # Берем текст пользователя или ставим дефолтный вопрос
        user_text = message.caption if message.caption else "Что ты видишь на этой картинке? Опиши коротко, в своем саркастичном и гиковском стиле."
        
        # Системная инструкция, чтобы он не выпадал из роли
        system_prompt = (
            "Ты — дерзкий, ироничный ИИ-надзиратель. "
            "Проанализируй картинку и ответь пользователю. "
            "Сохраняй саркастичный, геймерский тон. Если скинули фотку убранной комнаты — оцени качество уборки как строгий прапорщик."
        )
        
        # Отправляем в Gemini картинку и текст ОДНОВРЕМЕННО
        response = await gemini_client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=[image, user_text],
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt
            )
        )

        await message.reply(response.text)
        
    except Exception as e:
        logger.error(f"Ошибка анализа фото Gemini: {e}")
        await message.reply("❌ Мои оптические сенсоры сбоят. Либо картинка слишком шакальная, либо сервера гугла лежат.")
    finally:
        # Убираем сообщение "Сканирую пиксели..."
        try:
            await status_msg.delete()
        except:
            pass


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
        try:
            await status_msg.delete()
        except:
            pass

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
        # Внимание: если гугл выдаст 404, проверь точное название модели в документации
        # Возможно, оно пишется как 'imagen-4.0-ultra-generate-001' или похоже
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
        try:
            await status_msg.delete()
        except:
            pass


# ── НАСТРОЙКИ ──
async def callback_toggle_reminders(callback: types.CallbackQuery):
    data = load_data()
    data["reminders_enabled"] = not data.get("reminders_enabled", True)
    save_data(data)
    status = "✅ включены" if data["reminders_enabled"] else "❌ выключены"
    await callback.answer(f"напоминания {status}", show_alert=True)
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

# ── РЕГИСТРАЦИЯ ──
async def callback_register_personal(callback: types.CallbackQuery, state: FSMContext):
    names = " / ".join(SVODKI_LIST)
    sent = await callback.message.answer("🪪 *регистрация личных уведомлений*\n\nнапиши своё имя *точно как в списке*:\n{names}\n\nбуду писать тебе лично в день дежурства 👀", parse_mode="Markdown")
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

# ── ВЫБОР ДЕЖУРНОГО ──
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
    chat_txt = data.get("chat_id", "не настроен")
    sv_now = get_svodki_person(date.today())
    pr_now = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em = PERSON_EMOJI.get(sv_now, "👤")
    pr_em = PERSON_EMOJI.get(pr_now, "👤")
    mood_lbl = get_mood_label()
    await callback.message.edit_text(f"⚙️ *настройки*\n━━━━━━━━━━━━━━━━━━━━\n📍 чат: `{chat_txt}`\n🔔 напоминания: {enabled}\n🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n🪪 зарегистрировано: *{reg_count}* чел.\n🎭 настроение бота: *{mood_lbl}*\n━━━━━━━━━━━━━━━━━━━━", parse_mode="Markdown", reply_markup=settings_keyboard(data))
    await callback.answer()

async def callback_back_main(callback: types.CallbackQuery):
    await callback.message.answer("🏠 главное меню", reply_markup=main_keyboard())
    await callback.answer()

# ── ПОДТВЕРЖДЕНИЕ ──
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

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
def _startup_selftest():
    """Генерирует тестовую карточку прямо на старте, чтобы в логах сразу
    было видно, работает ли Pillow/шрифты на этом хостинге, не дожидаясь
    18:00 или 22:00."""
    test_path = os.path.join(TEMP_DIR, "_selftest.jpg")
    try:
        make_reminder_card(
            duty_type="svodki",
            person="Тест",
            position_label="самопроверка при старте",
            time_label="00:00",
            header_text="проверка генератора картинок",
            ending_text="всё ок",
            date_label=datetime.now(TIMEZONE).strftime("%d.%m.%Y"),
            mood_label="проверка",
            theme_key=random.choice(THEME_KEYS),
            output_path=test_path,
        )
        size = os.path.getsize(test_path)
        logger.info(f"✅ САМОПРОВЕРКА КАРТИНОК ПРОЙДЕНА: {test_path} ({size} байт)")
    except Exception:
        logger.error("❌ САМОПРОВЕРКА КАРТИНОК НЕ ПРОЙДЕНА:\n" + traceback.format_exc())
    finally:
        try:
            os.remove(test_path)
        except Exception:
            pass

async def main():
    logger.info("🟡 инициализация бота...")
    _startup_selftest()

    bot = Bot(token=TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # 1. Сначала регистрируем ВСЕ команды (через слэш)
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_getchatid, Command("chatid"))
    dp.message.register(cmd_pollinations, Command("poll"))
    dp.message.register(cmd_imagen, Command("imagen"))
    dp.message.register(cmd_meme, Command("meme"))
    # ДОБАВЛЯЕМ ПОГОДУ:
    dp.message.register(cmd_weather, Command("pogoda"))

    # 2. Затем регистрируем все текстовые кнопки меню
    dp.message.register(cmd_today, F.text == "📋 Наряд сегодня")
    dp.message.register(cmd_tomorrow, F.text == "📅 Наряд завтра")
    dp.message.register(cmd_week, F.text == "📊 Расписание на неделю")
    dp.message.register(cmd_log, F.text == "📓 Журнал")
    dp.message.register(cmd_remind_now, F.text == "🔔 Напомнить сейчас")
    dp.message.register(cmd_settings, F.text == "⚙️ Настройки")

    # 3. Регистрируем состояния (FSM) для ввода данных
    dp.message.register(process_chat_id, AdminStates.waiting_chat_id)
    dp.message.register(process_register, AdminStates.waiting_register)

    # 4. Регистрируем все callback-кнопки (инлайн)
    dp.callback_query.register(callback_done, F.data.startswith("done:"))
    dp.callback_query.register(callback_retry, F.data.startswith("retry:"))
    dp.callback_query.register(callback_toggle_reminders, F.data == "toggle_reminders")
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

    # 5. Регистрируем зрение для фоток
    dp.message.register(handle_photo, F.photo)

    # 6. В САМОМ КОНЦЕ регистрируем ИИ-болталку (она ловит любой оставшийся текст)
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
        # Если что-то всё-таки уронило бота на старте — печатаем полный
        # traceback и в logger, и напрямую в stdout/stderr, чтобы это
        # гарантированно попало в панель логов хостинга, какой бы она ни была.
        tb = traceback.format_exc()
        logger.critical("🔴 БОТ УПАЛ С НЕОБРАБОТАННОЙ ОШИБКОЙ:\n" + tb)
        print("🔴 БОТ УПАЛ С НЕОБРАБОТАННОЙ ОШИБКОЙ:\n" + tb, file=sys.stderr, flush=True)
        sys.exit(1)
