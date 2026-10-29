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
IG_DELAY_MIN          = float(os.getenv("IG_DELAY_MIN", "4"))       # мин. пауза между запросами instagrapi
IG_DELAY_MAX          = float(os.getenv("IG_DELAY_MAX", "9"))       # макс. пауза между запросами instagrapi
IG_SAME_ACC_RETRIES   = int(os.getenv("IG_SAME_ACC_RETRIES", "2"))  # попыток на ТОМ ЖЕ акке при ClientError
IG_RATE_WAIT_MIN      = float(os.getenv("IG_RATE_WAIT_MIN", "20"))  # backoff при 429/PleaseWaitFewMinutes
IG_RATE_WAIT_MAX      = float(os.getenv("IG_RATE_WAIT_MAX", "45"))
# Пункт 4: общий лимитер — запросы к IG идут строго по одному, с паузой между стартами
IG_MIN_INTERVAL       = float(os.getenv("IG_MIN_INTERVAL", "2"))     # мин. пауза между стартами запросов (пользователь)
# Фоновые задачи (проверка подписок, health-check) идут заметно медленнее пользователя
IG_BG_MIN_INTERVAL    = float(os.getenv("IG_BG_MIN_INTERVAL", "4"))
IG_BG_DELAY_MIN       = float(os.getenv("IG_BG_DELAY_MIN", "8"))
IG_BG_DELAY_MAX       = float(os.getenv("IG_BG_DELAY_MAX", "20"))
IG_REQ_TIMEOUT        = float(os.getenv("IG_REQ_TIMEOUT", "60"))     # таймаут одного запроса, сек
# Пункт 5: ретраи на сетевых ошибках (таймаут, обрыв, DNS)
IG_NET_RETRIES        = int(os.getenv("IG_NET_RETRIES", "3"))
# Пункт 6: как часто сохранять обновлённые куки сессии на диск (раз в N успешных запросов)
IG_DUMP_EVERY         = int(os.getenv("IG_DUMP_EVERY", "15"))
# Пункт 2: TTL кэша профилей (сек)
IG_USER_CACHE_TTL     = int(os.getenv("IG_USER_CACHE_TTL", "3600"))
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
IG_POSTS_FORCE_EVERY  = int(os.getenv("IG_POSTS_FORCE_EVERY", str(12 * 3600)))
# Устаревшая карточка: сколько дней держим копию профиля в БД, чтобы показать её,
# когда Instagram не отвечает
IG_STALE_MAX_DAYS     = int(os.getenv("IG_STALE_MAX_DAYS", "30"))
# «Показать сразу, обновить в фоне»: карточку из кэша старше IG_USER_CACHE_TTL, но моложе
# этого возраста, отдаём мгновенно, а свежие цифры подтягиваем фоном и правим сообщение
IG_SWR_MAX_AGE        = int(os.getenv("IG_SWR_MAX_AGE", str(6 * 3600)))
# Эскалация кэша «не найден»: 1-й раз, 2-й раз, 3-й и дальше (сек)
IG_NOTFOUND_TTLS      = (300, 3600, 86400)
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


# ── РУЧНОЕ ДОБАВЛЕНИЕ / УДАЛЕНИЕ ДОНОРОВ ──────────────────────────────────────
# Аккаунты из IG_ACCOUNTS остаются основой. Поверх них в БД хранится:
#   state='added'   — аккаунт, добавленный из бота (только куки, без пароля);
#   state='removed' — аккаунт, удалённый из бота (даже если он есть в IG_ACCOUNTS,
#                     после рестарта он не вернётся).

def norm_login(raw: str) -> str:
    """Логин Instagram в нижнем регистре или '' если формат неверный."""
    import re as _re
    v = (raw or "").strip().lstrip("@").lower()
    return v if _re.fullmatch(r"[a-z0-9._]{1,30}", v) else ""


def _pool_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(IG_DB, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS ig_pool_accounts ("
                 "login TEXT PRIMARY KEY, state TEXT NOT NULL, ts REAL NOT NULL DEFAULT 0)")
    return conn


def _pool_acc_rows() -> dict[str, str]:
    conn = _pool_conn()
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT login, state FROM ig_pool_accounts")}
    finally:
        conn.close()


def _pool_acc_set(login: str, state: str) -> None:
    conn = _pool_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO ig_pool_accounts (login, state, ts) VALUES (?,?,?)",
                     (login, state, time.time()))
        conn.commit()
    finally:
        conn.close()


def _merge_accounts(base: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """IG_ACCOUNTS минус удалённые из бота плюс добавленные из бота."""
    try:
        rows = _pool_acc_rows()
    except Exception as e:
        logger.warning(f"[pool] не прочитал ig_pool_accounts: {e}")
        return base
    out = [(l, p) for l, p in base if rows.get(l) != "removed"]
    have = {l for l, _ in out}
    for l, st in rows.items():
        if st == "added" and l not in have:
            out.append((l, ""))
    return out


def _purge_account_data(login: str) -> None:
    """Стереть сессию удалённого донора и его следы в БД (лайки, подписки, паузы)."""
    d = _session_dir()
    for suffix in (".json", ".json.bak", ".json.tmp"):
        try:
            (d / f"{login}{suffix}").unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"[pool] не удалил {login}{suffix}: {e}")
    for table in ("ig_sessions", "ig_follows", "ig_likes", "ig_action_blocks", "ig_proxy", "ig_acc_first_seen"):
        try:
            conn = sqlite3.connect(IG_DB, timeout=10)
            try:
                conn.execute(f"DELETE FROM {table} WHERE login=?", (login,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass        # таблицы может ещё не быть — не страшно


# ── ПРОКСИ НА АККАУНТ (1 аккаунт = 1 IP навсегда) ────────────────────────────
# Приоритет: прокси, заданный из бота (/ig_proxy) → IG_PROXIES → IG_PROXY (общий, не советуем).
# IG_PROXIES="login1=http://user:pass@host:port;login2=http://..."
IG_PROXY   = os.getenv("IG_PROXY", "").strip()
IG_PROXIES = os.getenv("IG_PROXIES", "")


def norm_proxy(raw: str) -> str:
    """Приводит прокси к scheme://[user:pass@]host:port или '' если формат неверный."""
    from urllib.parse import urlsplit
    v = (raw or "").strip()
    if not v:
        return ""
    if "://" not in v:
        parts = v.split(":")
        if "@" in v:
            v = "http://" + v
        elif len(parts) == 4:                      # host:port:user:pass (формат многих провайдеров)
            v = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        elif len(parts) == 2:
            v = "http://" + v
        else:
            return ""
    try:
        u = urlsplit(v)
        if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname or not u.port:
            return ""
    except ValueError:
        return ""
    return v


def proxy_mask(px: str) -> str:
    """host:port без логина/пароля — безопасно показывать в чате."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(px)
        return f"{u.hostname}:{u.port}" if u.hostname else ""
    except ValueError:
        return ""


def _scrub(msg: str, px: str) -> str:
    from urllib.parse import urlsplit
    msg = (msg or "").replace(px, "<proxy>")
    try:
        u = urlsplit(px)
        for secret in (u.password, u.username):
            if secret:
                msg = msg.replace(secret, "***")
    except ValueError:
        pass
    return msg


def _env_proxies() -> dict[str, str]:
    out: dict[str, str] = {}
    for part in IG_PROXIES.replace("\n", ";").replace(",", ";").split(";"):
        if "=" in part:
            lg, px = part.split("=", 1)
            lg, px = lg.strip().lstrip("@").lower(), norm_proxy(px)
            if lg and px:
                out[lg] = px
    return out


def _proxy_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(IG_DB, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS ig_proxy (login TEXT PRIMARY KEY, proxy TEXT NOT NULL, ts REAL)")
    return conn


def get_account_proxy(login: str) -> str:
    lg = (login or "").lower()
    try:
        conn = _proxy_conn()
        try:
            r = conn.execute("SELECT proxy FROM ig_proxy WHERE login=?", (login,)).fetchone()
        finally:
            conn.close()
        if r:
            return "" if r[0] == "-" else r[0]            # '-' = в боте явно выключили прокси
    except Exception as e:
        logger.warning(f"[proxy] не прочитал ig_proxy: {e}")
    return _env_proxies().get(lg) or IG_PROXY


def _proxy_db_set(login: str, proxy: str) -> None:
    conn = _proxy_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO ig_proxy (login, proxy, ts) VALUES (?,?,?)",
                     (login, proxy or "-", time.time()))
        conn.commit()
    finally:
        conn.close()


def _apply_proxy(cl, proxy: str) -> None:
    try:
        cl.set_proxy(proxy or "")
    except Exception:
        d = {"http": proxy, "https": proxy} if proxy else {}
        for sess in (getattr(cl, "public", None), getattr(cl, "private", None)):
            if sess is not None:
                sess.proxies = d


async def test_proxy(proxy: str) -> tuple[bool, str]:
    """Реальный запрос через прокси; при успехе возвращает IP выхода."""
    def _t():
        import requests
        r = requests.get("https://api.ipify.org", proxies={"http": proxy, "https": proxy}, timeout=15)
        r.raise_for_status()
        return r.text.strip()[:45]
    try:
        return True, await asyncio.get_event_loop().run_in_executor(None, _t)
    except Exception as e:
        return False, _scrub(f"{type(e).__name__}: {e}", proxy)[:160]


async def ig_set_proxy(login: str, proxy: str) -> None:
    """Сохранить прокси ('' = выключить) и сразу применить к живому клиенту."""
    _proxy_db_set(login, proxy)
    try:
        await _POOL.set_account_proxy(login, proxy)
    except Exception as e:
        logger.warning(f"[proxy] ig_set_proxy {login}: {e}")


def ig_proxy_masks() -> dict[str, str]:
    """{login: 'host:port' или ''} для всех аккаунтов пула."""
    return {l: proxy_mask(get_account_proxy(l)) for l, _ in _POOL._accounts}


_NOPROXY_WARNED: set = set()


def _apply_client_identity(cl, login: str) -> None:
    """Прокси + (по желанию) гео клиента. Вызывается после set_settings()."""
    px = get_account_proxy(login)
    if px:
        _apply_proxy(cl, px)
    elif login not in _NOPROXY_WARNED:
        _NOPROXY_WARNED.add(login)
        logger.warning(f"[pool] ⚠️ у @{login} нет прокси — запросы идут с IP хостинга "
                       f"(главная причина «подтвердите личность»). Задай: /ig_proxy")
    # Гео меняем только если явно задано в env: смена у существующей сессии = смена «устройства».
    geo = (("IG_LOCALE", "set_locale", str), ("IG_COUNTRY", "set_country", str),
           ("IG_COUNTRY_CODE", "set_country_code", int),
           ("IG_TZ_OFFSET_HOURS", "set_timezone_offset", lambda v: int(float(v) * 3600)))
    for env, method, conv in geo:
        val = os.getenv(env)
        if val and hasattr(cl, method):
            try:
                getattr(cl, method)(conv(val))
            except Exception as e:
                logger.warning(f"[pool] гео @{login} {env}: {e}")


# ── ВХОД ПО ЛОГИНУ И ПАРОЛЮ (вместо кук из браузера) ──────────────────────────
# Один раз логинимся «как приложение» с прокси аккаунта, сохраняем session.json и дальше
# работаем только по сессии. Пароль нигде не хранится. 2FA и коды подтверждения запрашиваются
# у админа через ask_code(kind) — async-функцию, которая вернёт строку с кодом или None.

# После 429 на вход повторять с того же IP нельзя: каждая попытка продлевает блок.
IG_LOGIN_COOLDOWN_MIN = float(os.getenv("IG_LOGIN_COOLDOWN_MIN", "45"))
_LOGIN_COOLDOWN: dict[str, float] = {}          # ключ «IP» (прокси или direct) → до какого времени ждём


def _is_429(e: Exception) -> bool:
    low = f"{type(e).__name__} {e}".lower()
    return ("429" in low or "too many" in low or "retryerror" in low
            or type(e).__name__ in ("PleaseWaitFewMinutes", "RateLimitError", "ClientThrottledError"))


def _login_key(login: str) -> str:
    return proxy_mask(get_account_proxy(login)) or "direct"


def _login_error(e: Exception, password: str) -> str:
    name = type(e).__name__
    txt = str(e).replace(password, "***")
    if _is_429(e):
        return (f"Instagram ответил 429 («слишком много запросов») на вход с этого IP. Повторять сейчас нельзя — "
                f"каждая попытка продлевает блок. Подожди ~{IG_LOGIN_COOLDOWN_MIN:.0f} минут. Чтобы не упереться снова: "
                f"задай прокси этому аккаунту (🌐 в /ig_accounts) — домашний или мобильный, из своей страны. "
                f"Пока прокси нет, войди через 🍪 куки: они не трогают страницу входа.")
    if name == "BadPassword":
        return ("Instagram не принял логин/пароль. Если пароль точно верный — это защита от входа с "
                "нового IP: подтверди вход в приложении Instagram или задай прокси из своей страны (🌐).")
    if name in ("ChallengeRequired", "ChallengeUnknownStep", "SelectContactPointRecoveryForm",
                "RecaptchaChallengeForm", "CheckpointRequired", "LegacyForceSetNewPasswordForm"):
        return ("Instagram просит подтвердить, что это ты. Открой приложение Instagram на телефоне, нажми "
                "«Это был я», подожди 10–15 минут и повтори вход.")
    if name in ("PleaseWaitFewMinutes", "RateLimitError"):
        return "Instagram просит подождать. Повтори вход через 15–30 минут."
    if name in ("ProxyAddressIsBlocked",):
        return "Instagram заблокировал этот прокси. Возьми другой (🌐)."
    if "код не получен" in txt:
        return txt
    return f"{name}: {txt[:200]}"


async def ig_password_login(login: str, password: str, ask_code) -> tuple[bool, str]:
    """
    Логин по паролю → session.json → в пул (добавить или перезагрузить).
    ask_code(kind): kind = '2fa' | 'challenge:EMAIL' | 'challenge:SMS' → await str | None.
    """
    login = norm_login(login)
    if not login or not password:
        return False, "Пустой логин или пароль"
    if not _INSTAGRAPI_OK:
        return False, "instagrapi не установлена"
    await _POOL._ensure_init()
    key = _login_key(login)
    left = _LOGIN_COOLDOWN.get(key, 0) - time.time()
    if left > 0:
        return False, (f"Instagram недавно ответил 429 на вход с этого IP ({key}). Подожди ещё ~{left / 60:.0f} мин, "
                       f"иначе блок продлится. Тем временем можно добавить аккаунт через 🍪 куки.")
    loop = asyncio.get_event_loop()
    path = _session_dir() / f"{login}.json"
    was_in_pool = any(l.lower() == login for l, _ in _POOL._accounts)

    def _ask(kind: str) -> str:
        code = asyncio.run_coroutine_threadsafe(ask_code(kind), loop).result(timeout=330)
        if not code:
            raise RuntimeError("код не получен (время вышло или отмена)")
        return str(code)

    def _do() -> None:
        cl = _new_client()
        cl.delay_range = [IG_DELAY_MIN, IG_DELAY_MAX]
        try:                    # одна попытка без скрытых повторов: 429 возвращаем сразу, а не бьём по нему 5 раз
            from requests.adapters import HTTPAdapter
            for sess in (getattr(cl, "private", None), getattr(cl, "public", None)):
                if sess is not None and hasattr(sess, "mount"):
                    sess.mount("https://", HTTPAdapter(max_retries=0))
                    sess.mount("http://", HTTPAdapter(max_retries=0))
        except Exception:
            pass
        if path.exists():       # то же «устройство», что и раньше, но без мёртвой авторизации
            try:
                old = json.loads(path.read_text())
                for key, setter in (("uuids", "set_uuids"), ("device_settings", "set_device"),
                                    ("user_agent", "set_user_agent")):
                    if old.get(key) and hasattr(cl, setter):
                        getattr(cl, setter)(old[key])
            except Exception as e:
                logger.info(f"[login] {login}: не перенёс прежнее устройство: {e}")
        _apply_client_identity(cl, login)
        # без этого instagrapi вызвал бы input() и повесил поток
        cl.challenge_code_handler = lambda username, choice: _ask(
            "challenge:" + str(getattr(choice, "name", choice)))
        try:
            cl.login(login, password)
        except Exception as e:
            if type(e).__name__ != "TwoFactorRequired":
                raise
            cl.login(login, password, verification_code=_ask("2fa"))
        if not getattr(cl, "user_id", None):
            raise RuntimeError("вход не вернул user_id")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        cl.dump_settings(tmp)
        os.replace(tmp, path)

    try:
        await loop.run_in_executor(None, _do)
    except Exception as e:
        logger.warning(f"[login] @{login}: {type(e).__name__}: {str(e).replace(password, '***')[:200]}")
        if _is_429(e):
            _LOGIN_COOLDOWN[key] = time.time() + IG_LOGIN_COOLDOWN_MIN * 60
        return False, _login_error(e, password)

    ok, msg = await (_POOL.reload_client(login) if was_in_pool else _POOL.add_account(login))
    if not ok:
        if not was_in_pool:
            try:
                path.unlink()
            except OSError:
                pass
        return False, msg
    try:
        persist_session(login)
    except Exception:
        pass
    logger.info(f"[login] ✅ @{login}: вход по паролю, сессия сохранена")
    return True, msg


# ── ТРАНСПОРТ instagrapi 3.2+ (curl_cffi: TLS/HTTP2-отпечаток как у настоящего клиента) ──
# IG_PUBLIC_TRANSPORT=curl|requests, IG_PUBLIC_IMPERSONATE=chrome136. На старой версии библиотеки
# или без curl_cffi тихо работаем как раньше. Обновление: pip install -U "instagrapi[curl]" (Python 3.11+).
IG_PUBLIC_TRANSPORT   = os.getenv("IG_PUBLIC_TRANSPORT", "curl").strip().lower()
IG_PUBLIC_IMPERSONATE = os.getenv("IG_PUBLIC_IMPERSONATE", "chrome136").strip()
_TRANSPORT_LOGGED = False


def _new_client():
    global _TRANSPORT_LOGGED
    why = ""
    if IG_PUBLIC_TRANSPORT == "curl":
        try:
            import curl_cffi  # noqa: F401
            have_curl = True
        except Exception:
            have_curl = False
        if not have_curl:
            why = 'не установлен curl_cffi — pip install -U "instagrapi[curl]"'
        else:
            try:
                cl = Client(public_transport="curl", public_transport_impersonate=IG_PUBLIC_IMPERSONATE)
                if not _TRANSPORT_LOGGED:
                    _TRANSPORT_LOGGED = True
                    logger.info(f"[pool] транспорт: curl/{IG_PUBLIC_IMPERSONATE}, instagrapi {_ig_version()}")
                return cl
            except TypeError:
                why = f"instagrapi {_ig_version()} не знает public_transport — обнови до 3.2+ (нужен Python 3.11+)"
            except Exception as e:
                why = f"{type(e).__name__}: {e}"
    if why and not _TRANSPORT_LOGGED:
        _TRANSPORT_LOGGED = True
        logger.warning(f"[pool] curl-транспорт выключен: {why}")
    return Client()


def _ig_version() -> str:
    try:
        from importlib.metadata import version
        return version("instagrapi")
    except Exception:
        return "?"


# ── ТИХИЕ ЧАСЫ (фоновые запросы ночью не идут) ───────────────────────────────
# IG_QUIET_HOURS="1-7" — по местному времени (IG_TZ_OFFSET_HOURS от UTC, по умолчанию +3).
# "off" — выключить. Запросы пользователя ночью работают; ждут только фоновые задачи.
IG_QUIET_HOURS     = os.getenv("IG_QUIET_HOURS", "1-7").strip().lower()
IG_TZ_OFFSET_HOURS = float(os.getenv("IG_TZ_OFFSET_HOURS", "3"))


def _quiet_window():
    if IG_QUIET_HOURS in ("", "off", "0", "no", "нет"):
        return None
    try:
        a, b = IG_QUIET_HOURS.replace("–", "-").split("-", 1)
        a, b = int(a) % 24, int(b) % 24
    except Exception:
        return None
    return None if a == b else (a, b)


def quiet_remaining() -> float:
    """Сколько секунд осталось до конца тихих часов (0 — сейчас не тихие)."""
    w = _quiet_window()
    if not w:
        return 0.0
    a, b = w
    h = ((time.time() + IG_TZ_OFFSET_HOURS * 3600) % 86400) / 3600.0
    inside = (a <= h < b) if a < b else (h >= a or h < b)
    return ((b - h) % 24) * 3600 if inside else 0.0


def quiet_hours_label() -> str:
    w = _quiet_window()
    return f"🌙 тихие часы {w[0]:02d}–{w[1]:02d} (UTC{IG_TZ_OFFSET_HOURS:+g}): фоновые запросы спят" if w else ""


# ── ПРОГРЕВ: новые аккаунты получают сниженные суточные лимиты ───────────────
IG_WARMUP_DAYS = float(os.getenv("IG_WARMUP_DAYS", "7"))      # 0 — выключить


def _seen_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(IG_DB, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS ig_acc_first_seen (login TEXT PRIMARY KEY, ts REAL NOT NULL)")
    return conn


def _seed_first_seen(accounts) -> None:
    """Аккаунты, что были до этого обновления, считаются «старыми» (ts=0)."""
    if not accounts:
        return
    conn = _seen_conn()
    try:
        empty = conn.execute("SELECT COUNT(*) FROM ig_acc_first_seen").fetchone()[0] == 0
        ts = 0.0 if empty else time.time()
        for l, _ in accounts:
            conn.execute("INSERT OR IGNORE INTO ig_acc_first_seen (login, ts) VALUES (?,?)", (l, ts))
        conn.commit()
    finally:
        conn.close()


def _first_seen_set(login: str) -> None:
    conn = _seen_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO ig_acc_first_seen (login, ts) VALUES (?,?)", (login, time.time()))
        conn.commit()
    finally:
        conn.close()


def warmup_factor(login: str) -> float:
    if IG_WARMUP_DAYS <= 0:
        return 1.0
    try:
        conn = _seen_conn()
        try:
            r = conn.execute("SELECT ts FROM ig_acc_first_seen WHERE login=?", (login,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return 1.0
    if not r or not r[0]:
        return 1.0
    age = (time.time() - float(r[0])) / 86400
    return 1.0 if age >= IG_WARMUP_DAYS else (0.5 if age >= IG_WARMUP_DAYS / 2 else 0.25)


def _day_limit(base: int, login: str) -> int:
    return 0 if base <= 0 else max(1, int(base * warmup_factor(login)))


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
        cl = _new_client()
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
        _apply_client_identity(cl, login)

        ds_user_id = settings.get("cookies", {}).get("ds_user_id", "")
        logger.info(f"[pool] ✅ загружен: {login}  ds_user_id={ds_user_id}")
        return cl

    def _init_all(self):
        if not _INSTAGRAPI_OK:
            return
        self._accounts = _merge_accounts(self._accounts)
        try:
            _seed_first_seen(self._accounts)
        except Exception as e:
            logger.warning(f"[pool] first_seen: {e}")
        if not self._accounts:
            logger.error("[pool] нет аккаунтов (IG_ACCOUNTS пуст, из бота не добавлены) — Instagram недоступен")
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

    def _select_among(self, logins: set, prio: int) -> Optional[int]:
        """Выбор аккаунта ТОЛЬКО из logins (например, тех, кто подписан на приватный профиль)."""
        now = time.time()
        idxs = [i for i, (l, _) in enumerate(self._accounts)
                if l in logins and i < len(self._clients) and self._clients[i] is not None]
        if not idxs:
            return None
        ok = [i for i in idxs if self._usable(i, now)]
        if ok:
            cur = self._current % max(len(self._clients), 1)
            return cur if cur in ok else ok[0]
        if prio >= PRIO_BG:
            return None
        alive = [i for i in idxs if self._accounts[i][0] not in self._known_dead] or idxs
        return min(alive, key=lambda k: self._rate_limited_until.get(k, 0))

    def live_logins(self) -> list[str]:
        """Логины живых (не мёртвых) аккаунтов пула."""
        return [l for i, (l, _) in enumerate(self._accounts)
                if i < len(self._clients) and self._clients[i] is not None and l not in self._known_dead]

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

    # ── пометки живой / мёртвой сессии (health-check и ручные проверки) ───────

    def _idx_of(self, login: str) -> Optional[int]:
        return next((i for i, (l, _) in enumerate(self._accounts) if l == login), None)

    def mark_dead(self, login: str) -> bool:
        """Пометить сессию мёртвой (с отметкой времени — иначе _usable() её не исключит).
        True — раньше она мёртвой не считалась (стоит слать алерт)."""
        first = login not in self._known_dead
        self._known_dead.add(login)
        self._dead_ts[login] = time.time()
        idx = self._idx_of(login)
        if idx is not None and self._clients and idx == self._current % len(self._clients):
            self._rotate_from(idx)
        return first

    def mark_alive(self, login: str) -> bool:
        """Снять пометку «мёртвая». True — сессия действительно ожила."""
        was = login in self._known_dead
        self._known_dead.discard(login)
        self._dead_ts.pop(login, None)
        return was

    async def set_account_proxy(self, login: str, proxy: str) -> bool:
        """Применить прокси к уже работающему клиенту (между запросами)."""
        await self._ensure_init()
        if self._idx_of(login) is None:
            return False
        async with self._gate.hold(PRIO_USER):
            idx = self._idx_of(login)
            if idx is None or idx >= len(self._clients) or self._clients[idx] is None:
                return False
            _apply_proxy(self._clients[idx], proxy)
        return True

    # ── ручное добавление / удаление доноров ─────────────────────────────────

    async def add_account(self, login: str) -> tuple[bool, str]:
        """
        Добавить донора в работающий пул. session.json должен уже лежать в session_dir
        (его собирает хендлер из присланных кук). Клиент сначала проверяется реальным
        запросом, и только потом попадает в пул.
        """
        login = norm_login(login)
        if not login:
            return False, "Неверный логин (буквы, цифры, точка, подчёркивание, до 30 символов)"
        await self._ensure_init()
        if any(l.lower() == login for l, _ in self._accounts):
            return False, f"{login} уже в пуле"

        loop = asyncio.get_event_loop()
        try:
            new_cl = await loop.run_in_executor(None, lambda: self._make_client(login, ""))
        except Exception as e:
            return False, f"Ошибка загрузки session.json: {e}"

        def _ping(cl):
            _unwrap_percent_re()
            info = cl.user_info_by_username(login)
            uid = str(getattr(cl, "user_id", "") or "")
            if uid and str(getattr(info, "pk", "")) != uid:
                raise ValueError(f"куки принадлежат другому аккаунту (ds_user_id={uid}), а не {login}")
            return info

        try:
            await loop.run_in_executor(None, lambda: _ping(new_cl))
        except LoginRequired as e:
            return False, f"Куки не работают — LoginRequired: {e}"
        except Exception as e:
            return False, f"Тест не прошёл: {e}"

        async with self._gate.hold(PRIO_USER):          # ни один запрос в этот момент не летит
            if any(l.lower() == login for l, _ in self._accounts):
                return False, f"{login} уже в пуле"
            self._accounts.append((login, ""))
            self._clients.append(new_cl)
            self._request_counts[len(self._clients) - 1] = 0
            self._initialized = True
        try:
            _pool_acc_set(login, "added")
            _first_seen_set(login)
        except Exception as e:
            logger.warning(f"[pool] add_account {login}: не записал в БД: {e}")
        logger.info(f"[pool] ➕ добавлен донор {login} (всего {len(self._accounts)})")
        return True, "Аккаунт добавлен и протестирован"

    async def remove_account(self, login: str) -> tuple[bool, str]:
        """Убрать донора из пула, стереть его сессию и записи в БД."""
        await self._ensure_init()
        if self._idx_of(login) is None:
            return False, f"{login} нет в пуле"

        async with self._gate.hold(PRIO_USER):          # индексы не должны сдвинуться под живым запросом
            idx = self._idx_of(login)
            if idx is None:
                return False, f"{login} нет в пуле"
            old_n = max(len(self._clients), 1)
            cur = self._current % old_n
            self._accounts.pop(idx)
            if idx < len(self._clients):
                self._clients.pop(idx)

            def _shift(d: dict) -> dict:
                return {(k - 1 if k > idx else k): v for k, v in d.items() if k != idx}

            self._request_counts = _shift(self._request_counts)
            self._rate_limited_until = _shift(self._rate_limited_until)
            self._since_dump = _shift(self._since_dump)
            for dct in (self._loaded_mtime, self._dump_hash, self._dead_ts):
                dct.pop(login, None)
            self._known_dead.discard(login)

            new_n = len(self._clients)
            if new_n == 0:
                self._current = 0
            else:
                if cur > idx:
                    cur -= 1
                self._current = cur % new_n

        try:
            _pool_acc_set(login, "removed")
            await asyncio.get_event_loop().run_in_executor(None, _purge_account_data, login)
        except Exception as e:
            logger.warning(f"[pool] remove_account {login}: чистка данных: {e}")
        logger.info(f"[pool] 🗑 удалён донор {login} (осталось {len(self._accounts)})")
        return True, "Аккаунт удалён из пула"

    # ── выполнение запроса с ротацией ────────────────────────────────────────

    async def run(self, fn, *args, retries: int | None = None, prio: int | None = None,
                  only: set | None = None, **kwargs):
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

        if prio >= PRIO_BG:                 # фон ночью спит; пользователь не ждёт никогда
            slept = False
            while True:
                qr = quiet_remaining()
                if qr <= 0:
                    break
                slept = True
                await asyncio.sleep(min(qr, 600))
            if slept:
                await asyncio.sleep(random.uniform(0, 900))   # не стартовать всем разом в 07:00

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
                    idx = self._select_among(only, prio) if only else self._select_idx(prio)
                    if idx is None:
                        logger.warning("[pool] нет доступных аккаунтов (все в 429/мертвы"
                                       + (f" или нет среди {sorted(only)}" if only else "") + ") — запрос пропущен")
                        return None
                    cl = self._clients[idx]

                    is_bg = prio >= PRIO_BG
                    gap = self._last_start + (IG_BG_MIN_INTERVAL if is_bg else IG_MIN_INTERVAL) - time.monotonic()
                    if gap > 0:
                        await asyncio.sleep(gap)
                    self._last_start = time.monotonic()

                    _unwrap_percent_re()
                    cl.delay_range = [IG_BG_DELAY_MIN, IG_BG_DELAY_MAX] if is_bg else [IG_DELAY_MIN, IG_DELAY_MAX]
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: fn(cl, *args, **kwargs)),
                        timeout=IG_REQ_TIMEOUT + (2 * IG_BG_DELAY_MAX if is_bg else 0),
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
            "dead": login in _POOL._known_dead,
            "dead_for": int(now - _POOL._dead_ts.get(login, now)) if login in _POOL._known_dead else 0,
            "proxy": bool(getattr(getattr(cl, "private", None), "proxies", None)) if cl is not None else False,
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


async def validate_session(login: str, strict: bool = True) -> bool:
    """
    Проверить, что сессия для login реально авторизована.
    Раньше дёргали user_info_by_username(login): он сначала идёт публичным запросом
    БЕЗ авторизации, и мёртвая сессия проходила проверку. Теперь бьём приватным
    users/<pk>/info/ — он без живых кук не отвечает.
      • LoginRequired            → False (сессия мертва);
      • 429 / PleaseWait         → True (сессия жива, просто лимит);
      • сеть / прочие ошибки     → False при strict=True (проверка новых кук),
                                    True при strict=False (health-check: сбой сети
                                    — не повод объявлять сессию мёртвой).
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
        uid = getattr(c, "user_id", None)
        if uid:
            try:
                return bool(c.user_info_v1(str(uid)))
            except LoginRequired:
                raise
            except (RateLimitError, PleaseWaitFewMinutes):
                return True
            except ClientError:
                pass      # эндпоинт капризничает — проверим старым способом ниже
        return bool(c.user_info_by_username(login))

    try:
        async with _POOL._gate.hold():
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _ping(cl)
            )
        return bool(result)
    except LoginRequired as e:
        logger.warning(f"validate_session {login}: LoginRequired {e}")
        return False
    except (RateLimitError, PleaseWaitFewMinutes):
        return True
    except Exception as e:
        logger.warning(f"validate_session {login}: {type(e).__name__}: {e}")
        return (not strict) if isinstance(e, _NET_ERRORS) else False


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


def _music_from_media(media) -> dict | None:
    """Метаданные звука рилса: licensed-трек (исполнитель/название) или оригинальный звук."""
    try:
        cm = getattr(media, "clips_metadata", None)
        if not isinstance(cm, dict):
            return None
        mi = cm.get("music_info")
        ma = mi.get("music_asset_info") if isinstance(mi, dict) else None
        if isinstance(ma, dict) and (ma.get("title") or ma.get("display_artist")):
            return {
                "kind":      "licensed",
                "artist":    str(ma.get("display_artist") or "").strip(),
                "title":     str(ma.get("title") or "").strip(),
                "audio_url": str(ma.get("progressive_download_url") or ""),
            }
        osi = cm.get("original_sound_info")
        if isinstance(osi, dict) and (osi.get("original_audio_title") or osi.get("progressive_download_url")):
            artist = ""
            ig_artist = osi.get("ig_artist")
            if isinstance(ig_artist, dict):
                artist = str(ig_artist.get("username") or "")
            return {
                "kind":      "original",
                "artist":    artist,
                "title":     str(osi.get("original_audio_title") or "Оригинальный звук").strip(),
                "audio_url": str(osi.get("progressive_download_url") or ""),
            }
    except Exception as e:
        logger.debug(f"_music_from_media: {e}")
    return None


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
            "music":         _music_from_media(media),
        }
    except Exception as e:
        logger.error(f"_media_to_post error: {e}")
        return {}


def _unwrap_ig_link(u: str) -> str:
    """l.instagram.com/?u=<реальная ссылка> → реальная ссылка."""
    try:
        from urllib.parse import urlparse, parse_qs
        p = urlparse(u)
        if p.netloc.endswith("l.instagram.com"):
            real = (parse_qs(p.query).get("u") or [""])[0]
            return real or u
    except Exception:
        pass
    return u


def _story_extras(item) -> dict:
    """taken_at / mentions / link из StoryItem (всё, что пришло в том же ответе — доп. запросов нет)."""
    out: dict = {"taken_at": 0, "mentions": [], "link": ""}
    try:
        t = getattr(item, "taken_at", None)
        if t:
            out["taken_at"] = int(t.timestamp())
    except Exception:
        pass
    try:
        seen = []
        for m in getattr(item, "mentions", None) or []:
            usr = getattr(m, "user", None)
            name = (getattr(usr, "username", "") or "").lower()
            if name and name not in seen:
                seen.append(name)
        out["mentions"] = seen[:10]
    except Exception:
        pass
    try:
        link = ""
        for l in getattr(item, "links", None) or []:
            u = str(getattr(l, "webUri", "") or getattr(l, "web_uri", "") or "")
            if u:
                link = u
                break
        if not link:
            cta = getattr(item, "story_cta", None)
            if isinstance(cta, list) and cta and isinstance(cta[0], dict):
                for l in cta[0].get("links") or []:
                    u = str(l.get("webUri") or "")
                    if u:
                        link = u
                        break
        out["link"] = _unwrap_ig_link(link) if link else ""
    except Exception:
        pass
    return out


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
            **_story_extras(item),
        }
    except Exception as e:
        logger.error(f"_story_to_dict error: {e}")
        return {}


def _fail_if_endpoint_404(cl, exc: Exception) -> None:
    """
    instagrapi превращает ЛЮБОЙ 404 в UserNotFound. Но 404 «Endpoint ... does not exist» —
    это Instagram режет сам эндпоинт (устаревший девайс/блок), а не «ника нет».
    Такое надо считать сбоем метода (идём дальше по каскаду), а не «не найден».
    """
    msg = str((getattr(cl, "last_json", None) or {}).get("message", ""))
    if "endpoint" in msg.lower():
        raise ClientError(f"IG endpoint 404: {msg}") from exc


# ── КАСКАД ПОЛУЧЕНИЯ ПРОФИЛЯ ──────────────────────────────────────────────────
# Раньше: instagrapi.user_info_by_username → публичный запрос БЕЗ кук → 429 с IP хостинга →
# приватный usernameinfo → 404 → «не найден». Теперь методы пробуются по очереди:
#   web     — web_profile_info, но С КУКАМИ аккаунта (с авторизацией лимиты мягче);
#   gql     — публичный метод instagrapi (если есть в установленной версии);
#   private — приватный usernameinfo;
#   search  — поиск по нику + users/<pk>/info/ (последний шанс).
# «Не найден» объявляем только если это подтвердил web (404) либо приватный API ответил
# настоящим «User not found». Любой сбой/лимит → пул ротирует аккаунт и повторяет.

_WEB_APP_ID = "936619743392459"
_WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_LOOKUP_STATS: dict[str, dict[str, int]] = {}
_LOOKUP_ORDER = ("web", "gql", "private", "search")


_LK_RECENT: dict[str, deque] = {}        # метод → последние (ts, ok) для адаптивного порядка
_LK_PROBE: Counter = Counter()
_LK_WINDOW = 3600                        # по каким результатам оцениваем метод, сек
_LK_BREAK_WINDOW = 600                   # «5 неудач подряд за это время» → метод временно пропускаем
_LOOKUP_LOG: deque = deque(maxlen=80)    # журнал для /ig_log


def _lk_note(method: str, ok: bool) -> None:
    d = _LOOKUP_STATS.setdefault(method, {"ok": 0, "fail": 0})
    d["ok" if ok else "fail"] += 1
    _LK_RECENT.setdefault(method, deque(maxlen=30)).append((time.time(), ok))


def _lk_score(method: str) -> float | None:
    """Доля успехов метода за последний час (None — данных мало, меньше 3 вызовов)."""
    now = time.time()
    xs = [ok for ts, ok in _LK_RECENT.get(method, ()) if now - ts <= _LK_WINDOW]
    return (sum(xs) / len(xs)) if len(xs) >= 3 else None


def _lk_broken(method: str) -> bool:
    now = time.time()
    last = [ok for ts, ok in _LK_RECENT.get(method, ()) if now - ts <= _LK_BREAK_WINDOW][-5:]
    return len(last) == 5 and not any(last)


def _lk_arrange(steps: list, commit: bool = True) -> list:
    """
    Адаптивный порядок: методы с лучшей недавней статистикой идут первыми, «лежащие»
    (5 неудач подряд за 10 минут) пропускаются — но каждый 5-й вызов пробуем снова, чтобы
    заметить, что метод ожил. «search» всегда последний; если пропустили бы всё — идём как есть.
    """
    head, tail = steps[:-1], steps[-1:]
    pos = {n: i for i, (n, _) in enumerate(head)}
    ordered = sorted(head, key=lambda st: (-(0.5 if _lk_score(st[0]) is None else _lk_score(st[0])),
                                           pos[st[0]]))
    live = []
    for st in ordered:
        if _lk_broken(st[0]):
            if not commit or _bump_probe(st[0]) % 5 != 0:
                continue
        live.append(st)
    return (live or ordered) + tail


def _bump_probe(method: str) -> int:
    _LK_PROBE[method] += 1
    return _LK_PROBE[method]


def _acc_login(cl) -> str:
    try:
        i = _POOL._clients.index(cl)
        return _POOL._accounts[i][0]
    except Exception:
        return "?"


def _lk_log(cl, username: str, result: str, method: str = "", notes: list | None = None) -> None:
    """result: ok | notfound | fail. Журнал для /ig_log."""
    _LOOKUP_LOG.append({
        "ts": time.time(), "kind": "lookup", "user": username, "result": result,
        "method": method, "notes": "; ".join(notes or []), "acc": _acc_login(cl),
    })


def _web_profile_info(cl, username: str):
    """web_profile_info с куками аккаунта (sessionid/csrftoken/ds_user_id)."""
    import requests
    from instagrapi.extractors import extract_user_gql

    jar: dict = {}
    try:
        jar = dict(cl.private.cookies.get_dict())
    except Exception:
        pass
    if not jar.get("sessionid"):
        try:
            jar.update((cl.get_settings() or {}).get("cookies") or {})
        except Exception:
            pass

    headers = {
        "User-Agent": _WEB_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": _WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": jar.get("csrftoken", ""),
        "Referer": f"https://www.instagram.com/{username}/",
    }
    proxies = None
    try:
        proxies = dict(getattr(cl.private, "proxies", None) or {}) or None
    except Exception:
        pass

    sess = requests.Session()
    for k, v in jar.items():
        if v:
            sess.cookies.set(k, str(v), domain=".instagram.com")
    r = sess.get(
        "https://www.instagram.com/api/v1/users/web_profile_info/",
        params={"username": username}, headers=headers, proxies=proxies, timeout=20,
    )
    if r.status_code == 404:
        raise UserNotFound(f"web 404 @{username}", username=username)
    if r.status_code == 429:
        raise RateLimitError(f"web 429 @{username}")
    if r.status_code in (401, 403):
        raise LoginRequired(f"web {r.status_code} @{username}")
    if r.status_code != 200:
        raise ClientError(f"web HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise ClientError("web: не JSON (логин-стена)")
    user = ((data or {}).get("data") or {}).get("user")
    if not user:
        raise UserNotFound(f"web: пустой ответ @{username}", username=username)
    return extract_user_gql(user)


def _lookup_via_search(cl, username: str):
    """Последний шанс: ищем ник в поиске IG, берём точное совпадение."""
    fn = (getattr(cl, "fbsearch_accounts", None)
          or getattr(cl, "search_users_v1", None)
          or getattr(cl, "search_users", None))
    if not fn:
        raise ClientError("search: метод недоступен")
    for u in fn(username) or []:
        if (getattr(u, "username", "") or "").lower() == username:
            try:
                return cl.user_info_v1(str(u.pk))
            except LoginRequired:
                raise
            except ClientError:
                d = _user_to_dict(u)
                d["partial"] = True       # без счётчиков — не кэшируем
                return d
    raise ClientError("search: точного совпадения нет")


def _lookup_user(cl, username: str) -> dict:
    """Каскад методов. Возвращает dict профиля или бросает исключение для пула."""
    steps: list[tuple[str, Callable]] = [("web", lambda: _web_profile_info(cl, username))]
    gql = getattr(cl, "user_info_by_username_gql", None)
    if gql:
        steps.append(("gql", lambda: gql(username)))
    v1 = getattr(cl, "user_info_by_username_v1", None)
    if v1:
        steps.append(("private", lambda: v1(username)))
    steps.append(("search", lambda: _lookup_via_search(cl, username)))
    steps = _lk_arrange(steps)

    rate = login = net = None
    nf_votes = 0
    notes: list[str] = []

    for name, fn in steps:
        try:
            _unwrap_percent_re()
            res = fn()
            d = res if isinstance(res, dict) else _user_to_dict(res)
            if not d or not d.get("username"):
                raise ClientError("пустой результат")
            _lk_note(name, True)
            _lk_log(cl, username, "ok", name, notes)
            return d
        except PrivateAccount:
            raise
        except UserNotFound as e:
            if name == "web":
                _lk_note(name, True)       # метод отработал — ответ подтверждённый
                _lk_log(cl, username, "notfound", name, notes)
                raise
            try:
                _fail_if_endpoint_404(cl, e)
                nf_votes += 1              # настоящий «User not found» от приватного API
                _lk_note(name, True)
            except ClientError as ce:
                _lk_note(name, False)
                notes.append(f"{name}: {ce}")
        except (RateLimitError, PleaseWaitFewMinutes) as e:
            _lk_note(name, False)
            rate = rate or e
            notes.append(f"{name}: 429")
        except LoginRequired as e:
            _lk_note(name, False)
            login = login or e
            notes.append(f"{name}: login")
        except ClientError as e:
            _lk_note(name, False)
            notes.append(f"{name}: {e}")
        except _NET_ERRORS as e:
            _lk_note(name, False)
            net = net or e
            notes.append(f"{name}: net {type(e).__name__}")
        except Exception as e:                 # битый ответ/pydantic и т.п. — не роняем каскад
            _lk_note(name, False)
            notes.append(f"{name}: {type(e).__name__}: {e}")

    logger.warning(f"[lookup] @{username}: все методы не сработали — {'; '.join(notes)}")
    _lk_log(cl, username, "notfound" if nf_votes else "fail", "", notes)
    if nf_votes:
        raise UserNotFound(f"private: User not found @{username}", username=username)
    if rate:
        raise rate
    if login:
        raise login
    if net:
        raise net
    raise ClientError(f"lookup @{username}: " + "; ".join(notes))


# ── ПУБЛИЧНЫЙ API ─────────────────────────────────────────────────────────────

# Кэш профилей: память (TTL IG_USER_CACHE_TTL) + диск (SQLite, переживает рестарт и
# нужен, чтобы показать устаревшую карточку, когда Instagram не отвечает).
_USER_CACHE: dict[str, dict] = {}
_NOTFOUND_CACHE: dict[str, tuple[float, int]] = {}   # ник → (когда, сколько раз подряд «не найден»)
_CSTAT: Counter = Counter()


def _pc_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(IG_DB, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ig_profile_cache ("
        "username TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL)"
    )
    return conn


def _pc_save(username: str, info: dict) -> None:
    try:
        conn = _pc_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO ig_profile_cache (username, data, ts) VALUES (?,?,?)",
                (username, json.dumps(info, ensure_ascii=False, default=str), time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"profile cache save {username}: {e}")


def _pc_load(username: str) -> tuple[dict, float] | None:
    try:
        conn = _pc_conn()
        try:
            row = conn.execute(
                "SELECT data, ts FROM ig_profile_cache WHERE username=?", (username,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return json.loads(row[0]), float(row[1])
    except Exception as e:
        logger.debug(f"profile cache load {username}: {e}")
        return None


def _pc_drop(username: str | None = None) -> None:
    try:
        conn = _pc_conn()
        try:
            if username:
                conn.execute("DELETE FROM ig_profile_cache WHERE username=?", (username,))
            else:
                conn.execute("DELETE FROM ig_profile_cache")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"profile cache drop: {e}")


def cached_usernames(limit: int = 500) -> list[str]:
    """Ники, которые бот уже видел (для подсказок при опечатке)."""
    names = set(_USER_CACHE)
    try:
        conn = _pc_conn()
        try:
            for (u,) in conn.execute(
                "SELECT username FROM ig_profile_cache ORDER BY ts DESC LIMIT ?", (limit,)
            ):
                names.add(u)
        finally:
            conn.close()
    except Exception:
        pass
    return list(names)


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
    _pc_save(username, info)


def _ucache_stale(username: str) -> tuple[dict, float] | None:
    """Копия профиля любого возраста (до IG_STALE_MAX_DAYS): память, потом диск."""
    e = _USER_CACHE.get(username)
    if e:
        return dict(e["info"]), e["ts"]
    row = _pc_load(username)
    if row and time.time() - row[1] <= IG_STALE_MAX_DAYS * 86400:
        return row
    return None


def _nf_ttl(count: int) -> int:
    return IG_NOTFOUND_TTLS[min(max(count, 1), len(IG_NOTFOUND_TTLS)) - 1]


def _nf_mark(username: str) -> None:
    prev = _NOTFOUND_CACHE.get(username)
    _NOTFOUND_CACHE[username] = (time.time(), (prev[1] if prev else 0) + 1)
    if len(_NOTFOUND_CACHE) > 1000:
        _NOTFOUND_CACHE.pop(min(_NOTFOUND_CACHE, key=lambda k: _NOTFOUND_CACHE[k][0]), None)


def _fail_result(username: str, allow_stale: bool) -> tuple[str, dict | None]:
    """Сбой получения профиля: устаревшая копия (если разрешили) или «error»."""
    if allow_stale:
        st = _ucache_stale(username)
        if st:
            info, ts = st
            info["_stale_ts"] = ts
            _CSTAT["stale"] += 1
            return "stale", info
    return "error", None


async def get_user_status(username: str, force: bool = False,
                          allow_stale: bool = False, swr: bool = False) -> tuple[str, dict | None]:
    """
    Как get_user_info, но различает причины пустого ответа:
      ("ok", info)       — профиль получен (в т.ч. приватный);
      ("notfound", None) — IG подтвердил, что такого ника нет;
      ("stale", info)    — только при allow_stale=True: IG не ответил, но есть сохранённая
                           копия (info["_stale_ts"] — когда она получена);
      ("swr", info)      — только при swr=True: кэш старше TTL, но моложе IG_SWR_MAX_AGE;
                           отдаём сразу, а вызывающий обновляет данные фоном (force=True);
      ("error", None)    — не вышло (429, сеть, пул пуст) — это НЕ «аккаунта нет».
    Нужно фоновой проверке: без этого смена ника и сбой сети выглядят одинаково.
    «error» никогда не кэшируется; «notfound» кэшируется с эскалацией TTL (5 мин → 1 ч → сутки).
    """
    username = username.lower().strip("@").strip()
    if not username:
        return "notfound", None

    if not force:
        cached = _ucache_get(username)
        if cached:
            _CSTAT["hit"] += 1
            return "ok", cached
        nf = _NOTFOUND_CACHE.get(username)
        if nf and time.time() - nf[0] < _nf_ttl(nf[1]):
            _CSTAT["nf_hit"] += 1
            return "notfound", None
        if swr:
            st = _ucache_stale(username)
            if st and time.time() - st[1] <= IG_SWR_MAX_AGE:
                info, ts = st
                info["_stale_ts"] = ts
                _CSTAT["swr"] += 1
                return "swr", info
    _CSTAT["miss"] += 1

    def _fetch(cl: Client):
        return _lookup_user(cl, username)

    try:
        result = await _POOL.run(_fetch)
    except UserNotFound:
        logger.warning(f"UserNotFound: @{username}")
        _nf_mark(username)
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
        return _fail_result(username, allow_stale)

    if result:
        if result.get("partial"):
            logger.info(f"get_user_info @{username}: частичные данные (через поиск), в кэш не кладу")
            return "ok", result
        _ucache_set(username, result)
        _NOTFOUND_CACHE.pop(username, None)
        logger.info(f"✅ get_user_info @{username}: {result.get('followers')} фолловеров")
        return "ok", result
    return _fail_result(username, allow_stale)


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
        try:
            return _user_to_dict(cl.user_info(int(pk)))
        except UserNotFound as e:
            _fail_if_endpoint_404(cl, e)
            raise

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


def reset_notfound_cache(username: str | None = None) -> None:
    """Сбросить «не найден» (кнопка «повторить»): один ник или все."""
    if username:
        _NOTFOUND_CACHE.pop(username.lower().strip("@"), None)
    else:
        _NOTFOUND_CACHE.clear()


def clear_profile_caches() -> dict:
    """Админская кнопка «сбросить кэш»: память + диск + notfound. Возвращает, сколько стёрто."""
    n = {"mem": len(_USER_CACHE), "nf": len(_NOTFOUND_CACHE), "search": len(_SEARCH_CACHE)}
    _USER_CACHE.clear()
    _NOTFOUND_CACHE.clear()
    _SEARCH_CACHE.clear()
    _pc_drop()
    return n


def get_lookup_stats() -> dict:
    """Сводка для /ig_status: методы каскада, кэш, устаревшие карточки."""
    db_n = 0
    try:
        conn = _pc_conn()
        try:
            db_n = conn.execute("SELECT COUNT(*) FROM ig_profile_cache").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        pass
    return {
        "methods": {m: dict(_LOOKUP_STATS.get(m, {"ok": 0, "fail": 0})) for m in _LOOKUP_ORDER},
        "cache_mem": len(_USER_CACHE),
        "cache_db": db_n,
        "notfound": len(_NOTFOUND_CACHE),
        "hit": _CSTAT["hit"], "miss": _CSTAT["miss"],
        "nf_hit": _CSTAT["nf_hit"], "stale": _CSTAT["stale"], "swr": _CSTAT["swr"],
        "order": [n for n, _ in _lk_arrange([(m, None) for m in _LOOKUP_ORDER[:-1]] + [("search", None)], commit=False)],
        "broken": [m for m in _LOOKUP_ORDER[:-1] if _lk_broken(m)],
        "scores": {m: _lk_score(m) for m in _LOOKUP_ORDER},
    }


def get_recent_events(n: int = 10, include_ok: bool = False) -> list[dict]:
    """
    Последние события для /ig_log: сбои каскада (с причиной по каждому методу и аккаунтом)
    и ошибки пула (429, LoginRequired, сеть). include_ok=True добавляет и успешные запросы.
    Новые — первыми.
    """
    ev: list[dict] = []
    for e in _LOOKUP_LOG:
        if include_ok or e["result"] != "ok":
            ev.append(e)
    for ts, kind, msg in _POOL._err_events:
        ev.append({"ts": ts, "kind": "pool", "type": kind, "msg": msg})
    ev.sort(key=lambda e: e["ts"], reverse=True)
    return ev[:max(1, n)]


# ── ПОИСК АККАУНТОВ ПО ИМЕНИ ──────────────────────────────────────────────────

_SEARCH_CACHE: dict[str, tuple[float, list]] = {}
_SEARCH_TTL = 600


async def search_accounts(query: str, limit: int = 8) -> list[dict] | None:
    """
    Поиск аккаунтов по имени/нику («leya cakey», «эмма уотсон»).
    Возвращает список dict (username, full_name, is_verified, is_private, pk) —
    пустой, если ничего нет, и None, если Instagram не ответил (сбой ≠ «пусто»).
    """
    q = " ".join((query or "").split())[:60]
    if len(q) < 2:
        return []
    key = q.lower()
    hit = _SEARCH_CACHE.get(key)
    if hit and time.time() - hit[0] < _SEARCH_TTL:
        return list(hit[1])[:limit]

    def _fetch(cl: Client):
        res = None
        last: Exception | None = None
        for name in ("fbsearch_accounts", "search_users_v1", "search_users"):
            fn = getattr(cl, name, None)
            if not fn:
                continue
            try:
                res = fn(q)
                if res:
                    break
            except (LoginRequired, RateLimitError, PleaseWaitFewMinutes):
                raise
            except ClientError as e:
                last = e
        if res is None and last is not None:
            raise last
        out = []
        for u in res or []:
            uname = getattr(u, "username", "") or ""
            if not uname:
                continue
            out.append({
                "username": uname,
                "full_name": getattr(u, "full_name", "") or "",
                "is_verified": bool(getattr(u, "is_verified", False)),
                "is_private": bool(getattr(u, "is_private", False)),
                "pk": str(getattr(u, "pk", "") or ""),
            })
        return out

    try:
        out = await _POOL.run(_fetch)
    except Exception as e:
        logger.warning(f"search_accounts {q!r}: {e}")
        return None
    if out is None:
        return None
    _SEARCH_CACHE[key] = (time.time(), out)
    if len(_SEARCH_CACHE) > 200:
        _SEARCH_CACHE.pop(min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0]), None)
    return out[:limit]


async def get_stories(username: str) -> list:
    """Получить активные истории пользователя."""
    info = await get_user_info(username)
    if not info:
        return []
    only = _access(username, info)
    if only is False:
        return []

    user_id = _pk_for(username, info)
    if not user_id:
        return []

    def _fetch(cl: Client):
        return cl.user_stories(int(user_id))

    try:
        items = await _POOL.run(_fetch, only=only)
        if not items:
            return []
        stories = [_story_to_dict(s) for s in items]
        stories = [s for s in stories if s.get("url")]
        logger.info(f"✅ get_stories @{username}: {len(stories)} шт.")
        return stories
    except PrivateAccount:
        _follow_lost(username, only)
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
    only = _access(username, info)
    if only is False:
        return {"posts": [], "next_cursor": "", "has_more": False, "private": True}

    user_id = _pk_for(username, info)
    if not user_id:
        return {"posts": [], "next_cursor": "", "has_more": False}
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
                res = await _POOL.run(_fetch, only=only)
            except PrivateAccount:
                _follow_lost(username, only)
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
    if not info:
        return None
    only = _access(username, info)
    user_id = _pk_for(username, info)
    if only is False or not user_id:
        return None

    def _fetch(cl: Client):
        medias = cl.user_medias(int(user_id), amount=amount)
        return [_media_to_post(m) for m in medias if m]

    try:
        posts = await _POOL.run(_fetch, only=only)
    except PrivateAccount:
        _follow_lost(username, only)
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
        user_pk = int(pk) if pk else int(_lookup_user(cl, username)["id"])
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

# yt-dlp / embed как запасной путь: работает, даже когда все аккаунты пула в 429 / с мёртвыми куками.
# IG_YTDLP_FIRST=1 — наоборот, пробовать yt-dlp ПЕРВЫМ (бережёт лимиты доноров, но без точных лайков/музыки).
IG_YTDLP_FIRST = os.getenv("IG_YTDLP_FIRST", "1") == "1"


async def _embed_post(url: str) -> dict | None:
    """Публичный /embed/captioned — последний шанс для публичных постов и рилсов."""
    import re as _re
    import aiohttp
    m = _re.search(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    sc = m.group(1)
    headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://www.instagram.com/p/{sc}/embed/captioned/", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                html = await r.text()
    except Exception as e:
        logger.debug(f"embed {sc}: {e}")
        return None

    def _unesc(x: str) -> str:
        try:
            return json.loads('"' + x + '"')
        except Exception:
            return x.replace("\\u0026", "&").replace("\\/", "/")

    vm = _re.search(r'"video_url":"([^"]+)"', html)
    im = (_re.search(r'"display_url":"([^"]+)"', html)
          or _re.search(r'class="EmbeddedMediaImage"[^>]+src="([^"]+)"', html))
    um = _re.search(r'"username":"([^"]+)"', html)
    thumb = _unesc(im.group(1)).replace("&amp;", "&") if im else ""
    if vm:
        media = [{"type": "video", "url": _unesc(vm.group(1)), "thumb": thumb}]
    elif thumb:
        media = [{"type": "photo", "url": thumb, "thumb": thumb}]
    else:
        return None
    return {
        "id": sc, "shortcode": sc, "timestamp": 0, "caption": "", "like_count": 0,
        "comment_count": 0, "media": media, "username": um.group(1) if um else "",
        "is_reel": "/reel" in url, "music": None, "fallback": "embed",
    }


async def _media_fallback(url: str) -> dict | None:
    try:
        from media_downloader import ytdlp_instagram_post
        post = await ytdlp_instagram_post(url)
        if post and post.get("media"):
            logger.info(f"get_media_by_url: yt-dlp-фоллбэк сработал для {url[:70]}")
            return post
    except Exception as e:
        logger.warning(f"yt-dlp fallback {url[:70]}: {e}")
    post = await _embed_post(url)
    if post:
        logger.info(f"get_media_by_url: embed-фоллбэк сработал для {url[:70]}")
    return post


async def get_media_by_url(url: str) -> dict | None:
    """
    Достать пост/рилс/IGTV по ссылке.
    Возвращает наш dict поста (как в get_posts) + username, is_reel, music,
    либо None если не получилось.
    Порядок: пул instagrapi → yt-dlp → embed (при IG_YTDLP_FIRST=1 — yt-dlp раньше пула).
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

    if IG_YTDLP_FIRST:
        post = await _media_fallback(url)
        if post:
            return post
    try:
        post = await _POOL.run(_fetch)
        if post and post.get("media"):
            return post
    except Exception as e:
        logger.warning(f"get_media_by_url {url[:80]}: {e}")
    fl = ig_follow_any_logins()
    if fl:
        try:
            post = await _POOL.run(_fetch, only=fl)
            if post and post.get("media"):
                return post
        except Exception as e:
            logger.info(f"get_media_by_url (через подписанные акки) {url[:80]}: {e}")
    if IG_YTDLP_FIRST:
        return None
    return await _media_fallback(url)


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
    if not info:
        return []
    only = _access(username, info)
    uid = _pk_for(username, info)
    if only is False or not uid:
        return []

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
        res = await _POOL.run(_fetch, only=only)
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
    # Миграция: текст БИО и имени для отслеживания изменений (0 доп. запросов к IG)
    for col in ("biography TEXT", "full_name TEXT"):
        try:
            c.execute(f"ALTER TABLE ig_user_state ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # Кэш профилей на диске (устаревшая карточка, подсказки при опечатке)
    c.execute("""CREATE TABLE IF NOT EXISTS ig_profile_cache (
        username TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL
    )""")
    c.execute("DELETE FROM ig_profile_cache WHERE ts < ?",
              (time.time() - max(IG_STALE_MAX_DAYS, 1) * 2 * 86400,))
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
    # Подписки донор-аккаунтов бота на профили (нужны для приватных)
    c.execute("""CREATE TABLE IF NOT EXISTS ig_follows (
        ig_username  TEXT NOT NULL,
        login        TEXT NOT NULL,
        pk           TEXT DEFAULT '',
        status       TEXT NOT NULL,          -- pending | following | lost
        requested_by TEXT DEFAULT '',
        ts           REAL DEFAULT 0,
        PRIMARY KEY (ig_username, login)
    )""")
    # Закладки пользователей бота
    c.execute("""CREATE TABLE IF NOT EXISTS ig_bookmarks (
        user_id   TEXT NOT NULL,
        shortcode TEXT NOT NULL,
        username  TEXT DEFAULT '',
        caption   TEXT DEFAULT '',
        created   REAL NOT NULL,
        PRIMARY KEY (user_id, shortcode)
    )""")
    # Журнал действий донор-аккаунтов (для дневных лимитов) и кто лайкнул пост
    c.execute("""CREATE TABLE IF NOT EXISTS ig_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL, shortcode TEXT DEFAULT '', login TEXT DEFAULT '',
        user_id TEXT DEFAULT '', ts REAL NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ig_likes (
        shortcode TEXT PRIMARY KEY, login TEXT NOT NULL, ts REAL NOT NULL
    )""")
    conn.commit()
    conn.close()
    if not _POOL._initialized:
        _POOL._accounts = _merge_accounts(_POOL._accounts)   # добавленные/удалённые из бота
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
    "biography", "full_name",
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
        "biography":        r[7],      # None — ещё не запоминали
        "full_name":        r[8],
    }


_STATE_COLS = ("ig_username, pk, media_count, last_posts_fetch, is_private, notfound_count, is_gone, "
               "biography, full_name")


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
                f"INSERT OR REPLACE INTO ig_user_state ({_STATE_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                (new, st[1], st[2], st[3], st[4], 0, 0, st[7], st[8]),
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


# ══════════════════════════════════════════════════════════════════════════════
#  ПОДПИСКА БОТА НА АККАУНТЫ (приватные и обычные), ЛАЙКИ, ЗАКЛАДКИ
# ══════════════════════════════════════════════════════════════════════════════
# Подписывается не «бот», а его донор-аккаунты. Приватный профиль виден только тому
# донору, чью заявку владелец одобрил, поэтому запросы к такому профилю идут через
# pool.run(..., only={логины}) — ротация не должна уводить запрос на аккаунт без доступа.
IG_FOLLOW_ALL      = os.getenv("IG_FOLLOW_ALL", "0") == "1"      # на обычные тоже подписывать всеми донорами
IG_FOLLOW_PER_DAY  = int(os.getenv("IG_FOLLOW_PER_DAY", "8"))   # подписок в сутки на ОДИН донор
IG_LIKE_PER_DAY    = int(os.getenv("IG_LIKE_PER_DAY", "40"))    # лайков в сутки на ОДИН донор
IG_BOOKMARK_ON_IG  = os.getenv("IG_BOOKMARK_ON_IG", "0") == "1"  # дублировать закладку в «Сохранённое» донора


def _norm_u(u: str) -> str:
    return (u or "").lower().strip().strip("@").strip()


def _dbx():
    return sqlite3.connect(IG_DB)


# ── ПАУЗА ДЕЙСТВИЙ ДОНОРА (action block) ─────────────────────────────────────
# Когда Instagram режет лайки/подписки у донора («feedback_required», «Action Blocked»),
# мы НЕ долбим дальше (это продлевает блок), а ставим донора на паузу только для действий.
# Просмотры/истории/посты продолжают работать. Каждый повторный блок — пауза дольше.
IG_BLOCK_HOURS        = float(os.getenv("IG_BLOCK_HOURS", "12"))        # первая пауза, ч
IG_BLOCK_MAX_HOURS    = float(os.getenv("IG_BLOCK_MAX_HOURS", "72"))    # потолок паузы, ч
IG_BLOCK_STRIKE_RESET = float(os.getenv("IG_BLOCK_STRIKE_RESET_DAYS", "7")) * 86400

_on_action_block: Optional[Callable] = None


def set_action_block_callback(cb: Callable) -> None:
    """Регистрирует async callback(login, until, hours, reason) — вызывается при новой паузе донора."""
    global _on_action_block
    _on_action_block = cb


def _blk_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(IG_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS ig_action_blocks (
        login   TEXT PRIMARY KEY,
        until   REAL NOT NULL DEFAULT 0,
        strikes INTEGER NOT NULL DEFAULT 0,
        reason  TEXT DEFAULT '',
        ts      REAL NOT NULL DEFAULT 0
    )""")
    return conn


_BLOCK_MARKERS = ("feedback_required", "action blocked", "action_blocked",
                  "we restrict certain activity", "temporarily blocked")


def _is_action_block(exc: Exception) -> bool:
    """Это блок действий (лайк/подписка), а не 429 и не протухшая сессия."""
    name = type(exc).__name__
    if name in ("PleaseWaitFewMinutes", "RateLimitError", "LoginRequired"):
        return False
    if name == "FeedbackRequired":
        return True
    low = str(exc).lower()
    return any(m in low for m in _BLOCK_MARKERS)


def ig_blocks_all() -> list[dict]:
    """Все записи пауз (в т.ч. истёкшие — фильтруй по b['until'] > time.time())."""
    conn = _blk_conn()
    try:
        rows = conn.execute("SELECT login, until, strikes, reason, ts FROM ig_action_blocks "
                            "ORDER BY until DESC").fetchall()
    finally:
        conn.close()
    return [{"login": r[0], "until": float(r[1]), "strikes": int(r[2]),
             "reason": r[3] or "", "ts": float(r[4])} for r in rows]


def ig_blocked_map() -> dict[str, float]:
    """{login: until} только для действующих пауз."""
    now = time.time()
    return {b["login"]: b["until"] for b in ig_blocks_all() if b["until"] > now}


def ig_blocked_logins() -> set:
    return set(ig_blocked_map())


def ig_block_clear(login: str | None = None) -> None:
    """Снять паузу у одного донора (или у всех, если login=None). Страйки сохраняются."""
    conn = _blk_conn()
    try:
        if login:
            conn.execute("UPDATE ig_action_blocks SET until=0 WHERE login=?", (login,))
        else:
            conn.execute("UPDATE ig_action_blocks SET until=0")
        conn.commit()
    finally:
        conn.close()


def _note_action_block(login: str, reason: str = "") -> float:
    """Поставить донора на паузу действий. Возвращает timestamp конца паузы."""
    now = time.time()
    conn = _blk_conn()
    try:
        r = conn.execute("SELECT until, strikes, ts FROM ig_action_blocks WHERE login=?", (login,)).fetchone()
        if r and float(r[0]) > now:
            return float(r[0])          # уже на паузе — не удлиняем и не алертим повторно
        strikes = int(r[1]) + 1 if (r and now - float(r[2]) < IG_BLOCK_STRIKE_RESET) else 1
        hours = min(IG_BLOCK_HOURS * (2 ** (strikes - 1)), IG_BLOCK_MAX_HOURS)
        until = now + hours * 3600
        conn.execute("INSERT OR REPLACE INTO ig_action_blocks (login, until, strikes, reason, ts) "
                     "VALUES (?,?,?,?,?)", (login, until, strikes, (reason or "")[:300], now))
        conn.commit()
    finally:
        conn.close()
    logger.warning(f"[action-block] @{login}: пауза действий {hours:.0f} ч (страйк {strikes}): {reason[:150]}")
    if _on_action_block:
        try:
            asyncio.ensure_future(_on_action_block(login, until, hours, reason or ""))
        except Exception:
            pass
    return until


# ── подписки доноров ─────────────────────────────────────────────────────────

def ig_follow_rows(username: str) -> list[dict]:
    conn = _dbx()
    try:
        rs = conn.execute("SELECT login, pk, status, requested_by, ts FROM ig_follows WHERE ig_username=?",
                          (_norm_u(username),)).fetchall()
    finally:
        conn.close()
    return [{"login": r[0], "pk": r[1], "status": r[2], "requested_by": r[3], "ts": r[4]} for r in rs]


def ig_follow_state(username: str) -> str:
    """none | pending | following"""
    st = {r["status"] for r in ig_follow_rows(username)}
    return "following" if "following" in st else ("pending" if "pending" in st else "none")


def ig_follow_logins(username: str, status: str = "following") -> list[str]:
    return [r["login"] for r in ig_follow_rows(username) if r["status"] == status]


def ig_follow_any_logins() -> set:
    conn = _dbx()
    try:
        rs = conn.execute("SELECT DISTINCT login FROM ig_follows WHERE status='following'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rs}


def ig_follow_set(username: str, login: str, status: str, pk: str = "", by: str = "") -> None:
    conn = _dbx()
    try:
        old = conn.execute("SELECT pk, requested_by FROM ig_follows WHERE ig_username=? AND login=?",
                           (_norm_u(username), login)).fetchone()
        pk = pk or (old[0] if old else "")
        by = by or (old[1] if old else "")
        conn.execute("INSERT OR REPLACE INTO ig_follows (ig_username, login, pk, status, requested_by, ts) "
                     "VALUES (?,?,?,?,?,?)", (_norm_u(username), login, pk, status, by, time.time()))
        conn.commit()
    finally:
        conn.close()


def ig_follow_del(username: str, login: str | None = None) -> None:
    conn = _dbx()
    try:
        if login:
            conn.execute("DELETE FROM ig_follows WHERE ig_username=? AND login=?", (_norm_u(username), login))
        else:
            conn.execute("DELETE FROM ig_follows WHERE ig_username=?", (_norm_u(username),))
        conn.commit()
    finally:
        conn.close()


def _follow_lost(username: str, only) -> None:
    """Приватный профиль перестал отвечать подписанному донору — подписку сняли."""
    if not only:
        return
    for lg in only:
        try:
            ig_follow_set(username, lg, "lost")
        except Exception:
            pass
    _ucache_drop_quiet(username)
    logger.warning(f"[follow] @{username}: доступ потерян у {sorted(only)}")


def _ucache_drop_quiet(username: str) -> None:
    try:
        _USER_CACHE.pop(_norm_u(username), None)
    except Exception:
        pass


def _access(username: str, info: dict | None):
    """None — подойдёт любой донор; set(логины) — только они; False — приватный, доступа нет."""
    if not info or not info.get("is_private"):
        return None
    lg = ig_follow_logins(username, "following")
    return set(lg) if lg else False


def _pk_for(username: str, info: dict | None) -> str:
    pk = str((info or {}).get("id") or "")
    if pk:
        return pk
    for r in ig_follow_rows(username):
        if r.get("pk"):
            return r["pk"]
    return ""


# ── журнал действий / лимиты ─────────────────────────────────────────────────

def ig_act_log(action: str, shortcode: str = "", login: str = "", user_id: str = "") -> None:
    conn = _dbx()
    try:
        conn.execute("INSERT INTO ig_actions (action, shortcode, login, user_id, ts) VALUES (?,?,?,?,?)",
                     (action, shortcode, login, str(user_id), time.time()))
        conn.execute("DELETE FROM ig_actions WHERE ts < ?", (time.time() - 3 * 86400,))
        conn.commit()
    finally:
        conn.close()


def ig_act_count(action: str, since: float, login: str | None = None) -> int:
    conn = _dbx()
    try:
        if login:
            r = conn.execute("SELECT COUNT(*) FROM ig_actions WHERE action=? AND ts>=? AND login=?",
                             (action, since, login)).fetchone()
        else:
            r = conn.execute("SELECT COUNT(*) FROM ig_actions WHERE action=? AND ts>=?", (action, since)).fetchone()
    finally:
        conn.close()
    return int(r[0] if r else 0)


# ── закладки ─────────────────────────────────────────────────────────────────

def ig_bm_has(user_id: str, shortcode: str) -> bool:
    conn = _dbx()
    try:
        return conn.execute("SELECT 1 FROM ig_bookmarks WHERE user_id=? AND shortcode=?",
                            (str(user_id), shortcode)).fetchone() is not None
    finally:
        conn.close()


def ig_bm_add(user_id: str, shortcode: str, username: str = "", caption: str = "") -> None:
    conn = _dbx()
    try:
        conn.execute("INSERT OR REPLACE INTO ig_bookmarks (user_id, shortcode, username, caption, created) "
                     "VALUES (?,?,?,?,?)", (str(user_id), shortcode, _norm_u(username), caption[:200], time.time()))
        conn.commit()
    finally:
        conn.close()


def ig_bm_del(user_id: str, shortcode: str) -> None:
    conn = _dbx()
    try:
        conn.execute("DELETE FROM ig_bookmarks WHERE user_id=? AND shortcode=?", (str(user_id), shortcode))
        conn.commit()
    finally:
        conn.close()


def ig_bm_count(user_id: str) -> int:
    conn = _dbx()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM ig_bookmarks WHERE user_id=?", (str(user_id),)).fetchone()[0])
    finally:
        conn.close()


def ig_bm_list(user_id: str, offset: int = 0, limit: int = 8) -> list[dict]:
    conn = _dbx()
    try:
        rs = conn.execute("SELECT shortcode, username, caption, created FROM ig_bookmarks WHERE user_id=? "
                          "ORDER BY created DESC LIMIT ? OFFSET ?", (str(user_id), limit, offset)).fetchall()
    finally:
        conn.close()
    return [{"shortcode": r[0], "username": r[1], "caption": r[2], "created": r[3]} for r in rs]


# ── лайки ────────────────────────────────────────────────────────────────────

def ig_like_get(shortcode: str) -> str | None:
    conn = _dbx()
    try:
        r = conn.execute("SELECT login FROM ig_likes WHERE shortcode=?", (shortcode,)).fetchone()
    finally:
        conn.close()
    return r[0] if r else None


def _ig_like_put(shortcode: str, login: str | None) -> None:
    conn = _dbx()
    try:
        if login:
            conn.execute("INSERT OR REPLACE INTO ig_likes (shortcode, login, ts) VALUES (?,?,?)",
                         (shortcode, login, time.time()))
        else:
            conn.execute("DELETE FROM ig_likes WHERE shortcode=?", (shortcode,))
        conn.commit()
    finally:
        conn.close()


# ── операции в Instagram ─────────────────────────────────────────────────────

def _rel_status(cl, pk: str, private: bool, followed_ok: bool | None = None) -> str:
    """Реальный статус подписки: following / pending / '' (нет)."""
    try:
        fr = cl.user_friendship_v1(int(pk))
        if getattr(fr, "following", False):
            return "following"
        if getattr(fr, "outgoing_request", False):
            return "pending"
        return ""
    except Exception as e:
        logger.info(f"[follow] friendship_v1 {pk}: {e}")
        if followed_ok is None:
            return ""
        return "following" if followed_ok and not private else ("pending" if private else ("following" if followed_ok else ""))


async def follow_user(username: str, requested_by: str = "") -> dict:
    """
    Подписать донор-аккаунты на профиль.
    Приватный → со ВСЕХ живых доноров (иначе после ротации доступ пропадёт), обычный →
    с одного (все — если IG_FOLLOW_ALL=1). Приватный вернёт status='pending' до одобрения.
    """
    username = _norm_u(username)
    status, info = await get_user_status(username, force=True)
    if status == "notfound":
        return {"ok": False, "error": "notfound"}
    if not info:
        return {"ok": False, "error": "unavailable"}
    pk = _pk_for(username, info)
    if not pk:
        return {"ok": False, "error": "nopk"}
    private = bool(info.get("is_private"))

    await _POOL._ensure_init()
    live_all = _POOL.live_logins()
    if not live_all:
        return {"ok": False, "error": "nodonors"}
    blocked = ig_blocked_map()
    live = [l for l in live_all if l not in blocked]
    if not live:
        return {"ok": False, "error": "blocked", "until": min(blocked[l] for l in live_all)}
    if private or IG_FOLLOW_ALL:
        logins = list(live)
    else:
        cur = _POOL._accounts[_POOL._current % len(_POOL._accounts)][0] if _POOL._accounts else live[0]
        logins = [cur if cur in live else live[0]]

    since = time.time() - 86400
    done: dict[str, str] = {}
    errors: list[str] = []
    skipped_limit = 0
    block_until: float | None = None
    for lg in logins:
        if ig_act_count("follow", since, lg) >= _day_limit(IG_FOLLOW_PER_DAY, lg):
            skipped_limit += 1
            continue

        def _do(cl, _pk=pk, _priv=private):
            try:
                ok = cl.user_follow(int(_pk))
            except Exception as e:
                if _is_action_block(e):
                    return ("__block__", str(e))
                raise
            return _rel_status(cl, _pk, _priv, bool(ok))

        try:
            st = await _POOL.run(_do, only={lg})
        except Exception as e:
            errors.append(f"{lg}: {type(e).__name__}: {e}"[:200])
            continue
        if isinstance(st, tuple) and st and st[0] == "__block__":
            u = _note_action_block(lg, st[1])
            block_until = u if block_until is None else min(block_until, u)
            errors.append(f"{lg}: блок действий")
            continue
        if st in ("following", "pending"):
            ig_follow_set(username, lg, st, pk=pk, by=requested_by)
            ig_act_log("follow", username, lg, requested_by)
            done[lg] = st
        else:
            errors.append(f"{lg}: Instagram не подтвердил подписку")

    if not done:
        if block_until is not None:
            return {"ok": False, "error": "blocked", "until": block_until, "errors": errors}
        return {"ok": False, "error": "limit" if skipped_limit == len(logins) else "failed", "errors": errors}
    best = "following" if "following" in done.values() else "pending"
    _ucache_drop_quiet(username)
    return {"ok": True, "status": best, "logins": done, "private": private, "errors": errors}


async def unfollow_user(username: str) -> dict:
    username = _norm_u(username)
    rows = [r for r in ig_follow_rows(username)]
    if not rows:
        return {"ok": True, "removed": 0}
    removed, errors = 0, []
    for r in rows:
        pk = r.get("pk")
        if not pk:
            ig_follow_del(username, r["login"])
            removed += 1
            continue

        def _do(cl, _pk=pk):
            return cl.user_unfollow(int(_pk))

        try:
            res = await _POOL.run(_do, only={r["login"]})
        except Exception as e:
            errors.append(f"{r['login']}: {e}"[:200])
            continue
        if res is None:
            errors.append(f"{r['login']}: нет ответа")
            continue
        ig_follow_del(username, r["login"])
        removed += 1
    _ucache_drop_quiet(username)
    return {"ok": removed > 0, "removed": removed, "errors": errors}


async def refresh_pending_follows() -> list[dict]:
    """
    Проверить заявки «pending»: одобрили → following, отклонили/отозвали → удаляем.
    Возвращает изменения [{username, login, new, requested_by}].
    """
    conn = _dbx()
    try:
        rows = conn.execute("SELECT ig_username, login, pk, requested_by FROM ig_follows "
                            "WHERE status='pending'").fetchall()
    finally:
        conn.close()
    changes: list[dict] = []
    for uname, lg, pk, by in rows:
        if not pk:
            continue

        def _do(cl, _pk=pk):
            fr = cl.user_friendship_v1(int(_pk))
            if getattr(fr, "following", False):
                return "following"
            if getattr(fr, "outgoing_request", False):
                return "pending"
            return "declined"

        try:
            st = await _POOL.run(_do, only={lg})
        except Exception as e:
            logger.info(f"[follow] проверка заявки @{uname}/{lg}: {e}")
            continue
        if st == "following":
            ig_follow_set(uname, lg, "following")
            changes.append({"username": uname, "login": lg, "new": "following", "requested_by": by})
        elif st == "declined":
            ig_follow_del(uname, lg)
            changes.append({"username": uname, "login": lg, "new": "declined", "requested_by": by})
    if changes:
        for ch in changes:
            _ucache_drop_quiet(ch["username"])
    return changes


async def _only_for_owner(username: str) -> tuple[set | None, str]:
    """Для действий над постом: (набор доноров | None=любой, ошибка)."""
    username = _norm_u(username)
    if not username:
        return None, ""
    info = await get_user_info(username)
    only = _access(username, info)
    if only is False:
        return None, "private"
    return only, ""


def _mid(cl, shortcode: str) -> str:
    return cl.media_id(cl.media_pk_from_code(shortcode))


async def like_toggle(shortcode: str, username: str = "", user_id: str = "") -> dict:
    """Поставить/снять лайк доноров. Лайк виден в Instagram от имени донор-аккаунта."""
    if not shortcode:
        return {"ok": False, "error": "nocode"}
    cur = ig_like_get(shortcode)
    if cur:
        bm = ig_blocked_map()
        if cur in bm:
            return {"ok": False, "error": "blocked", "until": bm[cur]}

        def _un(cl):
            try:
                return cl.media_unlike(_mid(cl, shortcode))
            except Exception as e:
                if _is_action_block(e):
                    return ("__block__", str(e))
                raise
        try:
            r = await _POOL.run(_un, only={cur})
        except Exception as e:
            logger.warning(f"unlike {shortcode}: {e}")
            return {"ok": False, "error": "failed"}
        if isinstance(r, tuple) and r and r[0] == "__block__":
            return {"ok": False, "error": "blocked", "until": _note_action_block(cur, r[1])}
        if r is None:
            return {"ok": False, "error": "failed"}
        _ig_like_put(shortcode, None)
        ig_act_log("unlike", shortcode, cur, user_id)
        return {"ok": True, "liked": False}

    only, err = await _only_for_owner(username)
    if err:
        return {"ok": False, "error": err}
    await _POOL._ensure_init()
    live = _POOL.live_logins()
    pool0 = [l for l in live if (not only or l in only)]
    bm = ig_blocked_map()
    pool1 = [l for l in pool0 if l not in bm]
    if pool0 and not pool1:
        return {"ok": False, "error": "blocked", "until": min(bm[l] for l in pool0)}
    since = time.time() - 86400
    pool = [l for l in pool1 if ig_act_count("like", since, l) < _day_limit(IG_LIKE_PER_DAY, l)]
    if not pool:
        return {"ok": False, "error": "limit"}

    def _do(cl):
        try:
            ok = cl.media_like(_mid(cl, shortcode))
        except Exception as e:
            if _is_action_block(e):
                return ("__block__", _acc_login(cl), str(e))
            raise
        return (bool(ok), _acc_login(cl))

    try:
        r = await _POOL.run(_do, only=set(pool))
    except Exception as e:
        logger.warning(f"like {shortcode}: {e}")
        return {"ok": False, "error": "failed"}
    if r and r[0] == "__block__":
        return {"ok": False, "error": "blocked", "until": _note_action_block(r[1], r[2])}
    if not r or not r[0]:
        return {"ok": False, "error": "failed"}
    _ig_like_put(shortcode, r[1])
    ig_act_log("like", shortcode, r[1], user_id)
    return {"ok": True, "liked": True}


async def bookmark_toggle(user_id: str, shortcode: str, username: str = "", caption: str = "") -> dict:
    """Закладка в боте (у каждого пользователя своя). Опционально дублируется в «Сохранённое» донора."""
    if not shortcode:
        return {"ok": False, "error": "nocode"}
    if ig_bm_has(user_id, shortcode):
        ig_bm_del(user_id, shortcode)
        saved = False
    else:
        ig_bm_add(user_id, shortcode, username, caption)
        saved = True
    if IG_BOOKMARK_ON_IG:
        try:
            only, err = await _only_for_owner(username)
            if not err:
                def _do(cl):
                    mid = _mid(cl, shortcode)
                    return cl.media_save(mid) if saved else cl.media_unsave(mid)
                await _POOL.run(_do, only=only)
        except Exception as e:
            logger.info(f"bookmark mirror {shortcode}: {e}")
    return {"ok": True, "saved": saved}
