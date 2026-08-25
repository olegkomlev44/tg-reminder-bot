"""
dembel_generator.py — генератор красивых карточек дембель-таймера.

Переиспользует шрифты, темы и примитивы рисования из card_generator.py
(тот же визуальный стиль: неоновые линии, угловые декоры, glow-фон),
но рисует свою композицию — крупный счётчик дней вместо шапки наряда.

Список людей и дат дембеля — фиксированный, задаётся ниже в DEMBEL_DATES.
Чтобы поменять дату или добавить человека, правь этот словарь.
"""

import os
import random
import math
from datetime import datetime, date, timedelta
from PIL import Image, ImageDraw

from card_generator import (
    W, H, BG_H, MARGIN, WHITE, DARK,
    THEMES, THEME_KEYS,
    FONT_BOLD_PATH, FONT_REGULAR_PATH,
    load_font, generate_background,
    _clean_for_image, _centered_x, _draw_centered, _wrap_text,
    _draw_outlined_text, _draw_noise, _draw_corner_decorations,
    _draw_neon_line, _draw_stat_badge,
    _draw_glow_text, _lerp_color, _darken, _lighten,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ══════════════════════════════════════════════
#  ДАТЫ ДЕМБЕЛЯ — правь здесь
# ══════════════════════════════════════════════
DEMBEL_DATES = {
    "Игорь":   date(2026, 10, 30),
    "Дима":    date(2026, 10, 20),
    "Арсений": date(2027, 4, 18),
    "Олег":    date(2026, 11, 7),
    "Ильнар":  date(2026, 12, 15),
    "Максим":  date(2026, 12, 17),
}

SERVICE_LENGTH_DAYS = 730


# ══════════════════════════════════════════════
#  РАНГИ ПО ПРОГРЕССУ
# ══════════════════════════════════════════════
def _get_rank(progress: float) -> tuple[str, str]:
    """Возвращает (звание, эмодзи) в зависимости от прогресса службы."""
    if progress >= 1.0:
        return "ДЕМБЕЛЬ", "🏠"
    elif progress >= 0.85:
        return "ВЕТЕРАН", "🎖"
    elif progress >= 0.65:
        return "БОЕЦ", "⚔"
    elif progress >= 0.40:
        return "СОЛДАТ", "🛡"
    elif progress >= 0.20:
        return "РЕКРУТ", "🎒"
    else:
        return "НОВИЧОК", "👶"


def _time_left(target: date, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    target_dt = datetime.combine(target, datetime.min.time())
    delta = target_dt - now
    total_seconds = delta.total_seconds()

    if total_seconds <= 0:
        return {"done": True, "days": 0, "hours": 0, "minutes": 0, "total_seconds": 0}

    days = int(total_seconds // 86400)
    hours = int((total_seconds % 86400) // 3600)
    minutes = int((total_seconds % 3600) // 60)

    return {
        "done": False,
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "total_seconds": total_seconds,
    }


def _progress_ratio(target: date, now: datetime | None = None) -> float:
    now = now or datetime.now()
    target_dt = datetime.combine(target, datetime.min.time())
    start_dt = target_dt - timedelta(days=SERVICE_LENGTH_DAYS)
    total = (target_dt - start_dt).total_seconds()
    passed = (now - start_dt).total_seconds()
    if total <= 0:
        return 1.0
    return max(0.0, min(1.0, passed / total))


def get_all_timers(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    result = []
    for person, target in DEMBEL_DATES.items():
        tl = _time_left(target, now)
        result.append({
            "person": person,
            "target": target,
            **tl,
        })
    result.sort(key=lambda r: r["total_seconds"] if not r["done"] else -1)
    return result


# ══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЕМБЕЛЯ
# ══════════════════════════════════════════════
def _draw_circular_progress(draw, cx, cy, radius, progress, accent, bg_width=8, fg_width=12):
    """Круговой прогресс-бар вокруг центра."""
    # Фоновый круг
    for r in range(radius - bg_width // 2, radius + bg_width // 2 + 1):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(30, 30, 40), width=1)

    # Заполненная дуга
    if progress > 0:
        steps = int(360 * progress)
        for angle in range(-90, -90 + steps):
            rad = math.radians(angle)
            x1 = cx + (radius - fg_width // 2) * math.cos(rad)
            y1 = cy + (radius - fg_width // 2) * math.sin(rad)
            x2 = cx + (radius + fg_width // 2) * math.cos(rad)
            y2 = cy + (radius + fg_width // 2) * math.sin(rad)
            # Градиент по углу
            t = (angle + 90) / 360 if steps > 0 else 0
            col = _lerp_color(accent, _lighten(accent, 60), t)
            draw.line([(x1, y1), (x2, y2)], fill=col, width=2)

    # Glow-эффект на конце дуги
    if 0 < progress < 1:
        end_angle = math.radians(-90 + int(360 * progress))
        ex = cx + radius * math.cos(end_angle)
        ey = cy + radius * math.sin(end_angle)
        for gr in range(10, 0, -2):
            alpha = int(100 * (1 - gr / 10))
            draw.ellipse([ex - gr, ey - gr, ex + gr, ey + gr], fill=(*accent, alpha))


def _draw_milestone_dots(draw, cx, cy, radius, accent):
    """Точки-вехи на круговом прогрессе (25%, 50%, 75%)."""
    for pct in [0.25, 0.50, 0.75]:
        angle = math.radians(-90 + 360 * pct)
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        draw.ellipse([x - 5, y - 5, x + 5, y + 5], fill=accent, outline=WHITE, width=2)


def _draw_time_card(draw, x, y, value, label, accent, font_val, font_lbl):
    """Маленькая карточка для часов/минут."""
    vb = draw.textbbox((0, 0), value, font=font_val)
    lb = draw.textbbox((0, 0), label, font=font_lbl)
    vw, vh = vb[2] - vb[0], vb[3] - vb[1]
    lw, lh = lb[2] - lb[0], lb[3] - lb[1]
    w = max(vw, lw) + 24
    h = vh + lh + 20

    draw.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=(18, 18, 26), outline=accent, width=2)
    draw.text((x + (w - vw) // 2, y + 6), value, font=font_val, fill=WHITE)
    draw.text((x + (w - lw) // 2, y + 8 + vh), label, font=font_lbl, fill=(140, 140, 160))
    return w, h


def _draw_rank_badge(draw, cx, cy, rank, emoji, accent):
    """Бейдж звания под счётчиком."""
    font = load_font(FONT_BOLD_PATH, 26)
    text = f"{emoji}  {rank}"
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    pad = 14
    w, h = tw + pad * 2, th + pad * 2
    x, y = cx - w // 2, cy - h // 2

    draw.rounded_rectangle([x, y, x + w, y + h], radius=20, fill=(20, 20, 28), outline=accent, width=2)
    # Glow
    for r in range(8, 0, -2):
        alpha = int(40 * (1 - r / 8))
        draw.rounded_rectangle([x - r, y - r, x + w + r, y + h + r], radius=20 + r,
                               outline=(*accent, alpha), width=1)
    draw.text((x + pad, y + pad), text, font=font, fill=accent)
    return w, h


def make_dembel_card(person: str, output_path: str, theme_key: str | None = None, now: datetime | None = None) -> str:
    if theme_key not in THEMES:
        theme_key = random.choice(THEME_KEYS)

    target = DEMBEL_DATES.get(person)
    if target is None:
        raise ValueError(f"Нет даты дембеля для {person!r}")

    tl = _time_left(target, now)
    progress = _progress_ratio(target, now)
    rank, rank_emoji = _get_rank(progress)

    try:
        theme = THEMES[theme_key]
        accent = theme["accent"]
        person_clean = _clean_for_image(person)

        # ── 1. Фон ────────────────────────────────────────────────────────
        bg = generate_background(theme_key, W, BG_H)
        img = Image.new("RGB", (W, H), DARK)
        img.paste(bg, (0, 0))

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

        _draw_corner_decorations(draw, accent, margin=30)

        # ── 2. Верхний бейдж темы ────────────────────────────────────────
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

        status_font = load_font(FONT_BOLD_PATH, 24)
        status_text = "ДОМА" if tl["done"] else "ДМБ"
        sb = draw.textbbox((0, 0), status_text, font=status_font)
        sw = sb[2] - sb[0]
        draw.rectangle(
            [W - MARGIN - sw - 2 * pad, MARGIN - 6, W - MARGIN + pad, MARGIN + bh + 8],
            fill=(12, 12, 18), outline=accent, width=1
        )
        draw.text((W - MARGIN - sw - pad, MARGIN), status_text, font=status_font, fill=WHITE)

        line_y = MARGIN + bh + 18
        _draw_neon_line(draw, MARGIN, line_y, W - MARGIN, line_y, accent, width=1)

        # ── 3. Имя ────────────────────────────────────────────────────────
        for name_size in (110, 96, 84, 72, 60):
            name_font = load_font(FONT_BOLD_PATH, name_size)
            nb = draw.textbbox((0, 0), person_clean, font=name_font)
            if nb[2] - nb[0] <= W - 160:
                break
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = (W - nw) // 2
        ny = MARGIN + bh + 46
        _draw_outlined_text(draw, (nx, ny), person_clean, name_font,
                            fill=WHITE, outline_color=(0, 0, 0), outline_w=4)

        # ── 4. Круговой прогресс + Огромный счётчик дней ──────────────────
        center_x = W // 2
        center_y = ny + nh + 180
        circle_r = 160

        if not tl["done"]:
            _draw_circular_progress(draw, center_x, center_y, circle_r, progress, accent)
            _draw_milestone_dots(draw, center_x, center_y, circle_r, accent)

        if tl["done"]:
            big_text = "ДОМА"
        else:
            big_text = str(tl["days"])

        for big_size in (280, 240, 200, 170, 140):
            big_font = load_font(FONT_BOLD_PATH, big_size)
            gb = draw.textbbox((0, 0), big_text, font=big_font)
            if gb[2] - gb[0] <= W - 120:
                break
        gw, gh = gb[2] - gb[0], gb[3] - gb[1]
        gx = (W - gw) // 2
        gy = center_y - gh // 2 - 10

        # Glow под счётчик
        _draw_glow_text(draw, (gx, gy), big_text, big_font, fill=accent, glow_color=accent, glow_radius=16)

        sub_y = gy + gh + 20
        if not tl["done"]:
            sub_font = load_font(FONT_REGULAR_PATH, 30)
            d = tl["days"]
            if d % 10 == 1 and d % 100 != 11:
                sub_text = "день остался"
            elif d % 10 in (2, 3, 4) and d % 100 not in (12, 13, 14):
                sub_text = "дня осталось"
            else:
                sub_text = "дней осталось"
            _draw_centered(draw, sub_text, sub_font, sub_y, color=(200, 200, 215), shadow=True, so=2)
            sub_y += 40
        else:
            sub_font = load_font(FONT_REGULAR_PATH, 30)
            _draw_centered(draw, "уже дома!", sub_font, sub_y, color=(200, 200, 215), shadow=True, so=2)
            sub_y += 40

        # ── 5. Звание (rank badge) ───────────────────────────────────────
        rank_y = sub_y + 10
        _draw_rank_badge(draw, center_x, rank_y + 20, rank, rank_emoji, accent)
        sub_y = rank_y + 50

        # ── 6. Часы / минуты — карточки (только пока не дома) ─────────────
        if not tl["done"]:
            val_font = load_font(FONT_BOLD_PATH, 36)
            lbl_font = load_font(FONT_REGULAR_PATH, 18)
            card_w, card_h = _draw_time_card(draw, 0, 0, f"{tl['hours']:02d}", "часов", accent, val_font, lbl_font)

            total_cards_w = card_w * 2 + 20
            start_x = (W - total_cards_w) // 2

            _draw_time_card(draw, start_x, sub_y, f"{tl['hours']:02d}", "часов", accent, val_font, lbl_font)
            _draw_time_card(draw, start_x + card_w + 20, sub_y, f"{tl['minutes']:02d}", "минут", accent, val_font, lbl_font)
            sub_y += card_h + 24

        # ── 7. Нижняя панель: дата дембеля + прогресс-бар ────────────────
        text_y = BG_H + 30

        meta_font = load_font(FONT_REGULAR_PATH, 22)
        date_str = target.strftime("%d.%m.%Y")
        meta_text = f"дембель · {date_str}"
        h = _draw_centered(draw, meta_text, meta_font, text_y, color=(140, 140, 160), shadow=False)
        text_y += h + 18

        _draw_neon_line(draw, 60, text_y, W - 60, text_y, accent, width=2)
        text_y += 26

        # прогресс-бар службы (горизонтальный с градиентом)
        bar_w = W - 200
        bar_x = 100
        bar_h = 26
        draw.rounded_rectangle(
            [bar_x, text_y, bar_x + bar_w, text_y + bar_h],
            radius=bar_h // 2, fill=(22, 22, 30), outline=(60, 60, 75), width=1
        )
        filled_w = int(bar_w * progress)
        if filled_w > bar_h:
            for fx in range(bar_x, bar_x + filled_w):
                t = (fx - bar_x) / filled_w if filled_w > 0 else 0
                col = _lerp_color(accent, _lighten(accent, 80), t)
                draw.line([(fx, text_y + 2), (fx, text_y + bar_h - 2)], fill=col)
            draw.ellipse([bar_x + filled_w - bar_h, text_y, bar_x + filled_w, text_y + bar_h],
                         fill=_lighten(accent, 40))

        pct_font = load_font(FONT_BOLD_PATH, 16)
        pct_text = f"{int(progress * 100)}% службы позади"
        pt = draw.textbbox((0, 0), pct_text, font=pct_font)
        pw = pt[2] - pt[0]
        draw.text(((W - pw) // 2, text_y + bar_h + 10), pct_text, font=pct_font, fill=(160, 160, 180))
        text_y += bar_h + 46

        # ── 8. Остальные — краткая сводка ────────────────────────────────
        all_timers = get_all_timers(now)
        idx = next((i for i, r in enumerate(all_timers) if r["person"] == person), None)
        if idx is not None:
            label_font = load_font(FONT_REGULAR_PATH, 18)
            val_font = load_font(FONT_BOLD_PATH, 20)

            prev_r = all_timers[idx - 1] if idx > 0 else None
            next_r = all_timers[idx + 1] if idx < len(all_timers) - 1 else None

            badges_y = text_y
            x_cursor = 100
            if prev_r:
                w1, h1 = _draw_stat_badge(
                    draw, label="РАНЬШЕ УХОДИТ", value=f"{prev_r['person']} ({prev_r['days']}д)",
                    x=x_cursor, y=badges_y, accent=accent,
                    font_label=label_font, font_value=val_font
                )
                x_cursor += w1 + 20
            if next_r:
                _draw_stat_badge(
                    draw, label="СЛЕДУЮЩИЙ", value=f"{next_r['person']} ({next_r['days']}д)",
                    x=x_cursor, y=badges_y, accent=accent,
                    font_label=label_font, font_value=val_font
                )

        # ── 9. Футер ──────────────────────────────────────────────────────
        foot_font = load_font(FONT_BOLD_PATH, 16)
        foot_text = f"◈ ДМБ-ТАЙМЕР  ×  {theme['title']} ◈"
        fx = _centered_x(draw, foot_text, foot_font)
        draw.text((fx, H - MARGIN - 22), foot_text, font=foot_font, fill=(55, 55, 70))

        _draw_noise(img, amount=7)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "JPEG", quality=92)
        return theme_key

    except Exception as e:
        import sys
        print(f"❌ Ошибка генерации дембель-карточки ({theme_key}): {e}", file=sys.stderr, flush=True)
        _save_fallback(output_path, person, tl)
        return theme_key


def _save_fallback(output_path: str, person: str, tl: dict):
    """Простая заглушка без anchor/спецшрифтов — гарантированно рисуется."""
    img = Image.new("RGB", (W, H), DARK)
    draw = ImageDraw.Draw(img)
    font_big = load_font(FONT_BOLD_PATH, 100)
    font_mid = load_font(FONT_BOLD_PATH, 56)
    font_small = load_font(FONT_REGULAR_PATH, 28)

    name = _clean_for_image(person)
    nb = draw.textbbox((0, 0), name, font=font_mid)
    draw.text(((W - (nb[2] - nb[0])) // 2, H // 2 - 220), name, font=font_mid, fill=WHITE)

    big_text = "ДОМА" if tl.get("done") else str(tl.get("days", "?"))
    gb = draw.textbbox((0, 0), big_text, font=font_big)
    draw.text(((W - (gb[2] - gb[0])) // 2, H // 2 - 100), big_text, font=font_big, fill=(255, 178, 40))

    if not tl.get("done"):
        sub = "дней осталось"
        sb = draw.textbbox((0, 0), sub, font=font_small)
        draw.text(((W - (sb[2] - sb[0])) // 2, H // 2 + 60), sub, font=font_small, fill=(190, 190, 200))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    img.save(output_path, "JPEG", quality=90)


if __name__ == "__main__":
    for p in DEMBEL_DATES:
        out = make_dembel_card(p, output_path=os.path.join(BASE_DIR, "temp", f"dembel_{p}.jpg"),
                                theme_key=random.choice(THEME_KEYS))
        print(f"{p}: тема {out}")
