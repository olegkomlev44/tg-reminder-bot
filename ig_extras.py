"""
Дополнения к Instagram-модулю бота.

  1. /ig_search слово      — поиск по закладкам и по постам, что бот уже присылал по твоим подпискам
  2. «👥 Похожие»          — кто ещё нравится подписчикам бота + совет самого Instagram
  5. /ig_dump ник [N]      — zip с последними N постами аккаунта (всё, что доступно донорам)
  7. /ig_tag, /ig_loc      — подписка на #хэштег и на место; /ig_topics — список и отписка

Подключение (main.py):
    from ig_extras import register_ig_extras, ig_topics_task
    register_ig_extras(dp)                    # после register_ig_handlers(dp)
    asyncio.create_task(ig_topics_task(bot))  # фоновая проверка тегов/мест
"""
import asyncio
import html as _html
import logging
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from instagram_viewer import (
    IG_DB, _POOL, _access, _norm_u, _pk_for, get_posts, get_user_info,
    ig_get_subscriptions, set_background_priority,
)
from instagram_handlers import (
    _act_row, _download, _fetch_media_files, _get_admin_ids, _out_chat,
    _post_caption_html, _send_files_safe,
)

logger = logging.getLogger(__name__)

# ── настройки ────────────────────────────────────────────────────────────────
IG_DUMP_MAX_POSTS = int(os.getenv("IG_DUMP_MAX_POSTS", "60"))        # максимум постов в одном архиве
IG_DUMP_MAX_MB = int(os.getenv("IG_DUMP_MAX_MB", "400"))             # общий размер медиа в архиве
IG_DUMP_FILE_MB = int(os.getenv("IG_DUMP_FILE_MB", "60"))            # файл крупнее — пропускаем
IG_DUMP_COOLDOWN = int(os.getenv("IG_DUMP_COOLDOWN", "600"))         # сек между архивами одного пользователя
IG_DUMP_PART_MB = float(os.getenv("IG_DUMP_PART_MB", "45"))          # часть архива для Telegram (лимит 50 МБ)
IG_TOPICS_PER_USER = int(os.getenv("IG_TOPICS_PER_USER", "10"))
IG_TOPIC_EVERY = int(os.getenv("IG_TOPIC_EVERY", str(3 * 3600)))     # как часто проверять одну тему, сек
IG_TOPIC_TICK = int(os.getenv("IG_TOPIC_TICK", "600"))               # как часто просыпается задача, сек
IG_TOPIC_PER_TICK = int(os.getenv("IG_TOPIC_PER_TICK", "2"))         # сколько тем за раз (бережём доноров)
IG_TOPIC_MAX_POSTS = int(os.getenv("IG_TOPIC_MAX_POSTS", "3"))       # сколько постов на тему за проверку
IG_LOC_LAT = float(os.getenv("IG_LOC_LAT", "55.75"))                 # центр для поиска мест по названию
IG_LOC_LNG = float(os.getenv("IG_LOC_LNG", "37.62"))
IG_WEBAPP_URL = os.getenv("IG_WEBAPP_URL", "")                       # https://…/ig-app


def _dbx() -> sqlite3.Connection:
    return sqlite3.connect(IG_DB, timeout=10)


def init_extras_db() -> None:
    conn = _dbx()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS ig_topics (
            user_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
            title TEXT DEFAULT '', created REAL NOT NULL,
            PRIMARY KEY (user_id, kind, key)
        )""")
        # общее состояние темы: один опрос Instagram на всех подписчиков
        conn.execute("""CREATE TABLE IF NOT EXISTS ig_topic_state (
            kind TEXT NOT NULL, key TEXT NOT NULL,
            last_ts INTEGER DEFAULT 0, last_check REAL DEFAULT 0, fails INTEGER DEFAULT 0,
            PRIMARY KEY (kind, key)
        )""")
        conn.commit()
    finally:
        conn.close()


def _plain(text: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", text or "")).replace("\n", " ").strip()


# ══════════════════════════════════════════════════════════════════════════════
# 1. ПОИСК
# ══════════════════════════════════════════════════════════════════════════════

def ig_search(user_id: str, query: str, limit: int = 12) -> tuple[list[dict], int]:
    """
    Ищем по подписи и нику (без учёта регистра — в том числе кириллицы, поэтому фильтр в Python,
    а не LIKE). Источники: закладки пользователя + кэш постов ТОЛЬКО тех аккаунтов,
    на которые он подписан. Возвращает (первые limit, всего найдено).
    """
    tokens = [t for t in query.casefold().split() if t]
    if not tokens:
        return [], 0
    uid = str(user_id)
    conn = _dbx()
    try:
        bms = conn.execute("SELECT shortcode, username, caption, created FROM ig_bookmarks WHERE user_id=?",
                           (uid,)).fetchall()
        subs = sorted({s["ig_username"] for s in ig_get_subscriptions(uid)})
        cached = []
        if subs:
            q = ",".join("?" * len(subs))
            cached = conn.execute(
                f"SELECT shortcode, username, caption, created FROM ig_file_cache WHERE username IN ({q})",
                subs).fetchall()
    finally:
        conn.close()

    bm_codes = {r[0] for r in bms}
    seen: dict[str, dict] = {}
    for src, rows in (("bm", bms), ("cache", cached)):
        for sc, user, cap, created in rows:
            if sc in seen:
                continue
            text = _plain(cap)
            hay = f"{user} {text}".casefold()
            if all(t in hay for t in tokens):
                seen[sc] = {"shortcode": sc, "username": user, "text": text,
                            "bookmarked": sc in bm_codes, "created": created or 0}
    res = sorted(seen.values(), key=lambda x: (not x["bookmarked"], -x["created"]))
    return res[:limit], len(res)


async def cmd_ig_search(message: types.Message, command: CommandObject):
    q = (command.args or "").strip()
    if not q:
        await message.answer(
            "🔍 <b>Поиск по постам</b>\n\n<code>/ig_search рецепт</code> или <code>/ig_search #путешествия</code>\n"
            "Ищу в твоих закладках и в постах аккаунтов, на которые ты подписан (по подписи и нику).",
            parse_mode="HTML")
        return
    res, total = ig_search(str(message.from_user.id), q)
    if not res:
        await message.answer(f"🔍 По запросу «{_html.escape(q)}» ничего не нашёл.\n"
                             f"<i>Ищу только среди закладок и присланных тебе постов.</i>", parse_mode="HTML")
        return
    rows = []
    for r in res:
        label = ("🔖 " if r["bookmarked"] else "") + (f"@{r['username']} · " if r["username"] else "")
        label += (r["text"] or r["shortcode"])[:34]
        rows.append([InlineKeyboardButton(text=label[:56], callback_data=f"ig_bmo:{r['shortcode']}")])
    more = f"\nПоказаны первые {len(res)} из {total}." if total > len(res) else ""
    await message.answer(f"🔍 Найдено: <b>{total}</b>{more}\nТап — открыть пост.", parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ══════════════════════════════════════════════════════════════════════════════
# 2. ПОХОЖИЕ АККАУНТЫ
# ══════════════════════════════════════════════════════════════════════════════

_CHAIN_CACHE: dict[str, tuple[float, list]] = {}
_CHAIN_TTL = 6 * 3600


def cosub_suggestions(user_id: str, username: str, limit: int = 8) -> list[tuple[str, int]]:
    """Кого ещё читают те, кто подписан на username (по базе бота, без запросов к Instagram)."""
    username = _norm_u(username)
    conn = _dbx()
    try:
        rs = conn.execute(
            """SELECT s2.ig_username, COUNT(DISTINCT s2.user_id) AS n
               FROM ig_subscriptions s1 JOIN ig_subscriptions s2 ON s1.user_id = s2.user_id
               WHERE s1.ig_username = ? AND s2.ig_username != ? AND s1.user_id != ?
                 AND s2.ig_username NOT IN (SELECT ig_username FROM ig_subscriptions WHERE user_id = ?)
               GROUP BY s2.ig_username ORDER BY n DESC, s2.ig_username LIMIT ?""",
            (username, username, str(user_id), str(user_id), limit)).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1]) for r in rs]


async def instagram_related(username: str) -> list[dict]:
    """«Похожие профили» от самого Instagram (discover/chaining). Любой сбой → пустой список."""
    username = _norm_u(username)
    hit = _CHAIN_CACHE.get(username)
    if hit and time.time() - hit[0] < _CHAIN_TTL:
        return hit[1]
    info = await get_user_info(username)
    if not info:
        return []
    only = _access(username, info)
    pk = _pk_for(username, info)
    if only is False or not pk:
        return []

    def _fetch(cl):
        res = cl.private_request("discover/chaining/", params={"target_id": str(pk)}) or {}
        out = []
        for u in res.get("users", []) or []:
            name = (u.get("username") or "").lower()
            if name and name != username:
                out.append({"username": name, "full_name": u.get("full_name") or "",
                            "is_private": bool(u.get("is_private"))})
        return out

    try:
        res = await _POOL.run(_fetch, only=only)
    except Exception as e:
        logger.info(f"related @{username}: {type(e).__name__}: {e}")
        return []
    res = res or []
    _CHAIN_CACHE[username] = (time.time(), res)
    return res


async def callback_ig_similar(callback: types.CallbackQuery):
    username = _norm_u(callback.data.split(":", 1)[1])
    uid = str(callback.from_user.id)
    await callback.answer("🔎 Ищу похожие...")
    mine = {s["ig_username"] for s in ig_get_subscriptions(uid)}
    co = cosub_suggestions(uid, username)
    ig_rel = [r for r in await instagram_related(username) if r["username"] not in mine]
    co_names = {u for u, _ in co}
    ig_rel = [r for r in ig_rel if r["username"] not in co_names][:8]

    if not co and not ig_rel:
        await callback.message.answer(
            f"😶 Для @{username} похожих не нашёл: у подписчиков бота пока нет общих аккаунтов, "
            f"а Instagram не дал список.")
        return
    lines = [f"👥 <b>Похожие на @{username}</b>"]
    rows: list[list[InlineKeyboardButton]] = []

    def _add(items):
        for i in range(0, len(items), 2):
            rows.append([InlineKeyboardButton(text=t[:40], callback_data=f"ig_profile:{u}")
                         for u, t in items[i:i + 2]])

    if co:
        lines.append("\n<b>Их же читают подписчики бота</b> (число — сколько человек):")
        _add([(u, f"👤 @{u} · {n}") for u, n in co])
    if ig_rel:
        lines.append("\n<b>Советует Instagram</b>:")
        _add([(r["username"], f"{'🔒' if r['is_private'] else '👤'} @{r['username']}") for r in ig_rel])
    rows.append([InlineKeyboardButton(text="◀️ Профиль", callback_data=f"ig_profile:{username}")])
    await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ══════════════════════════════════════════════════════════════════════════════
# 5. /ig_dump — АРХИВ АККАУНТА
# ══════════════════════════════════════════════════════════════════════════════

_DUMP_SEM = asyncio.Semaphore(1)
_DUMP_LAST: dict[int, float] = {}


def _fmt_mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} МБ" if n >= 1024 * 1024 else f"{n / 1024:.0f} КБ"


def _split_file(path: str, part_bytes: int) -> list[str]:
    if os.path.getsize(path) <= part_bytes:
        return [path]
    parts, i = [], 1
    with open(path, "rb") as f:
        while True:
            chunk = f.read(part_bytes)
            if not chunk:
                break
            pp = f"{path}.part{i:03d}"
            with open(pp, "wb") as o:
                o.write(chunk)
            parts.append(pp)
            i += 1
    os.remove(path)
    return parts


async def _collect_posts(username: str, n: int) -> tuple[list[dict], str]:
    posts: list[dict] = []
    offset = 0
    while len(posts) < n:
        r = await get_posts(username, str(offset) if offset else "")
        if r.get("private"):
            return posts, "private"
        page = r.get("posts") or []
        posts += page
        if not r.get("has_more") or not page:
            break
        offset += 10
    return posts[:n], ""


async def run_dump(bot: Bot, chat_id: int, username: str, n: int) -> None:
    username = _norm_u(username)
    status = await bot.send_message(chat_id, f"📦 Готовлю архив @{username} (до {n} постов)...")

    async def _say(text: str):
        try:
            await status.edit_text(text)
        except Exception:
            pass

    posts, err = await _collect_posts(username, n)
    if err == "private" and not posts:
        await _say(f"🔒 @{username} приватный, а бот на него не подписан — архив недоступен.")
        return
    if not posts:
        await _say(f"😶 Не получил посты @{username}: аккаунт пустой или Instagram сейчас не отвечает.")
        return

    sem = asyncio.Semaphore(4)
    total_bytes = 0
    skipped = 0
    truncated = False
    max_total = IG_DUMP_MAX_MB * 1024 * 1024
    max_file = IG_DUMP_FILE_MB * 1024 * 1024

    async def _grab(m: dict):
        kind = m.get("type", "photo")
        async with sem:
            data = await _download(m.get("url") or m.get("thumb", ""), kind)
            if not data and kind == "video" and m.get("thumb"):
                data, kind = await _download(m["thumb"], "photo"), "photo"
        return kind, data

    with tempfile.TemporaryDirectory(prefix="igdump_") as tmp:
        zpath = os.path.join(tmp, f"{username}_{time.strftime('%Y%m%d')}.zip")
        caps: list[str] = []
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
            for i, p in enumerate(posts, 1):
                if total_bytes >= max_total:
                    truncated = True
                    break
                sc = p.get("shortcode") or p.get("id") or str(i)
                day = datetime.fromtimestamp(p.get("timestamp") or 0).strftime("%Y-%m-%d")
                media = (p.get("media") or [])[:20]
                results = await asyncio.gather(*[_grab(m) for m in media])
                for j, (kind, data) in enumerate(results, 1):
                    if not data or len(data) > max_file:
                        skipped += 1
                        continue
                    ext = "mp4" if kind == "video" else "jpg"
                    z.writestr(f"{day}_{sc}_{j}.{ext}", data)
                    total_bytes += len(data)
                caps.append(
                    f"[{day}] https://www.instagram.com/p/{sc}/  ❤️ {p.get('like_count', 0)}  "
                    f"💬 {p.get('comment_count', 0)}\n{(p.get('caption') or '').strip()}\n" + "-" * 40)
                if i % 3 == 0 or i == len(posts):
                    await _say(f"📦 @{username}: {i}/{len(posts)} постов · {_fmt_mb(total_bytes)}")
            z.writestr("captions.txt", "\n".join(caps))

        if total_bytes == 0:
            await _say("❌ Не удалось скачать ни одного файла (CDN не отвечает). Попробуй позже.")
            return
        parts = _split_file(zpath, int(IG_DUMP_PART_MB * 1024 * 1024))
        note = (f"📦 @{username} · постов: {len(caps)} · {_fmt_mb(total_bytes)}"
                + (f"\n⚠️ Пропущено файлов: {skipped} (слишком большие или не скачались)" if skipped else "")
                + ("\n⚠️ Остановился на лимите размера архива" if truncated else "")
                + (f"\nЧастей: {len(parts)} — склей: <code>cat имя.zip.part* &gt; имя.zip</code>" if len(parts) > 1 else ""))
        await _say("📤 Отправляю архив...")
        try:
            for k, pp in enumerate(parts):
                await bot.send_document(chat_id, FSInputFile(pp), caption=note if k == 0 else None,
                                        parse_mode="HTML")
        except Exception as e:
            logger.warning(f"dump send {username}: {e}")
            await _say("❌ Не смог отправить архив — слишком большой или Telegram не принял.")
            return
        try:
            await status.delete()
        except Exception:
            pass


async def _start_dump(bot: Bot, chat_id: int, uid: int, username: str, n: int):
    n = max(1, min(n, IG_DUMP_MAX_POSTS))
    left = IG_DUMP_COOLDOWN - (time.time() - _DUMP_LAST.get(uid, 0))
    if left > 0 and uid not in _get_admin_ids():
        await bot.send_message(chat_id, f"⏳ Следующий архив можно собрать через {int(left // 60) + 1} мин.")
        return
    if _DUMP_SEM.locked():
        await bot.send_message(chat_id, "⏳ Сейчас собираю другой архив — повтори через пару минут.")
        return
    async with _DUMP_SEM:
        _DUMP_LAST[uid] = time.time()
        try:
            await run_dump(bot, chat_id, username, n)
        except Exception as e:
            logger.error(f"dump @{username}: {e}", exc_info=True)
            await bot.send_message(chat_id, "❌ Архив не получился — подробности в логах.")


async def cmd_ig_dump(message: types.Message, command: CommandObject):
    parts = (command.args or "").replace("@", "").split()
    if not parts:
        await message.answer(
            f"📦 <b>Архив аккаунта</b>\n<code>/ig_dump ник</code> — последние 20 постов\n"
            f"<code>/ig_dump ник 40</code> — до {IG_DUMP_MAX_POSTS}\n"
            f"Фото и видео в zip + captions.txt с подписями. Приватные — только если бот на них подписан.",
            parse_mode="HTML")
        return
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
    await _start_dump(message.bot, message.chat.id, message.from_user.id, parts[0], n)


async def callback_ig_dump_menu(callback: types.CallbackQuery):
    username = _norm_u(callback.data.split(":", 1)[1])
    await callback.answer()
    sizes = [n for n in (10, 30, 60) if n <= IG_DUMP_MAX_POSTS] or [IG_DUMP_MAX_POSTS]
    await callback.message.answer(
        f"📦 Скачать архив @{username}. Сколько последних постов?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=str(n), callback_data=f"ig_dmpgo:{username}:{n}") for n in sizes]]))


async def callback_ig_dump_go(callback: types.CallbackQuery):
    _, username, n = callback.data.split(":", 2)
    await callback.answer("⏳")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _start_dump(callback.bot, callback.message.chat.id, callback.from_user.id, username, int(n))


# ══════════════════════════════════════════════════════════════════════════════
# 7. ПОДПИСКА НА ХЭШТЕГИ И МЕСТА
# ══════════════════════════════════════════════════════════════════════════════

def topic_list(user_id: str) -> list[dict]:
    conn = _dbx()
    try:
        rs = conn.execute("SELECT kind, key, title FROM ig_topics WHERE user_id=? ORDER BY created",
                          (str(user_id),)).fetchall()
    finally:
        conn.close()
    return [{"kind": r[0], "key": r[1], "title": r[2]} for r in rs]


def topic_add(user_id: str, kind: str, key: str, title: str) -> bool:
    conn = _dbx()
    try:
        cur = conn.execute("INSERT OR IGNORE INTO ig_topics (user_id, kind, key, title, created) VALUES (?,?,?,?,?)",
                           (str(user_id), kind, key, title, time.time()))
        conn.execute("INSERT OR IGNORE INTO ig_topic_state (kind, key, last_ts, last_check) VALUES (?,?,?,?)",
                     (kind, key, 0, 0))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def topic_del(user_id: str, kind: str, key: str) -> None:
    conn = _dbx()
    try:
        conn.execute("DELETE FROM ig_topics WHERE user_id=? AND kind=? AND key=?", (str(user_id), kind, key))
        # тема без подписчиков больше не опрашивается
        conn.execute("DELETE FROM ig_topic_state WHERE kind=? AND key=? AND NOT EXISTS "
                     "(SELECT 1 FROM ig_topics WHERE kind=? AND key=?)", (kind, key, kind, key))
        conn.commit()
    finally:
        conn.close()


def _topic_id(t: dict) -> str:
    import hashlib
    return hashlib.md5(f"{t['kind']}:{t['key']}".encode()).hexdigest()[:12]


def _topic_label(t: dict) -> str:
    return f"#{t['key']}" if t["kind"] == "tag" else f"📍 {t['title'] or t['key']}"


def _norm_tag(raw: str) -> str:
    return re.sub(r"[^\w]", "", (raw or "").strip().lstrip("#").lower(), flags=re.UNICODE)[:60]


async def cmd_ig_tag(message: types.Message, command: CommandObject):
    raw = (command.args or "").strip()
    tag = _norm_tag(raw.split()[0]) if raw else ""
    if not tag:
        await message.answer(
            "#️⃣ <b>Подписка на хэштег</b>\n<code>/ig_tag #рецепты</code>\n"
            "Бот раз в несколько часов проверяет тег и присылает самые залайканные новые посты. "
            "Список и отписка: /ig_topics", parse_mode="HTML")
        return
    uid = str(message.from_user.id)
    mine = topic_list(uid)
    if len(mine) >= IG_TOPICS_PER_USER and not any(t["kind"] == "tag" and t["key"] == tag for t in mine):
        await message.answer(f"⛔ Максимум тем на человека: {IG_TOPICS_PER_USER}. Убери лишние в /ig_topics.")
        return
    added = topic_add(uid, "tag", tag, f"#{tag}")
    await message.answer(
        (f"✅ Подписал на <b>#{_html.escape(tag)}</b>. " if added else f"ℹ️ Ты уже подписан на #{_html.escape(tag)}. ")
        + f"Проверяю раз в ~{IG_TOPIC_EVERY // 3600} ч; пришлю до {IG_TOPIC_MAX_POSTS} лучших новых постов.",
        parse_mode="HTML")


def _loc_pk_from_url(text: str) -> str:
    m = re.search(r"instagram\.com/explore/locations/(\d+)", text or "")
    return m.group(1) if m else ""


async def cmd_ig_loc(message: types.Message, command: CommandObject):
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "📍 <b>Подписка на место</b>\n<code>/ig_loc Парк Горького</code> — поиск по названию\n"
            "или ссылкой: <code>/ig_loc https://www.instagram.com/explore/locations/123456/…</code>",
            parse_mode="HTML")
        return
    uid = str(message.from_user.id)
    pk = _loc_pk_from_url(raw)
    if pk:
        title = f"место {pk}"
        if topic_add(uid, "loc", pk, title):
            await message.answer(f"✅ Подписал на 📍 место {pk}. Проверяю раз в ~{IG_TOPIC_EVERY // 3600} ч.")
        else:
            await message.answer("ℹ️ Ты уже подписан на это место.")
        return

    def _search(cl):
        out = []
        for loc in cl.fbsearch_places(raw, lat=IG_LOC_LAT, lng=IG_LOC_LNG) or []:
            out.append({"pk": str(getattr(loc, "pk", "") or getattr(loc, "external_id", "")),
                        "name": getattr(loc, "name", "") or "", "address": getattr(loc, "address", "") or ""})
        return [x for x in out if x["pk"] and x["name"]][:6]

    wait = await message.answer("🔎 Ищу место...")
    try:
        found = await _POOL.run(_search)
    except Exception as e:
        logger.info(f"loc search {raw}: {type(e).__name__}: {e}")
        found = None
    try:
        await wait.delete()
    except Exception:
        pass
    if not found:
        await message.answer("😶 Не нашёл такое место. Пришли ссылку вида instagram.com/explore/locations/ID/…")
        return
    rows = [[InlineKeyboardButton(text=f"📍 {x['name']} · {x['address']}"[:56],
                                  callback_data=f"ig_locadd:{x['pk']}")] for x in found]
    # название нужно на этапе подтверждения — кладём в кэш в памяти
    for x in found:
        _LOC_NAMES[x["pk"]] = x["name"]
    await message.answer("Выбери место:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


_LOC_NAMES: dict[str, str] = {}


async def callback_ig_locadd(callback: types.CallbackQuery):
    pk = callback.data.split(":", 1)[1]
    uid = str(callback.from_user.id)
    if len(topic_list(uid)) >= IG_TOPICS_PER_USER:
        await callback.answer(f"Максимум тем: {IG_TOPICS_PER_USER}", show_alert=True)
        return
    name = _LOC_NAMES.get(pk, f"место {pk}")
    added = topic_add(uid, "loc", pk, name)
    await callback.answer("✅ Подписал" if added else "Уже подписан")
    try:
        await callback.message.edit_text(f"✅ Подписан на 📍 {name}. Проверяю раз в ~{IG_TOPIC_EVERY // 3600} ч.")
    except Exception:
        pass


def _topics_view(uid: str):
    items = topic_list(uid)
    if not items:
        return ("📭 Тем нет. Добавь: <code>/ig_tag #тег</code> или <code>/ig_loc место</code>", None)
    rows = [[InlineKeyboardButton(text=f"❌ {_topic_label(t)}"[:48], callback_data=f"ig_topdel:{_topic_id(t)}")]
            for t in items]
    return (f"📌 <b>Твои темы</b> ({len(items)}/{IG_TOPICS_PER_USER}). Тап — отписаться.",
            InlineKeyboardMarkup(inline_keyboard=rows))


async def cmd_ig_topics(message: types.Message):
    text, kb = _topics_view(str(message.from_user.id))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def callback_ig_topdel(callback: types.CallbackQuery):
    tid = callback.data.split(":", 1)[1]
    for t in topic_list(str(callback.from_user.id)):
        if _topic_id(t) == tid:
            topic_del(str(callback.from_user.id), t["kind"], t["key"])
            break
    await callback.answer("Отписал")
    text, kb = _topics_view(str(callback.from_user.id))
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


# ── фоновая проверка тем ─────────────────────────────────────────────────────

def _due_topics(limit: int) -> list[dict]:
    now = time.time()
    conn = _dbx()
    try:
        rs = conn.execute(
            "SELECT kind, key, last_ts, last_check, fails FROM ig_topic_state ORDER BY last_check ASC").fetchall()
    finally:
        conn.close()
    out = []
    for kind, key, last_ts, last_check, fails in rs:
        wait = IG_TOPIC_EVERY * (1 + min(fails, 3))       # при сбоях — реже, до ×4
        if now - (last_check or 0) >= wait:
            out.append({"kind": kind, "key": key, "last_ts": int(last_ts or 0), "fails": fails})
        if len(out) >= limit:
            break
    return out


def _topic_state_set(kind: str, key: str, **kw) -> None:
    cols = ", ".join(f"{k}=?" for k in kw)
    conn = _dbx()
    try:
        conn.execute(f"UPDATE ig_topic_state SET {cols} WHERE kind=? AND key=?", (*kw.values(), kind, key))
        conn.commit()
    finally:
        conn.close()


async def _fetch_topic_posts(kind: str, key: str, amount: int = 12) -> list[dict] | None:
    from instagram_viewer import _media_to_post

    def _fetch(cl):
        if kind == "tag":
            try:
                medias = cl.hashtag_medias_recent(key, amount=amount)
            except Exception as e:
                if "feedback" in str(e).lower():
                    raise
                medias = cl.hashtag_medias_top(key, amount=amount)
        else:
            medias = cl.location_medias_recent(int(key), amount=amount)
        out = []
        for m in medias or []:
            p = _media_to_post(m)
            p["username"] = (getattr(getattr(m, "user", None), "username", "") or "").lower()
            out.append(p)
        return out

    try:
        return await _POOL.run(_fetch)
    except Exception as e:
        logger.warning(f"topic {kind}:{key}: {type(e).__name__}: {e}")
        return None


async def _check_topic(bot: Bot, t: dict) -> None:
    kind, key = t["kind"], t["key"]
    posts = await _fetch_topic_posts(kind, key)
    now = time.time()
    if posts is None:
        _topic_state_set(kind, key, last_check=now, fails=t["fails"] + 1)
        return
    newest = max([p.get("timestamp", 0) for p in posts] or [0])
    if t["last_ts"] == 0:                               # первый опрос — только запоминаем «точку отсчёта»
        _topic_state_set(kind, key, last_ts=newest or int(now), last_check=now, fails=0)
        return
    fresh = [p for p in posts if p.get("timestamp", 0) > t["last_ts"] and p.get("media")]
    _topic_state_set(kind, key, last_ts=max(newest, t["last_ts"]), last_check=now, fails=0)
    if not fresh:
        return
    best = sorted(fresh, key=lambda p: p.get("like_count", 0), reverse=True)[:IG_TOPIC_MAX_POSTS]
    conn = _dbx()
    try:
        users = [r[0] for r in conn.execute("SELECT user_id FROM ig_topics WHERE kind=? AND key=?", (kind, key))]
        title = (conn.execute("SELECT title FROM ig_topics WHERE kind=? AND key=? LIMIT 1", (kind, key)).fetchone()
                 or ["", ""])[0]
    finally:
        conn.close()
    label = f"#{key}" if kind == "tag" else f"📍 {title or key}"
    for p in sorted(best, key=lambda x: x.get("timestamp", 0)):
        media = (p.get("media") or [])[:10]
        files, _ = await _fetch_media_files(media, limit=10)
        if not files:
            continue
        sc, user = p.get("shortcode", ""), p.get("username", "")
        cap = _post_caption_html(user, p.get("caption") or "", p.get("like_count", 0), p.get("comment_count", 0),
                                 extra=f"\n🔎 {_html.escape(label)}")
        if sc:
            cap = (cap + f'\n🔗 <a href="https://www.instagram.com/p/{sc}/">Открыть в Instagram</a>')[:1020]
        for uid in users:
            try:
                ar = _act_row(sc, user, uid) if len(files) == 1 else []
                kb = InlineKeyboardMarkup(inline_keyboard=[ar]) if ar else None
                await _send_files_safe(_out_chat(bot, uid), files, cap, kb=kb)
            except Exception as e:
                logger.warning(f"topic notify {uid} {label}: {e}")
            await asyncio.sleep(0.4)


async def ig_topics_task(bot: Bot):
    """Раз в IG_TOPIC_TICK берёт самые «просроченные» темы (не больше IG_TOPIC_PER_TICK) и проверяет."""
    set_background_priority()
    await asyncio.sleep(240)
    while True:
        try:
            for t in _due_topics(IG_TOPIC_PER_TICK):
                await _check_topic(bot, t)
                await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"ig_topics_task: {e}", exc_info=True)
        await asyncio.sleep(IG_TOPIC_TICK)


# ══════════════════════════════════════════════════════════════════════════════
# 10. кнопка мини-аппы «Лента»
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_ig_app(message: types.Message):
    if not IG_WEBAPP_URL.startswith("https://"):
        await message.answer("🖼 Мини-апп «Лента» не настроена: задай IG_WEBAPP_URL=https://…/ig-app "
                             "(нужен публичный https-адрес проекта).")
        return
    await message.answer("🖼 Твоя лента, закладки и подписки — одним экраном:", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🖼 Открыть ленту", web_app=WebAppInfo(url=IG_WEBAPP_URL))]]))


# ── регистрация ──────────────────────────────────────────────────────────────

def register_ig_extras(dp: Dispatcher):
    init_extras_db()
    dp.message.register(cmd_ig_search, Command("ig_search"))
    dp.message.register(cmd_ig_dump,   Command("ig_dump"))
    dp.message.register(cmd_ig_tag,    Command("ig_tag"))
    dp.message.register(cmd_ig_loc,    Command("ig_loc"))
    dp.message.register(cmd_ig_topics, Command("ig_topics"))
    dp.message.register(cmd_ig_app,    Command(commands=["ig_app", "lenta"]))
    dp.callback_query.register(callback_ig_similar,   F.data.startswith("ig_sim:"))
    dp.callback_query.register(callback_ig_dump_menu, F.data.startswith("ig_dmp:"))
    dp.callback_query.register(callback_ig_dump_go,   F.data.startswith("ig_dmpgo:"))
    dp.callback_query.register(callback_ig_locadd,    F.data.startswith("ig_locadd:"))
    dp.callback_query.register(callback_ig_topdel,    F.data.startswith("ig_topdel:"))
