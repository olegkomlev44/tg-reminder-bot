"""
fun_handlers.py — рулетки и выбор.

  /pick кофе или чай, пицца      — выбрать из вариантов (случайный стиль анимации из 11)
  /pick Что заказать: пицца, суши — то же, с заголовком
  /pick -слот кофе или чай       — стиль вручную; список: /pick -стили
  (слот, рулетка, взлом, вылет, прожектор, скачки, карты, лото, бомба, турнир, оракул, колесо-GIF)
  /pick -3 а, б, в, г, д         — несколько победителей (подиум 🥇🥈🥉); то же: «3 из: а, б, в»
  /pick -диплом …                — сразу прислать картинку-диплом (кнопка «🖼 Диплом» есть всегда)
  Каждый жребий «фиксируется» хешем до анимации; кнопка «🔐 Проверка» раскрывает seed и формулу.
  Inline: «@бот p: кофе, чай» (префикс p:/pick:/п:/жребий: или суффикс « /p») — анимация прямо в чате
  /who кто моет посуду           — лобби: люди жмут «🙋 Участвую», потом честный жребий
  /who кто моет посуду: Глеб, Дима, Олег — жребий сразу среди названных
  /roll [N | NdM]                — кубик (по умолчанию d6)
  /coin                          — орёл/решка

«Честный» жребий: используется secrets.SystemRandom (криптостойкий ГСЧ ОС), а не random.
Лобби живут в памяти (после рестарта бота старые кнопки отвечают «устарело»).
Подключение: register_fun_handlers(dp) в main.py.
"""

import asyncio
import hashlib
import html
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent,
)

try:                     # картинки (диплом, GIF-колесо) — нужен Pillow и card_generator.py
    from pick_cards import make_pick_card, make_wheel_gif
    _CARDS_OK = True
except Exception:        # без Pillow /pick работает как раньше, просто без диплома и колеса
    _CARDS_OK = False

logger = logging.getLogger(__name__)

_RNG = secrets.SystemRandom()
LOBBY_SECONDS = 90
MAX_OPTIONS = 30
_SPLIT_RE = re.compile(r"\s*(?:,|;|\||\n|\s+или\s+|\s+or\s+|\s+/\s+)\s*", re.IGNORECASE)


def _esc(s: str) -> str:
    return html.escape(str(s))


def parse_options(text: str) -> list[str]:
    """«кофе или чай, пицца» → ['кофе', 'чай', 'пицца'] (без пустых и дублей)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _SPLIT_RE.split(text or ""):
        o = raw.strip(" \t.!?«»\"'")
        if not o or o.lower() in seen:
            continue
        seen.add(o.lower())
        out.append(o[:60])
    return out[:MAX_OPTIONS]


def split_title_and_options(text: str) -> tuple[str, list[str]]:
    """«Что заказать: пицца, суши» → ('Что заказать', ['пицца','суши']); без «:» — заголовок пуст."""
    text = (text or "").strip()
    m = re.match(r"^([^:,;|\n]{2,80}):\s*(.+)$", text, re.DOTALL)
    if m:
        return m.group(1).strip(), parse_options(m.group(2))
    return "", parse_options(text)


# ── Хранилища в памяти ────────────────────────────────────────────────────────
_PICKS: "OrderedDict[str, dict]" = OrderedDict()      # token → {options, title}


def _remember_pick(options: list[str], title: str, forced: str | None = None, last: str | None = None) -> str:
    tok = secrets.token_hex(4)
    _PICKS[tok] = {"options": options, "title": title, "forced": forced, "last": last}
    while len(_PICKS) > 400:
        _PICKS.popitem(last=False)
    return tok


@dataclass
class Participant:
    key: str            # "u:<id>" для людей, "n:<имя>" для имён из списков
    name: str
    user_id: int | None = None


@dataclass
class Lobby:
    chat_id: int
    msg_id: int
    owner_id: int
    question: str
    created: float = field(default_factory=time.time)
    people: "OrderedDict[str, Participant]" = field(default_factory=OrderedDict)
    spinning: bool = False
    finished: bool = False


_LOBBIES: dict[str, Lobby] = {}


# ── Анимации ──────────────────────────────────────────────────────────────────
# Победитель выбирается ЗАРАНЕЕ криптостойким ГСЧ, анимация лишь «докручивается» до него.
# Стили: слот, рулетка, взлом, на вылет, прожектор, скачки, карты, лототрон, бомба, турнир, оракул.
# Telegram режет частые edit'ы, поэтому кадров немного (~8–12) и пауза между ними ≥0.5 с.
# Соседние кадры ВСЕГДА различаются (иначе Telegram отвечает «message is not modified»).

try:
    from edit_guard import safe_edit as _guard_edit      # рядом должен лежать edit_guard.py
except Exception:                                        # без него — прежнее (менее надёжное) поведение
    _guard_edit = None
    logging.getLogger(__name__).warning("edit_guard.py не найден — анимации могут зависать при флуд-лимитах Telegram. "
                                        "Положите edit_guard.py рядом с остальными модулями.")

ANIM_TIMEOUT = 45.0           # анимация дольше — прерываем и показываем результат (чтобы не «висело»)
_WHEEL_SEM = asyncio.Semaphore(2)    # GIF-колесо рисуется в потоке и грузит CPU — не больше двух одновременно


async def _safe_edit(msg: types.Message, text: str, reply_markup=None, *, final: bool | None = None) -> bool:
    """
    Правка сообщения. final=True — итог (доставляется обязательно: ждёт флуд-паузу и повторяется);
    final=False — кадр анимации (при перегрузке чата молча пропускается, чтобы анимация не зависала).
    По умолчанию итогом считается правка с кнопками.
    """
    if final is None:
        final = reply_markup is not None
    if _guard_edit is not None:
        return await _guard_edit(msg, text, reply_markup, final=final)
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return True
    except Exception as e:
        err = str(e).lower()
        if "retry after" in err:
            m = re.search(r"retry after (\d+)", err)
            await asyncio.sleep(min(int(m.group(1)) if m else 2, 5))
        return False


def _cut(s: str, n: int = 28) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _head(icon: str, label: str, title: str) -> str:
    t = f"\n<b>{_esc(_cut(title, 70))}</b>" if title else ""
    return f"{icon} <b>{label}</b> {icon}{t}\n\n"


def _intro_text(title: str) -> str:
    t = f"\n<b>{_esc(_cut(title, 70))}</b>" if title else ""
    return f"🎲 <b>Готовим розыгрыш…</b>{t}\n\n🎰 · 🎡 · 🃏 · 🎱 · 🔮"


def _hop_seq(n: int, idx: int, hops: int) -> list[int]:
    """Хаотичные прыжки курсора: соседние кадры различаются, последний — победитель."""
    if n == 1:
        return [idx] * hops
    seq: list[int] = []
    prev_ = -1
    for k in range(hops):
        if k == hops - 1:
            cur = idx
        else:
            cur = _RNG.randrange(n)
            while cur == prev_ or (k == hops - 2 and cur == idx and n > 2):
                cur = _RNG.randrange(n)
        seq.append(cur)
        prev_ = cur
    return seq


# 1) 🎰 Слот-машина: рычаг → размытие → три барабана по очереди встают на 7️⃣ → джекпот
_SLOT_SYMS = ["🍒", "🍋", "🔔", "🍇", "💎", "⭐", "🍀", "🎲", "🍉"]


async def _anim_slot(msg, options, title, idx):
    n = len(options)
    delays = [0.6, 0.5, 0.5, 0.5, 0.55, 0.65, 0.8, 0.9, 1.0, 1.1, 1.25, 0.6]
    frames = len(delays)
    stops = (6, 8, 10)                       # кадры, на которых встают барабаны
    for k in range(frames):
        pos = (idx - (frames - 1 - k)) % n
        locked = sum(k >= s for s in stops)
        blur = k < 3
        syms = []
        for i in range(3):
            if i < locked:
                syms.append("7️⃣")
            elif blur:
                syms.append(_RNG.choice(["💫", "🌀", "✨"]))
            else:
                syms.append(_RNG.choice(_SLOT_SYMS))
        if k == 0:
            status = "🕹 дёргаем рычаг…"
        elif locked == 3:
            status = "💥 <b>ДЖЕКПОТ!</b> 💥"
        elif locked:
            status = f"🔒 встало барабанов: {locked}/3"
        else:
            status = "🌀 крутится…"
        lamps = "💡🔴" * 4 if k % 2 == 0 else "🔴💡" * 4
        prev_, cur, nxt = options[(pos - 1) % n], options[pos % n], options[(pos + 1) % n]
        text = (
            _head("🎰", "СЛОТ-МАШИНА", title)
            + f"{lamps}\n"
            + f"【 {syms[0]} │ {syms[1]} │ {syms[2]} 】\n"
            + "━━━━━━━━━━━━━━\n"
            + f"<code>   {_esc(_cut(prev_))}</code>\n"
            + f"▶️ <b>{_esc(_cut(cur))}</b> ◀️\n"
            + f"<code>   {_esc(_cut(nxt))}</code>\n"
            + "━━━━━━━━━━━━━━\n"
            + f"<i>{status}</i>"
        )
        await _safe_edit(msg, text)
        await asyncio.sleep(delays[k])


# 2) 🎡 Рулетка казино: ставки → быстрый разгон → замедление → подскок шарика → 🟩
async def _anim_roulette(msg, options, title, idx):
    n = len(options)

    def color(i: int) -> str:
        return "🟥" if i % 2 == 0 else "⬛"

    offsets = [None, -31, -24, -18, -13, -9, -6, -3, -2, 2, -1, 0]
    delays = [0.8, 0.5, 0.5, 0.55, 0.65, 0.8, 0.95, 1.05, 1.0, 0.9, 0.9, 0.8]
    notes = ["ставки сделаны, ставок больше нет", "разгоняем колесо", "разгоняем колесо", "крутится",
             "крутится", "замедляется", "замедляется", "почти…", "шарик скачет…",
             "шарик подпрыгнул…", "шарик колеблется…", "СТОП!"]
    for k, off in enumerate(offsets):
        pos = (idx + (off or 0)) % n
        last = k == len(offsets) - 1
        cells = []
        for d in range(-4, 5):
            c = "🟩" if (last and d == 0) else color((pos + d) % n)
            cells.append(f"【{c}】" if d == 0 else c)
        ball = "🟢" if last else ("🎱" if k else "💰")
        l_name = _esc(_cut(options[(pos - 1) % n], 12))
        r_name = _esc(_cut(options[(pos + 1) % n], 12))
        text = (
            _head("🎡", "РУЛЕТКА", title)
            + "".join(cells) + "\n"
            + f"{ball} <b>{_esc(_cut(options[pos]))}</b>  <i>(№{pos + 1})</i>\n"
            + f"<code>◂ {l_name} · {r_name} ▸</code>\n\n"
            + f"<i>{'◐◓◑◒'[k % 4]} {notes[k]}</i>"
        )
        await _safe_edit(msg, text)
        await asyncio.sleep(delays[k])


# 3) 🕶 Взлом: терминал с логами, буквы-«шум» расшифровываются в имя победителя
_GLYPHS = "█▓▒░#$%&@*+=?<>/\\|0123456789"
_HACK_LOGS = ["подключаюсь к серверу…", "порт 443 открыт", "обход фаервола", "инъекция пакета",
              "перебор ключей 2048 bit", "хеш найден", "расшифровка блока", "ключ подошёл"]


async def _anim_hack(msg, options, title, idx):
    winner = _cut(options[idx], 28)
    frames = 10
    order = [i for i, ch in enumerate(winner) if ch != " "]
    _RNG.shuffle(order)
    for k in range(frames):
        reveal = set(order[: round(len(order) * k / (frames - 1))])
        s = "".join(ch if (i in reveal or ch == " ") else _RNG.choice(_GLYPHS)
                    for i, ch in enumerate(winner))
        pct = int(100 * k / (frames - 1))
        filled = round(10 * pct / 100)
        bar = "▰" * filled + "▱" * (10 - filled)
        logs = _HACK_LOGS[max(0, min(k, len(_HACK_LOGS) - 1) - 2): min(k, len(_HACK_LOGS) - 1) + 1]
        log_txt = "\n".join(f"[✓] {l}" for l in logs)
        done = k == frames - 1
        text = (
            _head("🕶", "ВЗЛОМ СИСТЕМЫ", title)
            + f"<code>$ crack --target pick\n{_esc(log_txt)}\n&gt; {_esc(s)}</code>\n\n"
            + f"<code>{bar} {pct}%</code>\n"
            + ("🔓 <b>ДОСТУП ОТКРЫТ</b>" if done else "<i>🔐 идёт расшифровка…</i>")
        )
        await _safe_edit(msg, text)
        await asyncio.sleep(0.7 if not done else 0.3)


# 4) ⚔️ На вылет: варианты выбывают по одному/пачками, финал «один на один»
async def _anim_duel(msg, options, title, idx):
    n = len(options)
    losers = [i for i in range(n) if i != idx]
    _RNG.shuffle(losers)
    frames = min(len(losers), 8)
    chunks, start = [], 0
    for f in range(frames):
        size = len(losers) // frames + (1 if f < len(losers) % frames else 0)
        chunks.append(losers[start:start + size])
        start += size
    dead: set[int] = set()
    for k, chunk in enumerate(chunks):
        dead.update(chunk)
        alive = n - len(dead)
        lines = []
        for i, o in enumerate(options):
            if i in dead:
                lines.append(f"💀 <s>{_esc(_cut(o))}</s>")
            elif alive == 1:
                lines.append(f"👑 <b>{_esc(_cut(o))}</b>")
            else:
                lines.append(f"🟢 {_esc(_cut(o))}")
        if len(chunk) <= 3:
            event = "💥 выбыл: " + ", ".join(f"<b>{_esc(_cut(options[i], 16))}</b>" for i in chunk)
        else:
            event = f"💥 выбыло сразу: <b>{len(chunk)}</b>"
        if alive == 1:
            tail = "👑 <b>остался один!</b>"
        elif alive == 2:
            tail = f"{event}\n⚡ остались двое — финал!"
        else:
            tail = f"{event}\nв живых: <b>{alive}</b>"
        text = _head("⚔️", "НА ВЫЛЕТ", title) + "\n".join(lines) + f"\n\n<i>{tail}</i>"
        await _safe_edit(msg, text)
        await asyncio.sleep(0.9 + 0.1 * k)


# 5) 💡 Прожектор: лампочка хаотично прыгает по вариантам, прыжки редеют
async def _anim_spot(msg, options, title, idx):
    n = len(options)
    seq = _hop_seq(n, idx, 11)
    delays = [0.45, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.05, 1.2, 1.3]
    for k, cur in enumerate(seq):
        last = k == len(seq) - 1
        bulbs = ("💡⚫" * 5) if k % 2 == 0 else ("⚫💡" * 5)
        lamp = "🌟" if last else "💡"
        lines = [
            f"{lamp} <b>{_esc(_cut(o))}</b>" if i == cur else f"▫️ {_esc(_cut(o))}"
            for i, o in enumerate(options)
        ]
        text = _head("💡", "ПРОЖЕКТОР", title) + bulbs + "\n" + "\n".join(lines) + "\n" + bulbs
        await _safe_edit(msg, text)
        await asyncio.sleep(delays[k])


# 6) 🏇 Скачки: у каждого варианта своя дорожка, победитель первым пересекает финиш
async def _anim_race(msg, options, title, idx):
    n = len(options)
    L = 12 if n <= 8 else 8
    F = 10
    icons = ["🐎", "🏎", "🚀", "🐢", "🦄", "🛵", "🐇", "🚲"]
    traj: list[list[int]] = []
    for i in range(n):
        pos, last = [], 0
        sp = _RNG.uniform(0.75, 1.1)
        for k in range(F):
            if i == idx:
                t = L if k == F - 1 else int(L * (k + 1) / F + _RNG.uniform(-1.5, 1.0))
                t = L if k == F - 1 else max(last, min(L - 1, t))
            else:
                t = int((L - 1) * (k + 1) / F * sp + _RNG.uniform(-1, 1))
                t = max(last, min(L - 1, t))
            pos.append(t)
            last = t
        traj.append(pos)
    notes = ["🚦 старт!", "🚦 пошли!", "🏃 набирают ход", "🏃 набирают ход", "🔥 борьба идёт",
             "🔥 борьба идёт", "📣 финиш близко!", "📣 финиш близко!", "🏁 последний рывок!", "🏆 ФИНИШ!"]
    for k in range(F):
        lines = []
        for i, o in enumerate(options):
            p = traj[i][k]
            won = (k == F - 1 and i == idx)
            icon = "🏆" if won else icons[i % len(icons)]
            name = f"<b>{_esc(_cut(o, 14))}</b>" if won else _esc(_cut(o, 14))
            lines.append(f"{'┈' * p}{icon}{'┈' * (L - p)}🏁 {name}")
        text = (_head("🏇", "СКАЧКИ", title) + "\n".join(lines)
                + f"\n\n<i>{notes[k]} {'▸' * (k + 1)}</i>")
        await _safe_edit(msg, text)
        await asyncio.sleep(0.85 if k < F - 1 else 0.3)


# 7) 🃏 Карты: тасуем колоду, тянем карту, переворачиваем — на лице имя победителя
async def _anim_cards(msg, options, title, idx):
    n = len(options)
    suits = ["♠️", "♥️", "♦️", "♣️"]
    pre = [
        ("🂠 🂠 🂠 🂠 🂠", "🃏 тасуем колоду…"),
        ("🂠🂠🂠🂠🂠🂠🂠", "🃏 тасуем колоду…"),
        ("🂠  🂠  🂠  🂠", "✂️ снимаем…"),
        ("🂠 🂠 🂠", "👆 тянем карту…"),
        ("     🂠 ⟶", "👆 тянем карту…"),
    ]
    for k, (row, note) in enumerate(pre):
        await _safe_edit(msg, _head("🃏", "КАРТЫ", title) + f"{row}\n\n<i>{note} {'▸' * (k + 1)}</i>")
        await asyncio.sleep(0.7)
    flicker_prev = -1
    for k in range(4):
        j = _RNG.randrange(n)
        while j == flicker_prev and n > 1:
            j = _RNG.randrange(n)
        flicker_prev = j
        s = suits[k % 4]
        text = (_head("🃏", "КАРТЫ", title)
                + f"🎴 карта переворачивается…\n\n{s} <i>{_esc(_cut(options[j]))}</i> {s}"
                + f"\n\n<i>{'▸' * (6 + k)}</i>")
        await _safe_edit(msg, text)
        await asyncio.sleep(0.65 + 0.15 * k)
    s = _RNG.choice(suits)
    await _safe_edit(msg, _head("🃏", "КАРТЫ", title)
                     + f"🂡 <b>ТУЗ!</b>\n\n{s} <b>{_esc(_cut(options[idx]))}</b> {s}")
    await asyncio.sleep(0.5)


# 8) 🎱 Лототрон: шарики в барабане перемешиваются, один выкатывается с номером
_BALLS = ["🔴", "🟡", "🟢", "🔵", "🟣", "🟠", "⚪"]


async def _anim_lotto(msg, options, title, idx):
    def drum(note: str, spout: str = "", k: int = 0) -> str:
        rows = ["┃ " + " ".join(_RNG.choice(_BALLS) for _ in range(3)) + " ┃" for _ in range(3)]
        return (_head("🎱", "ЛОТОТРОН", title)
                + "╭━━━━━━━╮\n" + "\n".join(rows) + "\n╰━━━┳━━━╯\n"
                + f"      {spout or '┃'}\n\n<i>{note} {'▸' * (k + 1)}</i>")

    for k in range(7):
        await _safe_edit(msg, drum("барабан крутится…", k=k))
        await asyncio.sleep(0.55 + 0.05 * k)
    await _safe_edit(msg, drum("шар покатился…", "⚪", 7))
    await asyncio.sleep(0.8)
    await _safe_edit(msg, drum("вылетает!", "🎱", 8))
    await asyncio.sleep(0.9)
    await _safe_edit(msg, _head("🎱", "ЛОТОТРОН", title)
                     + f"🎱 <b>шар №{idx + 1}</b>\n\n<b>{_esc(_cut(options[idx]))}</b>")
    await asyncio.sleep(0.5)


# 9) 💣 Горячая картошка: бомба перелетает от варианта к варианту, фитиль горит, БУМ!
async def _anim_bomb(msg, options, title, idx):
    n = len(options)
    hops = 9
    seq = _hop_seq(n, idx, hops)
    delays = [0.5, 0.5, 0.55, 0.6, 0.7, 0.8, 0.95, 1.1, 1.3]
    for k, cur in enumerate(seq):
        last = k == hops - 1
        fuse = f"🧨{'━' * (hops - 1 - k)}{'💥' if last else '🔥'}"
        lines = []
        for i, o in enumerate(options):
            if i == cur:
                lines.append(f"{'💥' if last else '💣'} <b>{_esc(_cut(o))}</b>")
            else:
                lines.append(f"▫️ {_esc(_cut(o))}")
        note = "💥 БА-БАХ!" if last else ("😰 сейчас рванёт…" if k >= hops - 3 else "💨 бомба летит дальше!")
        text = _head("💣", "ГОРЯЧАЯ КАРТОШКА", title) + fuse + "\n\n" + "\n".join(lines) + f"\n\n<i>{note}</i>"
        await _safe_edit(msg, text)
        await asyncio.sleep(delays[k])


# 10) 🏟 Турнир: сетка плей-офф, в каждом раунде пары, победитель идёт дальше
async def _anim_cup(msg, options, title, idx):
    n = len(options)
    cur = list(range(n))
    _RNG.shuffle(cur)
    rnd = 0
    while len(cur) > 1:
        rnd += 1
        pairs, nxt, bye = [], [], None
        for a, b in zip(cur[0::2], cur[1::2]):
            w = idx if idx in (a, b) else _RNG.choice((a, b))
            pairs.append((a, b, w))
            nxt.append(w)
        if len(cur) % 2:
            bye = cur[-1]
            nxt.append(bye)
        label = {2: "ФИНАЛ", 4: "ПОЛУФИНАЛ", 8: "1/4 ФИНАЛА", 16: "1/8 ФИНАЛА"}.get(len(cur), f"РАУНД {rnd}")
        lines = []
        for a, b, w in pairs:
            na, nb = _esc(_cut(options[a], 13)), _esc(_cut(options[b], 13))
            lines.append(f"<b>{na}</b> ⚔ <s>{nb}</s> ✅" if w == a else f"<s>{na}</s> ⚔ <b>{nb}</b> ✅")
        if bye is not None:
            lines.append(f"🎟 <b>{_esc(_cut(options[bye], 13))}</b> — проходит без боя")
        text = _head("🏟", "ТУРНИР", title) + f"<b>{label}</b>\n" + "\n".join(lines) \
            + f"\n\n<i>дальше: {len(nxt)}</i>"
        await _safe_edit(msg, text)
        await asyncio.sleep(1.2 if len(cur) > 2 else 1.5)
        cur = nxt


# 11) 🔮 Шар-оракул: туман, мерцающие имена, духи сходятся на одном ответе
async def _anim_oracle(msg, options, title, idx):
    n = len(options)
    mist = "✧･ﾟ:*˙·"
    frames = 9
    prev_ = -1
    for k in range(frames):
        fog = "".join(_RNG.choice(mist) for _ in range(14))
        if k == frames - 1:
            body = f"🔮 <b>{_esc(_cut(options[idx]))}</b>"
            note = "✨ духи сказали своё слово"
        else:
            j = _RNG.randrange(n)
            while j == prev_ and n > 1:
                j = _RNG.randrange(n)
            prev_ = j
            fade = "<i>" if k < frames - 3 else "<b>"
            fade_c = "</i>" if k < frames - 3 else "</b>"
            body = f"🔮 {fade}{_esc(_cut(options[j]))}{fade_c}"
            note = ("🌫 духи совещаются…", "🌫 духи совещаются…", "🌫 туман сгущается…")[k % 3] if k < frames - 3 \
                else "🌟 ответ проступает…"
        text = _head("🔮", "ОРАКУЛ", title) + f"{fog}\n{body}\n{fog[::-1]}\n\n<i>{note} {'▸' * (k + 1)}</i>"
        await _safe_edit(msg, text)
        await asyncio.sleep(0.6 + 0.08 * k)


# 12) 🏅 Подиум: несколько победителей — места разыгрываются по очереди (с последнего к первому)
_MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
MAX_WINNERS = len(_MEDALS)


async def _anim_podium(msg, options, title, order):
    n, k = len(options), len(order)
    per = 4 if k <= 3 else 3 if k <= 5 else 2
    awarded: dict[int, int] = {}
    step = 0
    for place in range(k - 1, -1, -1):
        target = order[place]
        cand = [i for i in range(n) if i not in awarded]
        seq = [cand[j] for j in _hop_seq(len(cand), cand.index(target), per)]
        for h, cur in enumerate(seq):
            step += 1
            landed = h == len(seq) - 1
            lines = []
            for i, o in enumerate(options):
                if i in awarded:
                    lines.append(f"{_MEDALS[awarded[i]]} <b>{_esc(_cut(o))}</b>")
                elif i == cur and landed:
                    lines.append(f"{_MEDALS[place]} <b>{_esc(_cut(o))}</b>")
                elif i == cur:
                    lines.append(f"💡 <b>{_esc(_cut(o))}</b>")
                else:
                    lines.append(f"▫️ {_esc(_cut(o))}")
            note = f"{_MEDALS[place]} место {place + 1} из {k}" + (" — есть!" if landed else "…")
            text = _head("🏅", "ПОДИУМ", title) + "\n".join(lines) + f"\n\n<i>{note} {'▸' * (step % 9 + 1)}</i>"
            await _safe_edit(msg, text)
            await asyncio.sleep(0.95 if landed else 0.5 + 0.05 * h)
        awarded[target] = place


STYLES = {
    "slot":     ("🎰", "слот-машина",       _anim_slot),
    "roulette": ("🎡", "рулетка",           _anim_roulette),
    "hack":     ("🕶", "взлом",             _anim_hack),
    "duel":     ("⚔️", "на вылет",          _anim_duel),
    "spot":     ("💡", "прожектор",         _anim_spot),
    "race":     ("🏇", "скачки",            _anim_race),
    "cards":    ("🃏", "карты",             _anim_cards),
    "lotto":    ("🎱", "лототрон",          _anim_lotto),
    "bomb":     ("💣", "горячая картошка",  _anim_bomb),
    "cup":      ("🏟", "турнир",            _anim_cup),
    "oracle":   ("🔮", "оракул",            _anim_oracle),
}
if _CARDS_OK:     # колесо — это GIF-картинка (Pillow), а не правка текста; fn=None
    STYLES["wheel"] = ("🎠", "колесо (GIF)", None)

_STYLE_ALIASES = {
    "slot": "slot", "слот": "slot", "slots": "slot", "слоты": "slot", "автомат": "slot",
    "roulette": "roulette", "casino": "roulette", "рулетка": "roulette", "казино": "roulette",
    "hack": "hack", "взлом": "hack", "хак": "hack",
    "duel": "duel", "battle": "duel", "вылет": "duel", "дуэль": "duel", "битва": "duel",
    "spot": "spot", "light": "spot", "свет": "spot", "прожектор": "spot", "лампочка": "spot",
    "race": "race", "скачки": "race", "гонка": "race", "гонки": "race",
    "cards": "cards", "card": "cards", "карты": "cards", "карта": "cards",
    "lotto": "lotto", "лото": "lotto", "лототрон": "lotto", "шары": "lotto", "барабан": "lotto",
    "bomb": "bomb", "бомба": "bomb", "картошка": "bomb", "бум": "bomb",
    "cup": "cup", "турнир": "cup", "tournament": "cup", "bracket": "cup", "плейофф": "cup",
    "oracle": "oracle", "оракул": "oracle", "шар": "oracle", "магия": "oracle", "ball": "oracle",
    "wheel": "wheel", "колесо": "wheel", "фортуна": "wheel", "gif": "wheel", "гиф": "wheel",
    "random": "random", "рандом": "random", "любой": "random",
    "list": "list", "стили": "list", "styles": "list", "help": "list",
}
_DIPLOMA_WORDS = {"диплом", "diploma", "карточка", "картинка", "грамота"}
_COUNT_RE = re.compile(r"^(?:top|топ)?(\d{1,2})$", re.IGNORECASE)
_LAST_STYLE: list[str] = []          # последние стили — чтобы подряд не повторялись


def _styles_help() -> str:
    lines = [f"{ic} <code>-{key}</code> — {label}" for key, (ic, label, _) in STYLES.items()]
    return ("🎨 <b>Стили анимации /pick</b>\n" + "\n".join(lines)
            + "\n\n🏅 <code>-3</code> (или <code>-топ3</code>, <code>3 из: …</code>) — несколько победителей, подиум"
            + "\n🖼 <code>-диплом</code> — сразу прислать картинку-диплом"
            + "\n\nПример: <code>/pick -слот кофе или чай</code>"
            + "\n<code>/pick -3 -диплом Кто идёт: Глеб, Дима, Олег, Маша</code>"
            + "\nБез флага стиль случайный. Inline: <code>@бот p: кофе, чай</code>")


def _pick_style(requested: str | None = None, avoid: str | None = None, media: bool = True) -> str:
    """Выбор стиля. media=False — без GIF-стилей (inline-сообщения и лобби не умеют менять текст на анимацию)."""
    usable = [k for k, v in STYLES.items() if media or v[2] is not None]
    if requested in usable:
        return requested
    bad = set(_LAST_STYLE[-2:])
    if avoid:
        bad.add(avoid)
    pool = [k for k in usable if k not in bad] or usable
    key = _RNG.choice(pool)
    _LAST_STYLE.append(key)
    del _LAST_STYLE[:-3]
    return key


def _extract_flags(text: str) -> tuple[str | None, int, bool, str]:
    """
    «-слот -3 -диплом Кто идёт: а, б, в» → ('slot', 3, True, 'Кто идёт: а, б, в').
    Флаги — только в начале; «3 из: а, б, в» тоже задаёт число победителей.
    """
    style, count, diploma, rest = None, 1, False, text or ""
    for _ in range(4):
        m = re.match(r"^\s*[-–—]{1,2}\s*([\w]+)\s*(.*)$", rest, re.DOTALL)
        if not m:
            break
        tok = m.group(1).lower()
        if tok in _STYLE_ALIASES:
            style = _STYLE_ALIASES[tok]
            if style == "list":
                return "list", 1, False, ""
        elif _COUNT_RE.match(tok):
            count = int(_COUNT_RE.match(tok).group(1))
        elif tok in _DIPLOMA_WORDS:
            diploma = True
        else:
            break
        rest = m.group(2)
    m = re.match(r"^\s*(\d{1,2})\s+из\s*:\s*(.*)$", rest, re.IGNORECASE | re.DOTALL)
    if m:
        count, rest = int(m.group(1)), m.group(2)
    if style == "random":
        style = None
    return style, max(1, count), diploma, rest


def _extract_style_flag(text: str) -> tuple[str | None, str]:
    """Совместимость со старым API: (style, rest)."""
    style, _c, _d, rest = _extract_flags(text)
    return style, rest


async def _finale(msg, title: str, winner: str):
    """Салют победы перед итоговым сообщением."""
    t = f"<b>{_esc(_cut(title, 70))}</b>\n\n" if title else ""
    w = _esc(_cut(winner, 50))
    frames = [
        ("💫 · · · · · · · 💫", f"{t}❓ <b>…</b>"),
        ("🎆 ✨ 🎇 ✨ 🎆 ✨ 🎇", f"{t}👑 <b>{w}</b>"),
        ("🎉✨🎊✨🎉✨🎊✨🎉", f"{t}🏆 <b>ПОБЕДИТЕЛЬ!</b>\n👑 <b>{w}</b>"),
        ("🎊🎉🎊🎉🎊🎉🎊🎉🎊", f"{t}🏆 <b>ПОБЕДИТЕЛЬ!</b>\n👑 <b>{w}</b>"),
    ]
    for fr, body in frames:
        await _safe_edit(msg, f"{fr}\n\n{body}\n\n{fr}")
        await asyncio.sleep(0.55)


async def spin(msg: types.Message, options: list[str], title: str,
               winner_idx: int | None = None, style: str | None = None) -> int:
    """
    Крутит анимацию редактированием сообщения и возвращает индекс победителя.
    style: slot|roulette|hack|duel|spot|race|cards|lotto|bomb|cup|oracle
    (None — случайный, не повторяющий последние).
    """
    n = len(options)
    idx = _RNG.randrange(n) if winner_idx is None else winner_idx
    if n < 2:
        return idx
    key = style if style in STYLES and STYLES[style][2] is not None else _pick_style(media=False)
    try:
        await asyncio.wait_for(STYLES[key][2](msg, options, title, idx), ANIM_TIMEOUT)
        await _finale(msg, title, options[idx])
    except Exception:
        logger.exception("анимация %s упала — показываю результат без неё", key)
    return idx


# ── Честный жребий: commit → анимация → reveal ────────────────────────────────
# До анимации бот показывает commit = sha256(seed). Победитель однозначно следует из seed и списка
# вариантов, так что после розыгрыша seed можно раскрыть и перепроверить (кнопка «🔐 Проверка»).
# Это доказывает, что результат зафиксирован ДО анимации и не «докручивался» под желаемый.

def _new_seed() -> tuple[str, str]:
    seed = secrets.token_hex(16)
    return seed, hashlib.sha256(seed.encode()).hexdigest()


def _draw(seed: str, options: list[str], k: int = 1) -> list[int]:
    """k победителей по местам: варианты сортируются по sha256(«seed|i|вариант»), берутся первые k."""
    order = sorted(range(len(options)),
                   key=lambda i: hashlib.sha256(f"{seed}|{i}|{options[i]}".encode()).digest())
    return order[:k]


def _commit_text(title: str, commit: str) -> str:
    t = f"\n<b>{_esc(_cut(title, 70))}</b>" if title else ""
    return (f"🔐 <b>Жребий зафиксирован</b>{t}\n\n"
            f"commit: <code>{commit[:24]}…</code>\n"
            f"<i>результат уже решён — seed раскрою после розыгрыша, можно проверить</i>")


def _check_text(data: dict) -> str:
    opts = data["options"]
    k = data.get("count", 1)
    snippet = (
        "import hashlib\n"
        f"seed = {data['seed']!r}\n"
        f"opts = {opts!r}\n"
        f"k = {k}\n"
        "print(hashlib.sha256(seed.encode()).hexdigest())   # = commit\n"
        "order = sorted(range(len(opts)), key=lambda i: hashlib.sha256(\n"
        "    f\"{seed}|{i}|{opts[i]}\".encode()).digest())\n"
        "print([opts[i] for i in order[:k]])"
    )
    if len(snippet) > 3200:
        snippet = f"seed = {data['seed']!r}  # список слишком длинный для кода — формула ниже"
    names = ", ".join(opts[i] for i in data["order"])
    return (
        "🔐 <b>Проверка жребия</b>\n\n"
        f"commit (показан до розыгрыша):\n<code>{data['commit']}</code>\n\n"
        f"seed (раскрыт):\n<code>{data['seed']}</code>\n\n"
        "Правило: для каждого варианта <code>sha256(«seed|номер|вариант»)</code>; "
        f"побеждают {k} с наименьшим хешем. sha256(seed) должен совпасть с commit.\n\n"
        f"<pre>{_esc(snippet)}</pre>\n"
        f"Итог: <b>{_esc(names)}</b>\n\n"
        "<i>Это доказывает, что итог зафиксирован до анимации и не менялся после.</i>"
    )


class _InlineMsg:
    """Адаптер: чтобы анимации (msg.edit_text) работали и для inline-сообщений (по inline_message_id)."""

    def __init__(self, bot, inline_id: str):
        self._bot, self._id = bot, inline_id

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        return await self._bot.edit_message_text(text=text, inline_message_id=self._id,
                                                 parse_mode=parse_mode, reply_markup=reply_markup)


def _again_kb(tok: str, current: str = "", k: int = 1, inline: bool = False) -> InlineKeyboardMarkup:
    """«Ещё раз» + диплом/проверка + выбор стиля (текущий отмечен ✔️)."""
    rows = [[InlineKeyboardButton(text="🔁 Ещё раз (новый стиль)" if k == 1 else "🔁 Ещё раз",
                                  callback_data=f"fun:again:{tok}")]]
    extra = []
    if _CARDS_OK:
        extra.append(InlineKeyboardButton(text="🖼 Диплом", callback_data=f"fun:card:{tok}"))
    extra.append(InlineKeyboardButton(text="🔐 Проверка", callback_data=f"fun:chk:{tok}"))
    rows.append(extra)
    if k == 1:
        btns = [InlineKeyboardButton(text=("✔️" if key == current else ic), callback_data=f"fun:st:{tok}:{key}")
                for key, (ic, _l, fn) in STYLES.items() if not (inline and fn is None)]
        for i in range(0, len(btns), 6):
            rows.append(btns[i:i + 6])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _result_text(title: str, winner: str, mention: str | None = None, style_label: str = "",
                 n: int = 0, winners: list[str] | None = None, commit: str = "") -> str:
    head = f"🏆 <b>{_esc(title)}</b>\n\n" if title else "🏆 <b>Выпало:</b>\n\n"
    line = "━━━━━━━━━━━━━━"
    if winners and len(winners) > 1:
        body = "\n".join(f"{_MEDALS[i]} <b>{_esc(w)}</b>" if i == 0 else f"{_MEDALS[i]} {_esc(w)}"
                         for i, w in enumerate(winners))
        odds = f"🎯 {len(winners)} из {n} · шанс попасть в топ: {100 * len(winners) / n:.0f}%\n" if n else ""
    else:
        body = f"👑 {mention or f'<b>{_esc(winner)}</b>'}"
        odds = f"🎯 шанс каждого: 1 из {n} ({100 / n:.0f}%)\n" if n >= 2 else ""
    tail = "🎲 жребий криптостойким ГСЧ"
    if commit:
        tail += f" · 🔐 <code>{commit[:10]}</code>"
    if style_label:
        tail = f"{style_label} · {tail}"
    return f"{head}{line}\n{body}\n{line}\n\n{odds}<i>{tail}</i>"


def _new_run(options, title, forced=None, count=1) -> str:
    tok = secrets.token_hex(4)
    _PICKS[tok] = {"options": options, "title": title, "forced": forced, "count": count, "last": None}
    while len(_PICKS) > 400:
        _PICKS.popitem(last=False)
    return tok


async def _run_round(msg, src: dict, key: str, reply_to: "types.Message | None", delete_after_wheel: bool) -> str:
    """
    Один розыгрыш: commit → анимация → итог. msg — редактируемое сообщение (обычное или _InlineMsg).
    reply_to нужен только для колеса (GIF отправляется отдельным сообщением). Возвращает id запуска.
    """
    options, title, k = src["options"], src["title"], src.get("count", 1)
    n = len(options)
    seed, commit = _new_seed()
    order = _draw(seed, options, k)
    names = [options[i] for i in order]
    rid = _new_run(options, title, src.get("forced"), k)
    _PICKS[rid].update(seed=seed, commit=commit, order=order, last=key)

    await _safe_edit(msg, _commit_text(title, commit), final=True)
    await asyncio.sleep(0.9)

    if k > 1:
        key, label = "podium", "🏅 подиум"
        try:
            await asyncio.wait_for(_anim_podium(msg, options, title, order), ANIM_TIMEOUT)
            await _finale(msg, title, names[0])
        except Exception:
            logger.exception("подиум упал — показываю результат без анимации")
    else:
        if key == "wheel" and (reply_to is None or not _CARDS_OK):
            key = "roulette"
        if key == "wheel":
            label = "🎠 колесо"
            await _safe_edit(msg, "🎠 <b>Раскручиваем колесо…</b>")
            try:
                async with _WHEEL_SEM:
                    gif = await asyncio.to_thread(make_wheel_gif, options, order[0], title, commit)
                await reply_to.reply_animation(
                    BufferedInputFile(gif, filename="wheel.gif"),
                    caption=_result_text(title, names[0], style_label=label, n=n, commit=commit),
                    parse_mode="HTML", reply_markup=_again_kb(rid, "wheel", 1))
                _PICKS[rid]["label"] = label
                if delete_after_wheel:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                return rid
            except Exception:
                logger.exception("колесо не отправилось — запасная рулетка")
                key = "roulette"
        icon, lab, _ = STYLES[key]
        label = f"{icon} {lab}"
        await spin(msg, options, title, winner_idx=order[0], style=key)

    _PICKS[rid]["label"] = label
    await _safe_edit(
        msg, _result_text(title, names[0], style_label=label, n=n,
                          winners=names if k > 1 else None, commit=commit),
        _again_kb(rid, key, k, inline=isinstance(msg, _InlineMsg)))
    return rid


async def run_pick(message: types.Message, options: list[str], title: str = "",
                   style: str | None = None, count: int = 1, diploma: bool = False) -> bool:
    """Выбор из вариантов с анимацией. Используется /pick и ИИ-инструментом."""
    options = [o for o in options if o][:MAX_OPTIONS]
    if len(options) < 2:
        await message.reply(
            "🎲 Нужно минимум 2 варианта.\nПример: <code>/pick кофе или чай, пицца</code>\n"
            "Несколько победителей: <code>/pick -3 а, б, в, г</code> · стили: <code>/pick -стили</code>",
            parse_mode="HTML")
        return False
    count = max(1, min(count, len(options), MAX_WINNERS))
    msg = await message.reply(_intro_text(title), parse_mode="HTML")
    await asyncio.sleep(0.5)
    src = {"options": options, "title": title, "forced": style, "count": count}
    key = "podium" if count > 1 else _pick_style(style, media=_CARDS_OK)
    rid = await _run_round(msg, src, key, message, True)
    if diploma:
        await _send_card(message, rid)
    return True


async def cmd_pick(message: types.Message):
    text = (message.text or "").split(maxsplit=1)
    arg = text[1] if len(text) > 1 else ""
    if not arg and message.reply_to_message:
        arg = message.reply_to_message.text or message.reply_to_message.caption or ""
    style, count, diploma, arg = _extract_flags(arg)
    if style == "list":
        await message.reply(_styles_help(), parse_mode="HTML")
        return
    title, options = split_title_and_options(arg)
    await run_pick(message, options, title, style=style, count=count, diploma=diploma)


# ── повторы, смена стиля, диплом, проверка ────────────────────────────────────
_BUSY: set[str] = set()


def _busy_key(cb: types.CallbackQuery) -> str:
    if cb.message is None:
        return f"i:{cb.inline_message_id}"
    return f"{cb.message.chat.id}:{cb.message.message_id}"


async def _rerun(callback: types.CallbackQuery, src_tok: str, requested: str | None):
    src = _PICKS.get(src_tok)
    if not src:
        await callback.answer("Это уже устарело — запусти заново", show_alert=True)
        return
    inline = callback.message is None
    if inline and not callback.inline_message_id:
        await callback.answer("Не могу изменить это сообщение", show_alert=True)
        return
    bkey = _busy_key(callback)
    if bkey in _BUSY:
        await callback.answer("Уже крутится 🎰")
        return
    k = src.get("count", 1)
    key = "podium" if k > 1 else _pick_style(requested or src.get("forced"), avoid=src.get("last"),
                                              media=_CARDS_OK and not inline)
    if inline and requested == "wheel":
        await callback.answer("Колесо-GIF недоступно в inline-сообщениях — взял другой стиль")
    else:
        await callback.answer()
    _BUSY.add(bkey)
    try:
        if inline:
            msg, reply_to, delete = _InlineMsg(callback.bot, callback.inline_message_id), None, False
        else:
            cm = callback.message
            media = bool(cm.animation or cm.photo or cm.document)    # медиа в текст не превратить
            if key == "wheel" or media:
                msg, delete = await cm.reply(_intro_text(src["title"]), parse_mode="HTML"), True
            else:
                msg, delete = cm, False
            reply_to = cm
        await _run_round(msg, src, key, reply_to, delete)
    except Exception:
        logger.exception("повтор жребия упал")
    finally:
        _BUSY.discard(bkey)


async def callback_again(callback: types.CallbackQuery):
    await _rerun(callback, (callback.data or "").split(":", 2)[-1], None)


async def callback_go(callback: types.CallbackQuery):
    """Кнопка «🎰 Крутить!» под inline-сообщением."""
    await _rerun(callback, (callback.data or "").split(":", 2)[-1], None)


async def callback_style(callback: types.CallbackQuery):
    parts = (callback.data or "").split(":")          # fun:st:<tok>:<style>
    if len(parts) != 4 or parts[3] not in STYLES:
        await callback.answer("Неизвестный стиль", show_alert=True)
        return
    await _rerun(callback, parts[2], parts[3])


async def _deliver(callback: types.CallbackQuery, *, text: str | None = None,
                   photo: "BufferedInputFile | None" = None, caption: str | None = None) -> bool:
    """Ответ в чат; у inline-сообщений нет чата → шлём в личку нажавшему."""
    try:
        if callback.message is not None:
            if photo is not None:
                await callback.message.reply_photo(photo, caption=caption, parse_mode="HTML")
            else:
                await callback.message.reply(text, parse_mode="HTML")
        else:
            uid = callback.from_user.id
            if photo is not None:
                await callback.bot.send_photo(uid, photo, caption=caption, parse_mode="HTML")
            else:
                await callback.bot.send_message(uid, text, parse_mode="HTML")
        return True
    except Exception:
        logger.exception("не получилось отправить ответ")
        return False


async def _card_bytes(data: dict) -> bytes:
    winners = [data["options"][i] for i in data["order"]]
    return await asyncio.to_thread(make_pick_card, data["title"], winners, len(data["options"]),
                                   data["commit"], data.get("label", ""))


async def _send_card(message: types.Message, rid: str):
    data = _PICKS.get(rid)
    if not data or "order" not in data or not _CARDS_OK:
        return
    try:
        png = await _card_bytes(data)
        await message.reply_photo(BufferedInputFile(png, filename="pick.png"), caption="🏆 Диплом победителя")
    except Exception:
        logger.exception("диплом не создался")


async def callback_card(callback: types.CallbackQuery):
    data = _PICKS.get((callback.data or "").split(":", 2)[-1])
    if not data or "order" not in data or not _CARDS_OK:
        await callback.answer("Это уже устарело — запусти заново", show_alert=True)
        return
    if data.get("card_busy"):
        await callback.answer("Уже рисую 🎨")
        return
    data["card_busy"] = True
    await callback.answer("Рисую диплом… 🎨")
    try:
        png = await _card_bytes(data)
        ok = await _deliver(callback, photo=BufferedInputFile(png, filename="pick.png"),
                            caption="🏆 Диплом победителя" + ("" if callback.message else " (из inline)"))
        if not ok and callback.message is None:
            logger.info("inline-диплом: пользователь не запускал бота — личка недоступна")
    except Exception:
        logger.exception("диплом не создался")
    finally:
        data["card_busy"] = False


async def callback_check(callback: types.CallbackQuery):
    data = _PICKS.get((callback.data or "").split(":", 2)[-1])
    if not data or "seed" not in data:
        await callback.answer("Это уже устарело — запусти заново", show_alert=True)
        return
    ok = await _deliver(callback, text=_check_text(data))
    await callback.answer("Проверка отправлена" if ok else "Нажми /start у бота — тогда пришлю в личку",
                          show_alert=not ok)


# ── Inline: «p: кофе, чай» (отдельный префикс — a: у аниме, ig у instagram, остальное — музыка) ──
_PICK_PREFIX_RE = re.compile(r"^\s*(?:p|pick|п|жребий)\s*:\s*", re.IGNORECASE)
_PICK_SUFFIX_RE = re.compile(r"\s+/p\s*$", re.IGNORECASE)          # «кофе, чай /p»


def is_pick_inline(raw: str) -> bool:
    return bool(_PICK_PREFIX_RE.match(raw or "") or _PICK_SUFFIX_RE.search(raw or ""))


def _strip_pick_inline(raw: str) -> str:
    return _PICK_SUFFIX_RE.sub("", _PICK_PREFIX_RE.sub("", raw or "", 1)).strip()


def _template_tok(options: list[str], title: str, count: int, forced: str | None) -> str:
    """Детерминированный токен шаблона: одинаковый запрос не плодит записи на каждое нажатие клавиши."""
    raw = json.dumps([options, title, count, forced], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


async def inline_pick(iq: types.InlineQuery):
    raw = _strip_pick_inline(iq.query)
    style, count, _diploma, rest = _extract_flags(raw)
    help_txt = ("🎲 <b>Жребий из inline</b>\nПример: <code>@бот p: кофе, чай, пицца</code>\n"
                "Несколько победителей: <code>p: -3 а, б, в, г</code>")
    if style == "list" or not rest.strip():
        await iq.answer([InlineQueryResultArticle(
            id="pick-help", title="🎲 Жребий: введи варианты через запятую",
            description="p: кофе, чай, пицца   ·   p: -3 а, б, в, г",
            input_message_content=InputTextMessageContent(message_text=help_txt, parse_mode="HTML"))],
            cache_time=0, is_personal=True)
        return
    title, options = split_title_and_options(rest)
    if len(options) < 2:
        await iq.answer([InlineQueryResultArticle(
            id="pick-need2", title="Нужно минимум 2 варианта", description="через запятую, «или» или /",
            input_message_content=InputTextMessageContent(message_text=help_txt, parse_mode="HTML"))],
            cache_time=0, is_personal=True)
        return
    count = max(1, min(count, len(options), MAX_WINNERS))
    tok = _template_tok(options, title, count, style)
    if tok not in _PICKS:
        _PICKS[tok] = {"options": options, "title": title, "forced": style, "count": count, "last": None}
        while len(_PICKS) > 400:
            _PICKS.popitem(last=False)
    else:
        _PICKS.move_to_end(tok)

    shown = " · ".join(_esc(_cut(o, 20)) for o in options[:12]) + (" …" if len(options) > 12 else "")
    goal = "выберем одного" if count == 1 else f"выберем {count} победителей"
    t = f"<b>{_esc(title)}</b>\n" if title else ""
    go_text = f"🎲 {t}Варианты ({len(options)}): {shown}\n\n{goal} — жми 👇"

    # мгновенный вариант: результат сразу (без анимации)
    seed, commit = _new_seed()
    order = _draw(seed, options, count)
    names = [options[i] for i in order]
    rid = _new_run(options, title, style, count)
    _PICKS[rid].update(seed=seed, commit=commit, order=order, label="⚡ мгновенно")
    fast_text = _result_text(title, names[0], style_label="⚡ мгновенно", n=len(options),
                             winners=names if count > 1 else None, commit=commit)

    await iq.answer([
        InlineQueryResultArticle(
            id=f"go{tok}", title="🎰 Крутить с анимацией", description=f"{len(options)} вариантов · {goal}",
            input_message_content=InputTextMessageContent(message_text=go_text, parse_mode="HTML"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎰 Крутить!", callback_data=f"fun:go:{tok}")]])),
        InlineQueryResultArticle(
            id=f"fast{rid}", title="⚡ Сразу результат", description="без анимации, итог — в сообщении",
            input_message_content=InputTextMessageContent(message_text=fast_text, parse_mode="HTML"),
            reply_markup=_again_kb(rid, "", count, inline=True)),
    ], cache_time=0, is_personal=True)


# ── /who — лобби ──────────────────────────────────────────────────────────────
def _user_link(p: Participant) -> str:
    if p.user_id:
        return f'<a href="tg://user?id={p.user_id}">{_esc(p.name)}</a>'
    return f"<b>{_esc(p.name)}</b>"


def _lobby_text(lb: Lobby, left: int | None = None) -> str:
    names = ", ".join(_esc(p.name) for p in lb.people.values()) or "пока никого"
    timer = f"\n⏳ Авто-жребий через ~{left} сек" if left is not None else ""
    return (
        f"🎲 <b>{_esc(lb.question)}</b>\n\n"
        f"Жми «🙋 Участвую». Инициатор запускает «🎰 Крутить» — или бот сам крутит по таймеру.\n\n"
        f"👥 <b>Участники ({len(lb.people)}):</b> {names}{timer}"
    )


def _lobby_kb(tok: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Участвую", callback_data=f"who:join:{tok}"),
         InlineKeyboardButton(text="🚪 Выйти", callback_data=f"who:leave:{tok}")],
        [InlineKeyboardButton(text="👥 Активные в чате", callback_data=f"who:active:{tok}"),
         InlineKeyboardButton(text="📋 Список наряда", callback_data=f"who:duty:{tok}")],
        [InlineKeyboardButton(text="🎰 Крутить", callback_data=f"who:go:{tok}")],
    ])


def _cleanup_lobbies():
    now = time.time()
    for k in [k for k, v in _LOBBIES.items() if now - v.created > 3600]:
        _LOBBIES.pop(k, None)


async def open_lobby(message: types.Message, question: str) -> None:
    """Открывает лобби «кто …?». Используется /who и ИИ-инструментом."""
    _cleanup_lobbies()
    question = (question or "").strip() or "Кого выбираем?"
    if not question.endswith("?"):
        question += "?"
    owner = message.from_user
    tok = secrets.token_hex(3)
    lb = Lobby(chat_id=message.chat.id, msg_id=0, owner_id=owner.id if owner else 0, question=question)
    msg = await message.reply(_lobby_text(lb, LOBBY_SECONDS), parse_mode="HTML", reply_markup=_lobby_kb(tok))
    lb.msg_id = msg.message_id
    _LOBBIES[tok] = lb
    asyncio.create_task(_lobby_timer(msg, tok))


async def _lobby_timer(msg: types.Message, tok: str):
    await asyncio.sleep(LOBBY_SECONDS)
    lb = _LOBBIES.get(tok)
    if not lb or lb.finished or lb.spinning:
        return
    if len(lb.people) < 2:
        lb.finished = True
        await _safe_edit(msg, f"🎲 <b>{_esc(lb.question)}</b>\n\n😴 Не набралось 2 участников — жребий отменён.", final=True)
        return
    await _finish_lobby(msg, tok)


async def _finish_lobby(msg: types.Message, tok: str):
    lb = _LOBBIES.get(tok)
    if not lb or lb.spinning:
        return
    lb.spinning = True
    people = list(lb.people.values())
    names = [p.name for p in people]
    idx = await spin(msg, names, lb.question)
    lb.spinning = False
    lb.finished = True
    winner = people[idx]
    await _safe_edit(
        msg, _result_text(lb.question, winner.name, _user_link(winner), n=len(people)),
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔁 Ещё раз (те же)", callback_data=f"who:redo:{tok}")]]),
    )


async def cmd_who(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg and message.reply_to_message:
        arg = message.reply_to_message.text or ""
    if not arg:
        await message.reply(
            "🎲 Кто будет… ?\n"
            "<code>/who кто моет посуду</code> — открою лобби с кнопкой «Участвую»\n"
            "<code>/who кто моет посуду: Глеб, Дима, Олег</code> — жребий сразу среди названных",
            parse_mode="HTML")
        return
    title, options = split_title_and_options(arg)
    if title and len(options) >= 2:          # явный список → без лобби
        q = title if title.endswith("?") else title + "?"
        await run_pick(message, options, q)
        return
    await open_lobby(message, arg)


def _lobby_or_alert(callback: types.CallbackQuery):
    tok = (callback.data or "").split(":", 2)[-1]
    return tok, _LOBBIES.get(tok)


async def _lobby_refresh(callback: types.CallbackQuery, tok: str, lb: Lobby):
    left = max(0, int(LOBBY_SECONDS - (time.time() - lb.created)))
    await _safe_edit(callback.message, _lobby_text(lb, left), _lobby_kb(tok))


async def callback_who_join(callback: types.CallbackQuery):
    tok, lb = _lobby_or_alert(callback)
    if not lb or lb.finished:
        await callback.answer("Лобби уже закрыто", show_alert=True)
        return
    u = callback.from_user
    key = f"u:{u.id}"
    if key in lb.people:
        await callback.answer("Ты уже в игре 😉")
        return
    lb.people[key] = Participant(key, (u.first_name or u.username or "Игрок")[:30], u.id)
    await callback.answer("Ты в игре! 🎲")
    await _lobby_refresh(callback, tok, lb)


async def callback_who_leave(callback: types.CallbackQuery):
    tok, lb = _lobby_or_alert(callback)
    if not lb or lb.finished:
        await callback.answer("Лобби уже закрыто", show_alert=True)
        return
    if lb.people.pop(f"u:{callback.from_user.id}", None) is None:
        await callback.answer("Тебя и не было в списке")
        return
    await callback.answer("Вышел")
    await _lobby_refresh(callback, tok, lb)


async def callback_who_duty(callback: types.CallbackQuery):
    tok, lb = _lobby_or_alert(callback)
    if not lb or lb.finished:
        await callback.answer("Лобби уже закрыто", show_alert=True)
        return
    if callback.from_user.id != lb.owner_id:
        await callback.answer("Это может только тот, кто открыл лобби", show_alert=True)
        return
    from config import SVODKI_LIST
    for name in SVODKI_LIST:
        lb.people.setdefault(f"n:{name}", Participant(f"n:{name}", name))
    await callback.answer("Добавил список наряда")
    await _lobby_refresh(callback, tok, lb)


def _active_names(chat_id: int) -> list[str]:
    """Имена авторов из недавней истории чата (формат «Имя (@user): текст»)."""
    from config import CHAT_HISTORY
    names: list[str] = []
    for line in CHAT_HISTORY[chat_id]:
        author = line.split(":", 1)[0].strip()
        author = re.sub(r"\s*\[ГС\]$", "", author)
        author = re.sub(r"\s*\(@[\w]+\)$", "", author).strip()
        if author and author not in ("Бот", "Кто-то") and author not in names:
            names.append(author)
    return names[:20]


async def callback_who_active(callback: types.CallbackQuery):
    tok, lb = _lobby_or_alert(callback)
    if not lb or lb.finished:
        await callback.answer("Лобби уже закрыто", show_alert=True)
        return
    if callback.from_user.id != lb.owner_id:
        await callback.answer("Это может только тот, кто открыл лобби", show_alert=True)
        return
    names = _active_names(lb.chat_id)
    if not names:
        await callback.answer("В истории чата пока никого нет", show_alert=True)
        return
    existing = {p.name.lower() for p in lb.people.values()}
    for n in names:
        if n.lower() not in existing:
            lb.people[f"n:{n}"] = Participant(f"n:{n}", n)
    await callback.answer(f"Добавил активных: {len(names)}")
    await _lobby_refresh(callback, tok, lb)


async def callback_who_go(callback: types.CallbackQuery):
    tok, lb = _lobby_or_alert(callback)
    if not lb or lb.finished:
        await callback.answer("Лобби уже закрыто", show_alert=True)
        return
    if callback.from_user.id != lb.owner_id:
        await callback.answer("Крутить может только тот, кто открыл лобби", show_alert=True)
        return
    if len(lb.people) < 2:
        await callback.answer("Нужно минимум 2 участника", show_alert=True)
        return
    await callback.answer("Крутим! 🎰")
    await _finish_lobby(callback.message, tok)


async def callback_who_redo(callback: types.CallbackQuery):
    tok, lb = _lobby_or_alert(callback)
    if not lb or lb.spinning:
        await callback.answer("Уже крутится или устарело", show_alert=True)
        return
    await callback.answer()
    await _finish_lobby(callback.message, tok)


# ── /roll и /coin ─────────────────────────────────────────────────────────────
async def cmd_roll(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else "d6"
    m = re.fullmatch(r"(\d{1,2})?[dд](\d{1,4})", arg)
    if m:
        cnt, sides = int(m.group(1) or 1), int(m.group(2))
    elif arg.isdigit():
        cnt, sides = 1, int(arg)
    else:
        cnt, sides = 1, 6
    if sides < 2 or cnt < 1:
        await message.reply("🎲 Пример: <code>/roll</code>, <code>/roll 100</code>, <code>/roll 3d6</code>", parse_mode="HTML")
        return
    rolls = [_RNG.randint(1, sides) for _ in range(cnt)]
    body = f"🎲 <b>{sum(rolls)}</b>" if cnt == 1 else f"🎲 {' + '.join(map(str, rolls))} = <b>{sum(rolls)}</b>"
    await message.reply(f"{body}  <i>(d{sides}{' ×' + str(cnt) if cnt > 1 else ''})</i>", parse_mode="HTML")


async def cmd_coin(message: types.Message):
    msg = await message.reply("🪙 Подбрасываю…")
    for frame in ("🪙 ⟳", "🪙 ⟲", "🪙 ⟳"):
        await asyncio.sleep(0.6)
        await _safe_edit(msg, frame)
    await _safe_edit(msg, f"🪙 <b>{_RNG.choice(['Орёл 🦅', 'Решка 🌕'])}</b>", final=True)


# ── Регистрация ───────────────────────────────────────────────────────────────
def register_fun_handlers(dp):
    dp.message.register(cmd_pick, Command("pick", "choose"))
    dp.message.register(cmd_who, Command("who", "kto"))
    dp.message.register(cmd_roll, Command("roll", "dice"))
    dp.message.register(cmd_coin, Command("coin", "flip"))
    dp.callback_query.register(callback_again, F.data.startswith("fun:again:"))
    dp.callback_query.register(callback_style, F.data.startswith("fun:st:"))
    dp.callback_query.register(callback_go, F.data.startswith("fun:go:"))
    dp.callback_query.register(callback_card, F.data.startswith("fun:card:"))
    dp.callback_query.register(callback_check, F.data.startswith("fun:chk:"))
    dp.callback_query.register(callback_who_join, F.data.startswith("who:join:"))
    dp.callback_query.register(callback_who_leave, F.data.startswith("who:leave:"))
    dp.callback_query.register(callback_who_duty, F.data.startswith("who:duty:"))
    dp.callback_query.register(callback_who_active, F.data.startswith("who:active:"))
    dp.callback_query.register(callback_who_go, F.data.startswith("who:go:"))
    dp.callback_query.register(callback_who_redo, F.data.startswith("who:redo:"))
    logger.info("🎲 fun_handlers зарегистрированы (/pick, /who, /roll, /coin)")
