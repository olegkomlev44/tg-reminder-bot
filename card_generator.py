import io
import os
import random
from PIL import Image, ImageDraw, ImageFont

# ══════════════════════════════════════════════
#  ШРИФТЫ – расширенный список
# ══════════════════════════════════════════════
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/arial/arialbd.ttf",
    "/usr/share/fonts/arial/arialbd.ttf",
]
_FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/arial/arial.ttf",
    "/usr/share/fonts/arial/arial.ttf",
]

def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None

FONT_BOLD    = _first_existing(_FONT_CANDIDATES_BOLD)
FONT_REGULAR = _first_existing(_FONT_CANDIDATES_REG)

def load_font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

# ══════════════════════════════════════════════
#  РАЗМЕРЫ И БАЗОВЫЕ ЦВЕТА
# ══════════════════════════════════════════════
W, H   = 1080, 1350     # 4:5
BG_H   = 620
MARGIN = 40

WHITE = (255, 255, 255)
DARK  = (12, 12, 16)

# ══════════════════════════════════════════════
#  23 ТЕМЫ (без изменений)
# ══════════════════════════════════════════════
THEMES = {
    "cs2": {
        "title": "CS2",
        "accent": (255, 178, 40),
        "prompts": [],  # больше не используются
    },
    "dota2": {
        "title": "DOTA 2",
        "accent": (210, 60, 60),
        "prompts": [],
    },
    "genshin": {
        "title": "GENSHIN IMPACT",
        "accent": (130, 195, 255),
        "prompts": [],
    },
    "valorant": {
        "title": "VALORANT",
        "accent": (255, 70, 85),
        "prompts": [],
    },
    "minecraft": {
        "title": "MINECRAFT",
        "accent": (110, 205, 90),
        "prompts": [],
    },
    "gta": {
        "title": "GTA",
        "accent": (255, 222, 60),
        "prompts": [],
    },
    "cyberpunk": {
        "title": "CYBERPUNK",
        "accent": (250, 230, 10),
        "prompts": [],
    },
    "lol": {
        "title": "LEAGUE OF LEGENDS",
        "accent": (20, 200, 220),
        "prompts": [],
    },
    "apex": {
        "title": "APEX LEGENDS",
        "accent": (255, 95, 30),
        "prompts": [],
    },
    "fortnite": {
        "title": "FORTNITE",
        "accent": (150, 95, 255),
        "prompts": [],
    },
    "overwatch": {
        "title": "OVERWATCH",
        "accent": (255, 155, 30),
        "prompts": [],
    },
    "amongus": {
        "title": "AMONG US",
        "accent": (255, 60, 60),
        "prompts": [],
    },
    "warzone": {
        "title": "CALL OF DUTY",
        "accent": (150, 165, 100),
        "prompts": [],
    },
    "pubg": {
        "title": "PUBG",
        "accent": (240, 175, 60),
        "prompts": [],
    },
    "witcher": {
        "title": "THE WITCHER",
        "accent": (205, 170, 95),
        "prompts": [],
    },
    "eldenring": {
        "title": "ELDEN RING",
        "accent": (230, 195, 115),
        "prompts": [],
    },
    "stardew": {
        "title": "STARDEW VALLEY",
        "accent": (255, 175, 95),
        "prompts": [],
    },
    "hollowknight": {
        "title": "HOLLOW KNIGHT",
        "accent": (115, 205, 230),
        "prompts": [],
    },
    "portal": {
        "title": "PORTAL",
        "accent": (255, 145, 30),
        "prompts": [],
    },
    "starwars": {
        "title": "STAR WARS",
        "accent": (255, 222, 105),
        "prompts": [],
    },
    "marvel": {
        "title": "SUPERHEROES",
        "accent": (235, 45, 45),
        "prompts": [],
    },
    "synthwave": {
        "title": "SYNTHWAVE",
        "accent": (255, 60, 180),
        "prompts": [],
    },
    "matrix": {
        "title": "TERMINAL",
        "accent": (60, 255, 110),
        "prompts": [],
    },
}
THEME_KEYS = list(THEMES.keys())

# ══════════════════════════════════════════════
#  ПРОЦЕДУРНАЯ ГЕНЕРАЦИЯ ФОНА (без AI)
# ══════════════════════════════════════════════
def generate_background(theme_key: str, width: int, height: int) -> Image.Image:
    """Создаёт красивый процедурный фон в стиле темы."""
    theme  = THEMES.get(theme_key, THEMES["matrix"])
    accent = theme["accent"]

    # База – тёмный фон
    img = Image.new("RGB", (width, height), (8, 8, 12))
    draw = ImageDraw.Draw(img)

    # 1. Градиент от тёмно-акцентного к чёрному
    dark_accent = tuple(max(0, c - 180) for c in accent)
    for y in range(height):
        t = y / height
        r = int(8 + (dark_accent[0] - 8) * t)
        g = int(8 + (dark_accent[1] - 8) * t)
        b = int(12 + (dark_accent[2] - 12) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # 2. Несколько кругов с акцентным цветом (с прозрачностью через отдельный слой)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    cx, cy = width // 2, int(height * 0.4)
    for r in range(40, max(width, height), 100):
        alpha = max(20, 120 - r // 10)
        color = accent + (alpha,)
        odraw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=random.randint(2, 6))

    # Добавим несколько случайных линий
    for _ in range(8):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        alpha = random.randint(20, 80)
        color = accent + (alpha,)
        odraw.line([(x1, y1), (x2, y2)], fill=color, width=random.randint(1, 4))

    # Наложим overlay на основное изображение
    img = img.convert("RGBA")
    img.alpha_composite(overlay)
    img = img.convert("RGB")

    return img

# ══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ РИСОВАНИЯ (без изменений)
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
    words, lines, cur = text.split(), [], ""
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
    return lines

def _draw_outlined_text(draw, xy, text, font, fill, outline_color=(0, 0, 0), outline_w=2):
    x, y = xy
    for dx in range(-outline_w, outline_w + 1):
        for dy in range(-outline_w, outline_w + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)

def _draw_noise(img: Image.Image, amount: int = 10):
    px = img.load()
    w, h = img.size
    for _ in range(w * h // 8):
        x = random.randint(0, w - 1)
        y = random.randint(0, h - 1)
        r, g, b = px[x, y][:3]
        d = random.randint(-amount, amount)
        px[x, y] = (
            max(0, min(255, r + d)),
            max(0, min(255, g + d)),
            max(0, min(255, b + d)),
        )

# ══════════════════════════════════════════════
#  ГЛАВНАЯ ФУНКЦИЯ – КАРТОЧКА НАПОМИНАНИЯ
# ══════════════════════════════════════════════
def make_reminder_card(
    duty_type: str,             # "svodki" | "proc"
    person: str,
    position_label: str,        # "Дежурный №1 по сводкам"
    time_label: str,            # "18:00" / "22:00"
    header_text: str,           # тематическая фраза дня
    ending_text: str,           # концовка
    date_label: str,            # "15.06.2026 • понедельник"
    mood_label: str,
    proc_day: int = 1,
    next_person: str = "",
    next_days: int = 0,
    total_duties: int = 0,
    theme_key: str | None = None,
    output_path: str = "/tmp/card.jpg",
) -> str:
    """
    Собирает карточку 1080x1350: сверху процедурный фон с именем,
    снизу – панель статистики. Возвращает использованный theme_key.
    """
    if theme_key not in THEMES:
        theme_key = random.choice(THEME_KEYS)
    theme  = THEMES[theme_key]
    accent = theme["accent"]

    # 1. Фон
    bg = generate_background(theme_key, W, BG_H)

    img = Image.new("RGB", (W, H), DARK)
    img.paste(bg, (0, 0))

    # 2. Затемнение перехода
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

    # 3. Бейдж темы (верхний левый)
    badge_font = load_font(FONT_BOLD, 26)
    badge_text = f"[ {theme['title']} ]"
    bb = draw.textbbox((0, 0), badge_text, font=badge_font)
    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
    draw.rectangle(
        [MARGIN - 12, MARGIN - 10, MARGIN + bw + 12, MARGIN + bh + 14],
        fill=(15, 15, 20),
    )
    draw.text((MARGIN, MARGIN), badge_text, font=badge_font, fill=accent)

    # время наряда (верхний правый)
    time_font = load_font(FONT_BOLD, 26)
    tb = draw.textbbox((0, 0), time_label, font=time_font)
    tw = tb[2] - tb[0]
    draw.rectangle(
        [W - MARGIN - tw - 24, MARGIN - 10, W - MARGIN + 12, MARGIN + bh + 14],
        fill=(15, 15, 20),
    )
    draw.text((W - MARGIN - tw - 12, MARGIN), time_label, font=time_font, fill=WHITE)

    # 4. Имя дежурного
    name_font = load_font(FONT_BOLD, 96)
    nb = draw.textbbox((0, 0), person, font=name_font)
    nw, nh = nb[2] - nb[0], nb[3] - nb[1]
    nx = (W - nw) // 2
    ny = BG_H - nh - 150
    _draw_outlined_text(draw, (nx, ny), person, name_font, fill=WHITE, outline_w=3)

    # позиция
    pos_font = load_font(FONT_REGULAR, 30)
    pos_text = f"— {position_label} —"
    px = _centered_x(draw, pos_text, pos_font)
    py = ny + nh + 18
    draw.text((px + 2, py + 2), pos_text, font=pos_font, fill=(0, 0, 0))
    draw.text((px, py), pos_text, font=pos_font, fill=accent)

    # ─────────────────────────────────────────
    #  ИНФО-ПАНЕЛЬ
    # ─────────────────────────────────────────
    text_y = BG_H + 30
    max_w  = W - 110

    # дата • время • настроение
    meta_font = load_font(FONT_REGULAR, 24)
    meta_text = f"{date_label}  •  {mood_label}"
    h = _draw_centered(draw, meta_text, meta_font, text_y, color=(150, 150, 165), shadow=False)
    text_y += h + 18

    # заголовок дня
    for fs in (50, 44, 38, 34):
        title_font = load_font(FONT_BOLD, fs)
        title_lines = _wrap_text(draw, header_text.upper(), title_font, max_w)
        if len(title_lines) <= 3:
            break
    for line in title_lines:
        h = _draw_centered(draw, line, title_font, text_y, color=WHITE)
        text_y += h + 6
    text_y += 10

    # разделитель
    draw.line([(90, text_y), (W - 90, text_y)], fill=accent, width=3)
    text_y += 18

    # тип наряда + прогресс (для процедурки)
    duty_font  = load_font(FONT_BOLD, 32)
    duty_title = "СВОДКИ" if duty_type == "svodki" else "ПРОЦЕДУРКА"
    if duty_type == "proc":
        filled = round((proc_day / 2) * 12)
        bar = "[" + "▓" * filled + "░" * (12 - filled) + "]"
        duty_line = f"{duty_title}  {bar}  ДЕНЬ {proc_day}/2"
    else:
        duty_line = f"{duty_title}  •  до {time_label}"
    h = _draw_centered(draw, duty_line, duty_font, text_y, color=accent)
    text_y += h + 20

    # статистика
    stat_font = load_font(FONT_REGULAR, 26)
    stat_text = f"нарядов всего: {total_duties}   •   следующий: {next_person} ({next_days}д)"
    h = _draw_centered(draw, stat_text, stat_font, text_y, color=(175, 175, 190), shadow=False)
    text_y += h + 24

    draw.line([(150, text_y), (W - 150, text_y)], fill=(55, 55, 65), width=1)
    text_y += 22

    # концовка
    end_font  = load_font(FONT_REGULAR, 30)
    end_lines = _wrap_text(draw, ending_text, end_font, max_w)
    for line in end_lines:
        h = _draw_centered(draw, line, end_font, text_y, color=(200, 200, 210), shadow=True, so=2)
        text_y += h + 6

    # подпись
    foot_font = load_font(FONT_BOLD, 18)
    foot_text = f"• НАРЯД-БОТ × {theme['title']} •"
    fx = _centered_x(draw, foot_text, foot_font)
    draw.text((fx, H - MARGIN - 26), foot_text, font=foot_font, fill=(70, 70, 80))

    # лёгкий шум
    _draw_noise(img, amount=8)

    img.save(output_path, "JPEG", quality=92)
    return theme_key
