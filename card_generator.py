"""
Генератор красивых карточек-напоминаний через Pillow (HUD / Glassmorphism v2).

ВАЖНО про шрифты:
В репозитории лежит папка fonts/ с шрифтом DejaVu Sans (обычный и bold).
Это сделано специально, чтобы карточка корректно рисовалась на ЛЮБОМ
хостинге — даже если хостинг разворачивает проект не через Dockerfile
и в системе нет шрифтов с кириллицей.
"""

import os
import re
import math
import random
import sys
import colorsys
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ══════════════════════════════════════════════
#  ШРИФТЫ
# ══════════════════════════════════════════════
_FONT_CANDIDATES_BOLD = [
    os.path.join(BASE_DIR, "fonts", "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
]
_FONT_CANDIDATES_REG = [
    os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf"),
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
        return ""
    text = _EMOJI_PATTERN.sub("", str(text))
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


# ══════════════════════════════════════════════
#  РАЗМЕРЫ И БАЗОВЫЕ ЦВЕТА
# ══════════════════════════════════════════════
W, H = 1080, 1350
BG_H = 620
MARGIN = 40
WHITE = (255, 255, 255)
DARK = (10, 11, 16)

# ══════════════════════════════════════════════
#  23 ТЕМЫ
# ══════════════════════════════════════════════
THEMES = {
    "cs2":          {"title": "CS2",               "accent": (255, 178, 40)},
    "dota2":        {"title": "DOTA 2",            "accent": (225, 65, 65)},
    "genshin":      {"title": "GENSHIN IMPACT",    "accent": (115, 200, 255)},
    "valorant":     {"title": "VALORANT",          "accent": (255, 70, 88)},
    "minecraft":    {"title": "MINECRAFT",         "accent": (105, 215, 90)},
    "gta":          {"title": "GTA",               "accent": (255, 215, 55)},
    "cyberpunk":    {"title": "CYBERPUNK",         "accent": (250, 232, 18)},
    "lol":          {"title": "LEAGUE OF LEGENDS", "accent": (25, 210, 230)},
    "apex":         {"title": "APEX LEGENDS",      "accent": (255, 95, 35)},
    "fortnite":     {"title": "FORTNITE",          "accent": (160, 95, 255)},
    "overwatch":    {"title": "OVERWATCH",         "accent": (255, 155, 30)},
    "amongus":      {"title": "AMONG US",          "accent": (255, 65, 65)},
    "warzone":      {"title": "CALL OF DUTY",      "accent": (165, 185, 105)},
    "pubg":         {"title": "PUBG",              "accent": (245, 178, 55)},
    "witcher":      {"title": "THE WITCHER",       "accent": (215, 175, 98)},
    "eldenring":    {"title": "ELDEN RING",        "accent": (235, 198, 115)},
    "stardew":      {"title": "STARDEW VALLEY",    "accent": (255, 175, 95)},
    "hollowknight": {"title": "HOLLOW KNIGHT",     "accent": (120, 210, 240)},
    "portal":       {"title": "PORTAL",            "accent": (255, 148, 30)},
    "starwars":     {"title": "STAR WARS",         "accent": (255, 222, 105)},
    "marvel":       {"title": "SUPERHEROES",       "accent": (240, 50, 55)},
    "synthwave":    {"title": "SYNTHWAVE",         "accent": (255, 60, 185)},
    "matrix":       {"title": "TERMINAL",          "accent": (55, 255, 115)},
}
THEME_KEYS = list(THEMES.keys())


def _shift_hue(rgb, degrees, light_boost=1.0, sat_boost=1.05):
    r, g, b = [c / 255.0 for c in rgb]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    h = (h + degrees / 360.0) % 1.0
    l = min(0.78, max(0.32, l * light_boost))
    s = min(1.0, s * sat_boost)
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return (int(r2 * 255), int(g2 * 255), int(b2 * 255))


def get_accent2(accent):
    """Вторая акцентная краска темы — гармоничный сосед по цветовому кругу."""
    return _shift_hue(accent, 42, light_boost=1.06)


def _lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _brighten(rgb, factor=1.35):
    return tuple(min(255, int(c * factor + 25)) for c in rgb)


# ══════════════════════════════════════════════
#  СТИЛЕВЫЕ ПАКЕТЫ
# ══════════════════════════════════════════════
STYLE_PACKS = {
    "tactical": {"corner": "bracket", "line": "solid",   "badge": "cut",  "bg": ["circuit", "nebula"]},
    "soft":     {"corner": "dot",     "line": "dashed",  "badge": "pill", "bg": ["aurora", "wave"]},
    "hex":      {"corner": "hex",     "line": "diamond", "badge": "cut",  "bg": ["sunburst", "particles"]},
    "cyber":    {"corner": "frame",   "line": "solid",   "badge": "cut",  "bg": ["circuit", "sunburst"]},
    "orbit":    {"corner": "dot",     "line": "diamond", "badge": "pill", "bg": ["particles", "aurora"]},
}
STYLE_KEYS = list(STYLE_PACKS.keys())


def pick_style():
    key = random.choice(STYLE_KEYS)
    return key, STYLE_PACKS[key]


# ══════════════════════════════════════════════
#  ФОН — КРАСИВЫЙ АРТ
# ══════════════════════════════════════════════
def _draw_glow_circle(overlay_draw, cx, cy, r, color_rgb, max_alpha=120):
    layers = 10
    for i in range(layers):
        ratio = i / layers
        cur_r = int(r * (1 - ratio * 0.72))
        alpha = int(max_alpha * ((1 - ratio) ** 1.4))
        if cur_r > 0 and alpha > 0:
            overlay_draw.ellipse(
                [cx - cur_r, cy - cur_r, cx + cur_r, cy + cur_r],
                fill=color_rgb + (alpha,),
            )


def _draw_hex_grid(draw, width, height, accent, spacing=115, alpha=16):
    hex_w = spacing
    hex_h = int(hex_w * 0.866)
    col_r = (*accent, alpha)
    for row in range(-1, height // hex_h + 2):
        for col in range(-1, width // hex_w + 2):
            cx = col * hex_w + (hex_w // 2 if row % 2 else 0)
            cy = row * hex_h
            pts = [
                (
                    cx + (spacing * 0.5) * math.cos(math.radians(60 * i - 30)),
                    cy + (spacing * 0.5) * math.sin(math.radians(60 * i - 30)),
                )
                for i in range(6)
            ]
            for i in range(6):
                draw.line([pts[i], pts[(i + 1) % 6]], fill=col_r, width=1)


def _draw_cyber_perspective_grid(odraw, width, height, horizon_y, accent, alpha=26):
    """Перспективная сетка в стиле Synthwave / Sci-Fi HUD."""
    vanish_x = width // 2
    bottom_y = height
    num_v = 14
    for i in range(-num_v, num_v + 1):
        bx = vanish_x + i * (width // 6)
        odraw.line([(vanish_x, horizon_y), (bx, bottom_y)], fill=accent + (alpha,), width=1)
    steps = 9
    for s in range(1, steps + 1):
        t = (s / steps) ** 1.7
        y = int(horizon_y + (bottom_y - horizon_y) * t)
        line_alpha = int(alpha * (0.4 + 0.6 * t))
        odraw.line([(0, y), (width, y)], fill=accent + (line_alpha,), width=1)


def _draw_plexus(odraw, width, height, accent, accent2, n=36, max_dist=180):
    """Созвездие из точек, соединённых тонкими линиями на близкой дистанции."""
    pts = [(random.randint(40, width - 40), random.randint(40, int(height * 0.85))) for _ in range(n)]
    for i in range(n):
        x1, y1 = pts[i]
        for j in range(i + 1, n):
            x2, y2 = pts[j]
            d = math.hypot(x2 - x1, y2 - y1)
            if d < max_dist:
                a = int(38 * (1.0 - d / max_dist))
                if a > 3:
                    odraw.line([(x1, y1), (x2, y2)], fill=accent + (a,), width=1)
    for idx, (px, py) in enumerate(pts):
        col = accent if idx % 2 == 0 else accent2
        r = random.choice([2, 3, 4])
        odraw.ellipse([px - r - 2, py - r - 2, px + r + 2, py + r + 2], fill=col + (35,))
        odraw.ellipse([px - r, py - r, px + r, py + r], fill=WHITE + (150,))


def _draw_hud_reticle(odraw, cx, cy, base_r, accent, accent2):
    """Тактический круговой прицел/радар с дугами и засечками."""
    odraw.ellipse(
        [cx - base_r, cy - base_r, cx + base_r, cy + base_r],
        outline=accent + (38,), width=1
    )
    r2 = int(base_r * 0.78)
    odraw.ellipse(
        [cx - r2, cy - r2, cx + r2, cy + r2],
        outline=accent2 + (28,), width=1
    )
    # Дуги-сегменты
    for start_ang in (15, 115, 205, 295):
        odraw.arc(
            [cx - base_r - 12, cy - base_r - 12, cx + base_r + 12, cy + base_r + 12],
            start=start_ang, end=start_ang + 48, fill=accent + (70,), width=3
        )
    # Засечки по кругу
    for deg in range(0, 360, 15):
        rad = math.radians(deg)
        inner = base_r - (10 if deg % 45 == 0 else 5)
        outer = base_r + (4 if deg % 45 == 0 else 0)
        x1, y1 = cx + inner * math.cos(rad), cy + inner * math.sin(rad)
        x2, y2 = cx + outer * math.cos(rad), cy + outer * math.sin(rad)
        a = 75 if deg % 45 == 0 else 32
        odraw.line([(x1, y1), (x2, y2)], fill=accent + (a,), width=2 if deg % 45 == 0 else 1)


def _draw_scan_lines(img, height, alpha=10):
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(0, height, 4):
        draw.line([(0, y), (img.width, y)], fill=(0, 0, 0, alpha))
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    return img_rgba.convert("RGB")


def _draw_vignette(img, strength=95):
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cx, cy = w // 2, int(h * 0.38)
    max_r = int(math.hypot(w, h) * 0.62)
    steps = 14
    for i in range(steps):
        r = int(max_r * (i + 1) / steps)
        a = int(strength * ((i / steps) ** 2.1))
        od.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            outline=(0, 0, 0, a),
            width=max_r // steps + 4,
        )
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    return img_rgba.convert("RGB")


def _base_gradient(width, height, accent, accent2, diagonal=True):
    img = Image.new("RGB", (width, height), (6, 7, 12))
    draw = ImageDraw.Draw(img)
    dark_a = tuple(max(0, int(c * 0.26)) for c in accent)
    dark_b = tuple(max(0, int(c * 0.22)) for c in accent2)
    for y in range(height):
        t = y / max(1, height - 1)
        curve = math.sin(t * math.pi * 0.85)
        r = int(7 + dark_a[0] * (1 - t) * 0.85 + dark_b[0] * curve * 0.55)
        g = int(8 + dark_a[1] * (1 - t) * 0.85 + dark_b[1] * curve * 0.55)
        b = int(14 + dark_a[2] * (1 - t) * 0.90 + dark_b[2] * curve * 0.65)
        draw.line([(0, y), (width, y)], fill=(min(255, r), min(255, g), min(255, b)))
    return img


def _draw_sunburst_rays(odraw, width, height, cx, cy, accent, accent2, n=28):
    max_len = int(math.hypot(width, height))
    for i in range(n):
        angle = (2 * math.pi / n) * i + random.uniform(-0.04, 0.04)
        spread = random.uniform(0.018, 0.042)
        x2 = cx + int(max_len * math.cos(angle - spread))
        y2 = cy + int(max_len * math.sin(angle - spread))
        x3 = cx + int(max_len * math.cos(angle + spread))
        y3 = cy + int(max_len * math.sin(angle + spread))
        col = accent if i % 2 == 0 else accent2
        alpha = random.randint(10, 26)
        odraw.polygon([(cx, cy), (x2, y2), (x3, y3)], fill=col + (alpha,))


def _draw_circuit_lines(odraw, width, height, accent, spacing=85):
    cols = width // spacing + 2
    rows = height // spacing + 2
    for _ in range(random.randint(16, 26)):
        cx = random.randint(0, cols - 1) * spacing
        cy = random.randint(0, rows - 1) * spacing
        pts = [(cx, cy)]
        for _ in range(random.randint(2, 5)):
            if random.random() < 0.5:
                cx += spacing * random.choice([-1, 1])
            else:
                cy += spacing * random.choice([-1, 1])
            pts.append((cx, cy))
        alpha = random.randint(20, 50)
        odraw.line(pts, fill=accent + (alpha,), width=2)
        node_r = random.choice([3, 4, 5])
        for px, py in pts:
            if random.random() < 0.65:
                odraw.ellipse(
                    [px - node_r, py - node_r, px + node_r, py + node_r],
                    fill=accent + (min(255, alpha + 55),),
                )


def _draw_wave_bands(odraw, width, height, accent, accent2, n=10):
    for i in range(n):
        base_y = height * (0.10 + 0.78 * i / n)
        amp = random.uniform(20, 62)
        freq = random.uniform(1.2, 2.5)
        phase = random.uniform(0, 6.28)
        col = accent if i % 2 == 0 else accent2
        alpha = random.randint(16, 38)
        pts = []
        step = 12
        for x in range(0, width + step, step):
            y = base_y + amp * math.sin((x / width) * freq * 2 * math.pi + phase)
            pts.append((x, y))
        odraw.line(pts, fill=col + (alpha,), width=random.randint(2, 4))


def _draw_particles_field(odraw, width, height, accent, accent2, n=95):
    for _ in range(n):
        sx = random.randint(0, width)
        sy = random.randint(0, height)
        col = accent if random.random() < 0.58 else accent2
        r = random.choice([1, 1, 2, 2, 3, 5, 9, 14])
        if r <= 3:
            alpha = random.randint(45, 175)
            odraw.ellipse([sx - r, sy - r, sx + r, py := sy + r], fill=col + (alpha,))
        else:
            alpha = random.randint(10, 32)
            odraw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=col + (alpha,))
            odraw.ellipse([sx - 2, sy - 2, sx + 2, sy + 2], fill=WHITE + (alpha * 2,))


def _draw_geo_shapes(odraw, width, height, accent, accent2, n=7):
    for _ in range(n):
        sx = random.randint(40, width - 40)
        sy = random.randint(40, height - 40)
        size = random.randint(22, 75)
        alpha_s = random.randint(14, 42)
        col_s = (accent if random.random() < 0.5 else accent2) + (alpha_s,)
        shape_type = random.choice(["diamond", "triangle", "cross", "ring"])
        if shape_type == "diamond":
            odraw.polygon([(sx, sy - size), (sx + size, sy), (sx, sy + size), (sx - size, sy)], outline=col_s, width=2)
        elif shape_type == "triangle":
            odraw.polygon([(sx, sy - size), (sx + size, sy + size), (sx - size, sy + size)], outline=col_s, width=2)
        elif shape_type == "ring":
            odraw.ellipse([sx - size, sy - size, sx + size, sy + size], outline=col_s, width=2)
        else:
            odraw.line([(sx - size, sy), (sx + size, sy)], fill=col_s, width=2)
            odraw.line([(sx, sy - size), (sx, sy + size)], fill=col_s, width=2)


BG_STYLES = ["nebula", "circuit", "sunburst", "wave", "particles", "aurora"]


def generate_background(theme_key, width, height, bg_style=None):
    """Генерирует детализированный арт-фон с неоновым свечением, сетками и HUD-геометрией."""
    theme = THEMES.get(theme_key, THEMES["matrix"])
    accent = theme["accent"]
    accent2 = get_accent2(accent)
    style = bg_style if bg_style in BG_STYLES else random.choice(BG_STYLES)

    cx, cy = width // 2, int(height * 0.34)
    img = _base_gradient(width, height, accent, accent2)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    # Фоновые мягкие пятна света
    _draw_glow_circle(odraw, cx, cy, int(min(width, height) * 0.62), accent, max_alpha=68)
    _draw_glow_circle(
        odraw,
        cx + random.randint(-width // 4, width // 4),
        cy + random.randint(-80, 140),
        int(min(width, height) * 0.42),
        accent2,
        max_alpha=60,
    )

    if style == "nebula":
        _draw_plexus(odraw, width, height, accent, accent2, n=34, max_dist=190)
        _draw_hud_reticle(odraw, cx, cy, int(min(width, height) * 0.34), accent, accent2)
        _draw_particles_field(odraw, width, height, accent, accent2, n=85)
        _draw_geo_shapes(odraw, width, height, accent, accent2, n=5)

    elif style == "circuit":
        _draw_circuit_lines(odraw, width, height, accent2, spacing=random.randint(75, 105))
        _draw_hud_reticle(odraw, cx, cy, int(min(width, height) * 0.30), accent, accent2)
        _draw_cyber_perspective_grid(odraw, width, height, int(height * 0.58), accent, alpha=22)
        _draw_particles_field(odraw, width, height, accent, accent2, n=55)

    elif style == "sunburst":
        origin_x = width // 2
        origin_y = int(height * 0.28)
        _draw_sunburst_rays(odraw, width, height, origin_x, origin_y, accent, accent2, n=28)
        _draw_glow_circle(odraw, origin_x, origin_y, int(min(width, height) * 0.38), accent2, max_alpha=88)
        _draw_hud_reticle(odraw, origin_x, origin_y, int(min(width, height) * 0.32), accent, accent2)
        _draw_particles_field(odraw, width, height, accent, accent2, n=70)

    elif style == "wave":
        _draw_wave_bands(odraw, width, height, accent, accent2, n=10)
        _draw_plexus(odraw, width, height, accent, accent2, n=26, max_dist=170)
        _draw_particles_field(odraw, width, height, accent, accent2, n=60)

    elif style == "particles":
        _draw_plexus(odraw, width, height, accent, accent2, n=42, max_dist=175)
        _draw_particles_field(odraw, width, height, accent, accent2, n=135)
        _draw_geo_shapes(odraw, width, height, accent, accent2, n=6)

    elif style == "aurora":
        for i in range(4):
            bx = random.randint(int(width * 0.15), int(width * 0.85))
            by = int(height * random.uniform(0.12, 0.52))
            col = accent if i % 2 == 0 else accent2
            _draw_glow_circle(
                odraw, bx, by,
                int(min(width, height) * random.uniform(0.32, 0.52)),
                col, max_alpha=55,
            )
        _draw_wave_bands(odraw, width, height, accent, accent2, n=7)
        _draw_hud_reticle(odraw, cx, cy, int(min(width, height) * 0.31), accent, accent2)
        _draw_particles_field(odraw, width, height, accent, accent2, n=65)

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img = img_rgba.convert("RGB")

    # Тонкая гексагональная сетка поверх
    hex_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _draw_hex_grid(
        ImageDraw.Draw(hex_overlay), width, height, accent,
        spacing=random.randint(95, 135), alpha=14,
    )
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(hex_overlay)
    img = img_rgba.convert("RGB")

    img = _draw_vignette(img, strength=85)
    img = _draw_scan_lines(img, height, alpha=9)
    return img


# ══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ РИСОВАНИЯ И ЭФФЕКТОВ
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
            if dx * dx + dy * dy > (outline_w + 0.5) ** 2:
                continue
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_glow_text(
    img, xy, text, font, fill, glow_color=None, blur_radius=12,
    glow_passes=2, outline_color=(0, 0, 0), outline_w=4, top_color=None
):
    """
    Рисует текст с мягким неоновым свечением (GaussianBlur) и вертикальным
    градиентом внутри букв (от top_color к fill).
    """
    if glow_color is None:
        glow_color = fill
    if top_color is None:
        top_color = _lerp_color(fill, WHITE, 0.72)

    x, y = int(xy[0]), int(xy[1])
    dummy = ImageDraw.Draw(img)
    bb = dummy.textbbox((0, 0), text, font=font)
    tw = max(1, bb[2] - bb[0])
    th = max(1, bb[3] - bb[1])
    pad = blur_radius * 3 + outline_w * 2 + 24

    patch_w = tw + pad * 2
    patch_h = th + pad * 2
    gx = -bb[0] + pad
    gy = -bb[1] + pad

    # 1. Слой свечения
    glow_layer = Image.new("RGBA", (patch_w, patch_h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow_layer)
    gdraw.text((gx, gy), text, font=font, fill=glow_color + (255,))
    for _ in range(glow_passes):
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(blur_radius))
    ga = glow_layer.split()[3].point(lambda a: min(255, int(a * 1.75)))
    glow_layer.putalpha(ga)

    # 2. Тёмная обводка для идеальной читаемости
    if outline_w > 0:
        out_draw = ImageDraw.Draw(glow_layer)
        for dx in range(-outline_w, outline_w + 1):
            for dy in range(-outline_w, outline_w + 1):
                if dx * dx + dy * dy > (outline_w + 0.4) ** 2:
                    continue
                out_draw.text((gx + dx, gy + dy), text, font=font, fill=outline_color + (235,))

    # 3. Градиентная заливка самих букв
    mask = Image.new("L", (patch_w, patch_h), 0)
    ImageDraw.Draw(mask).text((gx, gy), text, font=font, fill=255)

    grad = Image.new("RGBA", (patch_w, patch_h), (0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(grad)
    y_start = pad
    y_end = pad + th
    for py in range(y_start, y_end + 1):
        t = (py - y_start) / max(1, th)
        col = _lerp_color(top_color, fill, t ** 0.9)
        grad_draw.line([(0, py), (patch_w, py)], fill=col + (255,))

    glow_layer.paste(grad, (0, 0), mask)

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(glow_layer, (x + bb[0] - pad, y + bb[1] - pad))
    img.paste(img_rgba.convert("RGB"), (0, 0))


def _draw_glass_panel(
    img, box, accent, accent2=None, fill_alpha=185, radius=28,
    cut=0, border_w=2
):
    """
    Рисует полупрозрачную матовую панель (Glassmorphism) с верхним бликом
    и аккуратной неоновой рамкой.
    """
    if accent2 is None:
        accent2 = get_accent2(accent)
    x0, y0, x1, y1 = [int(v) for v in box]
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    base_fill = (14, 16, 24, fill_alpha)
    border_col = accent + (145,)
    highlight_col = (255, 255, 255, 42)

    if cut > 0:
        pts = [
            (x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut),
            (x1, y1 - cut), (x1 - cut, y1), (x0 + cut, y1),
            (x0, y1 - cut), (x0, y0 + cut),
        ]
        od.polygon(pts, fill=base_fill, outline=border_col)
        od.line([(x0 + cut + 4, y0 + 2), (x1 - cut - 4, y0 + 2)], fill=highlight_col, width=2)
    else:
        od.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=base_fill, outline=border_col, width=border_w)
        od.line([(x0 + radius, y0 + 2), (x1 - radius, y0 + 2)], fill=highlight_col, width=2)

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img.paste(img_rgba.convert("RGB"), (0, 0))


def _draw_gradient_bar(img, box, progress, accent, accent2, radius=None, stripes=True):
    """Рисует технологичный прогресс-бар с градиентом, полосками и светящимся курсором."""
    x0, y0, x1, y1 = [int(v) for v in box]
    bw = x1 - x0
    bh = y1 - y0
    if radius is None:
        radius = bh // 2
    progress = max(0.0, min(1.0, float(progress)))

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    # Подложка
    od.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=radius,
        fill=(18, 20, 30, 230),
        outline=(70, 75, 95, 180),
        width=2,
    )

    filled_w = int(bw * progress)
    if filled_w > 6:
        fill_w = max(bh, filled_w)
        bar_mask = Image.new("L", (fill_w, bh), 0)
        ImageDraw.Draw(bar_mask).rounded_rectangle([0, 0, fill_w - 1, bh - 1], radius=radius, fill=255)

        bar_grad = Image.new("RGBA", (fill_w, bh), (0, 0, 0, 0))
        bg_draw = ImageDraw.Draw(bar_grad)
        for px in range(fill_w):
            t = px / max(1, fill_w - 1)
            col = _lerp_color(accent, accent2, t)
            bg_draw.line([(px, 0), (px, bh)], fill=col + (245,))

        # Диагональные тактические насечки
        if stripes:
            step = 22
            for sx in range(-bh, fill_w + bh, step):
                bg_draw.line(
                    [(sx, bh), (sx + bh, 0)],
                    fill=(255, 255, 255, 38),
                    width=4,
                )

        # Верхний глянцевый блик
        bg_draw.rectangle([0, 2, fill_w, max(4, bh // 3)], fill=(255, 255, 255, 48))

        overlay.paste(bar_grad, (x0, y0), bar_mask)

        # Неоновый маркер на конце заполненной части
        end_x = x0 + fill_w - 4
        _draw_glow_circle(od, end_x, y0 + bh // 2, int(bh * 0.9), accent2, max_alpha=95)
        od.ellipse(
            [end_x - 5, y0 + bh // 2 - 5, end_x + 5, y0 + bh // 2 + 5],
            fill=WHITE + (240,),
        )

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img.paste(img_rgba.convert("RGB"), (0, 0))


def _draw_icon(draw, cx, cy, r, kind, color):
    """Абстрактные векторные значки для бейджей: diamond / shield / star / chevron / hex."""
    if kind == "diamond":
        draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], outline=color, width=2)
        draw.polygon(
            [(cx, cy - r * 0.45), (cx + r * 0.45, cy), (cx, cy + r * 0.45), (cx - r * 0.45, cy)],
            fill=color,
        )
    elif kind == "shield":
        pts = [
            (cx, cy - r),
            (cx + r * 0.85, cy - r * 0.5),
            (cx + r * 0.85, cy + r * 0.2),
            (cx, cy + r),
            (cx - r * 0.85, cy + r * 0.2),
            (cx - r * 0.85, cy - r * 0.5),
        ]
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
        draw.line(
            [(cx - r * 0.55, cy - r * 0.5), (cx, cy), (cx - r * 0.55, cy + r * 0.5)],
            fill=color, width=3, joint="curve",
        )
        draw.line(
            [(cx + r * 0.05, cy - r * 0.5), (cx + r * 0.6, cy), (cx + r * 0.05, cy + r * 0.5)],
            fill=color, width=3, joint="curve",
        )
    else:  # hex
        pts = [
            (cx + r * math.cos(math.radians(60 * i - 30)), cy + r * math.sin(math.radians(60 * i - 30)))
            for i in range(6)
        ]
        draw.polygon(pts, outline=color, width=2)
        draw.ellipse([cx - 3, cy - 3, cx + 3, py := cy + 3], fill=color)


def _draw_panel_frame(draw, margin, accent, cut=26, width=2):
    """Полноконтурная скошенная рамка-панель вокруг всей карточки."""
    x0, y0, x1, y1 = margin, margin, W - margin, H - margin
    pts = [
        (x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut),
        (x1, y1 - cut), (x1 - cut, y1), (x0 + cut, y1),
        (x0, y1 - cut), (x0, y0 + cut),
    ]
    draw.line(pts + [pts[0]], fill=accent, width=width)
    for px, py in pts:
        draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=accent)


def _draw_noise(img, amount=6):
    """Быстрое и равномерное плёночное зерно через встроенный генератор Pillow."""
    try:
        noise = Image.effect_noise(img.size, float(amount * 2.2)).convert("RGB")
        blended = Image.blend(img, noise, 0.035)
        img.paste(blended, (0, 0))
    except Exception:
        pass


# ══════════════════════════════════════════════
#  ПРОСТАЯ ЗАГЛУШКА НА СЛУЧАЙ ЛЮБОЙ ОШИБКИ
# ══════════════════════════════════════════════
def _save_fallback_card(output_path, person, header_text):
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
    style = style if style in CORNER_STYLES else random.choice(CORNER_STYLES)
    arm = 48
    thick = 3
    col = accent
    corners = [
        (margin, margin, +1, 0, 0, +1),
        (W - margin, margin, -1, 0, 0, +1),
        (margin, H - margin, +1, 0, 0, -1),
        (W - margin, H - margin, -1, 0, 0, -1),
    ]

    if style == "bracket":
        for x, y, dhx, dhy, dvx, dvy in corners:
            draw.line([(x, y), (x + dhx * arm, y + dhy * arm)], fill=col, width=thick)
            draw.line([(x, y), (x + dvx * arm, y + dvy * arm)], fill=col, width=thick)
            draw.rectangle([x - 4, y - 4, x + 4, y + 4], fill=WHITE)

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
                (x + dhx * s, y),
                (x + dhx * s * 1.6, y + dvy * s * 0.9),
                (x + dhx * s * 1.6, y + dvy * s * 1.8),
                (x + dhx * s, y + dvy * s * 2.4),
                (x, y + dvy * s * 1.8),
                (x, y + dvy * s * 0.9),
            ]
            draw.polygon(pts, outline=col, width=2)
            draw.line([(x + dhx * s * 1.6, y), (x + dhx * arm, y)], fill=col, width=2)
            draw.line([(x, y + dvy * s * 2.4), (x, y + dvy * arm)], fill=col, width=2)

    elif style == "frame":
        inset = 12
        draw.rounded_rectangle(
            [margin - inset, margin - inset, W - margin + inset, H - margin + inset],
            radius=24, outline=col, width=1,
        )
        for x, y, dhx, dhy, dvx, dvy in corners:
            draw.line([(x, y), (x + dhx * (arm - 8), y + dhy * (arm - 8))], fill=col, width=thick)
            draw.line([(x, y), (x + dvx * (arm - 8), y + dvy * (arm - 8))], fill=col, width=thick)


LINE_STYLES = ["solid", "dashed", "diamond"]


def _draw_neon_line(draw, x1, y1, x2, y2, accent, width=3, style=None):
    style = style if style in LINE_STYLES else random.choice(LINE_STYLES)
    glow = _brighten(accent, 1.25)

    if style == "solid":
        draw.line([(x1, y1), (x2, y2)], fill=accent, width=width + 4)
        draw.line([(x1, y1), (x2, y2)], fill=glow, width=max(1, width))
        draw.line([(x1 + 30, y1), (x2 - 30, y2)], fill=WHITE, width=1)
        # Боковые ромбики-ограничители
        for px in (x1, x2):
            draw.polygon([(px, y1 - 5), (px + 5, y1), (px, y1 + 5), (px - 5, y1)], fill=WHITE)

    elif style == "dashed":
        total = math.hypot(x2 - x1, y2 - y1)
        if total <= 0:
            return
        ux, uy = (x2 - x1) / total, (y2 - y1) / total
        dash, gap = 18, 10
        pos = 0.0
        while pos < total:
            seg_end = min(pos + dash, total)
            sx1, sy1 = x1 + ux * pos, y1 + uy * pos
            sx2, sy2 = x1 + ux * seg_end, y1 + uy * seg_end
            draw.line([(sx1, sy1), (sx2, sy2)], fill=glow, width=width + 2)
            draw.line([(sx1, sy1), (sx2, sy2)], fill=WHITE, width=1)
            pos += dash + gap

    elif style == "diamond":
        dim = tuple(c // 2 for c in accent)
        draw.line([(x1, y1), (x2, y2)], fill=dim, width=1)
        total = math.hypot(x2 - x1, y2 - y1)
        if total <= 0:
            return
        ux, uy = (x2 - x1) / total, (y2 - y1) / total
        step = 28
        pos = step / 2
        s = 4
        while pos < total:
            px, py = x1 + ux * pos, y1 + uy * pos
            draw.polygon([(px, py - s), (px + s, py), (px, py + s), (px - s, py)], fill=glow)
            pos += step


BADGE_SHAPES = ["rect", "pill", "cut"]


def _badge_rect(draw, box, accent, shape=None, fill=(16, 18, 26), outline_w=2):
    shape = shape if shape in BADGE_SHAPES else random.choice(BADGE_SHAPES)
    x0, y0, x1, y1 = [int(v) for v in box]
    if shape == "rect":
        draw.rounded_rectangle([x0, y0, x1, y1], radius=8, fill=fill, outline=accent, width=outline_w)
    elif shape == "pill":
        radius = min(22, max(6, (y1 - y0) // 2))
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill, outline=accent, width=outline_w)
    elif shape == "cut":
        c = min(14, max(4, (y1 - y0) // 3))
        pts = [
            (x0 + c, y0), (x1 - c, y0), (x1, y0 + c),
            (x1, y1 - c), (x1 - c, y1), (x0 + c, y1),
            (x0, y1 - c), (x0, y0 + c),
        ]
        draw.polygon(pts, fill=fill, outline=accent)
        draw.line(pts + [pts[0]], fill=accent, width=outline_w)
    return shape


def _draw_stat_badge(
    draw, label, value, x, y, accent, font_label, font_value,
    shape=None, icon=None, min_w=0
):
    """
    Рисует информационный бейдж со статистикой, левым акцентным кантом и векторной иконкой.
    """
    pad_x, pad_y = 20, 14
    lb = draw.textbbox((0, 0), label, font=font_label)
    vb = draw.textbbox((0, 0), value, font=font_value)
    text_w = max(lb[2] - lb[0], vb[2] - vb[0])
    lh = lb[3] - lb[1]
    vh = vb[3] - vb[1]
    box_h = lh + vh + pad_y * 2 + 10
    icon_slot = (box_h - pad_y * 2 + 16) if icon else 0
    box_w = max(min_w, text_w + pad_x * 2 + icon_slot + 8)

    _badge_rect(draw, (x, y, x + box_w, y + box_h), accent, shape=shape, fill=(18, 20, 30), outline_w=2)

    # Яркая вертикальная полоска-индикатор слева внутри бейджа
    stripe_x = x + 6
    draw.line([(stripe_x, y + 12), (stripe_x, y + box_h - 12)], fill=accent, width=4)

    text_x = x + pad_x + 6
    if icon:
        icon_r = max(12, (box_h - pad_y * 2) // 2 - 2)
        icon_cx = text_x + icon_r
        icon_cy = y + box_h // 2
        _draw_icon(draw, icon_cx, icon_cy, icon_r, icon, accent)
        text_x += icon_r * 2 + 16

    draw.text((text_x, y + pad_y), label, font=font_label, fill=(145, 150, 175))
    draw.text((text_x, y + pad_y + lh + 8), value, font=font_value, fill=WHITE)
    return box_w, box_h


# ══════════════════════════════════════════════
#  ГЛАВНАЯ ФУНКЦИЯ КАРТОЧКИ НАРЯДОВ
# ══════════════════════════════════════════════
def make_reminder_card(
    duty_type, person, position_label, time_label,
    header_text, ending_text, date_label, mood_label,
    proc_day=1, next_person="", next_days=0, total_duties=0,
    theme_key=None, output_path="/tmp/card.jpg"
):
    """
    Рисует карточку-напоминание и сохраняет её в output_path (JPEG).
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
        person_clean = _clean_for_image(person) or person
        pos_clean = _clean_for_image(position_label) or position_label

        # ── 1. Полноэкранный арт-фон + размытая нижняя зона ──────────────
        full_bg = generate_background(theme_key, W, H, bg_style=random.choice(style["bg"]))
        blurred_bottom = full_bg.crop((0, BG_H - 80, W, H)).filter(ImageFilter.GaussianBlur(18))
        img = full_bg.copy()
        img.paste(blurred_bottom, (0, BG_H - 80))

        # Плавное притемнение к низу, чтобы фон мягко просвечивал сквозь стекло
        img_rgba = img.convert("RGBA")
        fade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fade)
        fade_start = BG_H - 220
        for y in range(fade_start, H):
            t = min(1.0, (y - fade_start) / 260.0)
            a = int(215 * (t ** 1.2))
            fd.line([(0, y), (W, y)], fill=(DARK[0], DARK[1], DARK[2], a))
        img_rgba.alpha_composite(fade)
        img = img_rgba.convert("RGB")

        # ── 2. Стеклянная нижняя главная панель ──────────────────────────
        panel_top = BG_H - 10
        _draw_glass_panel(
            img,
            (MARGIN + 10, panel_top, W - MARGIN - 10, H - MARGIN - 42),
            accent=accent,
            accent2=accent2,
            fill_alpha=185,
            radius=30,
            cut=22 if style["badge"] == "cut" else 0,
            border_w=2,
        )

        draw = ImageDraw.Draw(img)
        _draw_corner_decorations(draw, accent, margin=26, style=style["corner"])
        if style["corner"] != "frame":
            _draw_panel_frame(draw, margin=16, accent=accent, cut=22, width=1)

        # ── 3. Верхняя шапка: бейдж темы + статус + время ────────────────
        badge_font = load_font(FONT_BOLD_PATH, 22)
        badge_text = f"◈ {theme['title']} ◈"
        bb = draw.textbbox((0, 0), badge_text, font=badge_font)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        pad_x, pad_y = 18, 10
        top_y = MARGIN + 8

        _badge_rect(
            draw,
            (MARGIN + 10, top_y, MARGIN + 10 + bw + pad_x * 2, top_y + bh + pad_y * 2),
            accent, shape=style["badge"], fill=(14, 16, 24), outline_w=2,
        )
        draw.text((MARGIN + 10 + pad_x, top_y + pad_y - 2), badge_text, font=badge_font, fill=accent)

        time_font = load_font(FONT_BOLD_PATH, 22)
        time_str = f"⏱ {time_label}" if not _EMOJI_PATTERN.search("⏱") else f"ДО {time_label}"
        tb = draw.textbbox((0, 0), time_str, font=time_font)
        tw = tb[2] - tb[0]
        _badge_rect(
            draw,
            (W - MARGIN - 10 - tw - pad_x * 2, top_y, W - MARGIN - 10, top_y + bh + pad_y * 2),
            accent2, shape=style["badge"], fill=(14, 16, 24), outline_w=2,
        )
        draw.text((W - MARGIN - 10 - tw - pad_x, top_y + pad_y - 2), time_str, font=time_font, fill=WHITE)

        line_y = top_y + bh + pad_y * 2 + 16
        _draw_neon_line(draw, MARGIN + 12, line_y, W - MARGIN - 12, line_y, accent, width=2, style=style["line"])

        # ── 4. Тактический шеврон/медальон роли над именем ───────────────
        emblem_cy = line_y + 68
        emblem_r = 36
        emblem_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        eo_draw = ImageDraw.Draw(emblem_overlay)
        _draw_glow_circle(eo_draw, W // 2, emblem_cy, emblem_r * 2, accent, max_alpha=90)
        img_rgba = img.convert("RGBA")
        img_rgba.alpha_composite(emblem_overlay)
        img = img_rgba.convert("RGB")
        draw = ImageDraw.Draw(img)

        draw.ellipse(
            [W // 2 - emblem_r, emblem_cy - emblem_r, W // 2 + emblem_r, emblem_cy + emblem_r],
            fill=(14, 16, 24), outline=accent, width=2,
        )
        _draw_icon(
            draw, W // 2, emblem_cy, emblem_r - 12,
            "shield" if duty_type == "svodki" else "star", accent2,
        )

        # ── 5. Имя дежурного (с градиентом и неоновым свечением) ─────────
        for name_size in (108, 94, 82, 70, 58):
            name_font = load_font(FONT_BOLD_PATH, name_size)
            nb = draw.textbbox((0, 0), person_clean, font=name_font)
            if nb[2] - nb[0] <= W - 150:
                break
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = (W - nw) // 2
        ny = emblem_cy + emblem_r + 18

        _draw_glow_text(
            img, (nx, ny), person_clean, name_font,
            fill=accent, glow_color=accent2,
            blur_radius=14, glow_passes=2, outline_w=5,
            top_color=WHITE,
        )
        draw = ImageDraw.Draw(img)

        # ── 6. Капсула позиции (должности) ───────────────────────────────
        pos_font = load_font(FONT_BOLD_PATH, 25)
        pos_text = f"◈  {pos_clean.upper()}  ◈"
        pb = draw.textbbox((0, 0), pos_text, font=pos_font)
        pw, ph = pb[2] - pb[0], pb[3] - pb[1]
        py = ny + nh + 28
        pos_box = ((W - pw) // 2 - 28, py - 10, (W + pw) // 2 + 28, py + ph + 14)
        _badge_rect(draw, pos_box, accent2, shape="pill", fill=(14, 16, 26), outline_w=2)
        draw.text(((W - pw) // 2, py), pos_text, font=pos_font, fill=WHITE)

        # ── 7. Содержимое стеклянной панели ──────────────────────────────
        text_y = panel_top + 28

        meta_font = load_font(FONT_BOLD_PATH, 21)
        meta_parts = [p for p in (date_label.upper(), mood_label.upper()) if p]
        meta_text = "   •   ".join(meta_parts)
        h = _draw_centered(draw, meta_text, meta_font, text_y, color=(155, 165, 190), shadow=False)
        text_y += h + 18

        _draw_neon_line(draw, MARGIN + 45, text_y, W - MARGIN - 45, text_y, accent, width=2, style=style["line"])
        text_y += 24

        # Заголовок наряда
        max_w = W - 160
        for fs in (44, 38, 34, 30, 26):
            title_font = load_font(FONT_BOLD_PATH, fs)
            title_lines = _wrap_text(draw, header_text.upper(), title_font, max_w)
            if len(title_lines) <= 3:
                break
        for line in title_lines[:3]:
            h = _draw_centered(draw, line, title_font, text_y, color=WHITE, shadow=True, so=3)
            text_y += h + 8
        text_y += 16

        # ── 8. Блок типа наряда (СВОДКИ / ПРОЦЕДУРКА) ────────────────────
        duty_font = load_font(FONT_BOLD_PATH, 26)
        duty_title = "СВОДКИ" if duty_type == "svodki" else "ПРОЦЕДУРКА"

        if duty_type == "proc":
            label_text = f"{duty_title}   ·   ЭТАП {proc_day} ИЗ 2"
            lb = draw.textbbox((0, 0), label_text, font=duty_font)
            lw, lh = lb[2] - lb[0], lb[3] - lb[1]
            box_w = W - 180
            box_h = lh + 76
            box_x0 = (W - box_w) // 2

            _badge_rect(
                draw, (box_x0, text_y, box_x0 + box_w, text_y + box_h),
                accent, shape=style["badge"], fill=(20, 22, 34), outline_w=2,
            )
            draw.text(((W - lw) // 2, text_y + 14), label_text, font=duty_font, fill=accent)

            bar_y = text_y + lh + 30
            bar_x0 = box_x0 + 40
            bar_x1 = box_x0 + box_w - 40
            _draw_gradient_bar(
                img, (bar_x0, bar_y, bar_x1, bar_y + 26),
                progress=min(1.0, proc_day / 2.0),
                accent=accent, accent2=accent2,
            )
            draw = ImageDraw.Draw(img)
            text_y += box_h + 24
        else:
            duty_line = f"⚡  МИССИЯ: {duty_title}   //   СДАТЬ ДО {time_label}"
            duty_line = _clean_for_image(duty_line)
            db = draw.textbbox((0, 0), duty_line, font=duty_font)
            dw, dh = db[2] - db[0], db[3] - db[1]
            box_w = max(dw + 70, W - 220)
            box_h = dh + 36
            bx0 = (W - box_w) // 2
            _badge_rect(
                draw, (bx0, text_y, bx0 + box_w, text_y + box_h),
                accent, shape=style["badge"], fill=(20, 22, 34), outline_w=2,
            )
            draw.text(((W - dw) // 2, text_y + 16), duty_line, font=duty_font, fill=accent)
            text_y += box_h + 24

        # ── 9. Две симметричные карточки статистики по центру ────────────
        stat_label_font = load_font(FONT_BOLD_PATH, 17)
        stat_val_font   = load_font(FONT_BOLD_PATH, 24)
        grid_w = W - 180
        gap = 24
        col_w = (grid_w - gap) // 2
        start_x = (W - grid_w) // 2

        _, badge_h1 = _draw_stat_badge(
            draw,
            label="ВЫПОЛНЕНО НАРЯДОВ",
            value=str(total_duties),
            x=start_x, y=text_y, accent=accent,
            font_label=stat_label_font, font_value=stat_val_font,
            shape=style["badge"], icon="star", min_w=col_w,
        )
        next_val = f"{_clean_for_image(next_person) or '—'} ({next_days}д)" if next_person else "—"
        _, badge_h2 = _draw_stat_badge(
            draw,
            label="СЛЕДУЮЩИЙ В ОЧЕРЕДИ",
            value=next_val,
            x=start_x + col_w + gap, y=text_y, accent=accent2,
            font_label=stat_label_font, font_value=stat_val_font,
            shape=style["badge"], icon="chevron", min_w=col_w,
        )
        text_y += max(badge_h1, badge_h2) + 24

        # ── 10. Концовка / цитата ────────────────────────────────────────
        if ending_text:
            draw.line([(start_x + 30, text_y), (W - start_x - 30, text_y)], fill=(50, 55, 75), width=1)
            text_y += 18
            end_font = load_font(FONT_REGULAR_PATH, 25)
            end_lines = _wrap_text(draw, f"«{ending_text}»", end_font, max_w - 20)
            for line in end_lines[:2]:
                h = _draw_centered(draw, line, end_font, text_y, color=(200, 205, 222), shadow=True, so=2)
                text_y += h + 6

        # ── 11. Футер ────────────────────────────────────────────────────
        foot_font = load_font(FONT_BOLD_PATH, 16)
        foot_text = f"◈ НАРЯД-БОТ  ×  {theme['title']}  ·  HUD v2.0 ◈"
        fx = _centered_x(draw, foot_text, foot_font)
        draw.text((fx, H - MARGIN - 24), foot_text, font=foot_font, fill=(95, 100, 125))

        _draw_noise(img, amount=6)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "JPEG", quality=94)
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
    out = make_reminder_card(
        duty_type="svodki",
        person="Тест",
        position_label="Дежурный №1 по сводкам",
        time_label="18:00",
        header_text="тестовая карточка нового поколения",
        ending_text="если ты это видишь — обновлённый генератор работает идеально",
        date_label="19.06.2026 • пятница",
        mood_label="геймерский",
        next_person="Игорь",
        next_days=1,
        total_duties=42,
        theme_key=random.choice(THEME_KEYS),
        output_path=os.path.join(BASE_DIR, "temp", "selftest.jpg"),
    )
    print("Тема:", out)
