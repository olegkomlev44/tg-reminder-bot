"""
card_generator.py — Генератор красивых карточек-напоминаний через Pillow (HUD / Glassmorphism v3.0).

Обновления HUD v3.0 (пункты 1, 2, 3, 4):
  1. Отрисовка блока дежурных врачей (Отв. врач, I хирург, II хирург) прямо на карточке сводок (18:00).
  2. Разделение визуальных архетипов:
     - СВОДКИ (18:00): тактический радарный/маршрутный HUD с векторным планшетом/радаром, точками маршрута и сводкой врачей/погоды.
     - ПРОЦЕДУРКА (22:00): неоновый биохазард/санитарный HUD с двухсекционным индикатором смены («ДЕНЬ 1/2 — ЭКВАТОР СМЕНЫ» / «ДЕНЬ 2/2 — СДАЧА ПОСТА · ФИНАЛ»).
  3. Мини-инфографика очереди («КТО ЗА ТОБОЙ»): цепочка из 3 следующих дежурных со стрелками (Глеб ➔ Дима ➔ Денис) и днями до их заступления.
  4. Интеграция мини-прогресса ДМБ дежурного прямо на карточку наряда (процент, дни до дома, звание и остаток нарядов до ДМБ).
"""

import os
import re
import math
import random
import sys
import colorsys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MSK_TZ = ZoneInfo("Europe/Moscow")

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
    for start_ang in (15, 115, 205, 295):
        odraw.arc(
            [cx - base_r - 12, cy - base_r - 12, cx + base_r + 12, cy + base_r + 12],
            start=start_ang, end=start_ang + 48, fill=accent + (70,), width=3
        )
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
            odraw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=col + (alpha,))
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

    glow_layer = Image.new("RGBA", (patch_w, patch_h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow_layer)
    gdraw.text((gx, gy), text, font=font, fill=glow_color + (255,))
    for _ in range(glow_passes):
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(blur_radius))
    ga = glow_layer.split()[3].point(lambda a: min(255, int(a * 1.75)))
    glow_layer.putalpha(ga)

    if outline_w > 0:
        out_draw = ImageDraw.Draw(glow_layer)
        for dx in range(-outline_w, outline_w + 1):
            for dy in range(-outline_w, outline_w + 1):
                if dx * dx + dy * dy > (outline_w + 0.4) ** 2:
                    continue
                out_draw.text((gx + dx, gy + dy), text, font=font, fill=outline_color + (235,))

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

        if stripes:
            step = 22
            for sx in range(-bh, fill_w + bh, step):
                bg_draw.line(
                    [(sx, bh), (sx + bh, 0)],
                    fill=(255, 255, 255, 38),
                    width=4,
                )

        bg_draw.rectangle([0, 2, fill_w, max(4, bh // 3)], fill=(255, 255, 255, 48))

        overlay.paste(bar_grad, (x0, y0), bar_mask)

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
    """
    Векторные значки для бейджей и эмблем:
    diamond / shield / star / chevron / hex / radar / clipboard / biohazard / medcross.
    """
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
    elif kind == "radar":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=2)
        r_mid = int(r * 0.56)
        draw.ellipse([cx - r_mid, cy - r_mid, cx + r_mid, cy + r_mid], outline=color, width=1)
        draw.line([(cx - r, cy), (cx + r, cy)], fill=color, width=1)
        draw.line([(cx, cy - r), (cx, cy + r)], fill=color, width=1)
        bx = cx + int(r * 0.78 * math.cos(math.radians(-38)))
        by = cy + int(r * 0.78 * math.sin(math.radians(-38)))
        draw.line([(cx, cy), (bx, by)], fill=WHITE, width=3)
        draw.ellipse([bx - 3, by - 3, bx + 3, by + 3], fill=color)
    elif kind == "biohazard":
        for deg in (-90, 30, 150):
            rad = math.radians(deg)
            ox = cx + int(r * 0.46 * math.cos(rad))
            oy = cy + int(r * 0.46 * math.sin(rad))
            rr = int(r * 0.52)
            draw.ellipse([ox - rr, oy - rr, ox + rr, oy + rr], outline=color, width=2)
        in_r = int(r * 0.28)
        draw.ellipse([cx - in_r, cy - in_r, cx + in_r, cy + in_r], fill=(14, 16, 24), outline=WHITE, width=2)
    elif kind == "medcross":
        arm = int(r * 0.34)
        ext = int(r * 0.88)
        pts = [
            (cx - arm, cy - ext), (cx + arm, cy - ext),
            (cx + arm, cy - arm), (cx + ext, cy - arm),
            (cx + ext, cy + arm), (cx + arm, cy + arm),
            (cx + arm, cy + ext), (cx - arm, cy + ext),
            (cx - arm, cy + arm), (cx - ext, cy + arm),
            (cx - ext, cy - arm), (cx - arm, cy - arm),
        ]
        draw.polygon(pts, fill=color, outline=WHITE)
    else:  # hex
        pts = [
            (cx + r * math.cos(math.radians(60 * i - 30)), cy + r * math.sin(math.radians(60 * i - 30)))
            for i in range(6)
        ]
        draw.polygon(pts, outline=color, width=2)
        draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)


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
    pad_x, pad_y = 20, 12
    lb = draw.textbbox((0, 0), label, font=font_label)
    vb = draw.textbbox((0, 0), value, font=font_value)
    text_w = max(lb[2] - lb[0], vb[2] - vb[0])
    lh = lb[3] - lb[1]
    vh = vb[3] - vb[1]
    box_h = lh + vh + pad_y * 2 + 8
    icon_slot = (box_h - pad_y * 2 + 16) if icon else 0
    box_w = max(min_w, text_w + pad_x * 2 + icon_slot + 8)

    _badge_rect(draw, (x, y, x + box_w, y + box_h), accent, shape=shape, fill=(18, 20, 30), outline_w=2)

    stripe_x = x + 6
    draw.line([(stripe_x, y + 10), (stripe_x, y + box_h - 10)], fill=accent, width=4)

    text_x = x + pad_x + 6
    if icon:
        icon_r = max(11, (box_h - pad_y * 2) // 2 - 2)
        icon_cx = text_x + icon_r
        icon_cy = y + box_h // 2
        _draw_icon(draw, icon_cx, icon_cy, icon_r, icon, accent)
        text_x += icon_r * 2 + 14

    draw.text((text_x, y + pad_y), label, font=font_label, fill=(145, 150, 175))
    draw.text((text_x, y + pad_y + lh + 6), value, font=font_value, fill=WHITE)
    return box_w, box_h


# ══════════════════════════════════════════════
#  СПЕЦИАЛЬНЫЕ HUD-АРХЕТИПЫ И БЛОКИ (Пункты 1, 2, 3, 4)
# ══════════════════════════════════════════════
_FALLBACK_DEMBEL_DATES = {
    "Игорь":   date(2026, 10, 30),
    "Руслан":  date(2026, 12, 15),
    "Дима":    date(2026, 10, 20),
    "Дима 2":  date(2026, 11, 12),
    "Арсений": date(2027, 4, 18),
    "Денис":   date(2027, 4, 18),
    "Олег":    date(2026, 11, 7),
    "Ильнар":  date(2026, 12, 15),
    "Максим":  date(2026, 12, 17),
}


def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Склонение существительных после числа: (1, 2, 5) -> ('сводка', 'сводки', 'сводок')."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def _resolve_dembel_info(person: str, dembel_info: dict | None = None) -> dict | None:
    """
    Возвращает данные ДМБ для бойца: либо переданный из duty_handlers словарь,
    либо автоматически рассчитывает по базе дат ДМБ (1 год службы, МСК).
    """
    if isinstance(dembel_info, dict) and dembel_info.get("has_dembel"):
        return dembel_info

    target = _FALLBACK_DEMBEL_DATES.get(person)
    if not target:
        return None

    now_dt = datetime.now(MSK_TZ).replace(tzinfo=None)
    try:
        start_date = target.replace(year=target.year - 1)
    except ValueError:
        start_date = target - timedelta(days=365)

    start_dt = datetime.combine(start_date, datetime.min.time())
    target_dt = datetime.combine(target, datetime.min.time())
    total_sec = max(1.0, (target_dt - start_dt).total_seconds())
    passed_sec = max(0.0, min(total_sec, (now_dt - start_dt).total_seconds()))
    left_sec = (target_dt - now_dt).total_seconds()
    progress = max(0.0, min(1.0, passed_sec / total_sec))
    done = left_sec <= 0
    days_left = 0 if done else int(left_sec // 86400)
    hours_left = 0 if done else int((left_sec % 86400) // 3600)

    return {
        "has_dembel": True,
        "done": done,
        "days": days_left,
        "hours": hours_left,
        "progress": progress,
        "target_str": target.strftime("%d.%m.%Y"),
        "duties_left": None,
    }


def _draw_archetype_hero_hud(img, duty_type: str, cx: int, cy: int, accent, accent2):
    """
    Пункт 2: Разделяет визуальный архетип верхней части карточки:
      - svodki: тактический радар с пеленгом, азимутальными кольцами и боковыми координатами маршрута
      - proc: неоновый био-санитарный гексагон со стерильным контуром и боковыми индикаторами чистоты
    """
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    hud_font = load_font(FONT_BOLD_PATH, 13)

    if duty_type == "svodki":
        for r, alpha in ((62, 110), (96, 65), (132, 38)):
            od.ellipse([cx - r, cy - r, cx + r, cy + r], outline=accent + (alpha,), width=2 if r == 62 else 1)
        od.pieslice([cx - 96, cy - 96, cx + 96, cy + 96], start=-55, end=-12, fill=accent + (42,))
        od.arc([cx - 112, cy - 112, cx + 112, cy + 112], start=155, end=215, fill=accent2 + (145,), width=3)
        od.arc([cx - 112, cy - 112, cx + 112, cy + 112], start=-35, end=25, fill=accent2 + (145,), width=3)

        for side, lbl in ((-1, "RADAR // ROUTE 18:00"), (1, "SECTOR // HOSPITAL")):
            sx = cx + side * 145
            ex = cx + side * 380
            od.line([(sx, cy), (ex, cy)], fill=accent + (115,), width=2)
            for step_i in range(1, 4):
                px = sx + side * (step_i * 68)
                od.ellipse([px - 4, cy - 4, px + 4, cy + 4], fill=accent2 + (210,))
            tb = od.textbbox((0, 0), lbl, font=hud_font)
            tw = tb[2] - tb[0]
            tx = (ex - tw) if side > 0 else ex
            od.text((tx, cy - 22), lbl, font=hud_font, fill=accent + (195,))
    else:
        for r, col, w in ((66, accent, 2), (102, accent2, 2), (134, accent, 1)):
            pts = [
                (cx + r * math.cos(math.radians(60 * i)), cy + r * math.sin(math.radians(60 * i)))
                for i in range(6)
            ]
            od.polygon(pts, outline=col + (105 if w == 2 else 45,), width=w)
            if r == 102:
                for px, py in pts:
                    od.ellipse([px - 4, py - 4, px + 4, py + 4], fill=WHITE + (200,))

        for side, lbl in ((-1, "BIO-CONTROL // 22:00"), (1, "STERILE // ZONE A")):
            sx = cx + side * 148
            ex = cx + side * 385
            p1 = (sx, cy)
            p2 = (sx + side * 55, cy)
            p3 = (sx + side * 72, cy - 18)
            p4 = (sx + side * 90, cy + 16)
            p5 = (sx + side * 108, cy)
            p6 = (ex, cy)
            od.line([p1, p2, p3, p4, p5, p6], fill=accent2 + (155,), width=2)
            tb = od.textbbox((0, 0), lbl, font=hud_font)
            tw = tb[2] - tb[0]
            tx = (ex - tw) if side > 0 else ex
            od.text((tx, cy - 24), lbl, font=hud_font, fill=accent2 + (205,))

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img.paste(img_rgba.convert("RGB"), (0, 0))


def _draw_stage_capsule(img, box, label: str, active: bool, c1, c2):
    """
    Рисует аккуратную ступень двухдневной смены процедурки без выходящих за края ореолов.
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    bw = x1 - x0
    bh = y1 - y0
    radius = 10

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    if active:
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=radius, fill=255)
        grad = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for px in range(bw):
            t = px / max(1, bw - 1)
            col = _lerp_color(c1, c2, t)
            gd.line([(px, 0), (px, bh)], fill=col + (235,))
        step = 20
        for sx in range(-bh, bw + bh, step):
            gd.line([(sx, bh), (sx + bh, 0)], fill=(255, 255, 255, 36), width=3)
        gd.rectangle([0, 2, bw, max(4, bh // 3)], fill=(255, 255, 255, 45))
        overlay.paste(grad, (x0, y0), mask)
        od.rounded_rectangle([x0, y0, x1, y1], radius=radius, outline=WHITE + (200,), width=2)
    else:
        od.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=(14, 16, 24, 230), outline=(68, 76, 102, 180), width=2)

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img.paste(img_rgba.convert("RGB"), (0, 0))

    draw = ImageDraw.Draw(img)
    seg_font = load_font(FONT_BOLD_PATH, 16)
    sb = draw.textbbox((0, 0), label, font=seg_font)
    sw_txt = sb[2] - sb[0]
    _draw_outlined_text(
        draw,
        (x0 + (bw - sw_txt) // 2, y0 + 9),
        label, seg_font,
        fill=WHITE if active else (130, 138, 165),
        outline_color=(0, 0, 0), outline_w=2 if active else 0,
    )


def _draw_dembel_mini_strip(img, draw, y: int, person: str, duty_type: str, total_duties: int, dembel_info: dict | None, accent, accent2, badge_shape: str) -> int:
    """
    Пункт 4: Рисует компактный мини-HUD личного ДМБ-таймера дежурного прямо под его должностью
    (процент выслуги, дни до дома, остаток нарядов до ДМБ и тонкая неоновая шкала).
    """
    box_w = W - 220
    box_h = 54
    x0 = (W - box_w) // 2
    x1 = x0 + box_w
    y0, y1 = y, y + box_h

    info = _resolve_dembel_info(person, dembel_info)
    font_main = load_font(FONT_BOLD_PATH, 18)
    font_sub = load_font(FONT_BOLD_PATH, 16)

    if info and info.get("has_dembel"):
        prog = float(info.get("progress", 0.0))
        done = bool(info.get("done", False))
        bar_acc = (255, 208, 64) if done else accent
        bar_acc2 = (255, 140, 30) if done else accent2

        _badge_rect(draw, (x0, y0, x1, y1), bar_acc, shape=badge_shape, fill=(14, 16, 26), outline_w=2)

        if done:
            left_txt = f"★ ДМБ ВЫПОЛНЕН ({info.get('target_str', '')}) · СТАТУС: ГРАЖДАНСКИЙ ★"
            right_txt = "100.00%"
        else:
            pct_str = f"{prog * 100:.2f}%"
            left_txt = f"ДМБ ({info.get('target_str', '')}): ОСТАЛОСЬ {info['days']} ДН. {info['hours']} Ч.  ·  {pct_str}"
            dl = info.get("duties_left")
            if dl is not None:
                if dl > 0:
                    forms = ("СВОДКА", "СВОДКИ", "СВОДОК") if duty_type == "svodki" else ("СМЕНА", "СМЕНЫ", "СМЕН")
                    right_txt = f"ДО ДОМА ~{dl} {_plural_ru(dl, forms)}"
                else:
                    right_txt = "КРАЙНИЙ НАРЯД!"
            else:
                right_txt = f"НАРЯДОВ: {total_duties}"

        draw.text((x0 + 22, y0 + 9), left_txt, font=font_main, fill=WHITE if not done else bar_acc)
        rb = draw.textbbox((0, 0), right_txt, font=font_sub)
        rw = rb[2] - rb[0]
        draw.text((x1 - rw - 22, y0 + 11), right_txt, font=font_sub, fill=bar_acc)

        bar_x0, bar_x1 = x0 + 22, x1 - 22
        bar_y0, bar_y1 = y1 - 14, y1 - 6
        draw.rounded_rectangle([bar_x0, bar_y0, bar_x1, bar_y1], radius=4, fill=(32, 36, 52))
        fill_w = int((bar_x1 - bar_x0) * max(0.03, min(1.0, prog)))
        draw.rounded_rectangle([bar_x0, bar_y0, bar_x0 + fill_w, bar_y1], radius=4, fill=bar_acc)
    else:
        _badge_rect(draw, (x0, y0, x1, y1), accent, shape=badge_shape, fill=(14, 16, 26), outline_w=2)
        txt = f"◈  БОЕВОЕ ДЕЖУРСТВО В СТРОЮ   ·   ЗАКРЫТО НАРЯДОВ: {total_duties}  ◈"
        _draw_centered(draw, txt, font_main, y0 + 15, color=accent, shadow=False)

    return box_h


def _draw_doctors_hud_panel(draw, y: int, doctors: tuple | list | None, weather_short: str | None, accent, accent2, badge_shape: str) -> int:
    """
    Пункт 1: Отрисовывает на карточке сводок специальную HUD-плашку с дежурными врачами
    (Ответственный врач, I хирург, II хирург) + краткую строку погоды/маршрута.
    """
    grid_w = W - 180
    x0 = (W - grid_w) // 2
    x1 = x0 + grid_w
    box_h = 108
    y0, y1 = y, y + box_h

    _badge_rect(draw, (x0, y0, x1, y1), accent, shape=badge_shape, fill=(16, 18, 28), outline_w=2)

    hdr_font = load_font(FONT_BOLD_PATH, 15)
    role_font = load_font(FONT_BOLD_PATH, 13)
    doc_font = load_font(FONT_BOLD_PATH, 19)

    w_clean = _clean_for_image(weather_short) if weather_short else ""
    if w_clean and len(w_clean) > 36:
        w_clean = w_clean[:34].rstrip() + "…"
    hdr_text = "◈  ДЕЖУРНАЯ БРИГАДА ВРАЧЕЙ НА СЕГОДНЯ" + (f"   ·   {w_clean.upper()}  ◈" if w_clean else "  ◈")
    _draw_centered(draw, hdr_text, hdr_font, y0 + 10, color=accent, shadow=False)

    if doctors and len(doctors) >= 1:
        roles = ("ОТВ. ДЕЖУРНЫЙ ВРАЧ", "I ХИРУРГ", "II ХИРУРГ")
        cols = min(3, len(doctors))
        pad_side = 18
        gap = 14
        col_w = (grid_w - pad_side * 2 - gap * (cols - 1)) // cols
        cy0 = y0 + 36
        cy1 = y1 - 12

        for i in range(cols):
            cx0 = x0 + pad_side + i * (col_w + gap)
            cx1 = cx0 + col_w
            col_acc = accent if i == 0 else accent2
            draw.rounded_rectangle(
                [cx0, cy0, cx1, cy1],
                radius=10,
                fill=(22, 26, 40),
                outline=col_acc,
                width=2 if i == 0 else 1,
            )
            role_lbl = roles[i] if i < len(roles) else f"ВРАЧ #{i+1}"
            rb = draw.textbbox((0, 0), role_lbl, font=role_font)
            rw = rb[2] - rb[0]
            draw.text((cx0 + (col_w - rw) // 2, cy0 + 6), role_lbl, font=role_font, fill=(155, 165, 190))

            doc_name = _clean_for_image(doctors[i]) or str(doctors[i])
            for fs in (19, 17, 15, 13):
                df = load_font(FONT_BOLD_PATH, fs)
                db = draw.textbbox((0, 0), doc_name, font=df)
                if db[2] - db[0] <= col_w - 14:
                    doc_font = df
                    break
            db = draw.textbbox((0, 0), doc_name, font=doc_font)
            dw = db[2] - db[0]
            draw.text((cx0 + (col_w - dw) // 2, cy0 + 27), doc_name, font=doc_font, fill=WHITE)
    else:
        sub_font = load_font(FONT_BOLD_PATH, 19)
        _draw_centered(
            draw,
            "МАРШРУТ: СОБРАТЬ СВОДКИ ПО ОТДЕЛЕНИЯМ И СДАТЬ ДЕЖУРНОМУ ВРАЧУ ДО 18:00",
            sub_font, y0 + 52, color=WHITE, shadow=False,
        )

    return box_h


def _draw_proc_sanitary_panel(draw, y: int, proc_day: int, next_days: int, accent, accent2, badge_shape: str) -> int:
    """
    Пункт 2 (для Процедурки): Отрисовывает неоновую панель санитарного протокола и статуса двухдневной смены.
    """
    grid_w = W - 180
    x0 = (W - grid_w) // 2
    x1 = x0 + grid_w
    box_h = 108
    y0, y1 = y, y + box_h

    _badge_rect(draw, (x0, y0, x1, y1), accent2, shape=badge_shape, fill=(16, 18, 28), outline_w=2)

    hdr_font = load_font(FONT_BOLD_PATH, 15)
    hdr_text = "◈  САНИТАРНЫЙ ПРОТОКОЛ ПРОЦЕДУРНОЙ   ·   КОНТРОЛЬ СТЕРИЛЬНОСТИ  ◈"
    _draw_centered(draw, hdr_text, hdr_font, y0 + 10, color=accent2, shadow=False)

    items = [
        ("ФАЗА ДЕЖУРСТВА", "ЭКВАТОР (ДЕНЬ 1/2)" if proc_day == 1 else "СДАЧА ПОСТА (2/2)"),
        ("ОБЪЕКТ КОНТРОЛЯ", "ПРОЦЕДУРНЫЙ КАБ."),
        ("ОТДЫХ ПОСЛЕ СМЕНЫ", f"ЧЕРЕЗ {next_days} ДН." if next_days else "ПО ГРАФИКУ"),
    ]
    pad_side = 18
    gap = 14
    col_w = (grid_w - pad_side * 2 - gap * 2) // 3
    cy0 = y0 + 36
    cy1 = y1 - 12
    role_font = load_font(FONT_BOLD_PATH, 13)
    val_font = load_font(FONT_BOLD_PATH, 18)

    for i, (lbl, val) in enumerate(items):
        cx0 = x0 + pad_side + i * (col_w + gap)
        cx1 = cx0 + col_w
        col_acc = accent if i == 0 else accent2
        draw.rounded_rectangle([cx0, cy0, cx1, cy1], radius=10, fill=(22, 26, 40), outline=col_acc, width=2 if i == 0 else 1)
        lb = draw.textbbox((0, 0), lbl, font=role_font)
        draw.text((cx0 + (col_w - (lb[2] - lb[0])) // 2, cy0 + 6), lbl, font=role_font, fill=(155, 165, 190))
        vb = draw.textbbox((0, 0), val, font=val_font)
        draw.text((cx0 + (col_w - (vb[2] - vb[0])) // 2, cy0 + 27), val, font=val_font, fill=WHITE)

    return box_h


def _draw_queue_chain_panel(draw, y: int, queue_chain: list | None, next_person: str, next_days: int, accent, accent2, badge_shape: str) -> int:
    """
    Пункт 3: Рисует мини-инфографику очереди («Кто за тобой»):
    цепочка из 3 следующих дежурных со стрелками (Имя 1 ➔ Имя 2 ➔ Имя 3).
    """
    grid_w = W - 180
    x0 = (W - grid_w) // 2
    x1 = x0 + grid_w
    box_h = 96
    y0, y1 = y, y + box_h

    _badge_rect(draw, (x0, y0, x1, y1), accent2, shape=badge_shape, fill=(16, 18, 28), outline_w=2)

    hdr_font = load_font(FONT_BOLD_PATH, 15)
    _draw_centered(draw, "◈  ОЧЕРЕДЬ НАРЯДОВ: КТО ИДЁТ СЛЕДОМ ЗА ТОБОЙ  ◈", hdr_font, y0 + 10, color=accent2, shadow=False)

    nodes = []
    if queue_chain:
        for item in queue_chain[:3]:
            if isinstance(item, dict):
                n_name = _clean_for_image(item.get("name", "")) or "?"
                n_days = item.get("days", 0)
                day_lbl = "завтра" if n_days == 1 else f"+{n_days}д"
                nodes.append((n_name, day_lbl))
            else:
                nodes.append((_clean_for_image(str(item)), ""))
    elif next_person:
        nodes.append((_clean_for_image(next_person), f"+{next_days}д" if next_days else "следом"))

    if not nodes:
        nodes = [("По расписанию", "")]

    n_count = len(nodes)
    pad_x = 24
    arrow_gap = 38
    node_w = (grid_w - pad_x * 2 - arrow_gap * max(0, n_count - 1)) // max(1, n_count)
    ny0 = y0 + 36
    ny1 = y1 - 12
    name_font = load_font(FONT_BOLD_PATH, 19)
    day_font = load_font(FONT_BOLD_PATH, 15)

    for i, (n_name, day_lbl) in enumerate(nodes):
        nx0 = x0 + pad_x + i * (node_w + arrow_gap)
        nx1 = nx0 + node_w
        n_col = accent if i == 0 else accent2

        draw.rounded_rectangle(
            [nx0, ny0, nx1, ny1],
            radius=12,
            fill=(24, 28, 42) if i == 0 else (20, 22, 34),
            outline=n_col,
            width=2 if i == 0 else 1,
        )

        full_txt = f"#{i+1} {n_name}"
        if day_lbl:
            full_txt += f" ({day_lbl})"
        nb = draw.textbbox((0, 0), full_txt, font=name_font)
        if nb[2] - nb[0] > node_w - 14:
            nb = draw.textbbox((0, 0), full_txt, font=day_font)
            use_f = day_font
        else:
            use_f = name_font
        tw = nb[2] - nb[0]
        th = nb[3] - nb[1]
        draw.text(
            (nx0 + (node_w - tw) // 2, ny0 + (ny1 - ny0 - th) // 2 - 2),
            full_txt,
            font=use_f,
            fill=WHITE if i == 0 else (215, 222, 240),
        )

        if i < n_count - 1:
            ax = nx1 + arrow_gap // 2
            ay = (ny0 + ny1) // 2
            _draw_icon(draw, ax, ay, 10, "chevron", accent)

    return box_h


# ══════════════════════════════════════════════
#  ГЛАВНАЯ ФУНКЦИЯ КАРТОЧКИ НАРЯДОВ (HUD v3.0)
# ══════════════════════════════════════════════
def make_reminder_card(
    duty_type, person, position_label, time_label,
    header_text, ending_text, date_label, mood_label,
    proc_day=1, next_person="", next_days=0, total_duties=0,
    theme_key=None, output_path="/tmp/card.jpg",
    doctors=None, queue_chain=None, dembel_info=None, weather_short=None,
):
    """
    Рисует карточку-напоминание HUD v3.0 и сохраняет её в output_path (JPEG).
    Возвращает использованный theme_key.
    Все новые аргументы (doctors, queue_chain, dembel_info, weather_short) опциональны
    и полностью обратно совместимы.
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
        chosen_bg = (
            random.choice(["circuit", "nebula", "sunburst"])
            if duty_type == "svodki"
            else random.choice(["aurora", "particles", "wave"])
        )
        full_bg = generate_background(theme_key, W, H, bg_style=chosen_bg)
        panel_top = 462
        blurred_bottom = full_bg.crop((0, panel_top - 40, W, H)).filter(ImageFilter.GaussianBlur(18))
        img = full_bg.copy()
        img.paste(blurred_bottom, (0, panel_top - 40))

        img_rgba = img.convert("RGBA")
        fade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fade)
        fade_start = panel_top - 160
        for y in range(fade_start, H):
            t = min(1.0, (y - fade_start) / 260.0)
            a = int(218 * (t ** 1.2))
            fd.line([(0, y), (W, y)], fill=(DARK[0], DARK[1], DARK[2], a))
        img_rgba.alpha_composite(fade)
        img = img_rgba.convert("RGB")

        # ── 2. Стеклянная нижняя главная панель ──────────────────────────
        _draw_glass_panel(
            img,
            (MARGIN + 10, panel_top, W - MARGIN - 10, H - MARGIN - 38),
            accent=accent,
            accent2=accent2,
            fill_alpha=192,
            radius=30,
            cut=22 if style["badge"] == "cut" else 0,
            border_w=2,
        )

        draw = ImageDraw.Draw(img)
        _draw_corner_decorations(draw, accent, margin=26, style=style["corner"])
        if style["corner"] != "frame":
            _draw_panel_frame(draw, margin=16, accent=accent, cut=22, width=1)

        # ── 3. Верхняя шапка: бейдж темы + архетип наряда + дедлайн ──────
        badge_font = load_font(FONT_BOLD_PATH, 21)
        badge_text = f"◈ {theme['title']} ◈"
        bb = draw.textbbox((0, 0), badge_text, font=badge_font)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        pad_x, pad_y = 18, 10
        top_y = MARGIN + 6

        _badge_rect(
            draw,
            (MARGIN + 10, top_y, MARGIN + 10 + bw + pad_x * 2, top_y + bh + pad_y * 2),
            accent, shape=style["badge"], fill=(14, 16, 24), outline_w=2,
        )
        draw.text((MARGIN + 10 + pad_x, top_y + pad_y - 2), badge_text, font=badge_font, fill=accent)

        time_font = load_font(FONT_BOLD_PATH, 21)
        time_str = f"ДЕДЛАЙН {time_label}"
        tb = draw.textbbox((0, 0), time_str, font=time_font)
        tw = tb[2] - tb[0]
        _badge_rect(
            draw,
            (W - MARGIN - 10 - tw - pad_x * 2, top_y, W - MARGIN - 10, top_y + bh + pad_y * 2),
            accent2, shape=style["badge"], fill=(14, 16, 24), outline_w=2,
        )
        draw.text((W - MARGIN - 10 - tw - pad_x, top_y + pad_y - 2), time_str, font=time_font, fill=WHITE)

        line_y = top_y + bh + pad_y * 2 + 12
        _draw_neon_line(draw, MARGIN + 12, line_y, W - MARGIN - 12, line_y, accent, width=2, style=style["line"])

        # ── 4. Пункт 2: Архетипный HUD-медальон (Радар vs Био-контроль) ───
        emblem_cy = line_y + 54
        emblem_r = 34
        _draw_archetype_hero_hud(img, duty_type, W // 2, emblem_cy, accent, accent2)

        emblem_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        eo_draw = ImageDraw.Draw(emblem_overlay)
        _draw_glow_circle(eo_draw, W // 2, emblem_cy, emblem_r * 2, accent, max_alpha=95)
        img_rgba = img.convert("RGBA")
        img_rgba.alpha_composite(emblem_overlay)
        img = img_rgba.convert("RGB")
        draw = ImageDraw.Draw(img)

        draw.ellipse(
            [W // 2 - emblem_r, emblem_cy - emblem_r, W // 2 + emblem_r, emblem_cy + emblem_r],
            fill=(14, 16, 24), outline=accent, width=2,
        )
        _draw_icon(
            draw, W // 2, emblem_cy, emblem_r - 10,
            "radar" if duty_type == "svodki" else "biohazard",
            accent2,
        )

        # ── 5. Имя дежурного (с градиентом и неоновым свечением) ─────────
        for name_size in (88, 78, 68, 58, 50):
            name_font = load_font(FONT_BOLD_PATH, name_size)
            nb = draw.textbbox((0, 0), person_clean, font=name_font)
            if nb[2] - nb[0] <= W - 160:
                break
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = (W - nw) // 2
        ny = emblem_cy + emblem_r + 12

        _draw_glow_text(
            img, (nx, ny), person_clean, name_font,
            fill=accent, glow_color=accent2,
            blur_radius=13, glow_passes=2, outline_w=5,
            top_color=WHITE,
        )
        draw = ImageDraw.Draw(img)

        # ── 6. Капсула позиции + Пункт 4: Мини-прогресс ДМБ дежурного ────
        pos_font = load_font(FONT_BOLD_PATH, 22)
        pos_text = f"◈  {pos_clean.upper()}  ◈"
        pb = draw.textbbox((0, 0), pos_text, font=pos_font)
        pw, ph = pb[2] - pb[0], pb[3] - pb[1]
        py = ny + nh + 18
        pos_box = ((W - pw) // 2 - 24, py - 8, (W + pw) // 2 + 24, py + ph + 10)
        _badge_rect(draw, pos_box, accent2, shape="pill", fill=(14, 16, 26), outline_w=2)
        draw.text(((W - pw) // 2, py), pos_text, font=pos_font, fill=WHITE)

        dembel_strip_y = py + ph + 20
        _draw_dembel_mini_strip(
            img, draw, dembel_strip_y,
            person=person, duty_type=duty_type,
            total_duties=total_duties, dembel_info=dembel_info,
            accent=accent, accent2=accent2, badge_shape=style["badge"],
        )
        draw = ImageDraw.Draw(img)

        # ── 7. Содержимое стеклянной панели ──────────────────────────────
        text_y = panel_top + 20

        meta_font = load_font(FONT_BOLD_PATH, 19)
        meta_parts = [p for p in (date_label.upper(), mood_label.upper()) if p]
        meta_text = "   •   ".join(meta_parts)
        h = _draw_centered(draw, meta_text, meta_font, text_y, color=(160, 170, 195), shadow=False)
        text_y += h + 14

        _draw_neon_line(draw, MARGIN + 45, text_y, W - MARGIN - 45, text_y, accent, width=2, style=style["line"])
        text_y += 18

        # ИИ-заголовок / фраза дня
        max_w = W - 160
        for fs in (36, 32, 28, 25, 22):
            title_font = load_font(FONT_BOLD_PATH, fs)
            title_lines = _wrap_text(draw, header_text.upper(), title_font, max_w)
            if len(title_lines) <= 2:
                break
        for line in title_lines[:2]:
            h = _draw_centered(draw, line, title_font, text_y, color=WHITE, shadow=True, so=3)
            text_y += h + 6
        text_y += 12

        # ── 8. Пункт 2: Специализированный блок задачи (Сводки vs Процедурка)
        duty_font = load_font(FONT_BOLD_PATH, 22)
        box_w = W - 180
        box_x0 = (W - box_w) // 2

        if duty_type == "proc":
            stage_title = (
                "ПРОЦЕДУРКА   ·   ДЕНЬ 1/2 — ЭКВАТОР СМЕНЫ"
                if proc_day == 1
                else "ПРОЦЕДУРКА   ·   ДЕНЬ 2/2 — СДАЧА ПОСТА · ФИНАЛ!"
            )
            box_h = 98
            _badge_rect(
                draw, (box_x0, text_y, box_x0 + box_w, text_y + box_h),
                accent, shape=style["badge"], fill=(20, 22, 34), outline_w=2,
            )
            _draw_centered(draw, stage_title, duty_font, text_y + 11, color=accent, shadow=False)

            seg_gap = 22
            seg_w = (box_w - 64 - seg_gap) // 2
            seg_h = 38
            seg_y = text_y + 46

            for s_idx, s_lbl in enumerate(("ДЕНЬ 1/2 · ЗАСТУПЛЕНИЕ", "ДЕНЬ 2/2 · СДАЧА ПОСТА"), 1):
                sx0 = box_x0 + 32 + (s_idx - 1) * (seg_w + seg_gap)
                sx1 = sx0 + seg_w
                is_active = (proc_day >= s_idx)
                _draw_stage_capsule(
                    img, (sx0, seg_y, sx1, seg_y + seg_h),
                    label=s_lbl, active=is_active,
                    c1=accent if s_idx == 1 else accent2,
                    c2=accent2 if s_idx == 1 else accent,
                )
            draw = ImageDraw.Draw(img)
            text_y += box_h + 14
        else:
            box_h = 88
            _badge_rect(
                draw, (box_x0, text_y, box_x0 + box_w, text_y + box_h),
                accent, shape=style["badge"], fill=(20, 22, 34), outline_w=2,
            )
            duty_line = f"МИССИЯ: СВОДКИ ОТДЕЛЕНИЙ   //   ДЕДЛАЙН {time_label} МСК"
            _draw_centered(draw, duty_line, duty_font, text_y + 11, color=accent, shadow=False)

            route_font = load_font(FONT_BOLD_PATH, 15)
            route_txt = "[ 1. ОБХОД ОТДЕЛЕНИЙ ]   ━━►   [ 2. СВЕРКА СПИСКОВ ]   ━━►   [ 3. ДОКЛАД ВРАЧУ ]"
            _draw_centered(draw, route_txt, route_font, text_y + 50, color=(215, 222, 240), shadow=False)
            text_y += box_h + 14

        # ── 9. Пункт 1: Блок дежурных врачей (Сводки) или Санитарный протокол (Процедурка)
        if duty_type == "svodki":
            h_doc = _draw_doctors_hud_panel(
                draw, text_y, doctors=doctors, weather_short=weather_short,
                accent=accent, accent2=accent2, badge_shape=style["badge"],
            )
            text_y += h_doc + 14
        else:
            h_san = _draw_proc_sanitary_panel(
                draw, text_y, proc_day=proc_day, next_days=next_days,
                accent=accent, accent2=accent2, badge_shape=style["badge"],
            )
            text_y += h_san + 14

        # ── 10. Две симметричные карточки личной статистики ──────────────
        stat_label_font = load_font(FONT_BOLD_PATH, 15)
        stat_val_font   = load_font(FONT_BOLD_PATH, 22)
        grid_w = W - 180
        gap = 22
        col_w = (grid_w - gap) // 2
        start_x = (W - grid_w) // 2

        _, badge_h1 = _draw_stat_badge(
            draw,
            label="ВЫПОЛНЕНО НАРЯДОВ",
            value=f"{total_duties} в зачёте",
            x=start_x, y=text_y, accent=accent,
            font_label=stat_label_font, font_value=stat_val_font,
            shape=style["badge"], icon="star", min_w=col_w,
        )
        next_self_val = f"Через {next_days} дн." if next_days else "По графику"
        _, badge_h2 = _draw_stat_badge(
            draw,
            label="ТВОЙ СЛЕДУЮЩИЙ ЦИКЛ",
            value=next_self_val,
            x=start_x + col_w + gap, y=text_y, accent=accent2,
            font_label=stat_label_font, font_value=stat_val_font,
            shape=style["badge"], icon="shield", min_w=col_w,
        )
        text_y += max(badge_h1, badge_h2) + 14

        # ── 11. Пункт 3: Мини-инфографика очереди («Кто за тобой») ───────
        h_queue = _draw_queue_chain_panel(
            draw, text_y, queue_chain=queue_chain,
            next_person=next_person, next_days=next_days,
            accent=accent, accent2=accent2, badge_shape=style["badge"],
        )
        text_y += h_queue + 12

        # ── 12. Концовка / цитата ────────────────────────────────────────
        if ending_text and text_y < H - MARGIN - 70:
            end_font = load_font(FONT_REGULAR_PATH, 21)
            end_lines = _wrap_text(draw, f"«{ending_text}»", end_font, max_w - 20)
            for line in end_lines[:2]:
                if text_y + 24 > H - MARGIN - 46:
                    break
                h = _draw_centered(draw, line, end_font, text_y, color=(205, 212, 230), shadow=True, so=2)
                text_y += h + 4

        # ── 13. Футер ────────────────────────────────────────────────────
        foot_font = load_font(FONT_BOLD_PATH, 15)
        arch_tag = "RADAR HUD v3.0" if duty_type == "svodki" else "BIO-HUD v3.0"
        foot_text = f"◈ НАРЯД-БОТ  ×  {theme['title']}  ·  {arch_tag} ◈"
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
    out1 = make_reminder_card(
        duty_type="svodki",
        person="Игорь",
        position_label="Дежурный №8 по сводкам",
        time_label="18:00",
        header_text="До ДМБ 10 дней, а Маевский уже ждёт сводки!",
        ending_text="go go go, таймер тикает как в CS",
        date_label="20.10.2026 • вторник",
        mood_label="геймерский",
        next_person="Ильнар",
        next_days=9,
        total_duties=18,
        doctors=("МАЕВСКИЙ М.А.", "РУМЯНЦЕВ К.О.", "РУШАНОВ Р.Р."),
        queue_chain=[
            {"name": "Ильнар", "days": 1},
            {"name": "Арсений", "days": 2},
            {"name": "Глеб", "days": 3},
        ],
        weather_short="+8°C, пасмурно, без дождя",
        theme_key=random.choice(THEME_KEYS),
        output_path=os.path.join(BASE_DIR, "temp", "selftest_svodki.jpg"),
    )
    out2 = make_reminder_card(
        duty_type="proc",
        person="Руслан",
        position_label="Дежурный №3 по процедурке",
        time_label="22:00",
        header_text="Второй день смены — добей процедурку и свободен!",
        ending_text="+50 XP за идеальную чистоту",
        date_label="20.10.2026 • вторник",
        mood_label="ироничный",
        proc_day=2,
        next_person="Игорь",
        next_days=10,
        total_duties=14,
        queue_chain=[
            {"name": "Игорь", "days": 1},
            {"name": "Ильнар", "days": 3},
            {"name": "Глеб", "days": 5},
        ],
        theme_key=random.choice(THEME_KEYS),
        output_path=os.path.join(BASE_DIR, "temp", "selftest_proc.jpg"),
    )
    print("Темы:", out1, out2)