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
import math
import random
import sys
import colorsys
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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


def _shift_hue(rgb, degrees):
    """Сдвигает оттенок цвета на `degrees` градусов — используется, чтобы
    получить вторую (парную) акцентную краску из основной темы без того,
    чтобы вручную прописывать её для всех 23 тем."""
    r, g, b = [c / 255 for c in rgb]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    h = (h + degrees / 360.0) % 1.0
    r2, g2, b2 = colorsys.hls_to_rgb(h, min(0.72, max(0.32, l)), min(1.0, s * 1.05))
    return (int(r2 * 255), int(g2 * 255), int(b2 * 255))


def get_accent2(accent):
    """Вторая акцентная краска темы — тёплый/холодный сосед по кругу цвета.
    Используется для двухцветных градиентов, свечения и текста."""
    return _shift_hue(accent, 38)


# ══════════════════════════════════════════════
#  СТИЛЕВЫЕ ПАКЕТЫ — задают "характер" конкретной карточки
# ══════════════════════════════════════════════
# Каждая генерация карточки случайно выбирает один пакет — набор:
#   - формы уголков (corner)
#   - формы разделительной линии (line)
#   - формы бейджей (badge)
#   - предпочитаемых стилей фона (bg)
# Это даёт разнообразие БЕЗ потери цельности: элементы одной карточки
# нарисованы в одном "почерке", а не случайной мешаниной.
STYLE_PACKS = {
    "tactical": {"corner": "bracket", "line": "solid",   "badge": "rect",  "bg": ["circuit", "nebula"]},
    "soft":     {"corner": "dot",     "line": "dashed",  "badge": "pill",  "bg": ["aurora", "wave"]},
    "hex":      {"corner": "hex",     "line": "diamond", "badge": "cut",   "bg": ["sunburst", "particles"]},
    "cyber":    {"corner": "frame",   "line": "solid",   "badge": "cut",   "bg": ["circuit", "sunburst"]},
    "orbit":    {"corner": "dot",     "line": "diamond", "badge": "pill",  "bg": ["particles", "aurora"]},
}
STYLE_KEYS = list(STYLE_PACKS.keys())


def pick_style():
    """Возвращает (style_key, style_dict) — случайный стилевой пакет на карточку."""
    key = random.choice(STYLE_KEYS)
    return key, STYLE_PACKS[key]


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


def _draw_vignette(img, strength=90):
    """Затемняет края картинки, оставляя центр светлее — придаёт глубину."""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cx, cy = w // 2, int(h * 0.42)
    max_r = int(math.hypot(w, h) * 0.6)
    steps = 10
    for i in range(steps):
        r = int(max_r * (i + 1) / steps)
        a = int(strength * (i / steps) ** 2)
        od.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 0, 0, a), width=max_r // steps + 2)
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    return img_rgba.convert("RGB")


def _base_gradient(width, height, accent, accent2, diagonal=True):
    """Базовый двухцветный тёмный градиент — общая основа для всех стилей фона."""
    img = Image.new("RGB", (width, height), (4, 4, 8))
    draw = ImageDraw.Draw(img)
    dark_a = tuple(max(0, c - 170) for c in accent)
    dark_b = tuple(max(0, c - 190) for c in accent2)
    for y in range(height):
        t = y / height
        mix = 0.65 + 0.35 * t if diagonal else 0.5
        r = int(6 + (dark_a[0] * (1 - t) + dark_b[0] * t * 0.5) * 0.6)
        g = int(6 + (dark_a[1] * (1 - t) + dark_b[1] * t * 0.5) * 0.6)
        b = int(10 + (dark_a[2] * (1 - t) + dark_b[2] * t * 0.5) * 0.7)
        draw.line([(0, y), (width, y)], fill=(min(255, r), min(255, g), min(255, b)))
    return img


def _draw_sunburst_rays(odraw, width, height, cx, cy, accent, accent2, n=26):
    """Веерные лучи из одной точки — эффект софита/восхода."""
    max_len = int(math.hypot(width, height))
    for i in range(n):
        angle = (2 * math.pi / n) * i + random.uniform(-0.05, 0.05)
        length = int(max_len * random.uniform(0.7, 1.0))
        x2 = cx + int(length * math.cos(angle))
        y2 = cy + int(length * math.sin(angle))
        col = accent if i % 2 == 0 else accent2
        alpha = random.randint(10, 34)
        odraw.line([(cx, cy), (x2, y2)], fill=col + (alpha,), width=random.randint(2, 5))


def _draw_circuit_lines(odraw, width, height, accent, spacing=90):
    """Прямоугольная 'плата' — ломаные линии-дорожки с узлами, техно-стиль."""
    cols = width // spacing + 2
    rows = height // spacing + 2
    for _ in range(random.randint(14, 22)):
        cx = random.randint(0, cols - 1) * spacing
        cy = random.randint(0, rows - 1) * spacing
        path_len = random.randint(2, 5)
        pts = [(cx, cy)]
        for _ in range(path_len):
            if random.random() < 0.5:
                cx += spacing * random.choice([-1, 1])
            else:
                cy += spacing * random.choice([-1, 1])
            pts.append((cx, cy))
        alpha = random.randint(18, 45)
        odraw.line(pts, fill=accent + (alpha,), width=1)
        node_r = random.choice([2, 2, 3, 4])
        for px, py in pts:
            if random.random() < 0.6:
                odraw.ellipse([px - node_r, py - node_r, px + node_r, py + node_r],
                              fill=accent + (min(255, alpha + 40),))


def _draw_wave_bands(odraw, width, height, accent, accent2, n=9):
    """Плавные горизонтальные волновые ленты — мягкий, 'эфирный' стиль."""
    for i in range(n):
        base_y = height * (0.15 + 0.75 * i / n)
        amp = random.uniform(18, 55)
        freq = random.uniform(1.3, 2.6)
        phase = random.uniform(0, 6.28)
        col = accent if i % 2 == 0 else accent2
        alpha = random.randint(14, 34)
        pts = []
        step = 14
        for x in range(0, width + step, step):
            y = base_y + amp * math.sin((x / width) * freq * 2 * math.pi + phase)
            pts.append((x, y))
        odraw.line(pts, fill=col + (alpha,), width=random.randint(2, 4))


def _draw_particles_field(odraw, width, height, accent, accent2, n=90):
    """Плотное поле разноразмерных частиц/боке-огней двух акцентных цветов."""
    for _ in range(n):
        sx = random.randint(0, width)
        sy = random.randint(0, height)
        col = accent if random.random() < 0.6 else accent2
        r = random.choice([1, 1, 2, 2, 3, 6, 10])
        alpha = random.randint(20, 150) if r <= 3 else random.randint(10, 35)
        odraw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=col + (alpha,))


def _draw_geo_shapes(odraw, width, height, accent, accent2, n=6):
    """Случайные геометрические фигуры (ромбы/треугольники/кресты)."""
    for _ in range(n):
        sx = random.randint(0, width)
        sy = random.randint(0, height)
        size = random.randint(20, 80)
        alpha_s = random.randint(10, 40)
        col_s = (accent if random.random() < 0.5 else accent2) + (alpha_s,)
        shape_type = random.choice(['diamond', 'triangle', 'cross'])
        if shape_type == 'diamond':
            odraw.polygon([(sx, sy - size), (sx + size, sy), (sx, sy + size), (sx - size, sy)], outline=col_s)
        elif shape_type == 'triangle':
            odraw.polygon([(sx, sy - size), (sx + size, sy + size), (sx - size, sy + size)], outline=col_s)
        else:
            odraw.line([(sx - size, sy), (sx + size, sy)], fill=col_s, width=2)
            odraw.line([(sx, sy - size), (sx, sy + size)], fill=col_s, width=2)


BG_STYLES = ["nebula", "circuit", "sunburst", "wave", "particles", "aurora"]


def generate_background(theme_key, width, height, bg_style=None):
    """Рисует фон-арт карточки. bg_style выбирает "характер" рисунка —
    если не задан, выбирается случайно, так что даже карточки одной темы
    выглядят по-разному от раза к разу."""
    theme = THEMES.get(theme_key, THEMES["matrix"])
    accent = theme["accent"]
    accent2 = get_accent2(accent)
    style = bg_style if bg_style in BG_STYLES else random.choice(BG_STYLES)

    cx, cy = width // 2, int(height * 0.38)
    img = _base_gradient(width, height, accent, accent2)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    if style == "nebula":
        _draw_glow_circle(odraw, cx, cy, int(min(width, height) * 0.55), accent, max_alpha=55)
        ox = cx + random.randint(-width // 5, width // 5)
        oy = cy + random.randint(-height // 8, height // 8)
        _draw_glow_circle(odraw, ox, oy, int(min(width, height) * 0.25), accent2, max_alpha=75)
        ring_step = random.randint(70, 110)
        for ring_r in range(ring_step, max(width, height), ring_step):
            alpha = max(8, 80 - ring_r // 14)
            odraw.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
                          outline=accent + (alpha,), width=random.randint(1, 3))
        for _ in range(random.randint(5, 10)):
            angle_r = random.uniform(0, 6.28)
            length = random.randint(int(height * 0.3), int(height * 0.9))
            x1 = cx + int(random.uniform(-60, 60))
            y1 = cy + int(random.uniform(-40, 40))
            x2 = x1 + int(length * math.cos(angle_r))
            y2 = y1 + int(length * math.sin(angle_r))
            odraw.line([(x1, y1), (x2, y2)], fill=accent2 + (random.randint(12, 50),), width=random.randint(1, 3))
        _draw_particles_field(odraw, width, height, accent, accent2, n=random.randint(60, 110))
        _draw_geo_shapes(odraw, width, height, accent, accent2, n=random.randint(3, 7))

    elif style == "circuit":
        _draw_glow_circle(odraw, cx, cy, int(min(width, height) * 0.5), accent, max_alpha=40)
        _draw_circuit_lines(odraw, width, height, accent2, spacing=random.randint(70, 110))
        _draw_particles_field(odraw, width, height, accent, accent2, n=random.randint(30, 60))

    elif style == "sunburst":
        origin_x = random.choice([int(width * 0.15), int(width * 0.5), int(width * 0.85)])
        origin_y = int(height * random.uniform(0.1, 0.3))
        _draw_sunburst_rays(odraw, width, height, origin_x, origin_y, accent, accent2, n=random.randint(20, 30))
        _draw_glow_circle(odraw, origin_x, origin_y, int(min(width, height) * 0.35), accent2, max_alpha=90)
        _draw_particles_field(odraw, width, height, accent, accent2, n=random.randint(40, 70))

    elif style == "wave":
        _draw_glow_circle(odraw, cx, cy, int(min(width, height) * 0.45), accent, max_alpha=35)
        _draw_wave_bands(odraw, width, height, accent, accent2, n=random.randint(7, 11))
        _draw_particles_field(odraw, width, height, accent, accent2, n=random.randint(30, 50))

    elif style == "particles":
        _draw_glow_circle(odraw, cx, cy, int(min(width, height) * 0.4), accent, max_alpha=45)
        _draw_particles_field(odraw, width, height, accent, accent2, n=random.randint(110, 170))
        _draw_geo_shapes(odraw, width, height, accent, accent2, n=random.randint(2, 5))

    elif style == "aurora":
        for i in range(random.randint(3, 5)):
            bx = random.randint(-width // 4, width)
            by = int(height * random.uniform(0.05, 0.6))
            col = accent if i % 2 == 0 else accent2
            _draw_glow_circle(odraw, bx, by, int(min(width, height) * random.uniform(0.3, 0.55)),
                              col, max_alpha=random.randint(35, 60))
        _draw_wave_bands(odraw, width, height, accent, accent2, n=random.randint(5, 8))
        _draw_particles_field(odraw, width, height, accent, accent2, n=random.randint(40, 70))

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img = img_rgba.convert("RGB")

    # ── Гексагональная сетка (почти всегда, лёгким слоем) ──────────────────
    if random.random() < 0.8:
        hex_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_hex_grid(ImageDraw.Draw(hex_overlay), width, height, accent,
                       spacing=random.randint(90, 150), alpha=random.randint(10, 22))
        img_rgba = img.convert("RGBA")
        img_rgba.alpha_composite(hex_overlay)
        img = img_rgba.convert("RGB")

    img = _draw_vignette(img, strength=random.randint(60, 100))
    img = _draw_scan_lines(img, height, alpha=random.randint(6, 16))

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


def _draw_glow_text(img, xy, text, font, fill, glow_color=None, blur_radius=10,
                    glow_passes=2, outline_color=(0, 0, 0), outline_w=4):
    """Рисует текст с настоящим неоновым свечением (размытие по Гауссу),
    а не просто жирной обводкой — даёт эффект как на референсных карточках.
    img должен быть RGB-изображением; функция рисует ПРЯМО в него."""
    if glow_color is None:
        glow_color = fill
    x, y = xy
    pad = blur_radius * 4 + 20

    layer = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    ldraw.text((x + pad, y + pad), text, font=font, fill=glow_color + (255,))

    glow = layer
    for i in range(glow_passes):
        glow = glow.filter(ImageFilter.GaussianBlur(blur_radius))
    # усиливаем яркость свечения, слегка приглушая альфу базового слоя
    glow_alpha = glow.split()[3].point(lambda a: min(255, int(a * 1.6)))
    glow.putalpha(glow_alpha)

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(glow, (-pad, -pad))
    img.paste(img_rgba.convert("RGB"), (0, 0))

    draw = ImageDraw.Draw(img)
    _draw_outlined_text(draw, (x, y), text, font, fill=fill,
                        outline_color=outline_color, outline_w=outline_w)


def _draw_icon(draw, cx, cy, r, kind, color):
    """Простые абстрактные векторные значки (не логотипы игр) для бейджей:
    diamond / shield / star / chevron / hex."""
    if kind == "diamond":
        draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
                     outline=color, width=2)
        draw.polygon([(cx, cy - r * 0.4), (cx + r * 0.4, cy), (cx, cy + r * 0.4), (cx - r * 0.4, cy)],
                     fill=color)
    elif kind == "shield":
        pts = [(cx, cy - r), (cx + r * 0.85, cy - r * 0.5), (cx + r * 0.85, cy + r * 0.2),
               (cx, cy + r), (cx - r * 0.85, cy + r * 0.2), (cx - r * 0.85, cy - r * 0.5)]
        draw.polygon(pts, outline=color, width=2)
        draw.line([(cx, cy - r * 0.45), (cx, cy + r * 0.45)], fill=color, width=2)
    elif kind == "star":
        pts = []
        for i in range(10):
            ang = math.pi / 2 + i * math.pi / 5
            rr = r if i % 2 == 0 else r * 0.42
            pts.append((cx + rr * math.cos(ang), cy - rr * math.sin(ang)))
        draw.polygon(pts, outline=color, width=2)
    elif kind == "chevron":
        draw.line([(cx - r * 0.6, cy - r * 0.5), (cx, cy), (cx - r * 0.6, cy + r * 0.5)],
                  fill=color, width=3, joint="curve")
        draw.line([(cx, cy - r * 0.5), (cx + r * 0.6, cy), (cx, cy + r * 0.5)],
                  fill=color, width=3, joint="curve")
    else:  # hex
        pts = [(cx + r * math.cos(math.radians(60 * i - 30)),
                cy + r * math.sin(math.radians(60 * i - 30))) for i in range(6)]
        draw.polygon(pts, outline=color, width=2)


def _draw_panel_frame(draw, margin, accent, cut=26, width=2):
    """Полноконтурная скошенная рамка-панель вокруг всей карточки
    (углы срезаны, как на техно-интерфейсах) — вместо/вместе с уголками."""
    x0, y0, x1, y1 = margin, margin, W - margin, H - margin
    pts = [
        (x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut),
        (x1, y1 - cut), (x1 - cut, y1), (x0 + cut, y1),
        (x0, y1 - cut), (x0, y0 + cut),
    ]
    draw.line(pts + [pts[0]], fill=accent, width=width)
    # маленькие засечки на срезах углов
    for px, py in pts:
        draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=accent)


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

CORNER_STYLES = ["bracket", "dot", "hex", "frame"]


def _draw_corner_decorations(draw, accent, margin=28, style=None):
    """Рисует угловой декор в каждом углу карточки. style задаёт форму —
    если не передан, выбирается случайно (это делает карточки разнообразнее
    даже в рамках одной темы)."""
    style = style if style in CORNER_STYLES else random.choice(CORNER_STYLES)
    arm = 44
    thick = 3
    col = accent
    corners = [
        (margin, margin,        +1, 0,  0, +1),
        (W - margin, margin,   -1, 0,  0, +1),
        (margin, H - margin,   +1, 0,  0, -1),
        (W - margin, H - margin, -1, 0, 0, -1),
    ]

    if style == "bracket":
        for x, y, dhx, dhy, dvx, dvy in corners:
            draw.line([(x, y), (x + dhx * arm, y + dhy * arm)], fill=col, width=thick)
            draw.line([(x, y), (x + dvx * arm, y + dvy * arm)], fill=col, width=thick)
            draw.rectangle([x - 4, y - 4, x + 4, y + 4], fill=col)

    elif style == "dot":
        for x, y, dhx, dhy, dvx, dvy in corners:
            draw.arc([x - arm, y - arm, x + arm, y + arm], 0, 360, fill=col, width=2)
            draw.ellipse([x - 5, y - 5, x + 5, y + 5], fill=col)
            draw.line([(x + dhx * 8, y + dhy * 8), (x + dhx * arm, y + dhy * arm)], fill=col, width=2)
            draw.line([(x + dvx * 8, y + dvy * 8), (x + dvx * arm, y + dvy * arm)], fill=col, width=2)

    elif style == "hex":
        for x, y, dhx, dhy, dvx, dvy in corners:
            s = 18
            pts = [
                (x + dhx * s, y), (x + dhx * s * 1.6, y + dvy * s * 0.9),
                (x + dhx * s * 1.6, y + dvy * s * 1.8), (x + dhx * s, y + dvy * s * 2.4),
                (x, y + dvy * s * 1.8), (x, y + dvy * s * 0.9),
            ]
            draw.polygon(pts, outline=col, width=2)
            draw.line([(x + dhx * s * 1.6, y), (x + dhx * arm, y)], fill=col, width=2)
            draw.line([(x, y + dvy * s * 2.4), (x, y + dvy * arm)], fill=col, width=2)

    elif style == "frame":
        inset = 14
        draw.rounded_rectangle(
            [margin - inset, margin - inset, W - margin + inset, H - margin + inset],
            radius=22, outline=col, width=1
        )
        for x, y, dhx, dhy, dvx, dvy in corners:
            draw.line([(x, y), (x + dhx * (arm - 10), y + dhy * (arm - 10))], fill=col, width=thick)
            draw.line([(x, y), (x + dvx * (arm - 10), y + dvy * (arm - 10))], fill=col, width=thick)


LINE_STYLES = ["solid", "dashed", "diamond"]


def _draw_neon_line(draw, x1, y1, x2, y2, accent, width=3, style=None):
    """Рисует разделительную линию с эффектом неонового свечения.
    style: solid (сплошная), dashed (пунктир), diamond (линия из ромбиков)."""
    style = style if style in LINE_STYLES else random.choice(LINE_STYLES)
    r, g, b = accent
    glow = (min(255, r + 60), min(255, g + 60), min(255, b + 60))

    if style == "solid":
        draw.line([(x1, y1), (x2, y2)], fill=glow, width=width + 4)
        draw.line([(x1, y1), (x2, y2)], fill=(255, 255, 255), width=1)

    elif style == "dashed":
        total = math.hypot(x2 - x1, y2 - y1)
        if total <= 0:
            return
        ux, uy = (x2 - x1) / total, (y2 - y1) / total
        dash, gap = 16, 10
        pos = 0.0
        while pos < total:
            seg_end = min(pos + dash, total)
            sx1, sy1 = x1 + ux * pos, y1 + uy * pos
            sx2, sy2 = x1 + ux * seg_end, y1 + uy * seg_end
            draw.line([(sx1, sy1), (sx2, sy2)], fill=glow, width=width + 3)
            draw.line([(sx1, sy1), (sx2, sy2)], fill=(255, 255, 255), width=1)
            pos += dash + gap

    elif style == "diamond":
        dim = tuple(c // 2 for c in accent)
        draw.line([(x1, y1), (x2, y2)], fill=dim, width=1)
        total = math.hypot(x2 - x1, y2 - y1)
        if total <= 0:
            return
        ux, uy = (x2 - x1) / total, (y2 - y1) / total
        step = 26
        pos = step / 2
        s = 4
        while pos < total:
            px, py = x1 + ux * pos, y1 + uy * pos
            draw.polygon([(px, py - s), (px + s, py), (px, py + s), (px - s, py)], fill=glow)
            pos += step


BADGE_SHAPES = ["rect", "pill", "cut"]


def _badge_rect(draw, box, accent, shape=None, fill=(16, 16, 22), outline_w=1):
    """Рисует фон+рамку бейджа заданной формы в прямоугольнике box=(x0,y0,x1,y1).
    Возвращает использованную форму (текст поверх рисуется вызывающим кодом)."""
    shape = shape if shape in BADGE_SHAPES else random.choice(BADGE_SHAPES)
    x0, y0, x1, y1 = box
    if shape == "rect":
        draw.rectangle(box, fill=fill, outline=accent, width=outline_w)
    elif shape == "pill":
        radius = min(18, (y1 - y0) // 2)
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=accent, width=outline_w)
    elif shape == "cut":
        c = min(12, (y1 - y0) // 3)
        pts = [
            (x0 + c, y0), (x1 - c, y0), (x1, y0 + c),
            (x1, y1 - c), (x1 - c, y1), (x0 + c, y1),
            (x0, y1 - c), (x0, y0 + c),
        ]
        draw.polygon(pts, fill=fill, outline=accent)
    return shape


def _draw_stat_badge(draw, label, value, x, y, accent, font_label, font_value, shape=None, icon=None):
    """Рисует маленький бейдж со статистикой. icon (опционально) — простой
    векторный значок слева ('diamond'/'shield'/'star'/'chevron'/'hex')."""
    pad_x, pad_y = 14, 10
    lb = draw.textbbox((0, 0), label, font=font_label)
    vb = draw.textbbox((0, 0), value, font=font_value)
    text_w = max(lb[2] - lb[0], vb[2] - vb[0])
    box_h = (lb[3] - lb[1]) + (vb[3] - vb[1]) + pad_y * 3
    icon_w = box_h - 2 * pad_y + 10 if icon else 0
    box_w = text_w + pad_x * 2 + icon_w
    _badge_rect(draw, (x, y, x + box_w, y + box_h), accent, shape=shape)
    text_x = x + pad_x
    if icon:
        icon_r = (box_h - 2 * pad_y) // 2
        icon_cx = x + pad_x + icon_r
        icon_cy = y + box_h // 2
        _draw_icon(draw, icon_cx, icon_cy, icon_r, icon, accent)
        text_x = x + pad_x + icon_w
    draw.text((text_x, y + pad_y), label, font=font_label, fill=(130, 130, 150))
    draw.text((text_x, y + pad_y + (lb[3] - lb[1]) + 4), value,
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
        accent2 = get_accent2(accent)
        style_key, style = pick_style()

        header_text = _clean_for_image(header_text)
        ending_text = _clean_for_image(ending_text)
        mood_label  = _clean_for_image(mood_label)

        # ── 1. Фон-арт ────────────────────────────────────────────────────
        bg = generate_background(theme_key, W, BG_H, bg_style=random.choice(style["bg"]))
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
        _draw_corner_decorations(draw, accent, margin=30, style=style["corner"])

        # ── 4. Верхняя строка: бейдж темы + время ────────────────────────
        badge_font = load_font(FONT_BOLD_PATH, 24)
        badge_text = f"◈ {theme['title']} ◈"
        bb = draw.textbbox((0, 0), badge_text, font=badge_font)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        pad = 10
        _badge_rect(draw, (MARGIN - pad, MARGIN - 6, MARGIN + bw + pad, MARGIN + bh + 8),
                    accent, shape=style["badge"], fill=(12, 12, 18))
        draw.text((MARGIN, MARGIN), badge_text, font=badge_font, fill=accent)

        time_font = load_font(FONT_BOLD_PATH, 24)
        tb = draw.textbbox((0, 0), time_label, font=time_font)
        tw = tb[2] - tb[0]
        _badge_rect(draw, (W - MARGIN - tw - 2 * pad, MARGIN - 6, W - MARGIN + pad, MARGIN + bh + 8),
                    accent2, shape=style["badge"], fill=(12, 12, 18))
        draw.text((W - MARGIN - tw - pad, MARGIN), time_label, font=time_font, fill=WHITE)

        # ── 5. Горизонтальная неоновая линия ─────────────────────────────
        line_y = MARGIN + bh + 18
        _draw_neon_line(draw, MARGIN, line_y, W - MARGIN, line_y, accent, width=1, style=style["line"])

        # ── 6. Имя дежурного (двухцветная обводка: accent2 снаружи, тень внутри) ──
        for name_size in (100, 88, 76, 64, 54):
            name_font = load_font(FONT_BOLD_PATH, name_size)
            nb = draw.textbbox((0, 0), person, font=name_font)
            if nb[2] - nb[0] <= W - 160:
                break
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = (W - nw) // 2
        ny = BG_H - nh - 160
        draw.text((nx + 3, ny + 3), person, font=name_font, fill=accent2)
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

        _draw_neon_line(draw, 60, text_y, W - 60, text_y, accent, width=2, style=style["line"])
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
            # Настоящая полоса прогресса (вместо символов ▓░) — выглядит
            # аккуратнее и не зависит от того, как шрифт рисует блоки.
            label_text = f"{duty_title}  ·  ДЕНЬ {proc_day}/2"
            lb = draw.textbbox((0, 0), label_text, font=duty_font)
            lw, lh = lb[2] - lb[0], lb[3] - lb[1]
            bar_w = min(360, W - 200)
            bar_h = 22
            box_w = max(bar_w, lw) + 60
            box_h = lh + bar_h + 34
            box_x0 = (W - box_w) // 2
            _badge_rect(draw, (box_x0, text_y - 6, box_x0 + box_w, text_y - 6 + box_h),
                        accent, shape=style["badge"], fill=(20, 20, 28), outline_w=2)
            draw.text(((W - lw) // 2, text_y + 6), label_text, font=duty_font, fill=accent)
            bar_y = text_y + 6 + lh + 12
            bar_x = (W - bar_w) // 2
            draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                                   radius=bar_h // 2, fill=(30, 30, 40), outline=accent2, width=1)
            filled_w = int(bar_w * min(1.0, proc_day / 2))
            if filled_w > bar_h:
                draw.rounded_rectangle([bar_x, bar_y, bar_x + filled_w, bar_y + bar_h],
                                       radius=bar_h // 2, fill=accent)
            text_y += box_h + 16
        else:
            duty_line = f">>> {duty_title}  /  ДО {time_label} <<<"
            db = draw.textbbox((0, 0), duty_line, font=duty_font)
            dw = db[2] - db[0]
            dx = (W - dw) // 2
            _badge_rect(draw, (dx - 18, text_y - 6, dx + dw + 18, text_y + (db[3] - db[1]) + 10),
                        accent, shape=style["badge"], fill=(20, 20, 28), outline_w=2)
            draw.text((dx, text_y), duty_line, font=duty_font, fill=accent)
            text_y += (db[3] - db[1]) + 22

        # ── Статистика ────────────────────────────────────────────────────
        stat_label_font = load_font(FONT_REGULAR_PATH, 18)
        stat_val_font   = load_font(FONT_BOLD_PATH, 22)
        badge_w1, badge_h1 = _draw_stat_badge(
            draw, label="НАРЯДОВ ВСЕГО", value=str(total_duties),
            x=100, y=text_y, accent=accent,
            font_label=stat_label_font, font_value=stat_val_font, shape=style["badge"]
        )
        badge_w2, badge_h2 = _draw_stat_badge(
            draw, label="СЛЕДУЮЩИЙ", value=f"{next_person} ({next_days}д)",
            x=100 + badge_w1 + 20, y=text_y, accent=accent2,
            font_label=stat_label_font, font_value=stat_val_font, shape=style["badge"]
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
        print(f"✅ Карточка сохранена: {output_path} ({theme_key}, стиль {style_key})", flush=True)
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
