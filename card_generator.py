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
#  ФОН — КРАСИВЫЙ АРТ
# ══════════════════════════════════════════════
def _draw_glow_circle(overlay_draw, cx, cy, r, color_rgb, max_alpha=120):
    """Рисует светящийся круг — несколько слоёв с убывающей прозрачностью."""
    layers = 6
    for i in range(layers):
        ratio = i / layers
        cur_r = int(r * (1 - ratio * 0.6))
        alpha = int(max_alpha * (1 - ratio))
        overlay_draw.ellipse(
            [cx - cur_r, cy - cur_r, cx + cur_r, cy + cur_r],
            fill=color_rgb + (alpha,)
        )


def _draw_hex_grid(draw, width, height, accent, spacing=120, alpha=18):
    """Рисует тонкую гексагональную решётку поверх фона."""
    import math
    hex_w = spacing
    hex_h = int(hex_w * 0.866)
    col_r = (*accent, alpha)
    for row in range(-1, height // hex_h + 2):
        for col in range(-1, width // hex_w + 2):
            cx = col * hex_w + (hex_w // 2 if row % 2 else 0)
            cy = row * hex_h
            pts = []
            for i in range(6):
                angle = math.radians(60 * i - 30)
                pts.append((cx + spacing // 2 * math.cos(angle),
                             cy + spacing // 2 * math.sin(angle)))
            # рисуем только рёбра, не заливку
            for i in range(6):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % 6]
                draw.line([(x1, y1), (x2, y2)], fill=col_r, width=1)


def _draw_scan_lines(img, height, alpha=12):
    """Лёгкие горизонтальные scanlines — даёт ощущение экрана."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(0, height, 4):
        draw.line([(0, y), (img.width, y)], fill=(0, 0, 0, alpha))
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    return img_rgba.convert("RGB")


def generate_background(theme_key, width, height):
    theme = THEMES.get(theme_key, THEMES["matrix"])
    accent = theme["accent"]

    # ── 1. Многослойный градиент фона ──────────────────────────────────────
    img = Image.new("RGB", (width, height), (4, 4, 8))
    draw = ImageDraw.Draw(img)

    dark_a = tuple(max(0, c - 170) for c in accent)
    mid_a  = tuple(max(0, c - 130) for c in accent)

    # Диагональный градиент (сверху-слева к снизу-справа)
    for y in range(height):
        t = y / height
        r = int(6  + dark_a[0] * (1 - t) * 0.6)
        g = int(6  + dark_a[1] * (1 - t) * 0.6)
        b = int(10 + dark_a[2] * (1 - t) * 0.7)
        draw.line([(0, y), (width, y)], fill=(
            min(255, r), min(255, g), min(255, b)
        ))

    # ── 2. Основная RGBA-оверлей ──────────────────────────────────────────
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    # --- Крупный glow в центре-верхней части ---
    cx, cy = width // 2, int(height * 0.38)
    _draw_glow_circle(odraw, cx, cy, int(min(width, height) * 0.55), accent, max_alpha=55)

    # --- Маленький яркий glow смещённый ---
    offset_x = random.randint(-width // 5, width // 5)
    offset_y = random.randint(-height // 8, height // 8)
    _draw_glow_circle(odraw, cx + offset_x, cy + offset_y,
                      int(min(width, height) * 0.25), accent, max_alpha=80)

    # --- Концентрические кольца ---
    ring_step = random.randint(70, 110)
    for ring_r in range(ring_step, max(width, height), ring_step):
        alpha = max(8, 80 - ring_r // 14)
        w_ring = random.randint(1, 3)
        odraw.ellipse(
            [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
            outline=accent + (alpha,), width=w_ring
        )

    # --- Диагональные "лучи" / энергетические линии ---
    num_rays = random.randint(5, 10)
    for _ in range(num_rays):
        angle_r = random.uniform(0, 6.28)
        length = random.randint(int(height * 0.3), int(height * 0.9))
        x1 = cx + int(random.uniform(-60, 60))
        y1 = cy + int(random.uniform(-40, 40))
        x2 = x1 + int(length * __import__('math').cos(angle_r))
        y2 = y1 + int(length * __import__('math').sin(angle_r))
        alpha_ray = random.randint(12, 50)
        odraw.line([(x1, y1), (x2, y2)],
                   fill=accent + (alpha_ray,),
                   width=random.randint(1, 3))

    # --- "Звёздная пыль" — мелкие точки ──────────────────────────────────
    num_stars = random.randint(60, 120)
    for _ in range(num_stars):
        sx = random.randint(0, width)
        sy = random.randint(0, height)
        star_r = random.randint(1, 4)
        star_alpha = random.randint(30, 160)
        odraw.ellipse(
            [sx - star_r, sy - star_r, sx + star_r, sy + star_r],
            fill=accent + (star_alpha,)
        )

    # --- Случайные геометрические фигуры (ромбы/треугольники) ──────────
    num_shapes = random.randint(3, 7)
    for _ in range(num_shapes):
        sx = random.randint(0, width)
        sy = random.randint(0, height)
        size = random.randint(20, 80)
        alpha_s = random.randint(10, 40)
        shape_type = random.choice(['diamond', 'triangle', 'cross'])
        col_s = accent + (alpha_s,)
        if shape_type == 'diamond':
            odraw.polygon([
                (sx, sy - size), (sx + size, sy),
                (sx, sy + size), (sx - size, sy)
            ], outline=col_s)
        elif shape_type == 'triangle':
            odraw.polygon([
                (sx, sy - size),
                (sx + size, sy + size),
                (sx - size, sy + size)
            ], outline=col_s)
        else:
            odraw.line([(sx - size, sy), (sx + size, sy)], fill=col_s, width=2)
            odraw.line([(sx, sy - size), (sx, sy + size)], fill=col_s, width=2)

    # ── 3. Применяем оверлей ─────────────────────────────────────────────
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img = img_rgba.convert("RGB")

    # ── 4. Гексагональная сетка ──────────────────────────────────────────
    hex_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _draw_hex_grid(ImageDraw.Draw(hex_overlay), width, height, accent,
                   spacing=random.randint(90, 150), alpha=random.randint(12, 25))
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(hex_overlay)
    img = img_rgba.convert("RGB")

    # ── 5. Scanlines для атмосферы ────────────────────────────────────────
    img = _draw_scan_lines(img, height, alpha=random.randint(8, 18))

    return img


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

def _draw_corner_decorations(draw, accent, margin=28):
    """Рисует угловые L-образные декоры в каждом углу карточки."""
    arm = 44
    thick = 3
    col = accent
    corners = [
        (margin, margin,        +1, 0,  0, +1),
        (W - margin, margin,   -1, 0,  0, +1),
        (margin, H - margin,   +1, 0,  0, -1),
        (W - margin, H - margin, -1, 0, 0, -1),
    ]
    for x, y, dhx, dhy, dvx, dvy in corners:
        draw.line([(x, y), (x + dhx * arm, y + dhy * arm)], fill=col, width=thick)
        draw.line([(x, y), (x + dvx * arm, y + dvy * arm)], fill=col, width=thick)
        draw.rectangle([x - 4, y - 4, x + 4, y + 4], fill=col)


def _draw_neon_line(draw, x1, y1, x2, y2, accent, width=3):
    """Рисует линию с эффектом неонового свечения."""
    r, g, b = accent
    draw.line([(x1, y1), (x2, y2)],
              fill=(min(255, r + 60), min(255, g + 60), min(255, b + 60)),
              width=width + 4)
    draw.line([(x1, y1), (x2, y2)], fill=(255, 255, 255), width=1)


def _draw_stat_badge(draw, label, value, x, y, accent, font_label, font_value):
    """Рисует маленький бейдж со статистикой."""
    pad_x, pad_y = 14, 10
    lb = draw.textbbox((0, 0), label, font=font_label)
    vb = draw.textbbox((0, 0), value, font=font_value)
    box_w = max(lb[2] - lb[0], vb[2] - vb[0]) + pad_x * 2
    box_h = (lb[3] - lb[1]) + (vb[3] - vb[1]) + pad_y * 3
    draw.rectangle([x, y, x + box_w, y + box_h],
                   fill=(18, 18, 24), outline=accent, width=1)
    draw.text((x + pad_x, y + pad_y), label, font=font_label, fill=(130, 130, 150))
    draw.text((x + pad_x, y + pad_y + (lb[3] - lb[1]) + 4), value,
              font=font_value, fill=(255, 255, 255))
    return box_w, box_h


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
        mood_label  = _clean_for_image(mood_label)

        # ── 1. Фон-арт ────────────────────────────────────────────────────
        bg = generate_background(theme_key, W, BG_H)
        img = Image.new("RGB", (W, H), DARK)
        img.paste(bg, (0, 0))

        # ── 2. Градиентный переход арт → тёмная панель ───────────────────
        img = img.convert("RGBA")
        fade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fade)
        fade_h = 300
        for y in range(BG_H - fade_h, BG_H):
            a = int(245 * (y - (BG_H - fade_h)) / fade_h)
            fd.line([(0, y), (W, y)], fill=(DARK[0], DARK[1], DARK[2], a))
        fd.rectangle([0, BG_H, W, H], fill=(DARK[0], DARK[1], DARK[2], 255))
        img.alpha_composite(fade)
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)

        # ── 3. Угловые декоры ─────────────────────────────────────────────
        _draw_corner_decorations(draw, accent, margin=30)

        # ── 4. Верхняя строка: бейдж темы + время ────────────────────────
        badge_font = load_font(FONT_BOLD_PATH, 24)
        badge_text = f"◈ {theme['title']} ◈"
        bb = draw.textbbox((0, 0), badge_text, font=badge_font)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        pad = 10
        draw.rectangle(
            [MARGIN - pad, MARGIN - 6, MARGIN + bw + pad, MARGIN + bh + 8],
            fill=(12, 12, 18), outline=accent, width=1
        )
        draw.text((MARGIN, MARGIN), badge_text, font=badge_font, fill=accent)

        time_font = load_font(FONT_BOLD_PATH, 24)
        tb = draw.textbbox((0, 0), time_label, font=time_font)
        tw = tb[2] - tb[0]
        draw.rectangle(
            [W - MARGIN - tw - 2 * pad, MARGIN - 6, W - MARGIN + pad, MARGIN + bh + 8],
            fill=(12, 12, 18), outline=accent, width=1
        )
        draw.text((W - MARGIN - tw - pad, MARGIN), time_label, font=time_font, fill=WHITE)

        # ── 5. Горизонтальная неоновая линия ─────────────────────────────
        line_y = MARGIN + bh + 18
        _draw_neon_line(draw, MARGIN, line_y, W - MARGIN, line_y, accent, width=1)

        # ── 6. Имя дежурного ──────────────────────────────────────────────
        for name_size in (100, 88, 76, 64, 54):
            name_font = load_font(FONT_BOLD_PATH, name_size)
            nb = draw.textbbox((0, 0), person, font=name_font)
            if nb[2] - nb[0] <= W - 160:
                break
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = (W - nw) // 2
        ny = BG_H - nh - 160
        _draw_outlined_text(draw, (nx, ny), person, name_font,
                            fill=WHITE, outline_color=(0, 0, 0), outline_w=4)

        # ── 7. Позиция ────────────────────────────────────────────────────
        pos_font = load_font(FONT_REGULAR_PATH, 28)
        pos_text = f"— {position_label} —"
        px = _centered_x(draw, pos_text, pos_font)
        py = ny + nh + 14
        pb = draw.textbbox((0, 0), pos_text, font=pos_font)
        pw = pb[2] - pb[0]
        draw.rectangle(
            [(W - pw) // 2 - 16, py - 4, (W + pw) // 2 + 16, py + (pb[3] - pb[1]) + 6],
            fill=(10, 10, 16)
        )
        draw.text((px + 1, py + 1), pos_text, font=pos_font, fill=(0, 0, 0))
        draw.text((px, py), pos_text, font=pos_font, fill=accent)

        # ── 8. Нижняя тёмная панель ───────────────────────────────────────
        text_y = BG_H + 22

        meta_font = load_font(FONT_REGULAR_PATH, 22)
        meta_text = f"{date_label}  ·  {mood_label}".strip(" ·")
        h = _draw_centered(draw, meta_text, meta_font, text_y, color=(120, 120, 140), shadow=False)
        text_y += h + 14

        _draw_neon_line(draw, 60, text_y, W - 60, text_y, accent, width=2)
        text_y += 16

        # ── Заголовок наряда ──────────────────────────────────────────────
        max_w = W - 100
        for fs in (48, 42, 36, 32, 28):
            title_font = load_font(FONT_BOLD_PATH, fs)
            title_lines = _wrap_text(draw, header_text.upper(), title_font, max_w)
            if len(title_lines) <= 3:
                break
        for line in title_lines:
            h = _draw_centered(draw, line, title_font, text_y, color=WHITE, shadow=True, so=2)
            text_y += h + 4
        text_y += 8

        # ── Duty badge ────────────────────────────────────────────────────
        duty_font = load_font(FONT_BOLD_PATH, 28)
        duty_title = "СВОДКИ" if duty_type == "svodki" else "ПРОЦЕДУРКА"
        if duty_type == "proc":
            filled = round((proc_day / 2) * 10)
            bar = "▓" * filled + "░" * (10 - filled)
            duty_line = f"[ {bar} ]  ДЕНЬ {proc_day}/2"
        else:
            duty_line = f">>> {duty_title}  /  ДО {time_label} <<<"
        db = draw.textbbox((0, 0), duty_line, font=duty_font)
        dw = db[2] - db[0]
        dx = (W - dw) // 2
        draw.rectangle(
            [dx - 18, text_y - 6, dx + dw + 18, text_y + (db[3] - db[1]) + 10],
            fill=(20, 20, 28), outline=accent, width=2
        )
        draw.text((dx, text_y), duty_line, font=duty_font, fill=accent)
        text_y += (db[3] - db[1]) + 22

        # ── Статистика ────────────────────────────────────────────────────
        stat_label_font = load_font(FONT_REGULAR_PATH, 18)
        stat_val_font   = load_font(FONT_BOLD_PATH, 22)
        badge_w1, badge_h1 = _draw_stat_badge(
            draw, label="НАРЯДОВ ВСЕГО", value=str(total_duties),
            x=100, y=text_y, accent=accent,
            font_label=stat_label_font, font_value=stat_val_font
        )
        badge_w2, badge_h2 = _draw_stat_badge(
            draw, label="СЛЕДУЮЩИЙ", value=f"{next_person} ({next_days}д)",
            x=100 + badge_w1 + 20, y=text_y, accent=accent,
            font_label=stat_label_font, font_value=stat_val_font
        )
        text_y += max(badge_h1, badge_h2) + 20

        # ── Разделитель ───────────────────────────────────────────────────
        draw.line([(120, text_y), (W - 120, text_y)], fill=(40, 40, 55), width=1)
        text_y += 18

        # ── Концовка ──────────────────────────────────────────────────────
        if ending_text:
            end_font = load_font(FONT_REGULAR_PATH, 26)
            end_lines = _wrap_text(draw, ending_text, end_font, max_w)
            for line in end_lines[:3]:
                h = _draw_centered(draw, line, end_font, text_y,
                                   color=(185, 185, 200), shadow=True, so=2)
                text_y += h + 4

        # ── Футер ────────────────────────────────────────────────────────
        foot_font = load_font(FONT_BOLD_PATH, 16)
        foot_text = f"◈ НАРЯД-БОТ  ×  {theme['title']} ◈"
        fx = _centered_x(draw, foot_text, foot_font)
        draw.text((fx, H - MARGIN - 22), foot_text, font=foot_font, fill=(55, 55, 70))

        _draw_noise(img, amount=7)

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
        ending_text="если ты это видишь — генератор работает",
        date_label="19.06.2026 • пятница",
        mood_label="геймерский",
        theme_key=random.choice(THEME_KEYS),
        output_path=os.path.join(BASE_DIR, "temp", "selftest.jpg"),
    )
    print("Тема:", out)
