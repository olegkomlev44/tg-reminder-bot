"""
media_downloader.py — скачивание медиа из YouTube, TikTok, Twitter/X, Reddit.

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
import tempfile
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import BufferedInputFile, FSInputFile

logger = logging.getLogger(__name__)

# ── ЛИМИТЫ ────────────────────────────────────────────────────────────────────
MAX_VIDEO_MB = 50          # Telegram ограничение — 50 МБ, оставляем запас
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = int(os.getenv("MEDIA_DOWNLOAD_TIMEOUT", "900"))   # секунд на скачивание

# Большие видео: качаем целиком в хорошем качестве и режем на части ≤ лимита Telegram (без пережатия).
MAX_TOTAL_MB = int(os.getenv("MEDIA_MAX_TOTAL_MB", "1000"))          # максимум исходного файла
MAX_TOTAL_BYTES = MAX_TOTAL_MB * 1024 * 1024
MAX_PARTS = int(os.getenv("MEDIA_MAX_PARTS", "15"))                 # максимум частей
PART_TARGET_BYTES = 42 * 1024 * 1024                                # целевой размер части
PART_HARD_BYTES = 49 * 1024 * 1024                                  # больше этого Telegram не примет
MAX_HEIGHT = int(os.getenv("MEDIA_MAX_HEIGHT", "1080"))             # потолок качества YouTube

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
}

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "tiktok":  "TikTok",
    "twitter": "𝕏 (Twitter)",
    "reddit":  "Reddit",
}

PLATFORM_EMOJIS = {
    "youtube": "▶️",
    "tiktok":  "🎵",
    "twitter": "🐦",
    "reddit":  "🤖",
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

def _ydl_opts(out_path: str, platform: str) -> dict:
    """Настройки yt-dlp под каждую платформу."""

    # Базовые настройки — берём лучшее видео до 720p
    base = {
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "max_filesize": MAX_TOTAL_BYTES,
    }

    if platform == "youtube":
        base.update({
            # Предпочитаем mp4, <=720p, с аудио в одном файле
            "format": (
                f"bestvideo[ext=mp4][height<={MAX_HEIGHT}]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={MAX_HEIGHT}]+bestaudio"
                f"/best[height<={MAX_HEIGHT}]"
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

    return base


async def _download_media(url: str, platform: str) -> tuple[str | None, dict]:
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

    opts = _ydl_opts(out_template, platform)

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
            "_tmp_dir":  tmp_dir,
            "_size":     file_size,
        }

        return found, info

    except asyncio.TimeoutError:
        logger.error(f"Таймаут скачивания {url}")
        return None, {"_timeout": True}
    except Exception as e:
        err_str = str(e)
        logger.error(f"yt-dlp error {url}: {e}")

        # Разбираем типичные ошибки
        if "private" in err_str.lower() or "login" in err_str.lower():
            return None, {"_private": True}
        if "not available" in err_str.lower() or "unavailable" in err_str.lower():
            return None, {"_unavailable": True}
        if "no video formats" in err_str.lower() or "no suitable" in err_str.lower():
            return None, {"_no_video": True}

        return None, {"_error": err_str[:200]}


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


async def _split_video(path: str, tmp_dir: str, duration: float = 0) -> list[str] | None:
    """
    Нарезать видео на части ≤ ~49 МБ БЕЗ пережатия (-c copy): качество то же, что в исходнике.
    Резка идёт по ключевым кадрам, поэтому размеры частей «плавают» — при перелёте уменьшаем шаг и повторяем.
    None — не вышло (нет ffmpeg / слишком много частей).
    """
    import glob
    size = os.path.getsize(path)
    duration = duration or await _probe_duration(path)
    if not duration:
        return None
    if size / PART_TARGET_BYTES > MAX_PARTS:
        return None
    seg = max(5, int(duration * PART_TARGET_BYTES / size))
    for _ in range(4):
        for old in glob.glob(os.path.join(tmp_dir, "part_*.mp4")):
            os.remove(old)
        pattern = os.path.join(tmp_dir, "part_%03d.mp4")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", path,
               "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
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
        parts = sorted(glob.glob(os.path.join(tmp_dir, "part_*.mp4")))
        if not parts:
            return None
        if all(os.path.getsize(x) <= PART_HARD_BYTES for x in parts):
            return parts if len(parts) <= MAX_PARTS else None
        seg = max(3, int(seg * 0.75))
    return None


async def _send_video_parts(message: types.Message, parts: list[str], caption: str,
                            width: int, height: int) -> None:
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

# Семафор — не грузим yt-dlp параллельно
_DL_SEMAPHORE = asyncio.Semaphore(3)

async def handle_media_link(message: types.Message, custom_url: str | None = None, custom_platform: str | None = None):
    """
    Основной хендлер: пользователь кинул ссылку →
    определяем платформу → скачиваем → отправляем.
    """
    text = custom_url or message.text or message.caption or ""
    
    if custom_platform and custom_url:
        platform, url = custom_platform, custom_url
    else:
        platform, url = detect_platform(text)

    if not platform or not url:
        return  # не наша ссылка — пропускаем

    emoji = PLATFORM_EMOJIS[platform]
    label = PLATFORM_LABELS[platform]

    # Статусное сообщение
    status = await message.answer(
        f"{emoji} Скачиваю с {label}...\n"
        f"`{url[:60]}{'...' if len(url) > 60 else ''}`",
        parse_mode="Markdown"
    )

    async with _DL_SEMAPHORE:
        filepath, info = await _download_media(url, platform)

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
        caption_parts.append(f"{emoji} *{uploader}*")
    if title:
        caption_parts.append(f"_{title}_")
    dur_str = _fmt_duration(duration)
    if dur_str:
        caption_parts.append(f"⏱ {dur_str}")
    caption_parts.append(f"🔗 {label}")
    caption = "\n".join(caption_parts)[:1000]

    try:
        await status.edit_text(f"{emoji} Загружаю в Telegram...", parse_mode="Markdown")
    except Exception:
        pass

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
            await _send_video_parts(message, parts, caption, width, height)
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
            "🤖 Reddit (видео и гиф)\n\n"
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
    await handle_media_link(message, custom_url=matched_url, custom_platform=platform)

# ── РЕГИСТРАЦИЯ ───────────────────────────────────────────────────────────────

def register_media_handlers(dp: Dispatcher):
    """Зарегистрировать хендлеры скачивания медиа."""
    from aiogram.filters import Command

    dp.message.register(cmd_dl, Command("dl"))

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
            r"redd\.it/"
            r")"
        )
    )

    logger.info("✅ Media downloader хендлеры зарегистрированы (/dl + ссылки)")
