"""
ai_tools.py — Function Calling для ИИ бота (Gemini).

ИИ сам решает, когда вызвать функцию бота: профиль Instagram, поиск/отправка песни,
аниме, товары, скачивание видео, наряды, дембель-таймер, картинки, поиск в интернете.

Как это работает:
  1. handle_ai_chat() вызывает run_ai_with_tools() вместо обычного _stream_reply().
  2. Gemini получает список функций (TOOLS). Если просьба — это действие, она отвечает
     function_call вместо текста.
  3. Мы выполняем соответствующий существующий хэндлер бота (он сам шлёт карточки,
     кнопки, аудио и т.д. в чат), а модели возвращаем короткий статус.
  4. Для «терминальных» функций (результат уже показан в чате) второй запрос к Gemini
     не делается — экономим квоту и время. Для web_search / ошибок — делается.

Отключить целиком: переменная окружения AI_TOOLS=0 (на BotHost, без правки кода).

Чтобы добавить новую функцию: напиши async def _tool_xxx(ctx, ...) и добавь запись в
TOOLS через _tool(...). Больше ничего менять не нужно.
"""

import asyncio
import html
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram import types

logger = logging.getLogger(__name__)

TOOLS_ENABLED = os.getenv("AI_TOOLS", "1") != "0"
MODEL = "gemini-2.5-flash"

MAX_STEPS = 4              # максимум «раундов» модель → функции → модель
MAX_CALLS_PER_STEP = 3     # максимум функций за один раунд
RATE_LIMIT_CALLS = 6       # вызовов функций на пользователя …
RATE_LIMIT_WINDOW = 60     # … за столько секунд

_RATE: dict[int, deque] = defaultdict(lambda: deque(maxlen=RATE_LIMIT_CALLS))


@dataclass
class ToolContext:
    message: types.Message
    user_id: int
    chat_id: int


@dataclass
class Tool:
    name: str
    description: str
    properties: dict            # {"имя": {"type": "STRING", "description": "..."}}
    required: list[str]
    handler: Callable[..., Awaitable[str]]
    terminal: bool = True       # True — результат уже показан в чате, follow-up не нужен


TOOLS: dict[str, Tool] = {}


def _tool(name, description, handler, properties=None, required=None, terminal=True):
    TOOLS[name] = Tool(name, description, properties or {}, required or [], handler, terminal)


def _s(desc: str) -> dict:
    return {"type": "STRING", "description": desc}


def _clean(value, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


# ══════════════════════════════════════════════════════════════════════════════
# Функции-обёртки над существующими хэндлерами. Импорты ленивые — иначе круговые
# зависимости (ai_handlers ↔ остальные модули).
# ══════════════════════════════════════════════════════════════════════════════

# ── Instagram ────────────────────────────────────────────────────────────────
async def _tool_instagram_profile(ctx: ToolContext, username: str = "") -> str:
    from instagram_handlers import _clean_username, _show_profile
    user = _clean_username(_clean(username))
    if not user:
        return "ОШИБКА: не указан username Instagram. Попроси пользователя его назвать."
    await _show_profile(ctx.message, user, ctx.user_id)
    return f"Карточка профиля @{user} отправлена в чат (с кнопками сторис, постов, подписки)."


async def _tool_instagram_download(ctx: ToolContext, url: str = "") -> str:
    from instagram_handlers import _dispatch_ig_link, _parse_ig_link
    parsed = _parse_ig_link(_clean(url, 500))
    if not parsed:
        return "ОШИБКА: это не ссылка на пост/рилс/сторис/хайлайт Instagram."
    ok = await _dispatch_ig_link(ctx.message, parsed)
    return ("Медиа из Instagram отправлено в чат." if ok
            else "ОШИБКА: не удалось достать (приватный аккаунт, удалено или Instagram капризничает).")


# ── Скачивание видео (YouTube / TikTok / X / Reddit) ─────────────────────────
async def _tool_download_video(ctx: ToolContext, url: str = "") -> str:
    from media_downloader import detect_platform, handle_media_link
    platform, link = detect_platform(_clean(url, 500))
    if not platform:
        return "ОШИБКА: поддерживаются только ссылки YouTube, TikTok, X/Twitter и Reddit."
    await handle_media_link(ctx.message, custom_url=link, custom_platform=platform)
    return f"Запрос на скачивание с {platform} обработан, результат (или ошибка) показан в чате."


# ── Музыка ───────────────────────────────────────────────────────────────────
async def _tool_music_search(ctx: ToolContext, query: str = "") -> str:
    from music_handlers import show_music_page
    q = _clean(query, 100)
    if not q:
        return "ОШИБКА: пустой поисковый запрос."
    await show_music_page(ctx.message, "search", q, 0)
    return f"Список найденных треков по «{q}» с кнопками скачивания отправлен в чат."


async def _tool_music_send_track(ctx: ToolContext, query: str = "") -> str:
    from music_handlers import send_track_to_user
    from music_engine import music_engine
    q = _clean(query, 100)
    if not q:
        return "ОШИБКА: не указано название трека или исполнителя."
    tracks = await music_engine.search_multi(q, limit=3)
    if not tracks:
        return f"ОШИБКА: по запросу «{q}» ничего не найдено."
    best = tracks[0]
    await send_track_to_user(ctx.message, best["id"])
    return f"Трек «{best.get('artist', '?')} — {best.get('title', '?')}» отправлен в чат."


async def _tool_music_charts(ctx: ToolContext) -> str:
    from music_handlers import show_music_page
    await show_music_page(ctx.message, "chart", "", 0)
    return "Чарт отправлен в чат."


# ── Аниме ────────────────────────────────────────────────────────────────────
async def _tool_anime_search(ctx: ToolContext, query: str = "") -> str:
    from anixart_handler import _do_search
    q = _clean(query, 100)
    if not q:
        return "ОШИБКА: не указано название аниме."
    await _do_search(ctx.message, q, offset=0)
    return f"Результаты поиска аниме «{q}» отправлены в чат (с кнопками карточек)."


async def _tool_anime_random(ctx: ToolContext) -> str:
    from anixart_handler import _do_random
    await _do_random(ctx.message)
    return "Случайное аниме (карточка) отправлено в чат."


async def _tool_anime_recommend(ctx: ToolContext, vibe: str = "") -> str:
    from anixart_handler import _do_ai_recommend
    v = _clean(vibe, 300)
    if not v:
        return "ОШИБКА: не описано, какое аниме хочется."
    await _do_ai_recommend(ctx.message, v)
    return "ИИ-подборка аниме по вайбу отправлена в чат."


async def _tool_anime_top(ctx: ToolContext) -> str:
    from anixart_handler import _do_top
    await _do_top(ctx.message)
    return "Топ аниме группы отправлен в чат."


async def _tool_anime_schedule(ctx: ToolContext) -> str:
    import datetime
    import config
    from anixart_handler import _render_schedule_view
    today_idx = datetime.datetime.now(config.TIMEZONE).weekday()
    await _render_schedule_view(ctx.message, ctx.user_id, today_idx, only_my_subs=False, edit=False)
    return "Календарь выхода серий отправлен в чат."


# ── Товары (поиск идёт по Wildberries; команда исторически называется /ozon) ──
async def _tool_product_search(ctx: ToolContext, query: str = "") -> str:
    from ozon_handler import ozon_search
    q = _clean(query, 150)
    if not q:
        return "ОШИБКА: не указано, какой товар искать."
    await ozon_search(ctx.message, q)
    return f"Результаты поиска товара «{q}» отправлены в чат."


# ── Наряды и дембель ─────────────────────────────────────────────────────────
async def _tool_duty_schedule(ctx: ToolContext, when: str = "today") -> str:
    from datetime import date, timedelta
    from duty_handlers import auto_delete_later, build_duty_message, build_week_schedule
    when = _clean(when, 20).lower()
    if when == "week":
        text = build_week_schedule()
    elif when == "tomorrow":
        text = build_duty_message(date.today() + timedelta(days=1), "завтра")
    else:
        text = build_duty_message(date.today(), "сегодня")
    sent = await ctx.message.answer(text, parse_mode="Markdown")
    auto_delete_later(ctx.message.bot, ctx.chat_id, sent.message_id, 300)
    return "График нарядов отправлен в чат."


async def _tool_dembel_timer(ctx: ToolContext, person: str = "") -> str:
    from duty_handlers import _send_dembel_card, _send_dembel_summary
    from dembel_generator import DEMBEL_DATES
    person = _clean(person, 40)
    if person:
        match = next((p for p in DEMBEL_DATES if p.lower() == person.lower()), None)
        if not match:
            return f"ОШИБКА: «{person}» нет в таймере. Доступны: {', '.join(DEMBEL_DATES)}."
        await _send_dembel_card(ctx.message, match, edit_in_place=False)
        return f"Дембель-карточка {match} отправлена в чат."
    await _send_dembel_summary(ctx.message, edit_in_place=False)
    return "Сводка дембель-таймеров отправлена в чат."


# ── Лента Reddit ─────────────────────────────────────────────────────────────
async def _tool_reddit_feed(ctx: ToolContext) -> str:
    from feed_handlers import cmd_feed
    await cmd_feed(ctx.message)
    return "Пост из ленты (мемы/картинки) отправлен в чат."


# ── Генерация картинки (Pollinations) ────────────────────────────────────────
async def _tool_generate_image(ctx: ToolContext, prompt: str = "") -> str:
    import urllib.parse as up
    import aiohttp
    from aiogram.types import BufferedInputFile
    p = _clean(prompt, 400)
    if not p:
        return "ОШИБКА: нет описания картинки."
    url = (f"https://image.pollinations.ai/prompt/{up.quote(p)}"
           f"?width=1024&height=1024&nologo=true&seed={abs(hash(p)) % 99999}")
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                return f"ОШИБКА: генератор картинок вернул HTTP {r.status}."
            data = await r.read()
    await ctx.message.answer_photo(
        BufferedInputFile(data, "image.jpg"),
        caption=f"🎨 <i>{html.escape(p)}</i>", parse_mode="HTML",
    )
    return "Картинка сгенерирована и отправлена в чат."


# ── Интернет (погода, новости, факты) ────────────────────────────────────────
async def _tool_web_search(ctx: ToolContext, query: str = "") -> str:
    """
    Gemini нельзя одновременно дать function calling и google_search в одном запросе,
    поэтому поиск вынесен в отдельную функцию с собственным вызовом модели.
    Результат возвращается ИИ-собеседнику (не в чат), он сам формулирует ответ.
    """
    from ai_handlers import get_gemini
    from google.genai import types as gt
    client = get_gemini()
    q = _clean(query, 300)
    if not client or not q:
        return "ОШИБКА: поиск недоступен."
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=f"Найди актуальную информацию и коротко (до 1200 символов, только факты) ответь: {q}",
        config=gt.GenerateContentConfig(tools=[{"google_search": {}}]),
    )
    return (resp.text or "").strip()[:3000] or "Ничего не нашлось."


# ── Память ───────────────────────────────────────────────────────────────────
async def _tool_remember_fact(ctx: ToolContext, fact: str = "") -> str:
    from ai_handlers import save_user_fact
    f = _clean(fact, 120)
    if not f:
        return "ОШИБКА: нечего запоминать."
    save_user_fact(ctx.user_id, f)
    return "Запомнил."


# ══════════════════════════════════════════════════════════════════════════════
# Реестр функций. description — это то, по чему ИИ решает, когда вызывать.
# ══════════════════════════════════════════════════════════════════════════════
_tool("instagram_profile",
      "Показать профиль Instagram (карточка: аватар, подписчики, кнопки сторис/постов/подписки). "
      "Вызывай, когда просят найти/показать/глянуть аккаунт или профиль в Instagram (инста, инсте, инстаграм).",
      _tool_instagram_profile,
      {"username": _s("Username без @, либо ссылка на профиль")}, ["username"])

_tool("instagram_download",
      "Скачать пост, рилс, сторис или хайлайт Instagram по ссылке. Вызывай, когда пользователь "
      "просит скачать/достать/скинуть медиа и есть ссылка instagram.com (в сообщении или в реплае).",
      _tool_instagram_download,
      {"url": _s("Полная ссылка instagram.com")}, ["url"])

_tool("download_video",
      "Скачать видео/медиа по ссылке с YouTube, TikTok, X (Twitter) или Reddit.",
      _tool_download_video,
      {"url": _s("Ссылка на видео")}, ["url"])

_tool("music_send_track",
      "Найти песню и СРАЗУ отправить её аудиофайлом. Вызывай на «скинь/кинь/пришли/включи песню X», "
      "когда понятно, какой именно трек нужен (исполнитель и/или название).",
      _tool_music_send_track,
      {"query": _s("Исполнитель и название, например «Rammstein Du Hast»")}, ["query"])

_tool("music_search",
      "Показать список найденных треков с кнопками на выбор. Вызывай на «найди песни/треки …», "
      "когда запрос расплывчатый (жанр, исполнитель целиком, «что-нибудь вроде …»).",
      _tool_music_search,
      {"query": _s("Поисковый запрос по музыке")}, ["query"])

_tool("music_charts", "Показать музыкальный чарт (популярные треки сейчас).", _tool_music_charts)

_tool("anime_search",
      "Найти аниме по названию (список с кнопками карточек).",
      _tool_anime_search,
      {"query": _s("Название аниме")}, ["query"])

_tool("anime_random", "Показать случайное аниме.", _tool_anime_random)

_tool("anime_recommend",
      "Подобрать аниме под настроение/вайб/описание (ИИ-подборка из 4 тайтлов). "
      "Вызывай на «посоветуй аниме», «что посмотреть из аниме».",
      _tool_anime_recommend,
      {"vibe": _s("Пожелания: жанр, настроение, что нравится/не нравится")}, ["vibe"])

_tool("anime_top", "Показать топ аниме, которое смотрят в этой группе.", _tool_anime_top)

_tool("anime_schedule", "Показать календарь выхода новых серий аниме на неделю.", _tool_anime_schedule)

_tool("product_search",
      "Найти товар в магазине (Wildberries) с ценами и рейтингами. Вызывай на «найди/посмотри на озоне/"
      "вайлдберриз/где купить …». Можно использовать слова-фильтры в запросе: «до 3000», «от 50000», "
      "«дешёвые», «рейтинг», «скидки», «новинки».",
      _tool_product_search,
      {"query": _s("Что искать, вместе с ценовыми фильтрами, если они названы")}, ["query"])

_tool("duty_schedule",
      "Показать график нарядов (кто на сводках и процедурке) на сегодня, завтра или неделю.",
      _tool_duty_schedule,
      {"when": {"type": "STRING", "description": "today, tomorrow или week", "enum": ["today", "tomorrow", "week"]}})

_tool("dembel_timer",
      "Показать дембель-таймер: общую сводку или карточку одного человека.",
      _tool_dembel_timer,
      {"person": _s("Имя из таймера; пусто — общая сводка")})

_tool("reddit_feed", "Прислать пост из ленты (мемы, картинки, видео из Reddit).", _tool_reddit_feed)

_tool("generate_image",
      "Нарисовать/сгенерировать картинку по описанию. Описание переведи на английский.",
      _tool_generate_image,
      {"prompt": _s("Подробное описание картинки на английском")}, ["prompt"])

_tool("web_search",
      "Найти информацию в интернете: погода, новости, курсы, факты, расписания, всё актуальное. "
      "Результат вернётся тебе — на его основе ответь пользователю сам.",
      _tool_web_search,
      {"query": _s("Поисковый запрос")}, ["query"], terminal=False)

_tool("remember_fact",
      "Запомнить факт о пользователе на будущее, если он явно просит «запомни …».",
      _tool_remember_fact,
      {"fact": _s("Короткая формулировка факта")}, ["fact"], terminal=False)


TOOLS_HINT = (
    "\n\nИНСТРУМЕНТЫ: у тебя есть функции бота (Instagram, музыка, аниме, товары, скачивание видео, "
    "наряды, дембель, картинки, поиск в интернете). Если просьба — это действие, которое покрывает "
    "функция, ВЫЗОВИ её, а не отвечай «я не умею» или «воспользуйся командой». Для обычного разговора, "
    "шуток и советов функции не вызывай. Не выдумывай параметры: если не хватает username, названия "
    "или ссылки — коротко спроси. Функции сами отправляют результат в чат, поэтому не пересказывай его. "
    "Если функция вернула ОШИБКА — объясни пользователю своими словами и предложи вариант. "
    "Ответы после функций — 1 короткая фраза в твоём характере."
)


# ══════════════════════════════════════════════════════════════════════════════
# Выполнение и основной цикл
# ══════════════════════════════════════════════════════════════════════════════
def _rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    q = _RATE[user_id]
    while q and now - q[0] > RATE_LIMIT_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT_CALLS:
        return True
    q.append(now)
    return False


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> tuple[bool, str]:
    """Возвращает (ok, текст результата для модели)."""
    tool = TOOLS.get(name)
    if not tool:
        return False, f"ОШИБКА: функции {name} не существует."
    if _rate_limited(ctx.user_id):
        return False, "ОШИБКА: слишком много вызовов функций подряд, попроси пользователя подождать минуту."
    clean_args = {k: v for k, v in (args or {}).items() if k in tool.properties}
    try:
        result = await tool.handler(ctx, **clean_args)
    except Exception as e:
        logger.exception(f"AI tool {name} failed: {e}")
        return False, f"ОШИБКА при выполнении {name}: {type(e).__name__}: {e}"
    logger.info(f"🛠 AI tool {name}({clean_args}) → {result[:120]!r}")
    return (not result.startswith("ОШИБКА")), result


def _build_gemini_tool(gt):
    decls = []
    for t in TOOLS.values():
        params = None
        if t.properties:
            params = {"type": "OBJECT", "properties": t.properties, "required": t.required}
        decls.append(gt.FunctionDeclaration(name=t.name, description=t.description, parameters=params))
    return gt.Tool(function_declarations=decls)


async def _stream_step(client, gt, message, contents, config):
    """
    Один запрос к модели со стримингом. Возвращает (text, function_call_parts, model_parts).
    Текст плавно показывается в сообщении, как в обычном _stream_reply.
    """
    from ai_handlers import _md_to_tg_html

    text = ""
    call_parts = []
    status = None
    last_edit = 0.0

    stream = await client.aio.models.generate_content_stream(
        model=MODEL, contents=contents, config=config,
    )
    async for chunk in stream:
        cands = getattr(chunk, "candidates", None) or []
        if not cands or not cands[0].content or not cands[0].content.parts:
            continue
        for part in cands[0].content.parts:
            if getattr(part, "function_call", None):
                call_parts.append(part)
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                text += part.text
        if text.strip():
            now = asyncio.get_running_loop().time()
            if status is None:
                status = await message.reply("▌")
                last_edit = now
            elif now - last_edit > 1.1:
                try:
                    await status.edit_text(text[:3900] + " ▌")
                    last_edit = now
                except Exception:
                    pass

    final = text.strip()[:4000]
    if status is not None:
        if final:
            try:
                await status.edit_text(_md_to_tg_html(final), parse_mode="HTML")
            except Exception:
                try:
                    await status.edit_text(final)
                except Exception:
                    pass
        else:
            try:
                await status.delete()
            except Exception:
                pass
    elif final:
        # текст пришёл одним куском без промежуточных правок
        try:
            await message.reply(_md_to_tg_html(final), parse_mode="HTML")
        except Exception:
            await message.reply(final)
    return final, call_parts


async def run_ai_with_tools(
    message: types.Message,
    prompt: str,
    system_instruction: str,
    user_id: int,
) -> tuple[bool, str | None]:
    """
    Возвращает (handled, text).
    handled=False — до выполнения каких-либо функций что-то сломалось, вызывающему стоит
    откатиться на обычный _stream_reply (чтобы пользователь не остался без ответа).
    """
    from ai_handlers import get_gemini

    client = get_gemini()
    if not client or not TOOLS_ENABLED:
        return False, None

    from google.genai import types as gt

    config = gt.GenerateContentConfig(
        system_instruction=system_instruction + TOOLS_HINT,
        tools=[_build_gemini_tool(gt)],
        automatic_function_calling=gt.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = [gt.Content(role="user", parts=[gt.Part(text=prompt)])]
    ctx = ToolContext(message=message, user_id=user_id, chat_id=message.chat.id)

    executed_any = False
    last_text = ""

    for _ in range(MAX_STEPS):
        try:
            text, call_parts = await _stream_step(client, gt, message, contents, config)
        except Exception as e:
            logger.warning(f"AI tools: ошибка запроса к Gemini: {e}")
            return (executed_any, last_text or None)

        if text:
            last_text = text
        if not call_parts:
            return True, last_text or None

        # Возвращаем модели её собственный ход (с function_call и thought-signature).
        model_parts = ([gt.Part(text=text)] if text else []) + list(call_parts)
        contents.append(gt.Content(role="model", parts=model_parts))

        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
        except Exception:
            pass

        response_parts = []
        all_ok_terminal = True
        for part in call_parts[:MAX_CALLS_PER_STEP]:
            fc = part.function_call
            ok, result = await execute_tool(fc.name, dict(fc.args or {}), ctx)
            executed_any = True
            tool = TOOLS.get(fc.name)
            if not (ok and tool and tool.terminal):
                all_ok_terminal = False
            response_parts.append(gt.Part.from_function_response(
                name=fc.name, response={"result": result}))
        # Лишние вызовы (сверх лимита) тоже надо закрыть ответом, иначе API вернёт 400.
        for part in call_parts[MAX_CALLS_PER_STEP:]:
            response_parts.append(gt.Part.from_function_response(
                name=part.function_call.name,
                response={"result": "ОШИБКА: слишком много функций за раз, не выполнено."}))
            all_ok_terminal = False

        contents.append(gt.Content(role="user", parts=response_parts))

        if all_ok_terminal:
            # Всё уже показано в чате карточками/файлами — второй запрос к модели не нужен.
            return True, last_text or None

    return True, last_text or None
