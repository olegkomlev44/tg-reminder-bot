"""
music_links.py — ссылки Spotify / Яндекс.Музыка / VK Музыка → поиск трека в боте.

Кинул ссылку на трек — бот определяет «исполнитель — название» и открывает обычный музыкальный
поиск (со скачиванием). Альбом/плейлист — список треков кнопками: тап = бот сам найдёт и пришлёт трек.

Как определяем:
  Spotify — embed-страница (там JSON с треками) → og-теги → oEmbed
  Яндекс.Музыка — публичный api.music.yandex.net (без токена) → HTML-страница
  VK — страницы закрыты логином, поэтому: текст рядом со ссылкой → (если задан VK_TOKEN) audio.getById
  Если ничего не вышло — ИИ (Gemini + Google Search) пробует опознать по ссылке.
Подключение: register_music_link_handlers(dp) в main.py.
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import aiohttp
from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

MUSIC_LINK_RE = re.compile(
    r"https?://(?:"
    r"open\.spotify\.com/[^\s]+|spotify\.link/[^\s]+|spoti\.fi/[^\s]+|"
    r"music\.yandex\.(?:ru|com|by|kz|uz)/[^\s]+|"
    r"(?:m\.)?vk\.(?:com|ru)/(?:audio|music|audios)[^\s]*"
    r")", re.IGNORECASE)

SERVICE_NAMES = {"spotify": "Spotify", "yandex": "Яндекс.Музыка", "vk": "VK Музыка"}


@dataclass
class Resolved:
    service: str
    kind: str                       # track | album | playlist | artist
    title: str = ""                 # трек/альбом/плейлист/артист
    artist: str = ""
    tracks: list[tuple[str, str]] = field(default_factory=list)      # (artist, title)
    via_ai: bool = False

    @property
    def query(self) -> str:
        if self.kind == "artist":
            return self.title
        return f"{self.artist} {self.title}".strip()


def find_music_url(text: str) -> tuple[str | None, str | None]:
    m = MUSIC_LINK_RE.search(text or "")
    if not m:
        return None, None
    url = m.group(0).rstrip(").,;!?»")
    host = url.lower().split("/")[2] if "//" in url else ""
    service = "spotify" if ("spotify" in host or "spoti.fi" in host) else "yandex" if "yandex" in host else "vk"
    return service, url


def clean_query(q: str, limit: int = 60) -> str:
    """Убирает символы, ломающие Markdown-заголовок и callback_data музыкального поиска (: * _ [ ` ])."""
    q = re.sub(r"\((?:feat|ft)[^)]*\)|\[(?:feat|ft)[^\]]*\]", " ", q or "", flags=re.I)
    q = re.sub(r"[:*_\[\]`]", " ", q)
    return re.sub(r"\s+", " ", q).strip()[:limit]


async def _fetch_text(session: aiohttp.ClientSession, url: str, ua: str = BROWSER_UA,
                      lang: str = "en") -> tuple[str, str]:
    """Возвращает (текст, финальный url)."""
    try:
        async with session.get(url, headers={"User-Agent": ua, "Accept-Language": f"{lang},en;q=0.8"},
                               timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True) as r:
            if r.status == 200:
                return await r.text(errors="ignore"), str(r.url)
    except Exception as e:
        logger.info("music_links fetch %s: %s", url[:60], e)
    return "", url


def _og(html_text: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)["\']', html_text, re.I) \
        or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']', html_text, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def _walk(obj):
    """Обход вложенного JSON: все словари."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


# ── Spotify ───────────────────────────────────────────────────────────────────
_SP_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?(track|album|playlist|artist)/([A-Za-z0-9]+)")


def parse_spotify_embed(page: str, kind: str) -> Resolved | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None
    entity = None
    for d in _walk(data):
        if d.get("type") in ("track", "album", "playlist", "artist") and (d.get("name") or d.get("title")) \
                and ("trackList" in d or "artists" in d or d.get("type") == "artist" or "subtitle" in d):
            entity = d
            break
    if not entity:
        return None
    title = (entity.get("title") or entity.get("name") or "").strip()
    artists = ", ".join(a.get("name", "") for a in entity.get("artists", []) if isinstance(a, dict)).strip() \
        or (entity.get("subtitle") or "").strip()
    tracks = []
    for t in entity.get("trackList") or []:
        if isinstance(t, dict) and t.get("title"):
            tracks.append(((t.get("subtitle") or artists or "").replace("\xa0", " "), t["title"]))
    if kind == "artist":
        return Resolved("spotify", "artist", title=title)
    if not title:
        return None
    if kind == "track":
        return Resolved("spotify", "track", title=title, artist=artists.split(",")[0].strip())
    return Resolved("spotify", kind, title=title, artist=artists, tracks=tracks[:40])


def parse_spotify_og(page: str, kind: str) -> Resolved | None:
    title = _og(page, "og:title")
    desc = _og(page, "og:description")
    if not title:
        return None
    if kind == "track":
        artist = ""
        if "·" in desc:
            artist = desc.split("·")[0].strip()
        elif " by " in desc:
            artist = desc.split(" by ")[-1].split(".")[0].strip()
        tm = re.search(r"<title>[^<]*? by ([^|<]+?)\s*\|", page)
        artist = artist or (tm.group(1).strip() if tm else "")
        return Resolved("spotify", "track", title=title, artist=artist) if artist else \
            Resolved("spotify", "track", title=title)
    artist = desc.split("·")[0].strip() if "·" in desc else ""
    return Resolved("spotify", kind, title=title, artist=artist if kind == "album" else "")


async def resolve_spotify(url: str) -> Resolved | None:
    async with aiohttp.ClientSession() as s:
        _, final = await _fetch_text(s, url) if "spotify.link" in url or "spoti.fi" in url else ("", url)
        m = _SP_RE.search(final)
        if not m:
            return None
        kind, sid = m.group(1), m.group(2)
        page, _ = await _fetch_text(s, f"https://open.spotify.com/embed/{kind}/{sid}")
        r = parse_spotify_embed(page, kind) if page else None
        if r and (r.kind != "track" or r.artist):
            return r
        page2, _ = await _fetch_text(s, f"https://open.spotify.com/{kind}/{sid}", ua=CRAWLER_UA)
        r2 = parse_spotify_og(page2, kind) if page2 else None
        if r2 and (r2.kind != "track" or r2.artist):
            return r2
        return r or r2


# ── Яндекс.Музыка ─────────────────────────────────────────────────────────────
YM_API = "https://api.music.yandex.net"
YM_HEADERS = {"X-Yandex-Music-Client": "YandexMusicAndroid/24023231", "User-Agent": "Yandex-Music-API",
              "Accept": "application/json"}


def _ym_artists(obj: dict) -> str:
    return ", ".join(a.get("name", "") for a in obj.get("artists", []) if isinstance(a, dict)).strip()


def parse_yandex_url(url: str) -> tuple[str, dict] | None:
    if m := re.search(r"/album/(\d+)/track/(\d+)", url):
        return "track", {"id": m.group(2)}
    if m := re.search(r"/track/(\d+)", url):
        return "track", {"id": m.group(1)}
    if m := re.search(r"/users/([^/]+)/playlists/(\d+)", url):
        return "playlist", {"user": m.group(1), "kind": m.group(2)}
    if m := re.search(r"/album/(\d+)", url):
        return "album", {"id": m.group(1)}
    if m := re.search(r"/artist/(\d+)", url):
        return "artist", {"id": m.group(1)}
    return None


async def _ym_get(session: aiohttp.ClientSession, path: str, **params):
    try:
        async with session.get(YM_API + path, params=params or None, headers=YM_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                js = await r.json(content_type=None)
                return js.get("result")
            logger.info("yandex api %s → HTTP %s", path, r.status)
    except Exception as e:
        logger.info("yandex api %s: %s", path, e)
    return None


def parse_yandex_title(page: str, kind: str) -> Resolved | None:
    """Запасной вариант: <title> вида «Название — Исполнитель — слушать онлайн на Яндекс Музыке»."""
    t = _og(page, "og:title") or (re.search(r"<title>([^<]+)</title>", page) or [None, ""])[1]
    t = re.sub(r"\s*[—\-–]\s*(?:слушать|listen).*$", "", html.unescape(t), flags=re.I).strip()
    if not t or "captcha" in t.lower() or "вы не робот" in t.lower():
        return None
    parts = [p.strip() for p in re.split(r"\s+[—–]\s+", t) if p.strip()]
    if kind == "track" and len(parts) >= 2:
        return Resolved("yandex", "track", title=parts[0], artist=parts[1])
    if kind == "album" and parts:
        return Resolved("yandex", "album", title=parts[0], artist=parts[1] if len(parts) > 1 else "")
    if kind == "artist" and parts:
        return Resolved("yandex", "artist", title=parts[0])
    return None


async def resolve_yandex(url: str) -> Resolved | None:
    parsed = parse_yandex_url(url)
    if not parsed:
        return None
    kind, ids = parsed
    async with aiohttp.ClientSession() as s:
        if kind == "track":
            res = await _ym_get(s, f"/tracks/{ids['id']}")
            if res:
                t = res[0]
                return Resolved("yandex", "track", title=t.get("title", ""), artist=_ym_artists(t).split(",")[0].strip())
        elif kind == "album":
            res = await _ym_get(s, f"/albums/{ids['id']}/with-tracks")
            if res:
                tracks = [(_ym_artists(t) or _ym_artists(res), t.get("title", ""))
                          for vol in res.get("volumes", []) for t in vol if t.get("title")]
                return Resolved("yandex", "album", title=res.get("title", ""), artist=_ym_artists(res), tracks=tracks[:40])
        elif kind == "playlist":
            res = await _ym_get(s, f"/users/{ids['user']}/playlists/{ids['kind']}", **{"rich-tracks": "true"})
            if res:
                tracks = []
                for it in res.get("tracks", []):
                    t = it.get("track") or {}
                    if t.get("title"):
                        tracks.append((_ym_artists(t), t["title"]))
                return Resolved("yandex", "playlist", title=res.get("title", ""), tracks=tracks[:40])
        elif kind == "artist":
            res = await _ym_get(s, f"/artists/{ids['id']}/brief-info")
            name = ((res or {}).get("artist") or {}).get("name")
            if name:
                return Resolved("yandex", "artist", title=name)
        page, _ = await _fetch_text(s, url, lang="ru")
        return parse_yandex_title(page, kind) if page else None


# ── VK ────────────────────────────────────────────────────────────────────────
async def resolve_vk(url: str) -> Resolved | None:
    token = os.getenv("VK_TOKEN", "").strip()
    m = re.search(r"audio(-?\d+_\d+)", url)
    if token and m:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.vk.com/method/audio.getById",
                                 params={"audios": m.group(1), "access_token": token, "v": "5.131"},
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    js = await r.json(content_type=None)
            item = (js.get("response") or [None])[0]
            if item and item.get("title"):
                return Resolved("vk", "track", title=item["title"], artist=item.get("artist", ""))
        except Exception as e:
            logger.info("vk audio.getById: %s", e)
    return None


# ── ИИ-фолбэк ─────────────────────────────────────────────────────────────────
async def resolve_via_ai(url: str, service: str) -> Resolved | None:
    from ai_handlers import get_gemini
    client = get_gemini()
    if not client or service == "vk":        # у VK страницы закрыты логином — ИИ тоже не увидит
        return None
    try:
        from google.genai import types as gt
        resp = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=(f"Определи по ссылке музыкальный трек или альбом: {url}\n"
                      'Ответ — ТОЛЬКО JSON: {"kind":"track|album|artist|unknown","artist":"...","title":"..."}. '
                      "Если не уверен — kind=unknown. Не выдумывай."),
            config=gt.GenerateContentConfig(tools=[{"google_search": {}}]))
        m = re.search(r"\{.*\}", resp.text or "", re.S)
        js = json.loads(m.group(0)) if m else {}
        kind = js.get("kind")
        if kind in ("track", "album", "artist") and js.get("title"):
            return Resolved(service, kind, title=str(js["title"]), artist=str(js.get("artist", "")), via_ai=True)
    except Exception as e:
        logger.info("music_links ai fallback: %s", e)
    return None


async def resolve_link(service: str, url: str) -> Resolved | None:
    try:
        if service == "spotify":
            r = await asyncio.wait_for(resolve_spotify(url), 30)
        elif service == "yandex":
            r = await asyncio.wait_for(resolve_yandex(url), 30)
        else:
            r = await asyncio.wait_for(resolve_vk(url), 20)
    except Exception as e:
        logger.info("resolve_link %s: %s", service, e)
        r = None
    if r:
        return r
    return await resolve_via_ai(url, service)


# ── Хэндлер ───────────────────────────────────────────────────────────────────
_LISTS: "OrderedDict[str, dict]" = OrderedDict()
PAGE = 8


def _list_text(r: Resolved) -> str:
    e = html.escape
    kind_ru = {"album": "Альбом", "playlist": "Плейлист"}.get(r.kind, "Список")
    head = f"💿 <b>{kind_ru}: {e(r.title or '?')}</b>" + (f" — {e(r.artist)}" if r.artist else "")
    return f"{head}\n<i>{SERVICE_NAMES[r.service]} · {len(r.tracks)} треков. Жми на трек — найду и пришлю.</i>"


def _list_kb(tok: str, r: Resolved, page: int) -> InlineKeyboardMarkup:
    rows = []
    for i in range(page * PAGE, min(len(r.tracks), (page + 1) * PAGE)):
        a, t = r.tracks[i]
        rows.append([InlineKeyboardButton(text=f"{i + 1}. {(a + ' — ' if a else '')}{t}"[:60],
                                          callback_data=f"mlk:t:{tok}:{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏪", callback_data=f"mlk:p:{tok}:{page - 1}"))
    if (page + 1) * PAGE < len(r.tracks):
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"mlk:p:{tok}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def process_music_link(message: types.Message, service: str, url: str, extra_text: str = "") -> bool:
    """Определяет трек по ссылке и показывает поиск. Возвращает True, если что-то показано."""
    from music_handlers import show_music_page
    e = html.escape
    status = await message.reply(f"🔗 {SERVICE_NAMES[service]}: определяю…")
    r = await resolve_link(service, url)

    if (not r or (r.kind == "track" and not r.query)) and 3 <= len(extra_text) <= 100:
        r = Resolved(service, "track", title=extra_text)        # пользователь подписал трек сам

    if not r or not r.query:
        hint = ("VK не отдаёт названия без входа в аккаунт. " if service == "vk" else "Не смог определить трек по ссылке. ")
        await status.edit_text(f"🤷 {hint}Напиши «исполнитель — название» — найду: <code>/find Rammstein Du Hast</code>"
                               + ("\n\n<i>Подсказка: можно приписать название к ссылке в одном сообщении.</i>" if service == "vk" else ""),
                               parse_mode="HTML")
        return False

    if r.kind in ("track", "artist"):
        q = clean_query(r.query)
        label = f"{r.artist} — {r.title}" if r.kind == "track" and r.artist else r.title
        await status.edit_text(f"🎧 {SERVICE_NAMES[service]}: <b>{e(label)}</b>" + (" <i>(определено ИИ)</i>" if r.via_ai else ""),
                               parse_mode="HTML")
        await show_music_page(message, "search", q, 0)
        return True

    if not r.tracks:                                            # альбом без списка — ищем по названию
        await status.edit_text(f"💿 {SERVICE_NAMES[service]}: <b>{e(r.title)}</b> — список треков недоступен, ищу по названию",
                               parse_mode="HTML")
        await show_music_page(message, "search", clean_query(r.query), 0)
        return True

    tok = secrets.token_hex(4)
    _LISTS[tok] = {"r": r, "ts": time.time()}
    while len(_LISTS) > 60:
        _LISTS.popitem(last=False)
    await status.edit_text(_list_text(r), parse_mode="HTML", reply_markup=_list_kb(tok, r, 0))
    return True


async def handle_music_link(message: types.Message):
    text = message.text or message.caption or ""
    service, url = find_music_url(text)
    if not url:
        return
    extra = re.sub(r"\s+", " ", text.replace(url, " ")).strip()
    await process_music_link(message, service, url, extra)


async def callback_music_list(callback: types.CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    _, action, tok, idx = parts[:4]
    item = _LISTS.get(tok)
    if not item:
        await callback.answer("Список устарел — кинь ссылку заново", show_alert=True)
        return
    r: Resolved = item["r"]
    if action == "p":
        try:
            await callback.message.edit_reply_markup(reply_markup=_list_kb(tok, r, int(idx)))
        except Exception:
            pass
        await callback.answer()
        return
    if action == "t":
        i = int(idx)
        if not (0 <= i < len(r.tracks)):
            await callback.answer()
            return
        artist, title = r.tracks[i]
        from music_engine import music_engine
        from music_handlers import send_track_to_user
        await callback.answer("🔎 Ищу трек…")
        found = await music_engine.search_multi(clean_query(f"{artist} {title}"), limit=3)
        if not found:
            await callback.message.answer(f"❌ Не нашёл «{html.escape(artist)} — {html.escape(title)}».", parse_mode="HTML")
            return
        await send_track_to_user(callback.message, found[0]["id"])


def register_music_link_handlers(dp):
    async def _has_music_link(message: types.Message) -> bool:
        return bool(find_music_url(message.text or "")[1])

    dp.message.register(handle_music_link, F.text, _has_music_link)
    dp.callback_query.register(callback_music_list, F.data.startswith("mlk:"))
    logger.info("🔗 music_links зарегистрирован (Spotify / Яндекс.Музыка / VK)")
