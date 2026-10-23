"""
pick_cards.py — картинки для /pick: диплом победителя (PNG) и колесо фортуны (GIF).

Использует шрифты и вспомогательные функции из card_generator.py (Pillow, кириллица).
Эмодзи на картинках не рисуются (в TTF их нет) — только векторные фигуры.
Функции синхронные и тяжёлые — вызывать через asyncio.to_thread().
"""

import io
import math
import random
import colorsys
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter

import card_generator as cg

GOLD = (255, 204, 64)
GOLD_DARK = (150, 100, 14)
SILVER = (205, 212, 224)
BRONZE = (214, 134, 70)
STEEL = (112, 124, 150)
_GOLD_THEMES = ["witcher", "eldenring", "cs2", "gta", "starwars", "stardew"]


# ── утилиты ───────────────────────────────────────────────────────────────────
def _font(bold: bool, size: int):
    return cg.load_font(cg.FONT_BOLD_PATH if bold else cg.FONT_REGULAR_PATH, size)


def _txt(s, limit=80) -> str:
    s = cg._clean_for_image(s) or "—"
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _fit(draw, text, bold, max_w, start, min_size=18):
    size = start
    while size > min_size:
        f = _font(bold, size)
        bb = draw.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
        size -= 2
    return _font(bold, min_size)


def _wrap2(draw, text, bold, max_w, start, min_size=26):
    """Имя в одну строку; если не влезло даже на min_size — переносим на две."""
    f = _fit(draw, text, bold, max_w, start, min_size)
    bb = draw.textbbox((0, 0), text, font=f)
    if bb[2] - bb[0] <= max_w or " " not in text:
        return [text], f
    words = text.split()
    best = (text, "")
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if abs(len(a) - len(b)) < abs(len(best[0]) - len(best[1])) or best[1] == "":
            best = (a, b)
    f2 = _fit(draw, max(best, key=len), bold, max_w, start, 20)
    return [best[0], best[1]], f2


def _center_text(img, y, text, font, fill, glow=False, shadow=True):
    d = ImageDraw.Draw(img)
    bb = d.textbbox((0, 0), text, font=font)
    x = (img.width - (bb[2] - bb[0])) // 2 - bb[0]
    if glow:
        cg._draw_glow_text(img, (x, y), text, font, fill, blur_radius=14, outline_w=3)
    else:
        if shadow:
            d.text((x + 3, y + 3), text, font=font, fill=(0, 0, 0))
        d.text((x, y), text, font=font, fill=fill)
    return bb[3] - bb[1]


def _star(draw, cx, cy, r, fill):
    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rr = r if i % 2 == 0 else r * 0.28
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    draw.polygon(pts, fill=fill)


def _crown(draw, cx, top, w=230, h=120):
    x0, x1 = cx - w // 2, cx + w // 2
    base_y = top + h
    pts = [(x0, base_y), (x0 - 6, top + 18), (x0 + w * 0.22, top + h * 0.5), (cx - w * 0.12, top + 6),
           (cx, top + h * 0.45), (cx + w * 0.12, top + 6), (x1 - w * 0.22, top + h * 0.5),
           (x1 + 6, top + 18), (x1, base_y)]
    draw.polygon(pts, fill=GOLD, outline=GOLD_DARK)
    draw.rounded_rectangle([x0, base_y - 20, x1, base_y + 10], radius=8, fill=(255, 224, 120), outline=GOLD_DARK, width=3)
    for gx, col in ((x0 + 28, (230, 60, 80)), (cx, (70, 150, 255)), (x1 - 28, (60, 210, 120))):
        draw.ellipse([gx - 9, base_y - 14, gx + 9, base_y + 4], fill=col, outline=(255, 255, 255))
    for px, py in ((x0 - 6, top + 18), (cx, top + 6), (x1 + 6, top + 18)):
        draw.ellipse([px - 8, py - 8, px + 8, py + 8], fill=(255, 240, 170), outline=GOLD_DARK, width=2)


def _background(w, h, rnd: random.Random):
    theme = rnd.choice(_GOLD_THEMES)
    try:
        return cg.generate_background(theme, w, h).convert("RGB"), cg.THEMES[theme]["accent"]
    except Exception:
        img = Image.new("RGB", (w, h), (18, 20, 34))
        return img, GOLD


def _borders(img):
    d = ImageDraw.Draw(img)
    w, h = img.size
    d.rounded_rectangle([26, 26, w - 26, h - 26], radius=34, outline=GOLD, width=6)
    d.rounded_rectangle([46, 46, w - 46, h - 46], radius=26, outline=GOLD_DARK, width=2)
    for x, y in ((26, 26), (w - 26, 26), (26, h - 26), (w - 26, h - 26)):
        d.ellipse([x - 12, y - 12, x + 12, y + 12], fill=GOLD, outline=GOLD_DARK, width=3)


def _sparkles(img, rnd, n=26):
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    w, h = img.size
    for _ in range(n):
        x, y = rnd.randint(70, w - 70), rnd.randint(70, h - 70)
        _star(d, x, y, rnd.randint(6, 18), (255, 236, 160, rnd.randint(70, 190)))
    base = img.convert("RGBA")
    base.alpha_composite(ov)
    img.paste(base.convert("RGB"))


def _footer(img, n, commit, label, y):
    parts = [f"шанс 1 из {n}" if n else "", cg._clean_for_image(label), f"commit {commit[:12]}" if commit else "",
             datetime.now().strftime("%d.%m.%Y")]
    line = "  ·  ".join(p for p in parts if p)
    d = ImageDraw.Draw(img)
    f = _fit(d, line, False, img.width - 140, 26, 14)
    _center_text(img, y, line, f, (205, 210, 225), shadow=False)


# ── диплом / доска итогов ─────────────────────────────────────────────────────
def make_pick_card(title: str, winners: list[str], n: int = 0, commit: str = "",
                   label: str = "", ) -> bytes:
    """PNG-диплом (1 победитель) или доска итогов (несколько мест)."""
    rnd = random.Random(commit or "pick")
    k = len(winners)
    W = 1200
    H = 860 if k == 1 else min(1500, 400 + 96 * k)
    img, accent = _background(W, H, rnd)
    _sparkles(img, rnd)
    cg._draw_glass_panel(img, (70, 70, W - 70, H - 70), accent, fill_alpha=120, radius=30, border_w=0)
    _borders(img)
    d = ImageDraw.Draw(img)
    clean_title = _txt(title, 70) if title else ""

    if k == 1:
        _crown(d, W // 2, 92)
        _center_text(img, 235, "ДИПЛОМ", _font(True, 104), GOLD, glow=True)
        _center_text(img, 362, "П О Б Е Д И Т Е Л Ю   Ж Р Е Б И Я", _font(False, 32), (230, 232, 245), shadow=False)
        cg._draw_glass_panel(img, (110, 430, W - 110, 650), accent, fill_alpha=170, radius=26, border_w=2)
        d = ImageDraw.Draw(img)
        lines, f = _wrap2(d, _txt(winners[0], 60), True, W - 300, 92, 40)
        total_h = sum(d.textbbox((0, 0), ln, font=f)[3] for ln in lines) + 10 * (len(lines) - 1)
        y = 430 + (220 - total_h) // 2 - 6
        for ln in lines:
            hgt = _center_text(img, y, ln, f, (255, 255, 255), glow=True)
            y += hgt + 18
        if clean_title:
            d = ImageDraw.Draw(img)
            ft = _fit(d, f"«{clean_title}»", False, W - 260, 40, 20)
            _center_text(img, 684, f"«{clean_title}»", ft, GOLD, shadow=False)
        _footer(img, n, commit, label, H - 112)
    else:
        _center_text(img, 92, "ИТОГИ ЖРЕБИЯ", _font(True, 74), GOLD, glow=True)
        y0 = 190
        if clean_title:
            d = ImageDraw.Draw(img)
            ft = _fit(d, f"«{clean_title}»", False, W - 260, 38, 20)
            _center_text(img, 190, f"«{clean_title}»", ft, (235, 238, 250), shadow=False)
            y0 = 256
        medal = [GOLD, SILVER, BRONZE]
        for i, w_ in enumerate(winners):
            top = y0 + i * 92
            cg._draw_glass_panel(img, (110, top, W - 110, top + 80), accent, fill_alpha=175, radius=22, border_w=2)
            d = ImageDraw.Draw(img)
            col = medal[i] if i < 3 else STEEL
            d.ellipse([132, top + 10, 192, top + 70], fill=col, outline=(255, 255, 255), width=3)
            nf = _font(True, 36)
            num = str(i + 1)
            bb = d.textbbox((0, 0), num, font=nf)
            d.text((162 - (bb[2] - bb[0]) / 2 - bb[0], top + 40 - (bb[3] - bb[1]) / 2 - bb[1]), num, font=nf, fill=(20, 20, 30))
            f = _fit(d, _txt(w_, 50), True, W - 420, 46, 22)
            d.text((222, top + 40 - d.textbbox((0, 0), "Ag", font=f)[3] / 2 - 2), _txt(w_, 50), font=f,
                   fill=(255, 255, 255) if i else (255, 240, 190))
        _footer(img, n, commit, label, H - 118)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ── колесо фортуны (GIF) ──────────────────────────────────────────────────────
def _sector_color(i, n):
    hue = (i / max(n, 1)) % 1.0
    s, v = (0.62, 0.92) if i % 2 == 0 else (0.78, 0.74)
    r, g, b = colorsys.hsv_to_rgb(hue, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _build_wheel(options: list[str], size: int) -> Image.Image:
    """Статичное колесо (RGBA). Сектор 0 начинается сверху и идёт по часовой стрелке."""
    S = size * 2                           # суперсэмплинг
    R = S // 2
    wheel = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(wheel)
    n = len(options)
    step = 360 / n
    d.ellipse([2, 2, S - 3, S - 3], fill=(30, 26, 40, 255))
    rim = int(R * 0.08)
    box = [rim, rim, S - rim, S - rim]
    for i in range(n):
        a0 = i * step - 90
        d.pieslice(box, a0, a0 + step, fill=_sector_color(i, n) + (255,), outline=(255, 255, 255, 255), width=3)

    # подписи
    avail = R * 0.64
    r_end = R * 0.90
    for i, opt in enumerate(options):
        mid = (i + 0.5) * step
        arc = 2 * math.pi * (R * 0.6) / n
        fs = int(max(16, min(64, arc * 0.62)))
        text = _txt(opt, 22)
        f = _font(True, fs)
        while fs > 16:
            bb = d.textbbox((0, 0), text, font=f)
            if bb[2] - bb[0] <= avail:
                break
            fs -= 2
            f = _font(True, fs)
        bb = d.textbbox((0, 0), text, font=f)
        while bb[2] - bb[0] > avail and len(text) > 3:
            text = text[:-2] + "…"
            bb = d.textbbox((0, 0), text, font=f)
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        flip = 180 < mid < 360                     # левая половина: текст «вверх ногами» → разворачиваем
        tx = (R - r_end - bb[0]) if flip else (R + r_end - tw - bb[0])
        ld.text((tx, R - th / 2 - bb[1]), text, font=f, fill=(255, 255, 255, 255),
                stroke_width=max(2, fs // 14), stroke_fill=(15, 12, 25, 255))
        layer = layer.rotate(90 - mid - (180 if flip else 0), resample=Image.BICUBIC, center=(R, R))
        wheel.alpha_composite(layer)

    # ободок с лампочками
    for k in range(n * 2 if n * 2 >= 16 else 16):
        ang = 2 * math.pi * k / (n * 2 if n * 2 >= 16 else 16)
        cx, cy = R + (R - rim / 2) * math.sin(ang), R - (R - rim / 2) * math.cos(ang)
        col = (255, 226, 120) if k % 2 == 0 else (255, 255, 255)
        d.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], fill=col + (255,))
    d.ellipse([2, 2, S - 3, S - 3], outline=GOLD + (255,), width=10)
    # хаб
    hr = int(R * 0.14)
    d.ellipse([R - hr, R - hr, R + hr, R + hr], fill=(40, 34, 56, 255), outline=GOLD + (255,), width=8)
    _star(d, R, R, hr * 0.7, GOLD + (255,))
    return wheel.resize((size, size), Image.LANCZOS)


def _pointer(canvas: Image.Image, cx: int, tip_y: int, tilt: float):
    ov = Image.new("RGBA", (120, 100), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.polygon([(60 - 20, 6), (60 + 20, 6), (60, 78)], fill=GOLD + (255,), outline=(255, 255, 255, 255))
    od.line([(60 - 20, 6), (60 + 20, 6), (60, 78), (60 - 20, 6)], fill=(120, 70, 8, 255), width=3)
    od.ellipse([60 - 9, 10, 60 + 9, 28], fill=(220, 40, 60, 255), outline=(255, 255, 255, 255), width=2)
    ov = ov.rotate(tilt, resample=Image.BICUBIC, center=(60, 6))
    canvas.alpha_composite(ov, (cx - 60, tip_y - 78))


def make_wheel_gif(options: list[str], winner_idx: int, title: str = "", commit: str = "") -> bytes:
    """Анимированное колесо: быстрый разгон, плавное торможение, тиканье указателя, баннер победителя."""
    n = len(options)
    CW, CH = 480, 610
    WD = 430
    wheel = _build_wheel(options, WD)
    step = 360 / n
    mid = (winner_idx + 0.5) * step
    jitter = random.uniform(-0.32, 0.32) * step              # не строго по центру сектора
    final = 360 * 4 + (360 - mid) + jitter
    cx, cy = CW // 2, 118 + WD // 2

    bg = Image.new("RGBA", (CW, CH), (16, 14, 26, 255))
    bd = ImageDraw.Draw(bg)
    for y in range(CH):                                       # лёгкий вертикальный градиент
        t = y / CH
        bd.line([(0, y), (CW, y)], fill=(int(16 + 22 * t), int(14 + 10 * t), int(26 + 34 * t), 255))
    if title:
        tf = _fit(bd, _txt(title, 60), True, CW - 40, 30, 16)
        bb = bd.textbbox((0, 0), _txt(title, 60), font=tf)
        bd.text(((CW - (bb[2] - bb[0])) / 2 - bb[0], 16), _txt(title, 60), font=tf, fill=(255, 236, 170))
    wheel_shadow = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    ImageDraw.Draw(wheel_shadow).ellipse([cx - WD // 2 - 6, cy - WD // 2 + 6, cx + WD // 2 + 6, cy + WD // 2 + 14], fill=(0, 0, 0, 120))
    bg.alpha_composite(wheel_shadow.filter(ImageFilter.GaussianBlur(8)))

    F = 64
    frames, durations = [], []
    for fi in range(F + 1):
        t = fi / F
        ease = 1 - (1 - t) ** 3.2
        ang = final * ease
        rot = wheel.rotate(-ang, resample=Image.BICUBIC)
        frame = bg.copy()
        frame.alpha_composite(rot, (cx - WD // 2, cy - WD // 2))
        phase = (ang % step) / step                           # «тик» при проходе границы сектора
        tilt = 26 * (1 - phase * 6) if (phase < 1 / 6 and t < 0.985) else 0
        _pointer(frame, cx, cy - WD // 2 + 20, tilt)
        frames.append(frame)
        durations.append(40 if fi < F * 0.55 else 55 + int(90 * ((fi / F - 0.55) / 0.45) ** 2))

    # финальный кадр с баннером
    last = frames[-1].copy()
    name = _txt(options[winner_idx], 32)
    ld = ImageDraw.Draw(last)
    nf = _fit(ld, name, True, CW - 80, 44, 20)
    ov = Image.new("RGBA", (CW, 64), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle([20, 4, CW - 20, 60], radius=18, fill=(20, 16, 30, 235), outline=GOLD + (255,), width=3)
    last.alpha_composite(ov, (0, CH - 78))
    ld = ImageDraw.Draw(last)
    bb = ld.textbbox((0, 0), name, font=nf)
    ld.text(((CW - (bb[2] - bb[0])) / 2 - bb[0], CH - 78 + 32 - (bb[3] - bb[1]) / 2 - bb[1]), name, font=nf, fill=(255, 255, 255),
            stroke_width=2, stroke_fill=(0, 0, 0))
    frames.append(last)
    durations.append(4000)

    pal = [f.convert("RGB").quantize(colors=96, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE) for f in frames]
    buf = io.BytesIO()
    pal[0].save(buf, "GIF", save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=False, disposal=1)
    return buf.getvalue()
