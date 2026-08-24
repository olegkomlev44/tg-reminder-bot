"""
card_generator.py — генератор карточек-напоминаний.

v2: 5 случайных лейаутов, новые фоновые эффекты, кинематографическая типографика.
"""

import math
import os
import random
import re
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Шрифты ────────────────────────────────────────────────────────────────────
_FONT_CANDIDATES_BOLD = [
    os.path.join(BASE_DIR, "fonts", "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_CANDIDATES_REG = [
    os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]

def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None

FONT_BOLD_PATH    = _first_existing(_FONT_CANDIDATES_BOLD)
FONT_REGULAR_PATH = _first_existing(_FONT_CANDIDATES_REG)

_FONT_CACHE = {}
def load_font(path, size):
    key = (path, size)
    if key not in _FONT_CACHE:
        font = None
        if path:
            try:
                font = ImageFont.truetype(path, size)
            except Exception:
                pass
        _FONT_CACHE[key] = font or ImageFont.load_default()
    return _FONT_CACHE[key]

# ── Очистка текста от эмодзи ──────────────────────────────────────────────────
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FFFF\U00002600-\U000026FF\U00002700-\U000027BF"
    "\U00002190-\U000021FF\U00002300-\U000023FF\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F\U0000200D]+",
    re.UNICODE,
)
def _clean(text: str) -> str:
    if not text: return ""
    return re.sub(r"[ \t]+", " ", _EMOJI_RE.sub("", text)).strip()

# ── Размеры ───────────────────────────────────────────────────────────────────
W, H   = 1080, 1350
BG_H   = 620
MARGIN = 40
WHITE  = (255, 255, 255)
DARK   = (12, 12, 16)

# ── 23 темы ───────────────────────────────────────────────────────────────────
THEMES = {
    "cs2":         {"title": "CS2",              "accent": (255, 178, 40)},
    "dota2":       {"title": "DOTA 2",           "accent": (210, 60,  60)},
    "genshin":     {"title": "GENSHIN IMPACT",   "accent": (130, 195, 255)},
    "valorant":    {"title": "VALORANT",         "accent": (255, 70,  85)},
    "minecraft":   {"title": "MINECRAFT",        "accent": (110, 205, 90)},
    "gta":         {"title": "GTA",              "accent": (255, 222, 60)},
    "cyberpunk":   {"title": "CYBERPUNK",        "accent": (250, 230, 10)},
    "lol":         {"title": "LEAGUE OF LEGENDS","accent": (20,  200, 220)},
    "apex":        {"title": "APEX LEGENDS",     "accent": (255, 95,  30)},
    "fortnite":    {"title": "FORTNITE",         "accent": (150, 95,  255)},
    "overwatch":   {"title": "OVERWATCH",        "accent": (255, 155, 30)},
    "amongus":     {"title": "AMONG US",         "accent": (255, 60,  60)},
    "warzone":     {"title": "CALL OF DUTY",     "accent": (150, 165, 100)},
    "pubg":        {"title": "PUBG",             "accent": (240, 175, 60)},
    "witcher":     {"title": "THE WITCHER",      "accent": (205, 170, 95)},
    "eldenring":   {"title": "ELDEN RING",       "accent": (230, 195, 115)},
    "stardew":     {"title": "STARDEW VALLEY",   "accent": (255, 175, 95)},
    "hollowknight":{"title": "HOLLOW KNIGHT",    "accent": (115, 205, 230)},
    "portal":      {"title": "PORTAL",           "accent": (255, 145, 30)},
    "starwars":    {"title": "STAR WARS",        "accent": (255, 222, 105)},
    "marvel":      {"title": "SUPERHEROES",      "accent": (235, 45,  45)},
    "synthwave":   {"title": "SYNTHWAVE",        "accent": (255, 60,  180)},
    "matrix":      {"title": "TERMINAL",         "accent": (60,  255, 110)},
}
THEME_KEYS = list(THEMES.keys())


# ══════════════════════════════════════════════════════════════════════════════
#  ФОНОВЫЕ ГЕНЕРАТОРЫ  (5 разных стилей)
# ══════════════════════════════════════════════════════════════════════════════

def _bg_glow_rings(accent, w, h) -> Image.Image:
    """Классический glow + концентрические кольца."""
    dark = tuple(max(0, c - 170) for c in accent)
    img  = Image.new("RGB", (w, h), (4, 4, 8))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        r = int(6 + dark[0] * (1 - t) * 0.6)
        g = int(6 + dark[1] * (1 - t) * 0.6)
        b = int(10 + dark[2] * (1 - t) * 0.7)
        draw.line([(0, y), (w, y)], fill=(min(255,r), min(255,g), min(255,b)))

    ov = Image.new("RGBA", (w, h), (0,0,0,0))
    od = ImageDraw.Draw(ov)
    cx, cy = w//2, int(h * 0.45)

    # glow
    for i in range(8):
        r2 = int(min(w,h) * 0.55 * (1 - i/8 * 0.6))
        a  = int(60 * (1 - i/8))
        od.ellipse([cx-r2, cy-r2, cx+r2, cy+r2], fill=accent+(a,))

    # кольца
    for ring_r in range(80, max(w,h), random.randint(70, 110)):
        a  = max(6, 70 - ring_r // 14)
        od.ellipse([cx-ring_r, cy-ring_r, cx+ring_r, cy+ring_r],
                   outline=accent+(a,), width=random.randint(1,3))

    # лучи
    for _ in range(random.randint(5, 10)):
        angle = random.uniform(0, 6.28)
        length = random.randint(int(h*0.3), int(h*0.9))
        x1 = cx + random.randint(-60, 60)
        y1 = cy + random.randint(-40, 40)
        x2 = x1 + int(length * math.cos(angle))
        y2 = y1 + int(length * math.sin(angle))
        od.line([(x1,y1),(x2,y2)], fill=accent+(random.randint(12,50),),
                width=random.randint(1,3))

    # звёзды
    for _ in range(random.randint(60, 120)):
        sx, sy = random.randint(0,w), random.randint(0,h)
        sr = random.randint(1,4)
        od.ellipse([sx-sr,sy-sr,sx+sr,sy+sr], fill=accent+(random.randint(30,160),))

    img = img.convert("RGBA"); img.alpha_composite(ov)
    return img.convert("RGB")


def _bg_diagonal_stripes(accent, w, h) -> Image.Image:
    """Диагональные полосы — кинематографический стиль."""
    r0, g0, b0 = tuple(max(0, c - 200) for c in accent)
    img  = Image.new("RGB", (w, h), (r0, g0, b0))
    draw = ImageDraw.Draw(img)

    # фоновый градиент
    for y in range(h):
        t = y / h
        draw.line([(0,y),(w,y)], fill=(
            int(r0 + accent[0]*0.12*(1-t)),
            int(g0 + accent[1]*0.12*(1-t)),
            int(b0 + accent[2]*0.14*(1-t)),
        ))

    ov = Image.new("RGBA", (w, h), (0,0,0,0))
    od = ImageDraw.Draw(ov)

    # диагональные полосы
    step = random.randint(55, 90)
    for x in range(-h, w + h, step):
        alpha = random.randint(15, 45)
        od.polygon([
            (x, 0), (x + step//3, 0),
            (x + step//3 + h, h), (x + h, h)
        ], fill=accent + (alpha,))

    # крупный glow в углу
    cx, cy = int(w * 0.25), int(h * 0.3)
    for i in range(6):
        r2 = int(min(w,h) * 0.4 * (1 - i/6*0.5))
        od.ellipse([cx-r2,cy-r2,cx+r2,cy+r2], fill=accent+(int(40*(1-i/6)),))

    # горизонтальные линии-акценты
    for _ in range(random.randint(3,7)):
        ly = random.randint(0, h)
        od.line([(0,ly),(w,ly)], fill=accent+(random.randint(20,60),),
                width=random.randint(1,2))

    img = img.convert("RGBA"); img.alpha_composite(ov)
    return img.convert("RGB")


def _bg_grid_matrix(accent, w, h) -> Image.Image:
    """Матричная сетка — технический стиль."""
    img  = Image.new("RGB", (w, h), (3, 5, 3))
    draw = ImageDraw.Draw(img)

    for y in range(h):
        t = y/h
        draw.line([(0,y),(w,y)], fill=(int(3+5*t), int(5+10*t), int(3+5*t)))

    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    od = ImageDraw.Draw(ov)

    # вертикальные линии сетки
    for x in range(0, w, random.randint(40, 70)):
        a = random.randint(8, 30)
        od.line([(x,0),(x,h)], fill=accent+(a,), width=1)
    # горизонтальные
    for y in range(0, h, random.randint(40, 70)):
        a = random.randint(8, 20)
        od.line([(0,y),(w,y)], fill=accent+(a,), width=1)

    # "падающие" вертикальные полосы
    for _ in range(random.randint(8,16)):
        x = random.randint(0, w)
        seg_h = random.randint(80, 300)
        y = random.randint(0, h - seg_h)
        a = random.randint(40, 120)
        od.rectangle([x-1, y, x+1, y+seg_h], fill=accent+(a,))

    # glow в центре
    cx, cy = w//2, h//2
    for i in range(5):
        r2 = int(min(w,h)*0.3*(1-i/5*0.5))
        od.ellipse([cx-r2,cy-r2,cx+r2,cy+r2], fill=accent+(int(30*(1-i/5)),))

    img = img.convert("RGBA"); img.alpha_composite(ov)
    return img.convert("RGB")


def _bg_aurora(accent, w, h) -> Image.Image:
    """Аурора — плавные волны цвета."""
    r0,g0,b0 = tuple(max(0,c-210) for c in accent)
    img  = Image.new("RGB", (w,h), (r0,g0,b0))
    draw = ImageDraw.Draw(img)

    # базовый градиент
    for y in range(h):
        t = y/h
        draw.line([(0,y),(w,y)], fill=(
            int(r0*(1-t*0.3)),
            int(g0*(1-t*0.3)),
            int(b0*(1-t*0.2)),
        ))

    # волны ауроры
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    od = ImageDraw.Draw(ov)

    for wave_i in range(random.randint(4,7)):
        pts = []
        amp  = random.randint(60, 200)
        freq = random.uniform(0.003, 0.008)
        base_y = random.randint(0, h)
        phase = random.uniform(0, 6.28)
        for x in range(0, w+1, 4):
            y = base_y + int(amp * math.sin(freq * x + phase))
            pts.extend([x, y])
        # рисуем заливкой через polygon
        wave_pts  = [(pts[i], pts[i+1]) for i in range(0, len(pts)-1, 2)]
        poly = wave_pts + [(w, h), (0, h)]
        alpha = random.randint(12, 35)
        od.polygon(poly, fill=accent+(alpha,))

    # блики
    for _ in range(random.randint(40, 80)):
        sx, sy = random.randint(0,w), random.randint(0,h)
        sr = random.randint(1,5)
        od.ellipse([sx-sr,sy-sr,sx+sr,sy+sr], fill=(255,255,255,random.randint(20,80)))

    img = img.convert("RGBA"); img.alpha_composite(ov)
    return img.convert("RGB")


def _bg_hexagon_tech(accent, w, h) -> Image.Image:
    """Технические гексагоны — sci-fi стиль."""
    dark = tuple(max(0,c-180) for c in accent)
    img  = Image.new("RGB", (w,h), dark)
    draw = ImageDraw.Draw(img)

    for y in range(h):
        t = y/h
        draw.line([(0,y),(w,y)], fill=(
            int(dark[0]*(1-t*0.4)+accent[0]*t*0.08),
            int(dark[1]*(1-t*0.4)+accent[1]*t*0.08),
            int(dark[2]*(1-t*0.4)+accent[2]*t*0.10),
        ))

    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    od = ImageDraw.Draw(ov)

    # гексагональная сетка с вариативным размером
    spacing = random.randint(80, 130)
    hex_h   = int(spacing * 0.866)
    for row in range(-1, h//hex_h + 2):
        for col in range(-1, w//spacing + 2):
            cx = col * spacing + (spacing//2 if row%2 else 0)
            cy = row * hex_h
            # случайно выделяем некоторые гексы ярче
            highlight = random.random() < 0.08
            pts = []
            for i in range(6):
                angle = math.radians(60*i - 30)
                pts.append((cx + spacing//2 * math.cos(angle),
                             cy + spacing//2 * math.sin(angle)))
            a = random.randint(30, 90) if highlight else random.randint(8, 22)
            if highlight:
                od.polygon(pts, fill=accent+(a//3,))
            for i in range(6):
                od.line([pts[i], pts[(i+1)%6]], fill=accent+(a,), width=1)

    # центральный glow
    cx, cy = w//2, int(h*0.4)
    for i in range(6):
        r2 = int(min(w,h)*0.45*(1-i/6*0.55))
        od.ellipse([cx-r2,cy-r2,cx+r2,cy+r2], fill=accent+(int(45*(1-i/6)),))

    img = img.convert("RGBA"); img.alpha_composite(ov)
    return img.convert("RGB")


def generate_background(theme_key: str, width: int, height: int) -> Image.Image:
    accent    = THEMES.get(theme_key, THEMES["matrix"])["accent"]
    generator = random.choice([
        _bg_glow_rings,
        _bg_diagonal_stripes,
        _bg_grid_matrix,
        _bg_aurora,
        _bg_hexagon_tech,
    ])
    img = generator(accent, width, height)

    # scanlines на всех
    ov  = Image.new("RGBA", img.size, (0,0,0,0))
    od  = ImageDraw.Draw(ov)
    for y in range(0, height, 4):
        od.line([(0,y),(width,y)], fill=(0,0,0,random.randint(8,18)))
    img = img.convert("RGBA"); img.alpha_composite(ov)
    return img.convert("RGB")


# ══════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ПРИМИТИВЫ
# ══════════════════════════════════════════════════════════════════════════════

def _centered_x(draw, text, font):
    bb = draw.textbbox((0,0), text, font=font)
    return (W - (bb[2]-bb[0])) // 2

def _tw(draw, text, font):
    bb = draw.textbbox((0,0), text, font=font)
    return bb[2]-bb[0], bb[3]-bb[1]

def _draw_centered(draw, text, font, y, color=WHITE, shadow=True, so=3):
    x = _centered_x(draw, text, font)
    if shadow:
        draw.text((x+so, y+so), text, font=font, fill=(0,0,0))
    draw.text((x, y), text, font=font, fill=color)
    _, h = _tw(draw, text, font)
    return h

def _wrap_text(draw, text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur+" "+w).strip()
        if _tw(draw, test, font)[0] <= max_w:
            cur = test
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines or [""]

def _draw_outlined(draw, xy, text, font, fill, outline=(0,0,0), ow=3):
    x, y = xy
    for dx in range(-ow, ow+1):
        for dy in range(-ow, ow+1):
            if dx or dy:
                draw.text((x+dx, y+dy), text, font=font, fill=outline)
    draw.text((x,y), text, font=font, fill=fill)

def _neon_line(draw, x1, y1, x2, y2, accent, width=2):
    r, g, b = accent
    draw.line([(x1,y1),(x2,y2)],
              fill=(min(255,r+60), min(255,g+60), min(255,b+60)), width=width+4)
    draw.line([(x1,y1),(x2,y2)], fill=(255,255,255), width=1)

def _corner_marks(draw, accent, margin=28):
    arm, thick, col = 48, 3, accent
    for x, y, dh, dv in [
        (margin, margin, 1, 1), (W-margin, margin, -1, 1),
        (margin, H-margin, 1, -1), (W-margin, H-margin, -1, -1)
    ]:
        draw.line([(x,y),(x+dh*arm,y)], fill=col, width=thick)
        draw.line([(x,y),(x,y+dv*arm)], fill=col, width=thick)
        draw.rectangle([x-4,y-4,x+4,y+4], fill=col)

def _stat_badge(draw, label, value, x, y, accent, fl, fv):
    pad = 14
    lw, lh = _tw(draw, label, fl)
    vw, vh = _tw(draw, value, fv)
    bw = max(lw, vw) + pad*2
    bh = lh + vh + pad*3
    draw.rectangle([x,y,x+bw,y+bh], fill=(18,18,24), outline=accent, width=1)
    draw.text((x+pad, y+pad), label, font=fl, fill=(130,130,150))
    draw.text((x+pad, y+pad+lh+4), value, font=fv, fill=WHITE)
    return bw, bh

def _draw_noise(img, amount=7):
    px = img.load(); ww, hh = img.size
    for _ in range(ww*hh//8):
        x, y = random.randint(0,ww-1), random.randint(0,hh-1)
        r,g,b = px[x,y][:3]
        d = random.randint(-amount, amount)
        px[x,y] = (max(0,min(255,r+d)), max(0,min(255,g+d)), max(0,min(255,b+d)))

def _blend_bg(bg: Image.Image) -> Image.Image:
    """Накладывает fade снизу BG_H → тёмный низ."""
    img = Image.new("RGB", (W, H), DARK)
    img.paste(bg, (0,0))
    img = img.convert("RGBA")
    fade = Image.new("RGBA", (W,H), (0,0,0,0))
    fd   = ImageDraw.Draw(fade)
    fade_h = 280
    for y in range(BG_H-fade_h, BG_H):
        a = int(250 * (y-(BG_H-fade_h)) / fade_h)
        fd.line([(0,y),(W,y)], fill=(DARK[0],DARK[1],DARK[2],a))
    fd.rectangle([0,BG_H,W,H], fill=(DARK[0],DARK[1],DARK[2],255))
    img.alpha_composite(fade)
    return img.convert("RGB")


# ══════════════════════════════════════════════════════════════════════════════
#  5 ЛЕЙАУТОВ КАРТОЧКИ
# ══════════════════════════════════════════════════════════════════════════════

def _layout_classic(draw, img, theme_key, accent, theme,
                    person, position_label, time_label, header_text,
                    ending_text, date_label, mood_label,
                    duty_type, proc_day, next_person, next_days, total_duties):
    """Классический вертикальный лейаут — имя по центру."""
    _corner_marks(draw, accent)

    # верхний бейдж
    bf  = load_font(FONT_BOLD_PATH, 24)
    btx = f"◈ {theme['title']} ◈"
    bw, bh = _tw(draw, btx, bf)
    draw.rectangle([MARGIN-10, MARGIN-6, MARGIN+bw+10, MARGIN+bh+8],
                   fill=(12,12,18), outline=accent, width=1)
    draw.text((MARGIN, MARGIN), btx, font=bf, fill=accent)
    # время справа
    tw2, _ = _tw(draw, time_label, bf)
    draw.rectangle([W-MARGIN-tw2-20, MARGIN-6, W-MARGIN+10, MARGIN+bh+8],
                   fill=(12,12,18), outline=accent, width=1)
    draw.text((W-MARGIN-tw2-10, MARGIN), time_label, font=bf, fill=WHITE)

    line_y = MARGIN + bh + 18
    _neon_line(draw, MARGIN, line_y, W-MARGIN, line_y, accent)

    # имя
    for s in (100, 88, 76, 64, 54):
        nf = load_font(FONT_BOLD_PATH, s)
        nw, nh = _tw(draw, person, nf)
        if nw <= W-160: break
    nx, ny = (W-nw)//2, BG_H-nh-160
    _draw_outlined(draw, (nx,ny), person, nf, WHITE, ow=4)

    # позиция
    pf = load_font(FONT_REGULAR_PATH, 28)
    pt = f"— {position_label} —"
    pw, ph = _tw(draw, pt, pf)
    px = (W-pw)//2; py = ny+nh+14
    draw.rectangle([px-16,py-4,px+pw+16,py+ph+6], fill=(10,10,16))
    draw.text((px,py), pt, font=pf, fill=accent)

    # нижняя панель
    ty = BG_H + 22
    mf = load_font(FONT_REGULAR_PATH, 22)
    h = _draw_centered(draw, f"{date_label}  ·  {mood_label}", mf, ty,
                       color=(120,120,140), shadow=False); ty += h+14
    _neon_line(draw, 60, ty, W-60, ty, accent, width=2); ty += 18

    # заголовок
    for fs in (46,40,34,30,26):
        tf = load_font(FONT_BOLD_PATH, fs)
        lines = _wrap_text(draw, header_text.upper(), tf, W-100)
        if len(lines) <= 3: break
    for l in lines:
        h = _draw_centered(draw, l, tf, ty, shadow=True, so=2); ty += h+4
    ty += 8

    # duty badge
    df = load_font(FONT_BOLD_PATH, 28)
    if duty_type == "proc":
        filled = round((proc_day/2)*10)
        dl = f"[ {'▓'*filled}{'░'*(10-filled)} ]  ДЕНЬ {proc_day}/2"
    else:
        dl = f">>> СВОДКИ  /  ДО {time_label} <<<"
    dw, dh = _tw(draw, dl, df)
    dx = (W-dw)//2
    draw.rectangle([dx-18,ty-6,dx+dw+18,ty+dh+10], fill=(20,20,28), outline=accent, width=2)
    draw.text((dx,ty), dl, font=df, fill=accent); ty += dh+22

    # бейджи статистики
    sl = load_font(FONT_REGULAR_PATH, 18); sv = load_font(FONT_BOLD_PATH, 22)
    w1,h1 = _stat_badge(draw,"НАРЯДОВ ВСЕГО",str(total_duties),100,ty,accent,sl,sv)
    _stat_badge(draw,"СЛЕДУЮЩИЙ",f"{next_person} ({next_days}д)",100+w1+20,ty,accent,sl,sv)
    ty += max(h1,32)+20

    draw.line([(120,ty),(W-120,ty)], fill=(40,40,55), width=1); ty += 18

    if ending_text:
        ef = load_font(FONT_REGULAR_PATH, 26)
        for l in _wrap_text(draw, ending_text, ef, W-100)[:3]:
            h = _draw_centered(draw,l,ef,ty,color=(185,185,200),shadow=True,so=2); ty+=h+4

    ff = load_font(FONT_BOLD_PATH, 16)
    ft = f"◈ НАРЯД-БОТ  ×  {theme['title']} ◈"
    fx = _centered_x(draw, ft, ff)
    draw.text((fx, H-MARGIN-22), ft, font=ff, fill=(55,55,70))


def _layout_split(draw, img, theme_key, accent, theme,
                  person, position_label, time_label, header_text,
                  ending_text, date_label, mood_label,
                  duty_type, proc_day, next_person, next_days, total_duties):
    """Split-лейаут: левая тёмная панель + правый визуал."""
    panel_w = W // 2

    # тёмная левая панель
    ov = Image.new("RGBA", (W,H),(0,0,0,0))
    od = ImageDraw.Draw(ov)
    od.rectangle([0,0,panel_w,H], fill=(8,8,12,230))
    img.convert("RGBA").alpha_composite(ov)
    img_temp = img.convert("RGBA"); img_temp.alpha_composite(ov); img.paste(img_temp.convert("RGB"))

    # неоновая вертикальная линия-разделитель
    _neon_line(draw, panel_w, 0, panel_w, H, accent, width=2)

    _corner_marks(draw, accent)

    # левая панель: текст
    lm = 50; ty = 80
    bf = load_font(FONT_BOLD_PATH, 20)
    draw.text((lm, ty), f"◈ {theme['title']}", font=bf, fill=accent); ty += 40
    _neon_line(draw, lm, ty, panel_w-lm, ty, accent, width=1); ty += 20

    # тип наряда
    dtype_txt = "СВОДКИ" if duty_type=="svodki" else "ПРОЦЕДУРКА"
    dtf = load_font(FONT_BOLD_PATH, 40)
    for l in _wrap_text(draw, dtype_txt, dtf, panel_w-lm*2):
        h = draw.textbbox((0,0),l,font=dtf)[3]
        draw.text((lm,ty), l, font=dtf, fill=accent); ty += h+4
    ty += 16

    # имя
    for s in (72,60,52,44):
        nf = load_font(FONT_BOLD_PATH,s)
        nw,nh = _tw(draw,person,nf)
        if nw <= panel_w-lm*2: break
    _draw_outlined(draw,(lm,ty),person,nf,WHITE,ow=3); ty += nh+12

    # позиция
    pf = load_font(FONT_REGULAR_PATH,20)
    for l in _wrap_text(draw,position_label,pf,panel_w-lm*2):
        draw.text((lm,ty),l,font=pf,fill=(160,160,180)); ty += _tw(draw,l,pf)[1]+4
    ty += 12

    _neon_line(draw,lm,ty,panel_w-lm,ty,accent,width=1); ty+=16

    # время и дата
    tf2 = load_font(FONT_BOLD_PATH,36)
    draw.text((lm,ty),f"⏰ {time_label}",font=tf2,fill=WHITE); ty += 50
    df2 = load_font(FONT_REGULAR_PATH,18)
    draw.text((lm,ty),date_label,font=df2,fill=(100,100,120)); ty += 32

    # прогресс-бар (для proc)
    if duty_type == "proc":
        filled = round((proc_day/2)*12)
        bar_txt = f"{'▓'*filled}{'░'*(12-filled)}"
        bf2 = load_font(FONT_BOLD_PATH,22)
        draw.text((lm,ty),f"[ {bar_txt} ]",font=bf2,fill=accent); ty+=36
        draw.text((lm,ty),f"ДЕНЬ {proc_day}/2",font=df2,fill=(160,160,180)); ty+=30

    ty += 20
    _neon_line(draw,lm,ty,panel_w-lm,ty,accent,width=1); ty+=16

    # заголовок
    hf = load_font(FONT_BOLD_PATH,24)
    for l in _wrap_text(draw,header_text.upper(),hf,panel_w-lm*2)[:4]:
        draw.text((lm,ty),l,font=hf,fill=(220,220,220)); ty += _tw(draw,l,hf)[1]+4
    ty += 12

    if ending_text:
        ef = load_font(FONT_REGULAR_PATH,18)
        for l in _wrap_text(draw,ending_text,ef,panel_w-lm*2)[:3]:
            draw.text((lm,ty),l,font=ef,fill=(140,140,160)); ty += _tw(draw,l,ef)[1]+4

    # статистика снизу левой панели
    sl = load_font(FONT_REGULAR_PATH,16); sv = load_font(FONT_BOLD_PATH,18)
    _stat_badge(draw,"ВСЕГО",str(total_duties),lm,H-160,accent,sl,sv)
    _stat_badge(draw,"СЛЕД.",f"{next_person}",lm+130,H-160,accent,sl,sv)

    ff = load_font(FONT_BOLD_PATH,13)
    draw.text((lm,H-40),f"НАРЯД-БОТ × {theme['title']}",font=ff,fill=(55,55,70))


def _layout_cinematic(draw, img, theme_key, accent, theme,
                      person, position_label, time_label, header_text,
                      ending_text, date_label, mood_label,
                      duty_type, proc_day, next_person, next_days, total_duties):
    """Кинематографический: широкие поля сверху и снизу, имя в центре огромное."""
    _corner_marks(draw, accent, margin=24)

    # горизонтальные полосы сверху и снизу (letterbox)
    ov = Image.new("RGBA",(W,H),(0,0,0,0))
    od = ImageDraw.Draw(ov)
    bar_h = 100
    od.rectangle([0,0,W,bar_h],fill=(0,0,0,200))
    od.rectangle([0,H-bar_h,W,H],fill=(0,0,0,200))
    img_t = img.convert("RGBA"); img_t.alpha_composite(ov); img.paste(img_t.convert("RGB"))

    # верхняя строка
    bf = load_font(FONT_BOLD_PATH,22)
    ty_top = 30
    draw.text((60, ty_top), f"◈ {theme['title']} ◈", font=bf, fill=accent)
    tw_r, _ = _tw(draw, time_label, bf)
    draw.text((W-60-tw_r, ty_top), time_label, font=bf, fill=WHITE)
    _neon_line(draw, 60, bar_h, W-60, bar_h, accent, width=1)

    # огромное имя по центру
    for s in (160,140,120,100,84):
        nf = load_font(FONT_BOLD_PATH,s)
        nw,nh = _tw(draw,person,nf)
        if nw <= W-120: break
    nx = (W-nw)//2; ny = (H-nh)//2 - 80
    _draw_outlined(draw,(nx,ny),person,nf,accent,ow=5)

    # тонкие линии вокруг имени
    _neon_line(draw,60,ny-20,W-60,ny-20,accent,width=1)
    _neon_line(draw,60,ny+nh+20,W-60,ny+nh+20,accent,width=1)

    # duty type
    dtf = load_font(FONT_BOLD_PATH,38)
    dtype_txt = ("СВОДКИ" if duty_type=="svodki" else f"ПРОЦЕДУРКА  ДЕНЬ {proc_day}/2")
    dw,dh = _tw(draw,dtype_txt,dtf)
    draw.text(((W-dw)//2, ny+nh+36), dtype_txt, font=dtf, fill=WHITE)

    # позиция
    pf = load_font(FONT_REGULAR_PATH,24)
    pw,ph = _tw(draw,position_label,pf)
    draw.text(((W-pw)//2, ny+nh+36+dh+12), position_label, font=pf, fill=(160,160,180))

    # header снизу над нижней полосой
    ty_b = H-bar_h+18
    hf = load_font(FONT_BOLD_PATH,26)
    lines = _wrap_text(draw, header_text.upper(), hf, W-200)[:2]
    for l in lines:
        lw,lh = _tw(draw,l,hf)
        draw.text(((W-lw)//2,ty_b),l,font=hf,fill=WHITE)
        ty_b += lh+4

    # статистика внизу
    sl = load_font(FONT_REGULAR_PATH,17); sv = load_font(FONT_BOLD_PATH,20)
    bw,bh = _stat_badge(draw,"НАРЯДОВ",str(total_duties),
                        W//2-220,H-bar_h-bh-20 if False else H//2+200,accent,sl,sv)
    _stat_badge(draw,"СЛЕДУЮЩИЙ",f"{next_person} ({next_days}д)",
                W//2-220+bw+16,H//2+200,accent,sl,sv)


def _layout_hud(draw, img, theme_key, accent, theme,
                person, position_label, time_label, header_text,
                ending_text, date_label, mood_label,
                duty_type, proc_day, next_person, next_days, total_duties):
    """HUD-стиль: геймерский интерфейс с рамками и индикаторами."""
    # внешняя рамка
    draw.rectangle([20,20,W-20,H-20], outline=accent, width=2)
    draw.rectangle([28,28,W-28,H-28], outline=(*accent,80), width=1)
    _corner_marks(draw, accent, margin=20)

    # угловые маркеры
    for x,y in [(20,20),(W-20,20),(20,H-20),(W-20,H-20)]:
        draw.rectangle([x-6,y-6,x+6,y+6], fill=accent)

    # верхний HUD-бар
    bf = load_font(FONT_BOLD_PATH,20)
    hud_top = 45
    l_txt = f"[DUTY] {('SVODKI' if duty_type=='svodki' else 'PROC')}"
    draw.text((50,hud_top), l_txt, font=bf, fill=accent)
    r_txt = f"{time_label} | {date_label}"
    rw,_ = _tw(draw,r_txt,bf)
    draw.text((W-50-rw, hud_top), r_txt, font=bf, fill=WHITE)
    _neon_line(draw,40,hud_top+30,W-40,hud_top+30,accent,width=1)

    # имя — большое
    ty = BG_H//2 - 60
    for s in (96,84,72,60):
        nf = load_font(FONT_BOLD_PATH,s)
        nw,nh = _tw(draw,person,nf)
        if nw <= W-160: break
    _draw_outlined(draw,((W-nw)//2,ty),person,nf,WHITE,ow=4)

    # тема — сбоку
    tf2 = load_font(FONT_BOLD_PATH,18)
    tw2, _ = _tw(draw, theme["title"], tf2)
    draw.text(((W-tw2)//2, ty+nh+10), theme["title"], font=tf2, fill=accent)

    # Нижняя панель
    panel_y = BG_H + 30; pm = 70
    mf = load_font(FONT_REGULAR_PATH,20)
    h = _draw_centered(draw, f"{position_label}", mf, panel_y,
                       color=(160,160,180), shadow=False); panel_y+=h+10
    _neon_line(draw,pm,panel_y,W-pm,panel_y,accent); panel_y+=16

    # прогресс
    if duty_type=="proc":
        pct = proc_day / 2
        bar_w = W-pm*2; bar_h2 = 24
        draw.rounded_rectangle([pm,panel_y,pm+bar_w,panel_y+bar_h2],
                                radius=12,fill=(20,20,28),outline=accent,width=1)
        fw = int(bar_w*pct)
        if fw > 12:
            draw.rounded_rectangle([pm,panel_y,pm+fw,panel_y+bar_h2],
                                    radius=12,fill=accent)
        ptf = load_font(FONT_BOLD_PATH,16)
        draw.text((pm, panel_y+bar_h2+6), f"ДЕНЬ {proc_day}/2", font=ptf, fill=(160,160,180))
        panel_y += bar_h2+36

    # заголовок
    hf = load_font(FONT_BOLD_PATH,34)
    for fs in (34,28,24,20):
        hf = load_font(FONT_BOLD_PATH,fs)
        lines = _wrap_text(draw,header_text.upper(),hf,W-pm*2)
        if len(lines)<=3: break
    for l in lines:
        h = _draw_centered(draw,l,hf,panel_y,shadow=True,so=2); panel_y+=h+4
    panel_y+=10

    # ending
    if ending_text:
        ef = load_font(FONT_REGULAR_PATH,24)
        for l in _wrap_text(draw,ending_text,ef,W-pm*2)[:2]:
            h = _draw_centered(draw,l,ef,panel_y,color=(185,185,200),shadow=True); panel_y+=h+4
    panel_y+=10

    # статистика
    sl = load_font(FONT_REGULAR_PATH,16); sv = load_font(FONT_BOLD_PATH,20)
    w1,h1 = _stat_badge(draw,"НАРЯДОВ ВСЕГО",str(total_duties),pm,panel_y,accent,sl,sv)
    w2,h2 = _stat_badge(draw,"СЛЕДУЮЩИЙ",f"{next_person} ({next_days}д)",
                        pm+w1+16,panel_y,accent,sl,sv)
    panel_y += max(h1,h2)+16

    # футер
    _neon_line(draw,pm,H-70,W-pm,H-70,accent,width=1)
    ff = load_font(FONT_BOLD_PATH,16)
    ft = f"[ НАРЯД-БОТ  ×  {theme['title']} ]"
    draw.text((_centered_x(draw,ft,ff), H-55), ft, font=ff, fill=(70,70,90))


def _layout_poster(draw, img, theme_key, accent, theme,
                   person, position_label, time_label, header_text,
                   ending_text, date_label, mood_label,
                   duty_type, proc_day, next_person, next_days, total_duties):
    """Постер: крупная типографика, минимализм."""
    _corner_marks(draw, accent, margin=32)

    # большая вертикальная линия слева
    _neon_line(draw, 60, 60, 60, H-60, accent, width=2)

    lm = 90  # отступ от линии
    ty = 70

    # тема
    bf = load_font(FONT_BOLD_PATH,18)
    draw.text((lm, ty), theme["title"].upper(), font=bf, fill=accent); ty+=34

    # горизонтальный разделитель
    _neon_line(draw, lm, ty, W-60, ty, accent, width=1); ty += 24

    # тип наряда — огромный
    for s in (120,100,88,76):
        dtf = load_font(FONT_BOLD_PATH,s)
        dtype = "СВОДКИ" if duty_type=="svodki" else "ПРОЦ."
        dw,dh = _tw(draw,dtype,dtf)
        if dw <= W-lm-60: break
    _draw_outlined(draw,(lm,ty),dtype,dtf,accent,ow=4); ty += dh+10

    # имя — чуть меньше
    for s in (80,68,58,48):
        nf = load_font(FONT_BOLD_PATH,s)
        nw,nh = _tw(draw,person,nf)
        if nw <= W-lm-60: break
    _draw_outlined(draw,(lm,ty),person,nf,WHITE,ow=3); ty += nh+16

    _neon_line(draw,lm,ty,W-60,ty,accent,width=1); ty += 20

    # позиция
    pf = load_font(FONT_REGULAR_PATH,26)
    draw.text((lm,ty),position_label,font=pf,fill=(160,160,180)); ty+=36

    # время
    tf2 = load_font(FONT_BOLD_PATH,42)
    draw.text((lm,ty),f"До {time_label}",font=tf2,fill=WHITE); ty+=60

    if duty_type=="proc":
        pbar_f = load_font(FONT_BOLD_PATH,28)
        filled = round((proc_day/2)*14)
        draw.text((lm,ty),f"{'▓'*filled}{'░'*(14-filled)}  {proc_day}/2",
                  font=pbar_f,fill=accent); ty+=48

    _neon_line(draw,lm,ty,W-60,ty,accent,width=1); ty+=24

    hf = load_font(FONT_BOLD_PATH,30)
    for l in _wrap_text(draw,header_text.upper(),hf,W-lm-80)[:3]:
        draw.text((lm,ty),l,font=hf,fill=(220,220,220)); ty+=_tw(draw,l,hf)[1]+6
    ty+=16

    if ending_text:
        ef = load_font(FONT_REGULAR_PATH,22)
        for l in _wrap_text(draw,ending_text,ef,W-lm-80)[:2]:
            draw.text((lm,ty),l,font=ef,fill=(140,140,160)); ty+=_tw(draw,l,ef)[1]+4
    ty+=20

    sl = load_font(FONT_REGULAR_PATH,16); sv = load_font(FONT_BOLD_PATH,20)
    w1,h1=_stat_badge(draw,"ВСЕГО",str(total_duties),lm,ty,accent,sl,sv)
    _stat_badge(draw,"СЛЕД.",f"{next_person} ({next_days}д)",lm+w1+14,ty,accent,sl,sv)

    ff = load_font(FONT_BOLD_PATH,14)
    draw.text((lm,H-50),f"{date_label}  ·  {mood_label}",font=ff,fill=(60,60,80))
    draw.text((lm,H-30),f"НАРЯД-БОТ  ×  {theme['title']}",font=ff,fill=(50,50,68))


LAYOUTS = [_layout_classic, _layout_split, _layout_cinematic, _layout_hud, _layout_poster]


# ══════════════════════════════════════════════════════════════════════════════
#  ПУБЛИЧНАЯ ФУНКЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

def make_reminder_card(
    duty_type, person, position_label, time_label,
    header_text, ending_text, date_label, mood_label,
    proc_day=1, next_person="", next_days=0, total_duties=0,
    theme_key=None, output_path="/tmp/card.jpg"
):
    if theme_key not in THEMES:
        theme_key = random.choice(THEME_KEYS)

    try:
        theme  = THEMES[theme_key]
        accent = theme["accent"]
        person       = _clean(person)
        header_text  = _clean(header_text)
        ending_text  = _clean(ending_text)
        mood_label   = _clean(mood_label)
        position_label = _clean(position_label)

        bg  = generate_background(theme_key, W, BG_H)
        img = _blend_bg(bg)

        draw   = ImageDraw.Draw(img)
        layout = random.choice(LAYOUTS)
        layout(
            draw, img, theme_key, accent, theme,
            person, position_label, time_label, header_text,
            ending_text, date_label, mood_label,
            duty_type, proc_day, next_person, next_days, total_duties
        )

        _draw_noise(img, amount=6)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "JPEG", quality=93)
        print(f"✅ Карточка сохранена: {output_path} ({theme_key}, {layout.__name__})", flush=True)
        return theme_key

    except Exception as e:
        print(f"❌ Ошибка карточки ({theme_key}): {e}", file=sys.stderr, flush=True)
        try:
            img2 = Image.new("RGB",(W,H),DARK)
            d2   = ImageDraw.Draw(img2)
            f2   = load_font(FONT_BOLD_PATH, 64)
            nb   = d2.textbbox((0,0),person or "Наряд",font=f2)
            d2.text(((W-(nb[2]-nb[0]))//2, H//2-80), person or "Наряд", font=f2, fill=WHITE)
            f3   = load_font(FONT_REGULAR_PATH,32)
            hb   = d2.textbbox((0,0),header_text or "",font=f3)
            d2.text(((W-(hb[2]-hb[0]))//2, H//2+20), header_text or "", font=f3, fill=(190,190,200))
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            img2.save(output_path, "JPEG", quality=90)
        except Exception:
            pass
        return theme_key


# ── Быстрый тест ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    for i, layout in enumerate(LAYOUTS):
        out = make_reminder_card(
            duty_type="svodki" if i%2==0 else "proc",
            person="Тестовый",
            position_label=f"Дежурный №{i+1} по сводкам",
            time_label="18:00",
            header_text="понедельник. сводки. страдания. погнали",
            ending_text="gg wp, увидимся на следующем напоминании",
            date_label="24.08.2026 • понедельник",
            mood_label="геймерский",
            proc_day=1, next_person="Глеб", next_days=3, total_duties=12,
            theme_key=THEME_KEYS[i % len(THEME_KEYS)],
            output_path=os.path.join(BASE_DIR,"temp",f"test_layout_{i}.jpg"),
        )
        print(f"Layout {i}: {layout.__name__} → {out}")
