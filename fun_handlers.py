"""
fun_handlers.py — рулетки и выбор.

  /pick кофе или чай, пицца      — выбрать из вариантов (анимация «барабана»)
  /pick Что заказать: пицца, суши — то же, с заголовком
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


def _remember_pick(options: list[str], title: str) -> str:
    tok = secrets.token_hex(4)
    _PICKS[tok] = {"options": options, "title": title}
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


# ── Анимация «барабана» ───────────────────────────────────────────────────────
def _reel(options: list[str], pos: int, title: str, icon: str = "🎰") -> str:
    n = len(options)
    prev_, cur, nxt = options[(pos - 1) % n], options[pos % n], options[(pos + 1) % n]
    head = f"{icon} <b>{_esc(title)}</b>\n\n" if title else f"{icon} <b>Крутим…</b>\n\n"
    return f"{head}<code>  {_esc(prev_)}</code>\n▶️ <b>{_esc(cur)}</b>\n<code>  {_esc(nxt)}</code>"


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


async def spin(msg: types.Message, options: list[str], title: str, winner_idx: int | None = None) -> int:
    """
    Крутит «барабан» редактированием сообщения и возвращает индекс победителя.
    Победитель выбирается ЗАРАНЕЕ криптостойким ГСЧ, анимация лишь «докручивается» до него.
    """
    n = len(options)
    idx = _RNG.randrange(n) if winner_idx is None else winner_idx
    if n < 2:
        return idx
    frames = 7
    delays = [0.5, 0.55, 0.65, 0.8, 0.95, 1.1, 1.3]
    for k in range(frames):
        pos = (idx - (frames - 1 - k)) % n
        await _safe_edit(msg, _reel(options, pos, title))
        await asyncio.sleep(delays[k])
    return idx


def _again_kb(tok: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Ещё раз", callback_data=f"fun:again:{tok}")
    ]])


def _result_text(title: str, winner: str, mention: str | None = None) -> str:
    who = mention or f"<b>{_esc(winner)}</b>"
    head = f"🏆 <b>{_esc(title)}</b>\n\n" if title else "🏆 <b>Выпало:</b>\n\n"
    return f"{head}{who}\n\n<i>🎲 жребий криптостойким ГСЧ — подтасовать нельзя</i>"


async def run_pick(message: types.Message, options: list[str], title: str = "") -> bool:
    """Выбор из вариантов с анимацией. Используется /pick и ИИ-инструментом."""
    options = [o for o in options if o][:MAX_OPTIONS]
    if len(options) < 2:
        await message.reply(
            "🎲 Нужно минимум 2 варианта.\nПример: <code>/pick кофе или чай, пицца</code>",
            parse_mode="HTML")
        return False
    msg = await message.reply(_reel(options, 0, title), parse_mode="HTML")
    idx = await spin(msg, options, title)
    tok = _remember_pick(options, title)
    await _safe_edit(msg, _result_text(title, options[idx]), _again_kb(tok))
    return True


async def cmd_pick(message: types.Message):
    text = (message.text or "").split(maxsplit=1)
    arg = text[1] if len(text) > 1 else ""
    if not arg and message.reply_to_message:
        arg = message.reply_to_message.text or message.reply_to_message.caption or ""
    title, options = split_title_and_options(arg)
    await run_pick(message, options, title)


async def callback_again(callback: types.CallbackQuery):
    tok = (callback.data or "").split(":", 2)[-1]
    data = _PICKS.get(tok)
    if not data:
        await callback.answer("Это уже устарело — запусти заново", show_alert=True)
        return
    await callback.answer()
    msg = callback.message
    idx = await spin(msg, data["options"], data["title"])
    await _safe_edit(msg, _result_text(data["title"], data["options"][idx]), _again_kb(tok))


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
