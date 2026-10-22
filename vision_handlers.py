"""
vision_handlers.py — вопросы по картинке/документу/видео/голосовому + мемы реплаем.

Как это работает (Gemini получает сам файл):
  • ответить на фото / скрин / стикер / PDF / DOCX / текстовый файл / видео / кружок / голосовое:
        «переведи» · «что тут написано» · «объясни мем» · «о чём документ» · «кратко» …
  • ответить на картинку: «сделай мем про дембель» → демотиватор (кнопки «ещё вариант», «другой стиль»)
  • фото/PDF с подписью «переведи» (без реплая) — тоже работает
  • /tr [язык] и /explain — те же сценарии командами
В группах бот реагирует на такие фразы без упоминания; произвольные вопросы по файлу — в личке,
по упоминанию (@бот …) или если ответ адресован сообщению самого бота.

Подключение: register_vision_handlers(dp) в main.py — ДО handle_photo/handle_document/handle_ai_chat.
"""

import asyncio
import html
import io
import logging
import mimetypes
import re
import secrets
import zipfile
from collections import OrderedDict
from dataclasses import dataclass

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

logger = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
TG_DOWNLOAD_LIMIT = 19 * 1024 * 1024         # Bot API отдаёт файлы только до 20 МБ
GEMINI_INLINE_LIMIT = 18 * 1024 * 1024
TEXT_CHAR_LIMIT = 120_000
CHUNK = 3500
MAX_CHUNKS = 6

# ── Распознавание намерения ───────────────────────────────────────────────────
_PREFIX = r"^\s*(?:@\w+[\s,]*)?(?:бот[\s,]+)?(?:пожалуйста[\s,]+|плиз[\s,]+)?"
RE_TRANSLATE = re.compile(_PREFIX + r"(?:переведи|перевод\b|переводи|translate)", re.I)
RE_OCR = re.compile(
    _PREFIX + r"(?:что\s+(?:тут|здесь|там)?\s*(?:вообще\s+)?(?:написано|сказано)|что\s+написано|"
    r"прочита(?:й|ть)|распознай|расшифруй|текст\s+с\s+(?:фото|картинки|скрина)|"
    r"что\s+говорит|что\s+он[аи]?\s+говор)", re.I)
RE_EXPLAIN = re.compile(
    _PREFIX + r"(?:объясни|поясни|разъясни|в\s+чём\s+(?:смысл|прикол|шутка|юмор)|"
    r"не\s+понял[аои]?\s+(?:шутк\w*|мем\w*|прикол\w*)|что\s+(?:на\s+(?:фото|картинке|скрине|скриншоте|изображении)|"
    r"это\s+(?:за|такое)|тут\s+происходит)|о\s+чём\s+(?:этот\s+)?(?:документ|файл|пдф|pdf|текст|видео|ролик|гс|голосовое)|"
    r"кратко|суммаризируй|сделай\s+(?:выжимку|конспект)|перескажи)", re.I)
RE_MEME = re.compile(
    _PREFIX + r"(?:сделай|создай|накидай|запили|придумай|давай)\s+(?:мем|демотиватор|подпись)"
    r"(?:\s+(?:про|на\s+тему|о|об|по|для)\s+(?P<topic>.+))?\s*$", re.I | re.S)
RE_IMPACT = re.compile(r"\b(?:импакт|impact|классическ\w+|верх\w*\s+и\s+низ\w*)\b", re.I)

_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def detect_intent(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    if RE_MEME.search(t):
        return "meme"
    if RE_TRANSLATE.search(t):
        return "translate"
    if RE_OCR.search(t):
        return "ocr"
    if RE_EXPLAIN.search(t):
        return "explain"
    return None


# ── Медиа в сообщении ─────────────────────────────────────────────────────────
@dataclass
class MediaRef:
    kind: str           # photo | sticker | document | video | audio | voice | text
    file_id: str = ""
    mime: str = ""
    size: int = 0
    name: str = ""
    text: str = ""      # для kind == "text"

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


def extract_media(msg: types.Message | None, allow_text: bool = False) -> MediaRef | None:
    if msg is None:
        return None
    if msg.photo:
        p = msg.photo[-1]
        return MediaRef("photo", p.file_id, "image/jpeg", p.file_size or 0)
    if msg.sticker:
        st = msg.sticker
        if getattr(st, "is_animated", False):
            return MediaRef("sticker", st.file_id, "application/x-tgsticker", st.file_size or 0)
        mime = "video/webm" if getattr(st, "is_video", False) else "image/webp"
        return MediaRef("sticker", st.file_id, mime, st.file_size or 0)
    if msg.document:
        d = msg.document
        mime = d.mime_type or mimetypes.guess_type(d.file_name or "")[0] or "application/octet-stream"
        return MediaRef("document", d.file_id, mime, d.file_size or 0, d.file_name or "")
    if msg.animation:
        a = msg.animation
        return MediaRef("video", a.file_id, a.mime_type or "video/mp4", a.file_size or 0)
    if msg.video:
        v = msg.video
        return MediaRef("video", v.file_id, v.mime_type or "video/mp4", v.file_size or 0)
    if msg.video_note:
        v = msg.video_note
        return MediaRef("video", v.file_id, "video/mp4", v.file_size or 0)
    if msg.voice:
        v = msg.voice
        return MediaRef("voice", v.file_id, v.mime_type or "audio/ogg", v.file_size or 0)
    if msg.audio:
        a = msg.audio
        return MediaRef("audio", a.file_id, a.mime_type or "audio/mpeg", a.file_size or 0, a.file_name or "")
    if allow_text:
        t = (msg.text or msg.caption or "").strip()
        if t:
            return MediaRef("text", text=t)
    return None


def _kind_label(m: MediaRef) -> str:
    return {
        "photo": "фото/скриншот", "sticker": "стикер", "video": "видео", "voice": "голосовое сообщение",
        "audio": "аудио", "text": "текст",
    }.get(m.kind, f"документ «{m.name}»" if m.name else "документ")


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(text).strip()


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


# ── Задания для Gemini ────────────────────────────────────────────────────────
NEUTRAL_SYSTEM = (
    "Ты — точный помощник по анализу файлов в Telegram-чате. Отвечай на русском, если пользователь "
    "не просит иной язык. Не выдумывай: если чего-то не видно или не слышно — так и скажи. "
    "Форматирование: обычный текст, **жирный** допустим, без таблиц и без заголовков #."
)


def build_task(intent: str, user_text: str, m: MediaRef) -> str:
    what = _kind_label(m)
    ask = f"Запрос пользователя: «{user_text.strip()}».\n" if user_text.strip() else ""
    if intent == "translate":
        return (
            f"Тебе прислали {what}. {ask}"
            "Найди ВЕСЬ текст/речь и переведи на русский (если пользователь назвал другой язык — на него; "
            "если текст уже на русском — переведи на английский). Сохрани структуру: абзацы, списки, "
            "порядок реплик. Сначала одной строкой укажи язык оригинала, затем перевод. "
            "Не комментируй и не добавляй от себя."
        )
    if intent == "ocr":
        return (
            f"Тебе прислали {what}. {ask}"
            "Дословно перепиши весь распознанный текст (для аудио/видео — транскрипт речи), "
            "затем в 1–2 предложениях объясни смысл. Если часть нечитаема — пометь [неразборчиво]."
        )
    if intent == "explain":
        return (
            f"Тебе прислали {what}. {ask}"
            "Объясни: что это (на картинке/в документе/в записи), в чём суть, а если это мем или шутка — "
            "в чём юмор: референс, контекст, формат мема. 3–6 предложений. Если есть текст — учти его."
        )
    return (
        f"Тебе прислали {what}. {ask or 'Опиши содержимое и кратко разбери суть.'}"
        "Ответь именно на вопрос пользователя, опираясь на содержимое файла."
    )


async def _download(bot, m: MediaRef) -> bytes:
    if m.size and m.size > TG_DOWNLOAD_LIMIT:
        raise ValueError(f"Файл {m.size / 1048576:.0f} МБ — Telegram не отдаёт боту файлы больше 20 МБ.")
    info = await bot.get_file(m.file_id)
    buf = await bot.download_file(info.file_path)
    data = buf.read() if hasattr(buf, "read") else bytes(buf)
    return data


async def _gemini_parts(bot, m: MediaRef):
    """Готовит часть(и) содержимого для Gemini. Бросает ValueError с понятным текстом."""
    from google.genai import types as gt
    if m.kind == "text":
        return [m.text[:TEXT_CHAR_LIMIT]]
    if m.mime == "application/x-tgsticker":
        raise ValueError("Анимированные стикеры (.tgs) я не умею разбирать. Статичные и видео-стикеры — умею.")
    name = (m.name or "").lower()
    data = await _download(bot, m)
    if len(data) > GEMINI_INLINE_LIMIT:
        raise ValueError("Файл слишком большой для разбора (лимит ≈18 МБ).")
    if name.endswith(".docx") or m.mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        try:
            txt = _docx_text(data)
        except Exception:
            raise ValueError("Не смог открыть DOCX.")
        if not txt:
            raise ValueError("В DOCX не нашлось текста.")
        return [f"[Содержимое документа {m.name}]\n{txt[:TEXT_CHAR_LIMIT]}"]
    if m.mime.startswith("text/") or m.mime in ("application/json", "application/xml", "application/x-yaml") \
            or name.endswith((".txt", ".md", ".csv", ".json", ".log", ".py", ".xml", ".yml", ".yaml", ".ini")):
        return [f"[Содержимое файла {m.name}]\n{_decode_text(data)[:TEXT_CHAR_LIMIT]}"]
    if m.kind == "document" and not (m.is_image or m.mime == "application/pdf"
                                     or m.mime.startswith(("video/", "audio/"))):
        raise ValueError(f"Формат {m.mime or 'файла'} я не умею читать. Умею: фото, PDF, DOCX, текст, видео, аудио.")
    if m.kind == "document" and name.endswith(_IMAGE_EXT) and not m.mime.startswith("image/"):
        m.mime = mimetypes.guess_type(name)[0] or "image/jpeg"
    return [gt.Part.from_bytes(data=data, mime_type=m.mime)]


def _split_chunks(text: str, limit: int = CHUNK) -> list[str]:
    chunks, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:                      # очень длинная строка
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur.strip():
        chunks.append(cur)
    return chunks


async def _send_long(status: types.Message, text: str, reply_to: types.Message):
    from ai_handlers import _md_to_tg_html
    chunks = _split_chunks(text.strip())
    truncated = len(chunks) > MAX_CHUNKS
    chunks = chunks[:MAX_CHUNKS]
    if truncated:
        chunks[-1] += "\n\n… (дальше обрезано)"
    for i, ch in enumerate(chunks):
        html_ch = _md_to_tg_html(ch)
        try:
            if i == 0:
                await status.edit_text(html_ch, parse_mode="HTML")
            else:
                await reply_to.answer(html_ch, parse_mode="HTML")
        except Exception:
            if i == 0:
                await status.edit_text(ch)
            else:
                await reply_to.answer(ch)


async def answer_about_media(
    message: types.Message, media: MediaRef, intent: str, user_text: str, reply_to: types.Message | None = None,
) -> bool:
    """Главная функция: берёт файл/текст, спрашивает Gemini и отвечает в чат."""
    from ai_handlers import get_gemini, _build_system_instruction
    from google.genai import types as gt

    client = get_gemini()
    target = reply_to or message
    if not client:
        await target.reply("❌ Gemini недоступен.")
        return False

    emoji = {"translate": "🌐 Перевожу…", "ocr": "🔤 Читаю…"}.get(intent, "👀 Смотрю…")
    status = await target.reply(emoji)
    try:
        parts = await _gemini_parts(message.bot, media)
        if intent in ("translate", "ocr"):
            sys_inst = NEUTRAL_SYSTEM
        else:
            uid = message.from_user.id if message.from_user else 0
            sys_inst = _build_system_instruction(message.chat.id, uid, include_live_context=False)
            sys_inst += "\nОтвечай по существу файла, персону проявляй умеренно, без длинных вступлений."
        resp = await client.aio.models.generate_content(
            model=MODEL,
            contents=[*parts, build_task(intent, user_text, media)],
            config=gt.GenerateContentConfig(system_instruction=sys_inst),
        )
        text = (resp.text or "").strip()
        if not text:
            await status.edit_text("🤷 Gemini ничего не вернул (возможно, контент отфильтрован).")
            return False
        await _send_long(status, text, target)
        return True
    except ValueError as e:
        await status.edit_text(f"❌ {e}")
    except Exception as e:
        logger.error(f"answer_about_media: {e}", exc_info=True)
        await status.edit_text(f"❌ Не получилось разобрать файл: {str(e)[:150]}")
    return False


# ── Мемы реплаем ──────────────────────────────────────────────────────────────
_MEME_SRC: "OrderedDict[str, dict]" = OrderedDict()


def _meme_kb(tok: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Ещё вариант", callback_data=f"meme:again:{tok}"),
        InlineKeyboardButton(text="🔀 Другой стиль", callback_data=f"meme:style:{tok}"),
    ]])


async def make_meme_for(message: types.Message, media: MediaRef, topic: str, impact: bool,
                        reply_to: types.Message | None = None) -> bool:
    from ai_handlers import build_meme_bytes, get_gemini
    from PIL import Image

    target = reply_to or message
    if not get_gemini():
        await target.reply("❌ Gemini недоступен.")
        return False
    if not media.is_image and media.kind not in ("photo", "sticker"):
        await target.reply("🖼 Мем делаю только из картинки (фото, скрин, стикер, картинка-файл).")
        return False
    status = await target.reply("⬛️ Придумываю мем…")
    try:
        data = await _download(message.bot, media)
        img = Image.open(io.BytesIO(data))
        jpg = await build_meme_bytes(message.chat.id, img, topic, impact)
        if not jpg:
            await status.edit_text("❌ Не получилось сделать мем.")
            return False
        tok = secrets.token_hex(4)
        _MEME_SRC[tok] = {"file_id": media.file_id, "topic": topic, "impact": impact, "chat": message.chat.id}
        while len(_MEME_SRC) > 150:
            _MEME_SRC.popitem(last=False)
        await target.reply_photo(BufferedInputFile(jpg, filename="meme.jpg"), reply_markup=_meme_kb(tok))
        try:
            await status.delete()
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error(f"make_meme_for: {e}", exc_info=True)
        await status.edit_text(f"❌ Нейроны перегрелись: {str(e)[:150]}")
        return False


async def callback_meme(callback: types.CallbackQuery):
    from ai_handlers import build_meme_bytes
    from PIL import Image
    _, action, tok = (callback.data or "meme:x:x").split(":", 2)
    src = _MEME_SRC.get(tok)
    if not src:
        await callback.answer("Исходник устарел — сделай мем заново", show_alert=True)
        return
    await callback.answer("Генерирую…")
    if action == "style":
        src["impact"] = not src["impact"]
    try:
        info = await callback.bot.get_file(src["file_id"])
        buf = await callback.bot.download_file(info.file_path)
        img = Image.open(io.BytesIO(buf.read()))
        jpg = await build_meme_bytes(src["chat"], img, src["topic"], src["impact"], fresh=True)
        if jpg:
            await callback.message.edit_media(
                InputMediaPhoto(media=BufferedInputFile(jpg, filename="meme.jpg")),
                reply_markup=_meme_kb(tok))
    except Exception as e:
        logger.error(f"callback_meme: {e}")
        await callback.answer("Не вышло, попробуй ещё раз", show_alert=True)


# ── Фильтры и хэндлеры ────────────────────────────────────────────────────────
def _addressed(message: types.Message, bot_username: str | None, bot_id: int) -> bool:
    """Обращение к боту: личка, упоминание в тексте или ответ на сообщение самого бота."""
    if message.chat.type == "private":
        return True
    txt = (message.text or message.caption or "").lower()
    if bot_username and f"@{bot_username.lower()}" in txt:
        return True
    rep = message.reply_to_message
    return bool(rep and rep.from_user and rep.from_user.id == bot_id)


async def _reply_media_filter(message: types.Message):
    """Текстовое сообщение-ответ на файл, адресованное разбору. Возвращает kwargs для хэндлера."""
    text = (message.text or "").strip()
    rep = message.reply_to_message
    if not text or not rep or text.startswith("/"):
        return False
    me = await message.bot.me()
    intent = detect_intent(text)
    from_bot = bool(rep.from_user and rep.from_user.id == me.id)
    # текстовые реплаи — только «переведи» и не на сообщения бота (те идут в обычный ИИ-чат)
    media = extract_media(rep, allow_text=(intent == "translate" and not from_bot))
    if not media:
        return False
    if intent is None:
        if not _addressed(message, me.username, me.id):
            return False
        if "?" not in text and len(text.split()) < 3:      # «лол», «ок» — не вопрос по файлу
            return False
        intent = "other"
    if intent == "meme" and not (media.is_image or media.kind in ("photo", "sticker")):
        return False
    return {"v_media": media, "v_intent": intent}


async def handle_reply_media(message: types.Message, v_media: MediaRef, v_intent: str):
    text = (message.text or "").strip()
    if v_intent == "meme":
        m = RE_MEME.search(text)
        topic = (m.group("topic") or "").strip() if m else ""
        impact = bool(RE_IMPACT.search(text))
        topic = RE_IMPACT.sub("", topic).strip()
        await make_meme_for(message, v_media, topic, impact, reply_to=message)
        return
    await answer_about_media(message, v_media, v_intent, text, reply_to=message)


async def _caption_filter(message: types.Message):
    """Файл, присланный сразу с подписью-триггером («переведи», «сделай мем про …»)."""
    cap = (message.caption or "").strip()
    if not cap or cap.startswith("/"):
        return False
    intent = detect_intent(cap)
    if intent is None:
        return False
    media = extract_media(message)
    if not media:
        return False
    if intent == "meme" and not (media.is_image or media.kind in ("photo", "sticker")):
        return False
    return {"v_media": media, "v_intent": intent}


async def handle_caption_media(message: types.Message, v_media: MediaRef, v_intent: str):
    cap = (message.caption or "").strip()
    if v_intent == "meme":
        m = RE_MEME.search(cap)
        topic = RE_IMPACT.sub("", (m.group("topic") or "") if m else "").strip()
        await make_meme_for(message, v_media, topic, bool(RE_IMPACT.search(cap)), reply_to=message)
        return
    await answer_about_media(message, v_media, v_intent, cap, reply_to=message)


async def _cmd_media(message: types.Message, intent: str, default_text: str):
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    media = extract_media(message.reply_to_message, allow_text=True)
    if not media:
        await message.reply("📎 Ответь этой командой на фото, PDF, видео, голосовое или текст.")
        return
    user_text = default_text if not arg else (f"{default_text} {arg}" if intent == "translate" else arg)
    await answer_about_media(message, media, intent, user_text, reply_to=message)


async def cmd_tr(message: types.Message):
    await _cmd_media(message, "translate", "Переведи")


async def cmd_explain(message: types.Message):
    await _cmd_media(message, "explain", "Объясни")


def register_vision_handlers(dp):
    dp.message.register(cmd_tr, Command("tr", "translate"))
    dp.message.register(cmd_explain, Command("explain", "what"))
    dp.message.register(handle_reply_media, F.text, _reply_media_filter)
    dp.message.register(handle_caption_media, F.photo | F.document | F.video | F.animation, _caption_filter)
    dp.callback_query.register(callback_meme, F.data.startswith("meme:"))
    logger.info("👀 vision_handlers зарегистрированы (реплаи на медиа, мемы, /tr, /explain)")
