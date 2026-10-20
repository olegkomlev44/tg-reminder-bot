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
import hashlib
import json
import logging
import os
import random
import sqlite3
import time
from collections import Counter, deque
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

    base_candidates = [
        os.getenv("DATA_DIR"),
        os.getenv("DB_DIR"),
        "/app/data",
        "/data",
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
        # Пункт 4: гейт — один запрос к IG за раз, FIFO
        self._gate = asyncio.Lock()
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

    def _next_client(self) -> Optional[Client]:
        if not self._clients:
            return None
        self._current = (self._current + 1) % len(self._clients)
        return self._clients[self._current]

    def _current_client(self) -> Optional[Client]:
        if not self._clients:
            return None
        cl = self._clients[self._current % len(self._clients)]
        if cl is None:
            # Слот пустой (failed init или убитый) — ищем живой
            for i, c in enumerate(self._clients):
                if c is not None:
                    self._current = i
                    return c
            return None
        return cl

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

    async def run(self, fn, *args, retries: int | None = None, **kwargs):
        """
        Запустить fn(client, *args, **kwargs) в executor.

        Пункт 4: все запросы идут через общий гейт — по одному, с минимальной
        паузой между стартами. Раньше хендлеры и фоновые задачи могли долбить IG
        параллельно с одного IP.
        Пункт 5: сетевые ошибки (таймаут, обрыв) ретраятся на том же аккаунте
        с экспоненциальным backoff, а не роняют запрос сразу.
        При 429 / LoginRequired переключаем аккаунт и повторяем.

        На разовую ClientError не прыгаем сразу на следующий аккаунт — сначала
        пробуем ещё раз тем же (с backoff): мгновенная ротация по кругу выглядит
        для IG подозрительнее, чем повтор на одном клиенте.
        """
        await self._ensure_init()
        if not self._clients:
            return None

        if retries is None:
            retries = IG_SAME_ACC_RETRIES + IG_NET_RETRIES + 3

        loop = asyncio.get_event_loop()
        last_err = None
        same_acc_failures = 0
        net_failures = 0
        healed = False

        for attempt in range(retries):
            try:
                async with self._gate:
                    # клиента берём ПОСЛЕ получения гейта: пока ждали очередь,
                    # другой запрос мог провести ротацию
                    cl = self._current_client()
                    if cl is None:
                        return None
                    idx = self._current % len(self._clients)

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
                    self._since_dump[idx] = self._since_dump.get(idx, 0) + 1
                    if self._since_dump[idx] >= IG_DUMP_EVERY:
                        self._since_dump[idx] = 0
                        await loop.run_in_executor(None, self._dump_session, idx)
                return result

            except (PleaseWaitFewMinutes, RateLimitError) as e:
                wait = random.uniform(IG_RATE_WAIT_MIN, IG_RATE_WAIT_MAX)
                cur = self._current % len(self._clients)
                self._rate_limited_until[cur] = time.time() + 1800
                self._note_err("rate", str(e))
                logger.warning(
                    f"[pool] 429 на акк #{cur}, "
                    f"ждём {wait:.0f}с перед ротацией... ({e})"
                )
                self._next_client()
                same_acc_failures = 0
                net_failures = 0
                await asyncio.sleep(wait)
                last_err = e

            except LoginRequired as e:
                login = self._accounts[self._current % len(self._accounts)][0]
                self._note_err("login", f"{login}: {e}")
                logger.warning(
                    f"[pool] LoginRequired @{login} — сессия протухла, ротация.\n"
                    f"👉 Обнови куки: /ig_refresh"
                )
                self._next_client()
                same_acc_failures = 0
                net_failures = 0
                last_err = e
                # Алерт только если эта сессия ещё не была помечена мёртвой
                if login not in self._known_dead:
                    self._known_dead.add(login)
                    if _on_session_dead:
                        try:
                            asyncio.ensure_future(_on_session_dead(login))
                        except Exception:
                            pass

            except (UserNotFound, PrivateAccount):
                raise  # не ошибка пула, пробрасываем выше

            except ClientError as e:
                self._note_err("client", str(e))
                same_acc_failures += 1
                if same_acc_failures <= IG_SAME_ACC_RETRIES:
                    wait = 2 * same_acc_failures + random.uniform(0, 2)
                    logger.warning(
                        f"[pool] ClientError на акк #{self._current} "
                        f"(попытка {same_acc_failures}/{IG_SAME_ACC_RETRIES} на этом акке): {e}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.warning(
                        f"[pool] ClientError: акк #{self._current} исчерпал лимит "
                        f"повторов на месте, ротирую: {e}"
                    )
                    self._next_client()
                    same_acc_failures = 0
                    await asyncio.sleep(2)
                last_err = e

            except _NET_ERRORS as e:
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
                    self._next_client()
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
    result = []
    for i, (login, _) in enumerate(_POOL._accounts):
        cl = _POOL._clients[i] if i < len(_POOL._clients) else None
        result.append({
            "login": login,
            "alive": cl is not None,
            "is_current": total > 0 and (i == _POOL._current % total),
            "requests": _POOL._request_counts.get(i, 0),
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
        async with _POOL._gate:
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


async def get_user_info(username: str, force: bool = False) -> dict | None:
    """
    Получить инфо о пользователе Instagram.
    force=True — игнорировать кэш (нужно фоновой проверке подписок).
    """
    username = username.lower().strip("@").strip()
    if not username:
        return None

    if not force:
        cached = _ucache_get(username)
        if cached:
            return cached
        nf = _NOTFOUND_CACHE.get(username)
        if nf and time.time() - nf < _NOTFOUND_TTL:
            return None

    def _fetch(cl: Client):
        user = cl.user_info_by_username(username)
        return _user_to_dict(user)

    try:
        result = await _POOL.run(_fetch)
        if result:
            _ucache_set(username, result)
            _NOTFOUND_CACHE.pop(username, None)
            logger.info(f"✅ get_user_info @{username}: {result.get('followers')} фолловеров")
        return result
    except UserNotFound:
        logger.warning(f"UserNotFound: @{username}")
        _NOTFOUND_CACHE[username] = time.time()
        return None
    except PrivateAccount:
        logger.info(f"PrivateAccount: @{username}")
        info = {
            "id": "", "username": username, "full_name": username,
            "biography": "", "followers": 0, "following": 0, "posts_count": 0,
            "avatar_url": "", "is_private": True, "is_verified": False, "posts": [],
        }
        _ucache_set(username, info)
        return info
    except Exception as e:
        logger.error(f"get_user_info @{username}: {e}")
        return None


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


async def get_posts(username: str, after_cursor: str = "") -> dict:
    """
    Получить посты пользователя.
    after_cursor — числовой offset (строка) или пустая строка для начала.
    """
    info = await get_user_info(username)
    if not info:
        return {"posts": [], "next_cursor": "", "has_more": False}
    if info.get("is_private"):
        return {"posts": [], "next_cursor": "", "has_more": False, "private": True}

    user_id = info.get("id", "")
    offset = int(after_cursor) if after_cursor.isdigit() else 0

    all_posts = _cache_get(username)

    if all_posts is None:
        def _fetch(cl: Client):
            medias = cl.user_medias(int(user_id), amount=33)
            return [_media_to_post(m) for m in medias if m]

        try:
            all_posts = await _POOL.run(_fetch) or []
            all_posts = [p for p in all_posts if p.get("id")]
            if all_posts:
                _cache_set(username, all_posts)
                logger.info(f"📦 get_posts @{username}: {len(all_posts)} в кэш")
        except PrivateAccount:
            return {"posts": [], "next_cursor": "", "has_more": False, "private": True}
        except Exception as e:
            logger.error(f"get_posts @{username}: {e}")
            return {"posts": [], "next_cursor": "", "has_more": False, "user": info}

    chunk    = all_posts[offset: offset + 10]
    has_more = (offset + 10) < len(all_posts)
    next_cur = str(offset + 10) if has_more else ""

    return {
        "posts":        chunk,
        "next_cursor":  next_cur,
        "has_more":     has_more,
        "total_cached": len(all_posts),
        "user":         info,
    }


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
