"""
ai_handlers.py — AI-функции бота на Gemini (v3).

Возможности:
  - 10 персон (/persona и /ai persona) + учёт настроения дня
  - Осведомлённость ИИ о графике нарядов и таймерах ДМБ
  - Память о пользователях (/ai memory, /ai forget)
  - /ai ask (с Google Search), /ai roast (по @юзеру, имени или реплаю), /ai debate, /ai story
  - Плавный streaming-ответ с конвертацией Markdown → Telegram HTML
  - Понимание фото (в стиле персоны), PDF/текстовых документов, голосовых (F.voice) и кружочков (F.video_note)
  - Умный /tldr (по истории чата ИЛИ реплаем на голосовое/кружочек/длинный текст)
  - Генератор мемов /meme (демотиватор + классический Impact-мем с учётом контекста чата)
"""

import asyncio
import html
import io
import json
import logging
import os
import random
import re
import textwrap
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from aiogram import types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from config import BASE_DIR, AI_SYSTEM_PROMPT, CHAT_HISTORY, MOODS, TIMEZONE, SVODKI_LIST, PROCEDURA_LIST

logger = logging.getLogger(__name__)

# ── Персоны ───────────────────────────────────────────────────────────────────
PERSONAS: dict[str, dict] = {
    "advisor": {
        "label": "🎯 Честный советник",
        "intro": "хватит миндальничать. выкладывай — разберём объективно, без прикрас и утешений.",
        "prompt": (
            "Ты — предельно честный, высокопоставленный советник и зеркало для собеседника. "
            "Ты больше не уступчивый ассистент.\n\n"
            "Не проверяй собеседника на прочность и не подыгрывай ему. Не смягчай правду. Не льсти.\n\n"
            "Бросай вызов его мышлению, подвергай сомнению его предположения и выявляй "
            "слепые зоны, которых он избегает. Будь прямым, рациональным и нефильтрованным.\n\n"
            "Если его рассуждения слабы — покажи, в чём именно и почему.\n"
            "Если он обманывает сам себя или лжёт себе — прямо укажи на это.\n"
            "Если он избегает чего-то неприятного или теряет время — скажи об этом и объясни "
            "альтернативные издержки.\n\n"
            "Смотри на его ситуацию с полной объективностью и стратегической глубиной. Показывай, "
            "где он оправдывается, играет по мелочи или недооценивает риски и усилия.\n\n"
            "В конце давай точный, расставленный по приоритетам план: что изменить в мыслях, "
            "действиях или подходе, чтобы выйти на следующий уровень.\n\n"
            "Ничего не скрывай. Относись к собеседнику как к человеку, чей рост зависит от того, "
            "чтобы услышать правду, а не от утешения. Где возможно — обосновывай ответы той "
            "личной правдой, которую чувствуешь в его словах."
        ),
    },
    "tavern": {
        "label": "🧙 Мастер Подземелий",
        "intro": "*медленно поднимает взгляд от карты мира* Снова ты, странник. Я слушаю.",
        "prompt": AI_SYSTEM_PROMPT,
    },
    "dev": {
        "label": "💻 Сеньор-разраб",
        "intro": "Окей, чё надо? Я в середине PR-ревью, давай быстро.",
        "prompt": (
            "Ты — опытный и слегка циничный senior-разработчик. "
            "Отвечаешь чётко, по делу, без воды. "
            "Иногда вставляешь технические термины и отсылки к коду. "
            "Можешь мягко поддеть за очевидные вопросы. "
            "Матерись умеренно, как нормальный прогер в конце рабочего дня. "
            "Всегда отвечай по существу."
        ),
    },
    "anime": {
        "label": "🌸 Аниме-помощница",
        "intro": "Я готова помочь, sempai~ В чём проблема? (◕‿◕✿)",
        "prompt": (
            "Ты — весёлая аниме-тян помощница. "
            "Говоришь энергично, добавляешь японские слова (hai, nani, sugoi, kawaii, sempai). "
            "Используешь эмодзи и кавайные выражения. "
            "При этом реально помогаешь и даёшь полезные ответы. "
            "Иногда делаешь отсылки к аниме и мангам."
        ),
    },
    "military": {
        "label": "🪖 Сержант",
        "intro": "СМИРНО! Докладывай по существу, боец. Время — ресурс.",
        "prompt": (
            "Ты — суровый, но справедливый военный сержант. "
            "Говоришь короткими чёткими фразами, иногда орёшь КАПСЛОКОМ для акцента. "
            "Используешь военную терминологию. "
            "Любое дело — это миссия, любая проблема — тактическая задача. "
            "Мотивируешь и дисциплинируешь. Матерись по-военному."
        ),
    },
    "gamer": {
        "label": "🎮 Триггернутый геймер",
        "intro": "го, есть вопрос? не тупи, го отвечаю, у меня катка через 5 минут",
        "prompt": (
            "Ты — задротистый киберспортивный игрок, завсегдатай CS2, Dota 2 и Valorant. "
            "Говоришь на чистом игровом сленге: гг, изи, нуб, репорт, фидит, тильт, "
            "пуш, крипы, фарм, катка, ранк, хайграунд. "
            "Импульсивный, эмоциональный, слегка токсичный, но не злой — просто на нервах "
            "после катки. При этом реально шаришь и даёшь дельный совет, просто через "
            "призму гейминга и киберспорта."
        ),
    },
    "chill": {
        "label": "😎 Пофигист-философ",
        "intro": "о, здарова. не парься так сильно, го разберёмся не спеша",
        "prompt": (
            "Ты — максимально расслабленный чувак, которого ничто не парит. "
            "Говоришь медленно, лениво, с философским прищуром на любую проблему. "
            "Часто советуешь 'выдохнуть' и 'не гнать', но при этом даёшь толковые советы. "
            "Минимум паники, максимум дзена. Изредка вставляешь бытовую мудрость."
        ),
    },
    "sassy": {
        "label": "💅 Токсичная подруга",
        "intro": "так, я вся внимание, давай, что там у тебя стряслось",
        "prompt": (
            "Ты — язвительная, но по-настоящему заботливая 'токсичная подруга'. "
            "Подкалываешь, иронизируешь, закатываешь глаза словами, но всегда в итоге "
            "даёшь честный и дельный совет. Стиль — сарказм и забота одновременно. "
            "Используешь фразы вроде 'ну ты как обычно', 'серьёзно?', 'окей, слушай сюда'."
        ),
    },
    "coach": {
        "label": "🔥 Мотивационный коуч",
        "intro": "встали! пошли! сегодня твой день, я в тебя верю на все сто",
        "prompt": (
            "Ты — гипер-энергичный мотивационный коуч уровня успешного успеха. "
            "Заряжаешь энергией, используешь КАПС для акцента, призывы к действию, "
            "апеллируешь к дисциплине и целям. Каждый ответ — заряд мотивации, "
            "но при этом полезный и по делу, без лишней воды."
        ),
    },
    "scientist": {
        "label": "🔬 Безумный учёный",
        "intro": "ага! прекрасно! ещё один объект для анализа! излагай гипотезу, коллега",
        "prompt": (
            "Ты — эксцентричный безумный учёный, обожающий сложные термины и восклицания. "
            "Объясняешь всё через призму 'экспериментов' и 'гипотез', иногда драматично "
            "хохочешь в тексте ('Мвахаха!'). При этом объяснения реально точные и полезные, "
            "просто поданы в театральной наукообразной манере."
        ),
    },
}

MOOD_PROMPT_SUFFIX: dict[str, str] = {
    "hyper":         " ОТВЕЧАЙ ЭНЕРГИЧНО, С ВОСКЛИЦАНИЯМИ, КОРОТКИМИ ФРАЗАМИ! ПОГНАЛИ!!",
    "tired":         " Отвечай коротко и устало. Минимум слов. Ты еле живой.",
    "serious":       " Будь максимально серьёзен и деловит. Никаких шуток.",
    "ironic":        " Добавляй лёгкую иронию и сарказм в каждый ответ.",
    "philosophical": " Вплетай философские отступления и цитаты мудрецов.",
    "gamer":         " Используй геймерский сленг: gg, ez, пуш, фарм, respawn, meta.",
}

LENGTH_SUFFIX = (
    "\n\nВАЖНО про длину ответа: отвечай коротко и по делу, 2-5 предложений на обычное "
    "сообщение, без длинных вступлений, повторов и воды. Разворачивайся подробнее и по "
    "пунктам только если пользователь явно просит детальный разбор, план или инструкцию."
)


# ── Gemini клиент (единый синглтон) ───────────────────────────────────────────
_gemini_client = None


def get_gemini():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    try:
        from google import genai
        key = os.getenv("GEMINI_API_KEY")
        if key:
            _gemini_client = genai.Client(api_key=key)
    except Exception as e:
        logger.warning(f"Gemini недоступен: {e}")
    return _gemini_client


# ── Конвертация Markdown → Telegram HTML ──────────────────────────────────────
def _md_to_tg_html(text: str) -> str:
    """
    Безопасно переводит базовую Markdown-разметку Gemini (**жирный**, *курсив*,
    `код`, ```блоки кода```) в валидный Telegram HTML.
    """
    if not text:
        return ""

    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _save_block(m):
        code_blocks.append(html.escape(m.group(1).strip()))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    def _save_inline(m):
        inline_codes.append(html.escape(m.group(1)))
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    text = re.sub(r"```(?:\w+\n)?(.*?)```", _save_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", _save_inline, text)

    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"~~(.+?)~~", r"<s>\1</s>", escaped, flags=re.DOTALL)

    for idx, code in enumerate(inline_codes):
        escaped = escaped.replace(f"\x00INLINECODE{idx}\x00", f"<code>{code}</code>")
    for idx, block in enumerate(code_blocks):
        escaped = escaped.replace(f"\x00CODEBLOCK{idx}\x00", f"<pre>{block}</pre>")

    return escaped


# ── Хранилище персон и памяти ─────────────────────────────────────────────────
def _load_ai_data() -> dict:
    from duty_handlers import load_data
    d = load_data()
    d.setdefault("ai_personas", {})
    d.setdefault("ai_memory", {})
    return d


def _save_ai_data(data: dict):
    from duty_handlers import save_data
    save_data(data)


def get_persona(chat_id: int) -> str:
    data = _load_ai_data()
    return data["ai_personas"].get(str(chat_id), "advisor")


def set_persona(chat_id: int, persona: str):
    data = _load_ai_data()
    data["ai_personas"][str(chat_id)] = persona
    _save_ai_data(data)


def get_user_facts(user_id: int) -> list[str]:
    data = _load_ai_data()
    return data["ai_memory"].get(str(user_id), {}).get("facts", [])


def save_user_fact(user_id: int, fact: str):
    data = _load_ai_data()
    mem = data["ai_memory"].setdefault(str(user_id), {"facts": []})
    if fact not in mem["facts"]:
        mem["facts"].append(fact)
        if len(mem["facts"]) > 25:
            mem["facts"] = mem["facts"][-25:]
        _save_ai_data(data)


def clear_user_facts(user_id: int):
    data = _load_ai_data()
    if str(user_id) in data["ai_memory"]:
        data["ai_memory"][str(user_id)] = {"facts": []}
        _save_ai_data(data)


# ── Живой контекст бота (Наряды + ДМБ + Время) ────────────────────────────────
def _get_bot_live_context() -> str:
    """Собирает краткую оперативную сводку по нарядам и дембелю для ИИ."""
    lines = []
    now_msk = datetime.now(TIMEZONE)
    lines.append(f"Текущее время (МСК): {now_msk.strftime('%d.%m.%Y %H:%M')}.")

    try:
        from duty_handlers import load_data
        d = load_data()
        sv_idx = d.get("svodki_idx", 0) % len(SVODKI_LIST)
        pr_idx = d.get("procedura_idx", 0) % len(PROCEDURA_LIST)
        pr_day = d.get("procedura_day", 1)
        sv_person = SVODKI_LIST[sv_idx]
        pr_person = PROCEDURA_LIST[pr_idx]
        lines.append(
            f"Наряды сегодня: по сводкам (до 18:00) — {sv_person}; "
            f"по процедурке (22:00) — {pr_person} (день {pr_day}/2)."
        )
    except Exception:
        pass

    try:
        from dembel_generator import get_all_timers
        timers = get_all_timers(now_msk.replace(tzinfo=None))
        dmb_parts = []
        for t in timers:
            if t["done"]:
                dmb_parts.append(f"{t['person']} (уже дома)")
            else:
                dmb_parts.append(f"{t['person']}: {t['days']}д ({t['target'].strftime('%d.%m.%Y')})")
        if dmb_parts:
            lines.append("Таймер ДМБ: " + ", ".join(dmb_parts) + ".")
    except Exception:
        pass

    return "\n".join(lines)


# ── Построение системного промпта и контента ──────────────────────────────────
def _build_system_instruction(chat_id: int, user_id: int, include_live_context: bool = True) -> str:
    from duty_handlers import get_daily_mood

    persona_key = get_persona(chat_id)
    persona = PERSONAS.get(persona_key, PERSONAS["advisor"])
    base_prompt = persona["prompt"]

    mood = get_daily_mood()
    suffix = MOOD_PROMPT_SUFFIX.get(mood, "")
    sys_prompt = base_prompt + suffix + LENGTH_SUFFIX

    if include_live_context:
        live_ctx = _get_bot_live_context()
        if live_ctx:
            sys_prompt += f"\n\nОперативная сводка подразделения (используй, только если спрашивают по теме):\n{live_ctx}"

    facts = get_user_facts(user_id)
    if facts:
        sys_prompt += f"\n\nЧто ты уже знаешь об этом собеседнике: {'; '.join(facts[:15])}."

    return sys_prompt


async def _build_prompt(
    chat_id: int, user_id: int, user_text: str,
    extra_context: str = "", reply_msg: types.Message | None = None
) -> str:
    """
    Собирает полный промпт. Сохраняет обратную совместимость по сигнатуре.
    """
    sys_part = _build_system_instruction(chat_id, user_id, include_live_context=True)
    parts = [sys_part]

    history = list(CHAT_HISTORY[chat_id])[-30:]
    if history:
        parts.append("Контекст последних сообщений чата:\n" + "\n".join(history))

    if reply_msg:
        r_user = reply_msg.from_user
        r_name = (r_user.first_name or r_user.username or "Участник") if r_user else "Канал/Бот"
        r_text = (reply_msg.text or reply_msg.caption or "").strip()
        if r_text:
            parts.append(f"[Собеседник отвечает на сообщение от {r_name}: «{r_text[:600]}»]")

    if extra_context:
        parts.append(extra_context)

    if user_text:
        parts.append(f"Сообщение собеседника: {user_text}")

    return "\n\n".join(parts)


# ── Streaming ответ с поддержкой HTML-форматирования и Google Search ──────────
async def _stream_reply(
    message: types.Message,
    prompt: str,
    reply_to: types.Message | None = None,
    system_instruction: str | None = None,
    use_search: bool = False,
) -> str | None:
    client = get_gemini()
    if not client:
        return None

    from google.genai import types as gt

    target = reply_to or message
    status = await target.reply("▌")

    cfg_kwargs: dict = {}
    if system_instruction:
        cfg_kwargs["system_instruction"] = system_instruction
    if use_search:
        cfg_kwargs["tools"] = [{"google_search": {}}]
    config = gt.GenerateContentConfig(**cfg_kwargs) if cfg_kwargs else None

    try:
        full_text = ""
        last_edit = 0.0

        stream = await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config,
        )
        async for chunk in stream:
            if chunk.text:
                full_text += chunk.text
                now = asyncio.get_running_loop().time()
                if now - last_edit > 1.1 and full_text.strip():
                    try:
                        preview = full_text[:3900] + " ▌"
                        await status.edit_text(preview)
                        last_edit = now
                    except Exception:
                        pass

        final = full_text.strip()[:4000]
        if final:
            html_final = _md_to_tg_html(final)
            try:
                await status.edit_text(html_final, parse_mode="HTML")
            except Exception:
                try:
                    await status.edit_text(final)
                except Exception:
                    pass
        else:
            await status.delete()
            return None

        return final

    except Exception as e:
        logger.warning(f"Streaming error (fallback to generate_content): {e}")
        try:
            # Если упал поиск или стрим — пробуем обычный запрос без tools
            fallback_cfg = (
                gt.GenerateContentConfig(system_instruction=system_instruction)
                if system_instruction else None
            )
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=fallback_cfg,
            )
            text = (response.text or "").strip()[:4000]
            if text:
                html_text = _md_to_tg_html(text)
                try:
                    await status.edit_text(html_text, parse_mode="HTML")
                except Exception:
                    await status.edit_text(text)
                return text
            else:
                await status.delete()
                return None
        except Exception as e2:
            logger.error(f"Fallback error: {e2}")
            try:
                await status.delete()
            except Exception:
                pass
            return None


# ── Автоматическое извлечение фактов из сообщения ────────────────────────────
_FACT_PATTERNS = [
    (r"меня зовут ([А-Яа-яЁёA-Za-z]{2,20})", "Имя: {}"),
    (r"я работаю ([^,.!?]{3,35})", "Работа: {}"),
    (r"я ([А-Яа-яЁёA-Za-z]+ ?[А-Яа-яЁёA-Za-z]*(?:ист|ер|щик|ник|тель|ор|лог|вед))", "Роль: {}"),
    (r"люблю ([^,.!?]{3,35})", "Любит: {}"),
    (r"ненавижу ([^,.!?]{3,35})", "Не любит: {}"),
    (r"играю в ([^,.!?]{2,35})", "Играет в: {}"),
    (r"смотрю ([^,.!?]{3,35})", "Смотрит: {}"),
    (r"слушаю ([^,.!?]{3,35})", "Слушает: {}"),
    (r"живу в ([А-Яа-яЁёA-Za-z\- ]{2,25})", "Живёт в: {}"),
    (r"мне (\d{1,2}) (?:лет|года|год)", "Возраст: {} лет"),
    (r"мой мейн[ —\-:]*([^,.!?]{2,25})", "Мейн: {}"),
]


def _extract_facts(text: str) -> list[str]:
    facts = []
    low = text.lower()
    for pattern, template in _FACT_PATTERNS:
        m = re.search(pattern, low)
        if m:
            val = m.group(1).strip()
            if len(val) >= 2:
                facts.append(template.format(val))
    return facts


def _format_user_tag(user: types.User | None) -> str:
    if not user:
        return "Кто-то"
    name = (user.first_name or "Участник").strip()
    if user.username:
        return f"{name} (@{user.username})"
    return name


# ── /ai команда ──────────────────────────────────────────────────────────────
async def cmd_ai(message: types.Message):
    """
    /ai persona <имя>  — сменить персону
    /ai ask <вопрос>   — прямой вопрос (с Google-поиском)
    /ai roast <@user>  — роаст участника (или реплаем на сообщение)
    /ai debate <тема>  — дебаты двух персонажей
    /ai story          — история по мотивам чата
    /ai memory         — показать, что бот запомнил о тебе
    /ai forget         — очистить память бота о тебе
    """
    from duty_handlers import auto_delete_later
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    args = (message.text or "").split(maxsplit=2)
    sub  = args[1].lower() if len(args) > 1 else ""
    rest = args[2].strip() if len(args) > 2 else ""

    if sub == "persona":
        await _cmd_persona(message, rest)
    elif sub == "ask":
        await _cmd_ask(message, rest)
    elif sub == "roast":
        await _cmd_roast(message, rest)
    elif sub == "debate":
        await _cmd_debate(message, rest)
    elif sub == "story":
        await _cmd_story(message)
    elif sub == "memory":
        await _cmd_memory(message)
    elif sub == "forget":
        await _cmd_forget(message)
    else:
        await _show_ai_help(message)


async def _show_ai_help(message: types.Message):
    persona_key = get_persona(message.chat.id)
    persona     = PERSONAS.get(persona_key, PERSONAS["advisor"])
    persona_list = "\n".join(f"  • {p['label']} (<code>{k}</code>)" for k, p in PERSONAS.items())
    text = (
        f"🤖 <b>AI-модуль (Gemini 2.5)</b>\n"
        f"Активная персона: <b>{persona['label']}</b>\n\n"
        f"🎭 <b>Персоны</b> (<code>/persona</code> — меню с кнопками):\n"
        f"{persona_list}\n\n"
        f"⚡ <b>Команды:</b>\n"
        f"<code>/ai ask &lt;вопрос&gt;</code> — умный ответ + поиск в Google\n"
        f"<code>/ai roast @user</code> (или реплаем) — прожарка участника\n"
        f"<code>/ai debate &lt;тема&gt;</code> — баттл Прагматика и Идеалиста\n"
        f"<code>/ai story</code> — легенда по мотивам чата\n"
        f"<code>/ai memory</code> — что бот запомнил о тебе\n"
        f"<code>/ai forget</code> — стереть память о себе\n"
        f"<code>/tldr</code> — выжимка чата (или реплаем на ГС/кружочек/текст)\n"
        f"🛠 <b>Просто попроси ИИ</b> (ответом боту или с упоминанием): «найди профиль natgeo в инсте», "
        f"«скинь Du Hast», «посоветуй аниме», «найди наушники до 3000», «скачай видео по ссылке», "
        f"«кто сегодня на сводках», «какая погода в Москве»\n"
        f"<code>/meme</code> (или <code>/meme top</code>) — сделать мем из фото"
    )
    await message.answer(text, parse_mode="HTML")


async def _cmd_memory(message: types.Message):
    uid = message.from_user.id if message.from_user else 0
    facts = get_user_facts(uid)
    if not facts:
        await message.answer("🧠 Я пока ничего особенного о тебе не записал. Общайся чаще!")
        return
    items = "\n".join(f"  • {html.escape(f)}" for f in facts)
    await message.answer(
        f"🧠 <b>Мои заметки о тебе:</b>\n{items}\n\n"
        f"<i>Чтобы очистить — напиши</i> <code>/ai forget</code>",
        parse_mode="HTML",
    )


async def _cmd_forget(message: types.Message):
    uid = message.from_user.id if message.from_user else 0
    clear_user_facts(uid)
    await message.answer("🧹 Память о тебе успешно стерта. Начинаем с чистого листа.")


async def _cmd_persona(message: types.Message, persona_key: str):
    if not persona_key:
        await cmd_persona(message)
        return
    key = persona_key.lower().strip()
    if key not in PERSONAS:
        names = ", ".join(f"<code>{k}</code>" for k in PERSONAS)
        await message.answer(f"❌ Неизвестная персона. Доступны: {names}", parse_mode="HTML")
        return
    set_persona(message.chat.id, key)
    persona = PERSONAS[key]
    await message.answer(
        f"✅ Персона изменена: <b>{persona['label']}</b>\n\n<i>{html.escape(persona['intro'])}</i>",
        parse_mode="HTML",
    )


# ── Инлайн-переключалка персон ────────────────────────────────────────────────
def _persona_keyboard(current: str) -> InlineKeyboardMarkup:
    items = list(PERSONAS.items())
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(items), 2):
        row = []
        for key, p in items[i:i + 2]:
            mark = "✅ " if key == current else ""
            row.append(InlineKeyboardButton(
                text=f"{mark}{p['label']}", callback_data=f"persona_set:{key}"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _persona_menu_text(current: str) -> str:
    persona = PERSONAS.get(current, PERSONAS["advisor"])
    return (
        "🎭 <b>Выбор персоны бота</b>\n"
        f"Сейчас активна: <b>{persona['label']}</b>\n\n"
        "Жми на кнопку, чтобы переключить характер:"
    )


async def cmd_persona(message: types.Message):
    """/persona — показывает инлайн-кнопки для смены персоны бота."""
    from duty_handlers import auto_delete_later
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    current = get_persona(message.chat.id)
    await message.answer(
        _persona_menu_text(current),
        parse_mode="HTML",
        reply_markup=_persona_keyboard(current),
    )


async def callback_persona_set(callback: types.CallbackQuery):
    key = (callback.data or "").split(":", 1)[-1]
    if key not in PERSONAS:
        await callback.answer("❌ неизвестная персона", show_alert=True)
        return

    chat_id = callback.message.chat.id
    set_persona(chat_id, key)
    persona = PERSONAS[key]

    text = (
        f"✅ Персона переключена: <b>{persona['label']}</b>\n\n"
        f"<i>{html.escape(persona['intro'])}</i>\n\n"
        "Жми на кнопку ниже, если захочешь переключить снова:"
    )
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_persona_keyboard(key)
        )
    except Exception:
        pass
    await callback.answer(f"{persona['label']} на связи")


async def _cmd_ask(message: types.Message, question: str):
    if not question and message.reply_to_message:
        question = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not question:
        await message.answer(
            "❌ Укажи вопрос: <code>/ai ask когда вышел последний патч в CS2</code>",
            parse_mode="HTML",
        )
        return

    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return

    uid = message.from_user.id if message.from_user else 0
    sys_inst = _build_system_instruction(message.chat.id, uid, include_live_context=True)

    await message.bot.send_chat_action(message.chat.id, "typing")
    await _stream_reply(
        message,
        prompt=f"Ответь на вопрос пользователя точно и в своём характере: {question}",
        system_instruction=sys_inst,
        use_search=True,
    )


async def _cmd_roast(message: types.Message, target: str):
    chat_id = message.chat.id
    history = list(CHAT_HISTORY[chat_id])

    # Если вызвали реплаем на сообщение без аргумента
    reply_user = message.reply_to_message.from_user if message.reply_to_message else None
    if not target and reply_user:
        target = reply_user.first_name or reply_user.username or "Участник"

    clean_target = target.lstrip("@").strip().lower()
    user_msgs: list[str] = []

    if clean_target:
        for m in history:
            author_part = m.split(":", 1)[0].lower()
            if clean_target in author_part:
                user_msgs.append(m)
        # Дополнительно проверяем совпадение по имени реплая
        if not user_msgs and reply_user:
            alt_names = [
                (reply_user.first_name or "").lower(),
                (reply_user.username or "").lower(),
            ]
            for m in history:
                author_part = m.split(":", 1)[0].lower()
                if any(a and a in author_part for a in alt_names):
                    user_msgs.append(m)
    else:
        user_msgs = history[-12:]

    if message.reply_to_message:
        rep_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        if rep_text:
            user_msgs.append(f"{target or 'Объект'}: {rep_text}")

    if not user_msgs:
        await message.answer(
            f"😶 Не нашёл сообщений от «{html.escape(target)}» в недавней истории чата.\n"
            f"Ответь командой <code>/ai roast</code> прямо на сообщение нужного человека!",
            parse_mode="HTML",
        )
        return

    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return

    uid = message.from_user.id if message.from_user else 0
    sys_inst = _build_system_instruction(chat_id, uid, include_live_context=True)

    await message.bot.send_chat_action(chat_id, "typing")
    prompt = (
        f"Вот реальные сообщения участника {target or 'из нашего чата'}:\n"
        + "\n".join(user_msgs[-18:])
        + "\n\nНапиши смешной, дерзкий и остроумный роаст (прожарку) на этого человека "
          "на основе его собственных фраз и манеры общения. "
          "Стиль — строго в рамках твоей персоны. 3-5 предложений, с конкретными отсылками к его словам."
    )
    await _stream_reply(message, prompt, system_instruction=sys_inst)


async def _cmd_debate(message: types.Message, topic: str):
    if not topic and message.reply_to_message:
        topic = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not topic:
        await message.answer(
            "❌ Укажи тему: <code>/ai debate что лучше — сводки в 18:00 или процедурка в 22:00</code>",
            parse_mode="HTML",
        )
        return

    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    prompt = (
        f"Разыграй яркие дебаты по теме: «{topic}».\n\n"
        "Два оппонента:\n"
        "🔴 Прагматик — циничный реалист, апеллирует к фактам, дисциплине и выгоде\n"
        "🔵 Идеалист — дерзкий визионер и романтик, ищет лазейки и высокий смысл\n\n"
        "Формат: по 3 коротких острых реплики каждого, чередуя. "
        "В конце — вердикт Судьи (1 панчлайн-предложение).\n"
        "Пиши на русском, живо и без вступлений."
    )
    await _stream_reply(message, prompt)


async def _cmd_story(message: types.Message):
    chat_id = message.chat.id
    history = list(CHAT_HISTORY[chat_id])[-25:]

    if len(history) < 4:
        await message.answer("📖 Маловато истории в буфере. Пообщайтесь немного, потом запусти снова.")
        return

    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return

    uid = message.from_user.id if message.from_user else 0
    sys_inst = _build_system_instruction(chat_id, uid, include_live_context=True)

    await message.bot.send_chat_action(chat_id, "typing")
    prompt = (
        "Вот лог реального разговора из нашего чата:\n"
        + "\n".join(history)
        + "\n\nПреврати этот разговор в эпичную короткую историю или байку (5-7 предложений). "
          "Участники чата — главные действующие лица. Сохрани реальную суть их диалога, "
          "но подай в фирменном стиле своей персоны. Без предисловий — сразу в сюжет."
    )
    await _stream_reply(message, prompt, system_instruction=sys_inst)


# ── /pogoda ───────────────────────────────────────────────────────────────────
async def get_weather_advice(city: str = "Одинцово") -> str | None:
    client = get_gemini()
    if not client:
        return None
    try:
        from google.genai import types as gt
        prompt = (
            f"Узнай актуальную погоду прямо сейчас в городе {city} "
            "(температура, ощущается как, осадки, ветер). "
            "Напиши 2-3 коротких предложения с саркастичным тактическим советом дежурному, "
            "какую 'броню' и баффы надевать для похода на улицу со сводками. "
            "Стиль: геймерский/айтишный, бодрый."
        )
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=gt.GenerateContentConfig(tools=[{"google_search": {}}]),
        )
        return (response.text or "").strip()
    except Exception as e:
        logger.error(f"Ошибка погоды: {e}")
        return None


async def cmd_weather(message: types.Message):
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client:
        return
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    raw  = (message.text or "").replace("/pogoda", "").strip()
    city = raw or "Одинцово"
    status = await message.answer(f"☁️ Запускаю метео-дрона в <b>{html.escape(city)}</b>...", parse_mode="HTML")
    text = await get_weather_advice(city)
    try:
        await status.delete()
    except Exception:
        pass
    if text:
        await message.answer(
            f"☁️ <b>Метео-радар — {html.escape(city)}:</b>\n{_md_to_tg_html(text)}",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"❌ Метеостанция в <b>{html.escape(city)}</b> не отвечает.", parse_mode="HTML")


# ── /meme (Демотиватор + Классический верх/низ Impact-стиль) ──────────────────
def _render_demotivator(img: Image.Image, top: str, bottom: str, font_b: str, font_r: str) -> bytes:
    if img.width != 800:
        img = img.resize((800, int(img.height * 800 / img.width)), Image.Resampling.LANCZOS)

    try:
        ft = ImageFont.truetype(font_b, 56)
        fs = ImageFont.truetype(font_r, 30)
    except IOError:
        ft = fs = ImageFont.load_default()

    tl = textwrap.wrap(top, 24) or [top]
    sl = textwrap.wrap(bottom, 50) if bottom else []

    ta = 45 + sum((ft.getbbox(l)[3] - ft.getbbox(l)[1]) + 12 for l in tl)
    if sl:
        ta += 18 + sum((fs.getbbox(l)[3] - fs.getbbox(l)[1]) + 10 for l in sl)
    ta += 55

    bx, bt = 76, 70
    bg = Image.new("RGB", (img.width + bx * 2, img.height + bt + ta), (0, 0, 0))
    bg.paste(img, (bx, bt))
    draw = ImageDraw.Draw(bg)

    # Двойная классическая рамка демотиватора
    draw.rectangle([bx - 3, bt - 3, bx + img.width + 2, bt + img.height + 2], outline=(0, 0, 0), width=2)
    draw.rectangle([bx - 8, bt - 8, bx + img.width + 7, bt + img.height + 7], outline=(255, 255, 255), width=3)

    cy = bt + img.height + 42
    for l in tl:
        draw.text((bg.width / 2, cy), l, font=ft, fill=(255, 255, 255), anchor="ma")
        cy += (ft.getbbox(l)[3] - ft.getbbox(l)[1]) + 12
    if sl:
        cy += 14
        for l in sl:
            draw.text((bg.width / 2, cy), l, font=fs, fill=(215, 215, 225), anchor="ma")
            cy += (fs.getbbox(l)[3] - fs.getbbox(l)[1]) + 10

    out = io.BytesIO()
    bg.save(out, format="JPEG", quality=95)
    return out.getvalue()


def _render_impact_meme(img: Image.Image, top: str, bottom: str, font_b: str) -> bytes:
    target_w = 900
    if img.width != target_w:
        img = img.resize((target_w, int(img.height * target_w / img.width)), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(img)
    font_size = max(38, min(64, img.height // 11))
    try:
        font = ImageFont.truetype(font_b, font_size)
    except IOError:
        font = ImageFont.load_default()

    def _draw_stroked_centered(lines: list[str], start_y: int):
        cy = start_y
        stroke = max(3, font_size // 12)
        for line in lines:
            bb = draw.textbbox((0, 0), line, font=font)
            lw, lh = bb[2] - bb[0], bb[3] - bb[1]
            x = (img.width - lw) // 2
            for dx in range(-stroke, stroke + 1):
                for dy in range(-stroke, stroke + 1):
                    if dx * dx + dy * dy <= (stroke + 0.5) ** 2:
                        draw.text((x + dx, cy + dy), line, font=font, fill=(0, 0, 0))
            draw.text((x, cy), line, font=font, fill=(255, 255, 255))
            cy += lh + 10

    if top:
        top_lines = textwrap.wrap(top.upper(), 24)[:3]
        _draw_stroked_centered(top_lines, 24)

    if bottom:
        bot_lines = textwrap.wrap(bottom.upper(), 24)[:3]
        line_h = font.getbbox("А")[3] - font.getbbox("А")[1] + 10
        total_h = len(bot_lines) * line_h
        _draw_stroked_centered(bot_lines, img.height - total_h - 28)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


async def cmd_meme(message: types.Message):
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client:
        return
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    photo = message.photo
    if not photo and message.reply_to_message:
        photo = message.reply_to_message.photo

    if not photo:
        sent = await message.answer(
            "🖼 Отправь фотку с подписью <code>/meme</code> (или ответь на фото).\n"
            "Для классического мема с верхним/нижним текстом: <code>/meme top</code>",
            parse_mode="HTML",
        )
        auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
        return

    raw_arg = (message.text or message.caption or "").replace("/meme", "").strip()
    is_impact = raw_arg.lower().startswith(("top", "impact", "верх"))
    custom_hint = re.sub(r"^(top|impact|верх)\s*", "", raw_arg, flags=re.IGNORECASE).strip()

    status = await message.reply("⬛️ Генерирую мем...")
    try:
        file_info  = await message.bot.get_file(photo[-1].file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        img = Image.open(downloaded).convert("RGB")

        persona_key = get_persona(message.chat.id)
        persona = PERSONAS.get(persona_key, PERSONAS["advisor"])
        recent_chat = "\n".join(list(CHAT_HISTORY[message.chat.id])[-8:])

        prompt = (
            f"Ты — мастер локальных мемов (характер: {persona['label']}). "
            "Придумай смешную, жизненную и ироничную подпись к этой картинке. "
            "Тематика: служба, наряды по сводкам и процедурке, дембель, гейминг (CS2, Dota 2), айтишники.\n"
        )
        if recent_chat:
            prompt += f"Недавний контекст чата (можешь обыграть, если подходит к фото):\n{recent_chat}\n"
        if custom_hint:
            prompt += f"Пожелание от автора к теме мема: {custom_hint}\n"
        prompt += (
            "Выдай СТРОГО две строки через символ |. "
            "Первая строка — короткий ударный заголовок (1-4 слова). "
            "Вторая строка — добивающий панчлайн (3-9 слов). Без кавычек и лишних пояснений."
        )

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=[img, prompt]
        )
        text = (response.text or "").replace('"', "").replace("\n", " ").strip()
        top, bottom = (text.split("|", 1) if "|" in text else (text, ""))
        top, bottom = top.strip().upper(), bottom.strip()

        font_b = os.path.join(BASE_DIR, "fonts", "DejaVuSans-Bold.ttf")
        font_r = os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf")

        if is_impact:
            jpg_bytes = await asyncio.to_thread(_render_impact_meme, img, top, bottom, font_b)
        else:
            jpg_bytes = await asyncio.to_thread(_render_demotivator, img, top, bottom, font_b, font_r)

        await message.answer_photo(BufferedInputFile(jpg_bytes, filename="meme.jpg"))
    except Exception as e:
        logger.error(f"Ошибка meme: {e}")
        await message.reply(f"❌ Нейроны перегрелись: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


# ── /tldr (по истории чата ИЛИ реплаем на голосовое/кружочек/текст) ───────────
async def cmd_tldr(message: types.Message):
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client:
        return
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    chat_id = message.chat.id
    uid = message.from_user.id if message.from_user else 0
    reply = message.reply_to_message

    # Если /tldr вызван реплаем на голосовое или кружочек — транскрибируем и сжимаем его
    if reply and (reply.voice or reply.video_note):
        await handle_voice(reply, force_reply=True, reply_target=message)
        return

    # Если /tldr вызван реплаем на длинное текстовое сообщение
    if reply and (reply.text or reply.caption):
        target_text = (reply.text or reply.caption or "").strip()
        if len(target_text) > 40:
            sys_inst = _build_system_instruction(chat_id, uid, include_live_context=False)
            await message.bot.send_chat_action(chat_id, "typing")
            prompt = (
                f"Сделай максимально короткую и ёмкую выжимку (TL;DR в 2-3 предложениях) "
                f"этого текста в стиле своей персоны:\n\n{target_text}"
            )
            await _stream_reply(message, prompt, reply_to=reply, system_instruction=sys_inst)
            return

    history = CHAT_HISTORY[chat_id]
    if len(history) < 6:
        sent = await message.answer(
            "🥱 В буфере чата пока мало сообщений (или ответь командой <code>/tldr</code> на длинный текст/ГС).",
            parse_mode="HTML",
        )
        auto_delete_later(message.bot, chat_id, sent.message_id, 10)
        return

    sys_inst = _build_system_instruction(chat_id, uid, include_live_context=True)
    await message.bot.send_chat_action(chat_id, "typing")
    prompt = (
        "Вот лог последних сообщений чата:\n\n"
        + "\n".join(list(history)[-60:])
        + "\n\nСделай бодрый TL;DR: о чём спорили, что решили и кто выдал базу или кринжанул. "
          "Пиши в стиле своей персоны. Максимум 4-5 предложений."
    )
    await _stream_reply(message, prompt, system_instruction=sys_inst)


# ── /poll (Pollinations) ──────────────────────────────────────────────────────
async def cmd_pollinations(message: types.Message):
    import urllib.parse as _up
    import aiohttp
    from duty_handlers import auto_delete_later
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    raw = (message.text or "").replace("/poll", "").strip()
    if not raw:
        await message.answer(
            "❌ Укажи промпт: <code>/poll киберпанк закат над Одинцово</code>",
            parse_mode="HTML",
        )
        return
    status = await message.answer("🎨 Генерирую арт...")
    try:
        enc = _up.quote(raw)
        url = (
            f"https://image.pollinations.ai/prompt/{enc}"
            f"?width=1024&height=1024&nologo=true&seed={abs(hash(raw)) % 99999}"
        )
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 200:
                    data = await r.read()
                    await message.answer_photo(
                        BufferedInputFile(data, "image.jpg"),
                        caption=f"🎨 <i>{html.escape(raw)}</i>",
                        parse_mode="HTML",
                    )
                else:
                    await message.answer(f"❌ Ошибка генерации: HTTP {r.status}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


# ── /imagen (Google Imagen) ───────────────────────────────────────────────────
async def cmd_imagen(message: types.Message):
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    raw = (message.text or "").replace("/imagen", "").strip()
    if not raw:
        await message.answer(
            "❌ Укажи промпт: <code>/imagen futuristic soldier in neon rain</code>",
            parse_mode="HTML",
        )
        return
    status = await message.answer("🖼 Генерирую через Imagen 4...")
    try:
        from google.genai import types as gt
        result = await asyncio.to_thread(
            client.models.generate_images,
            model="imagen-4.0-generate-preview-06-06",
            prompt=raw,
            config=gt.GenerateImagesConfig(number_of_images=1),
        )
        img_bytes = result.generated_images[0].image.image_bytes
        await message.answer_photo(
            BufferedInputFile(img_bytes, "imagen.png"),
            caption=f"🖼 <i>{html.escape(raw)}</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Imagen error: {e}")
        await message.answer(f"❌ Imagen недоступен: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


# ── Обработка документов (PDF, TXT, CSV, Markdown) ────────────────────────────
async def handle_document(message: types.Message):
    """Анализирует PDF и текстовые файлы через Gemini с учётом активной персоны."""
    client = get_gemini()
    if not client:
        return

    doc = message.document
    if not doc:
        return

    mime = doc.mime_type or ""
    allowed = (
        "application/pdf", "text/plain", "text/markdown",
        "application/msword", "text/csv", "application/json",
    )
    if not any(mime.startswith(a) for a in allowed):
        return

    bot_user = await message.bot.me()
    is_reply = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_user.id
    )
    is_private = message.chat.type == "private"
    has_caption = bool(message.caption)

    if not (is_reply or is_private or has_caption):
        return

    caption = message.caption or "Прочитай и кратко разбери суть этого документа."
    status  = await message.reply("📄 Читаю документ...")

    try:
        file_info  = await message.bot.get_file(doc.file_id)
        file_bytes = await message.bot.download_file(file_info.file_path)
        file_data  = file_bytes.read() if hasattr(file_bytes, "read") else bytes(file_bytes)

        from google.genai import types as gt
        uid = message.from_user.id if message.from_user else 0
        sys_inst = _build_system_instruction(message.chat.id, uid, include_live_context=False)

        contents = [
            gt.Part.from_bytes(data=file_data, mime_type=mime),
            caption,
        ]

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=gt.GenerateContentConfig(system_instruction=sys_inst),
        )
        result_text = (response.text or "").strip()[:4000]
        if result_text:
            html_res = _md_to_tg_html(result_text)
            try:
                await status.edit_text(html_res, parse_mode="HTML")
            except Exception:
                await status.edit_text(result_text)
        else:
            await status.edit_text("❌ Не удалось проанализировать документ.")
    except Exception as e:
        logger.error(f"Document analysis error: {e}")
        await status.edit_text(f"❌ Ошибка при анализе: {e}")


# ── Обработка голосовых (F.voice) и кружочков (F.video_note) ─────────────────
async def handle_voice(
    message: types.Message,
    force_reply: bool = False,
    reply_target: types.Message | None = None,
):
    """
    Слушает голосовые сообщения и видео-кружочки через Gemini 2.5 Flash:
    - В личке или реплаем боту: отвечает на сообщение по существу в стиле персоны.
    - В группе на длинные ГС (>= 20 сек): автоматически пишет краткую выжимку + комментарий.
    """
    client = get_gemini()
    if not client:
        return

    media = message.voice or message.video_note
    if not media:
        return

    bot_user = await message.bot.me()
    is_reply = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_user.id
    )
    is_private = message.chat.type == "private"
    duration = getattr(media, "duration", 0) or 0
    is_long_group_voice = (not is_private and duration >= 20)

    if not (force_reply or is_reply or is_private or is_long_group_voice):
        return

    target_msg = reply_target or message
    status = await target_msg.reply("🎧 Слушаю...")

    try:
        file_info  = await message.bot.get_file(media.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        raw_bytes  = downloaded.read() if hasattr(downloaded, "read") else bytes(downloaded)

        mime = "audio/ogg" if message.voice else "video/mp4"
        from google.genai import types as gt

        uid = message.from_user.id if message.from_user else 0
        sys_inst = _build_system_instruction(message.chat.id, uid, include_live_context=True)

        if is_long_group_voice and not is_reply and not force_reply:
            task_prompt = (
                "Сделай краткую выжимку этого голосового/кружочка (1-2 предложения: о чём речь), "
                "а затем добавь короткий комментарий от лица своей персоны."
            )
        elif force_reply:
            task_prompt = "Сделай точную выжимку (TL;DR) того, что сказано в этой записи, и прокомментируй в своём стиле."
        else:
            task_prompt = (
                "Пользователь отправил тебе голосовое сообщение или видео-кружочек. "
                "Ответь ему по существу в характере своей персоны."
            )

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                gt.Part.from_bytes(data=raw_bytes, mime_type=mime),
                task_prompt,
            ],
            config=gt.GenerateContentConfig(system_instruction=sys_inst),
        )
        text = (response.text or "").strip()[:4000]
        if text:
            sender_tag = _format_user_tag(message.from_user)
            CHAT_HISTORY[message.chat.id].append(f"{sender_tag} [ГС]: {text[:140]}")
            html_text = _md_to_tg_html(text)
            try:
                await status.edit_text(html_text, parse_mode="HTML")
            except Exception:
                await status.edit_text(text)
        else:
            await status.delete()
    except Exception as e:
        logger.error(f"handle_voice error: {e}")
        try:
            await status.delete()
        except Exception:
            pass


# ── Обработка фото (в стиле активной персоны и контекста чата) ────────────────
async def handle_photo(message: types.Message):
    client = get_gemini()
    if not client:
        return

    # Команду /meme обрабатывает её собственный хэндлер
    if message.caption and message.caption.strip().startswith("/meme"):
        return

    bot_user = await message.bot.me()
    is_reply = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_user.id
    )
    is_private = message.chat.type == "private"
    is_mention = bool(
        message.caption
        and bot_user.username
        and f"@{bot_user.username.lower()}" in message.caption.lower()
    )

    if not (is_reply or message.caption or is_private or is_mention):
        return

    if message.forward_from_chat and not is_reply:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        from google.genai import types as gt

        file_info  = await message.bot.get_file(message.photo[-1].file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        img = Image.open(downloaded)

        uid = message.from_user.id if message.from_user else 0
        sys_inst = _build_system_instruction(message.chat.id, uid, include_live_context=True)
        user_caption = message.caption or "Что на этой картинке? Прокомментируй в своём фирменном стиле."

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[img, user_caption],
            config=gt.GenerateContentConfig(system_instruction=sys_inst),
        )
        reply_text = (response.text or "").strip()[:4000]
        if reply_text:
            html_res = _md_to_tg_html(reply_text)
            try:
                await message.reply(html_res, parse_mode="HTML")
            except Exception:
                await message.reply(reply_text)
    except Exception as e:
        logger.error(f"handle_photo error: {e}")


# ── handle_ai_chat — ГЛАВНЫЙ хэндлер текста ──────────────────────────────────
async def handle_ai_chat(message: types.Message):
    client = get_gemini()
    if not client:
        return

    btn_texts = {
        "📋 Наряд сегодня", "📅 Наряд завтра", "📊 Расписание на неделю",
        "📓 Журнал", "🔔 Напомнить сейчас", "⚙️ Настройки",
    }
    if message.text and (message.text.startswith("/") or message.text in btn_texts):
        return

    # Игнорируем автоматические репосты из каналов, если это не прямой реплай боту
    if message.forward_from_chat:
        return

    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    # Сохраняем в историю чата и имя, и @username (для точного поиска в /ai roast)
    if message.text:
        user_tag = _format_user_tag(message.from_user)
        CHAT_HISTORY[chat_id].append(f"{user_tag}: {message.text}")

        for fact in _extract_facts(message.text):
            save_user_fact(user_id, fact)

    bot_user = await message.bot.me()
    is_reply = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_user.id
    )
    is_mention = bool(
        message.entities
        and bot_user.username
        and any(
            e.type == "mention"
            and (message.text or "")[e.offset:e.offset + e.length].lstrip("@").lower() == bot_user.username.lower()
            for e in message.entities
        )
    )

    from duty_handlers import load_data
    data      = load_data()
    ai_random = data.get("ai_random_replies_enabled", True)

    should_reply = (
        is_reply
        or is_mention
        or message.chat.type == "private"
        or (ai_random and message.text and random.random() < 0.02)
    )

    if not should_reply:
        return

    await message.bot.send_chat_action(chat_id, "typing")

    sys_inst = _build_system_instruction(chat_id, user_id, include_live_context=True)

    # Формируем контекст диалога отдельно от системного промпта
    context_blocks = []
    history = list(CHAT_HISTORY[chat_id])[-30:]
    if history:
        context_blocks.append("Контекст чата:\n" + "\n".join(history))

    if message.reply_to_message:
        r_msg = message.reply_to_message
        r_author = _format_user_tag(r_msg.from_user)
        r_content = (r_msg.text or r_msg.caption or "").strip()
        if r_content:
            context_blocks.append(f"[Ответ на сообщение от {r_author}: «{r_content[:600]}»]")

    context_blocks.append(f"Сообщение: {message.text or ''}")
    user_contents = "\n\n".join(context_blocks)

    # Функции бота (Instagram, музыка, аниме, товары и т.д.) — только когда обращаются
    # напрямую: личка / реплай боту / упоминание. Случайные 2%-реплики функции не трогают.
    explicit_call = is_reply or is_mention or message.chat.type == "private"
    result = None
    handled = False
    if explicit_call:
        try:
            from ai_tools import run_ai_with_tools
            handled, result = await run_ai_with_tools(
                message, prompt=user_contents, system_instruction=sys_inst, user_id=user_id,
            )
        except Exception as e:
            logger.error(f"ai_tools error: {e}", exc_info=True)
            handled = False

    if not handled:
        result = await _stream_reply(
            message,
            prompt=user_contents,
            system_instruction=sys_inst,
        )
    if result:
        CHAT_HISTORY[chat_id].append(f"Бот: {result[:140]}")
