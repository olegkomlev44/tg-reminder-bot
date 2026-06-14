import requests
import random

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

WIDTH = 1080
HEIGHT = 1350

THEMES = [
    "dota 2 fantasy interface green dashboard",
    "counter strike 2 futuristic tactical hud",
    "genshin impact fantasy menu screen",
    "cyberpunk neon dashboard interface",
    "gaming terminal hacker interface"
]


def get_background():

    prompt = random.choice(THEMES)

    url = (
        f"https://image.pollinations.ai/prompt/{prompt}"
        "?width=1080&height=1350&nologo=true"
    )

    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()

        with open("background.png", "wb") as f:
            f.write(r.content)

        return "background.png"

    except Exception:
        img = Image.new("RGB", (1080, 1350), (20, 20, 30))
        img.save("background.png")
        return "background.png"



def progress_bar(draw, x, y, value, total):

    width = 500
    height = 35

    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=12,
        outline="white",
        width=3
    )

    fill = int(width * value / total)

    draw.rounded_rectangle(
        (x, y, x + fill, y + height),
        radius=12,
        fill=(0, 255, 120)
    )


def generate_card(
        svodki,
        proc,
        mood,
        proc_day,
        total_svodki,
        total_proc):

    bg_file = get_background()

    img = Image.open(bg_file).convert("RGBA")

    img = img.resize((WIDTH, HEIGHT))

    overlay = Image.new(
        "RGBA",
        (WIDTH, HEIGHT),
        (0, 0, 0, 130)
    )

    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)

    title_font = ImageFont.truetype(
        "DejaVuSans-Bold.ttf",
        64
    )

    text_font = ImageFont.truetype(
        "DejaVuSans.ttf",
        42
    )

    draw.text(
        (50, 50),
        "🎮 DUTY QUEST",
        fill="white",
        font=title_font
    )

    draw.text(
        (50, 180),
        f"📋 Сводки: {svodki}",
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 280),
        f"🏆 Выполнено: {total_svodki}",
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 450),
        f"🧹 Процедурка: {proc}",
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 550),
        f"🏆 Выполнено: {total_proc}",
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 720),
        f"🎭 Режим: {mood}",
        fill="white",
        font=text_font
    )

    draw.text(
        (50, 850),
        "⚡ Прогресс процедурки",
        fill="white",
        font=text_font
    )

    progress_bar(
        draw,
        50,
        930,
        proc_day,
        2
    )

    draw.text(
        (600, 920),
        f"{proc_day}/2",
        fill="white",
        font=text_font
    )

    output = "duty_card.png"

    img.convert("RGB").save(
        output,
        quality=95
    )

    return output
