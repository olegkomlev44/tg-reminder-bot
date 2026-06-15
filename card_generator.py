import requests
import random
import os
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

WIDTH = 1080
HEIGHT = 1350

# Расширенный список тем (25+ игр/стилей)
THEMES = [
    "dota 2 fantasy interface green dashboard",
    "counter strike 2 futuristic tactical hud",
    "genshin impact fantasy menu screen",
    "valorant neon character select screen",
    "overwatch 2 competitive scoreboard background",
    "warzone military command center",
    "fortnite cartoon battle pass lobby",
    "cyberpunk 2077 neon dashboard",
    "anime fantasy rpg quest board",
    "elden ring gothic dark fantasy parchment",
    "the witcher medieval bestiary background",
    "minecraft blocky crafting table style",
    "league of legends summoner rift interface",
    "apex legends high-tech arena terminal",
    "star wars imperial display hologram",
    "final fantasy crystal menu",
    "destiny 2 vanguard tactical display",
    "borderlands comic style vault interface",
    "dark souls bonfire fantasy parchment",
    "doom eternal hellish metal panel",
    "stardew valley cozy farm journal",
    "hollow knight hand-drawn bug kingdom",
    "persona 5 stylish red and black menu",
    "nier automata desolate sci-fi UI",
    "gaming terminal hacker interface",
]

def get_font(size: int):
    """Загружает шрифт DejaVuSans (если есть) или стандартный."""
    try:
        # Попробуем загрузить системный DejaVuSans (обычно есть в Linux)
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except:
        try:
            # Альтернативный путь (для Docker)
            return ImageFont.truetype("DejaVuSans.ttf", size)
        except:
            # Фоллбэк — встроенный шрифт (но он мелкий)
            return ImageFont.load_default()

def get_background() -> str:
    """Получает фон через Pollinations.ai, при ошибке — чёрный."""
    prompt = random.choice(THEMES)
    url = (
        f"https://image.pollinations.ai/prompt/{prompt}"
        "?width=1080&height=1350&nologo=true"
    )
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        img.save("background.png")
        return "background.png"
    except Exception as e:
        print(f"Pollinations error: {e}")
        img = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 30))
        img.save("background.png")
        return "background.png"

def draw_progress_bar(draw, x, y, current, total, width=500, height=35):
    """Рисует красивую полосу прогресса."""
    # Обводка
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=12,
        outline="white",
        width=3
    )
    # Заполнение
    fill_width = int(width * current / total) if total > 0 else 0
    if fill_width > 0:
        draw.rounded_rectangle(
            (x, y, x + fill_width, y + height),
            radius=12,
            fill=(0, 255, 120)
        )

def draw_text_with_outline(draw, text, position, font, text_color="white", outline_color="black", outline_width=2):
    """Рисует текст с чёрной обводкой для читаемости."""
    x, y = position
    # Обводка
    for dx in range(-outline_width, outline_width+1):
        for dy in range(-outline_width, outline_width+1):
            if dx != 0 or dy != 0:
                draw.text((x+dx, y+dy), text, font=font, fill=outline_color)
    # Основной текст
    draw.text((x, y), text, font=font, fill=text_color)

def make_reminder_card(duty_type, person, position_label, time_label, header_text, ending_text,
                       date_label, proc_day, next_person, next_days, total_duties, mood_label, output_path):
    """
    Генерирует карточку для одного напоминания (сводки или процедурка).
    """
    bg_file = get_background()
    img = Image.open(bg_file).convert("RGBA")
    img = img.resize((WIDTH, HEIGHT))

    # Полупрозрачная тёмная подложка для контраста текста
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 140))
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)

    # Шрифты
    font_title = get_font(58)
    font_header = get_font(42)
    font_normal = get_font(38)
    font_small = get_font(32)

    # Заголовок
    draw_text_with_outline(draw, "🎮 DUTY QUEST", (50, 50), font_title)

    # Дата и настроение
    draw_text_with_outline(draw, f"📅 {date_label}  |  🎭 {mood_label}", (50, 140), font_small)

    # Разделитель
    draw.line((50, 200, WIDTH-50, 200), fill="white", width=2)

    # Тип наряда (сводки / процедурка)
    duty_icon = "📋" if duty_type == "svodki" else "🧹"
    duty_title = "СВОДКИ" if duty_type == "svodki" else "ПРОЦЕДУРКА"
    draw_text_with_outline(draw, f"{duty_icon} {duty_title}  •  {time_label}", (50, 260), font_header)

    # Основная информация
    draw_text_with_outline(draw, f"{position_label}:", (50, 350), font_normal)
    draw_text_with_outline(draw, f"{person}", (80, 420), get_font(48))
    draw_text_with_outline(draw, f"🏆 Всего нарядов: {total_duties}", (50, 520), font_normal)

    if duty_type == "proc":
        # Прогресс-бар для процедурки
        draw_text_with_outline(draw, f"⚡ День {proc_day}/2", (50, 620), font_normal)
        draw_progress_bar(draw, 50, 680, proc_day, 2)
        draw_text_with_outline(draw, f"{proc_day}/2", (580, 675), font_small)

        y_next = 780
    else:
        y_next = 620

    # Следующий дежурный
    draw_text_with_outline(draw, f"⏭ Следующий:", (50, y_next), font_normal)
    draw_text_with_outline(draw, f"{next_person} (через {next_days} д.)", (80, y_next+50), font_normal)

    # Заголовок (если есть)
    if header_text:
        draw_text_with_outline(draw, f"📢 {header_text[:50]}", (50, y_next+130), font_small, outline_color="orange")

    # Концовка (настроение дня)
    draw_text_with_outline(draw, ending_text[:80], (50, HEIGHT-120), font_small, outline_color="lime")

    # Сохраняем
    img.convert("RGB").save(output_path, quality=90)

def make_daily_card(svodki_person, svodki_label, proc_person, proc_label, proc_day, date_label,
                    weekday_name, weekday_icon, svodki_next, svodki_next_days, proc_next, proc_next_days,
                    svodki_total, proc_total, mood_label, output_path):
    """
    Генерирует карточку дня (оба дежурных сразу).
    """
    bg_file = get_background()
    img = Image.open(bg_file).convert("RGBA")
    img = img.resize((WIDTH, HEIGHT))

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 140))
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)

    font_title = get_font(58)
    font_header = get_font(42)
    font_normal = get_font(38)
    font_small = get_font(32)

    # Заголовок и дата
    draw_text_with_outline(draw, "🎮 DUTY QUEST", (50, 50), font_title)
    draw_text_with_outline(draw, f"{weekday_icon} {weekday_name.upper()}  •  {date_label}", (50, 140), font_small)
    draw_text_with_outline(draw, f"🎭 {mood_label}", (50, 190), font_small)
    draw.line((50, 240, WIDTH-50, 240), fill="white", width=2)

    # ---- Сводки ----
    draw_text_with_outline(draw, "📋 СВОДКИ  •  18:00", (50, 300), font_header)
    draw_text_with_outline(draw, f"{svodki_label}:", (50, 380), font_normal)
    draw_text_with_outline(draw, f"{svodki_person}", (80, 440), get_font(48))
    draw_text_with_outline(draw, f"🏆 Всего нарядов: {svodki_total}", (50, 530), font_normal)
    draw_text_with_outline(draw, f"⏭ Следующий: {svodki_next} (через {svodki_next_days} д.)", (50, 600), font_normal)

    # ---- Процедурка ----
    draw_text_with_outline(draw, "🧹 ПРОЦЕДУРКА  •  22:00", (50, 700), font_header)
    draw_text_with_outline(draw, f"{proc_label}:", (50, 780), font_normal)
    draw_text_with_outline(draw, f"{proc_person}", (80, 840), get_font(48))
    draw_text_with_outline(draw, f"🏆 Всего нарядов: {proc_total}", (50, 930), font_normal)
    draw_text_with_outline(draw, f"⚡ День {proc_day}/2", (50, 1000), font_normal)
    draw_progress_bar(draw, 50, 1060, proc_day, 2)
    draw_text_with_outline(draw, f"{proc_day}/2", (580, 1055), font_small)
    draw_text_with_outline(draw, f"⏭ Следующий: {proc_next} (через {proc_next_days} д.)", (50, 1130), font_normal)

    # Сохраняем
    img.convert("RGB").save(output_path, quality=90)
