"""
dembel_generator.py — генератор красивых карточек дембель-таймера (HUD v2).

Переиспользует шрифты, темы и эффекты из card_generator.py,
добавляя круговой HUD-индикатор прогресса вокруг главного счётчика дней,
стеклянные капсулы для табло ЧЧ : ММ : СС и градиентную шкалу выслуги.
"""

import math
import os
import random
from datetime import datetime, date, timedelta
from PIL import Image, ImageDraw, ImageFilter

from card_generator import (
    W, H, BG_H, MARGIN, WHITE, DARK,
    THEMES, THEME_KEYS,
    FONT_BOLD_PATH, FONT_REGULAR_PATH,
    load_font, generate_background, get_accent2, pick_style,
    _clean_for_image, _centered_x, _draw_centered, _wrap_text,
    _draw_outlined_text, _draw_glow_text, _draw_icon, _draw_panel_frame,
    _draw_noise, _draw_corner_decorations,
    _draw_neon_line, _draw_stat_badge, _badge_rect,
    _draw_glass_panel, _draw_gradient_bar, _draw_glow_circle,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ══════════════════════════════════════════════
#  ДАТЫ ДЕМБЕЛЯ — правь здесь
# ══════════════════════════════════════════════
DEMBEL_DATES = {
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

SERVICE_LENGTH_DAYS = 730


def _time_left(target: date, now: datetime | None = None) -> dict:
    """Считает, сколько осталось до target: дни/часы/минуты/секунды + флаг 'уже дома'."""
    now = now or datetime.now()
    target_dt = datetime.combine(target, datetime.min.time())
    delta = target_dt - now
    total_seconds = delta.total_seconds()

    if total_seconds <= 0:
        return {"done": True, "days": 0, "hours": 0, "minutes": 0, "seconds": 0, "total_seconds": 0}

    days = int(total_seconds // 86400)
    hours = int((total_seconds % 86400) // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)

    return {
        "done": False,
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "total_seconds": total_seconds,
    }


def _progress_ratio(target: date, now: datetime | None = None) -> float:
    """Оценка доли отслуженного времени (от 0.0 до 1.0)."""
    now = now or datetime.now()
    target_dt = datetime.combine(target, datetime.min.time())
    start_dt = target_dt - timedelta(days=SERVICE_LENGTH_DAYS)
    total = (target_dt - start_dt).total_seconds()
    passed = (now - start_dt).total_seconds()
    if total <= 0:
        return 1.0
    return max(0.0, min(1.0, passed / total))


def get_all_timers(now: datetime | None = None) -> list[dict]:
    """Возвращает список всех людей с посчитанным временем до дембеля, отсортированный по возрастанию."""
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


def _draw_circular_gauge(img, cx, cy, radius, progress, accent, accent2):
    """
    Рисует технологичное круговое кольцо-индикатор вокруг счётчика дней:
    фоновая шкала с делениями + яркая неоновая дуга текущего прогресса службы.
    """
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    # Внешнее тонкое кольцо с делениями
    outer_r = radius + 20
    for deg in range(0, 360, 6):
        rad = math.radians(deg - 90)
        is_major = (deg % 30 == 0)
        r_in = outer_r - (10 if is_major else 5)
        x1, y1 = cx + r_in * math.cos(rad), cy + r_in * math.sin(rad)
        x2, y2 = cx + outer_r * math.cos(rad), cy + outer_r * math.sin(rad)
        col = accent + (95,) if (deg / 360.0) <= progress else (120, 130, 160, 40)
        od.line([(x1, y1), (x2, y2)], fill=col, width=2 if is_major else 1)

    # Подложка основной дуги (с разрывом снизу под подпись "дней осталось")
    start_angle = 135
    sweep_total = 270
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    od.arc(bbox, start=start_angle, end=start_angle + sweep_total, fill=(30, 35, 52, 160), width=10)

    # Заполненная неоновая дуга прогресса
    prog_sweep = int(sweep_total * max(0.02, min(1.0, progress)))
    end_angle = start_angle + prog_sweep
    od.arc(
        [cx - radius - 2, cy - radius - 2, cx + radius + 2, cy + radius + 2],
        start=start_angle, end=end_angle, fill=accent2 + (90,), width=16,
    )
    od.arc(bbox, start=start_angle, end=end_angle, fill=accent + (240,), width=8)

    # Светящаяся точка на кончике дуги прогресса
    tip_rad = math.radians(end_angle)
    tx = int(cx + radius * math.cos(tip_rad))
    ty = int(cy + radius * math.sin(tip_rad))
    _draw_glow_circle(od, tx, ty, 24, accent2, max_alpha=130)
    od.ellipse([tx - 6, ty - 6, tx + 6, ty + 6], fill=WHITE + (255,))

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img.paste(img_rgba.convert("RGB"), (0, 0))


def _draw_timer_segments(draw, img, cx, y, segments, accent, val_font, label_font):
    """
    Рисует сегментированный таймер ЧЧ : ММ : СС в виде отдельных стеклянных
    капсул-плиток с неоновым свечением цифр.
    """
    accent2 = get_accent2(accent)
    card_w = 156
    card_h = 104
    gap = 38
    total_w = len(segments) * card_w + (len(segments) - 1) * gap
    start_x = cx - total_w // 2

    for i, (val, label) in enumerate(segments):
        bx0 = start_x + i * (card_w + gap)
        by0 = y
        bx1 = bx0 + card_w
        by1 = by0 + card_h

        _draw_glass_panel(
            img, (bx0, by0, bx1, by1),
            accent=accent if i % 2 == 0 else accent2,
            accent2=accent2,
            fill_alpha=200,
            radius=20,
            border_w=2,
        )
        draw = ImageDraw.Draw(img)

        vb = draw.textbbox((0, 0), val, font=val_font)
        vw, vh = vb[2] - vb[0], vb[3] - vb[1]
        vx = bx0 + (card_w - vw) // 2
        vy = by0 + 12
        _draw_glow_text(
            img, (vx, vy), val, val_font,
            fill=WHITE, glow_color=accent,
            blur_radius=7, glow_passes=1, outline_w=3,
        )
        draw = ImageDraw.Draw(img)

        lb = draw.textbbox((0, 0), label.upper(), font=label_font)
        lw = lb[2] - lb[0]
        draw.text(
            (bx0 + (card_w - lw) // 2, by1 - 28),
            label.upper(), font=label_font, fill=(165, 172, 195),
        )

        # Двоеточие между капсулами
        if i < len(segments) - 1:
            sep_x = bx1 + gap // 2
            sep_cy = by0 + card_h // 2
            for dy in (-12, 12):
                draw.ellipse([sep_x - 4, sep_cy + dy - 4, sep_x + 4, sep_cy + dy + 4], fill=accent)

    return total_w


def _plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ДЕНЬ ОСТАЛСЯ"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "ДНЯ ОСТАЛОСЬ"
    return "ДНЕЙ ОСТАЛОСЬ"


def make_dembel_card(person: str, output_path: str, theme_key: str | None = None, now: datetime | None = None) -> str:
    """
    Рисует карточку дембель-таймера для одного человека и сохраняет в output_path.
    Возвращает использованную theme_key.
    """
    if theme_key not in THEMES:
        theme_key = random.choice(THEME_KEYS)

    target = DEMBEL_DATES.get(person)
    if target is None:
        raise ValueError(f"Нет даты дембеля для {person!r}")

    tl = _time_left(target, now)
    progress = _progress_ratio(target, now)

    try:
        theme = THEMES[theme_key]
        accent = theme["accent"]
        accent2 = get_accent2(accent)
        style_key, style = pick_style()
        person_clean = _clean_for_image(person) or person

        # ── 1. Полноэкранный арт-фон с размытием под нижней панелью ──────
        full_bg = generate_background(theme_key, W, H, bg_style=random.choice(style["bg"]))
        blurred_bottom = full_bg.crop((0, BG_H + 40, W, H)).filter(ImageFilter.GaussianBlur(18))
        img = full_bg.copy()
        img.paste(blurred_bottom, (0, BG_H + 40))

        img_rgba = img.convert("RGBA")
        fade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fade)
        fade_start = BG_H - 120
        for y in range(fade_start, H):
            t = min(1.0, (y - fade_start) / 260.0)
            a = int(215 * (t ** 1.2))
            fd.line([(0, y), (W, y)], fill=(DARK[0], DARK[1], DARK[2], a))
        img_rgba.alpha_composite(fade)
        img = img_rgba.convert("RGB")

        # Нижняя стеклянная панель
        panel_top = BG_H + 75
        _draw_glass_panel(
            img,
            (MARGIN + 10, panel_top, W - MARGIN - 10, H - MARGIN - 42),
            accent=accent,
            accent2=accent2,
            fill_alpha=188,
            radius=30,
            cut=22 if style["badge"] == "cut" else 0,
            border_w=2,
        )

        draw = ImageDraw.Draw(img)
        _draw_corner_decorations(draw, accent, margin=26, style=style["corner"])
        if style["corner"] != "frame":
            _draw_panel_frame(draw, margin=16, accent=accent, cut=22, width=1)

        # ── 2. Верхняя шапка: бейдж темы + ранг в очереди ДМБ ────────────
        all_timers = get_all_timers(now)
        idx = next((i for i, r in enumerate(all_timers) if r["person"] == person), None)

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

        status_font = load_font(FONT_BOLD_PATH, 22)
        if tl["done"]:
            status_text = "СТАТУС: ДОМА"
        elif idx is not None:
            status_text = f"ДМБ #{idx + 1} ИЗ {len(all_timers)}"
        else:
            status_text = "ДМБ-ТАЙМЕР"
        sb = draw.textbbox((0, 0), status_text, font=status_font)
        sw = sb[2] - sb[0]
        _badge_rect(
            draw,
            (W - MARGIN - 10 - sw - pad_x * 2, top_y, W - MARGIN - 10, top_y + bh + pad_y * 2),
            accent2, shape=style["badge"], fill=(14, 16, 24), outline_w=2,
        )
        draw.text((W - MARGIN - 10 - sw - pad_x, top_y + pad_y - 2), status_text, font=status_font, fill=WHITE)

        line_y = top_y + bh + pad_y * 2 + 16
        _draw_neon_line(draw, MARGIN + 12, line_y, W - MARGIN - 12, line_y, accent, width=2, style=style["line"])

        # ── 3. Имя бойца ─────────────────────────────────────────────────
        for name_size in (86, 76, 66, 56):
            name_font = load_font(FONT_BOLD_PATH, name_size)
            nb = draw.textbbox((0, 0), person_clean, font=name_font)
            if nb[2] - nb[0] <= W - 160:
                break
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = (W - nw) // 2
        ny = line_y + 22
        _draw_glow_text(
            img, (nx, ny), person_clean, name_font,
            fill=WHITE, glow_color=accent,
            blur_radius=10, glow_passes=1, outline_w=4,
        )

        # ── 4. Круговой HUD-индикатор + Огромный счётчик дней ────────────
        gauge_cx = W // 2
        gauge_cy = ny + nh + 175
        gauge_r = 148
        _draw_circular_gauge(img, gauge_cx, gauge_cy, gauge_r, progress, accent, accent2)

        big_text = "ДОМА" if tl["done"] else str(tl["days"])
        max_big_w = gauge_r * 2 - 24 if not tl["done"] else W - 140
        for big_size in (185, 165, 145, 125, 105):
            big_font = load_font(FONT_BOLD_PATH, big_size)
            gb = ImageDraw.Draw(img).textbbox((0, 0), big_text, font=big_font)
            if gb[2] - gb[0] <= max_big_w:
                break
        gw, gh = gb[2] - gb[0], gb[3] - gb[1]
        gx = (W - gw) // 2
        gy = gauge_cy - gh // 2 - gb[1] - 12

        _draw_glow_text(
            img, (gx, gy), big_text, big_font,
            fill=accent, glow_color=accent2,
            blur_radius=15, glow_passes=2, outline_w=5,
            top_color=WHITE,
        )
        draw = ImageDraw.Draw(img)

        sub_font = load_font(FONT_BOLD_PATH, 22)
        sub_text = "СЛУЖБА ОКОНЧЕНА!" if tl["done"] else _plural_days(tl["days"])
        _draw_centered(draw, sub_text, sub_font, gauge_cy + gauge_r - 22, color=WHITE, shadow=True, so=2)

        # ── 5. Капсульное табло ЧЧ : ММ : СС ─────────────────────────────
        if not tl["done"]:
            seg_val_font = load_font(FONT_BOLD_PATH, 42)
            seg_label_font = load_font(FONT_BOLD_PATH, 15)
            segments = [
                (f"{tl['hours']:02d}", "часов"),
                (f"{tl['minutes']:02d}", "минут"),
                (f"{tl['seconds']:02d}", "секунд"),
            ]
            _draw_timer_segments(
                draw, img, W // 2, gauge_cy + gauge_r + 24,
                segments, accent, seg_val_font, seg_label_font,
            )
            draw = ImageDraw.Draw(img)

        # ── 6. Нижняя стеклянная панель: дата ДМБ и прогресс-бар ─────────
        text_y = panel_top + 28

        meta_font = load_font(FONT_BOLD_PATH, 24)
        date_str = target.strftime("%d.%m.%Y")
        meta_text = f"ПРИКАЗ / ДАТА ДМБ  ·  {date_str}"
        h = _draw_centered(draw, meta_text, meta_font, text_y, color=WHITE, shadow=True, so=2)
        text_y += h + 18

        _draw_neon_line(draw, MARGIN + 45, text_y, W - MARGIN - 45, text_y, accent, width=2, style=style["line"])
        text_y += 28

        # Градиентный прогресс-бар с процентом и счётчиком пройденных дней
        bar_x0 = 95
        bar_x1 = W - 95
        bar_h = 46
        _draw_gradient_bar(
            img, (bar_x0, text_y, bar_x1, text_y + bar_h),
            progress=progress, accent=accent, accent2=accent2,
        )
        draw = ImageDraw.Draw(img)

        pct_font = load_font(FONT_BOLD_PATH, 24)
        pct_text = f"{progress * 100:.1f}%"
        pt = draw.textbbox((0, 0), pct_text, font=pct_font)
        pw, ph = pt[2] - pt[0], pt[3] - pt[1]
        _draw_outlined_text(
            draw,
            ((W - pw) // 2, text_y + (bar_h - ph) // 2 - 2),
            pct_text, pct_font, fill=WHITE, outline_color=(0, 0, 0), outline_w=3,
        )

        passed_days = max(0, SERVICE_LENGTH_DAYS - tl["days"]) if not tl["done"] else SERVICE_LENGTH_DAYS
        cap_font = load_font(FONT_BOLD_PATH, 18)
        left_cap = f"ПОЗАДИ: {passed_days} ДН."
        right_cap = f"ОСТАЛОСЬ: {tl['days']} ДН."
        draw.text((bar_x0 + 8, text_y + bar_h + 12), left_cap, font=cap_font, fill=accent)
        rc_b = draw.textbbox((0, 0), right_cap, font=cap_font)
        draw.text((bar_x1 - (rc_b[2] - rc_b[0]) - 8, text_y + bar_h + 12), right_cap, font=cap_font, fill=accent2)
        text_y += bar_h + 56

        # ── 7. Соседи по очереди дембеля (симметричная сетка) ────────────
        if idx is not None:
            label_font = load_font(FONT_BOLD_PATH, 17)
            val_font = load_font(FONT_BOLD_PATH, 23)

            prev_r = all_timers[idx - 1] if idx > 0 else None
            next_r = all_timers[idx + 1] if idx < len(all_timers) - 1 else None

            grid_w = W - 190
            gap = 24
            col_w = (grid_w - gap) // 2
            start_x = (W - grid_w) // 2

            if prev_r and next_r:
                _draw_stat_badge(
                    draw,
                    label="УХОДИТ РАНЬШЕ",
                    value=f"{_clean_for_image(prev_r['person'])} ({prev_r['days']}д)",
                    x=start_x, y=text_y, accent=accent,
                    font_label=label_font, font_value=val_font,
                    shape=style["badge"], icon="chevron", min_w=col_w,
                )
                _draw_stat_badge(
                    draw,
                    label="СЛЕДУЮЩИЙ ЗА НИМ",
                    value=f"{_clean_for_image(next_r['person'])} ({next_r['days']}д)",
                    x=start_x + col_w + gap, y=text_y, accent=accent2,
                    font_label=label_font, font_value=val_font,
                    shape=style["badge"], icon="shield", min_w=col_w,
                )
            elif prev_r or next_r:
                single = prev_r or next_r
                lbl = "УХОДИТ РАНЬШЕ" if prev_r else "СЛЕДУЮЩИЙ ЗА НИМ"
                ico = "chevron" if prev_r else "shield"
                col = accent if prev_r else accent2
                _draw_stat_badge(
                    draw,
                    label=lbl,
                    value=f"{_clean_for_image(single['person'])} ({single['days']}д)",
                    x=(W - col_w) // 2, y=text_y, accent=col,
                    font_label=label_font, font_value=val_font,
                    shape=style["badge"], icon=ico, min_w=col_w,
                )

        # ── 8. Футер ──────────────────────────────────────────────────────
        foot_font = load_font(FONT_BOLD_PATH, 16)
        foot_text = f"◈ ДМБ-ТАЙМЕР  ×  {theme['title']}  ·  HUD v2.0 ◈"
        fx = _centered_x(draw, foot_text, foot_font)
        draw.text((fx, H - MARGIN - 24), foot_text, font=foot_font, fill=(95, 100, 125))

        _draw_noise(img, amount=6)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "JPEG", quality=94)
        return theme_key

    except Exception as e:
        import sys
        print(f"❌ Ошибка генерации дембель-карточки ({theme_key}): {e}", file=sys.stderr, flush=True)
        _save_fallback(output_path, person, tl)
        return theme_key


def _save_fallback(output_path: str, person: str, tl: dict):
    """Простая заглушка без спецэффектов — гарантированно рисуется."""
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
        out = make_dembel_card(
            p,
            output_path=os.path.join(BASE_DIR, "temp", f"dembel_{p}.jpg"),
            theme_key=random.choice(THEME_KEYS),
        )
        print(f"{p}: тема {out}")

