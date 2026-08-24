"""
dembel_generator.py — генератор карточек дембель-таймера v2.
3 лейаута: countdown, poster, progress.
"""

import math
import os
import random
from datetime import date, datetime, timedelta
from PIL import Image, ImageDraw

from card_generator import (
    W, H, BG_H, MARGIN, WHITE, DARK,
    THEMES, THEME_KEYS,
    FONT_BOLD_PATH, FONT_REGULAR_PATH,
    load_font, generate_background, _blend_bg,
    _clean, _centered_x, _draw_centered, _wrap_text,
    _draw_outlined, _draw_noise, _corner_marks,
    _neon_line, _stat_badge, _tw,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Даты дембеля ──────────────────────────────────────────────────────────────
DEMBEL_DATES = {
    "Игорь":   date(2026, 10, 30),
    "Дима":    date(2026, 10, 20),
    "Арсений": date(2027, 4,  18),
    "Денис": date(2027, 4,  18),
    "Олег":    date(2026, 11, 7),
    "Ильнар":  date(2026, 12, 15),
    "Руслан":  date(2026, 12, 15),
    "Максим":  date(2026, 12, 17),
}
SERVICE_LENGTH_DAYS = 730


# ── Вычисление времени ────────────────────────────────────────────────────────
def _time_left(target: date, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    delta = datetime.combine(target, datetime.min.time()) - now
    s = delta.total_seconds()
    if s <= 0:
        return {"done": True, "days": 0, "hours": 0, "minutes": 0, "total_seconds": 0}
    return {
        "done": False,
        "days": int(s // 86400),
        "hours": int((s % 86400) // 3600),
        "minutes": int((s % 3600) // 60),
        "total_seconds": s,
    }

def _progress(target: date, now: datetime | None = None) -> float:
    now = now or datetime.now()
    target_dt = datetime.combine(target, datetime.min.time())
    start_dt  = target_dt - timedelta(days=SERVICE_LENGTH_DAYS)
    total = (target_dt - start_dt).total_seconds()
    passed = (now - start_dt).total_seconds()
    return max(0.0, min(1.0, passed / total)) if total > 0 else 1.0

def get_all_timers(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    result = []
    for person, target in DEMBEL_DATES.items():
        tl = _time_left(target, now)
        result.append({"person": person, "target": target, **tl})
    result.sort(key=lambda r: r["total_seconds"] if not r["done"] else -1)
    return result

def _days_word(days: int) -> str:
    if days % 100 in (11,12,13,14): return "дней"
    if days % 10 == 1: return "день"
    if days % 10 in (2,3,4): return "дня"
    return "дней"


# ══════════════════════════════════════════════════════════════════════════════
#  3 ЛЕЙАУТА ДЕМБЕЛЯ
# ══════════════════════════════════════════════════════════════════════════════

def _dembel_countdown(draw, img, accent, theme, person, tl, progress_ratio, target, all_timers):
    """Классический: огромный счётчик дней по центру."""
    _corner_marks(draw, accent)

    # верхний бейдж
    bf = load_font(FONT_BOLD_PATH, 24)
    btx = f"◈ {theme['title']} ◈"
    bw, bh = _tw(draw, btx, bf)
    draw.rectangle([MARGIN-10, MARGIN-6, MARGIN+bw+10, MARGIN+bh+8],
                   fill=(12,12,18), outline=accent, width=1)
    draw.text((MARGIN, MARGIN), btx, font=bf, fill=accent)
    stxt = "ДОМА" if tl["done"] else "ДМБ"
    sw, _ = _tw(draw, stxt, bf)
    draw.rectangle([W-MARGIN-sw-20, MARGIN-6, W-MARGIN+10, MARGIN+bh+8],
                   fill=(12,12,18), outline=accent, width=1)
    draw.text((W-MARGIN-sw-10, MARGIN), stxt, font=bf, fill=WHITE)

    ly = MARGIN + bh + 18
    _neon_line(draw, MARGIN, ly, W-MARGIN, ly, accent)

    # имя
    for s in (110,96,84,72,60):
        nf = load_font(FONT_BOLD_PATH, s)
        nw, nh = _tw(draw, person, nf)
        if nw <= W-160: break
    nx = (W-nw)//2; ny = MARGIN+bh+46
    _draw_outlined(draw, (nx,ny), person, nf, WHITE, ow=4)

    # ОГРОМНЫЙ счётчик
    big = "ДОМА" if tl["done"] else str(tl["days"])
    for s in (280,240,200,170,140):
        bgf = load_font(FONT_BOLD_PATH, s)
        gw, gh = _tw(draw, big, bgf)
        if gw <= W-120: break
    gx = (W-gw)//2; gy = ny+nh+30
    _draw_outlined(draw, (gx,gy), big, bgf, accent, ow=6)

    sub_y = gy+gh+8
    sf = load_font(FONT_REGULAR_PATH, 30)
    if tl["done"]:
        _draw_centered(draw,"уже дома!",sf,sub_y,color=(200,200,215),shadow=True)
        sub_y += 44
    else:
        _draw_centered(draw, _days_word(tl["days"])+" осталось", sf, sub_y,
                       color=(200,200,215), shadow=True); sub_y += 44
        # часы:минуты
        hmf = load_font(FONT_BOLD_PATH, 34)
        hmt = f"{tl['hours']:02d} ч  {tl['minutes']:02d} мин"
        hmw, hmh = _tw(draw, hmt, hmf)
        draw.rectangle([(W-hmw)//2-20, sub_y-4, (W+hmw)//2+20, sub_y+hmh+12],
                       fill=(16,16,22), outline=accent, width=1)
        draw.text(((W-hmw)//2, sub_y+2), hmt, font=hmf, fill=WHITE)
        sub_y += hmh+24

    # нижняя панель
    ty = BG_H+30
    mf = load_font(FONT_REGULAR_PATH,22)
    date_str = target.strftime("%d.%m.%Y")
    h = _draw_centered(draw, f"дембель · {date_str}", mf, ty,
                       color=(140,140,160), shadow=False); ty += h+18
    _neon_line(draw, 60, ty, W-60, ty, accent, width=2); ty += 26

    # прогресс-бар
    bw2 = W-200; bx = 100; bh2 = 26
    draw.rounded_rectangle([bx,ty,bx+bw2,ty+bh2], radius=13,
                            fill=(22,22,30), outline=(60,60,75), width=1)
    fw = int(bw2*progress_ratio)
    if fw > bh2:
        draw.rounded_rectangle([bx,ty,bx+fw,ty+bh2], radius=13, fill=accent)
    pf2 = load_font(FONT_BOLD_PATH,16)
    pt  = f"{int(progress_ratio*100)}% службы позади"
    pw,_ = _tw(draw,pt,pf2)
    draw.text(((W-pw)//2, ty+bh2+10), pt, font=pf2, fill=(160,160,180))
    ty += bh2+48

    # соседи
    idx  = next((i for i,r in enumerate(all_timers) if r["person"]==person), None)
    if idx is not None:
        lf = load_font(FONT_REGULAR_PATH,18); vf = load_font(FONT_BOLD_PATH,20)
        xc = 100
        prev = all_timers[idx-1] if idx>0 else None
        nxt  = all_timers[idx+1] if idx<len(all_timers)-1 else None
        if prev:
            w1,h1 = _stat_badge(draw,"РАНЬШЕ УХОДИТ",
                                 f"{prev['person']} ({prev['days']}д)",xc,ty,accent,lf,vf)
            xc += w1+20
        if nxt:
            _stat_badge(draw,"СЛЕДУЮЩИЙ",f"{nxt['person']} ({nxt['days']}д)",xc,ty,accent,lf,vf)

    ff = load_font(FONT_BOLD_PATH,16)
    ft = f"◈ ДМБ-ТАЙМЕР  ×  {theme['title']} ◈"
    draw.text((_centered_x(draw,ft,ff), H-MARGIN-22), ft, font=ff, fill=(55,55,70))


def _dembel_poster(draw, img, accent, theme, person, tl, progress_ratio, target, all_timers):
    """Постер-стиль: крупная типографика, вертикальная линия слева."""
    _corner_marks(draw, accent, margin=28)
    _neon_line(draw, 60, 60, 60, H-60, accent, width=2)

    lm = 90; ty = 70
    bf = load_font(FONT_BOLD_PATH,18)
    draw.text((lm,ty), theme["title"].upper(), font=bf, fill=accent); ty+=36
    _neon_line(draw, lm, ty, W-60, ty, accent, width=1); ty+=28

    # ДЕМБЕЛЬ большим текстом
    for s in (130,110,90,76):
        dtf = load_font(FONT_BOLD_PATH, s)
        dtw, dth = _tw(draw,"ДЕМБЕЛЬ",dtf)
        if dtw <= W-lm-60: break
    _draw_outlined(draw,(lm,ty),"ДЕМБЕЛЬ",dtf,accent,ow=5); ty+=dth+10

    # имя
    for s in (90,76,64,52):
        nf = load_font(FONT_BOLD_PATH,s)
        nw,nh = _tw(draw,person,nf)
        if nw <= W-lm-60: break
    _draw_outlined(draw,(lm,ty),person,nf,WHITE,ow=4); ty+=nh+18
    _neon_line(draw,lm,ty,W-60,ty,accent,width=1); ty+=22

    # счётчик
    if tl["done"]:
        big="ДОМА"; big_color=accent
    else:
        big=str(tl["days"]); big_color=WHITE
    for s in (160,140,120,100):
        bgf=load_font(FONT_BOLD_PATH,s)
        gw,gh=_tw(draw,big,bgf)
        if gw<=W-lm-60: break
    _draw_outlined(draw,(lm,ty),big,bgf,big_color,ow=5); ty+=gh+4

    if not tl["done"]:
        wdf=load_font(FONT_REGULAR_PATH,32)
        draw.text((lm,ty),_days_word(tl["days"])+" осталось",font=wdf,fill=(180,180,200)); ty+=44
        hmf=load_font(FONT_BOLD_PATH,30)
        draw.text((lm,ty),f"{tl['hours']:02d} ч  {tl['minutes']:02d} мин",font=hmf,fill=accent); ty+=46

    _neon_line(draw,lm,ty,W-60,ty,accent,width=1); ty+=22

    # дата
    df3=load_font(FONT_BOLD_PATH,32)
    draw.text((lm,ty),f"📅 {target.strftime('%d.%m.%Y')}",font=df3,fill=WHITE); ty+=50

    # прогресс текстовый
    filled=round(progress_ratio*20)
    pbar_f=load_font(FONT_BOLD_PATH,24)
    draw.text((lm,ty),f"{'▓'*filled}{'░'*(20-filled)}  {int(progress_ratio*100)}%",
              font=pbar_f,fill=accent); ty+=44

    # соседи
    idx=next((i for i,r in enumerate(all_timers) if r["person"]==person),None)
    if idx is not None:
        lf=load_font(FONT_REGULAR_PATH,16); vf=load_font(FONT_BOLD_PATH,18)
        xc=lm
        if idx>0:
            w1,_=_stat_badge(draw,"РАНЬШЕ",f"{all_timers[idx-1]['person']}",xc,ty,accent,lf,vf)
            xc+=w1+14
        if idx<len(all_timers)-1:
            _stat_badge(draw,"ПОСЛЕ",f"{all_timers[idx+1]['person']}",xc,ty,accent,lf,vf)

    ff=load_font(FONT_BOLD_PATH,14)
    draw.text((lm,H-50),f"ДМБ-ТАЙМЕР  ×  {theme['title']}",font=ff,fill=(60,60,80))
    draw.text((lm,H-30),f"{target.strftime('%d.%m.%Y')} — день икс",font=ff,fill=(50,50,68))


def _dembel_progress(draw, img, accent, theme, person, tl, progress_ratio, target, all_timers):
    """Progress-стиль: акцент на прогресс-баре, все в одном взгляде."""
    _corner_marks(draw, accent, margin=24)
    draw.rectangle([20,20,W-20,H-20], outline=accent, width=2)

    # верх
    bf=load_font(FONT_BOLD_PATH,22)
    hud_y=45
    draw.text((50,hud_y),f"[ ДМБ-ТАЙМЕР ] {theme['title']}",font=bf,fill=accent)
    stxt="ДОМА" if tl["done"] else "В СТРОЮ"
    sw,_=_tw(draw,stxt,bf)
    draw.text((W-50-sw,hud_y),stxt,font=bf,fill=WHITE)
    _neon_line(draw,40,hud_y+32,W-40,hud_y+32,accent,width=1)

    # имя
    ty=BG_H//2-50
    for s in (96,84,72,60):
        nf=load_font(FONT_BOLD_PATH,s)
        nw,nh=_tw(draw,person,nf)
        if nw<=W-160: break
    _draw_outlined(draw,((W-nw)//2,ty),person,nf,WHITE,ow=4); ty+=nh+10
    tw2,_=_tw(draw,theme["title"],load_font(FONT_BOLD_PATH,18))
    draw.text(((W-tw2)//2,ty),theme["title"],font=load_font(FONT_BOLD_PATH,18),fill=accent)

    # нижняя панель
    pm=60; ty=BG_H+30
    date_str=target.strftime("%d.%m.%Y")
    mf=load_font(FONT_REGULAR_PATH,22)
    h=_draw_centered(draw,f"Дата дембеля: {date_str}",mf,ty,color=(140,140,160)); ty+=h+16
    _neon_line(draw,pm,ty,W-pm,ty,accent,width=2); ty+=22

    # БОЛЬШОЙ прогресс-бар
    bw3=W-pm*2; ty_bar=ty; bar_H=48
    draw.rounded_rectangle([pm,ty_bar,pm+bw3,ty_bar+bar_H], radius=24,
                            fill=(16,16,24),outline=accent,width=2)
    fw=int(bw3*progress_ratio)
    if fw>24:
        draw.rounded_rectangle([pm,ty_bar,pm+fw,ty_bar+bar_H], radius=24, fill=accent)
    pct=int(progress_ratio*100)
    pf3=load_font(FONT_BOLD_PATH,22)
    pt=f"{pct}% службы позади"
    pw,_=_tw(draw,pt,pf3)
    draw.text(((W-pw)//2,ty_bar+bar_H+10),pt,font=pf3,fill=(180,180,200))
    ty=ty_bar+bar_H+52

    _neon_line(draw,pm,ty,W-pm,ty,accent,width=1); ty+=20

    # счётчик дней
    if tl["done"]:
        big_txt="ДОМА!"; big_color=accent
    else:
        big_txt=str(tl["days"])+f" {_days_word(tl['days'])}"; big_color=WHITE
    for s in (80,68,58):
        bgf=load_font(FONT_BOLD_PATH,s)
        gw,gh=_tw(draw,big_txt,bgf)
        if gw<=W-pm*2: break
    _draw_outlined(draw,((W-gw)//2,ty),big_txt,bgf,big_color,ow=3); ty+=gh+12

    if not tl["done"]:
        hmf=load_font(FONT_BOLD_PATH,32)
        hmt=f"{tl['hours']:02d} ч  {tl['minutes']:02d} мин"
        hmw,hmh=_tw(draw,hmt,hmf)
        draw.rectangle([(W-hmw)//2-18,ty-4,(W+hmw)//2+18,ty+hmh+10],
                       fill=(16,16,22),outline=accent,width=1)
        draw.text(((W-hmw)//2,ty+2),hmt,font=hmf,fill=accent); ty+=hmh+28

    _neon_line(draw,pm,ty,W-pm,ty,accent,width=1); ty+=20

    # все таймеры — мини сводка
    lf=load_font(FONT_REGULAR_PATH,18); vf=load_font(FONT_BOLD_PATH,20)
    row=[]; x_cur=pm
    for r in all_timers[:4]:
        if r["person"]==person: continue
        bw4,bh4=_stat_badge(draw,r["person"],
                             "ДОМА" if r["done"] else f"{r['days']}д",
                             x_cur,ty,accent,lf,vf)
        x_cur+=bw4+12
        if x_cur+100>W-pm: break
    ty+=60

    ff=load_font(FONT_BOLD_PATH,14)
    _neon_line(draw,pm,H-70,W-pm,H-70,accent,width=1)
    ft=f"[ ДМБ-ТАЙМЕР  ×  {theme['title']} ]"
    draw.text((_centered_x(draw,ft,ff),H-55),ft,font=ff,fill=(70,70,90))


DEMBEL_LAYOUTS = [_dembel_countdown, _dembel_poster, _dembel_progress]


# ══════════════════════════════════════════════════════════════════════════════
#  ПУБЛИЧНАЯ ФУНКЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

def make_dembel_card(person: str, output_path: str,
                     theme_key: str | None = None,
                     now: datetime | None = None) -> str:
    if theme_key not in THEMES:
        theme_key = random.choice(THEME_KEYS)

    target = DEMBEL_DATES.get(person)
    if target is None:
        raise ValueError(f"Нет даты дембеля для {person!r}")

    tl          = _time_left(target, now)
    prog        = _progress(target, now)
    all_timers  = get_all_timers(now)
    person_c    = _clean(person)

    try:
        theme  = THEMES[theme_key]
        accent = theme["accent"]

        bg  = generate_background(theme_key, W, BG_H)
        img = _blend_bg(bg)
        draw = ImageDraw.Draw(img)

        layout = random.choice(DEMBEL_LAYOUTS)
        layout(draw, img, accent, theme, person_c, tl, prog, target, all_timers)

        _draw_noise(img, amount=6)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "JPEG", quality=93)
        print(f"✅ Дембель-карточка: {output_path} ({theme_key}, {layout.__name__})", flush=True)
        return theme_key

    except Exception as e:
        import sys
        print(f"❌ Ошибка дембель-карточки: {e}", file=sys.stderr, flush=True)
        try:
            img2=Image.new("RGB",(W,H),DARK); d2=ImageDraw.Draw(img2)
            f2=load_font(FONT_BOLD_PATH,100)
            nb=d2.textbbox((0,0),person_c or "?",font=f2)
            d2.text(((W-(nb[2]-nb[0]))//2,H//2-180),person_c or "?",font=f2,fill=WHITE)
            f3=load_font(FONT_BOLD_PATH,160)
            big="ДОМА" if tl["done"] else str(tl["days"])
            gb=d2.textbbox((0,0),big,font=f3)
            d2.text(((W-(gb[2]-gb[0]))//2,H//2-40),big,font=f3,fill=(255,178,40))
            os.makedirs(os.path.dirname(output_path) or ".",exist_ok=True)
            img2.save(output_path,"JPEG",quality=90)
        except Exception:
            pass
        return theme_key


# ── Тест ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    for p in list(DEMBEL_DATES.keys())[:3]:
        make_dembel_card(
            p,
            output_path=os.path.join(BASE_DIR,"temp",f"dembel_{p}.jpg"),
            theme_key=random.choice(THEME_KEYS),
        )
        print(f"✅ {p}")
