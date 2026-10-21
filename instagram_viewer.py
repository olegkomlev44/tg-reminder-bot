"""
instagram_viewer.py — доступ к Instagram через instagrapi.

Архитектура:
- Пул аккаунтов-доноров (ротация при 429 / LoginRequired / PleaseWaitFewMinutes)
- Сессии сохраняются в JSON-файлы (повторный login только при необходимости)
- Публичный интерфейс:
    get_user_info(username) → dict | None
    get_stories(username)   → list
    get_posts(username, after_cursor) → dict
    validate_session(login) → bool          ← НОВОЕ
    get_sessions_status()   → list[dict]    ← НОВОЕ
    get_pool_health()       → dict          ← здоровье пула (алерты)
    get_media_by_url(url)   → dict | None   ← пост/рилс по ссылке
  + всё из подписок (SQLite)

Что изменилось в этой версии:
  • приоритетная очередь к IG: запросы пользователей обгоняют фоновые проверки
  • ротация аккаунтов обходит те, что сидят в 429 / мертвы
  • session.json дублируются в SQLite (instagram.db) и восстанавливаются при старте
  • лёгкая проверка постов (get_latest_posts) + учёт media_count
  • отслеживание по pk (смена ника / удаление / приватность)
  • кэш Telegram file_id по shortcode
  • истории по ссылке, хайлайты

Установка:
    pip install instagrapi

Конфигурация (env / .env):
    IG_ACCOUNTS=login1:password1,login2:password2
    IG_SESSION_DIR=/data/ig_sessions
    IG_ADMIN_IDS=123456,789012
    DB_DIR=/data
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import heapq
import itertools
import json
import logging
import os
import random
import sqlite3
import time
from collections import Counter, deque
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── СТАБИЛЬНОСТЬ ПУЛА (настраивается через env, дефолты — безопаснее прежних) ──
# Без своих прокси все аккаунты бьют с одного IP хостинга — единственный рычаг
# снизить риск бана/выброса это (а) не спешить и (б) не дёргать по кругу все
# аккаунты при первой же временной ошибке.
IG_DELAY_MIN          = float(os.getenv("IG_DELAY_MIN", "3"))       # мин. пауза между запросами instagrapi
IG_DELAY_MAX          = float(os.getenv("IG_DELAY_MAX", "8"))       # макс. пауза между запросами instagrapi
IG_SAME_ACC_RETRIES   = int(os.getenv("IG_SAME_ACC_RETRIES", "2"))  # попыток на ТОМ ЖЕ акке при ClientError
IG_RATE_WAIT_MIN      = float(os.getenv("IG_RATE_WAIT_MIN", "20"))  # backoff при 429/PleaseWaitFewMinutes
IG_RATE_WAIT_MAX      = float(os.getenv("IG_RATE_WAIT_MAX", "45"))
# Пункт 4: общий лимитер — запросы к IG идут строго по одному, с паузой между стартами
IG_MIN_INTERVAL       = float(os.getenv("IG_MIN_INTERVAL", "1.5"))   # мин. пауза между стартами запросов
IG_REQ_TIMEOUT        = float(os.getenv("IG_REQ_TIMEOUT", "60"))     # таймаут одного запроса, сек
# Пункт 5: ретраи на сетевых ошибках (таймаут, обрыв, DNS)
IG_NET_RETRIES        = int(os.getenv("IG_NET_RETRIES", "3"))
# Пункт 6: как часто сохранять обновлённые куки сессии на диск (раз в N успешных запросов)
IG_DUMP_EVERY         = int(os.getenv("IG_DUMP_EVERY", "15"))
# Пункт 2: TTL кэша профилей (сек)
IG_USER_CACHE_TTL     = int(os.getenv("IG_USER_CACHE_TTL", "900"))
# Пункт 7: сколько дней хранить записи «уже отправлено»
IG_SENT_KEEP_DAYS     = int(os.getenv("IG_SENT_KEEP_DAYS", "14"))
# Сколько сидит в «карантине» аккаунт, получивший 429 (сек)
IG_RATE_COOLDOWN      = int(os.getenv("IG_RATE_COOLDOWN", "1800"))
# Через сколько пробуем снова аккаунт, которому прилетел LoginRequired (вдруг ложная тревога)
IG_DEAD_RETRY         = int(os.getenv("IG_DEAD_RETRY", "3600"))
# Лёгкая проверка постов: сколько последних постов тянуть в фоне (хватает 3–5)
IG_POSTS_CHECK_AMOUNT = int(os.getenv("IG_POSTS_CHECK_AMOUNT", "5"))
# Если media_count не менялся — посты не тянем, но не реже, чем раз в N сек
# (страховка от «удалили один и выложили другой» — счётчик тот же)
IG_POSTS_FORCE_EVERY  = int(os.getenv("IG_POSTS_FORCE_EVERY", str(6 * 3600)))
# Сколько хранить кэш Telegram file_id
IG_FILE_CACHE_DAYS    = int(os.getenv("IG_FILE_CACHE_DAYS", "60"))

# ── ПРИОРИТЕТЫ ЗАПРОСОВ ───────────────────────────────────────────────────────
# Чем меньше число — тем раньше обслуживается. Пользователь, открывший профиль,
# не должен ждать, пока фоновая проверка переберёт 12 ников.
PRIO_USER = 0
PRIO_BG   = 10
_ig_prio: ContextVar[int] = ContextVar("ig_prio", default=PRIO_USER)


def set_background_priority() -> None:
    """
    Вызвать в начале фоновой задачи (ig_checker_task, health-check):
    все запросы к IG из этой задачи (и её подзадач) станут фоновыми.
    ContextVar живёт в контексте задачи, на хендлеры пользователей не влияет.
    """
    _ig_prio.set(PRIO_BG)

# ── ЗАВИСИМОСТЬ ───────────────────────────────────────────────────────────────
try:
    from instagrapi import Client
    from instagrapi.exceptions import (
        ClientError,
        LoginRequired,
        PleaseWaitFewMinutes,
        PrivateAccount,
        RateLimitError,
        UserNotFound,
    )
    _INSTAGRAPI_OK = True
except ImportError:
    _INSTAGRAPI_OK = False
    logger.error("instagrapi не установлена! pip install instagrapi")


def _unwrap_percent_re() -> None:
    """
    yt-dlp оборачивает urllib3.util.url._PERCENT_RE своим Urllib3PercentREOverride.
    После горячей перезагрузки yt-dlp обёртка могла навеситься дважды, и тогда
    любой requests-запрос падает с "'Urllib3PercentREOverride' object has no
    attribute 'sub'". Снимаем все обёртки — возвращаем оригинальный regex.
    """
    try:
        import urllib3.util.url as _u
        r = _u._PERCENT_RE
        while type(r).__name__ == "Urllib3PercentREOverride":
            r = r.re
        _u._PERCENT_RE = r
    except Exception:
        pass


# Пункт 5: сетевые ошибки, на которых надо ретраить, а не падать
try:
    import requests as _requests
    _NET_ERRORS: tuple = (
        _requests.exceptions.RequestException,
        ConnectionError,
        TimeoutError,   # в py3.11 это же asyncio.TimeoutError
    )
except ImportError:
    _NET_ERRORS = (ConnectionError, TimeoutError)


# ── КОНФИГ ────────────────────────────────────────────────────────────────────

def _parse_accounts() -> list[tuple[str, str]]:
    """Читаем IG_ACCOUNTS=login1:pass1,login2:pass2"""
    raw = os.getenv("IG_ACCOUNTS", "")
    accounts: list[tuple[str, str]] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        login, _, pwd = pair.partition(":")
        if login and pwd:
            accounts.append((login.strip(), pwd.strip()))
    return accounts


def _session_dir() -> Path:
    explicit = os.getenv("IG_SESSION_DIR")
    if explicit:
        p = Path(explicit)
        p.mkdir(parents=True, exist_ok=True)
        logger.info(f"[pool] session_dir = {p}  (IG_SESSION_DIR)")
        return p

    # ВАЖНО: /data (постоянный том BotHost) стоит РАНЬШЕ /app/data — иначе
    # /app/data создаётся в слое контейнера и сессии умирают при каждом деплое.
    # Порядок совпадает с db.py / _db_path().
    base_candidates = [
        os.getenv("DATA_DIR"),
        os.getenv("DB_DIR"),
        "/data",
        "/app/data",
        os.path.dirname(os.path.abspath(__file__)),
    ]
    for base in base_candidates:
        if not base:
            continue
        p = Path(base) / "ig_sessions"
        try:
            p.mkdir(parents=True, exist_ok=True)
            test = p / ".wt"
            test.write_text("ok")
            test.unlink()
            logger.info(f"[pool] session_dir = {p}")
            return p
        except Exception:
            continue

    p = Path("/app/data/ig_sessions")
    p.mkdir(parents=True, exist_ok=True)
    logger.info(f"[pool] session_dir = {p}  (fallback)")
    return p


def _legacy_session_dirs() -> list[Path]:
    """Где сессии могли лежать при старом порядке выбора каталога."""
    here = Path(os.path.dirname(os.path.abspath(__file__)))
    return [Path("/app/data/ig_sessions"), Path("/data/ig_sessions"), here / "ig_sessions"]


# ── ДУБЛИРОВАНИЕ СЕССИЙ В БД ──────────────────────────────────────────────────
# Если диск с session.json стирается при деплое, а instagram.db (лежит в /data)
# остаётся — восстанавливаем файлы из БД. Побеждает более свежая копия.

def _sess_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(IG_DB, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ig_sessions ("
        "login TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)"
    )
    return conn


def _sess_db_save(login: str, text: str) -> None:
    conn = _sess_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ig_sessions (login, data, updated) VALUES (?,?,?)",
            (login, text, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _sess_db_load(login: str) -> tuple[str, float] | None:
    conn = _sess_conn()
    try:
        row = conn.execute(
            "SELECT data, updated FROM ig_sessions WHERE login=?", (login,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0], float(row[1])) if row else None


def _valid_session_text(text: str | None) -> bool:
    if not text:
        return False
    try:
        d = json.loads(text)
        return isinstance(d, dict) and bool(d.get("cookies") or d.get("authorization_data"))
    except Exception:
        return False


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _sync_session_file(login: str) -> None:
    """
    Свести файл сессии и копию в БД перед загрузкой клиента:
      • файла нет, в БД есть            → восстанавливаем файл из БД;
      • файла нет, в БД нет             → ищем в старых каталогах и копируем;
      • оба есть и различаются          → берём более свежий (по mtime / updated);
      • файл есть, в БД нет             → кладём копию в БД.
    """
    path = _session_dir() / f"{login}.json"
    try:
        row = _sess_db_load(login)
    except Exception as e:
        logger.warning(f"[pool] БД сессий недоступна ({login}): {e}")
        row = None

    file_text, file_mtime = None, 0.0
    if path.exists():
        try:
            file_text = path.read_text(encoding="utf-8")
            file_mtime = path.stat().st_mtime
        except OSError:
            file_text = None

    if file_text is None and row is None:
        for d in _legacy_session_dirs():
            old = d / f"{login}.json"
            if old != path and old.exists():
                try:
                    txt = old.read_text(encoding="utf-8")
                    if _valid_session_text(txt):
                        _write_atomic(path, txt)
                        file_text, file_mtime = txt, path.stat().st_mtime
                        logger.info(f"[pool] 📦 сессия {login} перенесена из {old}")
                        break
                except OSError:
                    continue

    try:
        if row:
            db_text, db_ts = row
            if file_text is None or not _valid_session_text(file_text):
                if _valid_session_text(db_text):
                    _write_atomic(path, db_text)
                    logger.info(f"[pool] ♻️ сессия {login} восстановлена из БД")
                return
            if file_text != db_text:
                if file_mtime + 1 < db_ts and _valid_session_text(db_text):
                    _write_atomic(path, db_text)
                    logger.info(f"[pool] ♻️ сессия {login}: в БД копия свежее файла — взял из БД")
                else:
                    _sess_db_save(login, file_text)
            return
        if file_text is not None and _valid_session_text(file_text):
            _sess_db_save(login, file_text)
            logger.info(f"[pool] 💾 сессия {login} продублирована в БД")
    except Exception as e:
        logger.warning(f"[pool] sync сессии {login}: {e}")


def persist_session(login: str) -> bool:
    """Принудительно записать текущий session.json в БД (например после /ig_refresh)."""
    try:
        path = _session_dir() / f"{login}.json"
        txt = path.read_text(encoding="utf-8")
        if not _valid_session_text(txt):
            return False
        _sess_db_save(login, txt)
        return True
    except Exception as e:
        logger.warning(f"[pool] persist_session {login}: {e}")
        return False


# ── ПРИОРИТЕТНЫЙ ГЕЙТ ─────────────────────────────────────────────────────────

class _PriorityGate:
    """
    Мьютекс с приоритетами: пока гейт занят, ожидающие сортируются по
    (приоритет, порядок прихода). Пользовательский запрос обгоняет любой
    фоновый, текущий выполняющийся запрос никто не прерывает.
    """

    def __init__(self) -> None:
        self._locked = False
        self._heap: list = []
        self._seq = itertools.count()

    async def _acquire(self, prio: int) -> None:
        if not self._locked and not self._heap:
            self._locked = True
            return
        fut = asyncio.get_event_loop().create_future()
        heapq.heappush(self._heap, (prio, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            # гейт мог быть уже передан нам, а отмена пришла следом — вернуть
            if fut.done() and not fut.cancelled():
                self._release()
            raise

    def _release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)      # передаём гейт следующему, _locked остаётся True
                return
        self._locked = False

    @contextlib.asynccontextmanager
    async def hold(self, prio: int | None = None):
        await self._acquire(_ig_prio.get() if prio is None else prio)
        try:
            yield
        finally:
            self._release()

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, f in self._heap if not f.done())


# ── CALLBACK ПРИ СМЕРТИ СЕССИИ ────────────────────────────────────────────────
# Устанавливается из instagram_handlers через set_session_dead_callback().
# Так мы избегаем циклического импорта: viewer ничего не знает про handlers.

_on_session_dead: Optional[Callable] = None


def set_session_dead_callback(cb: Callable) -> None:
    """
    Регистрирует async callback(login: str), вызывается при LoginRequired.
    Вызывать из register_ig_handlers().
    """
    global _on_session_dead
    _on_session_dead = cb


# ── POOL КЛИЕНТОВ ─────────────────────────────────────────────────────────────

class _AccountPool:
    """
    Пул instagrapi.Client.
    При ошибках (429, LoginRequired) переключается на следующий аккаунт.
    Новое: reload_client() — безопасная горячая замена клиента после /ig_refresh.
    """

    def __init__(self):
        self._accounts = _parse_accounts()
        self._clients: list[Optional[Client]] = []
        self._current = 0
        self._lock = asyncio.Lock()
        self._initialized = False
        # Счётчик успешных запросов по индексу аккаунта (для статистики)
        self._request_counts: dict[int, int] = {}
        # Уже известные мёртвые сессии — не спамим алертами повторно
        self._known_dead: set[str] = set()
        # Пункт 4: гейт — один запрос к IG за раз, по приоритетам (пользователь > фон)
        self._gate = _PriorityGate()
        # когда аккаунт получил LoginRequired (для повторной пробы через IG_DEAD_RETRY)
        self._dead_ts: dict[str, float] = {}
        self._last_start = 0.0
        # Пункт 6: учёт сохранения сессий
        self._loaded_mtime: dict[str, float] = {}
        self._dump_hash: dict[str, str] = {}
        self._since_dump: dict[int, int] = {}
        # Пункт 10: данные для health-алертов
        self._ok_ts: deque = deque(maxlen=2000)
        self._err_events: deque = deque(maxlen=300)   # (ts, kind, msg)
        self._rate_limited_until: dict[int, float] = {}

    # ── инициализация ────────────────────────────────────────────────────────

    def _make_client(self, login: str, pwd: str) -> Client:
        """
        Загружает клиент из session_dir/{login}.json.
        Пароль здесь не используется — только кукисы из файла.

        Раньше после set_settings() ещё раз руками патчились cookies и
        Authorization-заголовок ("Bearer IGT:2:<sessionid>"). Это лишнее и,
        похоже, вредное: наш session.json уже собран в родном формате
        instagrapi (uuids/cookies/device_settings/authorization_data), и
        set_settings() сам строит корректный Authorization из
        authorization_data. Ручной хедер с сырым sessionid не совпадает с
        реальным форматом instagrapi (там base64 от {ds_user_id, sessionid})
        и мог просто перетирать корректно посчитанный клиентом заголовок —
        то есть часть "случайных" разрывов сессии могла быть самодельной.
        Убрали патч, оставили только set_settings().
        """
        cl = Client()
        cl.delay_range = [IG_DELAY_MIN, IG_DELAY_MAX]
        session_file = _session_dir() / f"{login}.json"

        # Файл мог исчезнуть при деплое — восстановим из БД (и наоборот)
        _sync_session_file(login)

        if not session_file.exists():
            raise FileNotFoundError(
                f"Файл сессии не найден: {session_file}\n"
                f"Создай его через /ig_refresh или положи вручную в {session_file.parent}/"
            )

        try:
            self._loaded_mtime[login] = session_file.stat().st_mtime
        except OSError:
            pass
        settings = json.loads(session_file.read_text())
        cl.set_settings(settings)

        ds_user_id = settings.get("cookies", {}).get("ds_user_id", "")
        logger.info(f"[pool] ✅ загружен: {login}  ds_user_id={ds_user_id}")
        return cl

    def _init_all(self):
        if not _INSTAGRAPI_OK:
            return
        if not self._accounts:
            logger.error("[pool] IG_ACCOUNTS не задан — Instagram недоступен")
            return
        for login, pwd in self._accounts:
            try:
                cl = self._make_client(login, pwd)
                self._clients.append(cl)
                self._request_counts[len(self._clients) - 1] = 0
            except Exception as e:
                logger.error(f"[pool] не удалось загрузить {login}: {e}")
                self._clients.append(None)  # держим слот чтобы индексы совпадали
        self._initialized = True
        live = sum(1 for c in self._clients if c is not None)
        logger.info(f"[pool] готов: {live}/{len(self._accounts)} аккаунтов")

    # ── получение клиента ────────────────────────────────────────────────────

    async def _ensure_init(self):
        if self._initialized:
            return
        async with self._lock:
            if not self._initialized:
                await asyncio.get_event_loop().run_in_executor(None, self._init_all)

    def _usable(self, i: int, now: float | None = None) -> bool:
        """Аккаунт можно использовать: жив, не в 429-карантине, не «мёртвый»."""
        now = now or time.time()
        if i >= len(self._clients) or self._clients[i] is None:
            return False
        if self._rate_limited_until.get(i, 0) > now:
            return False
        if i < len(self._accounts):
            login = self._accounts[i][0]
            if login in self._known_dead and now - self._dead_ts.get(login, 0) < IG_DEAD_RETRY:
                return False
        return True

    def _select_idx(self, prio: int) -> Optional[int]:
        """
        Индекс аккаунта для запроса. Текущий, если он годится; иначе первый
        годный по кругу. Если годных нет: фоновый запрос отменяется (None),
        пользовательский идёт на аккаунт, у которого 429 истекает раньше всех.
        """
        n = len(self._clients)
        if n == 0:
            return None
        now = time.time()
        cur = self._current % n
        if self._usable(cur, now):
            return cur
        for step in range(1, n):
            i = (cur + step) % n
            if self._usable(i, now):
                self._current = i
                return i
        if prio >= PRIO_BG:
            return None
        cands = [i for i in range(n) if self._clients[i] is not None
                 and (i >= len(self._accounts) or self._accounts[i][0] not in self._known_dead)]
        if not cands:
            cands = [i for i in range(n) if self._clients[i] is not None]
        if not cands:
            return None
        best = min(cands, key=lambda k: self._rate_limited_until.get(k, 0))
        self._current = best
        return best

    def _rotate_from(self, cur: int) -> bool:
        """Перейти на следующий годный аккаунт (≠ cur). True — нашли другой."""
        n = len(self._clients)
        if n <= 1:
            return False
        now = time.time()
        for step in range(1, n):
            i = (cur + step) % n
            if self._usable(i, now):
                self._current = i
                return True
        return False

    def _next_client(self) -> Optional[Client]:
        """Совместимость: переключиться на следующий годный и вернуть клиента."""
        if not self._clients:
            return None
        cur = self._current % len(self._clients)
        self._rotate_from(cur)
        return self._clients[self._current % len(self._clients)]

    def _current_client(self) -> Optional[Client]:
        if not self._clients:
            return None
        i = self._select_idx(PRIO_USER)
        return self._clients[i] if i is not None else None

    # ── учёт событий (для health) ────────────────────────────────────────────

    def _note_ok(self) -> None:
        self._ok_ts.append(time.time())

    def _note_err(self, kind: str, msg: str = "") -> None:
        self._err_events.append((time.time(), kind, (msg or "")[:300]))

    # ── сохранение сессий на диск (пункт 6) ──────────────────────────────────

    def _dump_session(self, idx: int, force: bool = False) -> bool:
        """
        Записать актуальные куки/настройки клиента обратно в session.json.
        Не трогаем файл, если его изменили снаружи (например /ig_refresh),
        и не пишем для уже мёртвых сессий — чтобы не затереть свежие куки старыми.
        """
        try:
            if idx >= len(self._clients) or idx >= len(self._accounts):
                return False
            cl = self._clients[idx]
            if cl is None:
                return False
            login = self._accounts[idx][0]
            if login in self._known_dead:
                return False

            path = _session_dir() / f"{login}.json"
            if path.exists() and path.stat().st_mtime > self._loaded_mtime.get(login, 0) + 1:
                logger.info(f"[pool] сессия {login} изменена снаружи — не перезаписываю")
                return False

            data = json.dumps(cl.get_settings(), ensure_ascii=False, indent=2, default=str)
            digest = hashlib.sha1(data.encode("utf-8")).hexdigest()
            if not force and self._dump_hash.get(login) == digest:
                return False

            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, path)
            self._loaded_mtime[login] = path.stat().st_mtime
            self._dump_hash[login] = digest
            try:
                _sess_db_save(login, data)          # дубль в БД на случай стирания диска
            except Exception as e:
                logger.warning(f"[pool] не удалось продублировать сессию {login} в БД: {e}")
            logger.info(f"[pool] 💾 сессия {login} сохранена")
            return True
        except Exception as e:
            logger.warning(f"[pool] не удалось сохранить сессию #{idx}: {e}")
            return False

    def flush_sessions(self) -> None:
        """Синхронно сохранить все сессии (вызывается при выходе)."""
        for i in range(len(self._clients)):
            self._dump_session(i)

    # ── НОВОЕ: безопасный горячий перезапуск ─────────────────────────────────

    async def reload_client(self, login: str) -> tuple[bool, str]:
        """
        Безопасно перезагружает клиент для login:
        1. Создаёт нового клиента из обновлённого session.json
        2. Тестирует — делает реальный запрос к Instagram
        3. Только если тест прошёл — заменяет старый клиент в пуле

        Returns: (success: bool, message: str)
        """
        await self._ensure_init()

        idx = next(
            (i for i, (l, _) in enumerate(self._accounts) if l == login),
            None,
        )
        if idx is None:
            msg = f"{login} не найден в IG_ACCOUNTS"
            logger.warning(f"[pool] reload_client: {msg}")
            return False, msg

        # Создаём клиента
        try:
            new_cl = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._make_client(login, "")
            )
        except Exception as e:
            msg = f"Ошибка загрузки session.json: {e}"
            logger.error(f"[pool] reload_client {login}: {msg}")
            return False, msg

        # Тестируем — реальный запрос к IG
        try:
            def _ping(cl: Client):
                _unwrap_percent_re()
                return cl.user_info_by_username(login)

            await asyncio.get_event_loop().run_in_executor(
                None, lambda: _ping(new_cl)
            )
        except LoginRequired as e:
            msg = f"Куки не работают — LoginRequired: {e}"
            logger.error(f"[pool] reload_client {login}: {msg}")
            return False, msg
        except Exception as e:
            msg = f"Тест не прошёл: {e}"
            logger.error(f"[pool] reload_client {login}: {msg}")
            return False, msg

        # Всё ок — заменяем под блокировкой
        async with self._lock:
            while len(self._clients) <= idx:
                self._clients.append(None)
            self._clients[idx] = new_cl
            self._request_counts[idx] = 0
            self._known_dead.discard(login)  # сессия ожила — сбрасываем флаг

        logger.info(f"[pool] ✅ reload_client {login}: клиент заменён и протестирован")
        return True, "Клиент успешно перезагружен и протестирован"

    # ── выполнение запроса с ротацией ────────────────────────────────────────

    async def run(self, fn, *args, retries: int | None = None, prio: int | None = None, **kwargs):
        """
        Запустить fn(client, *args, **kwargs) в executor.

        Все запросы идут через общий приоритетный гейт — по одному, с минимальной
        паузой между стартами; запросы пользователей обгоняют фоновые.
        Аккаунты в 429 / мёртвые пропускаются при выборе клиента.
        Сетевые ошибки ретраятся на том же аккаунте с backoff, разовая ClientError
        тоже сначала повторяется на месте, а не сразу ротирует.
        Фоновый запрос, когда все аккаунты недоступны, возвращает None сразу —
        не жжёт ретраи и не держит гейт.
        """
        await self._ensure_init()
        if not self._clients:
            return None

        if prio is None:
            prio = _ig_prio.get()
        if retries is None:
            retries = IG_SAME_ACC_RETRIES + IG_NET_RETRIES + 3

        loop = asyncio.get_event_loop()
        last_err = None
        same_acc_failures = 0
        net_failures = 0
        healed = False

        for attempt in range(retries):
            idx: Optional[int] = None
            try:
                async with self._gate.hold(prio):
                    # аккаунт выбираем ПОСЛЕ получения гейта: пока ждали очередь,
                    # другой запрос мог получить 429 и провести ротацию
                    idx = self._select_idx(prio)
                    if idx is None:
                        logger.warning("[pool] нет доступных аккаунтов (все в 429/мертвы) — запрос пропущен")
                        return None
                    cl = self._clients[idx]

                    gap = self._last_start + IG_MIN_INTERVAL - time.monotonic()
                    if gap > 0:
                        await asyncio.sleep(gap)
                    self._last_start = time.monotonic()

                    _unwrap_percent_re()
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: fn(cl, *args, **kwargs)),
                        timeout=IG_REQ_TIMEOUT,
                    )

                    # успех
                    self._request_counts[idx] = self._request_counts.get(idx, 0) + 1
                    self._note_ok()
                    if idx < len(self._accounts):
                        self._known_dead.discard(self._accounts[idx][0])   # сессия доказала, что жива
                    self._since_dump[idx] = self._since_dump.get(idx, 0) + 1
                    if self._since_dump[idx] >= IG_DUMP_EVERY:
                        self._since_dump[idx] = 0
                        await loop.run_in_executor(None, self._dump_session, idx)
                return result

            except (PleaseWaitFewMinutes, RateLimitError) as e:
                cur = idx if idx is not None else self._current % len(self._clients)
                self._rate_limited_until[cur] = time.time() + IG_RATE_COOLDOWN
                self._note_err("rate", str(e))
                switched = self._rotate_from(cur)
                # есть куда уйти — хватит короткой паузы; некуда — ждём как раньше
                wait = random.uniform(2, 4) if switched else random.uniform(IG_RATE_WAIT_MIN, IG_RATE_WAIT_MAX)
                logger.warning(
                    f"[pool] 429 на акк #{cur}, "
                    + (f"ушёл на #{self._current % len(self._clients)}, " if switched else "свободных нет, ")
                    + f"пауза {wait:.0f}с ({e})"
                )
                same_acc_failures = 0
                net_failures = 0
                last_err = e
                await asyncio.sleep(wait)

            except LoginRequired as e:
                cur = idx if idx is not None else self._current % len(self._accounts)
                login = self._accounts[cur][0]
                self._note_err("login", f"{login}: {e}")
                logger.warning(
                    f"[pool] LoginRequired @{login} — сессия протухла, ротация.\n"
                    f"👉 Обнови куки: /ig_refresh"
                )
                first_time = login not in self._known_dead
                self._known_dead.add(login)
                self._dead_ts[login] = time.time()
                self._rotate_from(cur)
                same_acc_failures = 0
                net_failures = 0
                last_err = e
                # Алерт только если эта сессия ещё не была помечена мёртвой
                if first_time and _on_session_dead:
                    try:
                        asyncio.ensure_future(_on_session_dead(login))
                    except Exception:
                        pass

            except (UserNotFound, PrivateAccount):
                raise  # не ошибка пула, пробрасываем выше

            except ClientError as e:
                cur = idx if idx is not None else self._current % len(self._clients)
                self._note_err("client", str(e))
                same_acc_failures += 1
                if same_acc_failures <= IG_SAME_ACC_RETRIES:
                    wait = 2 * same_acc_failures + random.uniform(0, 2)
                    logger.warning(
                        f"[pool] ClientError на акк #{cur} "
                        f"(попытка {same_acc_failures}/{IG_SAME_ACC_RETRIES} на этом акке): {e}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.warning(
                        f"[pool] ClientError: акк #{cur} исчерпал лимит "
                        f"повторов на месте, ротирую: {e}"
                    )
                    self._rotate_from(cur)
                    same_acc_failures = 0
                    await asyncio.sleep(2)
                last_err = e

            except _NET_ERRORS as e:
                cur = idx if idx is not None else self._current % len(self._clients)
                self._note_err("net", f"{type(e).__name__}: {e}")
                net_failures += 1
                if net_failures <= IG_NET_RETRIES:
                    wait = min(2 ** net_failures, 20) + random.uniform(0, 1.5)
                    logger.warning(
                        f"[pool] сеть: {type(e).__name__} "
                        f"(попытка {net_failures}/{IG_NET_RETRIES}), повтор через {wait:.0f}с: {e}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.warning(f"[pool] сеть: лимит повторов исчерпан, ротирую: {e}")
                    self._rotate_from(cur)
                    net_failures = 0
                    await asyncio.sleep(2)
                last_err = e

            except Exception as e:
                # Самолечение: yt-dlp может навесить обёртку на urllib3 повторно
                if "Urllib3PercentREOverride" in str(e) and not healed:
                    healed = True
                    _unwrap_percent_re()
                    logger.warning("[pool] сломан urllib3._PERCENT_RE (yt-dlp) — починили, повторяем")
                    last_err = e
                    continue
                self._note_err("unexpected", f"{type(e).__name__}: {e}")
                logger.error(f"[pool] неожиданная ошибка: {e}", exc_info=True)
                last_err = e
                break

        logger.error(f"[pool] все попытки исчерпаны, последняя ошибка: {last_err}")
        return None


_POOL = _AccountPool()
atexit.register(_POOL.flush_sessions)


# ── СТАТУС И ВАЛИДАЦИЯ СЕССИЙ ─────────────────────────────────────────────────

def get_sessions_status() -> list[dict]:
    """
    Вернуть статус всех аккаунтов пула.
    Используется в health check таске и /ig_status.
    """
    total = len(_POOL._accounts)
    now = time.time()
    result = []
    for i, (login, _) in enumerate(_POOL._accounts):
        cl = _POOL._clients[i] if i < len(_POOL._clients) else None
        result.append({
            "login": login,
            "alive": cl is not None,
            "is_current": total > 0 and (i == _POOL._current % total),
            "requests": _POOL._request_counts.get(i, 0),
            "limited_for": max(0, int(_POOL._rate_limited_until.get(i, 0) - now)),
        })
    return result


def get_pool_health(window: int = 3600) -> dict:
    """
    Пункт 10: сводка по здоровью пула за последние `window` секунд.
    Используется для алертов админам и в /ig_status.
    """
    now = time.time()
    errs = [e for e in _POOL._err_events if now - e[0] <= window]
    counts = Counter(k for _, k, _ in errs)
    ok_n = sum(1 for t in _POOL._ok_ts if now - t <= window)
    last_ok = _POOL._ok_ts[-1] if _POOL._ok_ts else None
    last_err = _POOL._err_events[-1] if _POOL._err_events else None
    return {
        "initialized":  _POOL._initialized,
        "total":        len(_POOL._accounts),
        "live":         sum(1 for c in _POOL._clients if c is not None),
        "rate_limited": sum(1 for t in _POOL._rate_limited_until.values() if t > now),
        "queue":        _POOL._gate.waiting,
        "ok":           ok_n,
        "errors":       dict(counts),
        "last_ok_ago":  (now - last_ok) if last_ok else None,
        "last_error": (
            {"ts": last_err[0], "kind": last_err[1], "msg": last_err[2]}
            if last_err else None
        ),
    }


async def validate_session(login: str) -> bool:
    """
    Проверить что сессия для login работает.
    Делает лёгкий запрос к профилю самого аккаунта.
    Используется в /ig_refresh для подтверждения новых кук.
    """
    idx = next(
        (i for i, (l, _) in enumerate(_POOL._accounts) if l == login),
        None,
    )
    if idx is None:
        return False
    if idx >= len(_POOL._clients) or _POOL._clients[idx] is None:
        return False

    cl = _POOL._clients[idx]

    def _ping(c: Client):
        _unwrap_percent_re()
        return c.user_info_by_username(login)

    try:
        async with _POOL._gate.hold():
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _ping(cl)
            )
        return bool(result)
    except Exception as e:
        logger.warning(f"validate_session {login}: {e}")
        return False


# ── КЭШ ПОСТОВ ────────────────────────────────────────────────────────────────

_POSTS_CACHE: dict = {}
_POSTS_CACHE_TTL = 600  # 10 минут


def _cache_set(username: str, posts: list) -> None:
    _POSTS_CACHE[username] = {"posts": posts, "ts": time.time()}


def _cache_get(username: str) -> list | None:
    e = _POSTS_CACHE.get(username)
    if not e:
        return None
    if time.time() - e["ts"] > _POSTS_CACHE_TTL:
        del _POSTS_CACHE[username]
        return None
    return e["posts"]


# ── КОНВЕРТЕРЫ instagrapi → наш формат ───────────────────────────────────────

def _user_to_dict(user) -> dict:
    """UserShort / UserInfo → наш dict."""
    try:
        followers   = getattr(user, "follower_count", 0) or 0
        following   = getattr(user, "following_count", 0) or 0
        posts_cnt   = getattr(user, "media_count", 0) or 0
        biography   = getattr(user, "biography", "") or ""
        is_private  = getattr(user, "is_private", False)
        is_verified = getattr(user, "is_verified", False)
        full_name   = getattr(user, "full_name", "") or ""
        username    = getattr(user, "username", "") or ""
        pk          = str(getattr(user, "pk", "") or "")

        avatar_url = ""
        hd = getattr(user, "profile_pic_url_hd", None)
        sd = getattr(user, "profile_pic_url", None)
        if hd:
            avatar_url = str(hd)
        elif sd:
            avatar_url = str(sd)

        return {
            "id":          pk,
            "username":    username,
            "full_name":   full_name,
            "biography":   biography,
            "followers":   followers,
            "following":   following,
            "posts_count": posts_cnt,
            "avatar_url":  avatar_url,
            "is_private":  is_private,
            "is_verified": is_verified,
            "posts":       [],
        }
    except Exception as e:
        logger.error(f"_user_to_dict error: {e}")
        return {}


def _media_to_post(media) -> dict:
    """instagrapi Media → наш dict поста."""
    try:
        sc       = getattr(media, "code", "") or str(getattr(media, "pk", ""))
        pid      = str(getattr(media, "pk", sc))
        taken_at = getattr(media, "taken_at", None)
        ts       = int(taken_at.timestamp()) if taken_at else 0
        cap      = getattr(media, "caption_text", "") or ""
        likes    = getattr(media, "like_count", 0) or 0
        comments = getattr(media, "comment_count", 0) or 0

        media_type = getattr(media, "media_type", 1)

        urls: list[dict] = []
        if media_type == 8:  # альбом
            resources = getattr(media, "resources", []) or []
            for r in resources:
                r_type = getattr(r, "media_type", 1)
                if r_type == 2:
                    urls.append({
                        "type":  "video",
                        "url":   str(getattr(r, "video_url", "") or ""),
                        "thumb": str(getattr(r, "thumbnail_url", "") or ""),
                    })
                else:
                    u = str(getattr(r, "thumbnail_url", "") or "")
                    urls.append({"type": "photo", "url": u, "thumb": u})
        elif media_type == 2:  # видео
            vurl  = str(getattr(media, "video_url", "") or "")
            thumb = str(getattr(media, "thumbnail_url", "") or "")
            urls.append({"type": "video", "url": vurl, "thumb": thumb})
        else:  # фото
            u = str(getattr(media, "thumbnail_url", "") or "")
            urls.append({"type": "photo", "url": u, "thumb": u})

        return {
            "id":            pid,
            "shortcode":     sc,
            "timestamp":     ts,
            "caption":       cap[:500],
            "like_count":    likes,
            "comment_count": comments,
            "media":         urls,
        }
    except Exception as e:
        logger.error(f"_media_to_post error: {e}")
        return {}


def _story_to_dict(item) -> dict:
    """StoryItem → наш dict истории."""
    try:
        pk       = str(getattr(item, "pk", "") or "")
        mtype    = getattr(item, "media_type", 1)
        is_video = (mtype == 2)

        if is_video:
            url   = str(getattr(item, "video_url", "") or "")
            thumb = str(getattr(item, "thumbnail_url", "") or "")
            dur   = float(getattr(item, "video_duration", 15) or 15)
        else:
            url   = str(getattr(item, "thumbnail_url", "") or "")
            thumb = url
            dur   = 5.0

        return {
            "id":       pk,
            "type":     "video" if is_video else "photo",
            "url":      url,
            "thumb":    thumb,
            "duration": dur,
        }
    except Exception as e:
        logger.error(f"_story_to_dict error: {e}")
        return {}


# ── ПУБЛИЧНЫЙ API ─────────────────────────────────────────────────────────────

# Пункт 2: кэш профилей. Раньше get_posts / get_stories / get_avatar_bytes и
# проверка подписок каждый раз заново дёргали user_info_by_username —
# на один профиль уходило 2–3 лишних запроса к IG.
_USER_CACHE: dict[str, dict] = {}
_NOTFOUND_CACHE: dict[str, float] = {}
_NOTFOUND_TTL = 300  # 5 минут не долбим несуществующие ники


def _ucache_get(username: str) -> dict | None:
    e = _USER_CACHE.get(username)
    if not e:
        return None
    if time.time() - e["ts"] > IG_USER_CACHE_TTL:
        _USER_CACHE.pop(username, None)
        return None
    return dict(e["info"])


def _ucache_set(username: str, info: dict) -> None:
    if len(_USER_CACHE) > 500:
        oldest = min(_USER_CACHE, key=lambda k: _USER_CACHE[k]["ts"])
        _USER_CACHE.pop(oldest, None)
    _USER_CACHE[username] = {"info": dict(info), "ts": time.time()}


async def get_user_status(username: str, force: bool = False) -> tuple[str, dict | None]:
    """
    Как get_user_info, но различает причины пустого ответа:
      ("ok", info)       — профиль получен (в т.ч. приватный);
      ("notfound", None) — IG ответил, что такого ника нет;
      ("error", None)    — не вышло (429, сеть, пул пуст) — это НЕ «аккаунта нет».
    Нужно фоновой проверке: без этого смена ника и сбой сети выглядят одинаково.
    """
    username = username.lower().strip("@").strip()
    if not username:
        return "notfound", None

    if not force:
        cached = _ucache_get(username)
        if cached:
            return "ok", cached
        nf = _NOTFOUND_CACHE.get(username)
        if nf and time.time() - nf < _NOTFOUND_TTL:
            return "notfound", None

    def _fetch(cl: Client):
        user = cl.user_info_by_username(username)
        return _user_to_dict(user)

    try:
        result = await _POOL.run(_fetch)
    except UserNotFound:
        logger.warning(f"UserNotFound: @{username}")
        _NOTFOUND_CACHE[username] = time.time()
        return "notfound", None
    except PrivateAccount:
        logger.info(f"PrivateAccount: @{username}")
        info = {
            "id": "", "username": username, "full_name": username,
            "biography": "", "followers": 0, "following": 0, "posts_count": 0,
            "avatar_url": "", "is_private": True, "is_verified": False, "posts": [],
        }
        _ucache_set(username, info)
        return "ok", info
    except Exception as e:
        logger.error(f"get_user_info @{username}: {e}")
        return "error", None

    if result:
        _ucache_set(username, result)
        _NOTFOUND_CACHE.pop(username, None)
        logger.info(f"✅ get_user_info @{username}: {result.get('followers')} фолловеров")
        return "ok", result
    return "error", None


async def get_user_info(username: str, force: bool = False) -> dict | None:
    """
    Получить инфо о пользователе Instagram.
    force=True — игнорировать кэш (нужно фоновой проверке подписок).
    """
    _, info = await get_user_status(username, force=force)
    return info


async def get_user_status_by_pk(pk: str) -> tuple[str, dict | None]:
    """
    Профиль по числовому pk (он не меняется при смене ника).
    Статусы те же, что у get_user_status.
    """
    pk = str(pk or "").strip()
    if not pk.isdigit():
        return "error", None

    def _fetch(cl: Client):
        return _user_to_dict(cl.user_info(int(pk)))

    try:
        result = await _POOL.run(_fetch)
    except UserNotFound:
        return "notfound", None
    except PrivateAccount:
        return "error", None
    except Exception as e:
        logger.error(f"get_user_info_by_pk {pk}: {e}")
        return "error", None
    if result and result.get("username"):
        _ucache_set(result["username"].lower(), result)
        return "ok", result
    return "error", None


def forget_user_cache(username: str) -> None:
    """Сбросить кэши по нику (после переименования)."""
    username = username.lower().strip("@")
    _USER_CACHE.pop(username, None)
    _NOTFOUND_CACHE.pop(username, None)
    _POSTS_CACHE.pop(username, None)
    for k in [k for k in _LATEST_CACHE if k[0] == username]:
        _LATEST_CACHE.pop(k, None)


async def get_stories(username: str) -> list:
    """Получить активные истории пользователя."""
    info = await get_user_info(username)
    if not info or info.get("is_private"):
        return []

    user_id = info.get("id", "")
    if not user_id:
        return []

    def _fetch(cl: Client):
        return cl.user_stories(int(user_id))

    try:
        items = await _POOL.run(_fetch)
        if not items:
            return []
        stories = [_story_to_dict(s) for s in items]
        stories = [s for s in stories if s.get("url")]
        logger.info(f"✅ get_stories @{username}: {len(stories)} шт.")
        return stories
    except PrivateAccount:
        return []
    except Exception as e:
        logger.error(f"get_stories @{username}: {e}")
        return []


_POSTS_LOCKS: dict[str, asyncio.Lock] = {}


async def get_posts(username: str, after_cursor: str = "") -> dict:
    """
    Посты пользователя, страница по 10.
    after_cursor — числовой offset (строка) или пустая строка для начала.
    Страницы IG подгружаются по мере листания (user_medias_paginated), а не
    «первые 33 и всё»: кэш хранит уже загруженное и курсор для продолжения.
    """
    username = username.lower().strip("@").strip()
    info = await get_user_info(username)
    if not info:
        return {"posts": [], "next_cursor": "", "has_more": False}
    if info.get("is_private"):
        return {"posts": [], "next_cursor": "", "has_more": False, "private": True}

    user_id = info.get("id", "")
    offset = int(after_cursor) if after_cursor.isdigit() else 0
    need = offset + 10

    lock = _POSTS_LOCKS.setdefault(username, asyncio.Lock())
    async with lock:
        e = _POSTS_CACHE.get(username)
        if e and time.time() - e["ts"] > _POSTS_CACHE_TTL:
            _POSTS_CACHE.pop(username, None)
            e = None
        if e is None:
            e = {"posts": [], "ts": time.time(), "cursor": "", "done": False}
            _POSTS_CACHE[username] = e

        # дотягиваем страницы, пока не хватает постов для запрошенного окна
        while len(e["posts"]) < need and not e["done"]:
            cursor = e["cursor"]
            want = max(10, need - len(e["posts"]))

            def _fetch(cl: Client, _c=cursor, _w=want):
                medias, nxt = cl.user_medias_paginated(int(user_id), amount=_w, end_cursor=_c)
                return [_media_to_post(m) for m in medias if m], nxt

            try:
                res = await _POOL.run(_fetch)
            except PrivateAccount:
                return {"posts": [], "next_cursor": "", "has_more": False, "private": True}
            except Exception as ex:
                logger.error(f"get_posts @{username}: {ex}")
                res = None
            if res is None:                      # сбой — отдаём, что уже есть
                break
            page, nxt = res
            seen = {p["id"] for p in e["posts"]}
            e["posts"] += [p for p in page if p.get("id") and p["id"] not in seen]
            e["cursor"] = nxt or ""
            if not nxt or not page:
                e["done"] = True
            logger.info(f"📦 get_posts @{username}: загружено {len(e['posts'])}"
                        f"{'' if e['done'] else ' (есть ещё)'}")

        all_posts = e["posts"]

    chunk = all_posts[offset: offset + 10]
    has_more = (offset + 10) < len(all_posts) or (not e["done"] and bool(all_posts))
    total = int(info.get("posts_count") or 0)
    if total and (offset + 10) >= total:
        has_more = False                        # IG-счётчик говорит, что дальше пусто
    return {
        "posts":        chunk,
        "next_cursor":  str(offset + 10) if has_more else "",
        "has_more":     has_more,
        "total_cached": total or len(all_posts),
        "user":         info,
    }


# ── ЛЁГКАЯ ПРОВЕРКА ПОСТОВ ────────────────────────────────────────────────────
# Для подписок не нужно 33 поста: хватает нескольких последних. Отдельный
# крошечный кэш, чтобы несколько подписчиков одного ника не дёргали IG повторно.
_LATEST_CACHE: dict[tuple[str, int], tuple[float, list]] = {}
_LATEST_TTL = 120


async def get_latest_posts(username: str, amount: int | None = None) -> list | None:
    """
    Последние `amount` постов (по умолчанию IG_POSTS_CHECK_AMOUNT) для фоновой проверки.
    None — не удалось получить (отличается от [] — «постов нет»).
    Закреплённые посты IG отдаёт первыми, поэтому amount меньше 4–5 брать не стоит.
    """
    username = username.lower().strip("@").strip()
    amount = max(1, amount or IG_POSTS_CHECK_AMOUNT)

    full = _cache_get(username)            # если полный список уже в кэше — он ничего не стоит
    if full is not None:
        return full

    ent = _LATEST_CACHE.get((username, amount))
    if ent and time.time() - ent[0] < _LATEST_TTL:
        return ent[1]

    info = await get_user_info(username)
    if not info or info.get("is_private") or not info.get("id"):
        return None
    user_id = info["id"]

    def _fetch(cl: Client):
        medias = cl.user_medias(int(user_id), amount=amount)
        return [_media_to_post(m) for m in medias if m]

    try:
        posts = await _POOL.run(_fetch)
    except PrivateAccount:
        return None
    except Exception as e:
        logger.error(f"get_latest_posts @{username}: {e}")
        return None
    if posts is None:
        return None
    posts = [p for p in posts if p.get("id")]
    _LATEST_CACHE[(username, amount)] = (time.time(), posts)
    logger.info(f"🪶 get_latest_posts @{username}: {len(posts)} (amount={amount})")
    return posts


# ── АВАТАРКА (байты) ──────────────────────────────────────────────────────────

async def get_avatar_bytes(username: str) -> bytes | None:
    """Скачать аватарку через instagrapi (надёжнее, чем прямой URL)."""
    import tempfile

    username = username.lower().strip("@").strip()
    info = await get_user_info(username)          # из кэша — без лишнего запроса
    pk = (info or {}).get("id", "")

    def _fetch(cl: Client):
        user_pk = int(pk) if pk else cl.user_info_by_username(username).pk
        with tempfile.TemporaryDirectory() as tmpdir:
            path = cl.user_download_profile_pic(user_pk, folder=tmpdir)
            with open(path, "rb") as f:
                return f.read()

    try:
        return await _POOL.run(_fetch)
    except Exception as e:
        logger.error(f"get_avatar_bytes @{username}: {e}")
        return None


# ── ПОСТ / РИЛС ПО ССЫЛКЕ (пункт 9) ───────────────────────────────────────────

async def get_media_by_url(url: str) -> dict | None:
    """
    Достать пост/рилс/IGTV по ссылке.
    Возвращает наш dict поста (как в get_posts) + username и is_reel,
    либо None если не получилось.
    """
    def _fetch(cl: Client):
        pk = cl.media_pk_from_url(url)
        m = cl.media_info(pk)
        post = _media_to_post(m)
        if not post:
            return None
        usr = getattr(m, "user", None)
        post["username"] = getattr(usr, "username", "") or ""
        post["is_reel"] = getattr(m, "product_type", "") == "clips"
        return post

    try:
        return await _POOL.run(_fetch)
    except Exception as e:
        logger.warning(f"get_media_by_url {url[:80]}: {e}")
        return None


# ── ИСТОРИЯ ПО ССЫЛКЕ / ХАЙЛАЙТЫ ──────────────────────────────────────────────

async def get_story_by_url(url: str) -> dict | None:
    """Одна история по ссылке instagram.com/stories/<user>/<id>/ (+username в ответе)."""
    def _fetch(cl: Client):
        pk = cl.story_pk_from_url(url)
        st = cl.story_info(pk)
        d = _story_to_dict(st)
        if not d or not d.get("url"):
            return None
        usr = getattr(st, "user", None)
        d["username"] = getattr(usr, "username", "") or ""
        return d

    try:
        return await _POOL.run(_fetch)
    except Exception as e:
        logger.warning(f"get_story_by_url {url[:80]}: {e}")
        return None


async def get_highlights(username: str) -> list[dict]:
    """Список хайлайтов: [{pk, title, count}]."""
    info = await get_user_info(username)
    if not info or info.get("is_private") or not info.get("id"):
        return []
    uid = info["id"]

    def _fetch(cl: Client):
        out = []
        for h in cl.user_highlights(int(uid)) or []:
            items = getattr(h, "items", None) or []
            cnt = int(getattr(h, "media_count", 0) or len(items) or 0)
            out.append({
                "pk":    str(getattr(h, "pk", "")).replace("highlight:", ""),
                "title": (getattr(h, "title", "") or "—").strip() or "—",
                "count": cnt,
            })
        return out

    try:
        res = await _POOL.run(_fetch)
    except Exception as e:
        logger.warning(f"get_highlights @{username}: {e}")
        return []
    return [h for h in (res or []) if h["pk"]]


async def get_highlight_items(highlight_pk: str) -> dict | None:
    """Содержимое хайлайта: {title, username, stories:[story dict]}."""
    pk = str(highlight_pk).replace("highlight:", "")

    def _fetch(cl: Client):
        h = cl.highlight_info(pk)
        stories = [_story_to_dict(s) for s in (getattr(h, "items", None) or [])]
        stories = [s for s in stories if s.get("url")]
        usr = getattr(h, "user", None)
        return {
            "title":    (getattr(h, "title", "") or "").strip(),
            "username": getattr(usr, "username", "") or "",
            "stories":  stories,
        }

    try:
        res = await _POOL.run(_fetch)
    except Exception as e:
        logger.warning(f"get_highlight_items {pk}: {e}")
        return None
    return res if res and res.get("stories") else None


# ── БД (подписки) ─────────────────────────────────────────────────────────────

def _db_path() -> str:
    for candidate in [
        os.getenv("DB_DIR"), "/data", "/app/data",
        os.path.dirname(os.path.abspath(__file__)),
    ]:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            test = os.path.join(candidate, ".wt")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(candidate, "instagram.db")
        except Exception:
            continue
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "instagram.db")


IG_DB = _db_path()


def init_ig_db():
    conn = sqlite3.connect(IG_DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ig_subscriptions (
        user_id     TEXT NOT NULL,
        ig_username TEXT NOT NULL,
        sub_type    TEXT NOT NULL,
        last_seen   TEXT DEFAULT '',
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, ig_username, sub_type)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ig_sent (
        user_id     TEXT NOT NULL,
        media_id    TEXT NOT NULL,
        sent_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, media_id)
    )""")
    # Пункт 8: когда в последний раз проверяли ник и когда в нём была активность
    c.execute("""CREATE TABLE IF NOT EXISTS ig_check_state (
        ig_username   TEXT PRIMARY KEY,
        last_checked  REAL DEFAULT 0,
        last_activity REAL DEFAULT 0
    )""")
    # Состояние ника по pk: нужно, чтобы пережить смену ника / удаление / приватность
    c.execute("""CREATE TABLE IF NOT EXISTS ig_user_state (
        ig_username      TEXT PRIMARY KEY,
        pk               TEXT DEFAULT '',
        media_count      INTEGER,
        last_posts_fetch REAL DEFAULT 0,
        is_private       INTEGER,
        notfound_count   INTEGER DEFAULT 0,
        is_gone          INTEGER DEFAULT 0
    )""")
    # Кэш Telegram file_id: один и тот же пост/рилс не скачиваем и не заливаем дважды
    c.execute("""CREATE TABLE IF NOT EXISTS ig_file_cache (
        shortcode TEXT PRIMARY KEY,
        username  TEXT DEFAULT '',
        caption   TEXT DEFAULT '',
        items     TEXT NOT NULL,
        total     INTEGER NOT NULL,
        created   REAL NOT NULL
    )""")
    # Фильтры уведомлений (раньше жили в памяти и терялись при рестарте)
    c.execute("""CREATE TABLE IF NOT EXISTS ig_sub_filters (
        user_id     TEXT NOT NULL,
        ig_username TEXT NOT NULL,
        sub_type    TEXT NOT NULL,
        filter      TEXT NOT NULL,
        PRIMARY KEY (user_id, ig_username, sub_type)
    )""")
    # Копии session.json (на случай стирания диска при деплое)
    c.execute("""CREATE TABLE IF NOT EXISTS ig_sessions (
        login   TEXT PRIMARY KEY,
        data    TEXT NOT NULL,
        updated REAL NOT NULL
    )""")
    conn.commit()
    conn.close()
    logger.info("🟢 Instagram DB инициализирована")


def ig_subscribe(user_id: str, ig_username: str, sub_type: str, last_seen: str = "") -> bool:
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ig_subscriptions "
            "(user_id, ig_username, sub_type, last_seen) VALUES (?,?,?,?)",
            (str(user_id), ig_username, sub_type, last_seen),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"ig_subscribe error: {e}")
        return False
    finally:
        conn.close()


def ig_unsubscribe(user_id: str, ig_username: str, sub_type: str) -> bool:
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    conn.execute(
        "DELETE FROM ig_subscriptions WHERE user_id=? AND ig_username=? AND sub_type=?",
        (str(user_id), ig_username, sub_type),
    )
    conn.execute(
        "DELETE FROM ig_sub_filters WHERE user_id=? AND ig_username=? AND sub_type=?",
        (str(user_id), ig_username, sub_type),
    )
    conn.commit()
    conn.close()
    return True


def ig_get_subscriptions(user_id: str) -> list:
    conn = sqlite3.connect(IG_DB)
    rows = conn.execute(
        "SELECT ig_username, sub_type, last_seen FROM ig_subscriptions "
        "WHERE user_id=? ORDER BY ig_username",
        (str(user_id),),
    ).fetchall()
    conn.close()
    return [{"ig_username": r[0], "sub_type": r[1], "last_seen": r[2]} for r in rows]


def ig_is_subscribed(user_id: str, ig_username: str, sub_type: str) -> bool:
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    row = conn.execute(
        "SELECT 1 FROM ig_subscriptions WHERE user_id=? AND ig_username=? AND sub_type=?",
        (str(user_id), ig_username, sub_type),
    ).fetchone()
    conn.close()
    return row is not None


def ig_update_last_seen(user_id: str, ig_username: str, sub_type: str, last_seen: str):
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    conn.execute(
        "UPDATE ig_subscriptions SET last_seen=? "
        "WHERE user_id=? AND ig_username=? AND sub_type=?",
        (last_seen, str(user_id), ig_username, sub_type),
    )
    conn.commit()
    conn.close()


def ig_all_subscriptions() -> list:
    conn = sqlite3.connect(IG_DB)
    rows = conn.execute(
        "SELECT user_id, ig_username, sub_type, last_seen "
        "FROM ig_subscriptions ORDER BY ig_username"
    ).fetchall()
    conn.close()
    return [
        {"user_id": r[0], "ig_username": r[1], "sub_type": r[2], "last_seen": r[3]}
        for r in rows
    ]


def ig_mark_sent(user_id: str, media_id: str):
    conn = sqlite3.connect(IG_DB)
    conn.execute(
        "INSERT OR IGNORE INTO ig_sent (user_id, media_id) VALUES (?,?)",
        (str(user_id), media_id),
    )
    conn.commit()
    conn.close()


def ig_already_sent(user_id: str, media_id: str) -> bool:
    conn = sqlite3.connect(IG_DB)
    row = conn.execute(
        "SELECT 1 FROM ig_sent WHERE user_id=? AND media_id=?",
        (str(user_id), media_id),
    ).fetchone()
    conn.close()
    return row is not None


def ig_mark_sent_many(user_id: str, media_ids: list) -> None:
    """Пометить сразу пачку как отправленные (сидинг подписок)."""
    if not media_ids:
        return
    conn = sqlite3.connect(IG_DB)
    conn.executemany(
        "INSERT OR IGNORE INTO ig_sent (user_id, media_id) VALUES (?,?)",
        [(str(user_id), m) for m in media_ids],
    )
    conn.commit()
    conn.close()


def ig_cleanup_sent(days: int = IG_SENT_KEEP_DAYS) -> int:
    """
    Пункт 7: чистим ig_sent от старых записей (иначе таблица растёт вечно)
    и ig_check_state от ников, на которых уже никто не подписан.
    Безопасно: посты старше IG_POST_MAX_AGE фильтруются по дате и не придут повторно.
    """
    conn = sqlite3.connect(IG_DB)
    cur = conn.execute(
        "DELETE FROM ig_sent WHERE sent_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    n = cur.rowcount
    conn.execute(
        "DELETE FROM ig_check_state WHERE ig_username NOT IN "
        "(SELECT DISTINCT ig_username FROM ig_subscriptions)"
    )
    conn.execute(
        "DELETE FROM ig_user_state WHERE ig_username NOT IN "
        "(SELECT DISTINCT ig_username FROM ig_subscriptions)"
    )
    conn.execute(
        "DELETE FROM ig_file_cache WHERE created < ?",
        (time.time() - IG_FILE_CACHE_DAYS * 86400,),
    )
    conn.commit()
    conn.close()
    if n:
        logger.info(f"🧹 ig_sent: удалено {n} старых записей")
    return n


def ig_get_check_states() -> dict:
    """{ig_username: (last_checked, last_activity)}"""
    conn = sqlite3.connect(IG_DB)
    rows = conn.execute(
        "SELECT ig_username, last_checked, last_activity FROM ig_check_state"
    ).fetchall()
    conn.close()
    return {r[0]: (r[1] or 0.0, r[2] or 0.0) for r in rows}


def ig_set_check_state(ig_username: str, last_checked: float, last_activity: float | None = None):
    """Записать время проверки; last_activity обновляем только если передан."""
    conn = sqlite3.connect(IG_DB)
    conn.execute(
        "INSERT INTO ig_check_state (ig_username, last_checked, last_activity) VALUES (?,?,?) "
        "ON CONFLICT(ig_username) DO UPDATE SET last_checked=excluded.last_checked, "
        "last_activity=CASE WHEN ? IS NULL THEN ig_check_state.last_activity ELSE ? END",
        (ig_username, last_checked,
         last_activity if last_activity is not None else last_checked,
         last_activity, last_activity),
    )
    conn.commit()
    conn.close()


# ── СОСТОЯНИЕ НИКА ПО pk (смена ника, удаление, приватность, media_count) ─────

_USER_STATE_FIELDS = {
    "pk", "media_count", "last_posts_fetch", "is_private", "notfound_count", "is_gone",
}


def _state_row(r) -> dict:
    return {
        "ig_username":      r[0],
        "pk":               r[1] or "",
        "media_count":      r[2],
        "last_posts_fetch": r[3] or 0.0,
        "is_private":       None if r[4] is None else bool(r[4]),
        "notfound_count":   r[5] or 0,
        "is_gone":          bool(r[6]),
    }


_STATE_COLS = "ig_username, pk, media_count, last_posts_fetch, is_private, notfound_count, is_gone"


def ig_get_user_state(ig_username: str) -> dict | None:
    conn = sqlite3.connect(IG_DB)
    r = conn.execute(
        f"SELECT {_STATE_COLS} FROM ig_user_state WHERE ig_username=?", (ig_username,)
    ).fetchone()
    conn.close()
    return _state_row(r) if r else None


def ig_get_all_user_states() -> dict[str, dict]:
    conn = sqlite3.connect(IG_DB)
    rows = conn.execute(f"SELECT {_STATE_COLS} FROM ig_user_state").fetchall()
    conn.close()
    return {r[0]: _state_row(r) for r in rows}


def ig_set_user_state(ig_username: str, **fields) -> None:
    """Частичное обновление состояния ника (upsert)."""
    fields = {k: v for k, v in fields.items() if k in _USER_STATE_FIELDS}
    if "is_private" in fields and fields["is_private"] is not None:
        fields["is_private"] = int(bool(fields["is_private"]))
    if "is_gone" in fields:
        fields["is_gone"] = int(bool(fields["is_gone"]))
    conn = sqlite3.connect(IG_DB)
    try:
        conn.execute("INSERT OR IGNORE INTO ig_user_state (ig_username) VALUES (?)", (ig_username,))
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE ig_user_state SET {sets} WHERE ig_username=?",
                (*fields.values(), ig_username),
            )
        conn.commit()
    finally:
        conn.close()


def ig_rename_username(old: str, new: str) -> list[str]:
    """
    Перенести все подписки со старого ника на новый (человек сменил ник).
    Возвращает user_id подписчиков, которых это касается.
    Если у кого-то уже есть подписка того же типа на новый ник — дубликат просто удаляется.
    """
    old, new = old.lower().strip("@"), new.lower().strip("@")
    if not old or not new or old == new:
        return []
    conn = sqlite3.connect(IG_DB)
    try:
        rows = conn.execute(
            "SELECT user_id, sub_type, last_seen FROM ig_subscriptions WHERE ig_username=?",
            (old,),
        ).fetchall()
        for uid, st, ls in rows:
            conn.execute(
                "INSERT OR IGNORE INTO ig_subscriptions (user_id, ig_username, sub_type, last_seen) "
                "VALUES (?,?,?,?)", (uid, new, st, ls),
            )
        conn.execute("DELETE FROM ig_subscriptions WHERE ig_username=?", (old,))
        conn.execute(
            "INSERT OR IGNORE INTO ig_sub_filters (user_id, ig_username, sub_type, filter) "
            "SELECT user_id, ?, sub_type, filter FROM ig_sub_filters WHERE ig_username=?", (new, old))
        conn.execute("DELETE FROM ig_sub_filters WHERE ig_username=?", (old,))

        st = conn.execute(
            f"SELECT {_STATE_COLS} FROM ig_user_state WHERE ig_username=?", (old,)
        ).fetchone()
        conn.execute("DELETE FROM ig_user_state WHERE ig_username=?", (old,))
        if st:
            conn.execute(
                f"INSERT OR REPLACE INTO ig_user_state ({_STATE_COLS}) VALUES (?,?,?,?,?,?,?)",
                (new, st[1], st[2], st[3], st[4], 0, 0),
            )
        conn.execute("DELETE FROM ig_check_state WHERE ig_username=?", (new,))
        conn.execute("UPDATE ig_check_state SET ig_username=? WHERE ig_username=?", (new, old))
        conn.commit()
        affected = sorted({r[0] for r in rows})
    finally:
        conn.close()
    forget_user_cache(old)
    return affected


# ── КЭШ TELEGRAM file_id ПО shortcode ─────────────────────────────────────────

def ig_cache_get(shortcode: str) -> dict | None:
    """{username, caption, items:[{kind,file_id}], total, created} или None."""
    if not shortcode:
        return None
    conn = sqlite3.connect(IG_DB)
    try:
        r = conn.execute(
            "SELECT username, caption, items, total, created FROM ig_file_cache WHERE shortcode=?",
            (shortcode,),
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    if time.time() - (r[4] or 0) > IG_FILE_CACHE_DAYS * 86400:
        return None
    try:
        items = json.loads(r[2])
    except Exception:
        return None
    return {"username": r[0], "caption": r[1], "items": items, "total": r[3], "created": r[4]}


def ig_cache_put(shortcode: str, username: str, caption: str, items: list[dict], total: int) -> None:
    """Сохранить file_id'ы отправленного поста. Пишем только полные наборы (len(items)==total)."""
    if not shortcode or not items or len(items) != total:
        return
    conn = sqlite3.connect(IG_DB)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ig_file_cache (shortcode, username, caption, items, total, created) "
            "VALUES (?,?,?,?,?,?)",
            (shortcode, username or "", caption or "", json.dumps(items), total, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def ig_cache_drop(shortcode: str) -> None:
    conn = sqlite3.connect(IG_DB)
    try:
        conn.execute("DELETE FROM ig_file_cache WHERE shortcode=?", (shortcode,))
        conn.commit()
    finally:
        conn.close()


# ── ФИЛЬТРЫ УВЕДОМЛЕНИЙ (персистентные) ───────────────────────────────────────

def ig_get_filter(user_id: str, ig_username: str, sub_type: str) -> str:
    conn = sqlite3.connect(IG_DB)
    try:
        r = conn.execute(
            "SELECT filter FROM ig_sub_filters WHERE user_id=? AND ig_username=? AND sub_type=?",
            (str(user_id), ig_username.lower().strip("@"), sub_type),
        ).fetchone()
    finally:
        conn.close()
    return r[0] if r else "all"


def ig_set_filter(user_id: str, ig_username: str, sub_type: str, f: str) -> None:
    ig_username = ig_username.lower().strip("@")
    conn = sqlite3.connect(IG_DB)
    try:
        if f in ("", "all"):
            conn.execute(
                "DELETE FROM ig_sub_filters WHERE user_id=? AND ig_username=? AND sub_type=?",
                (str(user_id), ig_username, sub_type))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO ig_sub_filters (user_id, ig_username, sub_type, filter) "
                "VALUES (?,?,?,?)", (str(user_id), ig_username, sub_type, f))
        conn.commit()
    finally:
        conn.close()
