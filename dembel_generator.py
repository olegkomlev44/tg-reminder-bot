"""
dembel_generator.py — генератор карточек дембель-таймера (HUD v3.0).

Возможности v3.0:
  - Точный расчёт из 1 года службы (365 дней) по московскому времени (Europe/Moscow)
  - 6-значный процент прогресса (50.786545%) + точные дни и часы позади/осталось
  - 4 визуальных архетипа центрального HUD-индикатора (orbital, reactor, equalizer, octagon)
  - 2 стиля шкалы прогресса (градиентный неон и сегментированный штрихкод из 44 плашек)
  - Система уровней/званий по выслуге (LVL 1..5 + MAX LVL Гражданский)
  - Автоматический расчёт ближайшей вехи (экватор, 200 дней, стодневка, полтинник, космос и т.д.)
  - Встроенная база невоенных фактов (космос, океан, наука, игры, рекорды) под число дней
  - Эксклюзивный золотой скин ROYAL GOLD для тех, кто уже дома (done=True)
  - Сводный дашборд make_dembel_summary_card() со всеми бойцами на одном постере
"""

import math
import os
import random
import sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
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
    _lerp_color,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MSK_TZ = ZoneInfo("Europe/Moscow")

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

# Срок службы — 1 год (365 дней)
SERVICE_LENGTH_DAYS = 365

# Золотая палитра для тех, кто уже дома (Пункт 10)
GOLD_THEME = {
    "title": "ROYAL GOLD · ДОМА",
    "accent": (255, 208, 64),
    "accent2": (255, 136, 28),
}

MONTHS_RU_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

# ══════════════════════════════════════════════
#  НЕВОЕННЫЕ ИНТЕРЕСНЫЕ ФАКТЫ О ЧИСЛАХ ДНЕЙ
#  (Космос, наука, океан, игры, рекорды, поп-культура)
# ══════════════════════════════════════════════
EXACT_DAY_FACTS: dict[int, str] = {
    0:   "Ровно 0 дней на таймере — квест длиной в 365 суток официально пройден на 100%!",
    1:   "1 сутки на Венере длятся 243 земных дня — там день длиннее, чем её собственный год.",
    2:   "За 2 дня (48 часов) на геймджемах Ludum Dare разработчики с нуля создают полноценные хиты.",
    3:   "За 3 дня экипаж «Аполлона-11» долетел от Земли до орбиты Луны (384 400 км).",
    5:   "За 5 дней парусник «Мак-Кей» в XIX веке пересекал штормовую Северную Атлантику.",
    7:   "7 дней — ровно столько длилась миссия «Аполлона-13», благополучно вернувшаяся вопреки аварии.",
    9:   "За 9 дней зонд «Новые горизонты» пролетел мимо системы Юпитера, сделав уникальные снимки Ио.",
    10:  "За 10 дней Брендан Эйх в 1995 году написал первую рабочую версию языка JavaScript.",
    11:  "11 дней — мировой рекорд непрерывного бодрствования человека, установленный Рэнди Гарднером.",
    14:  "14 земных суток длится один солнечный день на поверхности Луны.",
    15:  "За 15 дней свет от звезды Бетельгейзе проходит более 388 миллиардов километров.",
    17:  "17 дней — длительность легендарной миссии «Аполлон-17», крайней высадки людей на Луну в XX веке.",
    18:  "18 дней длился первый беспосадочный перелёт дирижабля «Граф Цеппелин» вокруг света.",
    20:  "За 20 дней Жюль Верн в черновиках рассчитывал скоростной маршрут вокруг Земли до книги «80 дней».",
    21:  "21 день — классический цикл формирования устойчивой нейросети привычек у человека.",
    22:  "22 дня понадобилось экспедиции «Челленджера», чтобы открыть Марианскую впадину.",
    23:  "За 23 дня в 1993 году id Software собрала первый играбельный билд легендарного DOOM.",
    25:  "25 суток — период полного оборота Солнца вокруг своей оси на экваторе.",
    30:  "За 30 дней аппарат «Вояджер-1» улетает от Земли ещё на 44 миллиона километров в межзвёздную среду.",
    40:  "40 дней провёл в открытом океане на надувном плоту Ален Бомбар, доказав выживаемость человека.",
    42:  "42 — Главный ответ на вопрос жизни, Вселенной и всего такого по «Автостопом по Галактике».",
    50:  "За 50 дней солнечный парусник IKAROS успешно разогнался давлением света на пути к Венере.",
    55:  "За 55 дней экспедиция Нансена прошла самый сложный ледовый участок на пути к 86° с. ш.",
    56:  "56 дней понадобилось экспедиции Роберта Пири на собачьих упряжках, чтобы достичь Северного полюса.",
    57:  "57 дней — длительность легендарного перехода через Антарктиду на кайтах и лыжах.",
    60:  "За 60 дней ровер Perseverance на Марсе провёл первые полёты марсианского вертолёта Ingenuity.",
    75:  "75 дней длился одиночный переход Фёдора Конюхова через Тихий океан на гребной лодке.",
    80:  "За 80 дней Филеас Фогг обогнул земной шар в самом известном романе Жюля Верна.",
    88:  "88 земных суток длится полный год на Меркурии — самой быстрой планете Солнечной системы.",
    90:  "90 дней — расчётный срок миссии марсоходов Spirit и Opportunity, которые в итоге проработали годы.",
    100: "За 100 дней Тур Хейердал на бальсовом плоту «Кон-Тики» пересёк Тихий океан (8000 км).",
    150: "За 150 дней зонд «Новые горизонты» пролетел путь от орбиты Юпитера до пояса Койпера.",
    178: "178 дней на орбите провёл экипаж рекордной экспедиции станции «Салют-6» в 1979 году.",
    179: "179 суток — почти столько Джон Фэрфакс в одиночку переплывал Атлантику на вёсельной лодке в 1969 году.",
    180: "180 суток грёб Джон Фэрфакс, первым в истории пересёкший океан на вёсельной лодке (Канары — Флорида).",
    182: "182 дня — точный астрономический экватор земного года: позади ровно половина орбиты вокруг Солнца!",
    200: "За 200 суток космический корабль по траектории Гомана долетает от Земли до орбиты Марса.",
    225: "225 земных суток длится один полный год на Венере.",
    250: "За 250 дней ровер Curiosity прошёл первые километры по кратеру Гейла и нашёл следы древней воды.",
    300: "300 дней длилась антарктическая зимовка экспедиции Руаля Амундсена перед штурмом Южного полюса.",
    365: "365 суток — Земля замыкает полный виток в 940 миллионов километров вокруг Солнца!",
}

RANGE_FACTS: list[tuple[int, int, list[str]]] = [
    (1, 14, [
        "За {d} сут. Международная космическая станция совершает {orbits} полных витков вокруг Земли!",
        "За {d} дн. зонд «Вояджер-1» пролетает в межзвёздном пространстве около {voyager_mln} млн километров.",
        "Свет летит от Солнца до Земли 8 минут, а за {d} дн. луч лазера ушёл бы на {light_bln} млрд км!",
    ]),
    (15, 45, [
        "За {d} дн. Земля по своей орбите вокруг Солнца пролетает внушительные {earth_mln} млн километров.",
        "За {d} сут. МКС на высоте 420 км успевает облететь нашу планету ровно {orbits} раз.",
        "Один год на Меркурии длится 88 суток — твой остаток ({d} дн.) это меньше половины меркурианского года!",
    ]),
    (46, 99, [
        "80 дней понадобилось Филеасу Фоггу на кругосветку, а тут осталось всего {d} дн. ({hours_total} часов)!",
        "За {d} сут. Земля проходит {earth_mln} млн км вокруг Солнца — это дальше, чем расстояние до Марса.",
        "На создание первой версии Minecraft у Маркуса Перссона ушло несколько недель — сравнимо с {d} дн.!",
    ]),
    (100, 185, [
        "180 суток Джон Фэрфакс пересекал океан на вёсельной лодке в 1969 году — а на таймере сейчас {d} дн.!",
        "Полёт от Земли до Марса занимает около 200 суток — оставшиеся {d} дн. короче марсианского перелёта.",
        "За {d} сут. наша планета пролетает по орбите {earth_mln} млн км со скоростью 107 000 км/ч.",
    ]),
    (186, 366, [
        "Год на Венере длится 225 земных суток — {d} дн. сопоставимы с целым венерианским годом!",
        "За {d} сут. МКС делает {orbits} витков вокруг Земли, встречая рассвет каждые 90 минут.",
        "Земля за {d} дн. пролетает {earth_mln} млн километров вокруг Солнца без единой остановки.",
    ]),
]


def get_day_fact(days_left: int) -> str:
    """
    Возвращает интересный невоенный факт (космос, океан, наука, игры),
    привязанный к текущему числу оставшихся дней.
    """
    d = max(0, int(days_left))
    if d in EXACT_DAY_FACTS:
        return EXACT_DAY_FACTS[d]

    for neighbor in (d + 1, d - 1):
        if neighbor in (180, 100, 80, 50, 30, 10):
            return EXACT_DAY_FACTS[neighbor]

    orbits = max(16, d * 16)
    voyager_mln = round(d * 1.47, 1)
    light_bln = round(d * 25.9, 1)
    earth_mln = round(d * 2.57, 1)
    hours_total = d * 24

    for low, high, templates in RANGE_FACTS:
        if low <= d <= high:
            tpl = templates[d % len(templates)]
            return tpl.format(
                d=d,
                orbits=orbits,
                voyager_mln=voyager_mln,
                light_bln=light_bln,
                earth_mln=earth_mln,
                hours_total=hours_total,
            )

    return f"За {d} сут. Земля пролетает по орбите {earth_mln} млн км вокруг Солнца!"


# ══════════════════════════════════════════════
#  ЗВАНИЯ / УРОВНИ И ВЕХИ (Пункты 1, 5, 6)
# ══════════════════════════════════════════════
def get_dembel_rank(progress: float, done: bool = False) -> dict:
    """
    Определяет статус/уровень по проценту выслуги (от 0.0 до 1.0).
    Возвращает словарь с полями: title, level, short, icon.
    """
    if done or progress >= 1.0:
        return {"title": "ГРАЖДАНСКИЙ", "level": "MAX LVL", "short": "ДОМА", "icon": "star"}
    pct = progress * 100.0
    if pct < 16.0:
        return {"title": "ДУХ · СТАРТ", "level": "LVL 1", "short": "ДУХ", "icon": "hex"}
    if pct < 33.3:
        return {"title": "СЛОН", "level": "LVL 2", "short": "СЛОН", "icon": "diamond"}
    if pct < 50.0:
        return {"title": "ЧЕРПАК", "level": "LVL 3", "short": "ЧЕРПАК", "icon": "shield"}
    if pct < 75.0:
        return {"title": "ДЕД · ЭКВАТОР+", "level": "LVL 4", "short": "ДЕД", "icon": "chevron"}
    return {"title": "ДЕМБЕЛЬ · ФИНИШ", "level": "LVL 5", "short": "ДЕМБЕЛЬ", "icon": "star"}


def get_next_milestone(target: date, now: datetime | None = None) -> dict:
    """
    Находит ближайшую веху (круглую дату после старта или до ДМБ),
    как в мобильном приложении («200 дней со старта · 4 ноября · через 15д»).
    """
    now_dt = _now_msk(now)
    today = now_dt.date()
    start_date = _service_start_date(target)
    total_days = (target - start_date).days

    if today >= target:
        d_str = f"{target.day} {MONTHS_RU_GEN[target.month]}"
        return {
            "title": "ДМБ ВЫПОЛНЕН",
            "date": target,
            "date_label": d_str,
            "days_until": 0,
            "badge_value": f"Дома с {target.strftime('%d.%m')}",
            "full_label": f"Дома с {d_str}",
        }

    candidates = []
    passed_milestones = [
        (50,  "50 дней со старта"),
        (100, "100 дней со старта"),
        (150, "150 дней со старта"),
        (182, "Экватор (полгода)"),
        (200, "200 дней со старта"),
        (250, "250 дней со старта"),
        (300, "300 дней со старта"),
        (335, "335 дней (финишная)"),
    ]
    for p_days, title in passed_milestones:
        m_date = start_date + timedelta(days=p_days)
        if today <= m_date < target:
            candidates.append((m_date, title))

    left_milestones = [
        (150, "150 дней до дома"),
        (100, "Стодневка (100 дн.)"),
        (75,  "75 дней до дома"),
        (50,  "Полтинник (50 дн.)"),
        (30,  "30 дней до дома"),
        (20,  "20 дней до дома"),
        (14,  "2 недели до дома"),
        (10,  "Космос (10 дней)"),
        (7,   "Неделя до дома"),
        (3,   "Светофор (3 дня)"),
        (1,   "Крайний день!"),
        (0,   "День ДМБ!"),
    ]
    for l_days, title in left_milestones:
        m_date = target - timedelta(days=l_days)
        if today <= m_date <= target and l_days < total_days:
            candidates.append((m_date, title))

    candidates.sort(key=lambda x: x[0])
    if not candidates:
        m_date, title = target, "День ДМБ!"
    else:
        m_date, title = candidates[0]

    days_until = max(0, (m_date - today).days)
    date_label = f"{m_date.day} {MONTHS_RU_GEN[m_date.month]}"
    when_str = "СЕГОДНЯ!" if days_until == 0 else f"через {days_until} дн."
    return {
        "title": title,
        "date": m_date,
        "date_label": date_label,
        "days_until": days_until,
        "badge_value": f"{title[:18]} · {days_until}д" if days_until > 0 else f"{title[:18]} · СЕГОДНЯ!",
        "full_label": f"{title} ({date_label}, {when_str})",
    }


def get_today_milestones(now: datetime | None = None) -> list[dict]:
    """
    Проверяет всех участников DEMBEL_DATES на наступление круглой вехи СЕГОДНЯ (Пункт 5).
    Возвращает список словарей: {person, key, title, days_left, passed_days, done}.
    """
    now_dt = _now_msk(now)
    today = now_dt.date()
    events = []

    left_triggers = {
        0:   "🏠 СЕГОДНЯ ДМБ! Свобода и возвращение домой!",
        1:   "🔥 ОСТАЛСЯ 1 ДЕНЬ до дома! Завтра на свободу!",
        3:   "🚦 СВЕТОФОР — осталось ровно 3 дня до дома!",
        7:   "🗓 Ровно 1 НЕДЕЛЯ (7 дней) до дома!",
        10:  "🚀 КОСМОС! Пошёл обратный отсчёт: 10 дней до дома!",
        30:  "⚡ Финишная прямая — остался ровно 1 месяц (30 дней)!",
        50:  "🪙 ЗОЛОТАЯ ПОЛТИННИКА — 50 дней до дома!",
        100: "💯 СТОДНЕВКА — ровно 100 дней до дома!",
    }
    passed_triggers = {
        100: "🏅 100 дней позади!",
        182: "🌗 ЭКВАТОР! Ровно половина года позади!",
        200: "⭐ 200 дней позади! До дома меньше 165 суток!",
        300: "🔥 300 дней позади!",
    }

    for person, target in DEMBEL_DATES.items():
        start_date = _service_start_date(target)
        days_left = (target - today).days
        days_passed = (today - start_date).days

        if days_left in left_triggers:
            events.append({
                "person": person,
                "key": f"{person}:{today.isoformat()}:left_{days_left}",
                "title": left_triggers[days_left],
                "days_left": max(0, days_left),
                "passed_days": days_passed,
                "done": days_left <= 0,
            })
        elif days_passed in passed_triggers and days_left > 0:
            events.append({
                "person": person,
                "key": f"{person}:{today.isoformat()}:passed_{days_passed}",
                "title": passed_triggers[days_passed],
                "days_left": days_left,
                "passed_days": days_passed,
                "done": False,
            })

    return events


# ══════════════════════════════════════════════
#  БАЗОВАЯ МАТЕМАТИКА ТАЙМЕРА (1 ГОД, МСК)
# ══════════════════════════════════════════════
def _now_msk(now: datetime | None = None) -> datetime:
    """
    Возвращает текущее время по Москве (naive datetime) для точного расчёта
    на любом хостинге (включая Bothost с системным временем UTC).
    """
    if now is None:
        return datetime.now(MSK_TZ).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(MSK_TZ).replace(tzinfo=None)
    return now


def _service_start_date(target: date) -> date:
    """Дата старта — ровно за 1 календарный год до даты ДМБ."""
    try:
        return target.replace(year=target.year - 1)
    except ValueError:
        return target - timedelta(days=SERVICE_LENGTH_DAYS)


def _time_left(target: date, now: datetime | None = None) -> dict:
    """
    Считает, сколько времени осталось до target и сколько уже позади
    из 1 года (дни/часы/минуты/секунды + точный процент до 6 знаков).
    """
    now_dt = _now_msk(now)
    start_date = _service_start_date(target)
    start_dt = datetime.combine(start_date, datetime.min.time())
    target_dt = datetime.combine(target, datetime.min.time())

    total_service_seconds = max(1.0, (target_dt - start_dt).total_seconds())
    total_service_days = (target - start_date).days

    delta_left = target_dt - now_dt
    left_seconds = delta_left.total_seconds()

    delta_passed = now_dt - start_dt
    passed_seconds = max(0.0, min(total_service_seconds, delta_passed.total_seconds()))

    passed_days = int(passed_seconds // 86400)
    passed_hours = int((passed_seconds % 86400) // 3600)
    passed_minutes = int((passed_seconds % 3600) // 60)
    passed_seconds_rem = int(passed_seconds % 60)
    progress = max(0.0, min(1.0, passed_seconds / total_service_seconds))

    if left_seconds <= 0:
        rank = get_dembel_rank(1.0, done=True)
        return {
            "done": True,
            "days": 0,
            "hours": 0,
            "minutes": 0,
            "seconds": 0,
            "total_seconds": 0,
            "passed_days": total_service_days,
            "passed_hours": 0,
            "passed_minutes": 0,
            "passed_seconds": 0,
            "total_service_days": total_service_days,
            "start_date": start_date,
            "progress": 1.0,
            "rank": rank,
        }

    days = int(left_seconds // 86400)
    hours = int((left_seconds % 86400) // 3600)
    minutes = int((left_seconds % 3600) // 60)
    seconds = int(left_seconds % 60)
    rank = get_dembel_rank(progress, done=False)

    return {
        "done": False,
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "total_seconds": left_seconds,
        "passed_days": passed_days,
        "passed_hours": passed_hours,
        "passed_minutes": passed_minutes,
        "passed_seconds": passed_seconds_rem,
        "total_service_days": total_service_days,
        "start_date": start_date,
        "progress": progress,
        "rank": rank,
    }


def _progress_ratio(target: date, now: datetime | None = None) -> float:
    """Точная доля пройденного времени за 1 год (от 0.0 до 1.0)."""
    return _time_left(target, now)["progress"]


def get_all_timers(now: datetime | None = None) -> list[dict]:
    """Возвращает список всех людей с посчитанным временем до дембеля, отсортированный по возрастанию."""
    now_dt = _now_msk(now)
    result = []
    for person, target in DEMBEL_DATES.items():
        tl = _time_left(target, now_dt)
        result.append({
            "person": person,
            "target": target,
            **tl,
        })
    result.sort(key=lambda r: r["total_seconds"] if not r["done"] else -1)
    return result


def format_dembel_caption(person: str, now: datetime | None = None) -> str:
    """Формирует информативную подпись к карточке с точным процентом, вехой и фактом дня."""
    target = DEMBEL_DATES.get(person)
    if not target:
        return f"🎖 Дембель-таймер · *{person}*"

    now_dt = _now_msk(now)
    tl = _time_left(target, now_dt)
    ms = get_next_milestone(target, now_dt)
    fact = get_day_fact(tl["days"])
    rank = tl["rank"]

    if tl["done"]:
        return (
            f"🏆 *{person}* — *ГРАЖДАНСКИЙ (ДОМА!)*\n"
            f"📅 Дата ДМБ: `{target.strftime('%d.%m.%Y')}` (100%)\n"
            f"💡 _{fact}_"
        )

    pct_exact = f"{tl['progress'] * 100:.6f}".replace(".", ",")
    return (
        f"🎖 *{person}* · `{rank['title']}` ({rank['level']})\n"
        f"📊 Прогресс: *{pct_exact}%*\n"
        f"✅ Прошло: `{tl['passed_days']} дн. {tl['passed_hours']} ч. {tl['passed_minutes']} мин. {tl['passed_seconds']} сек.`\n"
        f"⏳ Осталось: `{tl['days']} дн. {tl['hours']} ч. {tl['minutes']} мин. {tl['seconds']} сек.`\n"
        f"⭐ Ближайшая веха: *{ms['full_label']}*\n\n"
        f"💡 _{fact}_"
    )


# ══════════════════════════════════════════════
#  4 ВАРИАТИВНЫХ АРХЕТИПА HUD-КОЛЬЦА
# ══════════════════════════════════════════════
GAUGE_MODES = ["orbital", "reactor", "equalizer", "octagon"]


def _draw_circular_gauge(img, cx, cy, radius, progress, accent, accent2, mode=None):
    """
    Рисует центральный HUD-индикатор в одном из 4 визуальных стилей:
      - orbital: классическое орбитальное кольцо с делениями и светящейся сферой
      - reactor: 32 неоновых сегмента-блока по кругу + внутренний контур
      - equalizer: орбитальное кольцо + боковые прицельные дуги
      - octagon: восьмиугольный силовой контур с узлами по вершинам
    """
    mode = mode if mode in GAUGE_MODES else random.choice(GAUGE_MODES)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    _draw_glow_circle(od, cx, cy, int(radius * 0.95), accent, max_alpha=48)

    if mode == "reactor":
        num_segs = 32
        start_base = 135.0
        sweep_total = 270.0
        seg_step = sweep_total / num_segs
        seg_span = seg_step * 0.68
        active_segs = int(round(num_segs * progress))

        for i in range(num_segs):
            a0 = start_base + i * seg_step
            a1 = a0 + seg_span
            if i < active_segs:
                t = i / max(1, num_segs - 1)
                col = _lerp_color(accent, accent2, t)
                od.arc(
                    [cx - radius - 2, cy - radius - 2, cx + radius + 2, cy + radius + 2],
                    start=a0, end=a1, fill=col + (85,), width=18,
                )
                od.arc(
                    [cx - radius, cy - radius, cx + radius, cy + radius],
                    start=a0, end=a1, fill=col + (245,), width=11,
                )
            else:
                od.arc(
                    [cx - radius, cy - radius, cx + radius, cy + radius],
                    start=a0, end=a1, fill=(45, 52, 75, 130), width=9,
                )

        r_in = radius - 18
        od.arc([cx - r_in, cy - r_in, cx + r_in, cy + r_in], start=135, end=405, fill=accent2 + (70,), width=2)

    elif mode == "octagon":
        outer_r = radius + 16
        pts = [
            (
                cx + outer_r * math.cos(math.radians(45 * i + 22.5)),
                cy + outer_r * math.sin(math.radians(45 * i + 22.5)),
            )
            for i in range(8)
        ]
        od.polygon(pts, outline=accent + (90,), width=2)
        for px, py in pts:
            od.ellipse([px - 4, py - 4, px + 4, py + 4], fill=accent2 + (190,))

        start_angle = 135
        sweep_total = 270
        bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
        od.arc(bbox, start=start_angle, end=start_angle + sweep_total, fill=(30, 35, 52, 165), width=10)
        prog_sweep = int(sweep_total * max(0.02, min(1.0, progress)))
        end_angle = start_angle + prog_sweep
        od.arc(
            [cx - radius - 2, cy - radius - 2, cx + radius + 2, cy + radius + 2],
            start=start_angle, end=end_angle, fill=accent2 + (95,), width=16,
        )
        od.arc(bbox, start=start_angle, end=end_angle, fill=accent + (245,), width=8)

        tip_rad = math.radians(end_angle)
        tx = int(cx + radius * math.cos(tip_rad))
        ty = int(cy + radius * math.sin(tip_rad))
        _draw_glow_circle(od, tx, ty, 22, accent2, max_alpha=135)
        od.ellipse([tx - 6, ty - 6, tx + 6, ty + 6], fill=WHITE + (255,))

    else:
        outer_r = radius + 20
        for deg in range(0, 360, 6):
            rad = math.radians(deg - 90)
            is_major = (deg % 30 == 0)
            r_in = outer_r - (11 if is_major else 5)
            x1, y1 = cx + r_in * math.cos(rad), cy + r_in * math.sin(rad)
            x2, y2 = cx + outer_r * math.cos(rad), cy + outer_r * math.sin(rad)
            col = accent + (105,) if (deg / 360.0) <= progress else (120, 130, 160, 42)
            od.line([(x1, y1), (x2, y2)], fill=col, width=2 if is_major else 1)

        if mode == "equalizer":
            bracket_r = outer_r + 14
            od.arc(
                [cx - bracket_r, cy - bracket_r, cx + bracket_r, cy + bracket_r],
                start=150, end=210, fill=accent2 + (140,), width=3,
            )
            od.arc(
                [cx - bracket_r, cy - bracket_r, cx + bracket_r, cy + bracket_r],
                start=330, end=390, fill=accent2 + (140,), width=3,
            )

        start_angle = 135
        sweep_total = 270
        bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
        od.arc(bbox, start=start_angle, end=start_angle + sweep_total, fill=(30, 35, 52, 160), width=10)

        prog_sweep = int(sweep_total * max(0.02, min(1.0, progress)))
        end_angle = start_angle + prog_sweep
        od.arc(
            [cx - radius - 2, cy - radius - 2, cx + radius + 2, cy + radius + 2],
            start=start_angle, end=end_angle, fill=accent2 + (90,), width=16,
        )
        od.arc(bbox, start=start_angle, end=end_angle, fill=accent + (240,), width=8)

        tip_rad = math.radians(end_angle)
        tx = int(cx + radius * math.cos(tip_rad))
        ty = int(cy + radius * math.sin(tip_rad))
        _draw_glow_circle(od, tx, ty, 24, accent2, max_alpha=130)
        od.ellipse([tx - 6, ty - 6, tx + 6, ty + 6], fill=WHITE + (255,))

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img.paste(img_rgba.convert("RGB"), (0, 0))


def _draw_segmented_barcode_bar(img, box, progress, accent, accent2):
    """
    Рисует прогресс-бар в виде вертикальных светящихся плашек-штрихкода
    (как в мобильном приложении ДМБ), чередующийся с классическим градиентным баром.
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    bw = x1 - x0
    bh = y1 - y0
    progress = max(0.0, min(1.0, float(progress)))

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    od.rounded_rectangle(
        [x0 - 8, y0 - 4, x1 + 8, y1 + 4],
        radius=12,
        fill=(14, 16, 26, 225),
        outline=(65, 72, 95, 160),
        width=2,
    )

    num_bars = 44
    gap = 5
    bar_w = max(4, (bw - (num_bars - 1) * gap) // num_bars)
    actual_total_w = num_bars * bar_w + (num_bars - 1) * gap
    start_x = x0 + (bw - actual_total_w) // 2
    active_count = int(round(num_bars * progress))

    for i in range(num_bars):
        bx = start_x + i * (bar_w + gap)
        if i < active_count:
            t = i / max(1, num_bars - 1)
            col = _lerp_color(accent, accent2, t)
            od.rounded_rectangle(
                [bx, y0 + 2, bx + bar_w, y1 - 2],
                radius=2,
                fill=col + (245,),
            )
            od.rectangle([bx, y0 + 2, bx + bar_w, y0 + 7], fill=WHITE + (90,))
        elif i == active_count and progress < 1.0:
            od.rounded_rectangle(
                [bx, y0, bx + bar_w, y1],
                radius=2,
                fill=WHITE + (230,),
            )
        else:
            od.rounded_rectangle(
                [bx, y0 + 5, bx + bar_w, y1 - 5],
                radius=2,
                fill=(55, 62, 82, 115),
            )

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    img.paste(img_rgba.convert("RGB"), (0, 0))


def _draw_timer_segments(draw, img, cx, y, segments, accent, val_font, label_font):
    """
    Рисует сегментированный таймер ЧЧ : ММ : СС в виде отдельных стеклянных
    капсул-плиток с неоновым свечением цифр.
    """
    accent2 = get_accent2(accent)
    card_w = 152
    card_h = 96
    gap = 34
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
            fill_alpha=205,
            radius=18,
            border_w=2,
        )
        draw = ImageDraw.Draw(img)

        vb = draw.textbbox((0, 0), val, font=val_font)
        vw, vh = vb[2] - vb[0], vb[3] - vb[1]
        vx = bx0 + (card_w - vw) // 2
        vy = by0 + 10
        _draw_glow_text(
            img, (vx, vy), val, val_font,
            fill=WHITE, glow_color=accent,
            blur_radius=7, glow_passes=1, outline_w=3,
        )
        draw = ImageDraw.Draw(img)

        lb = draw.textbbox((0, 0), label.upper(), font=label_font)
        lw = lb[2] - lb[0]
        draw.text(
            (bx0 + (card_w - lw) // 2, by1 - 25),
            label.upper(), font=label_font, fill=(165, 172, 195),
        )

        if i < len(segments) - 1:
            sep_x = bx1 + gap // 2
            sep_cy = by0 + card_h // 2
            for dy in (-11, 11):
                draw.ellipse([sep_x - 4, sep_cy + dy - 4, sep_x + 4, sep_cy + dy + 4], fill=accent)

    return total_w


def _plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ДЕНЬ ОСТАЛСЯ"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "ДНЯ ОСТАЛОСЬ"
    return "ДНЕЙ ОСТАЛОСЬ"


# ══════════════════════════════════════════════
#  ИНДИВИДУАЛЬНАЯ КАРТОЧКА ДМБ (HUD v3.0)
# ══════════════════════════════════════════════
def make_dembel_card(person: str, output_path: str, theme_key: str | None = None, now: datetime | None = None) -> str:
    """
    Рисует вариативную карточку дембель-таймера (HUD v3.0) для одного человека:
      - 4 архетипа центрального индикатора + 2 стиля прогресс-бара
      - плашка звания/уровня выслуги и точный 6-значный процент
      - блок ближайшей вехи + сосед по очереди
      - карточка интересного невоенного факта дня + эквивалент времени
      - автоматический золотой скин ROYAL GOLD для тех, кто уже дома (done=True)
    """
    if theme_key not in THEMES:
        theme_key = random.choice(THEME_KEYS)

    target = DEMBEL_DATES.get(person)
    if target is None:
        raise ValueError(f"Нет даты дембеля для {person!r}")

    now_dt = _now_msk(now)
    tl = _time_left(target, now_dt)
    progress = tl["progress"]
    rank_info = tl["rank"]
    milestone = get_next_milestone(target, now_dt)
    fact_text = _clean_for_image(get_day_fact(tl["days"]))

    try:
        if tl["done"]:
            theme_title = GOLD_THEME["title"]
            accent = GOLD_THEME["accent"]
            accent2 = GOLD_THEME["accent2"]
            bg_theme_key = "eldenring"
            bg_style = "sunburst"
        else:
            theme = THEMES[theme_key]
            theme_title = theme["title"]
            accent = theme["accent"]
            accent2 = get_accent2(accent)
            bg_theme_key = theme_key
            bg_style = None

        style_key, style = pick_style()
        gauge_mode = "octagon" if tl["done"] else random.choice(GAUGE_MODES)
        use_barcode_bar = (not tl["done"]) and (random.random() < 0.5)
        person_clean = _clean_for_image(person) or person

        # ── 1. Полноэкранный арт-фон с размытием под нижней панелью ──────
        chosen_bg = bg_style or random.choice(style["bg"])
        full_bg = generate_background(bg_theme_key, W, H, bg_style=chosen_bg)
        panel_top = BG_H + 56
        blurred_bottom = full_bg.crop((0, panel_top - 20, W, H)).filter(ImageFilter.GaussianBlur(18))
        img = full_bg.copy()
        img.paste(blurred_bottom, (0, panel_top - 20))

        img_rgba = img.convert("RGBA")
        fade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fade)
        fade_start = BG_H - 130
        for y in range(fade_start, H):
            t = min(1.0, (y - fade_start) / 260.0)
            a = int(218 * (t ** 1.2))
            fd.line([(0, y), (W, y)], fill=(DARK[0], DARK[1], DARK[2], a))
        img_rgba.alpha_composite(fade)
        img = img_rgba.convert("RGB")

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

        # ── 2. Верхняя шапка: бейдж темы + ранг в очереди ДМБ ────────────
        all_timers = get_all_timers(now_dt)
        idx = next((i for i, r in enumerate(all_timers) if r["person"] == person), None)

        badge_font = load_font(FONT_BOLD_PATH, 21)
        badge_text = f"◈ {theme_title} ◈"
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

        status_font = load_font(FONT_BOLD_PATH, 21)
        if tl["done"]:
            status_text = "★ ГРАЖДАНСКИЙ ★"
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

        line_y = top_y + bh + pad_y * 2 + 14
        _draw_neon_line(draw, MARGIN + 12, line_y, W - MARGIN - 12, line_y, accent, width=2, style=style["line"])

        # ── 3. Имя бойца + капсула Звания/Уровня (Пункт 6) ───────────────
        for name_size in (78, 70, 62, 54):
            name_font = load_font(FONT_BOLD_PATH, name_size)
            nb = draw.textbbox((0, 0), person_clean, font=name_font)
            if nb[2] - nb[0] <= W - 180:
                break
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = (W - nw) // 2
        ny = line_y + 14
        _draw_glow_text(
            img, (nx, ny), person_clean, name_font,
            fill=WHITE if not tl["done"] else accent,
            glow_color=accent,
            blur_radius=10, glow_passes=1, outline_w=4,
        )
        draw = ImageDraw.Draw(img)

        rank_font = load_font(FONT_BOLD_PATH, 18)
        rank_str = f"◈  СТАТУС: {rank_info['title']}  ·  {rank_info['level']}  ◈"
        rb = draw.textbbox((0, 0), rank_str, font=rank_font)
        rw, rh = rb[2] - rb[0], rb[3] - rb[1]
        ry = ny + nh + 16
        _badge_rect(
            draw,
            ((W - rw) // 2 - 20, ry - 6, (W + rw) // 2 + 20, ry + rh + 8),
            accent2, shape="pill", fill=(14, 16, 26), outline_w=2,
        )
        draw.text(((W - rw) // 2, ry), rank_str, font=rank_font, fill=accent)

        # ── 4. Вариативный HUD-индикатор + Огромный счётчик дней ─────────
        gauge_cx = W // 2
        gauge_cy = ry + rh + 155
        gauge_r = 136
        _draw_circular_gauge(img, gauge_cx, gauge_cy, gauge_r, progress, accent, accent2, mode=gauge_mode)

        big_text = "ДОМА" if tl["done"] else str(tl["days"])
        max_big_w = gauge_r * 2 - 26 if not tl["done"] else gauge_r * 2 + 40
        for big_size in (168, 150, 132, 114, 92):
            big_font = load_font(FONT_BOLD_PATH, big_size)
            gb = ImageDraw.Draw(img).textbbox((0, 0), big_text, font=big_font)
            if gb[2] - gb[0] <= max_big_w:
                break
        gw, gh = gb[2] - gb[0], gb[3] - gb[1]
        gx = (W - gw) // 2
        gy = gauge_cy - gh // 2 - gb[1] - 10

        _draw_glow_text(
            img, (gx, gy), big_text, big_font,
            fill=accent, glow_color=accent2,
            blur_radius=14, glow_passes=2, outline_w=5,
            top_color=WHITE,
        )
        draw = ImageDraw.Draw(img)

        sub_font = load_font(FONT_BOLD_PATH, 20)
        sub_text = "СЛУЖБА ОКОНЧЕНА!" if tl["done"] else _plural_days(tl["days"])
        _draw_centered(draw, sub_text, sub_font, gauge_cy + gauge_r - 22, color=WHITE, shadow=True, so=2)

        # ── 5. Капсульное табло ЧЧ : ММ : СС (или баннер СВОБОДА) ────────
        seg_top_y = gauge_cy + gauge_r + 18
        if not tl["done"]:
            seg_val_font = load_font(FONT_BOLD_PATH, 38)
            seg_label_font = load_font(FONT_BOLD_PATH, 14)
            segments = [
                (f"{tl['hours']:02d}", "часов"),
                (f"{tl['minutes']:02d}", "минут"),
                (f"{tl['seconds']:02d}", "секунд"),
            ]
            _draw_timer_segments(
                draw, img, W // 2, seg_top_y,
                segments, accent, seg_val_font, seg_label_font,
            )
            draw = ImageDraw.Draw(img)
        else:
            done_banner_font = load_font(FONT_BOLD_PATH, 26)
            done_str = f"★  ПРИКАЗ ВЫПОЛНЕН НА 100%  ·  СВОБОДА С {target.strftime('%d.%m.%Y')}  ★"
            db = draw.textbbox((0, 0), done_str, font=done_banner_font)
            dw, dh = db[2] - db[0], db[3] - db[1]
            _badge_rect(
                draw,
                ((W - dw) // 2 - 28, seg_top_y + 14, (W + dw) // 2 + 28, seg_top_y + dh + 46),
                accent, shape="pill", fill=(20, 18, 12), outline_w=2,
            )
            draw.text(((W - dw) // 2, seg_top_y + 28), done_str, font=done_banner_font, fill=accent)

        # ── 6. Нижняя стеклянная панель: даты, 6-значный % и шкала ───────
        text_y = panel_top + 22

        meta_font = load_font(FONT_BOLD_PATH, 21)
        start_str = tl["start_date"].strftime("%d.%m.%Y")
        date_str = target.strftime("%d.%m.%Y")
        meta_text = f"ПРИЗЫВ: {start_str}   ·   ДМБ: {date_str}   (1 ГОД)"
        h = _draw_centered(draw, meta_text, meta_font, text_y, color=WHITE, shadow=True, so=2)
        text_y += h + 14

        _draw_neon_line(draw, MARGIN + 45, text_y, W - MARGIN - 45, text_y, accent, width=2, style=style["line"])
        text_y += 20

        bar_x0 = 92
        bar_x1 = W - 92
        bar_h = 44
        if use_barcode_bar:
            _draw_segmented_barcode_bar(img, (bar_x0, text_y, bar_x1, text_y + bar_h), progress, accent, accent2)
        else:
            _draw_gradient_bar(
                img, (bar_x0, text_y, bar_x1, text_y + bar_h),
                progress=progress, accent=accent, accent2=accent2,
            )
        draw = ImageDraw.Draw(img)

        pct_font = load_font(FONT_BOLD_PATH, 23)
        pct_text = f"{progress * 100:.6f}%" if not tl["done"] else "100.000000%"
        pt = draw.textbbox((0, 0), pct_text, font=pct_font)
        pw, ph = pt[2] - pt[0], pt[3] - pt[1]
        _badge_rect(
            draw,
            ((W - pw) // 2 - 14, text_y + 5, (W + pw) // 2 + 14, text_y + bar_h - 5),
            accent, shape="pill", fill=(12, 14, 22), outline_w=1,
        )
        _draw_outlined_text(
            draw,
            ((W - pw) // 2, text_y + (bar_h - ph) // 2 - 2),
            pct_text, pct_font, fill=WHITE, outline_color=(0, 0, 0), outline_w=3,
        )

        passed_days = tl["passed_days"]
        passed_hours = tl["passed_hours"]
        cap_font = load_font(FONT_BOLD_PATH, 17)
        if tl["done"]:
            left_cap = f"ПОЗАДИ: {passed_days} ДН. (100%)"
            right_cap = "ОСТАЛОСЬ: 0 ДН."
        else:
            left_cap = f"ПОЗАДИ: {passed_days} ДН. {passed_hours} Ч."
            right_cap = f"ОСТАЛОСЬ: {tl['days']} ДН. {tl['hours']} Ч."
        draw.text((bar_x0 + 4, text_y + bar_h + 10), left_cap, font=cap_font, fill=accent)
        rc_b = draw.textbbox((0, 0), right_cap, font=cap_font)
        draw.text((bar_x1 - (rc_b[2] - rc_b[0]) - 4, text_y + bar_h + 10), right_cap, font=cap_font, fill=accent2)
        text_y += bar_h + 44

        # ── 7. Два симметричных бейджа: БЛИЖАЙШАЯ ВЕХА (Пункт 1) и ОЧЕРЕДЬ ─
        label_font = load_font(FONT_BOLD_PATH, 16)
        val_font = load_font(FONT_BOLD_PATH, 21)

        grid_w = W - 184
        gap = 22
        col_w = (grid_w - gap) // 2
        start_x = (W - grid_w) // 2

        ms_label = f"ВЕХА · {milestone['date_label'].upper()}"
        ms_val = milestone["badge_value"]
        _, bh_left = _draw_stat_badge(
            draw,
            label=ms_label,
            value=ms_val,
            x=start_x, y=text_y, accent=accent,
            font_label=label_font, font_value=val_font,
            shape=style["badge"], icon="star", min_w=col_w,
        )

        neighbor = None
        neighbor_lbl = "ОЧЕРЕДЬ ДМБ"
        if idx is not None:
            prev_r = all_timers[idx - 1] if idx > 0 else None
            next_r = all_timers[idx + 1] if idx < len(all_timers) - 1 else None
            if prev_r and prev_r["target"] == target:
                neighbor = prev_r
                neighbor_lbl = "В ОДИН ДЕНЬ"
            elif next_r and next_r["target"] == target:
                neighbor = next_r
                neighbor_lbl = "В ОДИН ДЕНЬ"
            elif prev_r and not prev_r["done"]:
                neighbor = prev_r
                neighbor_lbl = "УХОДИТ РАНЬШЕ"
            elif next_r:
                neighbor = next_r
                neighbor_lbl = "СЛЕДУЮЩИЙ ЗА НИМ"
            elif prev_r:
                neighbor = prev_r
                neighbor_lbl = "УЖЕ ДОМА"

        if neighbor:
            n_val = (
                f"{_clean_for_image(neighbor['person'])} (дома)"
                if neighbor["done"]
                else f"{_clean_for_image(neighbor['person'])} ({neighbor['days']}д)"
            )
        else:
            n_val = "Лидер очереди"

        _, bh_right = _draw_stat_badge(
            draw,
            label=neighbor_lbl,
            value=n_val,
            x=start_x + col_w + gap, y=text_y, accent=accent2,
            font_label=label_font, font_value=val_font,
            shape=style["badge"], icon="chevron", min_w=col_w,
        )
        text_y += max(bh_left, bh_right) + 18

        # ── 8. Блок «Интересный факт дня» + эквивалент времени (Пункт 2) ──
        fact_box_w = grid_w
        fact_box_x0 = start_x
        fact_box_h = max(120, H - MARGIN - 54 - text_y)
        _badge_rect(
            draw,
            (fact_box_x0, text_y, fact_box_x0 + fact_box_w, text_y + fact_box_h),
            accent, shape=style["badge"], fill=(16, 18, 28), outline_w=2,
        )

        fact_hdr_font = load_font(FONT_BOLD_PATH, 16)
        fact_hdr = f"◈  ФАКТ О ЧИСЛЕ {tl['days']} СУТ.  ·  НАУКА / КОСМОС / РЕКОРДЫ  ◈"
        _draw_centered(draw, fact_hdr, fact_hdr_font, text_y + 14, color=accent, shadow=False)

        fact_body_font = load_font(FONT_REGULAR_PATH, 21)
        fact_lines = _wrap_text(draw, fact_text, fact_body_font, fact_box_w - 56)[:3]
        line_h = 27
        block_h = len(fact_lines) * line_h
        avail_h = fact_box_h - 86
        fy = text_y + 42 + max(0, (avail_h - block_h) // 2)
        for line in fact_lines:
            _draw_centered(draw, line, fact_body_font, fy, color=(228, 233, 245), shadow=True, so=2)
            fy += line_h

        # Нижняя техническая строка эквивалента времени внутри блока факта
        div_y = text_y + fact_box_h - 42
        draw.line([(fact_box_x0 + 36, div_y), (fact_box_x0 + fact_box_w - 36, div_y)], fill=(45, 52, 72), width=1)
        eq_font = load_font(FONT_BOLD_PATH, 15)
        if tl["done"]:
            eq_str = "ПОЛНЫЙ ЦИКЛ: 365 СУТОК   ·   8 760 ЧАСОВ   ·   52 НЕДЕЛИ   ·   100% ЗАВЕРШЕНО"
        else:
            hours_left_total = int(tl["total_seconds"] // 3600)
            weeks_left = tl["days"] / 7.0
            sats_left = max(0, (tl["days"] + 5) // 7)
            eq_str = f"ЭКВИВАЛЕНТ ОСТАТКА: {hours_left_total} ЧАСОВ   ·   {weeks_left:.1f} НЕД.   ·   СУББОТ: {sats_left}"
        _draw_centered(draw, eq_str, eq_font, div_y + 11, color=accent2, shadow=False)

        # ── 9. Футер ──────────────────────────────────────────────────────
        foot_font = load_font(FONT_BOLD_PATH, 15)
        foot_text = f"◈ ДМБ-ТАЙМЕР  ×  {theme_title}  ·  HUD v3.0 ({gauge_mode.upper()}) ◈"
        fx = _centered_x(draw, foot_text, foot_font)
        draw.text((fx, H - MARGIN - 24), foot_text, font=foot_font, fill=(95, 100, 125))

        _draw_noise(img, amount=6)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "JPEG", quality=94)
        return theme_key

    except Exception as e:
        print(f"❌ Ошибка генерации дембель-карточки ({theme_key}): {e}", file=sys.stderr, flush=True)
        _save_fallback(output_path, person, tl)
        return theme_key


# ══════════════════════════════════════════════
#  ОБЩАЯ СВОДНАЯ КАРТОЧКА-ДАШБОРД (Пункт 4)
# ══════════════════════════════════════════════
def make_dembel_summary_card(output_path: str, theme_key: str | None = None, now: datetime | None = None) -> str:
    """
    Рисует единый сводный HUD-дашборд (1080x1350) со всеми бойцами из DEMBEL_DATES:
    общий средний процент, сколько уже дома, кто следующий на выход и строка прогресса на каждого.
    """
    if theme_key not in THEMES:
        theme_key = random.choice(THEME_KEYS)

    now_dt = _now_msk(now)
    timers = get_all_timers(now_dt)

    theme = THEMES[theme_key]
    accent = theme["accent"]
    accent2 = get_accent2(accent)
    style_key, style = pick_style()

    full_bg = generate_background(theme_key, W, H, bg_style=random.choice(style["bg"]))
    blurred = full_bg.crop((0, 210, W, H)).filter(ImageFilter.GaussianBlur(16))
    img = full_bg.copy()
    img.paste(blurred, (0, 210))

    img_rgba = img.convert("RGBA")
    fade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fade)
    for y in range(130, H):
        t = min(1.0, (y - 130) / 250.0)
        fd.line([(0, y), (W, y)], fill=(DARK[0], DARK[1], DARK[2], int(215 * (t ** 1.15))))
    img_rgba.alpha_composite(fade)
    img = img_rgba.convert("RGB")

    _draw_glass_panel(
        img,
        (MARGIN + 10, 224, W - MARGIN - 10, H - MARGIN - 38),
        accent=accent, accent2=accent2,
        fill_alpha=195, radius=28, border_w=2,
    )

    draw = ImageDraw.Draw(img)
    _draw_corner_decorations(draw, accent, margin=26, style=style["corner"])
    if style["corner"] != "frame":
        _draw_panel_frame(draw, margin=16, accent=accent, cut=22, width=1)

    title_font = load_font(FONT_BOLD_PATH, 44)
    sub_font = load_font(FONT_BOLD_PATH, 20)
    _draw_glow_text(
        img,
        (_centered_x(draw, "СВОДНЫЙ ДАШБОРД ДМБ", title_font), 52),
        "СВОДНЫЙ ДАШБОРД ДМБ", title_font,
        fill=WHITE, glow_color=accent, blur_radius=10, glow_passes=1, outline_w=4,
    )
    draw = ImageDraw.Draw(img)

    done_count = sum(1 for t in timers if t["done"])
    avg_prog = sum(t["progress"] for t in timers) / max(1, len(timers)) * 100.0
    next_up = next((t for t in timers if not t["done"]), None)
    next_str = f"БЛИЖАЙШИЙ: {next_up['person'].upper()} ({next_up['days']}Д)" if next_up else "ВСЕ ДОМА!"
    summary_line = f"ОБЩИЙ ПРОГРЕСС: {avg_prog:.2f}%   ·   ДОМА: {done_count}/{len(timers)}   ·   {next_str}"
    _draw_centered(draw, summary_line, sub_font, 120, color=accent, shadow=True, so=2)
    _draw_neon_line(draw, MARGIN + 20, 164, W - MARGIN - 20, 164, accent, width=2, style=style["line"])

    date_hdr_font = load_font(FONT_BOLD_PATH, 17)
    _draw_centered(
        draw,
        f"АКТУАЛЬНО НА {now_dt.strftime('%d.%m.%Y %H:%M')} МСК   ·   СРОК СЛУЖБЫ: 365 ДНЕЙ (1 ГОД)",
        date_hdr_font, 184, color=(175, 182, 205), shadow=False,
    )

    row_y = 246
    row_h = 98
    row_gap = 11
    card_x0 = MARGIN + 34
    card_x1 = W - MARGIN - 34

    name_font = load_font(FONT_BOLD_PATH, 26)
    info_font = load_font(FONT_BOLD_PATH, 18)
    pct_font = load_font(FONT_BOLD_PATH, 19)

    for i, t in enumerate(timers):
        y0 = row_y + i * (row_h + row_gap)
        y1 = y0 + row_h
        is_done = t["done"]
        row_acc = GOLD_THEME["accent"] if is_done else (accent if i % 2 == 0 else accent2)
        row_acc2 = GOLD_THEME["accent2"] if is_done else accent2

        _badge_rect(
            draw, (card_x0, y0, card_x1, y1),
            row_acc, shape="cut" if style["badge"] == "cut" else "rect",
            fill=(18, 20, 32), outline_w=2,
        )
        draw.line([(card_x0 + 8, y0 + 10), (card_x0 + 8, y1 - 10)], fill=row_acc, width=4)

        p_clean = _clean_for_image(t["person"]) or t["person"]
        rank_lbl = t["rank"]["short"]
        draw.text((card_x0 + 24, y0 + 12), f"#{i + 1}  {p_clean}", font=name_font, fill=WHITE)

        meta_right = (
            f"★ ДОМА С {t['target'].strftime('%d.%m.%Y')}"
            if is_done
            else f"{rank_lbl}  ·  ДМБ {t['target'].strftime('%d.%m.%Y')}  ·  ОСТАЛОСЬ: {t['days']}Д {t['hours']:02d}Ч"
        )
        mb = draw.textbbox((0, 0), meta_right, font=info_font)
        draw.text((card_x1 - (mb[2] - mb[0]) - 22, y0 + 15), meta_right, font=info_font, fill=row_acc)

        bx0 = card_x0 + 24
        bx1 = card_x1 - 160
        by0 = y0 + 52
        by1 = by0 + 28
        _draw_gradient_bar(img, (bx0, by0, bx1, by1), t["progress"], row_acc, row_acc2, stripes=True)
        draw = ImageDraw.Draw(img)

        pct_str = "100.00%" if is_done else f"{t['progress'] * 100:.2f}%"
        pb = draw.textbbox((0, 0), pct_str, font=pct_font)
        draw.text((card_x1 - (pb[2] - pb[0]) - 22, by0 + 3), pct_str, font=pct_font, fill=WHITE)

    foot_font = load_font(FONT_BOLD_PATH, 15)
    foot_text = f"◈ СВОДКА ДМБ  ×  {theme['title']}  ·  HUD v3.0 ◈"
    draw.text((_centered_x(draw, foot_text, foot_font), H - MARGIN - 24), foot_text, font=foot_font, fill=(95, 100, 125))

    _draw_noise(img, amount=5)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    img.save(output_path, "JPEG", quality=94)
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