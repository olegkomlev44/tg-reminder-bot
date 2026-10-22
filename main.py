"""
main.py — точка входа бота. Только импорты, регистрация хэндлеров и запуск.
Вся логика вынесена в отдельные модули:
  config.py          — константы, CHAT_HISTORY (per-chat), расписание
  duty_handlers.py   — наряды, расписание, планировщик
  ai_handlers.py     — Gemini: чат, /meme, /tldr, /poll, /imagen, фото, документы, ГС/кружочки
  vision_handlers.py — вопросы реплаем по фото/PDF/видео, мемы «сделай мем про…», /tr, /explain
  fun_handlers.py    — /pick, /who (жребий с анимацией), /roll, /coin
  places_handlers.py — /nearby и геопозиция: кафе, аптеки, магазины
  film_handler.py    — /film через TMDB
  freebies.py        — /free: раздачи Epic/Steam и уведомления
  music_links.py     — ссылки Spotify / Яндекс.Музыка / VK → поиск трека
  music_handlers.py  — музыка, плейлисты, DJ, inline
  anixart_handler.py — аниме поиск + inline
  ozon_handler.py    — поиск товаров
  instagram_handlers.py, feed_handlers.py, media_downloader.py — как раньше
"""

import asyncio
import logging
import sys
import traceback
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import TOKEN, TIMEZONE
from db import init_db
from web_server import start_web_server
from instagram_handlers import (
    register_ig_handlers, ig_checker_task, ig_session_health_task, _set_bot_ref,
    inline_instagram, parse_ig_inline,
)
from media_downloader import register_media_handlers
from feed_handlers import register_feed_handlers

# ── Хэндлеры из модулей ───────────────────────────────────────────────────────
from duty_handlers import (
    load_data, auto_delete_later, _startup_selftest,
    cmd_start, cmd_today, cmd_tomorrow, cmd_week, cmd_log, cmd_remind_now, cmd_settings,
    cmd_getchatid,
    send_svodki_reminder, send_procedura_reminder, send_monday_briefing,
    send_sunday_summary, daily_pinned_update, send_retry_reminder,
    callback_done, callback_retry, callback_toggle_reminders, callback_toggle_ai_replies,
    callback_set_chat, process_chat_id, callback_update_pinned, callback_register_personal,
    process_register, callback_pick_svodki, callback_pick_procedura,
    callback_set_svodki_idx, callback_set_proc_idx,
    callback_show_svodki_list, callback_show_procedura_list,
    callback_back_settings, callback_back_main,
    callback_dembel_card, cmd_dembel,
    AdminStates,
)
from ai_handlers import (
    cmd_weather, cmd_meme, cmd_tldr, cmd_pollinations, cmd_imagen,
    handle_photo, handle_document, handle_voice, handle_ai_chat,
    cmd_persona, callback_persona_set, cmd_ai,
)
from music_handlers import (
    cmd_music_find, cmd_charts, cmd_music_dashboard, cmd_my_music,
    cmd_playlists, cmd_queue, cmd_dj, cmd_wrapped,
    inline_music_search,
    callback_download_music, callback_lyrics, callback_music_recs,
    callback_music_fav, callback_music_page, callback_start_wave, callback_radar,
    callback_show_favs, callback_show_hist, callback_back_to_dash,
    callback_pl_pick, callback_pl_new, callback_pl_add, callback_pl_open,
    callback_pl_shuffle, callback_pl_to_queue, callback_pl_cancel,
    callback_pl_delete_menu, callback_pl_delete, callback_show_playlists,
    callback_queue_add, callback_queue_next, callback_queue_clear,
    callback_artist_page, callback_artist_sub, callback_artist_unsub,
    callback_dj_vote, callback_dj_finish,
    MusicStates, process_playlist_name,
)
from anixart_handler import (
    register_anixart_handlers, inline_anime, check_anime_episodes,
    anixart_startup_selftest, close_anixart_session,
)
from ozon_handler import cmd_ozon
from fun_handlers import register_fun_handlers
from places_handlers import register_places_handlers
from film_handler import register_film_handlers
from freebies import register_freebies_handlers, check_freebies
from music_links import register_music_link_handlers
from vision_handlers import register_vision_handlers

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


async def _webhook_janitor(bot: Bot, interval: int = 90) -> None:
    """
    Каждые `interval` секунд проверяет и снимает вебхук, если он появился,
    чтобы getUpdates не залипал в TelegramConflictError.
    """
    while True:
        try:
            info = await bot.get_webhook_info()
            if info.url:
                logger.warning(
                    f"⚠️ Обнаружен активный вебхук ({info.url}) — снимаю, чтобы polling не залипал."
                )
                await bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logger.error(f"Webhook janitor error: {e}")
        await asyncio.sleep(interval)


async def cmd_help(message: types.Message) -> None:
    """/help — краткая справка по командам бота."""
    text = (
        "🤖 <b>команды бота</b>\n\n"
        "🧠 <b>ии (gemini 2.5)</b>\n"
        "пиши в личку, упомяни или ответь боту — он сам запустит нужную функцию "
        "(инста, музыка, аниме, товары, видео, наряды, погода)\n"
        "/ai — ask, roast, debate, story, memory, forget\n"
        "/persona — сменить персону\n"
        "/meme, /tldr, /poll, /imagen — мемы, выжимка, картинки\n"
        "ответь на картинку: «сделай мем про …»\n"
        "ответь на фото/PDF/видео/ГС: «переведи», «что написано», «объясни мем», «о чём документ»\n\n"
        "🎲 <b>выбор и жребий</b>\n"
        "/pick кофе или чай, /who кто моет посуду, /roll, /coin\n\n"
        "📍 <b>рядом</b>\n"
        "/nearby — кафе, аптеки, магазины (скинь геопозицию или адрес)\n\n"
        "🍿 <b>фильмы и сериалы</b>\n"
        "/film &lt;название&gt; (trending, random, ai &lt;вайб&gt;)\n\n"
        "🎁 <b>бесплатные игры</b>\n"
        "/free — что раздают сейчас, /free sale — скидки, /free on|off — уведомления\n\n"
        "📋 <b>наряды и дембель</b>\n"
        "/svodki — напомнить сейчас\n"
        "/dembel — дембельский таймер\n\n"
        "🎵 <b>музыка</b>\n"
        "/find, /charts, /music, /my_music, /playlists, /queue, /dj, /wrapped\n\n"
        "📸 <b>instagram</b>\n"
        "/ig &lt;username&gt;, /igdl &lt;ссылка&gt;, /ig_subs, /ig_popular\n\n"
        "📥 <b>медиа</b>\n"
        "ссылка YouTube/TikTok/X/Reddit/Instagram/Pinterest/VK Видео/Twitch-клип/Threads или /dl &lt;ссылка&gt;\n"
        "ссылка Spotify/Яндекс.Музыки/VK Музыки → найду трек\n\n"
        "🎬 <b>аниме</b>\n"
        "/anime &lt;название&gt; (random, ai, top, schedule, profile)\n\n"
        "🔥 <b>лента</b>\n"
        "/feed, /saved, /feed_settings\n\n"
        "🛒 /ozon — поиск товаров\n"
        "🌦 /pogoda, /chatid\n\n"
        "жми /start, если нужна клавиатура с кнопками"
    )
    await message.answer(text, parse_mode="HTML")


async def main():
    logger.info("🟡 инициализация бота...")
    _startup_selftest()
    init_db()
    await start_web_server()

    bot = Bot(token=TOKEN)
    _set_bot_ref(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    dp  = Dispatcher(storage=MemoryStorage())

    # ── Сторонние роутеры ─────────────────────────────────────────────────────
    register_ig_handlers(dp)
    register_feed_handlers(dp)
    register_anixart_handlers(dp)
    await anixart_startup_selftest()

    # ── Inline ────────────────────────────────────────────────────────────────
    async def inline_query_router(iq: types.InlineQuery):
        raw = iq.query or ""
        if parse_ig_inline(raw) is not None:      # «ig natgeo» / «ig:natgeo» / ссылка на профиль
            logger.info(f"📸 inline → instagram: {raw!r}")
            await inline_instagram(iq)
        elif raw.strip().startswith("a:"):
            logger.info(f"🎬 inline → anime: {raw!r}")
            await inline_anime(iq)
        else:
            logger.info(f"🎵 inline → music: {raw!r}")
            await inline_music_search(iq)

    dp.inline_query.register(inline_query_router)

    # ── Команды ───────────────────────────────────────────────────────────────
    dp.message.register(cmd_ozon,            Command("ozon"))
    dp.message.register(cmd_start,           Command("start"))
    dp.message.register(cmd_getchatid,       Command("chatid"))
    dp.message.register(cmd_pollinations,    Command("poll"))
    dp.message.register(cmd_imagen,          Command("imagen"))
    dp.message.register(cmd_meme,            Command("meme"))
    dp.message.register(cmd_meme,            F.photo & F.caption.startswith("/meme"))
    dp.message.register(cmd_tldr,            Command("tldr"))
    dp.message.register(cmd_music_find,      Command("find"))
    dp.message.register(cmd_charts,          Command("charts"))
    dp.message.register(cmd_music_dashboard, Command("music"))
    dp.message.register(cmd_my_music,        Command("my_music"))
    dp.message.register(cmd_playlists,       Command("playlists"))
    dp.message.register(cmd_queue,           Command("queue"))
    dp.message.register(cmd_dj,              Command("dj"))
    dp.message.register(cmd_wrapped,         Command("wrapped"))
    dp.message.register(cmd_weather,         Command("pogoda"))
    dp.message.register(cmd_dembel,          Command("dembel"))
    dp.message.register(cmd_persona,         Command("persona"))
    dp.message.register(cmd_ai,              Command("ai"))
    dp.message.register(cmd_help,            Command("help"))
    dp.message.register(cmd_today,           Command("наряд"))
    dp.message.register(cmd_remind_now,      Command("svodki"))

    # ── Кнопки клавиатуры ─────────────────────────────────────────────────────
    dp.message.register(cmd_today,      F.text == "📋 Наряд сегодня")
    dp.message.register(cmd_tomorrow,   F.text == "📅 Наряд завтра")
    dp.message.register(cmd_week,       F.text == "📊 Расписание на неделю")
    dp.message.register(cmd_log,        F.text == "📓 Журнал")
    dp.message.register(cmd_remind_now, F.text == "🔔 Напомнить сейчас")
    dp.message.register(cmd_settings,   F.text == "⚙️ Настройки")

    # ── FSM ───────────────────────────────────────────────────────────────────
    dp.message.register(process_chat_id,       AdminStates.waiting_chat_id)
    dp.message.register(process_register,      AdminStates.waiting_register)
    dp.message.register(process_playlist_name, MusicStates.waiting_playlist_name)

    # ── Callbacks — наряды и персоны ──────────────────────────────────────────
    dp.callback_query.register(callback_done,                 F.data.startswith("done:"))
    dp.callback_query.register(callback_retry,                F.data.startswith("retry:"))
    dp.callback_query.register(callback_toggle_reminders,     F.data == "toggle_reminders")
    dp.callback_query.register(callback_toggle_ai_replies,    F.data == "toggle_ai_replies")
    dp.callback_query.register(callback_set_chat,             F.data == "set_chat")
    dp.callback_query.register(callback_register_personal,    F.data == "register_personal")
    dp.callback_query.register(callback_update_pinned,        F.data == "update_pinned")
    dp.callback_query.register(callback_pick_svodki,          F.data == "pick_svodki")
    dp.callback_query.register(callback_pick_procedura,       F.data == "pick_procedura")
    dp.callback_query.register(callback_set_svodki_idx,       F.data.startswith("set_svodki_idx:"))
    dp.callback_query.register(callback_set_proc_idx,         F.data.startswith("set_proc_idx:"))
    dp.callback_query.register(callback_show_svodki_list,     F.data == "show_svodki_list")
    dp.callback_query.register(callback_show_procedura_list,  F.data == "show_procedura_list")
    dp.callback_query.register(callback_back_settings,        F.data == "back_settings")
    dp.callback_query.register(callback_back_main,            F.data == "back_main")
    dp.callback_query.register(callback_dembel_card,          F.data.startswith("dembel_card:"))
    dp.callback_query.register(callback_persona_set,          F.data.startswith("persona_set:"))

    # ── Callbacks — музыка ────────────────────────────────────────────────────
    dp.callback_query.register(callback_download_music,  F.data.startswith("dl_sc:"))
    dp.callback_query.register(callback_music_fav,       F.data.startswith("fav_sc:"))
    dp.callback_query.register(callback_music_recs,      F.data.startswith("rec_sc:"))
    dp.callback_query.register(callback_music_page,      F.data.startswith("mus_pg:"))
    dp.callback_query.register(callback_lyrics,          F.data.startswith("lyrics:"))
    dp.callback_query.register(callback_show_favs,       F.data.startswith("show_favs:"))
    dp.callback_query.register(callback_show_hist,       F.data.startswith("show_hist:"))
    dp.callback_query.register(callback_back_to_dash,    F.data == "back_to_dash")
    dp.callback_query.register(callback_start_wave,      F.data == "start_wave")
    dp.callback_query.register(callback_radar,           F.data == "radar_releases")
    dp.callback_query.register(callback_pl_pick,         F.data.startswith("pl_pick:"))
    dp.callback_query.register(callback_pl_new,          F.data.startswith("pl_new:"))
    dp.callback_query.register(callback_pl_add,          F.data.startswith("pl_add:"))
    dp.callback_query.register(callback_pl_open,         F.data.startswith("pl_open:"))
    dp.callback_query.register(callback_pl_shuffle,      F.data.startswith("pl_shuffle:"))
    dp.callback_query.register(callback_pl_to_queue,     F.data.startswith("pl_to_queue:"))
    dp.callback_query.register(callback_pl_cancel,       F.data == "pl_cancel")
    dp.callback_query.register(callback_pl_delete_menu,  F.data == "pl_delete_menu")
    dp.callback_query.register(callback_pl_delete,       F.data.startswith("pl_delete:"))
    dp.callback_query.register(callback_show_playlists,  F.data == "show_playlists")
    dp.callback_query.register(callback_queue_add,       F.data.startswith("queue_add:"))
    dp.callback_query.register(callback_queue_next,      F.data == "queue_next")
    dp.callback_query.register(callback_queue_clear,     F.data == "queue_clear")
    dp.callback_query.register(lambda cb: cmd_queue(cb.message), F.data == "queue_show")
    dp.callback_query.register(callback_artist_page,     F.data.startswith("artist_page:"))
    dp.callback_query.register(callback_artist_sub,      F.data.startswith("artist_sub:"))
    dp.callback_query.register(callback_artist_unsub,    F.data.startswith("artist_unsub:"))
    dp.callback_query.register(callback_dj_vote,         F.data.startswith("dj_vote:"))
    dp.callback_query.register(callback_dj_finish,       F.data == "dj_finish")

    # ── Новые модули (ДО handle_photo/handle_document/handle_ai_chat — порядок важен) ──
    register_fun_handlers(dp)
    register_places_handlers(dp)
    register_film_handlers(dp)
    register_freebies_handlers(dp)
    register_music_link_handlers(dp)
    register_vision_handlers(dp)

    # ── Медиа, документы, ГС/кружочки и текстовый ИИ-чат ──────────────────────
    dp.message.register(handle_photo,    F.photo)
    dp.message.register(handle_document, F.document)
    dp.message.register(handle_voice,    F.voice | F.video_note)
    register_media_handlers(dp)
    dp.message.register(handle_ai_chat,  F.text & ~F.text.startswith("/"))

    # ── Планировщик ───────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_svodki_reminder,    CronTrigger(hour=18, minute=0,  timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_procedura_reminder, CronTrigger(hour=22, minute=0,  timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_monday_briefing,    CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_sunday_summary,     CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(daily_pinned_update,     CronTrigger(hour=0, minute=1,   timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_retry_reminder,     "interval", minutes=15, args=[bot, "svodki"])
    scheduler.add_job(send_retry_reminder,     "interval", minutes=15, args=[bot, "proc"])
    scheduler.add_job(check_anime_episodes,    "interval", hours=4, args=[bot])
    scheduler.add_job(check_freebies,          "interval", hours=2, args=[bot], id="freebies",
                      max_instances=1, coalesce=True,
                      next_run_time=datetime.now(TIMEZONE) + timedelta(seconds=45))
    scheduler.start()

    asyncio.create_task(ig_checker_task(bot))
    asyncio.create_task(ig_session_health_task(bot))
    asyncio.create_task(_webhook_janitor(bot))

    # ── Глобальный обработчик ошибок ─────────────────────────────────────────
    @dp.errors()
    async def _error_handler(event: types.ErrorEvent):
        logger.exception(
            f"❌ Ошибка [{type(event.exception).__name__}]: {event.exception}",
            exc_info=event.exception,
        )

    logger.info("✅ бот запущен. наряды под контролем 🫡")
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        scheduler.shutdown()
        await close_anixart_session()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        tb = traceback.format_exc()
        logger.critical("🔴 БОТ УПАЛ:\n" + tb)
        sys.exit(1)
