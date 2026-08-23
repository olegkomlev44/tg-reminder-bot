"""
ai_handlers.py — AI-функции: Gemini чат, /meme, /tldr, /poll, /imagen, обработка фото.
"""
import asyncio
import io
import logging
import os
import textwrap

from PIL import Image, ImageDraw, ImageFont
from aiogram import types
from aiogram.types import BufferedInputFile

from config import BASE_DIR, AI_SYSTEM_PROMPT, CHAT_HISTORY

logger = logging.getLogger(__name__)

# Gemini инициализируется лениво через get_gemini()
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


# ── /pogoda ───────────────────────────────────────────────────────────────────
async def get_weather_advice(city: str = "Одинцово") -> str | None:
    client = get_gemini()
    if not client: return None
    try:
        from google.genai import types as gt
        prompt = (
            f"Узнай актуальную погоду прямо сейчас в городе {city} (температура, осадки, ветер). "
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
    if not client: return
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    raw  = (message.text or "").replace("/pogoda", "").strip()
    city = raw or "Одинцово"
    status = await message.answer(f"☁️ Запускаю метео-дрона в *{city}*...", parse_mode="Markdown")
    text = await get_weather_advice(city)
    try: await status.delete()
    except: pass
    if text:
        await message.answer(f"☁️ *Метео-радар — {city}:*\n{text}", parse_mode="Markdown")
    else:
        await message.answer(f"❌ Метеостанция в *{city}* не отвечает.", parse_mode="Markdown")


# ── /meme ─────────────────────────────────────────────────────────────────────
async def cmd_meme(message: types.Message):
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client: return
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    photo = None
    if message.photo:
        photo = message.photo[-1]
    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]

    if not photo:
        sent = await message.answer("🖼 Отправь фотку с подписью `/meme` или сделай реплай на фотку.", parse_mode="Markdown")
        await auto_delete_later(message.bot, message.chat.id, sent.message_id, 10)
        return

    status = await message.reply("⬛️ Делаю демотиватор...")
    try:
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        img = Image.open(downloaded).convert("RGB")
        if img.width != 800:
            img = img.resize((800, int(img.height * 800 / img.width)), Image.Resampling.LANCZOS)

        prompt = (
            "Ты — создатель мемов. Придумай подпись в стиле демотиватора. "
            "Тематика: айтишники, геймеры (CS2, Dota 2), наряды по офису. "
            "Выдай СТРОГО две строки через |. Первая — заголовок (1-3 слова). Вторая — пояснение. Без лишних символов."
        )
        response = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=[img, prompt])
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
        ta = 40 + sum((ft.getbbox(l)[3]-ft.getbbox(l)[1])+10 for l in tl) + 20
        ta += sum((fs.getbbox(l)[3]-fs.getbbox(l)[1])+10 for l in sl) + 50

        bx, bt = 70, 70
        bg = Image.new("RGB", (img.width + bx*2, img.height + bt + ta), "black")
        bg.paste(img, (bx, bt))
        draw = ImageDraw.Draw(bg)
        draw.rectangle([bx-4, bt-4, bx+img.width+3, bt+img.height+3], outline="white", width=3)

        cy = bt + img.height + 40
        for l in tl:
            draw.text((bg.width/2, cy), l, font=ft, fill="white", anchor="ma")
            cy += (ft.getbbox(l)[3]-ft.getbbox(l)[1]) + 10
        cy += 20
        for l in sl:
            draw.text((bg.width/2, cy), l, font=fs, fill="white", anchor="ma")
            cy += (fs.getbbox(l)[3]-fs.getbbox(l)[1]) + 10

        out = io.BytesIO()
        bg.save(out, format="JPEG", quality=95)
        out.seek(0)
        await message.answer_photo(BufferedInputFile(out.read(), filename="meme.jpg"))
    except Exception as e:
        logger.error(f"Ошибка meme: {e}")
        await message.reply(f"❌ Нейроны перегрелись: {e}")
    finally:
        try: await status.delete()
        except: pass


# ── /tldr ─────────────────────────────────────────────────────────────────────
async def cmd_tldr(message: types.Message):
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client: return
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)

    chat_id = message.chat.id
    history = CHAT_HISTORY[chat_id]
    if len(history) < 10:
        sent = await message.answer("🥱 Чат слишком мертвый, нафлудите ещё немного.")
        await auto_delete_later(message.bot, chat_id, sent.message_id, 10)
        return

    status = await message.answer("🧠 Читаю ваш бред...")
    prompt = (
        "Ты — токсичный зумер-бот. Вот лог чата:\n\n"
        + "\n".join(history)
        + "\n\nСделай TL;DR: что обсуждали, кто кринжанул. "
          "Пиши саркастично, зумерский сленг, мат. Максимум 3-4 предложения."
    )
    try:
        r = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        await message.answer(f"📜 *Саммари чата:*\n\n{r.text.strip()}", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Нейроны перегрелись:\n`{e}`")
    finally:
        try: await status.delete()
        except: pass


# ── /poll (Pollinations) ──────────────────────────────────────────────────────
async def cmd_pollinations(message: types.Message):
    from duty_handlers import auto_delete_later
    import urllib.parse
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    raw = (message.text or "").replace("/poll", "").strip()
    if not raw:
        await message.answer("❌ Укажи промпт: `/poll закат над городом`", parse_mode="Markdown")
        return
    status = await message.answer("🎨 Генерирую картинку...")
    try:
        enc = urllib.parse.quote(raw)
        url = f"https://image.pollinations.ai/prompt/{enc}?width=1024&height=1024&nologo=true&seed={hash(raw)%9999}"
        import aiohttp
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
        try: await status.delete()
        except: pass


# ── /imagen (Google Imagen) ───────────────────────────────────────────────────
async def cmd_imagen(message: types.Message):
    from duty_handlers import auto_delete_later
    client = get_gemini()
    if not client:
        await message.answer("❌ Gemini недоступен.")
        return
    await auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    raw = (message.text or "").replace("/imagen", "").strip()
    if not raw:
        await message.answer("❌ Укажи промпт: `/imagen sunset over the city`", parse_mode="Markdown")
        return
    status = await message.answer("🖼 Генерирую через Imagen 4...")
    try:
        from google import genai as g
        ic = g.Client(api_key=os.getenv("GEMINI_API_KEY"))
        result = await asyncio.to_thread(
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
        try: await status.delete()
        except: pass


# ── Обработка фото ────────────────────────────────────────────────────────────
async def handle_photo(message: types.Message):
    client = get_gemini()
    if not client: return
    bot_user = await message.bot.me()
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    if not (is_reply or message.caption or message.chat.type == "private"):
        return
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        file_info = await message.bot.get_file(message.photo[-1].file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        img = Image.open(downloaded)
        prompt = message.caption or "Что на картинке? Ответь в своём стиле."
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[img, prompt]
        )
        await message.reply(response.text.strip())
    except Exception as e:
        logger.error(f"handle_photo error: {e}")


# ── handle_ai_chat ────────────────────────────────────────────────────────────
async def handle_ai_chat(message: types.Message):
    import random
    client = get_gemini()
    if not client: return

    # Пропускаем команды и кнопки
    btn_texts = {"📋 Наряд сегодня", "📅 Наряд завтра", "📊 Расписание на неделю",
                 "📓 Журнал", "🔔 Напомнить сейчас", "⚙️ Настройки"}
    if message.text and (message.text.startswith("/") or message.text in btn_texts):
        return

    chat_id = message.chat.id
    if message.text:
        user_name = (message.from_user.first_name or "Кто-то")
        CHAT_HISTORY[chat_id].append(f"{user_name}: {message.text}")

    bot_user = await message.bot.me()
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id

    from duty_handlers import load_data
    data = load_data()
    ai_random = data.get("ai_random_replies_enabled", True)

    if not is_reply:
        if ai_random and message.text and random.random() < 0.02:
            pass  # продолжаем
        else:
            return

    await message.bot.send_chat_action(chat_id, "typing")
    history_snapshot = list(CHAT_HISTORY[chat_id])[-30:]
    prompt = AI_SYSTEM_PROMPT + "\n\nКонтекст чата:\n" + "\n".join(history_snapshot)
    if message.text:
        prompt += f"\n\nСообщение: {message.text}"

    try:
        response = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        reply_text = response.text.strip()
        if reply_text:
            CHAT_HISTORY[chat_id].append(f"Бот: {reply_text[:100]}")
            await message.reply(reply_text)
    except Exception as e:
        logger.error(f"AI chat error: {e}")
