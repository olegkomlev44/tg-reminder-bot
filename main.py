"""
main.py — точка входа бота. Только импорты, регистрация хэндлеров и запуск.
Вся логика вынесена в отдельные модули:
  config.py         — константы, CHAT_HISTORY (per-chat), расписание
  duty_handlers.py  — наряды, расписание, планировщик
  ai_handlers.py    — Gemini: чат, /meme, /tldr, /poll, /imagen, фото
  music_handlers.py — музыка, плейлисты, DJ, inline
  anixart_handler.py — аниме поиск + inline
  ozon_handler.py   — поиск товаров
  instagram_handlers.py, feed_handlers.py, media_downloader.py — как раньше
"""

import asyncio
import logging
import sys
import traceback

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import TOKEN, TIMEZONE
from db import init_db
from web_server import start_web_server
from instagram_handlers import register_ig_handlers, ig_checker_task, ig_session_health_task, _set_bot_ref
from media_downloader import register_media_handlers
from feed_handlers import register_feed_handlers

# ── Хэндлеры из новых модулей ─────────────────────────────────────────────────
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
    handle_photo, handle_ai_chat,
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
from anixart_handler import register_anixart_handlers, inline_anime, check_anime_episodes
from ozon_handler import cmd_ozon

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


async def main():
    logger.info("🟡 инициализация бота...")
    _startup_selftest()
    init_db()
    await start_web_server()

    bot = Bot(token=TOKEN)
    _set_bot_ref(bot)
    dp  = Dispatcher(storage=MemoryStorage())

    # ── Сторонние роутеры ─────────────────────────────────────────────────────
    register_ig_handlers(dp)
    register_feed_handlers(dp)
    register_anixart_handlers(dp)

    # ── Inline ────────────────────────────────────────────────────────────────
    # Telegram присылает боту ровно один inline_query на запрос. Раньше на это
    # событие вешались два отдельных хэндлера с противоположными фильтрами
    # (~startswith / startswith) — победитель зависел от порядка регистрации
    # и от того, не перехватит ли запрос ещё что-то раньше (ig/feed роутеры).
    # Теперь один диспетчер сам решает маршрут — единственная точка, которую
    # нужно смотреть при отладке через логи BotHost.
    async def inline_query_router(iq: types.InlineQuery):
        raw = iq.query or ""
        if raw.strip().startswith("a:"):
            logger.info(f"🎬 inline → anime: {raw!r}")
            await inline_anime(iq)
        else:
            logger.info(f"🎵 inline → music: {raw!r}")
            await inline_music_search(iq)

    dp.inline_query.register(inline_query_router)

    # ── Команды ───────────────────────────────────────────────────────────────
    dp.message.register(cmd_ozon,         Command("ozon"))
    dp.message.register(cmd_start,        Command("start"))
    dp.message.register(cmd_getchatid,    Command("chatid"))
    dp.message.register(cmd_pollinations, Command("poll"))
    dp.message.register(cmd_imagen,       Command("imagen"))
    dp.message.register(cmd_meme,         Command("meme"))
    dp.message.register(cmd_tldr,         Command("tldr"))
    dp.message.register(cmd_music_find,   Command("find"))
    dp.message.register(cmd_charts,       Command("charts"))
    dp.message.register(cmd_music_dashboard, Command("music"))
    dp.message.register(cmd_my_music,     Command("my_music"))
    dp.message.register(cmd_playlists,    Command("playlists"))
    dp.message.register(cmd_queue,        Command("queue"))
    dp.message.register(cmd_dj,           Command("dj"))
    dp.message.register(cmd_wrapped,      Command("wrapped"))
    dp.message.register(cmd_weather,      Command("pogoda"))
    dp.message.register(cmd_dembel,       Command("dembel"))

    # ── Кнопки клавиатуры ─────────────────────────────────────────────────────
    dp.message.register(cmd_today,      F.text == "📋 Наряд сегодня")
    dp.message.register(cmd_tomorrow,   F.text == "📅 Наряд завтра")
    dp.message.register(cmd_week,       F.text == "📊 Расписание на неделю")
    dp.message.register(cmd_log,        F.text == "📓 Журнал")
    dp.message.register(cmd_remind_now, F.text == "🔔 Напомнить сейчас")
    dp.message.register(cmd_settings,   F.text == "⚙️ Настройки")

    # ── FSM ───────────────────────────────────────────────────────────────────
    dp.message.register(process_chat_id,      AdminStates.waiting_chat_id)
    dp.message.register(process_register,     AdminStates.waiting_register)
    dp.message.register(process_playlist_name, MusicStates.waiting_playlist_name)

    # ── Callbacks — наряды ────────────────────────────────────────────────────
    dp.callback_query.register(callback_done,             F.data.startswith("done:"))
    dp.callback_query.register(callback_retry,            F.data.startswith("retry:"))
    dp.callback_query.register(callback_toggle_reminders, F.data == "toggle_reminders")
    dp.callback_query.register(callback_toggle_ai_replies,F.data == "toggle_ai_replies")
    dp.callback_query.register(callback_set_chat,         F.data == "set_chat")
    dp.callback_query.register(callback_register_personal,F.data == "register_personal")
    dp.callback_query.register(callback_update_pinned,    F.data == "update_pinned")
    dp.callback_query.register(callback_pick_svodki,      F.data == "pick_svodki")
    dp.callback_query.register(callback_pick_procedura,   F.data == "pick_procedura")
    dp.callback_query.register(callback_set_svodki_idx,   F.data.startswith("set_svodki_idx:"))
    dp.callback_query.register(callback_set_proc_idx,     F.data.startswith("set_proc_idx:"))
    dp.callback_query.register(callback_show_svodki_list, F.data == "show_svodki_list")
    dp.callback_query.register(callback_show_procedura_list, F.data == "show_procedura_list")
    dp.callback_query.register(callback_back_settings,    F.data == "back_settings")
    dp.callback_query.register(callback_back_main,        F.data == "back_main")
    dp.callback_query.register(callback_dembel_card,      F.data.startswith("dembel_card:"))

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

    # Аниме (cmd_anime, все anix:* callbacks, FSM привязки аккаунта) регистрируются
    # через register_anixart_handlers(dp) выше — единым роутером, чтобы не забывать
    # вручную добавлять сюда новые хендлеры (как раньше забыли cb_search_page).

    # ── Фото и текст (всегда последними) ─────────────────────────────────────
    dp.message.register(handle_photo,    F.photo)
    register_media_handlers(dp)
    dp.message.register(handle_ai_chat, F.text & ~F.text.startswith("/"))

    # ── Планировщик ───────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_svodki_reminder,   CronTrigger(hour=18, minute=0,  timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_procedura_reminder,CronTrigger(hour=22, minute=0,  timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_monday_briefing,   CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_sunday_summary,    CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE), args=[bot])
    scheduler.add_job(daily_pinned_update,    CronTrigger(hour=0, minute=1,   timezone=TIMEZONE), args=[bot])
    scheduler.add_job(send_retry_reminder,    "interval", minutes=15, args=[bot, "svodki"])
    scheduler.add_job(send_retry_reminder,    "interval", minutes=15, args=[bot, "proc"])
    scheduler.add_job(check_anime_episodes,   "interval", hours=4, args=[bot])  # подписки на новые серии
    scheduler.start()

    asyncio.create_task(ig_checker_task(bot))
    asyncio.create_task(ig_session_health_task(bot))

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
