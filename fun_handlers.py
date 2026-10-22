"""
fun_handlers.py — рулетки и выбор.

  /pick кофе или чай, пицца      — выбрать из вариантов (случайный стиль анимации)
  /pick Что заказать: пицца, суши — то же, с заголовком
  /pick -слот|-рулетка|-взлом|-вылет|-прожектор кофе или чай — стиль анимации вручную
  /who кто моет посуду           — лобби: люди жмут «🙋 Участвую», потом честный жребий
  /who кто моет посуду: Глеб, Дима, Олег — жребий сразу среди названных
  /roll [N | NdM]                — кубик (по умолчанию d6)
  /coin                          — орёл/решка

«Честный» жребий: используется secrets.SystemRandom (криптостойкий ГСЧ ОС), а не random.
Лобби живут в памяти (после рестарта бота старые кнопки отвечают «устарело»).
Подключение: register_fun_handlers(dp) в main.py.
"""

import asyncio
import html
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
    while len(_PICKS) > 200:
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
# Стили: слот-машина, рулетка казино, взлом, «на вылет», прожектор.
# Telegram режет частые edit'ы, поэтому кадров немного (~8–10) и пауза между ними ≥0.5 с.

async def _safe_edit(msg: types.Message, text: str, reply_markup=None) -> bool:
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
    return f"🎲 <b>Готовим розыгрыш…</b>{t}\n\n🎰 · 🎡 · 🎲"


# 1) 🎰 Слот-машина: три барабана с символами по очереди «встают» на 7️⃣, в окне крутятся варианты
_SLOT_SYMS = ["🍒", "🍋", "🔔", "🍇", "💎", "⭐", "🍀", "🎲", "🍉"]


async def _anim_slot(msg, options, title, idx):
    n = len(options)
    delays = [0.5, 0.5, 0.5, 0.55, 0.65, 0.8, 0.9, 1.0, 1.1, 1.25]
    frames = len(delays)
    stops = (5, 7, 9)                      # на каком кадре «встаёт» каждый барабан
    for k in range(frames):
        pos = (idx - (frames - 1 - k)) % n
        locked = sum(k >= s for s in stops)
        syms = ["7️⃣" if i < locked else _RNG.choice(_SLOT_SYMS) for i in range(3)]
        prev_, cur, nxt = options[(pos - 1) % n], options[pos % n], options[(pos + 1) % n]
        text = (
            _head("🎰", "СЛОТ-МАШИНА", title)
            + f"【 {syms[0]} │ {syms[1]} │ {syms[2]} 】\n"
            + "━━━━━━━━━━━━━━\n"
            + f"<code>   {_esc(_cut(prev_))}</code>\n"
            + f"▶️ <b>{_esc(_cut(cur))}</b> ◀️\n"
            + f"<code>   {_esc(_cut(nxt))}</code>\n"
            + "━━━━━━━━━━━━━━"
            + ("\n\n💥 <b>ДЖЕКПОТ!</b>" if locked == 3 else "")
        )
        await _safe_edit(msg, text)
        await asyncio.sleep(delays[k])


# 2) 🎡 Рулетка казино: цветная лента, большие прыжки в начале (скорость), затем замедление и «подскок шарика»
async def _anim_roulette(msg, options, title, idx):
    n = len(options)

    def color(i: int) -> str:
        return "🟥" if i % 2 == 0 else "⬛"

    # смещения относительно победителя: чем больше шаг — тем быстрее вращение
    offsets = [-29, -22, -16, -11, -7, -4, -2, 1, -1, 0]
    delays = [0.5, 0.5, 0.55, 0.65, 0.8, 0.95, 1.1, 0.9, 0.9, 1.0]
    notes = ["набираем скорость", "набираем скорость", "крутится", "крутится", "замедляется",
             "замедляется", "почти…", "шарик подпрыгнул…", "шарик колеблется…", "СТОП!"]
    for k, off in enumerate(offsets):
        pos = (idx + off) % n
        cells = []
        for d in range(-4, 5):
            c = color((pos + d) % n)
            cells.append(f"【{c}】" if d == 0 else c)
        ball = "⚪" if k < len(offsets) - 1 else "🔴"
        text = (
            _head("🎡", "РУЛЕТКА", title)
            + "".join(cells) + "\n"
            + f"{ball} <b>{_esc(_cut(options[pos]))}</b>  <i>(№{pos % n + 1})</i>\n\n"
            + f"<i>{'◐◓◑◒'[k % 4]} {notes[k]}</i>"
        )
        await _safe_edit(msg, text)
        await asyncio.sleep(delays[k])


# 3) 🕶 Взлом: буквы-«шум» постепенно расшифровываются в имя победителя
_GLYPHS = "█▓▒░#$%&@*+=?<>/\\|0123456789"


async def _anim_hack(msg, options, title, idx):
    winner = _cut(options[idx], 28)
    frames = 9
    order = [i for i, ch in enumerate(winner) if ch != " "]
    _RNG.shuffle(order)
    states = ["сканирую порты…", "подбираю ключ…", "обхожу фаервол…", "ломаю шифр…",
              "расшифровываю…", "расшифровываю…", "почти готово…", "последние байты…", "ДОСТУП ОТКРЫТ"]
    for k in range(frames):
        reveal = set(order[: round(len(order) * k / (frames - 1))])
        s = "".join(ch if (i in reveal or ch == " ") else _RNG.choice(_GLYPHS)
                    for i, ch in enumerate(winner))
        pct = int(100 * k / (frames - 1))
        bar = "▰" * round(10 * pct / 100) + "▱" * (10 - round(10 * pct / 100))
        text = (
            _head("🕶", "ВЗЛОМ СИСТЕМЫ", title)
            + f"<code>&gt; {_esc(s)}</code>\n\n"
            + f"<code>{bar} {pct}%</code>\n"
            + f"<i>{states[k]}</i>"
        )
        await _safe_edit(msg, text)
        await asyncio.sleep(0.75 if k < frames - 1 else 0.3)


# 4) ⚔️ На вылет: варианты зачёркиваются по одному, остаётся последний выживший
async def _anim_duel(msg, options, title, idx):
    n = len(options)
    losers = [i for i in range(n) if i != idx]
    _RNG.shuffle(losers)
    frames = min(len(losers), 8)
    # делим вылетающих на frames примерно равных пачек
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
        tail = "остался один!" if alive == 1 else f"осталось в живых: <b>{alive}</b>"
        text = _head("⚔️", "НА ВЫЛЕТ", title) + "\n".join(lines) + f"\n\n<i>{tail}</i>"
        await _safe_edit(msg, text)
        await asyncio.sleep(0.9 + 0.1 * k)


# 5) 💡 Прожектор: лампочка прыгает по вариантам хаотично, прыжки редеют — как в «Поле чудес»
async def _anim_spot(msg, options, title, idx):
    n = len(options)
    hops = 10
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
    delays = [0.5, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0, 1.15, 1.3]
    for k, cur in enumerate(seq):
        bulbs = ("💡⚫" * 5) if k % 2 == 0 else ("⚫💡" * 5)
        lines = [
            f"💡 <b>{_esc(_cut(o))}</b>" if i == cur else f"▫️ {_esc(_cut(o))}"
            for i, o in enumerate(options)
        ]
        text = _head("💡", "ПРОЖЕКТОР", title) + bulbs + "\n" + "\n".join(lines) + "\n" + bulbs
        await _safe_edit(msg, text)
        await asyncio.sleep(delays[k])


STYLES = {
    "slot":     ("🎰", "слот-машина",   _anim_slot),
    "roulette": ("🎡", "рулетка",       _anim_roulette),
    "hack":     ("🕶", "взлом",         _anim_hack),
    "duel":     ("⚔️", "на вылет",      _anim_duel),
    "spot":     ("💡", "прожектор",     _anim_spot),
}
_STYLE_ALIASES = {
    "slot": "slot", "слот": "slot", "slots": "slot", "слоты": "slot", "автомат": "slot",
    "roulette": "roulette", "casino": "roulette", "рулетка": "roulette", "казино": "roulette",
    "hack": "hack", "взлом": "hack", "хак": "hack",
    "duel": "duel", "battle": "duel", "вылет": "duel", "дуэль": "duel", "битва": "duel",
    "spot": "spot", "light": "spot", "свет": "spot", "прожектор": "spot", "лампочка": "spot",
}
_LAST_STYLE: list[str] = []          # последний стиль — чтобы подряд не повторялся


def _pick_style(requested: str | None = None, avoid: str | None = None) -> str:
    if requested in STYLES:
        return requested
    bad = {avoid} if avoid else set(_LAST_STYLE[-1:])
    pool = [k for k in STYLES if k not in bad] or list(STYLES)
    key = _RNG.choice(pool)
    _LAST_STYLE.append(key)
    del _LAST_STYLE[:-3]
    return key


def _extract_style_flag(text: str) -> tuple[str | None, str]:
    """«-слот кофе или чай» / «--roulette a, b» → ('slot', 'кофе или чай'). Без флага — (None, text)."""
    m = re.match(r"^\s*[-–—]{1,2}\s*([\w]+)\s*(.*)$", text or "", re.DOTALL)
    if m and m.group(1).lower() in _STYLE_ALIASES:
        return _STYLE_ALIASES[m.group(1).lower()], m.group(2)
    return None, text or ""


async def _finale(msg, title: str, winner: str):
    """Короткая «вспышка» победы перед итоговым сообщением."""
    t = f"<b>{_esc(_cut(title, 70))}</b>\n\n" if title else ""
    frames = [
        "🎉✨🎊✨🎉✨🎊✨🎉",
        "✨🎊✨🎉✨🎊✨🎉✨",
        "🎊🎉🎊🎉🎊🎉🎊🎉🎊",
    ]
    for fr in frames:
        await _safe_edit(msg, f"{fr}\n\n{t}👑 <b>{_esc(_cut(winner, 50))}</b>\n\n{fr}")
        await asyncio.sleep(0.6)


async def spin(msg: types.Message, options: list[str], title: str,
               winner_idx: int | None = None, style: str | None = None) -> int:
    """
    Крутит анимацию редактированием сообщения и возвращает индекс победителя.
    style: slot | roulette | hack | duel | spot (None — случайный, не такой же как прошлый).
    """
    n = len(options)
    idx = _RNG.randrange(n) if winner_idx is None else winner_idx
    if n < 2:
        return idx
    key = style if style in STYLES else _pick_style()
    try:
        await STYLES[key][2](msg, options, title, idx)
        await _finale(msg, title, options[idx])
    except Exception:
        logger.exception("анимация %s упала — показываю результат без неё", key)
    return idx


def _again_kb(tok: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Ещё раз (новый стиль)", callback_data=f"fun:again:{tok}")
    ]])


def _result_text(title: str, winner: str, mention: str | None = None, style_label: str = "") -> str:
    who = mention or f"<b>{_esc(winner)}</b>"
    head = f"🏆 <b>{_esc(title)}</b>\n\n" if title else "🏆 <b>Выпало:</b>\n\n"
    line = "━━━━━━━━━━━━━━"
    tail = "🎲 жребий криптостойким ГСЧ — подтасовать нельзя"
    if style_label:
        tail = f"{style_label} · {tail}"
    return f"{head}{line}\n👑 {who}\n{line}\n\n<i>{tail}</i>"


async def run_pick(message: types.Message, options: list[str], title: str = "",
                   style: str | None = None) -> bool:
    """Выбор из вариантов с анимацией. Используется /pick и ИИ-инструментом."""
    options = [o for o in options if o][:MAX_OPTIONS]
    if len(options) < 2:
        await message.reply(
            "🎲 Нужно минимум 2 варианта.\nПример: <code>/pick кофе или чай, пицца</code>",
            parse_mode="HTML")
        return False
    msg = await message.reply(_intro_text(title), parse_mode="HTML")
    await asyncio.sleep(0.6)
    key = _pick_style(style)
    idx = await spin(msg, options, title, style=key)
    tok = _remember_pick(options, title, forced=style, last=key)
    icon, label, _ = STYLES[key]
    await _safe_edit(msg, _result_text(title, options[idx], style_label=f"{icon} {label}"), _again_kb(tok))
    return True


async def cmd_pick(message: types.Message):
    text = (message.text or "").split(maxsplit=1)
    arg = text[1] if len(text) > 1 else ""
    if not arg and message.reply_to_message:
        arg = message.reply_to_message.text or message.reply_to_message.caption or ""
    style, arg = _extract_style_flag(arg)
    title, options = split_title_and_options(arg)
    await run_pick(message, options, title, style=style)


async def callback_again(callback: types.CallbackQuery):
    tok = (callback.data or "").split(":", 2)[-1]
    data = _PICKS.get(tok)
    if not data:
        await callback.answer("Это уже устарело — запусти заново", show_alert=True)
        return
    if data.get("busy"):
        await callback.answer("Уже крутится 🎰")
        return
    await callback.answer()
    data["busy"] = True
    try:
        msg = callback.message
        key = _pick_style(data.get("forced"), avoid=data.get("last"))
        data["last"] = key
        idx = await spin(msg, data["options"], data["title"], style=key)
        icon, label, _ = STYLES[key]
        await _safe_edit(msg, _result_text(data["title"], data["options"][idx],
                                           style_label=f"{icon} {label}"), _again_kb(tok))
    finally:
        data["busy"] = False


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
        await _safe_edit(msg, f"🎲 <b>{_esc(lb.question)}</b>\n\n😴 Не набралось 2 участников — жребий отменён.")
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
        msg, _result_text(lb.question, winner.name, _user_link(winner)),
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
    await _safe_edit(msg, f"🪙 <b>{_RNG.choice(['Орёл 🦅', 'Решка 🌕'])}</b>")


# ── Регистрация ───────────────────────────────────────────────────────────────
def register_fun_handlers(dp):
    dp.message.register(cmd_pick, Command("pick", "choose"))
    dp.message.register(cmd_who, Command("who", "kto"))
    dp.message.register(cmd_roll, Command("roll", "dice"))
    dp.message.register(cmd_coin, Command("coin", "flip"))
    dp.callback_query.register(callback_again, F.data.startswith("fun:again:"))
    dp.callback_query.register(callback_who_join, F.data.startswith("who:join:"))
    dp.callback_query.register(callback_who_leave, F.data.startswith("who:leave:"))
    dp.callback_query.register(callback_who_duty, F.data.startswith("who:duty:"))
    dp.callback_query.register(callback_who_active, F.data.startswith("who:active:"))
    dp.callback_query.register(callback_who_go, F.data.startswith("who:go:"))
    dp.callback_query.register(callback_who_redo, F.data.startswith("who:redo:"))
    logger.info("🎲 fun_handlers зарегистрированы (/pick, /who, /roll, /coin)")
