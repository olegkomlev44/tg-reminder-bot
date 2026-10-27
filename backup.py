"""
Ежедневный бэкап БД бота в Telegram-чат админу.

Что делает:
  • находит все *.db рядом с music.db и instagram.db (на BotHost это постоянный том /data);
  • снимает консистентный снимок через sqlite3 backup API (бот при этом не останавливается);
  • по умолчанию ВЫРЕЗАЕТ из снимка таблицу ig_sessions (куки донор-аккаунтов): Telegram-чат —
    не место для сессий Instagram. Включить обратно: BACKUP_INCLUDE_SESSIONS=1;
  • упаковывает всё в zip (+ README с инструкцией восстановления) и шлёт админам документом;
    если архив больше BACKUP_PART_MB — режет на части.

Переменные окружения:
  BACKUP_ADMIN_IDS          кому слать (по умолчанию берётся IG_ADMIN_IDS). Пусто → бэкап выключен.
  BACKUP_EVERY_H=24         период, часов
  BACKUP_INCLUDE_SESSIONS=0 класть ли куки доноров в бэкап
  BACKUP_PART_MB=45         размер части (лимит Telegram для ботов — 50 МБ)

Подключение (main.py):
  from backup import register_backup_handlers, backup_task
  register_backup_handlers(dp)                 # команда /backup
  asyncio.create_task(backup_task(bot))        # расписание
"""
import asyncio
import glob
import json
import logging
import os
import sqlite3
import tempfile
import time
import zipfile

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)

BACKUP_EVERY_H = float(os.getenv("BACKUP_EVERY_H", "24"))
BACKUP_INCLUDE_SESSIONS = os.getenv("BACKUP_INCLUDE_SESSIONS", "0") == "1"
BACKUP_PART_MB = float(os.getenv("BACKUP_PART_MB", "45"))

_LOCK = asyncio.Lock()
_FAIL_NOTICE = {"ts": 0.0}


def _admin_ids() -> set[int]:
    raw = os.getenv("BACKUP_ADMIN_IDS") or os.getenv("IG_ADMIN_IDS", "")
    return {int(x) for x in (p.strip() for p in raw.split(",")) if x.isdigit()}


def _db_files() -> list[str]:
    """Все *.db рядом с БД бота и инстаграма (обычно один каталог — /data)."""
    dirs: set[str] = set()
    for mod, attr in (("db", "DB_PATH"), ("instagram_viewer", "IG_DB")):
        try:
            m = __import__(mod)
            dirs.add(os.path.dirname(os.path.abspath(getattr(m, attr))))
        except Exception as e:
            logger.debug(f"backup: не взял {mod}.{attr}: {e}")
    files: list[str] = []
    for d in dirs:
        files += [f for f in glob.glob(os.path.join(d, "*.db")) if os.path.isfile(f)]
    return sorted(set(files))


def _state_path() -> str:
    files = _db_files()
    base = os.path.dirname(files[0]) if files else os.getcwd()
    return os.path.join(base, "backup_state.json")


def _load_last() -> float:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return float(json.load(f).get("last_ok", 0))
    except Exception:
        return 0.0


def _save_last(ts: float) -> None:
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump({"last_ok": ts}, f)
    except Exception as e:
        logger.debug(f"backup: не записал состояние: {e}")


def _snapshot(src_path: str, dst_path: str) -> None:
    """Консистентная копия живой SQLite-БД (WAL-безопасно)."""
    src = sqlite3.connect(src_path, timeout=30)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
            if not BACKUP_INCLUDE_SESSIONS:
                has = dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ig_sessions'").fetchone()
                if has:
                    dst.execute("DELETE FROM ig_sessions")
                    dst.commit()
                    dst.execute("VACUUM")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _build_archive(workdir: str) -> tuple[str, list[tuple[str, int]]]:
    """Возвращает (путь к zip, [(имя, размер_БД)])."""
    stamp = time.strftime("%Y-%m-%d_%H%M")
    zpath = os.path.join(workdir, f"bot_backup_{stamp}.zip")
    info: list[tuple[str, int]] = []
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in _db_files():
            name = os.path.basename(f)
            tmp = os.path.join(workdir, name)
            _snapshot(f, tmp)
            info.append((name, os.path.getsize(tmp)))
            z.write(tmp, name)
            os.remove(tmp)
        z.writestr("README.txt", (
            "Бэкап БД бота. Как восстановить:\n"
            "1) Останови бота.\n"
            "2) Распакуй *.db в каталог с базами (на BotHost — постоянный том /data), заменив файлы.\n"
            "3) Удали рядом файлы *.db-wal и *.db-shm, если остались.\n"
            "4) Запусти бота.\n\n"
            + ("Куки Instagram-доноров ВКЛЮЧЕНЫ в этот бэкап.\n" if BACKUP_INCLUDE_SESSIONS else
               "Куки Instagram-доноров В БЭКАП НЕ ВХОДЯТ (таблица ig_sessions очищена) — после\n"
               "восстановления обнови их через /ig_refresh.\n")
            + "Если архив пришёл частями (.part001, .part002…): cat файл.zip.part* > файл.zip\n"
        ))
    return zpath, info


def _split(path: str, part_bytes: int) -> list[str]:
    if os.path.getsize(path) <= part_bytes:
        return [path]
    parts = []
    with open(path, "rb") as f:
        i = 1
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


def _fmt_size(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} МБ" if n >= 1024 * 1024 else f"{n / 1024:.0f} КБ"


async def run_backup(bot: Bot, reason: str = "auto", only_to: int | None = None) -> bool:
    """Сделать бэкап и отправить админам (или одному only_to). True — если ушло."""
    admins = {only_to} if only_to else _admin_ids()
    if not admins:
        logger.warning("backup: нет получателей (задай BACKUP_ADMIN_IDS или IG_ADMIN_IDS)")
        return False
    if not _db_files():
        logger.warning("backup: не нашёл ни одного *.db")
        return False
    async with _LOCK:
        loop = asyncio.get_event_loop()
        with tempfile.TemporaryDirectory(prefix="botbackup_") as tmp:
            zpath, info = await loop.run_in_executor(None, _build_archive, tmp)
            total = os.path.getsize(zpath)
            parts = await loop.run_in_executor(None, _split, zpath, int(BACKUP_PART_MB * 1024 * 1024))
            files_txt = "\n".join(f"• {n} — {_fmt_size(sz)}" for n, sz in info)
            head = (f"💾 <b>Бэкап БД</b> ({'по запросу' if reason == 'manual' else 'авто'}) · "
                    f"{time.strftime('%d.%m.%Y %H:%M')}\n{files_txt}\n"
                    f"Архив: {_fmt_size(total)}" + (f" · частей: {len(parts)}" if len(parts) > 1 else "") + "\n"
                    + ("⚠️ Куки доноров внутри." if BACKUP_INCLUDE_SESSIONS
                       else "🔒 Куки Instagram-доноров в бэкап не входят."))
            sent_any = False
            for uid in admins:
                try:
                    for i, pp in enumerate(parts):
                        await bot.send_document(uid, FSInputFile(pp), caption=head if i == 0 else None,
                                                parse_mode="HTML", disable_notification=True)
                    sent_any = True
                except Exception as e:
                    logger.warning(f"backup: не отправил {uid}: {e}")
        if sent_any:
            _save_last(time.time())
            logger.info(f"💾 backup отправлен ({_fmt_size(total)}, файлов БД: {len(info)})")
        return sent_any


async def backup_task(bot: Bot):
    """Фоновая задача: раз в BACKUP_EVERY_H часов. Время последнего успеха хранится на диске."""
    await asyncio.sleep(300)           # не мешаем старту
    while True:
        try:
            if _admin_ids() and time.time() - _load_last() >= BACKUP_EVERY_H * 3600:
                ok = await run_backup(bot, "auto")
                if not ok and time.time() - _FAIL_NOTICE["ts"] > 6 * 3600:
                    _FAIL_NOTICE["ts"] = time.time()
                    for uid in _admin_ids():
                        try:
                            await bot.send_message(uid, "⚠️ Авто-бэкап БД не удался — подробности в логах бота.")
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"backup_task: {e}", exc_info=True)
        await asyncio.sleep(600)


async def cmd_backup(message: types.Message):
    admins = _admin_ids()
    if not admins:
        await message.answer("⚙️ Бэкап выключен: задай переменную BACKUP_ADMIN_IDS (или IG_ADMIN_IDS).")
        return
    if message.from_user.id not in admins:
        await message.answer("⛔ Команда доступна только администратору.")
        return
    wait = await message.answer("⏳ Делаю бэкап...")
    ok = False
    try:
        ok = await run_backup(message.bot, "manual", only_to=message.from_user.id)
    except Exception as e:
        logger.error(f"/backup: {e}", exc_info=True)
    try:
        await wait.delete()
    except Exception:
        pass
    if not ok:
        await message.answer("❌ Бэкап не получился — смотри логи.")


def register_backup_handlers(dp: Dispatcher):
    dp.message.register(cmd_backup, Command("backup"))
