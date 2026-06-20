"""
Генератор красивых карточек-напоминаний через Pillow.

ВАЖНО про шрифты:
В репозитории лежит папка fonts/ с шрифтом DejaVu Sans (обычный и bold).
Это сделано специально, чтобы карточка корректно рисовалась на ЛЮБОМ
хостинге — даже если хостинг разворачивает проект не через Dockerfile
(а значит "apt-get install fonts-dejavu-core" из Dockerfile просто не
выполняется) и в системе вообще нет ни одного шрифта с кириллицей.
Если использовать системный шрифт PIL по умолчанию (ImageFont.load_default()),
кириллица рисуется как пустые квадраты ("тофу") — текст на карточке
становится нечитаемым. Поэтому путь к шрифту в репозитории — это primary
источник, системные пути ниже — просто дополнительная подстраховка.
"""

import os
import re
import random
import sys
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ══════════════════════════════════════════════
#  ШРИФТЫ
# ══════════════════════════════════════════════
_FONT_CANDIDATES_BOLD = [
    os.path.join(BASE_DIR, "fonts", "DejaVuSans-Bold.ttf"),   # бандл в репозитории — основной
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
]
_FONT_CANDIDATES_REG = [
    os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf"),        # бандл в репозитории — основной
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
]


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


FONT_BOLD_PATH = _first_existing(_FONT_CANDIDATES_BOLD)
FONT_REGULAR_PATH = _first_existing(_FONT_CANDIDATES_REG)

if not FONT_BOLD_PATH or not FONT_REGULAR_PATH:
    print(
        "⚠️ ВНИМАНИЕ: не найден ни один TTF-шрифт (ни бандл в fonts/, ни системный). "
        "Кириллица на карточках будет нечитаемой. Проверь, что папка fonts/ "
        "со шрифтами реально лежит рядом с card_generator.py.",
        file=sys.stderr,
        flush=True,
    )

_FONT_CACHE = {}


def load_font(path, size):
    key = (path, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    if path:
        try:
            font = ImageFont.truetype(path, size)
        except Exception as e:
            print(f"⚠️ Не удалось загрузить шрифт {path}: {e}", file=sys.stderr, flush=True)
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# ══════════════════════════════════════════════
#  ОЧИСТКА ТЕКСТА ОТ ЭМОДЗИ ДЛЯ КАРТИНКИ
# ══════════════════════════════════════════════
# Bundled-шрифт DejaVu отлично рисует кириллицу и латиницу, но не содержит
# цветных эмодзи — без чистки они рисовались бы как те же "тофу"-квадраты.
# В тексте сообщений (caption, обычные сообщения бота) эмодзи остаются —
# Telegram рисует их сам. А вот в текст, который идёт НА картинку, эмодзи
# подчищаем, чтобы не было лишних артефактов.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F000-\U0001FFFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "\U00002190-\U000021FF"
    "\U00002300-\U000023FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


def _clean_for_image(text):
    if not text:
        return text
    text = _EMOJI_PATTERN.sub("", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


# ══════════════════════════════════════════════
#  РАЗМЕРЫ И БАЗОВЫЕ ЦВЕТА
# ══════════════════════════════════════════════
W, H = 1080, 1350
BG_H = 620
MARGIN = 40
WHITE = (255, 255, 255)
DARK = (12, 12, 16)

# ══════════════════════════════════════════════
#  23 ТЕМЫ
# ══════════════════════════════════════════════
THEMES = {
    "cs2": {"title": "CS2", "accent": (255, 178, 40)},
    "dota2": {"title": "DOTA 2", "accent": (210, 60, 60)},
    "genshin": {"title": "GENSHIN IMPACT", "accent": (130, 195, 255)},
    "valorant": {"title": "VALORANT", "accent": (255, 70, 85)},
    "minecraft": {"title": "MINECRAFT", "accent": (110, 205, 90)},
    "gta": {"title": "GTA", "accent": (255, 222, 60)},
    "cyberpunk": {"title": "CYBERPUNK", "accent": (250, 230, 10)},
    "lol": {"title": "LEAGUE OF LEGENDS", "accent": (20, 200, 220)},
    "apex": {"title": "APEX LEGENDS", "accent": (255, 95, 30)},
    "fortnite": {"title": "FORTNITE", "accent": (150, 95, 255)},
    "overwatch": {"title": "OVERWATCH", "accent": (255, 155, 30)},
    "amongus": {"title": "AMONG US", "accent": (255, 60, 60)},
    "warzone": {"title": "CALL OF DUTY", "accent": (150, 165, 100)},
    "pubg": {"title": "PUBG", "accent": (240, 175, 60)},
    "witcher": {"title": "THE WITCHER", "accent": (205, 170, 95)},
    "eldenring": {"title": "ELDEN RING", "accent": (230, 195, 115)},
    "stardew": {"title": "STARDEW VALLEY", "accent": (255, 175, 95)},
    "hollowknight": {"title": "HOLLOW KNIGHT", "accent": (115, 205, 230)},
    "portal": {"title": "PORTAL", "accent": (255, 145, 30)},
    "starwars": {"title": "STAR WARS", "accent": (255, 222, 105)},
    "marvel": {"title": "SUPERHEROES", "accent": (235, 45, 45)},
    "synthwave": {"title": "SYNTHWAVE", "accent": (255, 60, 180)},
    "matrix": {"title": "TERMINAL", "accent": (60, 255, 110)},
}
THEME_KEYS = list(THEMES.keys())


# ══════════════════════════════════════════════
#  ФОН
# ══════════════════════════════════════════════
def generate_background(theme_key, width, height):
    theme = THEMES.get(theme_key, THEMES["matrix"])
    accent = theme["accent"]
    img = Image.new("RGB", (width, height), (8, 8, 12))
    draw = ImageDraw.Draw(img)

    dark_accent = tuple(max(0, c - 180) for c in accent)
    for y in range(height):
        t = y / height
        r = int(8 + (dark_accent[0] - 8) * t)
        g = int(8 + (dark_accent[1] - 8) * t)
        b = int(12 + (dark_accent[2] - 12) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    cx, cy = width // 2, int(height * 0.4)
    for r in range(40, max(width, height), 100):
        alpha = max(20, 120 - r // 10)
        color = accent + (alpha,)
        odraw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=random.randint(2, 6))
    for _ in range(8):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        alpha = random.randint(20, 80)
        color = accent + (alpha,)
        odraw.line([(x1, y1), (x2, y2)], fill=color, width=random.randint(1, 4))

    img = img.convert("RGBA")
    img.alpha_composite(overlay)
    return img.convert("RGB")


# ══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ РИСОВАНИЯ
# ══════════════════════════════════════════════
def _centered_x(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return (W - (bb[2] - bb[0])) // 2


def _draw_centered(draw, text, font, y, color=WHITE, shadow=True, so=3, shadow_color=(0, 0, 0)):
    x = _centered_x(draw, text, font)
    if shadow:
        draw.text((x + so, y + so), text, font=font, fill=shadow_color)
    draw.text((x, y), text, font=font, fill=color)
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


def _wrap_text(draw, text, font, max_w):
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bb = draw.textbbox((0, 0), test, font=font)
        if bb[2] - bb[0] <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _draw_outlined_text(draw, xy, text, font, fill, outline_color=(0, 0, 0), outline_w=3):
    x, y = xy
    for dx in range(-outline_w, outline_w + 1):
        for dy in range(-outline_w, outline_w + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_noise(img, amount=8):
    px = img.load()
    w, h = img.size
    for _ in range(w * h // 8):
        x = random.randint(0, w - 1)
        y = random.randint(0, h - 1)
        r, g, b = px[x, y][:3]
        d = random.randint(-amount, amount)
        px[x, y] = (max(0, min(255, r + d)), max(0, min(255, g + d)), max(0, min(255, b + d)))


# ══════════════════════════════════════════════
#  ПРОСТАЯ ЗАГЛУШКА НА СЛУЧАЙ ЛЮБОЙ ОШИБКИ
# ══════════════════════════════════════════════
def _save_fallback_card(output_path, person, header_text):
    """Простая, но гарантированно рабочая заглушка — без anchor и без
    зависимости от шрифтов, которые могли не загрузиться."""
    img = Image.new("RGB", (W, H), DARK)
    draw = ImageDraw.Draw(img)
    font_big = load_font(FONT_BOLD_PATH, 64)
    font_small = load_font(FONT_REGULAR_PATH, 32)

    title = _clean_for_image(person) or "Наряд"
    subtitle = _clean_for_image(header_text) or "Напоминание"

    tb = draw.textbbox((0, 0), title, font=font_big)
    tx = (W - (tb[2] - tb[0])) // 2
    ty = H // 2 - 80
    draw.text((tx, ty), title, font=font_big, fill=WHITE)

    sub_lines = _wrap_text(draw, subtitle, font_small, W - 160)
    sy = ty + (tb[3] - tb[1]) + 40
    for line in sub_lines[:4]:
        sb = draw.textbbox((0, 0), line, font=font_small)
        sx = (W - (sb[2] - sb[0])) // 2
        draw.text((sx, sy), line, font=font_small, fill=(190, 190, 200))
        sy += (sb[3] - sb[1]) + 10

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    img.save(output_path, "JPEG", quality=90)


# ══════════════════════════════════════════════
#  ГЛАВНАЯ ФУНКЦИЯ
# ══════════════════════════════════════════════
def make_reminder_card(
    duty_type, person, position_label, time_label,
    header_text, ending_text, date_label, mood_label,
    proc_day=1, next_person="", next_days=0, total_duties=0,
    theme_key=None, output_path="/tmp/card.jpg"
):
    """
    Рисует карточку-напоминание и сохраняет её в output_path (JPEG).
    Любая внутренняя ошибка перехватывается и вместо падения создаётся
    простая, но рабочая заглушка — так бот никогда не останется совсем
    без картинки и не упадёт целиком из-за бага в рисовании.
    Возвращает использованный theme_key.
    """
    if theme_key not in THEMES:
        theme_key = random.choice(THEME_KEYS)

    try:
        theme = THEMES[theme_key]
        accent = theme["accent"]

        header_text = _clean_for_image(header_text)
        ending_text = _clean_for_image(ending_text)
        mood_label = _clean_for_image(mood_label)

        bg = generate_background(theme_key, W, BG_H)
        img = Image.new("RGB", (W, H), DARK)
        img.paste(bg, (0, 0))

        # затемнение книзу
        img = img.convert("RGBA")
        fade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fade)
        fade_h = 260
        for y in range(BG_H - fade_h, BG_H):
            a = int(235 * (y - (BG_H - fade_h)) / fade_h)
            fd.line([(0, y), (W, y)], fill=(DARK[0], DARK[1], DARK[2], a))
        fd.rectangle([0, BG_H, W, H], fill=(DARK[0], DARK[1], DARK[2], 255))
        img.alpha_composite(fade)
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)

        # бейдж темы
        badge_font = load_font(FONT_BOLD_PATH, 26)
        badge_text = f"[ {theme['title']} ]"
        bb = draw.textbbox((0, 0), badge_text, font=badge_font)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        draw.rectangle([MARGIN - 12, MARGIN - 10, MARGIN + bw + 12, MARGIN + bh + 14], fill=(15, 15, 20))
        draw.text((MARGIN, MARGIN), badge_text, font=badge_font, fill=accent)

        # время
        time_font = load_font(FONT_BOLD_PATH, 26)
        tb = draw.textbbox((0, 0), time_label, font=time_font)
        tw = tb[2] - tb[0]
        draw.rectangle([W - MARGIN - tw - 24, MARGIN - 10, W - MARGIN + 12, MARGIN + bh + 14], fill=(15, 15, 20))
        draw.text((W - MARGIN - tw - 12, MARGIN), time_label, font=time_font, fill=WHITE)

        # имя
        name_font = load_font(FONT_BOLD_PATH, 96)
        nb = draw.textbbox((0, 0), person, font=name_font)
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = (W - nw) // 2
        ny = BG_H - nh - 150
        _draw_outlined_text(draw, (nx, ny), person, name_font, fill=WHITE, outline_w=3)

        # позиция
        pos_font = load_font(FONT_REGULAR_PATH, 30)
        pos_text = f"— {position_label} —"
        px = _centered_x(draw, pos_text, pos_font)
        py = ny + nh + 18
        draw.text((px + 2, py + 2), pos_text, font=pos_font, fill=(0, 0, 0))
        draw.text((px, py), pos_text, font=pos_font, fill=accent)

        # нижняя панель
        text_y = BG_H + 30
        max_w = W - 110

        meta_font = load_font(FONT_REGULAR_PATH, 24)
        meta_text = f"{date_label}  •  {mood_label}".strip(" •")
        h = _draw_centered(draw, meta_text, meta_font, text_y, color=(150, 150, 165), shadow=False)
        text_y += h + 18

        title_lines = [header_text.upper()] if header_text else [""]
        title_font = load_font(FONT_BOLD_PATH, 50)
        for fs in (50, 44, 38, 34):
            title_font = load_font(FONT_BOLD_PATH, fs)
            title_lines = _wrap_text(draw, header_text.upper(), title_font, max_w)
            if len(title_lines) <= 3:
                break
        for line in title_lines:
            h = _draw_centered(draw, line, title_font, text_y, color=WHITE)
            text_y += h + 6
        text_y += 10

        draw.line([(90, text_y), (W - 90, text_y)], fill=accent, width=3)
        text_y += 18

        duty_font = load_font(FONT_BOLD_PATH, 32)
        duty_title = "СВОДКИ" if duty_type == "svodki" else "ПРОЦЕДУРКА"
        if duty_type == "proc":
            filled = round((proc_day / 2) * 12)
            bar = "[" + "▓" * filled + "░" * (12 - filled) + "]"
            duty_line = f"{duty_title}  {bar}  ДЕНЬ {proc_day}/2"
        else:
            duty_line = f"{duty_title}  •  до {time_label}"
        h = _draw_centered(draw, duty_line, duty_font, text_y, color=accent)
        text_y += h + 20

        stat_font = load_font(FONT_REGULAR_PATH, 26)
        stat_text = f"нарядов всего: {total_duties}   •   следующий: {next_person} ({next_days}д)"
        h = _draw_centered(draw, stat_text, stat_font, text_y, color=(175, 175, 190), shadow=False)
        text_y += h + 24

        draw.line([(150, text_y), (W - 150, text_y)], fill=(55, 55, 65), width=1)
        text_y += 22

        end_font = load_font(FONT_REGULAR_PATH, 30)
        end_lines = _wrap_text(draw, ending_text, end_font, max_w) if ending_text else []
        for line in end_lines:
            h = _draw_centered(draw, line, end_font, text_y, color=(200, 200, 210), shadow=True, so=2)
            text_y += h + 6

        foot_font = load_font(FONT_BOLD_PATH, 18)
        foot_text = f"• НАРЯД-БОТ × {theme['title']} •"
        fx = _centered_x(draw, foot_text, foot_font)
        draw.text((fx, H - MARGIN - 26), foot_text, font=foot_font, fill=(70, 70, 80))

        _draw_noise(img, amount=8)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "JPEG", quality=92)
        print(f"✅ Карточка сохранена: {output_path} ({theme_key})", flush=True)
        return theme_key

    except Exception as e:
        print(f"❌ Ошибка генерации карточки ({theme_key}): {e}", file=sys.stderr, flush=True)
        try:
            _save_fallback_card(output_path, person, header_text)
            print(f"⚠️ Сохранена упрощённая заглушка: {output_path}", flush=True)
        except Exception as e2:
            print(f"❌ Критическая ошибка при создании заглушки: {e2}", file=sys.stderr, flush=True)
            raise
        return theme_key


if __name__ == "__main__":
    # Быстрый ручной тест: python3 card_generator.py
    out = make_reminder_card(
        duty_type="svodki",
        person="Тест",
        position_label="Дежурный №1 по сводкам",
        time_label="18:00",
        header_text="тестовая карточка",
        ending_text="если ты это видишь — генератор работает 🎮",
        date_label="19.06.2026 • пятница",
        mood_label="🎮 геймерский",
        theme_key=random.choice(THEME_KEYS),
        output_path=os.path.join(BASE_DIR, "temp", "selftest.jpg"),
    )
    print("Тема:", out)
