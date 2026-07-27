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
MAX_VIDEO_MB = 49          # Telegram ограничение — 50 МБ, оставляем запас
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = 120     # секунд на скачивание

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
        "max_filesize": MAX_VIDEO_BYTES,
    }

    if platform == "youtube":
        base.update({
            # Предпочитаем mp4, <=720p, с аудио в одном файле
            "format": (
                "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]"
                "/bestvideo[height<=720]+bestaudio"
                "/best[height<=720]"
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
        if file_size > MAX_VIDEO_BYTES:
            logger.warning(f"Файл слишком большой: {file_size/1024/1024:.1f} МБ (лимит {MAX_VIDEO_MB} МБ)")
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
                    f"Лимит: {MAX_VIDEO_MB} МБ\n\n"
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
            f"⚠️ Лимит: {MAX_VIDEO_MB} МБ (ограничение Telegram)",
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
