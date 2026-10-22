"""
media_downloader.py — скачивание медиа из YouTube, TikTok, Twitter/X, Reddit,
Pinterest, VK Видео/Клипы, Twitch-клипов и Threads.

Подключение в main.py:
    from media_downloader import register_media_handlers
    register_media_handlers(dp)

Пользователь просто кидает ссылку в чат — бот сам определяет платформу,
скачивает медиа через yt-dlp и отправляет файлом (видео или фото).
"""

import asyncio
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# ── ЛИМИТЫ ────────────────────────────────────────────────────────────────────
MAX_VIDEO_MB = 50          # Telegram ограничение — 50 МБ, оставляем запас
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = int(os.getenv("MEDIA_DOWNLOAD_TIMEOUT", "900"))   # секунд на скачивание

# Большие видео: качаем целиком в хорошем качестве и режем на части ≤ лимита Telegram (без пережатия).
MAX_TOTAL_MB = int(os.getenv("MEDIA_MAX_TOTAL_MB", "1000"))          # максимум исходного файла
MAX_TOTAL_BYTES = MAX_TOTAL_MB * 1024 * 1024
MAX_PARTS = int(os.getenv("MEDIA_MAX_PARTS", "25"))                 # максимум частей
PART_TARGET_BYTES = 42 * 1024 * 1024                                # целевой размер части
PART_HARD_BYTES = 49 * 1024 * 1024                                  # больше этого Telegram не примет
MAX_HEIGHT = int(os.getenv("MEDIA_MAX_HEIGHT", "720"))              # потолок качества YouTube

# Очередь и защита хостинга
QUEUE_SLOTS = int(os.getenv("MEDIA_MAX_CONCURRENT", "2"))           # сколько загрузок одновременно
PER_USER_LIMIT = int(os.getenv("MEDIA_PER_USER_LIMIT", "2"))        # сколько задач в очереди/работе на человека
QUEUE_MAX = int(os.getenv("MEDIA_QUEUE_MAX", "15"))                 # максимум ожидающих в очереди
MIN_FREE_MB = int(os.getenv("MEDIA_MIN_FREE_MB", "500"))            # минимум свободного места на диске
FRAGMENTS = int(os.getenv("MEDIA_FRAGMENTS", "4"))                  # параллельные фрагменты yt-dlp

# ── ПАТТЕРНЫ ССЫЛОК ───────────────────────────────────────────────────────────
URL_PATTERNS = {
    "youtube": re.compile(
        r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch|shorts|live)|youtu\.be/)\S+",
        re.IGNORECASE
    ),
    "tiktok": re.compile(
        r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+",
        re.IGNORECASE
    ),
    "twitter": re.compile(
        r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\S+/status/\d+\S*",
        re.IGNORECASE
    ),
    "reddit": re.compile(
        r"https?://(?:www\.|old\.|new\.)?reddit\.com/r/\S+|https?://redd\.it/\S+",
        re.IGNORECASE
    ),
    "pinterest": re.compile(
        r"https?://(?:[a-z]{2,3}\.|www\.)?pinterest\.[a-z.]{2,6}/pin/\S+|https?://pin\.it/\S+",
        re.IGNORECASE
    ),
    "vkvideo": re.compile(
        r"https?://(?:www\.|m\.)?(?:vk\.com|vk\.ru|vkvideo\.ru)/(?:video|clip)-?\d+_\d+\S*",
        re.IGNORECASE
    ),
    "twitch": re.compile(
        r"https?://(?:clips\.twitch\.tv/[\w-]+|(?:www\.|m\.)?twitch\.tv/\w+/clip/[\w-]+)\S*",
        re.IGNORECASE
    ),
    "threads": re.compile(
        r"https?://(?:www\.)?threads\.(?:net|com)/(?:@[\w.]+/post/|t/)[\w-]+\S*",
        re.IGNORECASE
    ),
}

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "tiktok":  "TikTok",
    "twitter": "𝕏 (Twitter)",
    "reddit":  "Reddit",
    "pinterest": "Pinterest",
    "vkvideo": "VK Видео",
    "twitch":  "Twitch",
    "threads": "Threads",
}

PLATFORM_EMOJIS = {
    "youtube": "▶️",
    "tiktok":  "🎵",
    "twitter": "🐦",
    "reddit":  "🤖",
    "pinterest": "📌",
    "vkvideo": "🟦",
    "twitch":  "🟣",
    "threads": "🧵",
}


def detect_platform(text: str) -> tuple[str | None, str | None]:
    """
    Найти ссылку и определить платформу.
    Возвращает (platform, url) или (None, None).
    """
    for platform, pattern in URL_PATTERNS.items():
        match = pattern.search(text)
        if match:
            return platform, match.group(0)
    return None, None


# ── YT-DLP ОБЁРТКА ────────────────────────────────────────────────────────────

def _ydl_opts(out_path: str, platform: str, height: int | None = None, audio_only: bool = False) -> dict:
    """Настройки yt-dlp под каждую платформу."""

    # Базовые настройки — берём лучшее видео до 720p
    base = {
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "max_filesize": MAX_TOTAL_BYTES,
        "concurrent_fragment_downloads": FRAGMENTS,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
    }
    h = height or MAX_HEIGHT

    if platform == "youtube":
        base.update({
            # Предпочитаем mp4, <=720p, с аудио в одном файле
            "format": (
                f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]"
                "/best"
            ),
            "merge_output_format": "mp4",
        })

    elif platform == "tiktok":
        base.update({
            # TikTok — берём версию без водяного знака если есть
            "format": "best",
            "extractor_args": {
                "tiktok": {
                    "webpage_download": ["1"],
                }
            },
        })

    elif platform == "twitter":
        base.update({
            "format": "best[ext=mp4]/best",
        })

    elif platform == "reddit":
        base.update({
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
        })

    elif platform in ("vkvideo", "twitch"):
        base.update({
            "format": f"best[height<={h}]/bestvideo[height<={h}]+bestaudio/best",
            "merge_output_format": "mp4",
        })

    elif platform == "pinterest":
        base.update({"format": "best"})

    if audio_only:
        # только звук: m4a без перекодирования (если источник AAC), Telegram играет его как аудио
        base.update({
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
        })
        base.pop("merge_output_format", None)

    return base


async def _download_ytdlp(url: str, platform: str, height: int | None = None,
                          audio_only: bool = False, progress: dict | None = None) -> tuple[str | None, dict]:
    """
    Скачать медиа через yt-dlp в временный файл.
    Возвращает (путь_к_файлу, info_dict) или (None, {}).
    """
    try:
        import yt_dlp
    except ImportError:
        logger.error("yt-dlp не установлен!")
        return None, {}

    # Временная папка — чистим сами после отправки
    tmp_dir = tempfile.mkdtemp(prefix="tgdl_")
    out_template = os.path.join(tmp_dir, "%(id)s.%(ext)s")

    opts = _ydl_opts(out_template, platform, height=height, audio_only=audio_only)
    prog = progress if progress is not None else {}
    prog.setdefault("files", {})

    def _hook(d):
        if prog.get("cancel"):
            raise yt_dlp.utils.DownloadCancelled("cancelled by user")
        st = d.get("status")
        fn = d.get("filename") or d.get("tmpfilename") or ""
        if st == "downloading":
            done = d.get("downloaded_bytes") or 0
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            prog["files"][fn] = (done, tot)
            prog["speed"] = d.get("speed") or 0
            if not prog.get("est_total"):
                rf = (d.get("info_dict") or {}).get("requested_formats") or []
                sizes = [(f.get("filesize") or f.get("filesize_approx") or 0) for f in rf]
                if rf and all(sizes):
                    prog["est_total"] = sum(sizes)
        elif st == "finished":
            done, tot = prog["files"].get(fn, (0, 0))
            size = d.get("total_bytes") or d.get("downloaded_bytes") or tot or done
            prog["files"][fn] = (size, size)

    def _pp_hook(d):
        if d.get("status") == "started":
            prog["phase"] = "post"

    opts["progress_hooks"] = [_hook]
    opts["postprocessor_hooks"] = [_pp_hook]

    info = {}
    try:
        loop = asyncio.get_event_loop()

        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                _info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(_info), _info

        # Запускаем блокирующий yt-dlp в отдельном треде
        filepath, raw_info = await asyncio.wait_for(
            loop.run_in_executor(None, _run),
            timeout=DOWNLOAD_TIMEOUT,
        )

        # Ищем реально скачанный файл (расширение может поменяться после merge)
        found = None
        for f in Path(tmp_dir).iterdir():
            if f.is_file():
                found = str(f)
                break

        if not found or not os.path.exists(found):
            logger.warning(f"yt-dlp: файл не найден после скачивания {url}")
            return None, {}

        file_size = os.path.getsize(found)
        if file_size > MAX_TOTAL_BYTES:
            logger.warning(f"Файл слишком большой: {file_size/1024/1024:.1f} МБ (лимит {MAX_TOTAL_MB} МБ)")
            os.remove(found)
            os.rmdir(tmp_dir)
            return None, {"_too_large": True, "_size_mb": round(file_size / 1024 / 1024, 1)}

        info = {
            "title":     raw_info.get("title", ""),
            "uploader":  raw_info.get("uploader") or raw_info.get("channel") or raw_info.get("creator") or "",
            "duration":  raw_info.get("duration") or 0,
            "width":     raw_info.get("width") or 0,
            "height":    raw_info.get("height") or 0,
            "thumbnail": raw_info.get("thumbnail") or "",
            "ext":       raw_info.get("ext") or "",
            "is_photo":  raw_info.get("ext") in ("jpg", "jpeg", "png", "webp"),
            "is_audio":  bool(audio_only),
            "_tmp_dir":  tmp_dir,
            "_size":     file_size,
        }

        return found, info

    except asyncio.TimeoutError:
        logger.error(f"Таймаут скачивания {url}")
        prog["cancel"] = True          # останавливаем фоновый поток yt-dlp
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, {"_timeout": True}
    except Exception as e:
        err_str = str(e)
        logger.error(f"yt-dlp error {url}: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Разбираем типичные ошибки
        if "private" in err_str.lower() or "login" in err_str.lower():
            return None, {"_private": True}
        if "not available" in err_str.lower() or "unavailable" in err_str.lower():
            return None, {"_unavailable": True}
        if "no video formats" in err_str.lower() or "no suitable" in err_str.lower():
            return None, {"_no_video": True}

        return None, {"_error": err_str[:200]}


# ── ОСОБЫЕ ПЛАТФОРМЫ: Threads, Pinterest (запасной путь без yt-dlp) ────────────
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")
_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"


def _md_escape(s: str) -> str:
    """Экранирование для parse_mode=Markdown (legacy): иначе «_» в названии ломает отправку."""
    return re.sub(r"([_*`\[])", r"\\\1", s or "")


async def _http_text(url: str, ua: str = _BROWSER_UA) -> tuple[str, str]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.8"},
                             timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as r:
                if r.status == 200:
                    return await r.text(errors="ignore"), str(r.url)
    except Exception as e:
        logger.info(f"http_text {url[:60]}: {e}")
    return "", url


def _meta(page: str, prop: str) -> str:
    import html as _h
    m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)["\']', page, re.I) \
        or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']', page, re.I)
    return _h.unescape(m.group(1)).strip() if m else ""


def _json_unescape(s: str) -> str:
    import json as _j
    try:
        return _j.loads('"' + s + '"')
    except Exception:
        return s.replace("\\/", "/").replace("\\u0026", "&")


async def _fetch_to_file(url: str, dest: str, max_bytes: int = MAX_TOTAL_BYTES) -> bool:
    """Скачать файл потоком (с лимитом размера). True при успехе."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": _BROWSER_UA, "Referer": url},
                             timeout=aiohttp.ClientTimeout(total=300)) as r:
                if r.status != 200:
                    return False
                size = 0
                with open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        size += len(chunk)
                        if size > max_bytes:
                            return False
                        f.write(chunk)
        return size > 1000
    except Exception as e:
        logger.error(f"fetch_to_file {url[:70]}: {e}")
        return False


def _threads_items(page: str, code: str) -> tuple[list[dict], str, str]:
    """Достаёт из HTML поста Threads: [{type,url}], текст, автора. Пусто — если медиа нет."""
    import json as _j
    items: list[dict] = []
    text = _meta(page, "og:description")
    user = ""

    def from_node(node: dict):
        nonlocal text, user
        if isinstance(node.get("user"), dict) and node["user"].get("username"):
            user = user or node["user"]["username"]
        if isinstance(node.get("caption"), dict) and node["caption"].get("text"):
            text = node["caption"]["text"]
        nodes = node.get("carousel_media") or [node]
        for n in nodes:
            vv = n.get("video_versions") or []
            if vv and vv[0].get("url"):
                items.append({"type": "video", "url": vv[0]["url"]})
                continue
            cands = (n.get("image_versions2") or {}).get("candidates") or []
            if cands and cands[0].get("url"):
                items.append({"type": "photo", "url": cands[0]["url"]})

    def walk(o):
        if isinstance(o, dict):
            if o.get("code") == code and (o.get("video_versions") or o.get("image_versions2") or o.get("carousel_media")):
                from_node(o)
                return True
            return any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        return False

    for blob in re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', page, re.S):
        if "video_versions" not in blob and "image_versions2" not in blob:
            continue
        try:
            if walk(_j.loads(blob)):
                break
        except Exception:
            continue

    if not items:   # запасной путь: регулярки по сырому HTML и og-теги
        m = re.search(r'"video_versions":\[\{"type":\d+,"url":"(.*?)"', page)
        if m:
            items.append({"type": "video", "url": _json_unescape(m.group(1))})
        ogv = _meta(page, "og:video")
        if not items and ogv:
            items.append({"type": "video", "url": ogv})
        ogi = _meta(page, "og:image")
        if not items and ogi:
            items.append({"type": "photo", "url": ogi})
    return items[:10], text, user


async def _download_threads(url: str) -> tuple[str | None, dict]:
    m = re.search(r"threads\.(?:net|com)/(?:@([\w.]+)/post|t)/([\w-]+)", url)
    if not m:
        return None, {"_error": "не разобрал ссылку Threads"}
    user, code = m.group(1) or "", m.group(2)
    pages = []
    for dom in ("threads.net", "threads.com"):
        base = f"https://www.{dom}/@{user}/post/{code}" if user else f"https://www.{dom}/t/{code}"
        pages += [(base, _BROWSER_UA), (base + "/embed", _BROWSER_UA), (base, _CRAWLER_UA)]
    items, text, author = [], "", user
    for page_url, ua in pages:
        page, _ = await _http_text(page_url, ua)
        if not page:
            continue
        items, text, a = _threads_items(page, code)
        author = a or author
        if items:
            break
        if text and not items:      # хотя бы текст поста — запомним, но попробуем другие варианты
            continue
    if not items and not text:
        return None, {"_unavailable": True}
    if not items:
        return None, {"_text_only": True, "uploader": author, "title": text}

    tmp_dir = tempfile.mkdtemp(prefix="tgdl_")
    paths = []
    for i, it in enumerate(items):
        ext = "mp4" if it["type"] == "video" else "jpg"
        dest = os.path.join(tmp_dir, f"th_{i}.{ext}")
        if await _fetch_to_file(it["url"], dest):
            paths.append((dest, it["type"]))
    if not paths:
        shutil_rm(tmp_dir)
        return None, {"_error": "Threads не отдал файл (ссылка протухла или пост закрыт)"}
    info = {
        "title": text[:100], "uploader": author, "duration": 0, "width": 0, "height": 0,
        "is_photo": paths[0][1] == "photo", "_tmp_dir": tmp_dir, "_size": os.path.getsize(paths[0][0]),
    }
    if len(paths) > 1:
        info["_album"] = paths
    return paths[0][0], info


def shutil_rm(path: str):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


async def _download_pinterest_fallback(url: str) -> tuple[str | None, dict]:
    """Если yt-dlp не справился (чаще всего — пин-картинка): берём og:video / og:image со страницы."""
    page, final = await _http_text(url)
    if not page:
        return None, {"_unavailable": True}
    vid = _meta(page, "og:video") or _meta(page, "og:video:url")
    img = _meta(page, "og:image")
    if not vid and not img:
        return None, {"_no_video": True}
    if not vid and img:
        img = re.sub(r"/\d+x(?:\d+)?/", "/originals/", img)
    tmp_dir = tempfile.mkdtemp(prefix="tgdl_")
    is_video = bool(vid) and vid.split("?")[0].endswith((".mp4", ".mov", ".webm"))
    ext = "mp4" if is_video else (img.split("?")[0].rsplit(".", 1)[-1][:4] or "jpg")
    dest = os.path.join(tmp_dir, f"pin.{ext}")
    ok = await _fetch_to_file(vid if is_video else img, dest)
    if not ok and not is_video and "/originals/" in img:      # originals бывают недоступны — берём как есть
        ok = await _fetch_to_file(_meta(page, "og:image"), dest)
    if not ok:
        shutil_rm(tmp_dir)
        return None, {"_error": "Pinterest не отдал файл"}
    info = {"title": _meta(page, "og:title")[:100], "uploader": "", "duration": 0, "width": 0, "height": 0,
            "is_photo": not is_video, "_tmp_dir": tmp_dir, "_size": os.path.getsize(dest)}
    return dest, info


async def _download_media(url: str, platform: str, height: int | None = None,
                          audio_only: bool = False, progress: dict | None = None) -> tuple[str | None, dict]:
    """Диспетчер: Threads — свой парсер; Pinterest — yt-dlp, затем og-теги; остальное — yt-dlp."""
    if platform == "threads":
        return await _download_threads(url)
    if platform == "pinterest":
        if "pin.it/" in url:                                   # короткая ссылка → реальный адрес пина
            _, final = await _http_text(url)
            if "/pin/" in final:
                url = final
        path, info = await _download_ytdlp(url, platform, height, audio_only, progress)
        if path:
            return path, info
        fb_path, fb_info = await _download_pinterest_fallback(url)
        return (fb_path, fb_info) if fb_path else (None, info or fb_info)
    return await _download_ytdlp(url, platform, height, audio_only, progress)



async def ytdlp_instagram_post(url: str) -> dict | None:
    """
    Метаданные поста/рилса Instagram через yt-dlp БЕЗ скачивания файла — только прямые CDN-ссылки.
    Возвращает dict в формате instagram_viewer (media: [{type,url,thumb}], username, caption…) или None.
    Опционально: IG_YTDLP_COOKIES=/путь/cookies.txt (Netscape) — для постов, требующих логин.
    """
    try:
        import yt_dlp
    except ImportError:
        return None

    def _run():
        opts = {
            "quiet": True, "no_warnings": True, "noprogress": True,
            "skip_download": True, "socket_timeout": 20, "format": "best",
        }
        cookie = os.getenv("IG_YTDLP_COOKIES", "").strip()
        if cookie and os.path.exists(cookie):
            opts["cookiefile"] = cookie
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(asyncio.get_event_loop().run_in_executor(None, _run), timeout=60)
    except Exception as e:
        logger.info(f"yt-dlp instagram {url[:70]}: {str(e)[:150]}")
        return None
    if not info:
        return None

    def _one(e: dict) -> dict | None:
        thumb = e.get("thumbnail") or ""
        ext = (e.get("ext") or "").lower()
        fmts = [f for f in (e.get("formats") or [])
                if f.get("url") and f.get("vcodec") not in (None, "none")]
        with_audio = [f for f in fmts if f.get("acodec") not in (None, "none")]
        pool = with_audio or fmts
        if pool:
            best = max(pool, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))
            return {"type": "video", "url": best["url"], "thumb": thumb}
        if e.get("url") and ext in ("mp4", "mov", "webm", "m4v"):
            return {"type": "video", "url": e["url"], "thumb": thumb}
        img = e.get("url") if ext in ("jpg", "jpeg", "png", "webp") else thumb
        if img:
            return {"type": "photo", "url": img, "thumb": img}
        return None

    entries = info.get("entries") or [info]
    media = [m for m in (_one(e) for e in entries if isinstance(e, dict)) if m]
    if not media:
        return None
    sc_m = re.search(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    sc = sc_m.group(1) if sc_m else str(info.get("id") or "")
    username = (info.get("channel") or info.get("uploader_id") or info.get("uploader") or "")
    return {
        "id": str(info.get("id") or sc), "shortcode": sc,
        "timestamp": int(info.get("timestamp") or 0),
        "caption": (info.get("description") or info.get("title") or "")[:500],
        "like_count": int(info.get("like_count") or 0),
        "comment_count": int(info.get("comment_count") or 0),
        "media": media[:20], "username": str(username).lstrip("@"),
        "is_reel": "/reel" in url, "music": None, "fallback": "yt-dlp",
    }


def _make_error_message(info: dict, label: str, url: str) -> str:
    """Текст ошибки скачивания (раньше функция вызывалась, но не была определена)."""
    if info.get("_too_large"):
        return (f"❌ Файл слишком большой даже для нарезки: {info.get('_size_mb', '?')} МБ "
                f"(максимум {MAX_TOTAL_MB} МБ).\n\n🔗 Оригинал: {url}")
    if info.get("_timeout"):
        return f"⏱ {label}: скачивание заняло слишком много времени."
    if info.get("_private"):
        return f"🔒 {label}: видео приватное или требует входа."
    if info.get("_unavailable"):
        return f"❌ {label}: видео недоступно."
    if info.get("_no_video"):
        return f"❌ {label}: не нашёл видео по этой ссылке."
    return f"❌ Не удалось скачать с {label}.\n{str(info.get('_error', ''))[:200]}".strip()


async def _probe_duration(path: str) -> float:
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=30)
        return float((out or b"0").strip() or 0)
    except Exception:
        return 0.0


async def _split_video(path: str, tmp_dir: str, duration: float = 0, audio: bool = False) -> list[str] | None:
    """
    Нарезать видео на части ≤ ~49 МБ БЕЗ пережатия (-c copy): качество то же, что в исходнике.
    Резка идёт по ключевым кадрам, поэтому размеры частей «плавают» — при перелёте уменьшаем шаг и повторяем.
    None — не вышло (нет ffmpeg / слишком много частей).
    """
    import glob
    ext = "m4a" if audio else "mp4"
    maps = ["-map", "0:a:0"] if audio else ["-map", "0:v:0", "-map", "0:a:0?"]
    size = os.path.getsize(path)
    duration = duration or await _probe_duration(path)
    if not duration:
        logger.error(f"split: не удалось определить длительность ({path})")
        return None
    if size / PART_TARGET_BYTES > MAX_PARTS:
        logger.error(f"split: {size/1024/1024:.0f} МБ → ≥{size/PART_TARGET_BYTES:.0f} частей, лимит {MAX_PARTS}")
        return None
    seg = max(5, int(duration * PART_TARGET_BYTES / size))
    for attempt in range(6):
        for old in glob.glob(os.path.join(tmp_dir, f"part_*.{ext}")):
            os.remove(old)
        pattern = os.path.join(tmp_dir, f"part_%03d.{ext}")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", path,
               *maps, "-c", "copy",
               "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1",
               "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart", pattern]
        try:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(p.communicate(), timeout=600)
        except FileNotFoundError:
            logger.error("ffmpeg не найден — нарезка невозможна")
            return None
        except Exception as e:
            logger.error(f"ffmpeg split: {e}")
            return None
        if p.returncode != 0:
            logger.error(f"ffmpeg split rc={p.returncode}: {(err or b'')[:200]!r}")
            return None
        parts = sorted(glob.glob(os.path.join(tmp_dir, f"part_*.{ext}")))
        if not parts:
            logger.error("split: ffmpeg не создал ни одной части")
            return None
        biggest = max(os.path.getsize(x) for x in parts) / 1024 / 1024
        logger.info(f"split: попытка {attempt+1}, seg={seg}с, частей={len(parts)}, макс. часть={biggest:.1f} МБ")
        if all(os.path.getsize(x) <= PART_HARD_BYTES for x in parts):
            if len(parts) <= MAX_PARTS:
                return parts
            logger.error(f"split: частей {len(parts)} > лимита {MAX_PARTS}")
            return None
        # уменьшаем шаг пропорционально перелёту, а не слепо на 25%
        seg = max(3, int(seg * min(0.85, PART_TARGET_BYTES / (biggest * 1024 * 1024))))
    logger.error("split: не удалось уложить части в лимит Telegram за 6 попыток")
    return None


async def _send_video_parts(message: types.Message, parts: list[str], caption: str,
                            width: int, height: int, progress_cb=None) -> None:
    """Части — альбомами по 10, по порядку. Подпись с номерами частей."""
    from aiogram.types import InputMediaVideo
    n = len(parts)
    for g in range(0, n, 10):
        chunk = parts[g:g + 10]
        media = []
        for i, fp in enumerate(chunk):
            num = g + i + 1
            cap = (f"{caption}\n\n✂️ Часть {num}/{n}" if g == 0 and i == 0 else f"✂️ Часть {num}/{n}")
            media.append(InputMediaVideo(
                media=FSInputFile(fp, filename=f"part_{num:02d}_of_{n:02d}.mp4"),
                caption=cap[:1000], parse_mode="Markdown", supports_streaming=True,
                width=width or None, height=height or None))
        try:
            if len(media) == 1:
                m0 = media[0]
                await message.bot.send_video(
                    message.chat.id, m0.media, caption=m0.caption, parse_mode="Markdown",
                    supports_streaming=True, width=m0.width, height=m0.height, request_timeout=900)
            else:
                await message.bot.send_media_group(message.chat.id, media, request_timeout=900)
        except TypeError:       # старый aiogram без request_timeout
            if len(media) == 1:
                m0 = media[0]
                await message.bot.send_video(message.chat.id, m0.media, caption=m0.caption,
                                             parse_mode="Markdown", supports_streaming=True)
            else:
                await message.bot.send_media_group(message.chat.id, media)
        if progress_cb:
            try:
                await progress_cb(min(g + 10, n), n)
            except Exception:
                pass
        await asyncio.sleep(1.0)


def _cleanup(filepath: str, info: dict):
    """Удалить временные файлы."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        tmp_dir = info.get("_tmp_dir")
        if tmp_dir and os.path.isdir(tmp_dir):
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:
        logger.debug(f"cleanup error: {e}")


def _fmt_duration(seconds: int | float) -> str:
    """Форматировать длительность в MM:SS или HH:MM:SS."""
    if not seconds:
        return ""
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


# ── СКАЧИВАНИЕ ФОТО ДЛЯ REDDIT/TWITTER (картинки без видео) ──────────────────

async def _download_image_bytes(url: str) -> bytes | None:
    """Скачать изображение напрямую через aiohttp."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return data if len(data) > 1000 else None
    except Exception as e:
        logger.error(f"image download error {url}: {e}")
    return None


# ── ХЕНДЛЕР ───────────────────────────────────────────────────────────────────

# ── Форматирование ────────────────────────────────────────────────────────────
def _fmt_size(b: float) -> str:
    mb = b / 1024 / 1024
    return f"{mb / 1024:.1f} ГБ" if mb >= 1024 else f"{mb:.0f} МБ"


def _fmt_wait(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"~{max(5, round(sec / 5) * 5)} с"
    m, s = divmod(round(sec / 10) * 10, 60)
    if m >= 10:
        return f"~{m + (1 if s >= 30 else 0)} мин"
    return f"~{m} мин {s} с" if s else f"~{m} мин"


def _bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, pct * width // 100))
    return "▓" * filled + "░" * (width - filled)


# ── Очередь загрузок ──────────────────────────────────────────────────────────
class _Job:
    _seq = 0

    def __init__(self, user_id: int, kind: str, est_bytes: int = 0):
        _Job._seq += 1
        self.id = _Job._seq
        self.user_id = user_id
        self.kind = kind                      # "video" | "audio"
        self.est_bytes = est_bytes
        self.event = asyncio.Event()          # выставляется, когда задача получила слот
        self.cancelled = False
        self.ok = False
        self.started = 0.0
        self.prog: dict = {"files": {}, "speed": 0, "est_total": 0, "phase": "download"}


class _DownloadQueue:
    """FIFO-очередь с N слотами. Считает позицию и примерное ожидание по реальному времени прошлых задач."""

    def __init__(self, slots: int):
        self.slots = max(1, slots)
        self.running: list[_Job] = []
        self.waiting: list[_Job] = []
        self.avg = {"video": 90.0, "audio": 40.0}      # стартовые оценки, дальше — скользящее среднее

    def active_for(self, uid: int) -> int:
        return sum(1 for j in self.running + self.waiting if j.user_id == uid)

    def position(self, job: _Job) -> int:
        return self.waiting.index(job) + 1 if job in self.waiting else 0

    def eta(self, job: _Job) -> float:
        """Сколько секунд до старта job: прогоняем простую симуляцию по слотам."""
        now = time.monotonic()
        free = sorted(max(5.0, self.avg[j.kind] - (now - j.started)) for j in self.running)
        free += [0.0] * (self.slots - len(free))
        free.sort()
        for j in self.waiting:
            t = free.pop(0)
            if j is job:
                return t
            free.append(t + self.avg[j.kind])
            free.sort()
        return 0.0

    def submit(self, job: _Job) -> bool:
        """True — слот свободен, стартуем сразу; False — встали в очередь."""
        if len(self.running) < self.slots and not self.waiting:
            self._start(job)
            return True
        self.waiting.append(job)
        return False

    def _start(self, job: _Job) -> None:
        job.started = time.monotonic()
        self.running.append(job)
        job.event.set()

    def _dispatch(self) -> None:
        while self.waiting and len(self.running) < self.slots:
            self._start(self.waiting.pop(0))

    def cancel_waiting(self, job: _Job) -> bool:
        if job in self.waiting:
            self.waiting.remove(job)
            job.event.set()
            return True
        return False

    def release(self, job: _Job) -> None:
        if job in self.running:
            self.running.remove(job)
            if job.ok:
                el = time.monotonic() - job.started
                self.avg[job.kind] = 0.7 * self.avg[job.kind] + 0.3 * el
        self._dispatch()


_QUEUE = _DownloadQueue(QUEUE_SLOTS)
_JOBS: dict[int, _Job] = {}
_PENDING: "OrderedDict[str, dict]" = OrderedDict()        # ожидают выбора качества
QUALITY_TIERS = [h for h in (1080, 720, 480, 360) if h <= MAX_HEIGHT] or [MAX_HEIGHT]


async def _edit(status: types.Message, text: str, kb: InlineKeyboardMarkup | None = None, **kw) -> None:
    try:
        await status.edit_text(text, reply_markup=kb, **kw)
    except Exception as e:
        if "not modified" not in str(e):
            logger.debug(f"status edit: {e}")


def _cancel_kb(job: _Job) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"dlx:{job.id}")]])


def _job_header(emoji: str, label: str, quality, title: str = "") -> str:
    q = " · 🎧 аудио" if quality == "audio" else (f" · {quality}p" if isinstance(quality, int) else "")
    head = f"{emoji} {label}{q}"
    return f"{head}\n🎬 {title[:60]}" if title else head


def _render_progress(header: str, job: _Job) -> str:
    pr = job.prog
    if pr.get("phase") == "post":
        return f"{header}\n🔧 Обрабатываю (склейка видео и звука)…"
    done = sum(d for d, _ in pr["files"].values())
    total = pr.get("est_total") or sum(t for _, t in pr["files"].values())
    if not done:
        el = int(time.monotonic() - job.started) if job.started else 0
        return f"{header}\n⬇️ Скачиваю… {el} с"
    lines = [header]
    if total and total >= done:
        pct = min(99, int(done * 100 / total))
        lines.append(f"⬇️ {_bar(pct)} {pct}%  ({_fmt_size(done)} из {_fmt_size(total)})")
    else:
        lines.append(f"⬇️ Скачано {_fmt_size(done)}")
    speed = pr.get("speed") or 0
    if speed:
        tail = f"🚀 {speed / 1024 / 1024:.1f} МБ/с"
        if total and total > done:
            tail += f" · осталось {_fmt_wait((total - done) / speed)}"
        lines.append(tail)
    return "\n".join(lines)


async def _progress_ticker(status: types.Message, job: _Job, header: str, interval: float = 3.5) -> None:
    last = ""
    kb = _cancel_kb(job)
    while True:
        await asyncio.sleep(interval)
        text = _render_progress(header, job)
        if text != last:
            await _edit(status, text, kb)
            last = text


async def _wait_in_queue(job: _Job, status: types.Message, header: str) -> bool:
    """Ждём слот, показывая позицию и ожидание. False — отменили, пока ждали."""
    if _QUEUE.submit(job):
        return True
    last = ""
    kb = _cancel_kb(job)
    while not job.event.is_set():
        pos = _QUEUE.position(job)
        if pos:
            ahead = pos - 1
            text = (f"⏳ Ты в очереди: {pos}-й\n"
                    f"👥 Впереди тебя: {ahead} · сейчас в работе: {len(_QUEUE.running)}\n"
                    f"⌛ Примерное ожидание: {_fmt_wait(_QUEUE.eta(job))}\n\n{header}")
            if text != last:
                await _edit(status, text, kb)
                last = text
        try:
            await asyncio.wait_for(job.event.wait(), timeout=6)
        except asyncio.TimeoutError:
            pass
    return not job.cancelled


# ── Выбор качества (YouTube) ──────────────────────────────────────────────────
async def _probe_youtube(url: str) -> dict | None:
    try:
        import yt_dlp
    except ImportError:
        return None

    def _run():
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                "socket_timeout": 20}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await asyncio.wait_for(asyncio.get_event_loop().run_in_executor(None, _run), timeout=30)
    except Exception as e:
        logger.info(f"probe youtube {url[:60]}: {str(e)[:150]}")
        return None


def _quality_options(info: dict | None) -> list[dict]:
    """[{tier, label, size}] — только варианты, которые реально влезут в лимиты. Последний — аудио."""
    if not info:
        return ([{"tier": h, "label": f"{h}p", "size": 0} for h in QUALITY_TIERS]
                + [{"tier": "audio", "label": "🎧 Аудио", "size": 0}])
    dur = info.get("duration") or 0
    fmts = info.get("formats") or []

    def fsz(f: dict) -> int:
        sz = f.get("filesize") or f.get("filesize_approx")
        if sz:
            return int(sz)
        tbr = f.get("tbr") or 0
        return int(tbr * 1000 / 8 * dur) if tbr and dur else 0

    audio = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    a_pool = [f for f in audio if f.get("ext") == "m4a"] or audio
    a_best = max(a_pool, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    a_size = fsz(a_best) if a_best else (int(128 * 1000 / 8 * dur) if dur else 0)

    out, seen = [], set()
    for tier in QUALITY_TIERS:
        vids = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
                and (f.get("height") or 0) and f["height"] <= tier]
        if not vids:
            continue
        pool = [f for f in vids if f.get("ext") == "mp4"] or vids
        best = max(pool, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))
        actual = best["height"]
        if actual in seen:
            continue
        seen.add(actual)
        v = fsz(best)
        size = v + a_size if v else 0
        parts = -(-size // PART_TARGET_BYTES) if size else 0
        if size and (size > MAX_TOTAL_BYTES or parts > MAX_PARTS):
            continue                                   # не влезет — не предлагаем
        out.append({"tier": tier, "label": f"{actual}p", "size": size})
    if not out and not fmts:                           # нет данных о форматах — даём стандартный набор
        out = [{"tier": h, "label": f"{h}p", "size": 0} for h in QUALITY_TIERS]
    out.append({"tier": "audio", "label": "🎧 Аудио", "size": a_size})
    return out


def _quality_kb(tok: str, options: list[dict]) -> InlineKeyboardMarkup:
    btns = []
    for o in options:
        txt = o["label"] + (f" · ~{_fmt_size(o['size'])}" if o["size"] else "")
        btns.append(InlineKeyboardButton(text=txt, callback_data=f"dlq:{tok}:{o['tier']}"))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=f"dlq:{tok}:x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _expire_pending(tok: str, status: types.Message, delay: int = 300) -> None:
    await asyncio.sleep(delay)
    if _PENDING.pop(tok, None) is not None:
        await _edit(status, "⌛ Время выбора качества вышло — кинь ссылку ещё раз.")


async def _ask_youtube_quality(message: types.Message, url: str) -> None:
    status = await message.answer("🔎 Смотрю видео…")
    info = await _probe_youtube(url)
    if info and info.get("is_live"):
        await _edit(status, "📡 Это прямой эфир — скачать его нельзя.")
        return
    options = _quality_options(info)
    tok = secrets.token_hex(4)
    title = ((info or {}).get("title") or "")[:80]
    _PENDING[tok] = {"message": message, "url": url, "user_id": message.from_user.id if message.from_user else 0,
                     "options": options, "title": title}
    while len(_PENDING) > 200:
        _PENDING.popitem(last=False)
    lines = ["▶️ YouTube"]
    if title:
        lines.append(f"🎬 {title}")
    meta = []
    dur = _fmt_duration((info or {}).get("duration") or 0)
    if dur:
        meta.append(f"⏱ {dur}")
    up = (info or {}).get("uploader") or (info or {}).get("channel")
    if up:
        meta.append(str(up)[:40])
    if meta:
        lines.append(" · ".join(meta))
    lines.append("\nВыбери качество" + (" (размеры примерные):" if any(o["size"] for o in options) else ":"))
    await _edit(status, "\n".join(lines), _quality_kb(tok, options))
    asyncio.create_task(_expire_pending(tok, status))


async def callback_dl_quality(callback: types.CallbackQuery):
    try:
        _, tok, q = (callback.data or "").split(":", 2)
    except ValueError:
        await callback.answer()
        return
    p = _PENDING.get(tok)
    if not p:
        await callback.answer("Выбор устарел — кинь ссылку заново", show_alert=True)
        return
    if callback.from_user.id != p["user_id"]:
        await callback.answer("Это не твоя ссылка 🙂", show_alert=True)
        return
    _PENDING.pop(tok, None)
    if q == "x":
        await _edit(callback.message, "❌ Отменено.")
        await callback.answer()
        return
    await callback.answer()
    quality = "audio" if q == "audio" else int(q)
    opt = next((o for o in p["options"] if str(o["tier"]) == q), None)
    await _start_job(p["message"], "youtube", p["url"], quality=quality, status=callback.message,
                     est_bytes=(opt or {}).get("size", 0), title=p["title"], user_id=callback.from_user.id)


async def callback_dl_cancel(callback: types.CallbackQuery):
    try:
        jid = int((callback.data or "").split(":")[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    job = _JOBS.get(jid)
    if not job:
        await callback.answer("Уже не активно", show_alert=False)
        return
    if callback.from_user.id != job.user_id:
        await callback.answer("Это не твоя загрузка 🙂", show_alert=True)
        return
    job.cancelled = True
    job.prog["cancel"] = True
    _QUEUE.cancel_waiting(job)
    await callback.answer("Отменяю…")


# ── Запуск задачи ─────────────────────────────────────────────────────────────
async def handle_media_link(message: types.Message, custom_url: str | None = None,
                            custom_platform: str | None = None, ask_quality: bool | None = None,
                            quality=None):
    """
    Основной хендлер: пользователь кинул ссылку →
    определяем платформу → (YouTube: выбор качества) → очередь → скачиваем → отправляем.
    Вызовы из других модулей (custom_url) качество не спрашивают — берут MAX_HEIGHT.
    """
    text = custom_url or message.text or message.caption or ""

    if custom_platform and custom_url:
        platform, url = custom_platform, custom_url
    else:
        platform, url = detect_platform(text)

    if not platform or not url:
        return  # не наша ссылка — пропускаем

    if ask_quality is None:
        ask_quality = custom_url is None
    if platform == "youtube" and ask_quality and quality is None:
        await _ask_youtube_quality(message, url)
        return
    await _start_job(message, platform, url, quality=quality)


async def _start_job(message: types.Message, platform: str, url: str, quality=None,
                     status: types.Message | None = None, est_bytes: int = 0,
                     title: str = "", user_id: int | None = None):
    emoji = PLATFORM_EMOJIS[platform]
    label = PLATFORM_LABELS[platform]
    uid = user_id if user_id is not None else (message.from_user.id if message.from_user else 0)
    is_bot = bool(message.from_user and message.from_user.is_bot)
    header = _job_header(emoji, label, quality, title)

    async def _say(text: str):
        nonlocal status
        if status is None:
            status = await message.answer(text)
        else:
            await _edit(status, text)

    if not is_bot and uid and _QUEUE.active_for(uid) >= PER_USER_LIMIT:
        await _say(f"⏳ У тебя уже {_QUEUE.active_for(uid)} загрузк(и) в очереди — дождись, пока они закончатся.")
        return
    if len(_QUEUE.waiting) >= QUEUE_MAX:
        await _say(f"🚧 Очередь переполнена ({len(_QUEUE.waiting)} ожидают). Попробуй чуть позже.")
        return

    job = _Job(uid, "audio" if quality == "audio" else "video", est_bytes)
    _JOBS[job.id] = job
    if status is None:
        status = await message.answer(f"{header}\n⏳ Принял, ставлю в очередь…", reply_markup=_cancel_kb(job))
    else:
        await _edit(status, f"{header}\n⏳ Принял, ставлю в очередь…", _cancel_kb(job))

    try:
        if not await _wait_in_queue(job, status, header):
            await _edit(status, "❌ Загрузка отменена.")
            return
        await _process(message, platform, url, quality, status, job, header, title)
    except Exception as e:
        logger.exception(f"media job error: {e}")
        await _edit(status, f"❌ Что-то пошло не так.\n{str(e)[:200]}")
    finally:
        _JOBS.pop(job.id, None)
        _QUEUE.release(job)


async def _send_audio_result(message: types.Message, status: types.Message, filepath: str,
                             info: dict, header: str) -> None:
    title = (info.get("title") or "audio")[:60]
    performer = (info.get("uploader") or "")[:60]
    duration = int(info.get("duration") or 0)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title).strip() or "audio"

    async def _send(path: str, fname: str, caption: str | None, dur: int | None):
        kw = dict(caption=caption, title=title, performer=performer or None, duration=dur)
        f = FSInputFile(path, filename=fname)
        try:
            await message.bot.send_audio(message.chat.id, f, request_timeout=600, **kw)
        except TypeError:       # старый aiogram без request_timeout
            await message.bot.send_audio(message.chat.id, f, **kw)

    if os.path.getsize(filepath) <= MAX_VIDEO_BYTES:
        await _edit(status, f"{header}\n📤 Загружаю в Telegram…")
        await _send(filepath, f"{safe}.m4a", f"🎧 {title}"[:1000], duration or None)
    else:
        await _edit(status, f"{header}\n✂️ Файл {_fmt_size(os.path.getsize(filepath))} — режу на части…")
        parts = await _split_video(filepath, info.get("_tmp_dir") or os.path.dirname(filepath), duration, audio=True)
        if not parts:
            raise RuntimeError("не удалось нарезать аудио на части")
        n = len(parts)
        for i, fp in enumerate(parts, 1):
            await _edit(status, f"{header}\n📤 Загружаю в Telegram: {i}/{n} частей")
            await _send(fp, f"{safe}_part{i:02d}.m4a", f"🎧 {title}\n✂️ Часть {i}/{n}"[:1000], None)
            await asyncio.sleep(0.5)
    try:
        await status.delete()
    except Exception:
        pass


async def _process(message: types.Message, platform: str, url: str, quality, status: types.Message,
                   job: _Job, header: str, title_hint: str = ""):
    """Скачивание → отправка. Выполняется, пока задача держит слот очереди."""
    emoji = PLATFORM_EMOJIS[platform]
    label = PLATFORM_LABELS[platform]

    # защита диска хостинга
    try:
        free = shutil.disk_usage(tempfile.gettempdir()).free
    except Exception:
        free = None
    need = max(MIN_FREE_MB * 1024 * 1024, int(job.est_bytes * 2.3))
    if free is not None and free < need:
        logger.warning(f"мало места на диске: свободно {free/1024/1024:.0f} МБ, нужно {need/1024/1024:.0f} МБ")
        await _edit(status, f"💾 На сервере сейчас мало места (свободно {_fmt_size(free)}). Попробуй чуть позже.")
        return

    height = quality if isinstance(quality, int) else None
    audio_only = quality == "audio"
    await _edit(status, f"{header}\n⬇️ Начинаю скачивание…", _cancel_kb(job))
    ticker = asyncio.create_task(_progress_ticker(status, job, header))
    try:
        filepath, info = await _download_media(url, platform, height=height, audio_only=audio_only,
                                               progress=job.prog)
    finally:
        ticker.cancel()

    if job.cancelled:
        _cleanup(filepath, info)
        await _edit(status, "❌ Загрузка отменена.")
        return

    # ── Threads: пост только с текстом ────────────────────────────
    if info.get("_text_only"):
        job.ok = True
        who = f"*{_md_escape(info.get('uploader', ''))}*\n" if info.get("uploader") else ""
        body = _md_escape(info.get("title", ""))[:3500]
        try:
            await status.edit_text(f"{emoji} {who}{body}\n\n_В посте нет фото/видео — прислал текст_",
                                   parse_mode="Markdown", disable_web_page_preview=True)
        except Exception:
            await status.edit_text(f"{emoji} {info.get('uploader', '')}\n{info.get('title', '')}")
        return

    # ── Ошибки ────────────────────────────────────────────────────
    if not filepath:
        err_msg = _make_error_message(info, label, url)
        try:
            await status.edit_text(err_msg, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception:
            await message.answer(err_msg, parse_mode="Markdown")
        return

    # ── Успех: отправляем медиа ───────────────────────────────────
    title    = info.get("title", "")[:100]
    uploader = info.get("uploader", "")[:60]
    duration = info.get("duration", 0)
    width    = info.get("width", 0)
    height   = info.get("height", 0)
    is_photo = info.get("is_photo", False)

    caption_parts = []
    if uploader:
        caption_parts.append(f"{emoji} *{_md_escape(uploader)}*")
    if title:
        caption_parts.append(f"_{_md_escape(title)}_")
    dur_str = _fmt_duration(duration)
    if dur_str:
        caption_parts.append(f"⏱ {dur_str}")
    caption_parts.append(f"🔗 {label}")
    caption = "\n".join(caption_parts)[:1000]

    try:
        await status.edit_text(f"{emoji} Загружаю в Telegram...", parse_mode="Markdown")
    except Exception:
        pass

    if info.get("is_audio"):
        try:
            await _send_audio_result(message, status, filepath, info, header)
            job.ok = True
        except Exception as e:
            logger.error(f"audio send error: {e}")
            await _edit(status, f"❌ Не удалось отправить аудио.\n{str(e)[:200]}")
        finally:
            _cleanup(filepath, info)
        return

    # Карусель (Threads): отправляем альбомом
    if info.get("_album"):
        from aiogram.types import InputMediaPhoto, InputMediaVideo
        media = []
        for i, (pth, typ) in enumerate(info["_album"][:10]):
            if os.path.getsize(pth) > MAX_VIDEO_BYTES:
                continue
            cls = InputMediaVideo if typ == "video" else InputMediaPhoto
            media.append(cls(media=FSInputFile(pth), caption=caption if i == 0 else None,
                             parse_mode="Markdown" if i == 0 else None))
        try:
            if len(media) > 1:
                await message.bot.send_media_group(message.chat.id, media)
            elif media:
                m0 = media[0]
                if isinstance(m0, InputMediaVideo):
                    await message.answer_video(m0.media, caption=caption, parse_mode="Markdown")
                else:
                    await message.answer_photo(m0.media, caption=caption, parse_mode="Markdown")
            else:
                raise ValueError("все файлы больше лимита")
            job.ok = True
            try:
                await status.delete()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"album send error: {e}")
            try:
                await status.edit_text(f"❌ Не удалось отправить альбом.\n`{str(e)[:200]}`", parse_mode="Markdown")
            except Exception:
                pass
        finally:
            _cleanup(filepath, info)
        return

    # Большое видео: режем на части ≤ лимита Telegram без потери качества
    if not is_photo and os.path.getsize(filepath) > MAX_VIDEO_BYTES:
        size_mb = os.path.getsize(filepath) / 1024 / 1024
        try:
            await status.edit_text(f"✂️ Файл {size_mb:.0f} МБ — режу на части без потери качества…")
        except Exception:
            pass
        parts = await _split_video(filepath, info.get("_tmp_dir") or os.path.dirname(filepath), duration)
        if not parts:
            try:
                await status.edit_text(
                    f"❌ Не удалось нарезать ({size_mb:.0f} МБ, максимум частей: {MAX_PARTS}).\n\n"
                    f"🔗 Оригинал: {url}", disable_web_page_preview=True)
            except Exception:
                pass
            _cleanup(filepath, info)
            return
        try:
            await status.edit_text(f"{emoji} Загружаю {len(parts)} частей в Telegram…")
        except Exception:
            pass
        try:
            async def _pcb(done, total):
                await _edit(status, f"{header}\n📤 Загружаю в Telegram: {done}/{total} частей")
            await _send_video_parts(message, parts, caption, width, height, progress_cb=_pcb)
            job.ok = True
            try:
                await status.delete()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"send parts error: {e}")
            try:
                await status.edit_text(f"❌ Не удалось отправить части.\n`{str(e)[:200]}`",
                                       parse_mode="Markdown")
            except Exception:
                pass
        finally:
            _cleanup(filepath, info)
        return

    try:
        if is_photo:
            with open(filepath, "rb") as f:
                data = f.read()
            await message.answer_photo(
                BufferedInputFile(data, filename="photo.jpg"),
                caption=caption,
                parse_mode="Markdown",
            )
        else:
            video_file = FSInputFile(filepath, filename="video.mp4")
            await message.answer_video(
                video_file,
                caption=caption,
                parse_mode="Markdown",
                width=width or None,
                height=height or None,
                duration=int(duration) if duration else None,
                supports_streaming=True,
            )

        job.ok = True
        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        err_str = str(e)
        logger.error(f"Telegram send error: {e}")

        if "file is too big" in err_str.lower() or "request entity too large" in err_str.lower():
            try:
                await status.edit_text(
                    f"❌ Файл слишком большой для Telegram.\n"
                    f"Лимит одного файла: {MAX_VIDEO_MB} МБ\n\n"
                    f"🔗 Оригинал: {url}",
                    disable_web_page_preview=True
                )
            except Exception:
                pass
        else:
            try:
                await status.edit_text(
                    f"❌ Не удалось отправить медиа.\n`{err_str[:200]}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
    finally:
        _cleanup(filepath, info)


async def cmd_dl(message: types.Message):
    """
    /dl <url> — явная команда скачивания.
    """
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2 or not arg[1].strip():
        await message.answer(
            "📥 *Скачиватель медиа*\n\n"
            "Поддерживаемые платформы:\n"
            "▶️ YouTube (видео и Shorts)\n"
            "🎵 TikTok (без водяного знака)\n"
            "🐦 X / Twitter (видео и фото)\n"
            "🤖 Reddit (видео и гиф)\n"
            "📌 Pinterest (пины-картинки и видео)\n"
            "🟦 VK Видео и Клипы\n"
            "🟣 Twitch-клипы\n"
            "🧵 Threads (фото, видео, карусели)\n\n"
            "Просто скинь ссылку в чат — бот сам скачает!\n"
            "Или: `/dl <ссылка>`\n\n"
            f"⚠️ Лимит файла в Telegram — {MAX_VIDEO_MB} МБ: большие видео (до {MAX_TOTAL_MB} МБ) "
            f"бот режет на части без потери качества",
            parse_mode="Markdown"
        )
        return

    url = arg[1].strip()
    platform, matched_url = detect_platform(url)

    if not platform:
        platform = "youtube"
        matched_url = url

    # Вызываем функцию напрямую без модификации объекта message
    await handle_media_link(message, custom_url=matched_url, custom_platform=platform, ask_quality=True)

# ── РЕГИСТРАЦИЯ ───────────────────────────────────────────────────────────────

def cleanup_stale_tmp(max_age_s: int = 7200) -> None:
    """Удаляем «осиротевшие» папки tgdl_* (после падений/рестартов), чтобы не забивать диск хостинга."""
    import glob
    now = time.time()
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "tgdl_*")):
        try:
            if os.path.isdir(d) and now - os.path.getmtime(d) > max_age_s:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def register_media_handlers(dp: Dispatcher):
    """Зарегистрировать хендлеры скачивания медиа."""
    from aiogram.filters import Command

    dp.message.register(cmd_dl, Command("dl"))
    dp.callback_query.register(callback_dl_quality, F.data.startswith("dlq:"))
    dp.callback_query.register(callback_dl_cancel, F.data.startswith("dlx:"))
    cleanup_stale_tmp()

    # Перехватываем текстовые сообщения со ссылками
    # Регистрируем ДО общего handle_ai_chat, чтобы ссылки не уходили в AI
    dp.message.register(
        handle_media_link,
        F.text.regexp(
            r"https?://(?:"
            r"(?:www\.|m\.)?(?:youtube\.com|youtu\.be)|"
            r"(?:www\.|vm\.|vt\.)?tiktok\.com|"
            r"(?:www\.)?(?:twitter\.com|x\.com)/\S+/status/|"
            r"(?:www\.|old\.|new\.)?reddit\.com/r/|"
            r"redd\.it/|"
            r"(?:[a-z]{2,3}\.|www\.)?pinterest\.[a-z.]{2,6}/pin/|pin\.it/|"
            r"(?:www\.|m\.)?(?:vk\.com|vk\.ru|vkvideo\.ru)/(?:video|clip)-?\d+_\d+|"
            r"clips\.twitch\.tv/|(?:www\.|m\.)?twitch\.tv/\w+/clip/|"
            r"(?:www\.)?threads\.(?:net|com)/"
            r")"
        )
    )

    logger.info("✅ Media downloader хендлеры зарегистрированы (/dl + ссылки)")
