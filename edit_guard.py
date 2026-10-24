"""
edit_guard.py — безопасное редактирование сообщений Telegram для анимаций.

Проблема: анимация = десяток правок одного сообщения за несколько секунд. В группе Telegram разрешает
порядка 20 правок/сообщений в минуту на чат. Две одновременные анимации (или несколько открытий подряд)
упираются в лимит → «Too Many Requests: retry after N». Раньше бот спал ≤5 с, продолжал слать кадры в
действующий лимит, а ИТОГОВОЕ сообщение с результатом терялось — анимация «зависала» на последнем кадре.

Как работает защита:
  • Для каждого чата ведётся счётчик правок в скользящем окне (группа: ≤16 за 60 с — с запасом под лимит
    Telegram ~20/мин, личка свободнее). Кадры анимации (final=False) необязательны: если окно почти заполнено,
    чат в флуд-паузе или прошло слишком мало времени с прошлой правки этого сообщения (0.7 с в группах) — кадр молча пропускается
    (анимация станет «рваной», но не зависнет).
  • У кадров есть резерв: последние места в окне берегутся для ИТОГОВЫХ правок (final=True).
  • Итоговая правка (результат, экран с кнопками) никогда не пропускается: она ждёт токен/окончания флуд-паузы
    (до MAX_FINAL_WAIT секунд) и повторяется после «retry after», сетевых ошибок и таймаутов.
  • Любой вызов Telegram ограничен таймаутом, чтобы обработчик не висел бесконечно.
"""

import asyncio
import logging
import re
import time
from collections import deque

logger = logging.getLogger(__name__)

ENABLED = True               # выключается в тестах
GROUP = {"window": 60.0, "limit": 16, "reserve": 4, "msg_gap": 0.7}     # группы: не больше 16 правок за 60 с (кадры — первые 12)
PRIVATE = {"window": 10.0, "limit": 14, "reserve": 3, "msg_gap": 0.35}  # личка и inline
MAX_FINAL_WAIT = 90.0        # сколько максимум ждём ради итоговой правки (лучше пауза, чем потерянный результат)
EDIT_TIMEOUT = 15.0          # таймаут одного запроса к Telegram
MAX_RETRY_AFTER = 120.0

_now = time.monotonic
_sleep = asyncio.sleep
STATS = {"frames_ok": 0, "frames_dropped": 0, "finals_ok": 0, "finals_failed": 0, "floods": 0}


class _Bucket:
    """Скользящее окно: времена последних правок чата + время конца флуд-паузы."""
    __slots__ = ("times", "blocked_until")

    def __init__(self):
        self.times: deque = deque()
        self.blocked_until = 0.0

    def purge(self, now: float, cfg: dict):
        while self.times and now - self.times[0] >= cfg["window"]:
            self.times.popleft()

    def free(self, now: float, cfg: dict) -> int:
        self.purge(now, cfg)
        return cfg["limit"] - len(self.times)

    def next_free_in(self, now: float, cfg: dict) -> float:
        self.purge(now, cfg)
        if len(self.times) < cfg["limit"]:
            return 0.0
        return self.times[0] + cfg["window"] - now


_buckets: dict = {}
_last_msg: dict = {}


def _chat_key(msg):
    cid = getattr(getattr(msg, "chat", None), "id", None)
    if cid is not None:
        return cid
    return getattr(msg, "_id", None) or id(msg)          # inline-сообщение: ключ — inline_message_id


def _cfg(key) -> dict:
    return GROUP if isinstance(key, int) and key < 0 else PRIVATE


def _bucket(key, now: float) -> _Bucket:
    b = _buckets.get(key)
    if b is None:
        if len(_buckets) > 2000:                          # чистим простаивающие
            for k in [k for k, v in _buckets.items() if not v.times or now - v.times[-1] > 600]:
                _buckets.pop(k, None)
        b = _buckets[key] = _Bucket()
    return b


def retry_after(e: Exception):
    ra = getattr(e, "retry_after", None)
    if isinstance(ra, (int, float)):
        return min(float(ra), MAX_RETRY_AFTER)
    m = re.search(r"retry after (\d+)", str(e).lower())
    return min(float(m.group(1)), MAX_RETRY_AFTER) if m else None


_DEAD = ("message to edit not found", "message can't be edited", "message_id_invalid", "chat not found",
         "bot was blocked", "bot was kicked", "message is not accessible")


async def _try(msg, text, markup, parse_mode, b: _Bucket, mk) -> str:
    """ok | flood | retry | dead | fail"""
    try:
        await asyncio.wait_for(msg.edit_text(text, parse_mode=parse_mode, reply_markup=markup), EDIT_TIMEOUT)
        _last_msg[mk] = _now()
        if len(_last_msg) > 6000:
            cut = _now() - 600
            for k in [k for k, t in _last_msg.items() if t < cut]:
                _last_msg.pop(k, None)
        return "ok"
    except asyncio.TimeoutError:
        return "retry"
    except Exception as e:                                  # noqa: BLE001 — разбираем по тексту, типы aiogram не импортируем
        err = str(e).lower()
        if "not modified" in err:
            return "ok"
        ra = retry_after(e)
        if ra is not None:
            STATS["floods"] += 1
            b.blocked_until = _now() + ra + 0.3
            logger.warning("Telegram flood-limit: пауза %.0f с для чата", ra)
            return "flood"
        if any(s in err for s in _DEAD):
            return "dead"
        name = type(e).__name__.lower()
        if any(s in name for s in ("network", "timeout", "connection", "clienterror", "oserror")) or "timed out" in err:
            return "retry"
        logger.warning("edit не удался: %s", e)
        return "fail"


async def safe_edit(msg, text: str, reply_markup=None, *, final: bool = True, parse_mode: str = "HTML") -> bool:
    """final=False — кадр анимации (можно пропустить). final=True — результат/экран (доставляется обязательно)."""
    if not ENABLED:
        try:
            await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except Exception as e:                              # noqa: BLE001
            return "not modified" in str(e).lower()
    key = _chat_key(msg)
    mk = (key, getattr(msg, "message_id", None))
    cfg = _cfg(key)
    now = _now()
    b = _bucket(key, now)

    if not final:
        if (b.blocked_until > now or now - _last_msg.get(mk, -1e9) < cfg["msg_gap"]
                or b.free(now, cfg) < 1 + cfg["reserve"]):
            STATS["frames_dropped"] += 1
            return False
        b.times.append(now)
        res = await _try(msg, text, reply_markup, parse_mode, b, mk)
        STATS["frames_ok" if res == "ok" else "frames_dropped"] += 1
        return res == "ok"

    deadline = now + MAX_FINAL_WAIT
    for _ in range(8):
        now = _now()
        wait = max(b.blocked_until - now, b.next_free_in(now, cfg), 0.0)
        if wait > 0:
            if now + wait > deadline:
                break
            await _sleep(wait + 0.05)
            continue
        b.times.append(now)
        res = await _try(msg, text, reply_markup, parse_mode, b, mk)
        if res == "ok":
            STATS["finals_ok"] += 1
            return True
        if res in ("dead", "fail"):
            break
        if res == "retry":
            await _sleep(1.0)
    STATS["finals_failed"] += 1
    logger.error("итоговая правка не доставлена (флуд-лимит/сеть)")
    return False
