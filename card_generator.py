from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
import random

WIDTH = 1080
HEIGHT = 1350

BACKGROUNDS = [
    (30, 30, 40),
    (20, 50, 90),
    (50, 20, 80),
    (20, 80, 60),
]

def generate_card(
    svodki,
    proc,
    mood,
    proc_day
):
    bg = random.choice(BACKGROUNDS)

    img = Image.new("RGB", (WIDTH, HEIGHT), bg)
    draw = ImageDraw.Draw(img)

    title_font = ImageFont.truetype(
        "DejaVuSans-Bold.ttf",
        60
    )

    text_font = ImageFont.truetype(
        "DejaVuSans.ttf",
        40
    )

    draw.text(
        (50, 50),
        "НАРЯД НА СЕГОДНЯ",
        fill="white",
        font=title_font
    )

    draw.text(
        (50, 180),
        datetime.now().strftime("%d.%m.%Y"),
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 300),
        f"📋 Сводки: {svodki}",
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 400),
        f"🧹 Процедурка: {proc}",
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 500),
        f"🎭 Настроение: {mood}",
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 600),
        f"⚡ День: {proc_day}/2",
        fill="white",
        font=text_font
    )

    img.save("duty_card.png")

    return "duty_card.png"
