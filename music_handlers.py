"""
music_handlers.py — всё что связано с музыкой:
cmd_music_find, cmd_charts, cmd_music_dashboard, cmd_my_music,
cmd_playlists, cmd_queue, cmd_dj, cmd_wrapped,
inline_music_search, все music-callbacks, make_track_card, _make_wrapped_card.
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import textwrap
import time
import urllib.parse
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InlineQueryResultCachedAudio,
    InputTextMessageContent, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton,
)

from config import BASE_DIR, DJ_SESSIONS
from db import (
    get_cached_file_id, save_cached_file_id,
    save_music_fav, get_music_favs, log_track_history, get_user_history,
)
from duty_handlers import (
    auto_delete_later, load_data,
    get_playlists, save_playlist_track, delete_playlist,
    queue_push, queue_pop, queue_list, queue_clear,
    get_artist_subs, add_artist_sub, remove_artist_sub,
    get_explicit_tag,
)
from music_engine import music_engine, add_id3_tags
from recsys import generate_wave_tracks

logger = logging.getLogger(__name__)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(6)

# ── FSM ───────────────────────────────────────────────────────────────────────
class MusicStates(StatesGroup):
    waiting_playlist_name = State()


# ── Визуальные хелперы ────────────────────────────────────────────────────────
def _dominant_color(img_bytes: bytes) -> tuple:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((64, 64))
        q   = img.quantize(colors=5, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette()[:15]
        best, best_sat = (80, 80, 80), 0.0
        for i in range(5):
            r, g, b = pal[i*3], pal[i*3+1], pal[i*3+2]
            mx, mn  = max(r,g,b), min(r,g,b)
            sat = (mx - mn) / mx if mx else 0
            if sat > best_sat:
                best_sat = sat; best = (r, g, b)
        return best
    except Exception:
        return (30, 30, 40)


def make_track_card(artist: str, title: str, duration: str,
                    source: str, cover_bytes: bytes | None, genre: str = "") -> bytes:
    W, H, COVER_W = 900, 300, 300
    dom  = _dominant_color(cover_bytes) if cover_bytes else (30, 30, 40)
    dark = tuple(max(0, c - 80) for c in dom)

    card = Image.new("RGB", (W, H), dark)
    draw = ImageDraw.Draw(card)
    for x in range(COVER_W, W):
        t = (x - COVER_W) / (W - COVER_W)
        r = int(dom[0]*(1-t) + dark[0]*t)
        g = int(dom[1]*(1-t) + dark[1]*t)
        b = int(dom[2]*(1-t) + dark[2]*t)
        draw.line([(x,0),(x,H)], fill=(r,g,b))

    if cover_bytes:
        try:
            ci = Image.open(io.BytesIO(cover_bytes)).convert("RGB").resize((COVER_W, H), Image.Resampling.LANCZOS)
            card.paste(ci, (0, 0))
        except Exception:
            pass
    for x in range(20):
        draw.line([(COVER_W+x, 0),(COVER_W+x, H)], fill=(*dom, int(200*(1-x/20))))

    font_dir = os.path.join(BASE_DIR, "fonts")
    try:
        f_big   = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 36)
        f_med   = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans-Bold.ttf"), 22)
        f_small = ImageFont.truetype(os.path.join(font_dir, "DejaVuSans.ttf"), 18)
    except Exception:
        f_big = f_med = f_small = ImageFont.load_default()

    TX   = COVER_W + 28
    luma = 0.299*dom[0] + 0.587*dom[1] + 0.114*dom[2]
    fg   = (255,255,255) if luma < 140 else (20,20,20)
    fg_d = tuple(min(255, c+60) for c in fg) if luma < 140 else (80,80,80)

    draw.text((TX, 55), (artist[:32]+"…" if len(artist)>32 else artist), font=f_med, fill=fg_d)
    title_s = title[:44]+"…" if len(title)>44 else title
    y = 90
    for line in textwrap.wrap(title_s, 22)[:2]:
        draw.text((TX, y), line, font=f_big, fill=fg); y += 44
    if genre and genre.lower() not in ("неизвестен","мультиплатформа"):
        clean = re.sub(r'[^a-zA-Zа-яА-Я0-9 ]','',genre).strip()
        if clean:
            draw.text((TX, y+10), f"#{clean.replace(' ','_').lower()}", font=f_small, fill=fg_d)
    src_icon = {"SoundCloud":"🔊","YouTube Music":"▶️"}.get(source,"🎵")
    draw.text((TX, H-42), f"{src_icon} {source}  ·  ⏱ {duration}", font=f_small, fill=fg_d)
    draw.rectangle([(0,H-4),(W,H)], fill=dom)

    out = io.BytesIO()
    card.save(out, format="JPEG", quality=88)
    return out.getvalue()


def _make_wrapped_card(hist: list, user_name: str) -> bytes:
    W, H = 900, 1100
    artists: dict = {}; sources: dict = {}
    for t in hist:
        a = t.get("artist","?"); s = t.get("source","SoundCloud")
        artists[a] = artists.get(a,0) + 1
        sources[s] = sources.get(s,0) + 1
    top_artists = sorted(artists.items(), key=lambda x: x[1], reverse=True)[:3]
    top_tracks  = hist[:5]
    total = len(hist)
    persona = "🔥 Марафонец" if total>=50 else "🎧 Меломан" if total>=20 else "🎵 Слушатель" if total>=10 else "🌱 Новичок"

    font_dir = os.path.join(BASE_DIR, "fonts")
    try:
        f_hero  = ImageFont.truetype(os.path.join(font_dir,"DejaVuSans-Bold.ttf"),52)
        f_big   = ImageFont.truetype(os.path.join(font_dir,"DejaVuSans-Bold.ttf"),34)
        f_med   = ImageFont.truetype(os.path.join(font_dir,"DejaVuSans-Bold.ttf"),24)
        f_small = ImageFont.truetype(os.path.join(font_dir,"DejaVuSans.ttf"),20)
    except Exception:
        f_hero = f_big = f_med = f_small = ImageFont.load_default()

    img = Image.new("RGB",(W,H),(10,5,20)); draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y/H
        draw.line([(0,y),(W,y)], fill=(int(80*(1-t)+10*t), int(0*(1-t)+5*t), int(120*(1-t)+20*t)))

    draw = ImageDraw.Draw(img)
    draw.text((W//2,55),"ТВОЯ МУЗЫКА",font=f_hero,fill=(220,180,255),anchor="mm")
    draw.text((W//2,115),f"@{user_name}",font=f_med,fill=(160,120,210),anchor="mm")
    draw.line([(60,145),(W-60,145)],fill=(100,50,160),width=2)
    draw.text((60,165),"🎵 Прослушано треков:",font=f_med,fill=(180,180,180))
    draw.text((60,198),str(total),font=f_hero,fill=(255,255,255))
    draw.text((200,218),persona,font=f_big,fill=(220,160,255))
    draw.line([(60,270),(W-60,270)],fill=(70,40,110),width=1)
    draw.text((60,290),"👑 Топ артисты",font=f_big,fill=(220,180,255))
    for i,(art,cnt) in enumerate(top_artists):
        y = 335+i*52
        draw.text((60,y),f"{'🥇🥈🥉'[i]} {art[:30]}",font=f_med,fill=(255,255,255))
        draw.text((W-70,y),f"{cnt} тр.",font=f_small,fill=(160,120,210),anchor="ra")
    draw.line([(60,500),(W-60,500)],fill=(70,40,110),width=1)
    draw.text((60,520),"🎧 Последние 5 треков",font=f_big,fill=(220,180,255))
    for i,t in enumerate(top_tracks):
        y = 565+i*48
        draw.text((60,y),f"{i+1}.",font=f_med,fill=(140,100,200))
        line = f"{t.get('artist','?')} — {t.get('title','?')}"
        draw.text((100,y),(line[:41]+"…" if len(line)>42 else line),font=f_small,fill=(230,230,230))
    draw.line([(60,810),(W-60,810)],fill=(70,40,110),width=1)
    draw.text((60,830),"📊 Источники",font=f_big,fill=(220,180,255))
    src_colors={"SoundCloud":(255,85,0),"YouTube Music":(255,0,0)}
    bar_y=878
    for src,cnt in sorted(sources.items(),key=lambda x:x[1],reverse=True):
        ratio=cnt/total if total else 0; bar_w=max(4,int((W-120)*ratio))
        col=src_colors.get(src,(120,80,200))
        draw.rounded_rectangle([(60,bar_y),(60+bar_w,bar_y+36)],radius=8,fill=col)
        draw.text((70,bar_y+8),f"{src}: {cnt} ({int(ratio*100)}%)",font=f_small,fill=(255,255,255))
        bar_y+=50
    draw.line([(60,H-60),(W-60,H-60)],fill=(100,50,160),width=2)
    draw.text((W//2,H-35),f"🤖 generated by бот • {datetime.now().strftime('%d.%m.%Y')}",font=f_small,fill=(100,80,140),anchor="mm")

    out=io.BytesIO(); img.save(out,format="JPEG",quality=88); return out.getvalue()


# ── Анимации ──────────────────────────────────────────────────────────────────
async def animate_loading(msg):
    frames = [
        "`[░░░░░░░░░░] 0%  — Инициализация...`",
        "`[██░░░░░░░░] 20% — Поиск потока...`",
        "`[████░░░░░░] 40% — Качаю...`",
        "`[██████░░░░] 60% — Обрабатываю...`",
        "`[████████░░] 80% — Финальные штрихи...`",
        "`[██████████] 99% — Почти готово...`",
    ]
    try:
        for frame in frames:
            await msg.edit_text(frame, parse_mode="Markdown")
            await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass

async def animate_wave(msg):
    frames = ["🌊`░░░░░░░░░░`","🌊`██░░░░░░░░`","🌊`████░░░░░░`","🌊`██████░░░░`","🌊`████████░░`","🌊`██████████`"]
    try:
        for frame in frames:
            await msg.edit_text(f"{frame} Моя Волна...", parse_mode="Markdown")
            await asyncio.sleep(0.8)
    except Exception:
        pass


# ── send_track_to_user ────────────────────────────────────────────────────────
async def send_track_to_user(target_obj, track_id: str):
    is_cb  = isinstance(target_obj, types.CallbackQuery)
    message = target_obj.message if is_cb else target_obj
    if is_cb: await target_obj.answer("⏳ Проверяю базу...", show_alert=False)

    cached_file_id = get_cached_file_id(track_id)
    track = await music_engine.get_track_details(track_id)
    if not track or not track.get("stream_url"):
        await message.answer("❌ Не удалось получить поток трека (удалён или залочен).")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Избранное", callback_data=f"fav_sc:{track_id}"),
         InlineKeyboardButton(text="➕ В очередь", callback_data=f"queue_add:{track_id}")],
        [InlineKeyboardButton(text="🧠 Похожее",  callback_data=f"rec_sc:{track_id}"),
         InlineKeyboardButton(text="📝 Текст",    callback_data=f"lyrics:{track_id}")],
        [InlineKeyboardButton(text="📋 В плейлист", callback_data=f"pl_pick:{track_id}"),
         InlineKeyboardButton(text="👤 Артист",   callback_data=f"artist_page:{track_id}")],
    ])

    genre_raw  = track.get("genre","Неизвестен")
    clean_g    = re.sub(r'[^a-zA-Zа-яА-Я0-9\s]','',genre_raw).strip().replace(" ","_").lower()
    genre_tag  = f"#{clean_g}" if clean_g and clean_g != "неизвестен" else "#music"
    explicit   = get_explicit_tag(track["title"])
    _src       = track.get("source","SoundCloud")
    _src_icon  = {"SoundCloud":"🔊","YouTube Music":"▶️"}.get(_src,"🎵")
    caption    = (f"🎧 *{track['artist']} — {track['title']}*{explicit}\n\n"
                  f"🎼 *Жанр:* {genre_tag}\n{_src_icon} *Источник:* {_src}")

    if cached_file_id:
        try:
            await message.answer_audio(cached_file_id, performer=track["artist"],
                                       title=track["title"], caption=caption,
                                       parse_mode="Markdown", reply_markup=keyboard)
            return
        except Exception:
            pass

    status_msg = await message.answer("`[░░░░░░░░░░] 0% — Инициализация...`", parse_mode="Markdown")
    anim_task  = asyncio.create_task(animate_loading(status_msg))

    async with DOWNLOAD_SEMAPHORE:
        final_cover_url = await music_engine.fetch_itunes_cover(track["artist"], track["title"])
        if not final_cover_url:
            final_cover_url = track.get("artwork_url")
        if not final_cover_url or "default_avatar" in (final_cover_url or ""):
            safe_p = urllib.parse.quote(f"cool abstract aesthetic music album cover for {track['artist']} genre {track.get('genre','music')} without any text")
            final_cover_url = f"https://image.pollinations.ai/prompt/{safe_p}?width=1000&height=1000&nologo=true&seed={random.randint(1,999999)}"

        audio_bytes, cover_bytes = await asyncio.gather(
            music_engine.download_file(track["stream_url"]),
            music_engine.download_file(final_cover_url)
        )
        anim_task.cancel()

        if not audio_bytes:
            await status_msg.edit_text("❌ Ошибка при скачивании файла.")
            return

        mb_size   = round(len(audio_bytes)/(1024*1024), 2)
        geek_data = f"\n|| 💽 ID: {track_id} | 📦 {mb_size} MB | ⚙️ ~128 kbps ||"
        caption   = (f"🎧 *{track['artist']} — {track['title']}*\n\n"
                     f"🎼 *Жанр:* {genre_tag}\n{_src_icon} *Источник:* {_src}{geek_data}")

        audio_bytes = add_id3_tags(audio_bytes, track["title"], track["artist"], cover_bytes)
        audio_file  = BufferedInputFile(audio_bytes, filename=f"{track['title']}.mp3")
        thumb_file  = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None

        try:
            dur_str    = f"{track.get('duration','?')}"
            card_bytes = await asyncio.to_thread(
                make_track_card, track["artist"], track["title"], dur_str,
                track.get("source","SoundCloud"), cover_bytes, track.get("genre","")
            )
            await message.answer_photo(BufferedInputFile(card_bytes,"card.jpg"),
                                       caption=caption, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Карточка не создалась: {e}")

        try:
            sent = await message.answer_audio(audio=audio_file, performer=track["artist"],
                                              title=track["title"], reply_markup=keyboard,
                                              thumbnail=thumb_file)
            save_cached_file_id(track_id, sent.audio.file_id)
            log_track_history(target_obj.from_user.id, {
                "id": track_id, "title": track["title"],
                "artist": track["artist"], "source": track.get("source","SoundCloud")
            })
            await status_msg.delete()
        except Exception as e:
            logger.error(f"Ошибка отправки аудио: {e}")
            await status_msg.edit_text(f"❌ Телеграм отказался: {e}")


# ── Команды ───────────────────────────────────────────────────────────────────
async def cmd_music_find(message: types.Message):
    query = (message.text or "").replace("/find","").strip()
    if not query:
        await message.answer("🎧 Что ищем? Пример: `/find Rammstein`", parse_mode="Markdown")
        return
    await show_music_page(message, "search", query, 0)

async def cmd_charts(message: types.Message):
    await show_music_page(message, "chart", "", 0)

async def show_music_page(ctx, mode: str, query: str, page: int):
    limit  = 5; offset = page * limit
    if mode == "chart":
        tracks      = await music_engine.get_charts(limit=limit, offset=offset)
        src_names   = list(set(t.get("source","SC") for t in tracks)) if tracks else ["SoundCloud"]
        header_text = f"🔥 *Чарт* ({', '.join(src_names)}) — стр. {page+1}"
    else:
        tracks      = await music_engine.search_multi(query, limit=limit, offset=offset)
        src_names   = list(set(t.get("source","SC") for t in tracks)) if tracks else ["SoundCloud"]
        header_text = f"🎧 *Поиск:* «{query}» ({', '.join(src_names)}) — стр. {page+1}"

    if not tracks:
        if isinstance(ctx, types.CallbackQuery):
            await ctx.answer("❌ Больше треков нет.", show_alert=True)
        else:
            await ctx.answer("❌ Ничего не нашли 💀")
        return

    _icons   = {"SoundCloud":"🔊","YouTube Music":"▶️"}
    buttons  = []
    for t in tracks:
        icon = _icons.get(t.get("source",""),"🎵")
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {t['artist']} — {t['title']} [{t['duration']}]",
            callback_data=f"dl_sc:{t['id']}"
        )])

    safe_q = (query[:25] if query else "none")
    nav    = []
    if page > 0:
        nav += [InlineKeyboardButton(text="⏪", callback_data=f"mus_pg:{mode}:{safe_q}:{page-1}"),
                InlineKeyboardButton(text=str(page), callback_data=f"mus_pg:{mode}:{safe_q}:{page-1}")]
    nav.append(InlineKeyboardButton(text=f"· {page+1} ·", callback_data="ignore"))
    nav += [InlineKeyboardButton(text=str(page+2), callback_data=f"mus_pg:{mode}:{safe_q}:{page+1}"),
            InlineKeyboardButton(text="⏩", callback_data=f"mus_pg:{mode}:{safe_q}:{page+1}")]
    buttons.append(nav)

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(ctx, types.CallbackQuery):
        await ctx.message.edit_text(header_text, reply_markup=kb, parse_mode="Markdown")
    else:
        await ctx.answer(header_text, reply_markup=kb, parse_mode="Markdown")

async def callback_music_page(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 4: return
    _, mode, query, page = parts
    query = "" if query == "none" else query
    await show_music_page(callback, mode, query, int(page))

async def cmd_music_dashboard(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    WEB_APP_URL = os.getenv("WEB_APP_URL","https://bot-1784440802-6263-olegbff.bothost.tech/")
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Открыть плеер (WebApp)", web_app=WebAppInfo(url=WEB_APP_URL))],
        [InlineKeyboardButton(text="🌊 Моя Волна", callback_data="start_wave"),
         InlineKeyboardButton(text="🔥 Чарты",     callback_data="mus_pg:chart:none:0")],
        [InlineKeyboardButton(text="❤️ Избранное", callback_data="show_favs:0"),
         InlineKeyboardButton(text="🕒 История",   callback_data="show_hist:0")],
        [InlineKeyboardButton(text="📋 Плейлисты", callback_data="show_playlists"),
         InlineKeyboardButton(text="🎵 Очередь",   callback_data="queue_show")],
        [InlineKeyboardButton(text="🚀 Радар релизов", callback_data="radar_releases")],
    ])
    text = ("🎶 <b>MUSIC DASHBOARD</b>\n\n"
            "Добро пожаловать в хаб. Выбирай вайб или открой плеер.\n"
            "Инлайн-поиск: <code>@Betboomers_bot название</code>")
    if message.chat.type == "private":
        reply_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🎵 Открыть плеер", web_app=WebAppInfo(url=WEB_APP_URL))]],
            resize_keyboard=True, one_time_keyboard=False
        )
        await message.answer("🎵", reply_markup=reply_kb)
    await message.answer(text, reply_markup=inline_kb, parse_mode="HTML")

async def callback_back_to_dash(callback: types.CallbackQuery):
    try: await callback.message.delete()
    except: pass
    await cmd_music_dashboard(callback.message)

async def cmd_my_music(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    favs = get_music_favs(message.from_user.id)
    if not favs:
        await message.answer("💀 *Твой плейлист пуст.*\n\nНажимай ❤️ под треками.",parse_mode="Markdown")
        return
    buttons = []
    for f in favs[:6]:
        exp  = get_explicit_tag(f["title"])
        icon = {"SoundCloud":"🔊","YouTube Music":"▶️"}.get(f.get("source",""),"🎵")
        buttons.append([InlineKeyboardButton(text=f"{icon} {f['artist']} — {f['title']}{exp}", callback_data=f"dl_sc:{f['id']}")])
    if len(favs) > 6:
        buttons.append([InlineKeyboardButton(text="⏩",callback_data="show_favs:1")])
    buttons.append([InlineKeyboardButton(text="🎶 В дашборд",callback_data="back_to_dash")])
    await message.answer(f"❤️ *Твоя база — {len(favs)} трек(ов):*",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

async def callback_show_favs(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    favs = get_music_favs(callback.from_user.id)
    if not favs:
        await callback.answer("💀 Плейлист пуст.",show_alert=True); return
    limit  = 6; offset = page*limit; pf = favs[offset:offset+limit]
    buttons= []
    for f in pf:
        exp = get_explicit_tag(f["title"])
        buttons.append([InlineKeyboardButton(text=f"🎵 {f['artist']} — {f['title']}{exp}",callback_data=f"dl_sc:{f['id']}")])
    nav=[]
    if page>0:         nav.append(InlineKeyboardButton(text="⏪",callback_data=f"show_favs:{page-1}"))
    if offset+limit<len(favs): nav.append(InlineKeyboardButton(text="⏩",callback_data=f"show_favs:{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ Дашборд",callback_data="back_to_dash")])
    await callback.message.edit_text("❤️ *Твоя база:*",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="Markdown")

async def callback_show_hist(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    hist = get_user_history(callback.from_user.id)
    if not hist:
        await callback.answer("🕳 Ты ещё ничего не слушал.",show_alert=True); return
    limit  = 6; offset = page*limit; ph = hist[offset:offset+limit]
    buttons= []
    for f in ph:
        exp = get_explicit_tag(f["title"])
        buttons.append([InlineKeyboardButton(text=f"🕒 {f['artist']} — {f['title']}{exp}",callback_data=f"dl_sc:{f['id']}")])
    nav=[]
    if page>0:         nav.append(InlineKeyboardButton(text="⏪",callback_data=f"show_hist:{page-1}"))
    if offset+limit<len(hist): nav.append(InlineKeyboardButton(text="⏩",callback_data=f"show_hist:{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ Дашборд",callback_data="back_to_dash")])
    await callback.message.edit_text("🕒 *Недавно прослушанное:*",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="Markdown")

# ── Download / lyrics / recs / fav ───────────────────────────────────────────
async def callback_download_music(callback: types.CallbackQuery):
    await send_track_to_user(callback, callback.data.split(":")[1])

async def callback_lyrics(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    await callback.answer("⏳ Ищу текст...", show_alert=False)
    track = await music_engine.get_track_details(track_id)
    if not track: return
    lyrics = await music_engine.fetch_lyrics(track["artist"], track["title"])
    if not lyrics:
        await callback.answer("❌ Текст не найден.", show_alert=True); return
    safe = re.sub(r'(\[.*?\])',r'*\1*',lyrics).replace('\r\n\r\n','\n\n').replace('\n\n','\n\n❖ ❖ ❖\n\n')
    safe = safe[:3900] + ("\n\n[...]" if len(safe)>3900 else "")
    await callback.message.answer(f"📝 *{track['artist']} — {track['title']}*\n\n{safe}", parse_mode="Markdown")

async def callback_music_recs(callback: types.CallbackQuery):
    track_id  = callback.data.split(":")[1]
    await callback.answer("🧠 ИИ подбирает вайб...", show_alert=False)
    status    = await callback.message.answer("⏳ Нейросеть генерирует плейлист...")
    track     = await music_engine.get_track_details(track_id)
    if not track: return
    try:
        from google import genai
        key = os.getenv("GEMINI_API_KEY")
        if not key: raise RuntimeError("no key")
        client = genai.Client(api_key=key)
        prompt = (f"Посоветуй 5 треков похожих по стилю на: {track['artist']} — {track['title']}. "
                  "Выдай ТОЛЬКО валидный JSON-массив строк. Без лишнего текста.")
        response  = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        recs_list = json.loads(response.text.replace("```json","").replace("```","").strip())
        await status.edit_text("🔍 Пробиваю треки по базе...")
        buttons   = []
        for q in recs_list[:5]:
            res = await music_engine.search_multi(q, limit=1)
            if res:
                t = res[0]
                buttons.append([InlineKeyboardButton(text=f"🎵 {t['artist']} — {t['title']}", callback_data=f"dl_sc:{t['id']}")])
        if buttons:
            await status.edit_text("🧠 *Умные рекомендации:*",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
        else:
            await status.edit_text("❌ ИИ выдал треки, но в SoundCloud их нет.")
    except Exception as e:
        logger.error(f"Recs error: {e}")
        await status.edit_text("❌ Процессоры перегрелись.")

async def callback_music_fav(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    track    = await music_engine.get_track_details(track_id)
    if not track:
        await callback.answer("❌ Трек сгорел в серверах", show_alert=True); return
    ok = save_music_fav(callback.from_user.id,{"id":track_id,"title":track["title"],"artist":track["artist"]})
    await callback.answer("❤️ Сохранено!" if ok else "🤡 Уже добавлено.", show_alert=True)

# ── Волна ─────────────────────────────────────────────────────────────────────
async def callback_start_wave(callback: types.CallbackQuery):
    await callback.answer()
    status = await callback.message.answer("🌊 Запускаю Мою Волну...")
    anim   = asyncio.create_task(animate_wave(status))
    try:
        recs = await generate_wave_tracks(callback.from_user.id, limit=5)
        buttons = [[InlineKeyboardButton(text=f"🌊 {t['artist']} — {t['title']}", callback_data=f"dl_sc:{t['id']}")] for t in recs]
        buttons.append([InlineKeyboardButton(text="🔄 Следующая Волна", callback_data="start_wave")])
        anim.cancel()
        if buttons:
            await status.edit_text("🌊 *Моя Волна* — бесконечный поток под твой вкус:",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
        else:
            await status.edit_text("❌ Волна разбилась о скалы. Попробуй ещё.")
    except Exception as e:
        anim.cancel()
        logger.error(f"Wave error: {e}")
        await status.edit_text("❌ Ошибка генерации волны.")

async def callback_radar(callback: types.CallbackQuery):
    await callback.answer("🚀 Сканирую новинки...", show_alert=False)
    favs = get_music_favs(callback.from_user.id)
    if not favs:
        await callback.message.answer("❌ Добавь треки в избранное, чтобы радар знал кого искать!"); return
    artist  = random.choice(list(set(f["artist"] for f in favs)))
    tracks  = await music_engine.search_multi(f"{artist} 2026", limit=3)
    if not tracks:
        await callback.message.answer(f"🔕 У *{artist}* пока нет свежих дропов."); return
    buttons = [[InlineKeyboardButton(text=f"🚀 {t['artist']} — {t['title']}", callback_data=f"dl_sc:{t['id']}")] for t in tracks]
    await callback.message.answer(f"🚀 *Радар релизов* — свежее от *{artist}*:",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

# ── Wrapped ───────────────────────────────────────────────────────────────────
async def cmd_wrapped(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    hist = get_user_history(message.from_user.id)
    if len(hist) < 5:
        await message.answer("🤡 Какой тебе Wrapped, ты даже 5 треков не послушал."); return
    status = await message.answer("🎁 Рендерю инфографику...")
    try:
        user_name  = message.from_user.username or message.from_user.first_name or "анон"
        card_bytes = await asyncio.to_thread(_make_wrapped_card, hist, user_name)
        await message.answer_photo(BufferedInputFile(card_bytes,"wrapped.jpg"),
                                   caption=f"🎁 *Твои музыкальные итоги*\n🎵 {len(hist)} треков в истории",
                                   parse_mode="Markdown")
        await status.delete()
    except Exception as e:
        logger.error(f"Wrapped error: {e}")
        await status.edit_text(f"❌ Инфографика не сгенерировалась: {e}")

# ── Плейлисты ─────────────────────────────────────────────────────────────────
async def cmd_playlists(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    pls = get_playlists(message.from_user.id)
    if not pls:
        await message.answer("📋 *У тебя нет плейлистов.*\n\nНажми *«📋 В плейлист»* под треком.",parse_mode="Markdown"); return
    buttons = [[InlineKeyboardButton(text=f"📋 {name} ({len(tracks)} тр.)",callback_data=f"pl_open:{name[:30]}")] for name,tracks in pls.items()]
    buttons += [[InlineKeyboardButton(text="🗑 Удалить плейлист",callback_data="pl_delete_menu")],
                [InlineKeyboardButton(text="◀️ Дашборд",callback_data="back_to_dash")]]
    await message.answer("📋 *Твои плейлисты:*",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="Markdown")

async def callback_pl_pick(callback: types.CallbackQuery):
    track_id = callback.data.split(":",1)[1]
    pls = get_playlists(callback.from_user.id)
    buttons = [[InlineKeyboardButton(text=f"📋 {name}",callback_data=f"pl_add:{name[:30]}:{track_id}")] for name in pls]
    buttons += [[InlineKeyboardButton(text="➕ Новый плейлист",callback_data=f"pl_new:{track_id}")],
                [InlineKeyboardButton(text="✖️ Отмена",callback_data="pl_cancel")]]
    await callback.message.answer("📋 Выбери плейлист:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

async def callback_pl_new(callback: types.CallbackQuery, state: FSMContext):
    track_id = callback.data.split(":",1)[1]
    await state.set_state(MusicStates.waiting_playlist_name)
    await state.update_data(pending_track_id=track_id)
    await callback.message.answer("✏️ Напиши название нового плейлиста:")
    await callback.answer()

async def process_playlist_name(message: types.Message, state: FSMContext):
    data     = await state.get_data()
    track_id = data.get("pending_track_id")
    name     = (message.text or "").strip()[:40]
    await state.clear()
    if not name:
        await message.answer("❌ Пустое имя — не катит."); return
    if track_id:
        track = await music_engine.get_track_details(track_id)
        if track:
            save_playlist_track(message.from_user.id, name,
                                {"id":track_id,"title":track["title"],"artist":track["artist"],"source":track.get("source","SoundCloud")})
            await message.answer(f"✅ Плейлист *«{name}»* создан, трек добавлен!", parse_mode="Markdown")
        else:
            await message.answer("❌ Не удалось получить инфу о треке.")

async def callback_pl_add(callback: types.CallbackQuery):
    parts = callback.data.split(":",2); _, pl_name, track_id = parts
    track = await music_engine.get_track_details(track_id)
    if not track:
        await callback.answer("❌ Трек не найден",show_alert=True); return
    ok = save_playlist_track(callback.from_user.id, pl_name,
                             {"id":track_id,"title":track["title"],"artist":track["artist"],"source":track.get("source","SoundCloud")})
    await callback.answer(f"✅ Добавлено в «{pl_name}»" if ok else "🤡 Уже есть в плейлисте.", show_alert=True)

async def callback_pl_open(callback: types.CallbackQuery):
    pl_name = callback.data.split(":",1)[1]
    pls = get_playlists(callback.from_user.id); tracks = pls.get(pl_name,[])
    if not tracks:
        await callback.answer("💀 Плейлист пуст",show_alert=True); return
    buttons = []
    for t in tracks[:10]:
        icon = {"SoundCloud":"🔊","YouTube Music":"▶️"}.get(t.get("source",""),"🎵")
        buttons.append([InlineKeyboardButton(text=f"{icon} {t['artist']} — {t['title']}",callback_data=f"dl_sc:{t['id']}")])
    buttons += [[InlineKeyboardButton(text="🔀 Шаффл",callback_data=f"pl_shuffle:{pl_name[:30]}"),
                 InlineKeyboardButton(text="▶️ Всё в очередь",callback_data=f"pl_to_queue:{pl_name[:30]}")],
                [InlineKeyboardButton(text="◀️ Плейлисты",callback_data="show_playlists")]]
    await callback.message.edit_text(f"📋 *{pl_name}* — {len(tracks)} тр.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="Markdown")

async def callback_pl_shuffle(callback: types.CallbackQuery):
    pl_name = callback.data.split(":",1)[1]
    tracks  = get_playlists(callback.from_user.id).get(pl_name,[])
    if not tracks:
        await callback.answer("💀 Пусто",show_alert=True); return
    t = random.choice(tracks)
    await callback.answer("🔀 Кидаю случайный трек...")
    await send_track_to_user(callback, str(t["id"]))

async def callback_pl_to_queue(callback: types.CallbackQuery):
    pl_name = callback.data.split(":",1)[1]
    tracks  = get_playlists(callback.from_user.id).get(pl_name,[])
    if not tracks:
        await callback.answer("💀 Пусто",show_alert=True); return
    for t in tracks: queue_push(callback.from_user.id, t)
    await callback.answer(f"➕ {len(tracks)} треков в очереди", show_alert=True)

async def callback_pl_cancel(callback: types.CallbackQuery):
    try: await callback.message.delete()
    except: pass
    await callback.answer()

async def callback_pl_delete_menu(callback: types.CallbackQuery):
    pls = get_playlists(callback.from_user.id)
    if not pls:
        await callback.answer("Нет плейлистов",show_alert=True); return
    buttons = [[InlineKeyboardButton(text=f"🗑 {name}",callback_data=f"pl_delete:{name[:30]}")] for name in pls]
    buttons.append([InlineKeyboardButton(text="✖️ Отмена",callback_data="pl_cancel")])
    await callback.message.answer("Какой удалить?",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

async def callback_pl_delete(callback: types.CallbackQuery):
    pl_name = callback.data.split(":",1)[1]
    delete_playlist(callback.from_user.id, pl_name)
    await callback.answer(f"🗑 «{pl_name}» удалён",show_alert=True)
    try: await callback.message.delete()
    except: pass

async def callback_show_playlists(callback: types.CallbackQuery):
    pls = get_playlists(callback.from_user.id)
    if not pls:
        await callback.answer("📋 Нет плейлистов",show_alert=True); return
    buttons = [[InlineKeyboardButton(text=f"📋 {name} ({len(tracks)} тр.)",callback_data=f"pl_open:{name[:30]}")] for name,tracks in pls.items()]
    buttons.append([InlineKeyboardButton(text="◀️ Дашборд",callback_data="back_to_dash")])
    await callback.message.edit_text("📋 *Твои плейлисты:*",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="Markdown")

# ── Очередь ───────────────────────────────────────────────────────────────────
async def cmd_queue(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    q = queue_list(message.from_user.id)
    if not q:
        await message.answer("🎵 Очередь пуста. Жми ➕ под треками."); return
    buttons = []
    for i,t in enumerate(q[:10]):
        icon = {"SoundCloud":"🔊","YouTube Music":"▶️"}.get(t.get("source",""),"🎵")
        buttons.append([InlineKeyboardButton(text=f"{i+1}. {icon} {t['artist']} — {t['title']}",callback_data=f"dl_sc:{t['id']}")])
    buttons.append([InlineKeyboardButton(text="▶️ Следующий",callback_data="queue_next"),
                    InlineKeyboardButton(text="🗑 Очистить",callback_data="queue_clear")])
    await message.answer(f"🎵 *Очередь — {len(q)} треков:*",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="Markdown")

async def callback_queue_add(callback: types.CallbackQuery):
    track_id = callback.data.split(":",1)[1]
    track    = await music_engine.get_track_details(track_id)
    if not track:
        await callback.answer("❌ Трек не найден",show_alert=True); return
    queue_push(callback.from_user.id,{"id":track_id,"title":track["title"],"artist":track["artist"],"source":track.get("source","SoundCloud")})
    q = queue_list(callback.from_user.id)
    await callback.answer(f"➕ В очередь! Позиция: {len(q)}")

async def callback_queue_next(callback: types.CallbackQuery):
    track = queue_pop(callback.from_user.id)
    if not track:
        await callback.answer("🎵 Очередь пуста!",show_alert=True); return
    await callback.answer(f"▶️ Играю: {track['artist']} — {track['title']}")
    await send_track_to_user(callback, str(track["id"]))

async def callback_queue_clear(callback: types.CallbackQuery):
    queue_clear(callback.from_user.id)
    await callback.answer("🗑 Очередь очищена",show_alert=True)
    try: await callback.message.delete()
    except: pass

# ── Артист ────────────────────────────────────────────────────────────────────
async def callback_artist_page(callback: types.CallbackQuery):
    track_id = callback.data.split(":",1)[1]
    await callback.answer("👤 Загружаю профиль...")
    track    = await music_engine.get_track_details(track_id)
    if not track:
        await callback.answer("❌ Трек не найден",show_alert=True); return
    artist  = track["artist"]
    status  = await callback.message.answer(f"🔍 Ищу треки *{artist}*...",parse_mode="Markdown")
    top     = await music_engine.search_multi(artist, limit=5)
    art_tr  = [t for t in top if artist.lower() in t["artist"].lower()][:5] or top[:5]
    subs    = get_artist_subs(callback.from_user.id)
    is_sub  = artist in subs
    sub_btn = InlineKeyboardButton(
        text="🔕 Отписаться" if is_sub else "🔔 Следить",
        callback_data=f"artist_unsub:{artist[:40]}" if is_sub else f"artist_sub:{artist[:40]}"
    )
    buttons = []
    for t in art_tr:
        icon = {"SoundCloud":"🔊","YouTube Music":"▶️"}.get(t.get("source",""),"🎵")
        buttons.append([InlineKeyboardButton(text=f"{icon} {t['title']} [{t['duration']}]",callback_data=f"dl_sc:{t['id']}")])
    buttons += [[sub_btn],[InlineKeyboardButton(text="◀️ Назад",callback_data="back_to_dash")]]
    await status.edit_text(f"👤 *{artist}*\n\n🎵 Топ треков:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="Markdown")

async def callback_artist_sub(callback: types.CallbackQuery):
    artist = callback.data.split(":",1)[1]
    ok = add_artist_sub(callback.from_user.id, artist)
    await callback.answer(f"🔔 Подписался на {artist}!" if ok else "Уже подписан", show_alert=True)

async def callback_artist_unsub(callback: types.CallbackQuery):
    artist = callback.data.split(":",1)[1]
    remove_artist_sub(callback.from_user.id, artist)
    await callback.answer(f"🔕 Отписался от {artist}", show_alert=True)

# ── DJ Mode ───────────────────────────────────────────────────────────────────
async def cmd_dj(message: types.Message):
    auto_delete_later(message.bot, message.chat.id, message.message_id, 1)
    chat_id = message.chat.id
    if message.chat.type == "private":
        await message.answer("🎧 DJ Mode работает только в группах!"); return
    query = (message.text or "").replace("/dj","").strip()
    if not query:
        await message.answer("🎧 Укажи что ищем: `/dj название трека`",parse_mode="Markdown"); return
    status = await message.answer(f"🎧 DJ ищет варианты: *{query}*...",parse_mode="Markdown")
    tracks = await music_engine.search_multi(query, limit=4)
    if not tracks:
        await status.edit_text("❌ Ничего не нашли."); return

    DJ_SESSIONS[chat_id] = {
        "tracks": tracks,
        "votes":  {str(t["id"]): set() for t in tracks},
        "msg_id": None,
        "initiator": message.from_user.id
    }
    buttons = []
    for t in tracks:
        icon = {"SoundCloud":"🔊","YouTube Music":"▶️"}.get(t.get("source",""),"🎵")
        buttons.append([InlineKeyboardButton(text=f"{icon} {t['artist']} — {t['title']} [{t['duration']}]  👍 0",callback_data=f"dj_vote:{t['id']}")])
    buttons.append([InlineKeyboardButton(text="🏁 Завершить голосование",callback_data="dj_finish")])
    sent = await status.edit_text(
        f"🎧 *DJ MODE*\n{message.from_user.first_name} запустил голосование!\nГолосуй за следующий трек:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="Markdown"
    )
    DJ_SESSIONS[chat_id]["msg_id"] = sent.message_id

async def callback_dj_vote(callback: types.CallbackQuery):
    chat_id  = callback.message.chat.id
    user_id  = callback.from_user.id
    track_id = callback.data.split(":",1)[1]
    session  = DJ_SESSIONS.get(chat_id)
    if not session:
        await callback.answer("❌ Сессия закончилась",show_alert=True); return
    for voters in session["votes"].values(): voters.discard(user_id)
    session["votes"][track_id].add(user_id)
    buttons = []
    for t in session["tracks"]:
        tid   = str(t["id"]); cnt = len(session["votes"].get(tid,set()))
        icon  = {"SoundCloud":"🔊","YouTube Music":"▶️"}.get(t.get("source",""),"🎵")
        mark  = " ✅" if tid==track_id and user_id in session["votes"].get(tid,set()) else ""
        buttons.append([InlineKeyboardButton(text=f"{icon} {t['artist']} — {t['title']}  👍 {cnt}{mark}",callback_data=f"dj_vote:{tid}")])
    buttons.append([InlineKeyboardButton(text="🏁 Завершить",callback_data="dj_finish")])
    try: await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except: pass
    await callback.answer("✅ Голос засчитан!")

async def callback_dj_finish(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    session = DJ_SESSIONS.get(chat_id)
    if not session:
        await callback.answer("❌ Сессия не найдена",show_alert=True); return
    winner_id    = max(session["votes"], key=lambda tid: len(session["votes"][tid]))
    winner_count = len(session["votes"][winner_id])
    winner_track = next((t for t in session["tracks"] if str(t["id"])==winner_id), None)
    DJ_SESSIONS.pop(chat_id, None)
    if not winner_track or winner_count == 0:
        await callback.message.edit_text("🎧 Никто не проголосовал. DJ уходит обиженным."); return
    await callback.message.edit_text(
        f"🏆 *Победитель:*\n🎵 {winner_track['artist']} — {winner_track['title']}\n👍 {winner_count} голос(ов)\n\n⏳ Загружаю...",
        parse_mode="Markdown"
    )
    await send_track_to_user(callback, winner_id)

# ── Inline музыка ─────────────────────────────────────────────────────────────
async def inline_music_search(inline_query: types.InlineQuery):
    query = (inline_query.query or "").strip()
    if not query: return

    if query.startswith("share_"):
        track_id = query.split("_",1)[1]
        track    = await music_engine.get_track_details(track_id)
        if not track: return
        bot_user    = await inline_query.bot.me()
        web_app_url = f"https://t.me/{bot_user.username}/app?startapp=play_track_{track_id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Слушать в плеере",url=web_app_url)]])
        res = InlineQueryResultArticle(
            id=f"share_{track_id}", title="Поделиться треком",
            description=f"{track['artist']} — {track['title']}",
            thumbnail_url=track.get("artwork_url","https://i.imgur.com/8mX1wGg.png"),
            input_message_content=InputTextMessageContent(
                message_text=f"🔥 Зацени трек!\n\n🎧 *{track['artist']} — {track['title']}*",
                parse_mode="Markdown"
            ), reply_markup=kb
        )
        await inline_query.answer([res], cache_time=0)
        return

    tracks   = await music_engine.search_multi(query, limit=50)
    results  = []
    bot_user = await inline_query.bot.me()
    fallback = "https://i.imgur.com/8mX1wGg.png"
    _icons   = {"SoundCloud":"🔊","YouTube Music":"▶️"}

    for t in tracks:
        track_id       = str(t["id"])
        cached_file_id = get_cached_file_id(track_id)
        src_icon       = _icons.get(t.get("source",""),"🎵")
        thumb          = t.get("artwork_url") or fallback

        if cached_file_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❤️ В избранное",callback_data=f"fav_sc:{track_id}"),
                InlineKeyboardButton(text="🧠 Похожее",    callback_data=f"rec_sc:{track_id}")
            ]])
            results.append(InlineQueryResultCachedAudio(
                id=f"cached_{track_id}", audio_file_id=cached_file_id,
                caption=f"{src_icon} *{t['artist']} — {t['title']}* [{t['duration']}]",
                parse_mode="Markdown", reply_markup=kb
            ))
        else:
            deep_link = f"https://t.me/{bot_user.username}?start=track_{track_id}"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Слушать / скачать",url=deep_link)]])
            results.append(InlineQueryResultArticle(
                id=f"art_{track_id}", title=f"{t['artist']} — {t['title']}",
                description=f"⏱ {t['duration']}  {src_icon} {t.get('source','SC')}",
                thumbnail_url=thumb,
                input_message_content=InputTextMessageContent(
                    message_text=f"🎧 *{t['artist']} — {t['title']}*\n⏱ {t['duration']}  {src_icon} {t.get('source','SoundCloud')}",
                    parse_mode="Markdown"
                ), reply_markup=kb
            ))

    await inline_query.answer(results, cache_time=60, is_personal=True)
