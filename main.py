import asyncio
import logging
import json
import os
import random
import tempfile
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram.types import FSInputFile
from card_generator import make_reminder_card, make_daily_card
import pytz

    
# ══════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════
TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID   = os.getenv("CHAT_ID",   "YOUR_CHAT_ID_HERE")
DATA_FILE = "duty_data.json"
TIMEZONE  = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#  СПИСКИ ДЕЖУРНЫХ
# ══════════════════════════════════════════════
SVODKI_LIST    = ["Олег", "Максим", "Игорь", "Илья", "Глеба", "Слава", "Ильнар"]
PROCEDURA_LIST = ["Илья", "Слава", "Игорь", "Глеба", "Ильнар"]

PERSON_EMOJI = {
 "Олег": "🐻", "Максим": "🦁", "Игорь": "🐺",
 "Илья": "🦅", "Глеба": "🐯", "Слава": "🦝", "Ильнар": "🐉",
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
#  ✦ УЛУЧШЕНИЕ 1 — 50+ ФРАЗ ПО ДНЯМ НЕДЕЛИ
#  ✦ УЛУЧШЕНИЕ 2 — НАСТРОЕНИЕ ДНЯ
# ══════════════════════════════════════════════

# Заголовки по дням недели (7 дней × несколько вариантов)
SVODKI_HEADERS_BY_DAY = {
    0: [  # понедельник — строгий, апокалиптический
        "📋 понедельник. сводки. страдания. погнали",
        "📋 начало недели, начало боли. твой черёд, дежурный",
        "📋 пн детектед. наряд активирован. сопротивление бесполезно",
        "📋 понедельник говорит: подъём. сводки говорят: иди",
        "📋 новая неделя — те же сводки. ты знал на что шёл",
    ],
    1: [  # вторник — ироничный
        "📋 вторник. скучный день, но сводки яркие",
        "📋 не понедельник, но тоже не пятница. зато сводки",
        "📋 вт детектед. уже не начало, ещё не конец. но сводки — вот они",
        "📋 вторник шепчет: сходи со сводками. ты слышишь?",
        "📋 второй день недели, первый раз сходишь со сводками",
    ],
    2: [  # среда — усталый
        "📋 экватор недели. ты держишься. теперь ещё и сводки",
        "📋 среда — день силы. особенно если ты несёшь сводки",
        "📋 горб недели. сводки — твоя ноша сегодня",
        "📋 среда, половина ада позади. впереди — сводки",
        "📋 ср детектед. времени на нытьё нет. сводки ждут",
    ],
    3: [  # четверг — почти пятница
        "📋 чт — это почти пт. почти. но сводки точно сейчас",
        "📋 ещё чуть-чуть до пятницы. но сначала — сводки, дружище",
        "📋 четверг мотивирует. не тебя. сводки — тебя",
        "📋 предпятничный наряд активирован",
        "📋 четверг: завтра пятница, сегодня сводки",
    ],
    4: [  # пятница — расслабленный
        "📋 пятница! почти выходные. но сначала — сводки, не расслабляйся",
        "📋 🔥 пятничный наряд. после него — свобода",
        "📋 последний рывок недели: сводки и на покой",
        "📋 пт = праздник. но сначала сводки донеси, потом праздник",
        "📋 финишная прямая недели. сводки — последний барьер",
    ],
    5: [  # суббота — весёлый
        "📋 🥳 сб! выходной! и всё равно сводки лол",
        "📋 суббота — день отдыха и сводок. в таком порядке нет",
        "📋 выходной? да. наряд отменён? нет 😂",
        "📋 сб детектед. сводки не знают что такое выходные",
        "📋 поздравляем! сегодня суббота и сводки одновременно",
    ],
    6: [  # воскресенье — апокалипсис
        "📋 💀 воскресенье. последний день. и сводки",
        "📋 вс — конец недели и начало следующей боли. зато сводки сейчас",
        "📋 солнечный воскресный наряд. почти поэзия",
        "📋 воскресенье говорит: расслабься. сводки говорят: нет",
        "📋 последний шанс отличиться на этой неделе. сводки. вперёд",
    ],
}

PROC_HEADERS_BY_DAY = {
    0: [
        "🧹 пн, 22:00 — неделя началась с уборки. классика",
        "🧹 понедельник не щадит никого. процедурка тебя тоже",
        "🧹 начало недели, начало страданий с тряпкой",
        "🧹 пн-детокс: убираешь процедурку — очищаешь карму",
        "🧹 новая неделя требует чистой процедурки. логично",
    ],
    1: [
        "🧹 вторник, 22:00. уборка — это вторая работа",
        "🧹 вт-наряд: тихо, без пафоса, но процедурка ждёт",
        "🧹 второй день — второе дыхание для уборки",
        "🧹 вторник скучный? не с уборкой процедурки",
        "🧹 вт детектед. швабра в руки, погнали",
    ],
    2: [
        "🧹 среда, середина недели, середина уборки",
        "🧹 экватор достигнут. процедурка тебя поздравляет",
        "🧹 срединный наряд. философски. убирай",
        "🧹 ср 22:00 — самое время почистить процедурку",
        "🧹 горб недели взят. теперь горб уборки",
    ],
    3: [
        "🧹 чт 22:00 — завтра пятница, но сначала тряпка",
        "🧹 предпятничная уборка — традиция сильных духом",
        "🧹 четверг: последний бой перед выходными. процедурка",
        "🧹 чт-детектед. финальный наряд рабочей недели",
        "🧹 завтра пятница — мотивация убраться сегодня",
    ],
    4: [
        "🧹 🔥 пятница! последняя уборка рабочей недели",
        "🧹 пятница, 22:00. убери и иди наконец отдыхать",
        "🧹 финальная процедурка рабочей недели. заслуженно",
        "🧹 пт-наряд. после — выходные. держись",
        "🧹 последний рывок: процедурка и свобода",
    ],
    5: [
        "🧹 🥳 сб 22:00 — уборка в выходной. это уже героизм",
        "🧹 суббота — день уборки оказывается",
        "🧹 субботний вечер, процедурка. почти романтика",
        "🧹 сб-детектед. выходной наряд — особая честь",
        "🧹 уборка в выходной? ну ты силён, дежурный",
    ],
    6: [
        "🧹 💀 воскресенье, 22:00 — последняя уборка недели",
        "🧹 финальный босс недели — процедурка в воскресенье",
        "🧹 вс-наряд: убери и встречай новую неделю чистым",
        "🧹 последнее испытание недели. процедурка. справишься",
        "🧹 воскресный финал. процедурка прощается до следующей недели",
    ],
}

# ══════════════════════════════════════════════
#  ✦ УЛУЧШЕНИЕ 2 — НАСТРОЕНИЕ ДНЯ
# ══════════════════════════════════════════════
# Хранится в data["daily_mood"] = {"date": "2024-06-01", "mood": "tired"}
MOODS = {
    "hyper": {
        "label": "🚀 гиперактивный",
        "endings": [
            "ПОГНАЛИ!! ты лучший!! всё получится!! 🚀🚀🚀",
            "ВСЁ БУДЕТ ЧЁТКО!! верим!! не сомневаемся!! 💪💪",
            "ТЫ СУПЕРЗВЕЗДА НАРЯДОВ!! УДАЧИ!! 🌟🌟🌟",
            "НАРЯД ПРИНЯТ!! ВЫПОЛНЯЙ!! МЫ С ТОБОЙ!! 🎉🎉",
        ],
    },
    "tired": {
        "label": "😴 уставший",
        "endings": [
            "ну сделай и ладно. я сам устал уже напоминать",
            "всё. иди. я полежу пока",
            "давай. я верю. хотя сил уже нет верить",
            "сделаешь — хорошо. не сделаешь — ну и бог с ним",
        ],
    },
    "serious": {
        "label": "😤 строгий",
        "endings": [
            "наряд — это обязательство. выполни его.",
            "не нужно слов. просто сделай.",
            "ты дежурный. это всё что нужно знать.",
            "отступления нет. выполняй.",
        ],
    },
    "ironic": {
        "label": "😏 ироничный",
        "endings": [
            "удачи. хотя тут не нужна удача, просто встань и сделай 😐",
            "ты конечно можешь не идти. но лучше иди",
            "наряд сам себя не выполнит. к сожалению для тебя",
            "ну не знаю, может само рассосётся? нет? тогда иди",
        ],
    },
    "philosophical": {
        "label": "🧘 философский",
        "endings": [
            "дежурный, как и квант — существует пока его не наблюдают",
            "уборка — это форма медитации. почти",
            "каждый наряд приближает тебя к просветлению",
            "сводки — это метафора ответственности. неси её буквально",
        ],
    },
}

# ══════════════════════════════════════════════
#  ✦ УЛУЧШЕНИЕ 3 — ПАСХАЛКИ (5% шанс)
# ══════════════════════════════════════════════
EASTER_EGGS = [
    # официальный протокол
    lambda person, duty: (
        f"📎 *ОФИЦИАЛЬНЫЙ ПРОТОКОЛ №{random.randint(100,999)}/А*\n\n"
        f"Настоящим уведомляем гражданина *{person}* о необходимости "
        f"исполнения обязанностей по {'сводкам' if duty=='svodki' else 'процедурке'}. "
        f"Уклонение от исполнения влечёт последствия.\n\n"
        f"_Подписано: Комитет по нарядам_"
    ),
    # инопланетяне
    lambda person, duty: (
        f"👽 *ВХОДЯЩИЙ СИГНАЛ ИЗ ГЛУБОКОГО КОСМОСА*\n\n"
        f"Земляни-ин *{person}*!\n"
        f"Ва-аша планета назначи-ила тебя дежурным по "
        f"{'сводкам' if duty=='svodki' else 'процедурке'}.\n"
        f"Не подводи-и Землю-у. Мы наблюдаем-м.\n\n"
        f"_— цивилизация Зоргов_"
    ),
    # рэп
    lambda person, duty: (
        f"🎤 *ДРОП*\n\n"
        f"йо, {person}, слышишь зов?\n"
        f"{'сводки' if duty=='svodki' else 'процедурка'} — это не прикол\n"
        f"встань с дивана, хватит спать\n"
        f"время наряд выполнять\n"
        f"хоп-хоп, не тупи\n"
        f"погнал, до свидания 🎤"
    ),
    # стихотворение
    lambda person, duty: (
        f"📜 *Ода дежурному*\n\n"
        f"О, {person}, герой без плаща,\n"
        f"{'Сводки несёт, душа трепеща' if duty=='svodki' else 'Процедурку убрав, не спеша'}.\n"
        f"Наряд как судьба — не избегнуть его,\n"
        f"Иди же скорее, не медли ничё.\n\n"
        f"_— анонимный поэт нарядов_"
    ),
    # баг матрицы
    lambda person, duty: (
        f"⚠️ *ОБНАРУЖЕН БАГ В МАТРИЦЕ*\n\n"
        f"```\nERROR: duty_avoidance detected\nUSER: {person}\n"
        f"TASK: {'svodki' if duty=='svodki' else 'procedura'}\nSTATUS: PENDING\n```\n\n"
        f"Перезагрузка невозможна. Выполни наряд для продолжения реальности."
    ),
]

# обычные концовки (фоллбэк)
ENDINGS_DEFAULT = [
    "давай, не подведи команду 💪",
    "ты справишься, не ной 🫡",
    "вперёд, легенда 🚀",
    "команда верит. ну или типа того 👊",
    "не тупи, сделай и иди отдыхай 🛋",
    "всё в твоих руках, чемпион 🤝",
    "удачи. хотя тут и удача не нужна, просто сделай 😐",
    "погнали, чё стоишь 💨",
]

DONE_REPLIES = [
    "✅ красава! записал, можешь выдыхать 🛋",
    "✅ зафиксировал. команда тобой гордится (наверное)",
    "✅ чётко отработал, уважение 🫡",
    "✅ выполнено. ты молодец, иди отдыхай",
    "✅ записал в журнал. так держать, братан 💪",
    "✅ сделано! статистика не врёт — ты топ",
    "✅ зачёт. командная работа — это святое 🤝",
    "✅ принято! ты буквально держишь этот коллектив 🙌",
]

RETRY_PREFIX = [
    "⏰ эй, ты там живой? повторяю:",
    "⏰ второй звонок, не игнорируй:",
    "⏰ окей окей, напоминаю ещё раз:",
    "⏰ хм, всё ещё не сделано? ладно:",
    "⏰ я просто делаю своё дело. напоминаю:",
    "⏰ настойчивость — это я. вот снова:",
]

PERSONAL_SVODKI = [
    "эй {name} 👋 сегодня твои сводки. не забудь, ладно?",
    "yo {name}, наряд по сводкам — это ты сегодня. погнали 📋",
    "{name}, привет. сводки сами себя не потащат, ты понял",
    "внимание, {name}! сводки сегодня на тебе. удачи 🫡",
    "псс, {name} 👀 напоминаю тихонько — сводки твои сегодня",
    "{name} сегодня несёт сводки. это ты. это не я. это ты 😐",
]

PERSONAL_PROC = [
    "эй {name} 🧹 вечером процедурка на тебе. не забей",
    "yo {name}, уборка процедурки сегодня твоя. справишься",
    "{name}, привет. процедурка ждёт тебя в 22:00 👀",
    "внимание, {name}! процедурка сегодня твоя. это судьба",
    "псс, {name} — убраться в процедурке это ты сегодня 🧹",
    "{name}, 22:00 приближается. процедурка уже скучает по тебе",
]

MONDAY_INTROS = [
    "☀️ доброе утро, страдальцы. новая неделя — новые наряды:",
    "☀️ понедельник, все мы немного умерли. но наряды никто не отменял:",
    "☀️ йоу, начинаем неделю. вот кто пашет:",
    "☀️ пн 9:00 — самое то узнать кому досталось на этой неделе:",
    "☀️ новая неделя ворвалась без предупреждения. вот расписание:",
    "☀️ понедельник не спросил хотите ли вы. вот кто дежурит:",
]

SUNDAY_INTROS = [
    "🏆 итоги недели. кто вообще старался:",
    "🏆 неделя прошла, подводим итоги нарядов:",
    "🏆 воскресенье — время чтить героев:",
    "🏆 статистика недели. цифры не врут:",
    "🏆 занавес опускается. подсчитываем потери и победы:",
    "🏆 всё, конец недели. смотрим кто реально работал:",
]

# ══════════════════════════════════════════════
#  ХРАНИЛИЩЕ ДАННЫХ
# ══════════════════════════════════════════════
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    data = {
        "start_date":            date.today().isoformat(),
        "svodki_start_index":    0,
        "procedura_start_index": 0,
        "chat_id":               CHAT_ID,
        "reminders_enabled":     True,
        "personal_ids":          {},
        "log":                   {},
        "pending_retry":         {},
        "duty_counts":           {},
        # ✦ 19 — id закреплённых сообщений для редактирования
        "pinned_msg_id":         None,   # единое закреплённое сообщение
        # ✦ 18 — очередь на удаление: [{"chat_id": x, "msg_id": y, "delete_at": iso}]
        "delete_queue":          [],
        # ✦ 2 — настроение дня
        "daily_mood":            {},     # {"date": "...", "mood": "ironic"}
        # ✦ 18 — id последних служебных сообщений для автоудаления
        "last_svodki_msg_id":    None,
        "last_proc_msg_id":      None,
    }
    save_data(data)
    return data

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def increment_count(name: str, duty_type: str):
    data = load_data()
    counts = data.setdefault("duty_counts", {})
    person = counts.setdefault(name, {"svodki": 0, "proc": 0})
    person[duty_type] = person.get(duty_type, 0) + 1
    save_data(data)

def get_total_duties(name: str) -> int:
    data = load_data()
    counts = data.get("duty_counts", {}).get(name, {})
    return counts.get("svodki", 0) + counts.get("proc", 0)

# ══════════════════════════════════════════════
#  ✦ УЛУЧШЕНИЕ 2 — НАСТРОЕНИЕ ДНЯ
# ══════════════════════════════════════════════
def get_daily_mood() -> str:
    """Возвращает настроение на сегодня, генерирует если нет."""
    data  = load_data()
    today = date.today().isoformat()
    dm    = data.get("daily_mood", {})
    if dm.get("date") != today:
        mood = random.choice(list(MOODS.keys()))
        data["daily_mood"] = {"date": today, "mood": mood}
        save_data(data)
        return mood
    return dm.get("mood", "ironic")

def get_ending() -> str:
    """Концовка с учётом настроения дня."""
    mood = get_daily_mood()
    return random.choice(MOODS[mood]["endings"])

def get_mood_label() -> str:
    mood = get_daily_mood()
    return MOODS[mood]["label"]

# ══════════════════════════════════════════════
#  ✦ УЛУЧШЕНИЕ 18 — УДАЛЕНИЕ СЛУЖЕБНЫХ СООБЩЕНИЙ
# ══════════════════════════════════════════════
async def schedule_delete(bot: Bot, chat_id: int | str, msg_id: int, delay_seconds: int):
    """Удаляет сообщение через delay_seconds секунд."""
    await asyncio.sleep(delay_seconds)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass  # уже удалено или нет прав

async def auto_delete_later(bot: Bot, chat_id: int | str, msg_id: int, seconds: int = 30):
    """Запускает фоновую задачу на удаление."""
    asyncio.create_task(schedule_delete(bot, chat_id, msg_id, seconds))

# ══════════════════════════════════════════════
#  ✦ УЛУЧШЕНИЕ 19 — ЕДИНОЕ ЗАКРЕПЛЁННОЕ СООБЩЕНИЕ
# ══════════════════════════════════════════════
def build_pinned_content() -> str:
    """Содержимое единого закреплённого сообщения — актуальный наряд на сегодня."""
    today    = date.today()
    svodki   = get_svodki_person(today)
    proc     = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    sv_em    = PERSON_EMOJI.get(svodki, "👤")
    pr_em    = PERSON_EMOJI.get(proc, "👤")
    div      = weekday_divider(today)
    day_name, icon, _, _ = WEEKDAY_STYLE[today.weekday()]
    date_str = today.strftime("%d.%m.%Y")
    mood_lbl = get_mood_label()
    filled   = round((proc_day / 2) * 8)
    bar      = "▓" * filled + "░" * (8 - filled)

    return (
        f"📌 *АКТУАЛЬНЫЙ НАРЯД*\n"
        f"{icon} {day_name}, {date_str}\n"
        f"_настроение бота: {mood_lbl}_\n"
        f"{div}\n\n"
        f"🤷‍♀️ *Дежурный №{SVODKI_LIST.index(svodki)+1} по сводкам:*\n"
        f"  {sv_em} *{svodki}*\n\n"
        f"⚡️ *Дежурный №{PROCEDURA_LIST.index(proc)+1} по процедурке:*\n"
        f"  {pr_em} *{proc}*\n"
        f"  `[{bar}]` день {proc_day}/2\n\n"
        f"{div}\n"
        f"_обновляется автоматически каждый день_"
    )

async def update_pinned_message(bot: Bot):
    """
    ✦ 19 — Редактирует единое закреплённое сообщение.
    Если его нет — создаёт и закрепляет.
    """
    data    = load_data()
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        return

    content       = build_pinned_content()
    pinned_msg_id = data.get("pinned_msg_id")

    if pinned_msg_id:
        # пробуем отредактировать существующее
        try:
            await bot.edit_message_text(
                content, chat_id=chat_id,
                message_id=pinned_msg_id,
                parse_mode="Markdown"
            )
            logger.info("✦19 закреплённое сообщение обновлено")
            return
        except Exception as e:
            logger.warning(f"✦19 не удалось отредактировать закреплённое: {e}")

    # создаём новое и закрепляем
    try:
        sent = await bot.send_message(chat_id, content, parse_mode="Markdown")
        try:
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except Exception:
            pass
        data["pinned_msg_id"] = sent.message_id
        save_data(data)
        logger.info(f"✦19 создано новое закреплённое сообщение id={sent.message_id}")
    except Exception as e:
        logger.error(f"✦19 ошибка создания закреплённого: {e}")

# ══════════════════════════════════════════════
#  ЛОГИКА НАРЯДОВ
# ══════════════════════════════════════════════
def get_svodki_idx(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return (data["svodki_start_index"] + (target_date - start).days) % len(SVODKI_LIST)

def get_svodki_person(target_date: date) -> str:
    return SVODKI_LIST[get_svodki_idx(target_date)]

def get_procedura_idx(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return (data["procedura_start_index"] + (target_date - start).days // 2) % len(PROCEDURA_LIST)

def get_procedura_person(target_date: date) -> str:
    return PROCEDURA_LIST[get_procedura_idx(target_date)]

def get_duty_day_number(target_date: date) -> int:
    data = load_data()
    start = date.fromisoformat(data["start_date"])
    return ((target_date - start).days % 2) + 1

def days_until_svodki(name: str, from_date: date) -> int:
    for i in range(1, len(SVODKI_LIST) + 1):
        if get_svodki_person(from_date + timedelta(days=i)) == name:
            return i
    return len(SVODKI_LIST)

def days_until_procedura(name: str, from_date: date) -> int:
    for i in range(1, len(PROCEDURA_LIST) * 2 + 1):
        if get_procedura_person(from_date + timedelta(days=i)) == name:
            return i
    return len(PROCEDURA_LIST) * 2

# ══════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════
class AdminStates(StatesGroup):
    waiting_chat_id  = State()
    waiting_register = State()

# ══════════════════════════════════════════════
#  ОФОРМЛЕНИЕ
# ══════════════════════════════════════════════
def is_weekend(d: date) -> bool:
    return d.weekday() >= 5

def person_tag(name: str) -> str:
    return f"{PERSON_EMOJI.get(name, '👤')} *{name}*"

def progress_bar(current: int, total: int) -> str:
    filled = round((current / total) * 8)
    return f"`[{'▓'*filled}{'░'*(8-filled)}]` {current}/{total}"

def weekday_divider(d: date) -> str:
    _, _, sym_l, sym_r = WEEKDAY_STYLE[d.weekday()]
    return f"{sym_l}━━━━━━━━━━━━━━━━{sym_r}"

def build_date_header(d: date, label: str) -> str:
    day_name, icon, _, _ = WEEKDAY_STYLE[d.weekday()]
    date_str = d.strftime("%d.%m.%Y")
    if is_weekend(d):
        return f"{icon} *{label} — {day_name.upper()}, {date_str}* {icon}"
    return f"{icon} *{label} — {day_name}, {date_str}*"

def svodki_num_label(name: str) -> str:
    return f"Дежурный №{SVODKI_LIST.index(name)+1} по сводкам"

def proc_num_label(name: str) -> str:
    return f"Дежурный №{PROCEDURA_LIST.index(name)+1} по процедурке"

def next_person_svodki(name: str) -> str:
    return SVODKI_LIST[(SVODKI_LIST.index(name) + 1) % len(SVODKI_LIST)]

def next_person_proc(name: str) -> str:
    return PROCEDURA_LIST[(PROCEDURA_LIST.index(name) + 1) % len(PROCEDURA_LIST)]

# ══════════════════════════════════════════════
#  ПОСТРОЕНИЕ СООБЩЕНИЙ
# ══════════════════════════════════════════════
def build_duty_message(target_date: date, label: str) -> str:
    svodki   = get_svodki_person(target_date)
    proc     = get_procedura_person(target_date)
    proc_day = get_duty_day_number(target_date)
    div      = weekday_divider(target_date)
    header   = build_date_header(target_date, label)
    mood_lbl = get_mood_label()
    weekend_note = "\n🥳 *выходной, но наряды никто не отменял*" if is_weekend(target_date) else ""
    return "\n".join([
        f"{header}{weekend_note}",
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

def build_week_schedule() -> str:
    today = date.today()
    short = ["пн","вт","ср","чт","пт","сб","вс"]
    lines = ["*📊 расписание нарядов — 7 дней вперёд*\n_кто следующий страдалец? смотри:_\n"]
    for i in range(7):
        d       = today + timedelta(days=i)
        sv      = get_svodki_person(d)
        pr      = get_procedura_person(d)
        pd_     = get_duty_day_number(d)
        wk_icon = WEEKDAY_STYLE[d.weekday()][1]
        lbl     = "сегодня" if i == 0 else ("завтра" if i == 1 else f"{short[d.weekday()]} {d.strftime('%d.%m')}")
        lines.append(
            f"{wk_icon} *{lbl}*\n"
            f"  {PERSON_EMOJI.get(sv,'👤')} №{SVODKI_LIST.index(sv)+1} {sv} — сводки\n"
            f"  {PERSON_EMOJI.get(pr,'👤')} №{PROCEDURA_LIST.index(pr)+1} {pr} — процедурка {progress_bar(pd_,2)}\n"
        )
    lines.append(weekday_divider(today))
    return "\n".join(lines)

def build_log_message() -> str:
    data  = load_data()
    log   = data.get("log", {})
    lines = ["*📓 журнал за 7 дней*\n_кто делал, кто забил — всё тут:_\n"]
    for i in range(6, -1, -1):
        d     = date.today() - timedelta(days=i)
        entry = log.get(d.isoformat(), {})
        sv    = get_svodki_person(d)
        pr    = get_procedura_person(d)
        sv_ok = "✅" if entry.get("svodki") else "❌"
        pr_ok = "✅" if entry.get("proc")   else "❌"
        lbl   = "сегодня" if i == 0 else d.strftime("%d.%m")
        lines.append(f"📅 *{lbl}*  {sv_ok}{PERSON_EMOJI.get(sv,'👤')}{sv}  |  {pr_ok}{PERSON_EMOJI.get(pr,'👤')}{pr}")
    lines.append("\n_✅ = сделано, ❌ = либо молча сделал, либо нет 🤷_")
    return "\n".join(lines)

def build_monday_briefing() -> str:
    today = date.today()
    intro = random.choice(MONDAY_INTROS)
    short = ["пн","вт","ср","чт","пт","сб","вс"]
    lines = [f"{intro}\n"]
    for i in range(7):
        d       = today + timedelta(days=i)
        sv      = get_svodki_person(d)
        pr      = get_procedura_person(d)
        pd_     = get_duty_day_number(d)
        wk_icon = WEEKDAY_STYLE[d.weekday()][1]
        lbl     = f"{short[d.weekday()]} {d.strftime('%d.%m')}"
        lines.append(
            f"{wk_icon} *{lbl}* — "
            f"{PERSON_EMOJI.get(sv,'👤')}{sv} / "
            f"{PERSON_EMOJI.get(pr,'👤')}{pr} {progress_bar(pd_,2)}"
        )
    lines.append(f"\n{weekday_divider(today)}\n_удачи всем. она вам понадобится_ 💀")
    return "\n".join(lines)

def build_sunday_summary() -> str:
    data   = load_data()
    log    = data.get("log", {})
    counts = {}
    intro  = random.choice(SUNDAY_INTROS)
    for i in range(6, -1, -1):
        d     = date.today() - timedelta(days=i)
        entry = log.get(d.isoformat(), {})
        for field in ("svodki", "proc"):
            name = entry.get(field)
            if name:
                counts.setdefault(name, {"svodki": 0, "proc": 0})
                counts[name][field] += 1
    if not counts:
        return (
            f"{intro}\n\n😶 за эту неделю никто не нажимал «сделано»\n"
            "либо все реально пахали молча, либо… ну ты понял"
        )
    mvp    = max(counts, key=lambda n: counts[n]["svodki"] + counts[n]["proc"])
    mvp_em = PERSON_EMOJI.get(mvp, "👤")
    lines  = [f"{intro}\n"]
    for name, c in sorted(counts.items(), key=lambda x: -(x[1]["svodki"]+x[1]["proc"])):
        em    = PERSON_EMOJI.get(name, "👤")
        total = c["svodki"] + c["proc"]
        lines.append(f"  {em} *{name}* — сводки: {c['svodki']}, процедурка: {c['proc']} | итого: {total}")
    lines.append(f"\n🏆 *mvp недели: {mvp_em} {mvp}*")
    lines.append("_уважение и почёт. заслужил_ 🫡")
    return "\n".join(lines)

# ══════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Наряд сегодня"),        KeyboardButton(text="📅 Наряд завтра")],
            [KeyboardButton(text="📊 Расписание на неделю"),  KeyboardButton(text="📓 Журнал")],
            [KeyboardButton(text="🔔 Напомнить сейчас"),     KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True
    )

def confirmation_keyboard(duty_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ сделано, всё чётко",  callback_data=f"done:{duty_type}"),
        InlineKeyboardButton(text="⏰ напомни через 15м",   callback_data=f"retry:{duty_type}"),
    ]])

def settings_keyboard(data: dict) -> InlineKeyboardMarkup:
    enabled     = data.get("reminders_enabled", True)
    toggle_text = "🔕 вырубить напоминания" if enabled else "🔔 включить напоминания"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text,                          callback_data="toggle_reminders")],
        [InlineKeyboardButton(text="📍 установить чат",                  callback_data="set_chat")],
        [InlineKeyboardButton(text="👤 кто сегодня — сводки",            callback_data="pick_svodki")],
        [InlineKeyboardButton(text="👤 кто сегодня — процедурка",        callback_data="pick_procedura")],
        [InlineKeyboardButton(text="📝 список сводок",                   callback_data="show_svodki_list")],
        [InlineKeyboardButton(text="📝 список процедурки",               callback_data="show_procedura_list")],
        [InlineKeyboardButton(text="🪪 зарегистрировать личный id",      callback_data="register_personal")],
        [InlineKeyboardButton(text="📌 обновить закреплённое",           callback_data="update_pinned")],
        [InlineKeyboardButton(text="◀️ назад",                           callback_data="back_main")],
    ])

def svodki_pick_keyboard(actual_idx: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, name in enumerate(SVODKI_LIST):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = "✅ " if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{mark}{em}{name}", callback_data=f"set_svodki_idx:{i}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def procedura_pick_keyboard(actual_idx: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, name in enumerate(PROCEDURA_LIST):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = "✅ " if i == actual_idx else ""
        row.append(InlineKeyboardButton(text=f"{mark}{em}{name}", callback_data=f"set_proc_idx:{i}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ назад к настройкам", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ══════════════════════════════════════════════
#  ГЕНЕРАЦИЯ И ОТПРАВКА КАРТОЧКИ
# ══════════════════════════════════════════════
WEEKDAY_FULL_NAMES = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
WEEKDAY_ICONS_LIST = ["😵","😐","⬡","✦","🔥","🥳","💀"]

async def send_card_with_message(
    bot: Bot,
    chat_id,
    msg_text: str,
    reply_markup,
    duty_type: str,
    person: str,
    position_label: str,
    time_label: str,
    header_text: str,
    ending_text: str,
    proc_day: int = 1,
    next_person: str = "",
    next_days: int = 0,
    total_duties: int = 0,
) -> int | None:
    """
    Генерирует картинку и отправляет её вместе с текстом.
    Возвращает message_id отправленного сообщения.
    """
    today    = datetime.now(TIMEZONE).date()
    date_lbl = today.strftime("%d.%m.%Y") + " • " + WEEKDAY_FULL_NAMES[today.weekday()]
    mood_lbl = get_mood_label()

    if CARDS_ENABLED:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tmp_path = tf.name
        try:
            make_reminder_card(
                duty_type      = duty_type,
                person         = person,
                position_label = position_label,
                time_label     = time_label,
                header_text    = header_text,
                ending_text    = ending_text,
                date_label     = date_lbl,
                proc_day       = proc_day,
                next_person    = next_person,
                next_days      = next_days,
                total_duties   = total_duties,
                mood_label     = mood_lbl,
                output_path    = tmp_path,
            )
            photo = FSInputFile(tmp_path)
            sent = await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile(tmp_path),
            caption=msg_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
            )
            return sent.message_id
        except Exception as e:
            logger.error(f"ошибка генерации карточки: {e}")
        finally:
            try: os.unlink(tmp_path)
            except: pass

    # фоллбэк — просто текст без картинки
    sent = await bot.send_message(
        chat_id, msg_text,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return sent.message_id


async def send_daily_card(bot: Bot, chat_id):
    """Карточка дня с обеими зонами (для понедельничной рассылки)."""
    if not CARDS_ENABLED:
        return
    today = datetime.now(TIMEZONE).date()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tmp_path = tf.name
    try:
        make_daily_card(
            svodki_person    = get_svodki_person(today),
            svodki_label     = svodki_num_label(get_svodki_person(today)),
            proc_person      = get_procedura_person(today),
            proc_label       = proc_num_label(get_procedura_person(today)),
            proc_day         = get_duty_day_number(today),
            date_label       = today.strftime("%d.%m.%Y"),
            weekday_name     = WEEKDAY_FULL_NAMES[today.weekday()],
            weekday_icon     = WEEKDAY_ICONS_LIST[today.weekday()],
            svodki_next      = next_person_svodki(get_svodki_person(today)),
            svodki_next_days = days_until_svodki(get_svodki_person(today), today),
            proc_next        = next_person_proc(get_procedura_person(today)),
            proc_next_days   = days_until_procedura(get_procedura_person(today), today),
            svodki_total     = get_total_duties(get_svodki_person(today)),
            proc_total       = get_total_duties(get_procedura_person(today)),
            mood_label       = get_mood_label(),
            output_path      = tmp_path,
        )
        photo = FSInputFile(tmp_path)
        await bot.send_photo(chat_id, photo=photo)
    except Exception as e:
        logger.error(f"ошибка daily card: {e}")
    finally:
        try: os.unlink(tmp_path)
        except: pass


# ══════════════════════════════════════════════
#  ОТПРАВКА НАПОМИНАНИЙ
# ══════════════════════════════════════════════
async def _send_reminder(bot: Bot, duty_type: str, retry: bool = False):
    card = generate_card(
    svodki=get_svodki_person(today),
    proc=get_procedura_person(today),
    mood=get_mood_label(),
    proc_day=get_duty_day_number(today),
    total_svodki=get_total_duties(
        get_svodki_person(today)
    ),
    total_proc=get_total_duties(
        get_procedura_person(today)
    )
    )
    data    = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        logger.warning("CHAT_ID не настроен!"); return

    today   = datetime.now(TIMEZONE).date()
    div     = weekday_divider(today)
    wd      = today.weekday()

    # ✦ 3 — пасхалка с шансом 5%
    easter  = random.random() < 0.05 and not retry

    if duty_type == "svodki":
        person   = get_svodki_person(today)
        next_d   = days_until_svodki(person, today)
        next_n   = next_person_svodki(person)
        total    = get_total_duties(person)
        # ✦ 1 — заголовок по дню недели
        header   = random.choice(RETRY_PREFIX) if retry else random.choice(SVODKI_HEADERS_BY_DAY[wd])
        # ✦ 2 — концовка по настроению дня
        ending   = get_ending()

        if easter:
            msg_text = random.choice(EASTER_EGGS)(person, duty_type)
        else:
            msg_text = (
                f"*{header}*\n{div}\n\n"
                f"🤷‍♀️ *{svodki_num_label(person)}:*\n"
                f"    {person_tag(person)}\n"
                f"    📊 нарядов за всё время: *{total}*\n"
                f"    ⏭ после — {person_tag(next_n)} через *{next_d} д.*\n\n"
                f"_{ending}_\n{div}"
            )

    else:
        person     = get_procedura_person(today)
        proc_day   = get_duty_day_number(today)
        tomorrow   = today + timedelta(days=1)
        proc_next  = get_procedura_person(tomorrow)
        proc_day_t = get_duty_day_number(tomorrow)
        next_d     = days_until_procedura(person, today)
        next_n     = next_person_proc(person)
        total      = get_total_duties(person)
        header     = random.choice(RETRY_PREFIX) if retry else random.choice(PROC_HEADERS_BY_DAY[wd])
        ending     = get_ending()

        if easter:
            msg_text = random.choice(EASTER_EGGS)(person, duty_type)
        else:
            msg_text = (
                f"*{header}*\n{div}\n\n"
                f"⚡️ *{proc_num_label(person)}:*\n"
                f"    {person_tag(person)}\n"
                f"    {progress_bar(proc_day, 2)}\n"
                f"    📊 нарядов за всё время: *{total}*\n"
                f"    ⏭ после — {person_tag(next_n)} через *{next_d} д.*\n\n"
                f"⏭ *завтра пашет:*\n"
                f"    {person_tag(proc_next)} {progress_bar(proc_day_t, 2)}\n\n"
                f"_{ending}_\n{div}"
            )

    try:
        # ✦ 19 — удаляем предыдущее напоминание того же типа
        prev_key = "last_svodki_msg_id" if duty_type == "svodki" else "last_proc_msg_id"
        prev_id  = data.get(prev_key)
        if prev_id:
            try:
                await bot.delete_message(chat_id, prev_id)
            except Exception:
                pass

        # отправляем карточку + текст
        sent_id = await send_card_with_message(
            bot          = bot,
            chat_id      = chat_id,
            msg_text     = msg_text,
            reply_markup = confirmation_keyboard(duty_type),
            duty_type    = duty_type,
            person       = person,
            position_label = svodki_num_label(person) if duty_type == "svodki" else proc_num_label(person),
            time_label   = "18:00" if duty_type == "svodki" else "22:00",
            header_text  = header.replace("📋 ", "").replace("🧹 ", ""),
            ending_text  = ending,
            proc_day     = proc_day if duty_type == "proc" else 1,
            next_person  = next_n,
            next_days    = next_d,
            total_duties = total,
        )

        # сохраняем id для удаления при следующем напоминании
        if sent_id:
            data[prev_key] = sent_id
            save_data(data)

        # ✦ 19 — обновляем закреплённое сообщение
        await update_pinned_message(bot)

        # личное уведомление
        uid = data.get("personal_ids", {}).get(person)
        if uid:
            tmpl = random.choice(PERSONAL_SVODKI if duty_type == "svodki" else PERSONAL_PROC)
            try:
                await bot.send_message(uid, tmpl.format(name=person))
            except Exception as e:
                logger.warning(f"личное сообщение {person}: {e}")

        logger.info(f"напоминание [{duty_type}] → {person} {'🥚ПАСХАЛКА' if easter else ''}")
    except Exception as e:
        logger.error(f"ошибка отправки [{duty_type}]: {e}")


async def send_svodki_reminder(bot: Bot):
    await _send_reminder(bot, "svodki")

async def send_procedura_reminder(bot: Bot):
    await _send_reminder(bot, "proc")

async def send_retry_reminder(bot: Bot, duty_type: str):
    data    = load_data()
    pending = data.get("pending_retry", {})
    today   = date.today().isoformat()
    if pending.get(duty_type) == today:
        await _send_reminder(bot, duty_type, retry=True)
        pending.pop(duty_type, None)
        data["pending_retry"] = pending
        save_data(data)

async def send_monday_briefing(bot: Bot):
    data    = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try:
        await bot.send_message(chat_id, build_monday_briefing(), parse_mode="Markdown")
        # обновляем закреплённое — новая неделя
        await update_pinned_message(bot)
    except Exception as e:
        logger.error(f"понедельничная рассылка: {e}")

async def send_sunday_summary(bot: Bot):
    data    = load_data()
    if not data.get("reminders_enabled", True): return
    chat_id = data.get("chat_id", CHAT_ID)
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE": return
    try:
        await bot.send_message(chat_id, build_sunday_summary(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"воскресный итог: {e}")

async def daily_pinned_update(bot: Bot):
    """✦ 19 — обновляем закреплённое каждый день в полночь."""
    await update_pinned_message(bot)

# ══════════════════════════════════════════════
#  ХЕНДЛЕРЫ
# ══════════════════════════════════════════════
async def cmd_start(message: types.Message):
    sent = await message.answer(
        "👋 *йоу! я бот-напоминалка нарядов*\n\n"
        "каждый день шлю:\n"
        "• *18:00* — кто тащит сводки\n"
        "• *22:00* — кто убирает процедурку\n"
        "• пн *9:00* — расписание на всю неделю\n"
        "• вс *20:00* — итоги недели + mvp\n\n"
        "у меня есть *настроение* — каждый день разное 🎭\n"
        "иногда случаются *пасхалки* — следи за напоминаниями 👀\n"
        "старые напоминания удаляются автоматически 🗑\n"
        "закреплённое сообщение обновляется каждый день 📌\n\n"
        "поехали 👇",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    # ✦ 18 — удаляем через 60 секунд (команда /start — служебное)
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 60)

async def cmd_today(message: types.Message):
    sent = await message.answer(build_duty_message(date.today(), "сегодня"), parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)  # 5 минут

async def cmd_tomorrow(message: types.Message):
    sent = await message.answer(build_duty_message(date.today() + timedelta(days=1), "завтра"), parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)

async def cmd_week(message: types.Message):
    sent = await message.answer(build_week_schedule(), parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 300)

async def cmd_log(message: types.Message):
    sent = await message.answer(build_log_message(), parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 120)

async def cmd_remind_now(message: types.Message):
    # ✦ 18 — удаляем сообщение с кнопкой через 30 сек
    sent = await message.answer("🔔 *погнали, отправляю в чат...*", parse_mode="Markdown")
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
    await _send_reminder(message.bot, "svodki")
    await _send_reminder(message.bot, "proc")

async def cmd_settings(message: types.Message):
    data      = load_data()
    enabled   = "✅ включены" if data.get("reminders_enabled", True) else "❌ выключены"
    chat_txt  = data.get("chat_id", "не настроен")
    sv_now    = get_svodki_person(date.today())
    pr_now    = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em     = PERSON_EMOJI.get(sv_now, "👤")
    pr_em     = PERSON_EMOJI.get(pr_now, "👤")
    mood_lbl  = get_mood_label()
    await message.answer(
        f"⚙️ *настройки*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 чат: `{chat_txt}`\n"
        f"🔔 напоминания: {enabled}\n"
        f"🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n"
        f"⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n"
        f"🪪 зарегистрировано: *{reg_count}* чел.\n"
        f"🎭 настроение бота: *{mood_lbl}*\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=settings_keyboard(data)
    )

async def cmd_getchatid(message: types.Message):
    sent = await message.answer(
        f"📍 *id этого чата:*\n`{message.chat.id}`\n\nскопируй и вставь в настройки",
        parse_mode="Markdown"
    )
    # ✦ 18 — удаляем служебное через 30 сек
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 30)

# ── ПОДТВЕРЖДЕНИЕ ──
async def callback_done(callback: types.CallbackQuery):
    duty_type = callback.data.split(":")[1]
    today     = date.today()
    data      = load_data()
    log       = data.setdefault("log", {})
    entry     = log.setdefault(today.isoformat(), {})
    person    = get_svodki_person(today) if duty_type == "svodki" else get_procedura_person(today)
    field     = "svodki" if duty_type == "svodki" else "proc"
    entry[field] = person
    data["log"]  = log
    data.get("pending_retry", {}).pop(duty_type, None)
    save_data(data)
    increment_count(person, field)
    await callback.answer(random.choice(DONE_REPLIES), show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)
    # ✦ 19 — обновляем закреплённое после подтверждения
    await update_pinned_message(callback.bot)

async def callback_retry(callback: types.CallbackQuery):
    duty_type = callback.data.split(":")[1]
    data      = load_data()
    data.setdefault("pending_retry", {})[duty_type] = date.today().isoformat()
    save_data(data)
    await callback.answer("⏰ окей, напомню через 15 минут", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)

# ── НАСТРОЙКИ ──
async def callback_toggle_reminders(callback: types.CallbackQuery):
    data = load_data()
    data["reminders_enabled"] = not data.get("reminders_enabled", True)
    save_data(data)
    status = "✅ включены" if data["reminders_enabled"] else "❌ выключены"
    await callback.answer(f"напоминания {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(data))

async def callback_set_chat(callback: types.CallbackQuery, state: FSMContext):
    sent = await callback.message.answer(
        "📍 *напиши id чата* куда кидать напоминания:\n"
        "узнать: напиши `/chatid` в нужном чате",
        parse_mode="Markdown"
    )
    await auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await state.set_state(AdminStates.waiting_chat_id)
    await callback.answer()

async def process_chat_id(message: types.Message, state: FSMContext):
    chat_id = message.text.strip()
    data    = load_data()
    data["chat_id"] = chat_id
    save_data(data)
    await state.clear()
    sent = await message.answer(f"✅ *чат установлен!*\nid: `{chat_id}`", parse_mode="Markdown", reply_markup=main_keyboard())
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 30)

# ── ✦ 19 ОБНОВИТЬ ЗАКРЕПЛЁННОЕ ВРУЧНУЮ ──
async def callback_update_pinned(callback: types.CallbackQuery):
    await callback.answer("📌 обновляю закреплённое...", show_alert=False)
    await update_pinned_message(callback.bot)
    await callback.answer("✅ закреплённое обновлено!", show_alert=True)

# ── РЕГИСТРАЦИЯ ──
async def callback_register_personal(callback: types.CallbackQuery, state: FSMContext):
    names = " / ".join(SVODKI_LIST)
    sent  = await callback.message.answer(
        "🪪 *регистрация личных уведомлений*\n\n"
        f"напиши своё имя *точно как в списке*:\n{names}\n\n"
        "буду писать тебе лично в день дежурства 👀",
        parse_mode="Markdown"
    )
    await auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await state.set_state(AdminStates.waiting_register)
    await callback.answer()

async def process_register(message: types.Message, state: FSMContext):
    name      = message.text.strip()
    all_names = list(set(SVODKI_LIST + PROCEDURA_LIST))
    if name not in all_names:
        sent = await message.answer(
            f"❌ *{name}* не найдено в списках\nпроверь написание и попробуй снова",
            parse_mode="Markdown"
        )
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 20)
        return
    data = load_data()
    data.setdefault("personal_ids", {})[name] = message.from_user.id
    save_data(data)
    em = PERSON_EMOJI.get(name, "👤")
    await state.clear()
    sent = await message.answer(
        f"✅ {em} *{name}* зарегистрирован!\nбуду писать тебе лично в день наряда 🫡",
        parse_mode="Markdown", reply_markup=main_keyboard()
    )
    await auto_delete_later(message.bot, message.chat.id, sent.message_id, 30)

# ── ВЫБОР ДЕЖУРНОГО ──
async def callback_pick_svodki(callback: types.CallbackQuery):
    data        = load_data()
    today       = date.today()
    days_passed = (today - date.fromisoformat(data["start_date"])).days
    actual_idx  = (data["svodki_start_index"] + days_passed) % len(SVODKI_LIST)
    await callback.message.edit_text(
        f"🤷‍♀️ *кто сегодня по сводкам?*\n"
        f"сейчас: {PERSON_EMOJI.get(SVODKI_LIST[actual_idx],'👤')} *{SVODKI_LIST[actual_idx]}*\n\n"
        f"нажми на нужное имя:",
        parse_mode="Markdown", reply_markup=svodki_pick_keyboard(actual_idx)
    )
    await callback.answer()

async def callback_pick_procedura(callback: types.CallbackQuery):
    data        = load_data()
    today       = date.today()
    days_passed = (today - date.fromisoformat(data["start_date"])).days
    actual_idx  = (data["procedura_start_index"] + days_passed // 2) % len(PROCEDURA_LIST)
    await callback.message.edit_text(
        f"⚡️ *кто сегодня по процедурке?*\n"
        f"сейчас: {PERSON_EMOJI.get(PROCEDURA_LIST[actual_idx],'👤')} *{PROCEDURA_LIST[actual_idx]}*\n\n"
        f"нажми на нужное имя:",
        parse_mode="Markdown", reply_markup=procedura_pick_keyboard(actual_idx)
    )
    await callback.answer()

async def callback_set_svodki_idx(callback: types.CallbackQuery):
    chosen_idx  = int(callback.data.split(":")[1])
    chosen_name = SVODKI_LIST[chosen_idx]
    data        = load_data()
    days_passed = (date.today() - date.fromisoformat(data["start_date"])).days
    data["svodki_start_index"] = (chosen_idx - days_passed) % len(SVODKI_LIST)
    save_data(data)
    await callback.answer(f"✅ сводки сегодня: {PERSON_EMOJI.get(chosen_name,'👤')} {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=svodki_pick_keyboard(chosen_idx))
    await update_pinned_message(callback.bot)

async def callback_set_proc_idx(callback: types.CallbackQuery):
    chosen_idx  = int(callback.data.split(":")[1])
    chosen_name = PROCEDURA_LIST[chosen_idx]
    data        = load_data()
    days_passed = (date.today() - date.fromisoformat(data["start_date"])).days
    data["procedura_start_index"] = (chosen_idx - days_passed // 2) % len(PROCEDURA_LIST)
    save_data(data)
    await callback.answer(f"✅ процедурка сегодня: {PERSON_EMOJI.get(chosen_name,'👤')} {chosen_name}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=procedura_pick_keyboard(chosen_idx))
    await update_pinned_message(callback.bot)

async def callback_show_svodki_list(callback: types.CallbackQuery):
    today   = date.today()
    current = get_svodki_person(today)
    lines   = [f"*👥 наряд по сводкам:*\nсейчас: {PERSON_EMOJI.get(current,'👤')} *{current}*\n"]
    for i, name in enumerate(SVODKI_LIST, 1):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {em} {name}{mark}")
    sent = await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await callback.answer()

async def callback_show_procedura_list(callback: types.CallbackQuery):
    today    = date.today()
    current  = get_procedura_person(today)
    proc_day = get_duty_day_number(today)
    lines    = [f"*👥 наряд по процедурке:*\nсейчас: {PERSON_EMOJI.get(current,'👤')} *{current}* (день {proc_day}/2)\n"]
    for i, name in enumerate(PROCEDURA_LIST, 1):
        em   = PERSON_EMOJI.get(name, "👤")
        mark = " ◀️ сегодня" if name == current else ""
        lines.append(f"{i}️⃣ {em} {name} *(2 дня)*{mark}")
    sent = await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await auto_delete_later(callback.bot, callback.message.chat.id, sent.message_id, 60)
    await callback.answer()

async def callback_back_settings(callback: types.CallbackQuery):
    data      = load_data()
    enabled   = "✅ включены" if data.get("reminders_enabled", True) else "❌ выключены"
    chat_txt  = data.get("chat_id", "не настроен")
    sv_now    = get_svodki_person(date.today())
    pr_now    = get_procedura_person(date.today())
    reg_count = len(data.get("personal_ids", {}))
    sv_em     = PERSON_EMOJI.get(sv_now, "👤")
    pr_em     = PERSON_EMOJI.get(pr_now, "👤")
    mood_lbl  = get_mood_label()
    await callback.message.edit_text(
        f"⚙️ *настройки*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 чат: `{chat_txt}`\n"
        f"🔔 напоминания: {enabled}\n"
        f"🤷‍♀️ сводки сегодня: {sv_em} *{sv_now}*\n"
        f"⚡️ процедурка сегодня: {pr_em} *{pr_now}*\n"
        f"🪪 зарегистрировано: *{reg_count}* чел.\n"
        f"🎭 настроение бота: *{mood_lbl}*\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown", reply_markup=settings_keyboard(data)
    )
    await callback.answer()

async def callback_back_main(callback: types.CallbackQuery):
    await callback.message.answer("🏠 главное меню", reply_markup=main_keyboard())
    await callback.answer()

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
async def main():
    bot     = Bot(token=TOKEN)
    storage = MemoryStorage()
    dp      = Dispatcher(storage=storage)

    # команды и кнопки
    dp.message.register(cmd_start,        Command("start"))
    dp.message.register(cmd_getchatid,    Command("chatid"))
    dp.message.register(cmd_today,        F.text == "📋 Наряд сегодня")
    dp.message.register(cmd_tomorrow,     F.text == "📅 Наряд завтра")
    dp.message.register(cmd_week,         F.text == "📊 Расписание на неделю")
    dp.message.register(cmd_log,          F.text == "📓 Журнал")
    dp.message.register(cmd_remind_now,   F.text == "🔔 Напомнить сейчас")
    dp.message.register(cmd_settings,     F.text == "⚙️ Настройки")
    dp.message.register(process_chat_id,  AdminStates.waiting_chat_id)
    dp.message.register(process_register, AdminStates.waiting_register)

    # callbacks
    dp.callback_query.register(callback_done,                F.data.startswith("done:"))
    dp.callback_query.register(callback_retry,               F.data.startswith("retry:"))
    dp.callback_query.register(callback_toggle_reminders,    F.data == "toggle_reminders")
    dp.callback_query.register(callback_set_chat,            F.data == "set_chat")
    dp.callback_query.register(callback_register_personal,   F.data == "register_personal")
    dp.callback_query.register(callback_update_pinned,       F.data == "update_pinned")
    dp.callback_query.register(callback_pick_svodki,         F.data == "pick_svodki")
    dp.callback_query.register(callback_pick_procedura,      F.data == "pick_procedura")
    dp.callback_query.register(callback_set_svodki_idx,      F.data.startswith("set_svodki_idx:"))
    dp.callback_query.register(callback_set_proc_idx,        F.data.startswith("set_proc_idx:"))
    dp.callback_query.register(callback_show_svodki_list,    F.data == "show_svodki_list")
    dp.callback_query.register(callback_show_procedura_list, F.data == "show_procedura_list")
    dp.callback_query.register(callback_back_settings,       F.data == "back_settings")
    dp.callback_query.register(callback_back_main,           F.data == "back_main")

    # планировщик
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_svodki_reminder,    CronTrigger(hour=18, minute=0,  timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_procedura_reminder,  CronTrigger(hour=22, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_monday_briefing,    CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_sunday_summary,     CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE), args=[bot])
    # ✦ 19 — обновляем закреплённое каждый день в 00:01
    scheduler.add_job(daily_pinned_update,     CronTrigger(hour=0, minute=1, timezone=TIMEZONE), args=[bot])
    # повторные напоминания
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
    asyncio.run(main())
    
