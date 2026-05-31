from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime

TOKEN = "8874089866:AAEoGd63Dm2DC6YNSQ29oO2zcUlVNI5zY1Y"

# Дата начала очередей
START_DATE = datetime(2026, 6, 1).date()

svodki = [
    "Саша",
    "Олег",
    "Максим",
    "Игорь",
    "Илья",
    "Глеба",
    "Слава",
    "Ильнар"
]

procedurka = [
    "Илья",
    "Слава",
    "Саша",
    "Игоооорь",
    "Глеба",
    "Ильнар"
]

# ID группы
CHAT_ID = -1001234567890


def get_message():
    today = datetime.now().date()
    days_passed = (today - START_DATE).days

    svodki_name = svodki[days_passed % len(svodki)]
    proc_name = procedurka[(days_passed // 2) % len(procedurka)]

    return f"""
🚨 ВНИМАНИЕ, СТАВОЧНИКИ 🚨

📦 Квест дня: «Сводки сами себя не отнесут»

🎖 Исполнитель:
🔥 {svodki_name}

━━━━━━━━━━━━━━━

🧽 Побочный квест: «Уничтожение Процедурки»

🎖 Исполнитель:
⚡ {proc_name}

━━━━━━━━━━━━━━━

🎯 Ежедневные задания выданы.
💸 Коэффициент на успешное выполнение: 1.01

🍀 Всем удачного захода в день!
"""


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_message())


async def send_daily(bot: Bot):
    await bot.send_message(
        chat_id=CHAT_ID,
        text=get_message()
    )


async def scheduled_job(application):
    await send_daily(application.bot)


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("today", today_command))

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        scheduled_job,
        "cron",
        hour=18,
        minute=0,
        args=[app]
    )
    scheduler.start()

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
