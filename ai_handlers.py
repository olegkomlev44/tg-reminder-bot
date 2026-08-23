"""
ai_handlers.py — AI-функции бота на Gemini.

Улучшения v2:
  - Персоны (/ai persona): tavern, dev, anime, military
  - Настроение влияет на промпт
  - Память о пользователях (факты из разговора)
  - /ai ask, /ai roast, /ai debate, /ai story
  - Streaming-эффект (редактирование сообщения по мере ответа)
  - Анализ документов (PDF через Gemini)
  - Фикс: пересланные посты из каналов игнорируются
"""

import asyncio
import io
import json
import logging
import os
import re
import textwrap

from PIL import Image, ImageDraw, ImageFont
from aiogram import types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from config import BASE_DIR, AI_SYSTEM_PROMPT, CHAT_HISTORY, MOODS

logger = logging.getLogger(__name__)

# ── Персоны ───────────────────────────────────────────────────────────────────
PERSONAS: dict[str, dict] = {
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
}

# Суффиксы к промпту в зависимости от настроения
MOOD_PROMPT_SUFFIX: dict[str, str] = {
    "hyper":         " ОТВЕЧАЙ ЭНЕРГИЧНО, С ВОСКЛИЦАНИЯМИ, КОРОТКИМИ ФРАЗАМИ! ПОГНАЛИ!!",
    "tired":         " Отвечай коротко и устало. Минимум слов. Ты еле живой.",
    "serious":       " Будь максимально серьёзен и деловит. Никаких шуток.",
    "ironic":        " Добавляй лёгкую иронию и сарказм в каждый ответ.",
    "philosophical": " Вплетай философские отступления и цитаты мудрецов.",
    "gamer":         " Используй геймерский сленг: gg, ez, пуш, фарм, respawn, meta.",
}


# ── Gemini клиент ─────────────────────────────────────────────────────────────
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


# ── Хранилище персон и памяти (in-file) ──────────────────────────────────────
def _load_ai_data() -> dict:
    from duty_handlers import load_data
    d = load_data()
    d.setdefault("ai_personas", {})    # chat_id → persona_key
    d.setdefault("ai_memory", {})      # user_id → {"facts": [...]}
    return d

def _save_ai_data(data: dict):
    from duty_handlers import save_data
    save_data(data)

def get_persona(chat_id: int) -> str:
    data = _load_ai_data()
    return data["ai_personas"].get(str(chat_id), "tavern")

def set_persona(chat_id: int, persona: str):
    data = _load_ai_data()
    data["ai_personas"][str(chat_id)] = persona
    _save_ai_data(data)

def get_user_facts(user_id: int) -> list[str]:
    data = _load_ai_data()
    return data["ai_memory"].get(str(user_id), {}).get("facts", [])

def save_user_fact(user_id: int, fact: str):
    data = _load_ai_data()
    mem  = data["ai_memory"].setdefault(str(user_id), {"facts": []})
    if fact not in mem["facts"]:
        mem["facts"].append(fact)
        if len(mem["facts"]) > 20:
            mem["facts"] = mem["facts"][-20:]
    _save_ai_data(data)


# ── Построение промпта ────────────────────────────────────────────────────────
async def _build_prompt(chat_id: int, user_id: int, user_text: str,
                        extra_context: str = "") -> str:
    from duty_handlers import get_daily_mood

    persona_key = get_persona(chat_id)
    persona     = PERSONAS.get(persona_key, PERSONAS["tavern"])
    base_prompt = persona["prompt"]

    mood     = get_daily_mood()
    suffix   = MOOD_PROMPT_SUFFIX.get(mood, "")
    prompt   = base_prompt + suffix

    # Факты о пользователе
    facts = get_user_facts(user_id)
    if facts:
        prompt += f"\n\nТы помнишь об этом человеке: {'; '.join(facts[:10])}."

    # История чата
    history = list(CHAT_HISTORY[chat_id])[-30:]
    if history:
        prompt += "\n\nКонтекст чата:\n" + "\n".join(history)

    if extra_context:
        prompt += f"\n\n{extra_context}"

    if user_text:
        prompt += f"\n\nСообщение: {user_text}"

    return prompt


# ── Streaming ответ ───────────────────────────────────────────────────────────
async def _stream_reply(message: types.Message, prompt: str,
                        reply_to: types.Message | None = None) -> str | None:
    client = get_gemini()
    if not client:
        return None

    target = reply_to or message
    status = await target.reply("▌")

    try:
        full_text = ""
        last_edit = 0.0

        async for chunk in await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
        ):
            if chunk.text:
                full_text += chunk.text
                now = asyncio.get_event_loop().time()
                # Редактируем не чаще 1 раза в секунду
                if now - last_edit > 1.0 and full_text.strip():
                    try:
                        preview = full_text[:3900] + " ▌"
                        await status.edit_text(preview)
                        last_edit = now
                    except Exception:
                        pass

        # Финальное сообщение без курсора
        final = full_text.strip()[:4096]
        if final:
            try:
                await status.edit_text(final)
            except Exception:
                pass
        else:
            await status.delete()
            return None

        return final

    except Exception as e:
        logger.error(f"Streaming error: {e}")
        try:
            await status.delete()
        except Exception:
            pass
        # Fallback — обычный запрос
        try:
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            )
            text = (response.text or "").strip()
            if text:
                await target.reply(text)
            return text
        except Exception as e2:
            logger.error(f"Fallback error: {e2}")
            return None


# ── Автоматическое извлечение фактов из сообщения ────────────────────────────
_FACT_PATTERNS = [
    (r"меня зовут ([А-Яа-яA-Za-z]+)", "Имя: {}"),
    (r"я ([А-Яа-яA-Za-z]+ ?[А-Яа-яA-Za-z]*(?:ист|ер|щик|ник|тель|ор))", "Профессия/роль: {}"),
    (r"люблю ([^,.!?]{3,30})", "Любит: {}"),
    (r"играю в ([^,.!?]{3,30})", "Играет в: {}"),
    (r"живу в ([А-Яа-яA-Za-z]+)", "Живёт в: {}"),
    (r"мне (\d{1,2}) ле[тг]", "Возраст: {} лет"),
]

def _extract_facts(text: str) -> list[str]:
    facts = []
    low = text.lower()
    for pattern, template in _FACT_PATTERNS:
        m = re.search(pattern, low)
        if m:
            facts.append(template.format(m.group(1).strip()))
    return facts


# ── /ai команда ──────────────────────────────────────────────────────────────
async def cmd_ai(message: types.Message):
    """
    /ai persona <имя>  — сменить персону
    /ai ask <вопрос>   — прямой вопрос без контекста
    /ai roast <@user>  — роаст участника
    /ai debate <тема>  — дебаты двух персонажей
    /ai story          — история по мотивам чата
    """
    from duty_handlers import auto_delete_later
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    args  = (message.text or "").split(maxsplit=2)
    sub   = args[1].lower() if len(args) > 1 else ""
    rest  = args[2].strip() if len(args) > 2 else ""

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
    else:
        await _show_ai_help(message)


async def _show_ai_help(message: types.Message):
    persona_key = get_persona(message.chat.id)
    persona     = PERSONAS.get(persona_key, PERSONAS["tavern"])
    text = (
        f"🤖 <b>AI-команды</b>\n"
        f"Текущая персона: {persona['label']}\n\n"
        f"<code>/ai persona tavern</code> — 🧙 Мастер Подземелий\n"
        f"<code>/ai persona dev</code> — 💻 Сеньор-разраб\n"
        f"<code>/ai persona anime</code> — 🌸 Аниме-помощница\n"
        f"<code>/ai persona military</code> — 🪖 Сержант\n\n"
        f"<code>/ai ask &lt;вопрос&gt;</code> — прямой вопрос\n"
        f"<code>/ai roast @username</code> — роаст\n"
        f"<code>/ai debate &lt;тема&gt;</code> — дебаты\n"
        f"<code>/ai story</code> — история по чату\n"
    )
    await message.answer(text, parse_mode="HTML")


async def _cmd_persona(message: types.Message, persona_key: str):
    if persona_key not in PERSONAS:
        names = ", ".join(f"<code>{k}</code>" for k in PERSONAS)
        await message.answer(f"❌ Неизвестная персона. Доступны: {names}", parse_mode="HTML")
        return
    set_persona(message.chat.id, persona_key)
    persona = PERSONAS[persona_key]
    await message.answer(
        f"✅ Персона изменена: {persona['label']}\n\n{persona['intro']}"
    )


async def _cmd_ask(message: types.Message, question: str):
    if not question:
        await message.answer("❌ Укажи вопрос: <code>/ai ask что такое квантовый компьютер</code>",
                             parse_mode="HTML")
        return

    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return

    persona_key = get_persona(message.chat.id)
    persona     = PERSONAS.get(persona_key, PERSONAS["tavern"])

    await message.bot.send_chat_action(message.chat.id, "typing")
    prompt = persona["prompt"] + f"\n\nВопрос: {question}"
    await _stream_reply(message, prompt)


async def _cmd_roast(message: types.Message, target: str):
    chat_id = message.chat.id
    history = list(CHAT_HISTORY[chat_id])

    # Ищем сообщения от целевого пользователя
    clean_target = target.lstrip("@").lower()
    if clean_target:
        user_msgs = [m for m in history if m.lower().startswith(clean_target + ":")]
    else:
        user_msgs = history[-10:]

    if not user_msgs:
        await message.answer(
            f"😶 Не нашёл сообщений от «{target}» в последней истории чата."
        )
        return

    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return

    persona_key = get_persona(chat_id)
    persona     = PERSONAS.get(persona_key, PERSONAS["tavern"])

    await message.bot.send_chat_action(chat_id, "typing")
    prompt = (
        persona["prompt"]
        + f"\n\nВот последние сообщения пользователя {target}:\n"
        + "\n".join(user_msgs[-15:])
        + f"\n\nНапиши смешной, дерзкий и остроумный роаст на этого человека "
          f"на основе его сообщений. Стиль — в рамках твоей персоны. "
          f"3-5 предложений, с конкретными деталями из его слов."
    )
    await _stream_reply(message, prompt)


async def _cmd_debate(message: types.Message, topic: str):
    if not topic:
        await message.answer(
            "❌ Укажи тему: <code>/ai debate должны ли роботы платить налоги</code>",
            parse_mode="HTML"
        )
        return

    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    prompt = (
        f"Разыграй дебаты по теме: «{topic}».\n\n"
        "Два персонажа:\n"
        "🔴 Прагматик — циничный реалист, апеллирует к фактам и выгоде\n"
        "🔵 Идеалист — романтичный мечтатель, верит в лучшее\n\n"
        "Формат: 3 реплики каждого, чередуя. Диалог живой, с характером. "
        "В конце — короткий вердикт Ведущего (1 предложение).\n"
        "Пиши на русском, без лишних предисловий."
    )
    await _stream_reply(message, prompt)


async def _cmd_story(message: types.Message):
    chat_id = message.chat.id
    history = list(CHAT_HISTORY[chat_id])[-20:]

    if len(history) < 5:
        await message.answer("📖 Маловато истории. Пообщайтесь немного, потом попробуй.")
        return

    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return

    persona_key = get_persona(chat_id)
    persona     = PERSONAS.get(persona_key, PERSONAS["tavern"])

    await message.bot.send_chat_action(chat_id, "typing")
    prompt = (
        persona["prompt"]
        + "\n\nВот лог реального разговора из чата:\n"
        + "\n".join(history)
        + "\n\nПревратить этот разговор в короткую легенду или сказку (5-7 предложений). "
          "Участники чата — герои истории. Сохрани суть обсуждения, "
          "но подай в стиле своей персоны. Без предисловий — сразу в историю."
    )
    await _stream_reply(message, prompt)


# ── /pogoda ───────────────────────────────────────────────────────────────────
async def get_weather_advice(city: str = "Одинцово") -> str | None:
    client = get_gemini()
    if not client:
        return None
    try:
        from google.genai import types as gt
        prompt = (
            f"Узнай актуальную погоду прямо сейчас в городе {city} "
            "(температура, осадки, ветер). "
            "Напиши 2-3 коротких предложения с саркастичным советом дежурному, "
            "какую 'броню' надевать для похода на улицу со сводками. "
            "Стиль: геймерский/айтишный, немного токсичный."
        )
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=gt.GenerateContentConfig(tools=[{"google_search": {}}])
        )
        return response.text.strip()
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
    status = await message.answer(f"☁️ Запускаю метео-дрона в *{city}*...", parse_mode="Markdown")
    text = await get_weather_advice(city)
    try:
        await status.delete()
    except Exception:
        pass
    if text:
        await message.answer(f"☁️ *Метео-радар — {city}:*\n{text}", parse_mode="Markdown")
    else:
        await message.answer(f"❌ Метеостанция в *{city}* не отвечает.", parse_mode="Markdown")


# ── /meme ─────────────────────────────────────────────────────────────────────
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
            "🖼 Отправь фотку с подписью `/meme` или реплай на фото.",
            parse_mode="Markdown"
        )
        auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
        return

    status = await message.reply("⬛️ Делаю демотиватор...")
    try:
        file_info  = await message.bot.get_file(photo[-1].file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        img = Image.open(downloaded).convert("RGB")
        if img.width != 800:
            img = img.resize(
                (800, int(img.height * 800 / img.width)),
                Image.Resampling.LANCZOS
            )

        prompt = (
            "Ты — создатель мемов. Придумай подпись в стиле демотиватора. "
            "Тематика: айтишники, геймеры (CS2, Dota 2), наряды по офису. "
            "Выдай СТРОГО две строки через |. Первая — заголовок (1-3 слова). "
            "Вторая — пояснение. Без лишних символов."
        )
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=[img, prompt]
        )
        text = response.text.replace('"', "").replace("\n", "").strip()
        top, bottom = (text.split("|", 1) if "|" in text else (text, ""))
        top, bottom = top.strip().upper(), bottom.strip()

        font_b = os.path.join(BASE_DIR, "fonts", "DejaVuSans-Bold.ttf")
        font_r = os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf")
        try:
            ft = ImageFont.truetype(font_b, 60)
            fs = ImageFont.truetype(font_r, 30)
        except IOError:
            ft = fs = ImageFont.load_default()

        tl = textwrap.wrap(top, 25)
        sl = textwrap.wrap(bottom, 55)
        ta = 40 + sum((ft.getbbox(l)[3] - ft.getbbox(l)[1]) + 10 for l in tl) + 20
        ta += sum((fs.getbbox(l)[3] - fs.getbbox(l)[1]) + 10 for l in sl) + 50

        bx, bt = 70, 70
        bg = Image.new("RGB", (img.width + bx * 2, img.height + bt + ta), "black")
        bg.paste(img, (bx, bt))
        draw = ImageDraw.Draw(bg)
        draw.rectangle([bx-4, bt-4, bx+img.width+3, bt+img.height+3],
                       outline="white", width=3)

        cy = bt + img.height + 40
        for l in tl:
            draw.text((bg.width / 2, cy), l, font=ft, fill="white", anchor="ma")
            cy += (ft.getbbox(l)[3] - ft.getbbox(l)[1]) + 10
        cy += 20
        for l in sl:
            draw.text((bg.width / 2, cy), l, font=fs, fill="white", anchor="ma")
            cy += (fs.getbbox(l)[3] - fs.getbbox(l)[1]) + 10

        out = io.BytesIO()
        bg.save(out, format="JPEG", quality=95)
        out.seek(0)
        await message.answer_photo(BufferedInputFile(out.read(), filename="meme.jpg"))
    except Exception as e:
        logger.error(f"Ошибка meme: {e}")
        await message.reply(f"❌ Нейроны перегрелись: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


# ── /tldr ─────────────────────────────────────────────────────────────────────
async def cmd_tldr(message: types.Message):
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client:
        return
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    chat_id = message.chat.id
    history = CHAT_HISTORY[chat_id]
    if len(history) < 10:
        sent = await message.answer("🥱 Чат слишком мёртвый, нафлудите ещё немного.")
        auto_delete_later(message.bot, chat_id, sent.message_id, 10)
        return

    persona_key = get_persona(chat_id)
    persona     = PERSONAS.get(persona_key, PERSONAS["tavern"])

    await message.bot.send_chat_action(chat_id, "typing")
    prompt = (
        persona["prompt"]
        + "\n\nВот лог чата:\n\n"
        + "\n".join(list(history)[-50:])
        + "\n\nСделай TL;DR: что обсуждали, кто кринжанул. "
          "Пиши в стиле своей персоны. Максимум 4 предложения."
    )
    await _stream_reply(message, prompt)


# ── /poll (Pollinations) ──────────────────────────────────────────────────────
async def cmd_pollinations(message: types.Message):
    import urllib.parse as _up
    import aiohttp
    from duty_handlers import auto_delete_later
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    raw = (message.text or "").replace("/poll", "").strip()
    if not raw:
        await message.answer("❌ Укажи промпт: `/poll закат над городом`",
                             parse_mode="Markdown")
        return
    status = await message.answer("🎨 Генерирую картинку...")
    try:
        enc = _up.quote(raw)
        url = (f"https://image.pollinations.ai/prompt/{enc}"
               f"?width=1024&height=1024&nologo=true&seed={hash(raw) % 9999}")
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 200:
                    data = await r.read()
                    await message.answer_photo(
                        BufferedInputFile(data, "image.jpg"),
                        caption=f"🎨 _{raw}_", parse_mode="Markdown"
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
    import asyncio as _a
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    raw = (message.text or "").replace("/imagen", "").strip()
    if not raw:
        await message.answer("❌ Укажи промпт: `/imagen sunset over the city`",
                             parse_mode="Markdown")
        return
    status = await message.answer("🖼 Генерирую через Imagen 4...")
    try:
        from google import genai as g
        ic = g.Client(api_key=os.getenv("GEMINI_API_KEY"))
        result = await _a.to_thread(
            ic.models.generate_images,
            model="imagen-4.0-generate-preview-06-06",
            prompt=raw,
            config=g.types.GenerateImagesConfig(number_of_images=1)
        )
        img_bytes = result.generated_images[0].image.image_bytes
        await message.answer_photo(
            BufferedInputFile(img_bytes, "imagen.png"),
            caption=f"🖼 _{raw}_", parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Imagen error: {e}")
        await message.answer(f"❌ Imagen недоступен: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


# ── Обработка документов (PDF и текст) ───────────────────────────────────────
async def handle_document(message: types.Message):
    """Анализирует PDF и текстовые файлы через Gemini."""
    client = get_gemini()
    if not client:
        return

    doc = message.document
    if not doc:
        return

    # Только PDF и текстовые файлы
    mime = doc.mime_type or ""
    allowed = ("application/pdf", "text/plain", "text/markdown",
                "application/msword", "text/csv")
    if not any(mime.startswith(a) for a in allowed):
        return

    # Реагируем только если реплай на бота или личка
    bot_user = await message.bot.me()
    is_reply = (message.reply_to_message and
                message.reply_to_message.from_user.id == bot_user.id)
    is_private = message.chat.type == "private"
    has_caption = bool(message.caption)

    if not (is_reply or is_private or has_caption):
        return

    caption = message.caption or "Прочитай и кратко объясни содержимое этого документа."
    status  = await message.reply("📄 Читаю документ...")

    try:
        file_info = await message.bot.get_file(doc.file_id)
        file_bytes = await message.bot.download_file(file_info.file_path)
        file_data  = file_bytes.read() if hasattr(file_bytes, "read") else bytes(file_bytes)

        from google import genai as g
        from google.genai import types as gt
        ic = g.Client(api_key=os.getenv("GEMINI_API_KEY"))

        persona_key = get_persona(message.chat.id)
        persona     = PERSONAS.get(persona_key, PERSONAS["tavern"])

        contents = [
            gt.Part.from_bytes(data=file_data, mime_type=mime),
            persona["prompt"] + f"\n\n{caption}",
        ]

        response = await ic.aio.models.generate_content(
            model="gemini-2.5-flash", contents=contents
        )
        result_text = (response.text or "").strip()
        if result_text:
            await status.edit_text(result_text[:4096])
        else:
            await status.edit_text("❌ Не удалось проанализировать документ.")
    except Exception as e:
        logger.error(f"Document analysis error: {e}")
        await status.edit_text(f"❌ Ошибка при анализе: {e}")


# ── Обработка фото ────────────────────────────────────────────────────────────
async def handle_photo(message: types.Message):
    client = get_gemini()
    if not client:
        return

    bot_user   = await message.bot.me()
    is_reply   = (message.reply_to_message and
                  message.reply_to_message.from_user.id == bot_user.id)
    is_private = message.chat.type == "private"

    if not (is_reply or message.caption or is_private):
        return

    # Игнорируем пересланные посты из каналов
    if message.forward_from_chat and not is_reply:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        file_info  = await message.bot.get_file(message.photo[-1].file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        img = Image.open(downloaded)
        prompt = message.caption or "Что на картинке? Ответь в стиле своей персоны."
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash", contents=[img, prompt]
        )
        await message.reply(response.text.strip())
    except Exception as e:
        logger.error(f"handle_photo error: {e}")


# ── handle_ai_chat — ГЛАВНЫЙ хэндлер текста ──────────────────────────────────
async def handle_ai_chat(message: types.Message):
    import random
    client = get_gemini()
    if not client:
        return

    # ── Фильтры: что ИГНОРИРУЕМ ───────────────────────────────────────────────
    btn_texts = {
        "📋 Наряд сегодня", "📅 Наряд завтра", "📊 Расписание на неделю",
        "📓 Журнал", "🔔 Напомнить сейчас", "⚙️ Настройки"
    }
    if message.text and (message.text.startswith("/") or message.text in btn_texts):
        return

    # Пересланные посты из каналов — ИГНОРИРУЕМ
    if message.forward_from_chat:
        return

    # Боты — игнорируем
    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    # Добавляем в историю чата
    if message.text:
        user_name = message.from_user.first_name or "Кто-то"
        CHAT_HISTORY[chat_id].append(f"{user_name}: {message.text}")

        # Извлекаем факты о пользователе
        facts = _extract_facts(message.text)
        for fact in facts:
            save_user_fact(user_id, fact)

    # Определяем нужно ли отвечать
    bot_user   = await message.bot.me()
    is_reply   = (message.reply_to_message and
                  message.reply_to_message.from_user.id == bot_user.id)
    is_mention = (message.entities and any(
        e.type == "mention" and
        (message.text or "")[e.offset:e.offset + e.length].lstrip("@").lower() == bot_user.username.lower()
        for e in message.entities
    ))

    from duty_handlers import load_data
    data       = load_data()
    ai_random  = data.get("ai_random_replies_enabled", True)

    should_reply = (
        is_reply or
        is_mention or
        message.chat.type == "private" or
        (ai_random and message.text and random.random() < 0.02)
    )

    if not should_reply:
        return

    await message.bot.send_chat_action(chat_id, "typing")
    prompt = await _build_prompt(chat_id, user_id, message.text or "")

    result = await _stream_reply(message, prompt)
    if result:
        CHAT_HISTORY[chat_id].append(f"Бот: {result[:100]}")
