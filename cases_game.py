"""
cases_game.py — 🎰 КЕЙС-КЛУБ: игра с кейсами, монетами и соревнованием между игроками.

Игровой цикл:
  • У каждого игрока монеты 🪙 (старт 1000) и инвентарь предметов (скины CS, машины, ценные вещи, хлам).
  • Кейсы платные: Стартовый 100 · Геймерский 250 · Оружейный CS 400 · Стрит-хайп 800 · Гараж 1500 · Люкс 5000 · Мифик 20 000.
    В каталоге 270 предметов в 5 категориях: скины CS, машины и транспорт, ценности, стрит-хайп, геймерское.
    Шансы посчитаны так, чтобы в среднем кейс возвращал ~95% цены (RTP) — монеты медленно утекают,
    а удача и умение вовремя продавать решают, кто в топе.
  • Выпавший предмет можно продать за его цену (кнопка под дропом или в инвентаре) либо копить.
  • 🎁 Раз в день — бонус монет (серия дней даёт больше). 🆘 Если игрок «на нуле» (монет и стоимости
    инвентаря меньше цены самого дешёвого кейса) — гарантированное пособие (раз в 2 часа).
  • 🏆 Топ по капиталу (монеты + инвентарь) и ⚔️ БАТЛЫ: двое платят за один кейс, у кого дороже дроп —
    забирает оба предмета.

Дополнительно:
  • 🚀 АПГРЕЙД: рискнуть предметом ради более дорогого (шанс = 95% × цена/цена_цели — честный).
  • 🧾 КОНТРАКТ: 3–10 предметов → один (возврат всегда ниже 100%, обычно 88–96% суммы, с шансом на большой куш).
  • 🎟 Бесплатный кейс раз в день и 💥 «предложение дня» (−15% на один кейс, 1 раз в день).
  • 🎁 /give — подарки монет (комиссия 5%) и предметов, 🤝 /trade — обмен предмет на предмет.
  • 📅 Сезоны по 14 дней: топ-3 по ПРИРОСТУ капитала получают кубки (прогресс не сбрасывается),
    топ отдельно по чату. 🏅 Достижения и титулы.

Команды: /case (меню), /daily, /inv, /ctop, /give, /trade. Всё остальное — кнопками.
Данные: SQLite-файл (CASES_DB_PATH, по умолчанию cases_game.db рядом с модулем).
Подключение: register_cases_handlers(dp) в main.py.
"""

import asyncio
import bisect
import html
import json
import logging
import math
import os
import random
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.getenv("CASES_TZ", "Europe/Moscow"))
except Exception:                                    # нет tzdata — фиксированный МСК
    _TZ = timezone(timedelta(hours=3))

logger = logging.getLogger(__name__)
_RNG = secrets.SystemRandom()

# ── Настройки экономики ───────────────────────────────────────────────────────
START_COINS = 1000
DAILY_BASE = 500            # ежедневный бонус
DAILY_STEP = 100            # +100 за каждый день серии подряд
DAILY_STREAK_MAX = 6        # максимум +600 (итого 1100 на 7-й день серии)
BAILOUT = 300               # пособие «на нуле»
BAILOUT_COOLDOWN = 2 * 3600
RTP = 0.95                  # какую долю цены кейс возвращает в среднем (было 0.90 — шансы подняты)
CONFIRM_SELL_AT = 5000      # дороже этого — продажа с подтверждением
BATTLE_TTL = 20 * 60        # открытый батл «живёт» 20 минут, потом возврат ставки
INV_PER_PAGE = 6
SEASON_DAYS = 14            # длина сезона
DEAL_DISCOUNT = 0.10        # скидка «предложения дня» (1 раз в день); при RTP 95% −10% даёт то же +5% выгоды, что раньше −15%
UPGRADE_MULTS = (1.5, 2, 3, 5, 10, 25)
UPGRADE_MIN_CHANCE, UPGRADE_MAX_CHANCE = 0.01, 0.75
CONTRACT_MIN, CONTRACT_MAX = 3, 10
# (множитель к сумме, вероятность): сумма m·p ≈ 0.995 < 1, а предмет выдаётся не дороже цели — возврат всегда <100%
CONTRACT_OUTCOMES = [(0.4, 0.368), (0.8, 0.30), (1.2, 0.20), (2.0, 0.09), (4.0, 0.037), (8.0, 0.005)]
GIVE_MIN, GIVE_FEE, GIVE_DAILY_CAP = 10, 0.05, 20000     # подарки: минимум, комиссия, лимит в сутки (в 🪙-стоимости)
TRADE_TTL = 30 * 60
ANIM_TIMEOUT = 45.0         # анимация дольше — прерываем и показываем результат (чтобы не «висело»)
# Админы: только по числовому Telegram-ID. @BFF_Oleg — для справки (username можно сменить, поэтому по нему права не даём).
ADMIN_USERNAME = "BFF_Oleg"
ADMIN_IDS = {891298064} | {int(x) for x in re.split(r"[,\s]+", os.getenv("CASES_ADMIN_IDS", "")) if x.strip().lstrip("-").isdigit()}
COMMANDS = {"menu": ("case", "cases"), "daily": ("daily",), "inv": ("inv", "inventory"), "top": ("ctop",),
            "give": ("give",), "trade": ("trade",), "admin": ("cadmin",),
            "quests": ("quests", "quest"), "market": ("market",), "games": ("games",), "spin": ("spin",),
            "pass": ("pass",), "album": ("album",)}

# ── Каталог ───────────────────────────────────────────────────────────────────
RARITY = {
    "junk":   ("🗑", "Хлам"),
    "common": ("⚪", "Обычный"),
    "rare":   ("🔵", "Редкий"),
    "epic":   ("🟣", "Эпический"),
    "legend": ("🔴", "Легендарный"),
    "mythic": ("🟡", "Мифический"),
}
RARITY_ORDER = list(RARITY)

# (категория, редкость, ключ, название, цена продажи в 🪙). Ключ НЕ менять — он лежит в инвентарях.
_ITEMS_RAW = [
 ("cs","junk","cs_p250_sand","P250 | Sand Dune",22),("cs","junk","cs_nova_walnut","Nova | Walnut",30),("cs","junk","cs_sg_sheen","SG 553 | Army Sheen",38),
 ("cs","common","cs_mp9_bull","MP9 | Bulldozer",90),("cs","common","cs_famas_col","FAMAS | Colony",120),("cs","common","cs_dual_con","Dual Berettas | Contractor",150),("cs","common","cs_glock_water","Glock-18 | Water Elemental",190),
 ("cs","rare","cs_m4s_guard","M4A1-S | Guardian",450),("cs","rare","cs_usp_orion","USP-S | Orion",650),("cs","rare","cs_ak_redline","AK-47 | Redline",800),("cs","rare","cs_deagle_blaze","Desert Eagle | Blaze",950),
 ("cs","epic","cs_awp_asiimov","AWP | Asiimov",1800),("cs","epic","cs_ak_vulcan","AK-47 | Vulcan",2600),("cs","epic","cs_m4a4_neo","M4A4 | Neo-Noir",3200),("cs","epic","cs_glock_fade","Glock-18 | Fade",4200),
 ("cs","legend","cs_ak_fire","AK-47 | Fire Serpent",9000),("cs","legend","cs_kara_doppler","★ Karambit | Doppler",14000),("cs","legend","cs_awp_lore","AWP | Dragon Lore",28000),
 ("cs","mythic","cs_m4_howl","M4A4 | Howl",90000),("cs","mythic","cs_butterfly","★ Butterfly Knife | Fade",120000),("cs","mythic","cs_gloves","★ Sport Gloves | Pandora's Box",180000),

 ("car","junk","car_pine","Ёлочка-ароматизатор",12),("car","junk","car_wheelbike","Колесо от велосипеда",20),("car","junk","car_sign","Знак «Шипы»",32),
 ("car","common","car_vac","Автомобильный пылесос",110),("car","common","car_dvr","Видеорегистратор",160),("car","common","car_scooter","Самокат (б/у)",210),("car","common","car_tires","Комплект зимней резины",240),
 ("car","rare","car_granta","Lada Granta (б/у)",600),("car","rare","car_niva","Lada Niva",900),("car","rare","car_rapid","Skoda Rapid",1150),("car","rare","car_moto","Мотоцикл Yamaha",1300),
 ("car","epic","car_camry","Toyota Camry",4500),("car","epic","car_bmw3","BMW 3 Series",6000),("car","epic","car_tesla","Tesla Model 3",8500),
 ("car","legend","car_g63","Mercedes-AMG G63",24000),("car","legend","car_911","Porsche 911",38000),("car","legend","car_urus","Lamborghini Urus",45000),
 ("car","mythic","car_aventador","Lamborghini Aventador",150000),("car","mythic","car_laferrari","Ferrari LaFerrari",280000),("car","mythic","car_chiron","Bugatti Chiron",450000),

 ("lux","junk","lux_magnet","Магнит на холодильник",12),("lux","junk","lux_ticket","Просроченный билет в кино",18),("lux","junk","lux_piggy","Пустая копилка",25),
 ("lux","common","lux_pbank","Powerbank 20 000 мАч",130),("lux","common","lux_buds","Беспроводные наушники",180),("lux","common","lux_watch","Умные часы",260),("lux","common","lux_kb","Механическая клавиатура",300),
 ("lux","rare","lux_switch","Nintendo Switch",500),("lux","rare","lux_ps5","PlayStation 5",700),("lux","rare","lux_iphone","iPhone 16",850),("lux","rare","lux_gpu","Видеокарта RTX 4070",1100),
 ("lux","epic","lux_mac","MacBook Pro",3500),("lux","epic","lux_gold100","Золотой слиток 100 г",5500),("lux","epic","lux_rolex","Rolex Submariner",7000),
 ("lux","legend","lux_diamond","Бриллиант 5 карат",26000),("lux","legend","lux_flat","Квартира-студия",38000),("lux","legend","lux_bar","Золотой слиток 1 кг",42000),
 ("lux","mythic","lux_penthouse","Пентхаус в Москва-Сити",300000),("lux","mythic","lux_yacht","Яхта",420000),("lux","mythic","lux_island","Частный остров",600000),

 # ═══ расширение каталога (v3). Ключи/цены старых предметов не менялись ═══
 # ── CS: скины ──
 ("cs","junk","cs_mac10_silver","MAC-10 | Silver",18),("cs","junk","cs_p90_sand","P90 | Sand Spray",15),("cs","junk","cs_galil_sage","Galil AR | Sage Spray",20),("cs","junk","cs_ump_carbon","UMP-45 | Carbon Fiber",28),("cs","junk","cs_tec9_urban","Tec-9 | Urban DDPAT",34),
 ("cs","common","cs_mp7_army","MP7 | Army Recon",70),("cs","common","cs_aug_storm","AUG | Storm",85),("cs","common","cs_ssg_blood","SSG 08 | Blood in the Water",105),("cs","common","cs_p250_mehndi","P250 | Mehndi",130),("cs","common","cs_fiveseven_candy","Five-SeveN | Candy Apple",140),("cs","common","cs_xm_tranq","XM1014 | Tranquility",165),("cs","common","cs_mag7_bull","MAG-7 | Bulldozer",175),("cs","common","cs_cz75_dart","CZ75-Auto | Poison Dart",210),
 ("cs","rare","cs_ak_elite","AK-47 | Elite Build",410),("cs","rare","cs_m4s_decimator","M4A1-S | Decimator",510),("cs","rare","cs_awp_hive","AWP | Electric Hive",560),("cs","rare","cs_usp_caiman","USP-S | Caiman",590),("cs","rare","cs_p250_seeya","P250 | See Ya Later",700),("cs","rare","cs_glock_twilight","Glock-18 | Twilight Galaxy",760),("cs","rare","cs_dual_cobra","Dual Berettas | Cobra Strike",970),("cs","rare","cs_deagle_cobalt","Desert Eagle | Cobalt Disruption",1050),
 ("cs","epic","cs_awp_hyper","AWP | Hyper Beast",1800),("cs","epic","cs_m4s_cyrex","M4A1-S | Cyrex",1850),("cs","epic","cs_ak_neon","AK-47 | Neon Revolution",2250),("cs","epic","cs_deagle_print","Desert Eagle | Printstream",2750),("cs","epic","cs_awp_contain","AWP | Containment Breach",4100),("cs","epic","cs_usp_kill","USP-S | Kill Confirmed",4850),
 ("cs","legend","cs_m4s_knight","M4A1-S | Knight",9000),("cs","legend","cs_ak_arabesque","AK-47 | Gold Arabesque",13000),("cs","legend","cs_bayonet_fade","★ Bayonet | Fade",16000),("cs","legend","cs_awp_gungnir","AWP | Gungnir",29000),
 ("cs","mythic","cs_kara_sapphire","★ Karambit | Sapphire",105000),("cs","mythic","cs_m9_ruby","★ M9 Bayonet | Ruby",145000),
 # ── Машины и транспорт ──
 ("car","junk","car_wipers","Дворник (одна штука)",10),("car","junk","car_mat","Резиновый коврик",16),("car","junk","car_scraper","Скребок для льда",14),("car","junk","car_fuses","Набор предохранителей",24),("car","junk","car_fine","Штраф ГИБДД (оплаченный)",28),
 ("car","common","car_battery","Аккумулятор 60 Ач",110),("car","common","car_oil","Набор моторных масел",125),("car","common","car_navi","Автомобильный навигатор",140),("car","common","car_seat","Детское автокресло",160),("car","common","car_roofbox","Багажник на крышу",175),("car","common","car_compressor","Автокомпрессор",190),("car","common","car_fridge","Автохолодильник",240),("car","common","car_helmet","Мотошлем",280),
 ("car","rare","car_vespa","Скутер Vespa",520),("car","rare","car_atv","Квадроцикл",700),("car","rare","car_kopeyka","ВАЗ-2101 «Копейка»",480),("car","rare","car_logan","Renault Logan",780),("car","rare","car_solaris","Hyundai Solaris",1000),("car","rare","car_polo","Volkswagen Polo",1100),("car","rare","car_ebike","Электровелосипед",560),("car","rare","car_ninja","Мотоцикл Kawasaki Ninja",1450),
 ("car","epic","car_mazda3","Mazda 3",3200),("car","epic","car_audi_a4","Audi A4",4800),("car","epic","car_octavia_rs","Skoda Octavia RS",5200),("car","epic","car_mustang","Ford Mustang GT",7200),("car","epic","car_harley","Harley-Davidson Fat Boy",6500),("car","epic","car_jetski","Гидроцикл Sea-Doo",3900),("car","epic","car_defender","Land Rover Defender",8800),
 ("car","legend","car_cayenne","Porsche Cayenne Turbo",27000),("car","legend","car_m5","BMW M5 Competition",31000),("car","legend","car_rangerover","Range Rover Autobiography",33000),("car","legend","car_boat","Катер Sea Ray",48000),
 ("car","mythic","car_phantom","Rolls-Royce Phantom",200000),("car","mythic","car_heli","Вертолёт Robinson R44",365000),
 # ── Ценности: техника, часы, золото, недвижимость ──
 ("lux","junk","lux_stickers","Набор наклеек",10),("lux","junk","lux_pen","Гостиничная ручка",14),("lux","junk","lux_bag","Пакет из «Пятёрочки»",8),("lux","junk","lux_candle","Свеча «Морской бриз»",22),("lux","junk","lux_cable","Сломанная зарядка",16),
 ("lux","common","lux_speaker","Bluetooth-колонка",130),("lux","common","lux_mouse","Игровая мышь",165),("lux","common","lux_tablet","Планшет",270),("lux","common","lux_ereader","Электронная книга",210),("lux","common","lux_actioncam","Экшн-камера",290),("lux","common","lux_ssd","Внешний SSD 1 ТБ",180),("lux","common","lux_monitor","Монитор 27 дюймов",280),("lux","common","lux_headset","Игровая гарнитура",170),
 ("lux","rare","lux_airpods","AirPods Pro",480),("lux","rare","lux_drone","Дрон DJI Mini",710),("lux","rare","lux_galaxy","Samsung Galaxy S24",820),("lux","rare","lux_ipad","iPad Pro",1100),("lux","rare","lux_tv","OLED-телевизор 55 дюймов",1200),("lux","rare","lux_steamdeck","Steam Deck",550),("lux","rare","lux_dyson","Пылесос Dyson",440),("lux","rare","lux_awatch","Apple Watch Ultra",870),
 ("lux","epic","lux_macstudio","Mac Studio",3800),("lux","epic","lux_omega","Omega Speedmaster",5600),("lux","epic","lux_leica","Камера Leica M11",7400),("lux","epic","lux_tag","TAG Heuer Carrera",3450),("lux","epic","lux_gold500","Золотой слиток 500 г",7800),("lux","epic","lux_xdr","Apple Pro Display XDR",5250),("lux","epic","lux_vision","Apple Vision Pro",3250),
 ("lux","legend","lux_patek","Patek Philippe Nautilus",36000),("lux","legend","lux_ap","Audemars Piguet Royal Oak",31000),("lux","legend","lux_emerald","Изумруд 20 карат",22000),("lux","legend","lux_cottage","Дом в Подмосковье",47000),
 ("lux","mythic","lux_villa","Вилла на Лазурном берегу",355000),("lux","mythic","lux_pink","Розовый бриллиант",495000),
 # ── Стрит-хайп: одежда, кроссовки, сумки ──
 ("fash","junk","fash_socks","Носки с дыркой",8),("fash","junk","fash_laces","Шнурки (одна пара)",12),("fash","junk","fash_cap_market","Кепка с рынка",20),("fash","junk","fash_tee_sochi","Футболка «Я ♥ Сочи»",26),("fash","junk","fash_tote","Эко-сумка",18),("fash","junk","fash_flipflops","Шлёпанцы",30),("fash","junk","fash_plastic_glasses","Пластиковые очки",22),("fash","junk","fash_fan_scarf","Шарф болельщика",35),
 ("fash","common","fash_hoodie","Худи no-name",90),("fash","common","fash_levis","Джинсы Levi's",130),("fash","common","fash_vans","Vans Old Skool",160),("fash","common","fash_converse","Converse Chuck Taylor",150),("fash","common","fash_uniqlo","Футболка Uniqlo",80),("fash","common","fash_herschel","Рюкзак Herschel",140),("fash","common","fash_nb530","New Balance 530",260),("fash","common","fash_samba","Adidas Samba",240),("fash","common","fash_puma","Puma Suede",170),("fash","common","fash_yankees","Кепка New York Yankees",120),("fash","common","fash_belt","Кожаный ремень",100),("fash","common","fash_carhartt","Шапка Carhartt",110),
 ("fash","rare","fash_dunk_panda","Nike Dunk Low Panda",450),("fash","rare","fash_stone","Куртка Stone Island",700),("fash","rare","fash_cdg","Футболка Comme des Garçons",520),("fash","rare","fash_nuptse","The North Face Nuptse",900),("fash","rare","fash_yeezy350","Yeezy Boost 350",1000),("fash","rare","fash_supreme_box","Supreme Box Logo Hoodie",1300),("fash","rare","fash_rayban","Ray-Ban Aviator",420),("fash","rare","fash_gucci_belt","Ремень Gucci",1100),("fash","rare","fash_offwhite","Футболка Off-White",650),("fash","rare","fash_bape","BAPE Shark Hoodie",1200),("fash","rare","fash_aj4","Air Jordan 4 Military Black",1400),("fash","rare","fash_martens","Dr. Martens 1460",480),
 ("fash","epic","fash_aj1_chicago","Air Jordan 1 Chicago",2200),("fash","epic","fash_lv_neverfull","Сумка Louis Vuitton Neverfull",3500),("fash","epic","fash_yeezy700","Yeezy Boost 700",1900),("fash","epic","fash_moncler","Пуховик Moncler",3200),("fash","epic","fash_dior_b23","Dior B23",2800),("fash","epic","fash_balenciaga","Balenciaga Triple S",2400),("fash","epic","fash_hermes_scarf","Платок Hermès",4200),("fash","epic","fash_goose","Canada Goose Expedition",2000),("fash","epic","fash_travis_aj","Travis Scott × Jordan 1 Low",6800),("fash","epic","fash_prada_nylon","Рюкзак Prada Re-Nylon",3000),
 ("fash","legend","fash_birkin30","Hermès Birkin 30",14000),("fash","legend","fash_chanel_flap","Chanel Classic Flap",11000),("fash","legend","fash_air_mag","Nike Air Mag («Назад в будущее»)",38000),("fash","legend","fash_sable_coat","Шуба из соболя",21000),("fash","legend","fash_kaws","KAWS Companion",26000),("fash","legend","fash_bearbrick","Bearbrick 1000%",9500),("fash","legend","fash_lv_supreme_trunk","Чемодан Louis Vuitton × Supreme",32000),
 ("fash","mythic","fash_birkin_himalaya","Birkin Himalaya (крокодил)",190000),("fash","mythic","fash_aj1_dior","Air Jordan 1 × Dior",110000),("fash","mythic","fash_chanel_necklace","Колье Chanel Haute Joaillerie",240000),("fash","mythic","fash_dior_couture","Платье Dior Haute Couture",150000),("fash","mythic","fash_chinchilla","Пальто из шиншиллы",95000),
 # ── Геймерское и коллекционное ──
 ("game","junk","game_tetris_cd","Диск «Тетрис» (поцарапан)",12),("game","junk","game_joystick","Сломанный джойстик",18),("game","junk","game_mousepad","Коврик для мыши",24),("game","junk","game_steamkey","Steam-ключ (уже активирован)",8),("game","junk","game_aa","Батарейки AA (севшие)",10),("game","junk","game_hdmi","HDMI-кабель",28),("game","junk","game_poster","Постер с киберспортсменом",16),("game","junk","game_poke_stickers","Наклейки с покемонами",20),
 ("game","common","game_xbox_pad","Геймпад Xbox",170),("game","common","game_kb_membrane","Мембранная клавиатура",90),("game","common","game_deskmat","Игровой коврик XL",110),("game","common","game_steamcard","Подарочная карта Steam",200),("game","common","game_ssd500","SSD 500 ГБ",220),("game","common","game_webcam","Веб-камера Full HD",150),("game","common","game_chair_budget","Игровое кресло (бюджетное)",300),("game","common","game_fifa","Игра FIFA (диск)",120),("game","common","game_lego_tech","Набор LEGO Technic",260),("game","common","game_funko","Фигурка Funko Pop",100),("game","common","game_usb_mic","USB-микрофон",180),("game","common","game_ps_pad","Геймпад DualSense",160),
 ("game","rare","game_switch_oled","Nintendo Switch OLED",650),("game","rare","game_rtx3060","Видеокарта RTX 3060",600),("game","rare","game_secretlab","Кресло Secretlab",700),("game","rare","game_monitor144","Монитор 144 Гц",520),("game","rare","game_xsx","Xbox Series X",900),("game","rare","game_quest3","Meta Quest 3",1000),("game","rare","game_rog_ally","ASUS ROG Ally",1100),("game","rare","game_gpro","Мышь Logitech G Pro X Superlight",450),("game","rare","game_wooting","Клавиатура Wooting 60HE",560),("game","rare","game_falcon","LEGO Star Wars Millennium Falcon",1400),("game","rare","game_pc_mid","Игровой ПК (средний)",1350),("game","rare","game_amiibo","Коллекция amiibo",480),
 ("game","epic","game_pc_4080","Игровой ПК RTX 4080",4200),("game","epic","game_rtx4090","Видеокарта RTX 4090",3600),("game","epic","game_oled_mon","OLED-монитор 34 дюйма",3200),("game","epic","game_simrig","Гоночный симулятор (руль и кресло)",5200),("game","epic","game_poke_box","Бустер-бокс Pokémon 1999",2800),("game","epic","game_nes_box","NES в коробке (1985)",4500),("game","epic","game_gb_red","Game Boy + Pokémon Red (запечатан)",6200),("game","epic","game_arcade","Аркадный автомат",7200),("game","epic","game_valve_index","VR-комплект Valve Index",2400),("game","epic","game_major_cup","Кубок Major (реплика)",3900),
 ("game","legend","game_mario_sealed","Super Mario Bros. (запечатан)",18000),("game","legend","game_charizard","Pokémon Charizard 1st Edition",24000),("game","legend","game_zelda_gold","Золотой картридж The Legend of Zelda",16000),("game","legend","game_pong","Atari Pong (оригинал)",12000),("game","legend","game_pacman","Автомат Pac-Man (коллекционный)",9800),("game","legend","game_ps1_devkit","Dev-kit PlayStation 1",30000),("game","legend","game_ti_ring","Чемпионский перстень The International",40000),
 ("game","mythic","game_ps_proto","Nintendo PlayStation (прототип)",400000),("game","mythic","game_pikachu_ill","Pokémon Pikachu Illustrator",650000),("game","mythic","game_mario_wata","Super Mario Bros. WATA 9.8",260000),("game","mythic","game_gold_pad","Золотой геймпад (24 карата)",120000),("game","mythic","game_odyssey","Magnavox Odyssey (оригинал)",95000),
]

ITEMS: dict[str, dict] = {k: {"name": n, "price": p, "rarity": r, "cat": c} for c, r, k, n, p in _ITEMS_RAW}
assert len(ITEMS) == len(_ITEMS_RAW), "в каталоге повторяются ключи предметов"

# Кейсы: тиры выше «sink» задаются относительными весами (shape); sink (самый частый тир) добирает остаток,
# а общий масштаб подбирается автоматически так, чтобы средняя выплата = RTP × цена.
CASES = {
    "starter": dict(icon="🎁", name="Стартовый", price=100, cats=("cs", "car", "lux", "fash", "game"), sink="junk",
                    shape={"common": 1.0, "rare": 0.2},
                    desc="всё понемногу: хлам, обычное, иногда редкое"),
    "game":    dict(icon="🎮", name="Геймерский", price=250, cats=("game",), sink="junk",
                    shape={"common": 1.0, "rare": 0.4, "epic": 0.07, "legend": 0.006, "mythic": 0.0004},
                    desc="геймпады, видеокарты, ретро-консоли и коллекционные карточки"),
    "cs":      dict(icon="🔫", name="Оружейный CS", price=400, cats=("cs",), sink="junk",
                    shape={"common": 1.0, "rare": 0.55, "epic": 0.15, "legend": 0.015, "mythic": 0.0015},
                    desc="скины из CS: от Sand Dune до Howl и Sapphire"),
    "fash":    dict(icon="👟", name="Стрит-хайп", price=800, cats=("fash",), sink="junk",
                    shape={"common": 1.0, "rare": 0.6, "epic": 0.15, "legend": 0.015, "mythic": 0.0015},
                    desc="кроссовки, худи, сумки — от носков до Birkin"),
    "garage":  dict(icon="🚗", name="Гараж", price=1500, cats=("car",), sink="junk",
                    shape={"common": 1.0, "rare": 0.6, "epic": 0.18, "legend": 0.02, "mythic": 0.002},
                    desc="от ёлочки до Bugatti, катеров и вертолёта"),
    "lux":     dict(icon="💎", name="Люкс", price=5000, cats=("lux",), sink="common",
                    shape={"rare": 1.0, "epic": 0.5, "legend": 0.07, "mythic": 0.006},
                    desc="гаджеты, золото, бриллианты, недвижимость"),
    "mythic":  dict(icon="🌌", name="Мифик", price=20000, cats=("cs", "car", "lux", "fash", "game"), sink="rare",
                    shape={"epic": 1.0, "legend": 0.9, "mythic": 0.06},
                    desc="только дорогое: эпик, легенда, миф. Для смелых"),
    # бесплатный кейс дня: цена 0, средняя выплата задана напрямую (ev_target), в магазине не продаётся
    "free":    dict(icon="🎟", name="Бесплатный кейс дня", price=0, cats=("cs", "car", "lux", "fash", "game"), sink="junk",
                    shape={"common": 1.0, "rare": 0.25, "epic": 0.03}, ev_target=85, free=True,
                    desc="раз в день бесплатно — хлам, обычное, иногда редкое"),
}
SHOP = [k for k, c in CASES.items() if not c.get("free")]
MIN_PRICE = min(CASES[k]["price"] for k in SHOP)


def _avg_price(cats, rarity) -> float | None:
    ps = [v["price"] for v in ITEMS.values() if v["cat"] in cats and v["rarity"] == rarity]
    return sum(ps) / len(ps) if ps else None


def _build_cases():
    for key, c in CASES.items():
        pool = {r: [k for k, v in ITEMS.items() if v["cat"] in c["cats"] and v["rarity"] == r] for r in RARITY_ORDER}
        sink, shape = c["sink"], c["shape"]
        p_sink = _avg_price(c["cats"], sink)
        gain = sum(w * (_avg_price(c["cats"], t) - p_sink) for t, w in shape.items())
        alpha = (c.get("ev_target", RTP * c["price"]) - p_sink) / gain
        weights = {t: alpha * w for t, w in shape.items()}
        weights[sink] = 1 - sum(weights.values())
        assert weights[sink] > 0 and all(pool[t] for t in weights), f"кейс {key}: не сходится экономика"
        c["key"], c["pool"], c["weights"] = key, pool, weights
        c["ev"] = sum(weights[t] * _avg_price(c["cats"], t) for t in weights)


_build_cases()


def roll_item(case_key: str) -> str:
    """Криптостойкий ГСЧ: сначала редкость по весам кейса, затем предмет внутри редкости."""
    c = CASES[case_key]
    tiers = list(c["weights"])
    r = _RNG.random()
    acc = 0.0
    for t in tiers:
        acc += c["weights"][t]
        if r < acc:
            return _RNG.choice(c["pool"][t])
    return _RNG.choice(c["pool"][tiers[-1]])


_TROPHY = {1: ("mythic", 30000), 2: ("legend", 15000), 3: ("epic", 8000)}


def item_info(key: str) -> dict:
    it = ITEMS.get(key)
    if it:
        return it
    if key.startswith("trophy:"):                      # кубок сезона: trophy:<сезон>:<место>
        try:
            _, season, place = key.split(":")
            r, p = _TROPHY[int(place)]
            return {"name": f"Кубок сезона {season} · {place} место", "price": p, "rarity": r, "cat": "trophy"}
        except (ValueError, KeyError):
            pass
    if key.startswith("pass:"):                        # награда премиум-пропуска: pass:<сезон>
        return {"name": f"Награда пропуска сезона {key.split(':')[1]}", "price": 2000, "rarity": "epic", "cat": "trophy"}
    return {"name": "Неизвестный предмет", "price": 0, "rarity": "junk", "cat": "?"}


def _fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _e(s) -> str:
    return html.escape(str(s))


def _it(key: str) -> str:
    i = item_info(key)
    return f"{RARITY[i['rarity']][0]} <b>{_e(i['name'])}</b>"


# ── SQLite ────────────────────────────────────────────────────────────────────
_DB_PATH = os.getenv("CASES_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_game.db")
_conn: sqlite3.Connection | None = None
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, name TEXT, coins INTEGER NOT NULL, last_daily TEXT DEFAULT '', streak INTEGER DEFAULT 0,
  last_bailout REAL DEFAULT 0, opened INTEGER DEFAULT 0, spent INTEGER DEFAULT 0, earned INTEGER DEFAULT 0,
  best_item TEXT DEFAULT '', best_price INTEGER DEFAULT 0, bw INTEGER DEFAULT 0, bl INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS inventory(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, item TEXT NOT NULL, case_key TEXT, got_at REAL);
CREATE INDEX IF NOT EXISTS inv_user ON inventory(user_id);
CREATE TABLE IF NOT EXISTS battles(
  id TEXT PRIMARY KEY, chat_id INTEGER, message_id INTEGER, creator INTEGER, creator_name TEXT,
  case_key TEXT, price INTEGER, created REAL, status TEXT);
CREATE TABLE IF NOT EXISTS achievements(user_id INTEGER, key TEXT, got_at REAL, PRIMARY KEY(user_id, key));
CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS basket(user_id INTEGER, inv_id INTEGER, PRIMARY KEY(user_id, inv_id));
CREATE TABLE IF NOT EXISTS chat_members(chat_id INTEGER, user_id INTEGER, seen REAL, PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, to_id INTEGER,
  kind TEXT, value INTEGER, ts REAL);
CREATE INDEX IF NOT EXISTS tr_from ON transfers(from_id, ts);
CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, chat_id INTEGER, message_id INTEGER, a INTEGER, a_name TEXT,
  b INTEGER, b_name TEXT, a_inv INTEGER, b_inv INTEGER, created REAL, status TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS season_snap(season INTEGER, user_id INTEGER, base INTEGER, PRIMARY KEY(season, user_id));
CREATE TABLE IF NOT EXISTS season_results(season INTEGER, place INTEGER, user_id INTEGER, name TEXT, growth INTEGER,
  PRIMARY KEY(season, place));
"""

# колонки, добавленные после первой версии: старые базы мигрируют на лету (данные сохраняются)
_NEW_USER_COLS = {
    "username": "TEXT DEFAULT ''", "last_free": "TEXT DEFAULT ''", "last_deal": "TEXT DEFAULT ''",
    "title": "TEXT DEFAULT ''", "contracts": "INTEGER DEFAULT 0", "up_win": "INTEGER DEFAULT 0",
    "up_try": "INTEGER DEFAULT 0", "lucky_up": "INTEGER DEFAULT 0", "bstreak": "INTEGER DEFAULT 0",
    "bstreak_best": "INTEGER DEFAULT 0", "trades": "INTEGER DEFAULT 0", "gifts": "INTEGER DEFAULT 0",
    "bailouts": "INTEGER DEFAULT 0", "best_sale": "INTEGER DEFAULT 0", "seasons_top": "INTEGER DEFAULT 0",
    "seasons_win": "INTEGER DEFAULT 0",
    # v5: уровни, билеты, спин, квесты, рынок, игры, альбом, пропуск
    "xp": "INTEGER DEFAULT 0", "tickets": "INTEGER DEFAULT 0", "spin_n": "INTEGER DEFAULT 1", "spin_ts": "REAL DEFAULT 0",
    "quests_done": "INTEGER DEFAULT 0", "market_sales": "INTEGER DEFAULT 0", "market_buys": "INTEGER DEFAULT 0",
    "game_plays": "INTEGER DEFAULT 0", "game_wins": "INTEGER DEFAULT 0", "best_mines": "INTEGER DEFAULT 0",
    "best_crash": "INTEGER DEFAULT 0", "spin_jp": "INTEGER DEFAULT 0", "pass_prem": "INTEGER DEFAULT 0",
    "pass_max": "INTEGER DEFAULT 0", "col_done": "INTEGER DEFAULT 0",
    "xp_day": "TEXT DEFAULT ''", "xp_social": "INTEGER DEFAULT 0",
}


_SCHEMA_ADMIN = """
CREATE TABLE IF NOT EXISTS admin_log(id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, target_id INTEGER,
  action TEXT, detail TEXT, ts REAL);
"""


_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS quests(user_id INTEGER, period TEXT, pkey TEXT, idx INTEGER, qid TEXT, event TEXT, target INTEGER,
  progress INTEGER DEFAULT 0, coins INTEGER, xp INTEGER, tickets INTEGER, claimed INTEGER DEFAULT 0,
  PRIMARY KEY(user_id, period, pkey, idx));
CREATE TABLE IF NOT EXISTS season_xp(season INTEGER, user_id INTEGER, xp INTEGER DEFAULT 0, PRIMARY KEY(season, user_id));
CREATE TABLE IF NOT EXISTS market(id INTEGER PRIMARY KEY AUTOINCREMENT, seller INTEGER, seller_name TEXT, item TEXT,
  price INTEGER, created REAL, status TEXT, buyer INTEGER, closed REAL);
CREATE INDEX IF NOT EXISTS market_status ON market(status, created);
CREATE INDEX IF NOT EXISTS market_seller ON market(seller, status);
CREATE TABLE IF NOT EXISTS album(user_id INTEGER, item TEXT, first_at REAL, PRIMARY KEY(user_id, item));
CREATE TABLE IF NOT EXISTS col_claims(user_id INTEGER, col TEXT, claimed_at REAL, PRIMARY KEY(user_id, col));
CREATE TABLE IF NOT EXISTS pass_claims(user_id INTEGER, season INTEGER, tier INTEGER, track TEXT, claimed_at REAL,
  PRIMARY KEY(user_id, season, tier, track));
CREATE TABLE IF NOT EXISTS pass_premium(user_id INTEGER, season INTEGER, bought_at REAL, PRIMARY KEY(user_id, season));
CREATE TABLE IF NOT EXISTS game_state(user_id INTEGER PRIMARY KEY, game TEXT, bet INTEGER, data TEXT, started REAL);
"""


def _migrate(conn: sqlite3.Connection):
    have = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col, decl in _NEW_USER_COLS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
    if "xp" not in have:                  # старым игрокам — опыт по прошлой активности, чтобы они не начинали с нуля
        conn.execute("UPDATE users SET xp = MIN(30000, opened*12 + bw*35 + up_try*10 + contracts*15 + trades*20 + gifts*8)")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        _conn.executescript(_SCHEMA_ADMIN)
        had_album = _conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='album'").fetchone()
        _conn.executescript(_SCHEMA_V5)
        if not had_album:                 # альбом старых игроков = то, что лежит у них в инвентаре сейчас
            _conn.execute("INSERT OR IGNORE INTO album(user_id,item,first_at) SELECT user_id,item,got_at FROM inventory")
    return _conn


@contextmanager
def _tx():
    c = _db()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _ensure(c, uid: int, name: str | None = None, now: float | None = None) -> dict:
    row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        c.execute("INSERT INTO users(user_id,name,coins,created) VALUES(?,?,?,?)",
                  (uid, name or str(uid), START_COINS, now or time.time()))
        no = _meta_get(c, "season_no")
        if no is not None:                                   # новичок стартует сезон с чистого листа
            c.execute("INSERT OR IGNORE INTO season_snap VALUES(?,?,?)", (int(no), uid, START_COINS))
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    elif name and row["name"] != name:
        c.execute("UPDATE users SET name=? WHERE user_id=?", (name, uid))
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return dict(row)


def _inv_stats(c, uid: int) -> tuple[int, int]:
    """(предметов в инвентаре; стоимость инвентаря + предметов, выставленных на рынок — они «заморожены», но твои)"""
    cnt = val = 0
    for r in c.execute("SELECT item, COUNT(*) n FROM inventory WHERE user_id=? GROUP BY item", (uid,)):
        cnt += r["n"]
        val += r["n"] * item_info(r["item"])["price"]
    return cnt, val + _market_escrow(c, uid)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, _TZ).date().isoformat()


def _secs_to_midnight(ts: float) -> int:
    d = datetime.fromtimestamp(ts, _TZ)
    nxt = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - d).total_seconds()))


def _human(secs: int) -> str:
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h} ч {m} мин" if h else f"{max(m, 1)} мин"


def get_profile(uid: int, name: str | None = None, now: float | None = None, username: str | None = None) -> dict:
    """Профиль + возврат ставок по просроченным батлам + ленивое закрытие сезона. Создаёт игрока при первом обращении."""
    now = now or time.time()
    with _tx() as c:
        _rollover(c, now)
        _ensure(c, uid, name, now)
        if username is not None:
            c.execute("UPDATE users SET username=? WHERE user_id=?", (username.lower().lstrip("@"), uid))
        refunded = 0
        for b in c.execute("SELECT * FROM battles WHERE creator=? AND status='open' AND created<?",
                           (uid, now - BATTLE_TTL)).fetchall():
            c.execute("UPDATE battles SET status='expired' WHERE id=?", (b["id"],))
            c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (b["price"], uid))
            refunded += b["price"]
        _market_expire(c, now)
        _game_cleanup(c, uid, now)
        u = _ensure(c, uid)
        cnt, val = _inv_stats(c, uid)
        lots = c.execute("SELECT COUNT(*) n FROM market WHERE seller=? AND status='open'", (uid,)).fetchone()["n"]
    u.update(inv_count=cnt, inv_value=val, networth=u["coins"] + val, refunded=refunded, level=level_of(u["xp"]), lots=lots)
    return u


def daily_status(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
    if u["last_daily"] == _day(now):
        return {"ready": False, "wait": _secs_to_midnight(now), "streak": u["streak"]}
    return {"ready": True, "wait": 0, "streak": u["streak"]}


def claim_daily(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    today = _day(now)
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        if u["last_daily"] == today:
            return {"ok": False, "wait": _secs_to_midnight(now), "streak": u["streak"]}
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        streak = u["streak"] + 1 if u["last_daily"] == yesterday else 1
        amount = DAILY_BASE + DAILY_STEP * min(streak - 1, DAILY_STREAK_MAX)
        c.execute("UPDATE users SET coins=coins+?, last_daily=?, streak=? WHERE user_id=?", (amount, today, streak, uid))
        _emit(c, uid, "daily", 1, 0, now)
        _check_ach(c, uid, now)
    return {"ok": True, "amount": amount, "streak": streak}


def maybe_bailout(uid: int, now: float | None = None) -> dict:
    """Гарантия «на нуле»: монет и стоимости инвентаря меньше самого дешёвого кейса → пособие (раз в 2 часа)."""
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        _, val = _inv_stats(c, uid)
        if u["coins"] >= MIN_PRICE or val >= MIN_PRICE:
            return {"given": 0, "broke": False}
        since = now - u["last_bailout"]
        if since < BAILOUT_COOLDOWN:
            return {"given": 0, "broke": True, "wait": int(BAILOUT_COOLDOWN - since)}
        c.execute("UPDATE users SET coins=coins+?, last_bailout=?, bailouts=bailouts+1 WHERE user_id=?", (BAILOUT, now, uid))
        _check_ach(c, uid, now)
    return {"given": BAILOUT, "broke": True}


def _record_drop(c, uid: int, case_key: str, item: str, now: float) -> int:
    cur = c.execute("INSERT INTO inventory(user_id,item,case_key,got_at) VALUES(?,?,?,?)", (uid, item, case_key, now))
    c.execute("INSERT OR IGNORE INTO album(user_id,item,first_at) VALUES(?,?,?)", (uid, item, now))
    return cur.lastrowid


def _update_best(c, uid: int, item: str):
    p = item_info(item)["price"]
    c.execute("UPDATE users SET best_item=?, best_price=? WHERE user_id=? AND best_price<?", (item, p, uid, p))


def open_case(uid: int, case_key: str, times: int = 1, now: float | None = None, deal: bool = False) -> dict:
    """Покупка и открытие. deal=True — «предложение дня»: скидка, 1 раз в день, только ×1."""
    now = now or time.time()
    case = CASES.get(case_key)
    if case is None or case.get("free"):
        return {"ok": False, "reason": "bad", "need": 0}
    today = _day(now)
    if deal:
        if case_key != deal_case(now) or times != 1:
            return {"ok": False, "reason": "bad", "need": 0}
        unit = deal_price(case_key)
    else:
        unit = case["price"]
    price = unit * times
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        if deal and u["last_deal"] == today:
            return {"ok": False, "reason": "deal_used", "need": 0}
        if u["coins"] < price:
            return {"ok": False, "reason": "money", "need": price - u["coins"], "coins": u["coins"]}
        c.execute("UPDATE users SET coins=coins-?, spent=spent+?, opened=opened+? WHERE user_id=?",
                  (price, price, times, uid))
        if deal:
            c.execute("UPDATE users SET last_deal=? WHERE user_id=?", (today, uid))
        drops = []
        for _ in range(times):
            item = roll_item(case_key)
            drops.append((item, _record_drop(c, uid, case_key, item, now)))
            _update_best(c, uid, item)
        _emit(c, uid, "open", times, case["price"], now)
        _check_ach(c, uid, now)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "drops": drops, "coins": coins, "cost": price}


def sell_item(uid: int, inv_id: int) -> dict:
    with _tx() as c:
        row = c.execute("SELECT * FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None:
            return {"ok": False}
        price = item_info(row["item"])["price"]
        _drop_inv(c, inv_id)
        c.execute("UPDATE users SET coins=coins+?, earned=earned+?, best_sale=MAX(best_sale, ?) WHERE user_id=?",
                  (price, price, price, uid))
        _emit(c, uid, "sell", 1, price)
        _check_ach(c, uid)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "price": price, "item": row["item"], "coins": coins}


_BULK = {
    "junk": ("🗑 Хлам", {"junk"}),
    "cheap": ("⚪🔵 Всё до редкого", {"junk", "common", "rare"}),
    "all": ("💎 Вообще всё", set(RARITY_ORDER)),
}


def bulk_preview(uid: int, mode: str) -> tuple[int, int]:
    rs = _BULK[mode][1]
    rows = [r for r in _db().execute("SELECT item FROM inventory WHERE user_id=?", (uid,)) if item_info(r["item"])["rarity"] in rs]
    return len(rows), sum(item_info(r["item"])["price"] for r in rows)


def sell_bulk(uid: int, mode: str) -> dict:
    rs = _BULK[mode][1]
    with _tx() as c:
        rows = [r for r in c.execute("SELECT id, item FROM inventory WHERE user_id=?", (uid,)).fetchall()
                if item_info(r["item"])["rarity"] in rs]
        total = sum(item_info(r["item"])["price"] for r in rows)
        for r in rows:
            _drop_inv(c, r["id"])
        if total:
            c.execute("UPDATE users SET coins=coins+?, earned=earned+? WHERE user_id=?", (total, total, uid))
            _emit(c, uid, "sell", len(rows), total)
            _check_ach(c, uid)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "count": len(rows), "total": total, "coins": coins}


def inventory_page(uid: int, page: int, asc: bool = False, no_trophy: bool = False) -> dict:
    rows = _db().execute("SELECT id, item FROM inventory WHERE user_id=?", (uid,)).fetchall()
    if no_trophy:
        rows = [r for r in rows if not _is_trophy(r["item"])]
    sign = 1 if asc else -1
    rows = sorted(rows, key=lambda r: (sign * item_info(r["item"])["price"], item_info(r["item"])["name"], r["id"]))
    pages = max(1, -(-len(rows) // INV_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * INV_PER_PAGE:(page + 1) * INV_PER_PAGE]
    return {"items": [(r["id"], r["item"]) for r in chunk], "page": page, "pages": pages, "total": len(rows)}


def leaderboard(limit: int | None = 10, chat_id: int | None = None) -> list[dict]:
    """Топ по капиталу (монеты + инвентарь). chat_id — только игроки, замеченные в этом групповом чате."""
    c = _db()
    worth = _all_networth(c)
    members = None
    if chat_id is not None:
        members = {r["user_id"] for r in c.execute("SELECT user_id FROM chat_members WHERE chat_id=?", (chat_id,))}
    out = [{"user_id": u["user_id"], "name": u["name"], "title": u["title"], "coins": u["coins"],
            "networth": worth.get(u["user_id"], u["coins"]), "bw": u["bw"], "bl": u["bl"]}
           for u in c.execute("SELECT * FROM users") if members is None or u["user_id"] in members]
    out.sort(key=lambda d: -d["networth"])
    for i, d in enumerate(out, 1):
        d["rank"] = i
    return out if limit is None else out[:limit]




# ── Батлы ─────────────────────────────────────────────────────────────────────
def create_battle(uid: int, name: str, case_key: str, chat_id: int, msg_id: int, now: float | None = None) -> dict:
    now = now or time.time()
    price = CASES[case_key]["price"]
    with _tx() as c:
        u = _ensure(c, uid, name, now)
        if u["coins"] < price:
            return {"ok": False, "need": price - u["coins"]}
        c.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (price, uid))     # ставка в «эскроу»
        bid = secrets.token_hex(4)
        c.execute("INSERT INTO battles VALUES(?,?,?,?,?,?,?,?,'open')",
                  (bid, chat_id, msg_id, uid, name, case_key, price, now))
    return {"ok": True, "id": bid, "price": price}


def cancel_battle(bid: str, uid: int) -> dict:
    with _tx() as c:
        b = c.execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
        if b is None or b["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if b["creator"] != uid:
            return {"ok": False, "reason": "not_owner"}
        c.execute("UPDATE battles SET status='cancelled' WHERE id=?", (bid,))
        c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (b["price"], uid))
    return {"ok": True, "price": b["price"]}


def accept_battle(bid: str, opp: int, opp_name: str, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        b = c.execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
        if b is None or b["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if b["creator"] == opp:
            return {"ok": False, "reason": "self"}
        if now - b["created"] > BATTLE_TTL:
            c.execute("UPDATE battles SET status='expired' WHERE id=?", (bid,))
            c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (b["price"], b["creator"]))
            return {"ok": False, "reason": "expired"}
        o = _ensure(c, opp, opp_name, now)
        price, ck, a = b["price"], b["case_key"], b["creator"]
        if o["coins"] < price:
            return {"ok": False, "reason": "money", "need": price - o["coins"]}
        c.execute("UPDATE users SET coins=coins-?, spent=spent+?, opened=opened+1 WHERE user_id=?", (price, price, opp))
        c.execute("UPDATE users SET spent=spent+?, opened=opened+1 WHERE user_id=?", (price, a))
        ia, ib = roll_item(ck), roll_item(ck)
        pa, pb = item_info(ia)["price"], item_info(ib)["price"]
        _update_best(c, a, ia)
        _update_best(c, opp, ib)
        if pa == pb:
            winner = None
            _record_drop(c, a, ck, ia, now)
            _record_drop(c, opp, ck, ib, now)
        else:
            winner = a if pa > pb else opp
            loser = opp if winner == a else a
            _record_drop(c, winner, ck, ia, now)
            _record_drop(c, winner, ck, ib, now)
            c.execute("UPDATE users SET bw=bw+1, bstreak=bstreak+1, bstreak_best=MAX(bstreak_best, bstreak+1) "
                      "WHERE user_id=?", (winner,))
            c.execute("UPDATE users SET bl=bl+1, bstreak=0 WHERE user_id=?", (loser,))
        c.execute("UPDATE battles SET status='done' WHERE id=?", (bid,))
        _emit(c, a, "battle", 1, price, now)
        _emit(c, opp, "battle", 1, price, now)
        if winner is not None:
            _emit(c, winner, "battle_win", 1, price, now)
        _check_ach(c, a, now)
        _check_ach(c, opp, now)
    return {"ok": True, "case": ck, "price": price, "a": a, "a_name": b["creator_name"], "a_item": ia,
            "b": opp, "b_name": opp_name, "b_item": ib, "winner": winner}


def get_battle(bid: str) -> dict | None:
    r = _db().execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
    return dict(r) if r else None


# ══════════════════════════════════════════════════════════════════════════════
#  v2: уведомления, достижения, сезоны, апгрейды, контракты, подарки, обмен
# ══════════════════════════════════════════════════════════════════════════════
def _notice(c, uid: int, text: str, now: float):
    c.execute("INSERT INTO notices(user_id,text,ts) VALUES(?,?,?)", (uid, text, now))


def pop_notices(uid: int) -> str:
    with _tx() as c:
        rows = c.execute("SELECT id, text FROM notices WHERE user_id=? ORDER BY id", (uid,)).fetchall()
        c.execute("DELETE FROM notices WHERE user_id=?", (uid,))
    return "\n".join(r["text"] for r in rows[-6:])


def _drop_inv(c, inv_id: int):
    """Убрать предмет из инвентаря (и из корзины контракта, если он там лежал)."""
    c.execute("DELETE FROM inventory WHERE id=?", (inv_id,))
    c.execute("DELETE FROM basket WHERE inv_id=?", (inv_id,))


def _is_trophy(item: str) -> bool:
    """Кубки сезона и награды пропуска — «коллекционные»: их нельзя дарить, менять, апгрейдить, вкладывать в контракт и продавать на рынке."""
    return item.startswith(("trophy:", "pass:"))


# ── достижения и титулы ───────────────────────────────────────────────────────
ACH = {
    "first_open": ("🎰", "Новичок", "открыть первый кейс", lambda u, x: u["opened"] >= 1),
    "open_50":    ("📦", "Кейсовод", "открыть 50 кейсов", lambda u, x: u["opened"] >= 50),
    "open_500":   ("🔥", "Одержимый", "открыть 500 кейсов", lambda u, x: u["opened"] >= 500),
    "epic":       ("🟣", "Фиолетовый", "выбить эпик 🟣 или лучше", lambda u, x: u["best_price"] >= 1800),
    "legend":     ("🔴", "Красная нить", "выбить легендарный дроп 🔴", lambda u, x: u["best_price"] >= 9000),
    "mythic":     ("🟡", "Золотая жила", "выбить мифический дроп 🟡", lambda u, x: u["best_price"] >= 90000),
    "rich_10k":   ("💵", "Состоятельный", "капитал 10 000 🪙", lambda u, x: x["networth"] >= 10_000),
    "rich_100k":  ("💰", "Магнат", "капитал 100 000 🪙", lambda u, x: x["networth"] >= 100_000),
    "rich_1m":    ("🏦", "Миллионер", "капитал 1 000 000 🪙", lambda u, x: x["networth"] >= 1_000_000),
    "bw_1":       ("⚔️", "Боец", "выиграть первый батл", lambda u, x: u["bw"] >= 1),
    "bstreak_5":  ("🛡", "Непобедимый", "5 побед в батлах подряд", lambda u, x: u["bstreak_best"] >= 5),
    "bw_25":      ("🗡", "Гладиатор", "25 побед в батлах", lambda u, x: u["bw"] >= 25),
    "up_win":     ("🚀", "Рисковый", "успешный апгрейд", lambda u, x: u["up_win"] >= 1),
    "up_lucky":   ("🌠", "Ва-банк", "удачный апгрейд с шансом ≤10%", lambda u, x: u["lucky_up"] >= 1),
    "contract":   ("🧾", "Делец", "подписать первый контракт", lambda u, x: u["contracts"] >= 1),
    "trade":      ("🤝", "Меняла", "завершить обмен с игроком", lambda u, x: u["trades"] >= 1),
    "gift":       ("🎁", "Щедрая душа", "сделать первый подарок", lambda u, x: u["gifts"] >= 1),
    "daily_7":    ("📅", "Постоянный клиент", "серия бонуса 7 дней", lambda u, x: u["streak"] >= 7),
    "broke":      ("🆘", "На дне", "получить пособие", lambda u, x: u["bailouts"] >= 1),
    "sale_10k":   ("🏷", "Продажа века", "продать предмет за 10 000+ 🪙", lambda u, x: u["best_sale"] >= 10_000),
    "season_top": ("🏅", "Призёр сезона", "войти в топ-3 сезона", lambda u, x: u["seasons_top"] >= 1),
    "season_win": ("🏆", "Чемпион сезона", "выиграть сезон", lambda u, x: u["seasons_win"] >= 1),
}


def _check_ach(c, uid: int, now: float | None = None) -> list[str]:
    """Выдаёт новые достижения (внутри текущей транзакции) и кладёт уведомления."""
    now = now or time.time()
    row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        return []
    u = dict(row)
    _, val = _inv_stats(c, uid)
    x = {"networth": u["coins"] + val}
    have = {r["key"] for r in c.execute("SELECT key FROM achievements WHERE user_id=?", (uid,))}
    new = []
    for key, (icon, name, desc, check) in ACH.items():
        if key not in have and check(u, x):
            c.execute("INSERT OR IGNORE INTO achievements VALUES(?,?,?)", (uid, key, now))
            _notice(c, uid, f"🏅 Достижение: {icon} <b>{name}</b> — {desc}", now)
            new.append(key)
    return new


def get_achievements(uid: int) -> dict[str, float]:
    return {r["key"]: r["got_at"] for r in _db().execute("SELECT key, got_at FROM achievements WHERE user_id=?", (uid,))}


def set_title(uid: int, key: str) -> bool:
    with _tx() as c:
        if key == "-":
            c.execute("UPDATE users SET title='' WHERE user_id=?", (uid,))
            return True
        if key not in ACH or not c.execute("SELECT 1 FROM achievements WHERE user_id=? AND key=?", (uid, key)).fetchone():
            return False
        c.execute("UPDATE users SET title=? WHERE user_id=?", (key, uid))
    return True


def user_exists(uid: int) -> bool:
    return _db().execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone() is not None


def user_name(uid: int) -> str | None:
    r = _db().execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
    return r["name"] if r else None


def find_user_by_username(username: str) -> int | None:
    r = _db().execute("SELECT user_id FROM users WHERE LOWER(username)=?", (username.lower().lstrip("@"),)).fetchone()
    return r["user_id"] if r else None


def touch_chat(uid: int, chat_id: int | None, now: float | None = None):
    """Запоминаем, что игрок активен в этом групповом чате (для чат-топа)."""
    if chat_id is None or chat_id >= 0:
        return
    _db().execute("INSERT INTO chat_members VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET seen=excluded.seen",
                  (chat_id, uid, now or time.time()))


# ── сезоны ────────────────────────────────────────────────────────────────────
def _meta_get(c, k: str, default=None):
    r = c.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r["value"] if r else default


def _meta_set(c, k: str, v):
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


def _all_networth(c) -> dict[int, int]:
    worth = {r["user_id"]: r["coins"] for r in c.execute("SELECT user_id, coins FROM users")}
    for r in c.execute("SELECT user_id, item, COUNT(*) n FROM inventory GROUP BY user_id, item"):
        if r["user_id"] in worth:
            worth[r["user_id"]] += r["n"] * item_info(r["item"])["price"]
    for uid_, v in _market_escrow_all(c).items():
        if uid_ in worth:
            worth[uid_] += v
    return worth


def _snapshot_all(c, season: int):
    c.executemany("INSERT OR REPLACE INTO season_snap VALUES(?,?,?)", [(season, u, w) for u, w in _all_networth(c).items()])


def _season_state(c, now: float) -> tuple[int, float]:
    no = _meta_get(c, "season_no")
    if no is None:
        _meta_set(c, "season_no", 1)
        _meta_set(c, "season_start", now)
        _snapshot_all(c, 1)
        return 1, now
    return int(no), float(_meta_get(c, "season_start"))


def _rollover(c, now: float):
    """Лениво закрывает сезон: топ-3 по ПРИРОСТУ капитала получают кубки; прогресс остальных не сбрасывается."""
    no, start = _season_state(c, now)
    end = start + SEASON_DAYS * 86400
    if now < end:
        return
    _pass_autoclaim(c, no, now)
    worth = _all_networth(c)
    base = {r["user_id"]: r["base"] for r in c.execute("SELECT user_id, base FROM season_snap WHERE season=?", (no,))}
    names = {r["user_id"]: r["name"] for r in c.execute("SELECT user_id, name FROM users")}
    growth = sorted(((worth[u] - base.get(u, worth[u]), u) for u in worth), reverse=True)
    for place, (g, u) in enumerate([x for x in growth if x[0] > 0][:3], 1):
        key = f"trophy:{no}:{place}"
        _record_drop(c, u, "season", key, now)
        c.execute("INSERT OR REPLACE INTO season_results VALUES(?,?,?,?,?)", (no, place, u, names.get(u, str(u)), g))
        c.execute("UPDATE users SET seasons_top=seasons_top+1" + (", seasons_win=seasons_win+1" if place == 1 else "")
                  + " WHERE user_id=?", (u,))
        _notice(c, u, f"🏆 <b>Сезон {no} завершён!</b> Ты занял <b>{place}</b> место "
                      f"(+{g:,} 🪙 за сезон).\nНаграда в инвентаре: {item_info(key)['name']} "
                      f"({item_info(key)['price']:,} 🪙)".replace(",", " "), now)
        _check_ach(c, u, now)
    no += 1
    new_start = end if now < end + SEASON_DAYS * 86400 else now
    _meta_set(c, "season_no", no)
    _meta_set(c, "season_start", new_start)
    _snapshot_all(c, no)


def season_info(now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _rollover(c, now)
        no, start = _season_state(c, now)
        last = [dict(r) for r in c.execute("SELECT * FROM season_results WHERE season=? ORDER BY place", (no - 1,))]
    end = start + SEASON_DAYS * 86400
    return {"no": no, "start": start, "end": end, "left": max(0, int(end - now)), "last": last}


def season_growth(uid: int, now: float | None = None) -> int:
    info = season_info(now)
    c = _db()
    base = c.execute("SELECT base FROM season_snap WHERE season=? AND user_id=?", (info["no"], uid)).fetchone()
    worth = _all_networth(c).get(uid, 0)
    return worth - (base["base"] if base else worth)


def season_leaderboard(limit: int | None = 10, chat_id: int | None = None, now: float | None = None) -> list[dict]:
    info = season_info(now)
    c = _db()
    worth = _all_networth(c)
    base = {r["user_id"]: r["base"] for r in c.execute("SELECT user_id, base FROM season_snap WHERE season=?", (info["no"],))}
    members = None
    if chat_id is not None:
        members = {r["user_id"] for r in c.execute("SELECT user_id FROM chat_members WHERE chat_id=?", (chat_id,))}
    out = []
    for u in c.execute("SELECT user_id, name, title FROM users"):
        if members is not None and u["user_id"] not in members:
            continue
        w = worth.get(u["user_id"], 0)
        out.append({"user_id": u["user_id"], "name": u["name"], "title": u["title"],
                    "growth": w - base.get(u["user_id"], w), "networth": w})
    out.sort(key=lambda d: -d["growth"])
    for i, d in enumerate(out, 1):
        d["rank"] = i
    return out if limit is None else out[:limit]


# ── бесплатный кейс дня и предложение дня ─────────────────────────────────────
def free_status(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
    ready = u["last_free"] != _day(now)
    return {"ready": ready, "wait": 0 if ready else _secs_to_midnight(now)}


def open_free(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    today = _day(now)
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        if u["last_free"] == today:
            return {"ok": False, "wait": _secs_to_midnight(now)}
        item = roll_item("free")
        inv_id = _record_drop(c, uid, "free", item, now)
        c.execute("UPDATE users SET last_free=?, opened=opened+1 WHERE user_id=?", (today, uid))
        _update_best(c, uid, item)
        _emit(c, uid, "free", 1, 0, now)
        _check_ach(c, uid, now)
    return {"ok": True, "item": item, "inv_id": inv_id}


def deal_case(now: float | None = None) -> str:
    """Кейс дня со скидкой: вращается по дням детерминированно."""
    keys = list(SHOP)
    return keys[date.fromisoformat(_day(now or time.time())).toordinal() % len(keys)]


def deal_price(case_key: str) -> int:
    return max(1, round(CASES[case_key]["price"] * (1 - DEAL_DISCOUNT)))


def deal_available(uid: int, now: float | None = None) -> bool:
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
    return u["last_deal"] != _day(now)


# ── апгрейд ───────────────────────────────────────────────────────────────────
def upgrade_options(src_item: str) -> list[dict]:
    """Варианты апгрейда: для каждого множителя — случайная цель из каталога около цены×множитель.
    Шанс = RTP × цена_предмета / цена_цели, так что среднее остаётся равным RTP (<100%) — апгрейд не «печатает деньги»."""
    p0 = item_info(src_item)["price"]
    if p0 <= 0 or _is_trophy(src_item):
        return []
    out, seen = [], set()
    for m in UPGRADE_MULTS:
        t = p0 * m
        cands = [k for k, v in ITEMS.items() if 0.85 * t <= v["price"] <= 1.25 * t and v["price"] > p0]
        if not cands:
            continue
        key = _RNG.choice(cands)
        chance = RTP * p0 / ITEMS[key]["price"]
        if key in seen or not (UPGRADE_MIN_CHANCE <= chance <= UPGRADE_MAX_CHANCE):
            continue
        seen.add(key)
        out.append({"mult": ITEMS[key]["price"] / p0, "target": key, "chance": chance})
    return out


def upgrade_item(uid: int, inv_id: int, target: str, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        row = c.execute("SELECT * FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None:
            return {"ok": False, "reason": "gone"}
        src = row["item"]
        if _is_trophy(src) or target not in ITEMS:
            return {"ok": False, "reason": "bad"}
        p0, pt = item_info(src)["price"], ITEMS[target]["price"]
        chance = RTP * p0 / pt
        if pt <= p0 or not (UPGRADE_MIN_CHANCE <= chance <= UPGRADE_MAX_CHANCE):
            return {"ok": False, "reason": "bad"}
        r = _RNG.random()
        success = r < chance
        _drop_inv(c, inv_id)
        new_id = _record_drop(c, uid, "upgrade", target, now) if success else None
        c.execute("UPDATE users SET up_try=up_try+1" + (", up_win=up_win+1" if success else "")
                  + (", lucky_up=lucky_up+1" if success and chance <= 0.10 else "") + " WHERE user_id=?", (uid,))
        _emit(c, uid, "upgrade", 1, p0, now)
        if success:
            _emit(c, uid, "upgrade_win", 1, p0, now)
        _check_ach(c, uid, now)
    return {"ok": True, "success": success, "chance": chance, "r": r, "src": src, "target": target, "inv_id": new_id}


# ── контракты ─────────────────────────────────────────────────────────────────
def basket_items(uid: int) -> list[tuple[int, str]]:
    rows = _db().execute("SELECT i.id, i.item FROM basket b JOIN inventory i ON i.id=b.inv_id AND i.user_id=b.user_id "
                         "WHERE b.user_id=? ORDER BY i.id", (uid,)).fetchall()
    return [(r["id"], r["item"]) for r in rows]


def basket_toggle(uid: int, inv_id: int) -> dict:
    with _tx() as c:
        row = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None or _is_trophy(row["item"]):
            return {"ok": False, "reason": "bad"}
        if c.execute("SELECT 1 FROM basket WHERE user_id=? AND inv_id=?", (uid, inv_id)).fetchone():
            c.execute("DELETE FROM basket WHERE user_id=? AND inv_id=?", (uid, inv_id))
            return {"ok": True, "added": False}
        n = c.execute("SELECT COUNT(*) n FROM basket WHERE user_id=?", (uid,)).fetchone()["n"]
        if n >= CONTRACT_MAX:
            return {"ok": False, "reason": "full"}
        c.execute("INSERT INTO basket VALUES(?,?)", (uid, inv_id))
    return {"ok": True, "added": True}


def basket_clear(uid: int):
    _db().execute("DELETE FROM basket WHERE user_id=?", (uid,))


def contract_target_pool(total: int, mult: float) -> list[str]:
    """Кандидаты на выдачу: цена в [0.85·цель, цель]. Предмет НИКОГДА не дороже цели — поэтому средний возврат
    контракта строго ниже суммы множителей (<100%) при любой сумме, как бы «зернист» ни был каталог."""
    t = total * mult
    cands = [k for k, v in ITEMS.items() if 0.85 * t <= v["price"] <= t]
    if cands:
        return cands
    below = [k for k, v in ITEMS.items() if v["price"] <= t]
    return [max(below, key=lambda k: ITEMS[k]["price"])] if below else [min(ITEMS, key=lambda k: ITEMS[k]["price"])]


def contract_expected_ratio(total: int) -> float:
    """Точное матожидание выплаты/стоимости для суммы total (используется в тестах и в UI)."""
    e = 0.0
    for m, w in CONTRACT_OUTCOMES:
        pool = contract_target_pool(total, m)
        e += w * sum(ITEMS[k]["price"] for k in pool) / len(pool)
    return e / total


def sign_contract(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        rows = c.execute("SELECT i.id, i.item FROM basket b JOIN inventory i ON i.id=b.inv_id AND i.user_id=b.user_id "
                         "WHERE b.user_id=?", (uid,)).fetchall()
        if not (CONTRACT_MIN <= len(rows) <= CONTRACT_MAX) or any(_is_trophy(r["item"]) for r in rows):
            return {"ok": False, "reason": "count", "n": len(rows)}
        total = sum(item_info(r["item"])["price"] for r in rows)
        r = _RNG.random()
        acc, mult = 0.0, CONTRACT_OUTCOMES[-1][0]
        for m, w in CONTRACT_OUTCOMES:
            acc += w
            if r < acc:
                mult = m
                break
        item = _RNG.choice(contract_target_pool(total, mult))
        for row in rows:
            _drop_inv(c, row["id"])
        inv_id = _record_drop(c, uid, "contract", item, now)
        c.execute("UPDATE users SET contracts=contracts+1 WHERE user_id=?", (uid,))
        _emit(c, uid, "contract", 1, total, now)
        _check_ach(c, uid, now)
    return {"ok": True, "n": len(rows), "total": total, "mult": mult, "item": item, "inv_id": inv_id,
            "src": [r["item"] for r in rows]}


# ── подарки ───────────────────────────────────────────────────────────────────
def _sent_last_day(c, uid: int, now: float) -> int:
    return c.execute("SELECT COALESCE(SUM(value),0) s FROM transfers WHERE from_id=? AND ts>?", (uid, now - 86400)).fetchone()["s"]


def give_left(uid: int, now: float | None = None) -> int:
    now = now or time.time()
    return max(0, GIVE_DAILY_CAP - _sent_last_day(_db(), uid, now))


def give_coins(uid: int, to: int, amount: int, now: float | None = None) -> dict:
    now = now or time.time()
    if uid == to:
        return {"ok": False, "reason": "self"}
    if amount < GIVE_MIN:
        return {"ok": False, "reason": "min"}
    with _tx() as c:
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (to,)).fetchone() is None:
            return {"ok": False, "reason": "no_target"}
        u = _ensure(c, uid, None, now)
        if u["coins"] < amount:
            return {"ok": False, "reason": "money", "need": amount - u["coins"]}
        left = GIVE_DAILY_CAP - _sent_last_day(c, uid, now)
        if amount > left:
            return {"ok": False, "reason": "cap", "left": max(0, left)}
        fee = int(amount * GIVE_FEE)
        c.execute("UPDATE users SET coins=coins-?, gifts=gifts+1 WHERE user_id=?", (amount, uid))
        c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (amount - fee, to))
        c.execute("INSERT INTO transfers(from_id,to_id,kind,value,ts) VALUES(?,?,?,?,?)", (uid, to, "coins", amount, now))
        _emit(c, uid, "gift", 1, amount, now)
        _check_ach(c, uid, now)
        _check_ach(c, to, now)
    return {"ok": True, "amount": amount, "fee": fee, "got": amount - fee}


def give_item(uid: int, to: int, inv_id: int, now: float | None = None) -> dict:
    now = now or time.time()
    if uid == to:
        return {"ok": False, "reason": "self"}
    with _tx() as c:
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (to,)).fetchone() is None:
            return {"ok": False, "reason": "no_target"}
        row = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None:
            return {"ok": False, "reason": "gone"}
        if _is_trophy(row["item"]):
            return {"ok": False, "reason": "trophy"}
        price = item_info(row["item"])["price"]
        left = GIVE_DAILY_CAP - _sent_last_day(c, uid, now)
        if price > left:
            return {"ok": False, "reason": "cap", "left": max(0, left)}
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (to, inv_id))
        c.execute("DELETE FROM basket WHERE inv_id=?", (inv_id,))
        _album_add(c, to, row["item"], now)
        c.execute("UPDATE users SET gifts=gifts+1 WHERE user_id=?", (uid,))
        c.execute("INSERT INTO transfers(from_id,to_id,kind,value,ts) VALUES(?,?,?,?,?)", (uid, to, "item", price, now))
        _emit(c, uid, "gift", 1, price, now)
        _check_ach(c, uid, now)
    return {"ok": True, "item": row["item"], "price": price}


# ── обмен предметами между игроками ───────────────────────────────────────────
def create_trade(a: int, a_name: str, b: int, b_name: str, a_inv: int, b_inv: int, chat_id: int, msg_id: int,
                 now: float | None = None) -> dict:
    now = now or time.time()
    if a == b:
        return {"ok": False, "reason": "self"}
    with _tx() as c:
        ra = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (a_inv, a)).fetchone()
        rb = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (b_inv, b)).fetchone()
        if ra is None or rb is None:
            return {"ok": False, "reason": "gone"}
        if _is_trophy(ra["item"]) or _is_trophy(rb["item"]):
            return {"ok": False, "reason": "trophy"}
        tid = secrets.token_hex(4)
        c.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,'open')",
                  (tid, chat_id, msg_id, a, a_name, b, b_name, a_inv, b_inv, now))
    return {"ok": True, "id": tid, "a_item": ra["item"], "b_item": rb["item"]}


def get_trade(tid: str) -> dict | None:
    r = _db().execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def close_trade(tid: str, uid: int, kind: str) -> dict:
    """kind: 'cancel' (только инициатор) или 'decline' (только адресат)."""
    with _tx() as c:
        t = c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if t is None or t["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if uid != (t["a"] if kind == "cancel" else t["b"]):
            return {"ok": False, "reason": "not_yours"}
        c.execute("UPDATE trades SET status=? WHERE id=?", ("cancelled" if kind == "cancel" else "declined", tid))
    return {"ok": True}


def accept_trade(tid: str, uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        t = c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if t is None or t["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if uid != t["b"]:
            return {"ok": False, "reason": "not_yours"}
        if now - t["created"] > TRADE_TTL:
            c.execute("UPDATE trades SET status='expired' WHERE id=?", (tid,))
            return {"ok": False, "reason": "expired"}
        ra = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (t["a_inv"], t["a"])).fetchone()
        rb = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (t["b_inv"], t["b"])).fetchone()
        if ra is None or rb is None:
            c.execute("UPDATE trades SET status='void' WHERE id=?", (tid,))
            return {"ok": False, "reason": "gone"}
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (t["b"], t["a_inv"]))
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (t["a"], t["b_inv"]))
        c.execute("DELETE FROM basket WHERE inv_id IN (?,?)", (t["a_inv"], t["b_inv"]))
        c.execute("UPDATE users SET trades=trades+1 WHERE user_id IN (?,?)", (t["a"], t["b"]))
        c.execute("UPDATE trades SET status='done' WHERE id=?", (tid,))
        _album_add(c, t["b"], ra["item"], now)
        _album_add(c, t["a"], rb["item"], now)
        _emit(c, t["a"], "trade", 1, 0, now)
        _emit(c, t["b"], "trade", 1, 0, now)
        _check_ach(c, t["a"], now)
        _check_ach(c, t["b"], now)
    return {"ok": True, "a": t["a"], "a_name": t["a_name"], "b": t["b"], "b_name": t["b_name"],
            "a_item": ra["item"], "b_item": rb["item"]}


# ══════════════════════════════════════════════════════════════════════════════
#  v4: админ-команды (начисление баланса, выдача предметов) и тег админа
# ══════════════════════════════════════════════════════════════════════════════
ADMIN_MAX = 10 ** 12


def is_admin(uid: int) -> bool:
    """Права — только по числовому Telegram-ID (username можно сменить/перехватить, ID — нет)."""
    return uid in ADMIN_IDS


def _alog(c, admin_id: int, target: int, action: str, detail: str, now: float):
    c.execute("INSERT INTO admin_log(admin_id,target_id,action,detail,ts) VALUES(?,?,?,?,?)",
              (admin_id, target, action, detail, now))


def admin_adjust(admin_id: int, target: int, mode: str, amount: int, name: str | None = None,
                 now: float | None = None) -> dict:
    """mode: add — начислить, take — снять (не ниже 0), set — установить баланс."""
    if not is_admin(admin_id):
        return {"ok": False, "reason": "forbidden"}
    if mode not in ("add", "take", "set") or not (0 <= amount <= ADMIN_MAX):
        return {"ok": False, "reason": "bad"}
    now = now or time.time()
    with _tx() as c:
        old = _ensure(c, target, name, now)["coins"]
        new = old + amount if mode == "add" else (max(0, old - amount) if mode == "take" else amount)
        c.execute("UPDATE users SET coins=? WHERE user_id=?", (new, target))
        _alog(c, admin_id, target, mode, f"{old}->{new}", now)
        _check_ach(c, target, now)
    return {"ok": True, "mode": mode, "old": old, "new": new, "amount": amount, "delta": new - old}


def admin_search_items(query: str, limit: int = 12) -> list[str]:
    q = query.strip().lower()
    if not q:
        return []
    if q in ITEMS:
        return [q]
    hits = [k for k, v in ITEMS.items() if q in k.lower() or q in v["name"].lower()]
    return sorted(hits, key=lambda k: (-ITEMS[k]["price"], k))[:limit]


def admin_grant_item(admin_id: int, target: int, item_key: str, count: int = 1, name: str | None = None,
                     now: float | None = None) -> dict:
    if not is_admin(admin_id):
        return {"ok": False, "reason": "forbidden"}
    if item_key not in ITEMS:
        return {"ok": False, "reason": "unknown_item"}
    if not (1 <= count <= 50):
        return {"ok": False, "reason": "bad"}
    now = now or time.time()
    with _tx() as c:
        _ensure(c, target, name, now)
        ids = [_record_drop(c, target, "admin", item_key, now) for _ in range(count)]
        _alog(c, admin_id, target, "item", f"{item_key} x{count}", now)
        _check_ach(c, target, now)
    return {"ok": True, "item": item_key, "count": count, "ids": ids}


def admin_log_tail(limit: int = 10) -> list[dict]:
    rows = _db().execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  v5: уровни и опыт, квесты, мини-спин, билеты, рынок игроков, альбом, пропуск, мини-игры
# ══════════════════════════════════════════════════════════════════════════════
# ── уровни ────────────────────────────────────────────────────────────────────
LEVEL_MAX = 100
_LEVEL_TABLE = [int(60 * (l - 1) ** 1.8) for l in range(1, LEVEL_MAX + 1)]    # суммарный XP для уровня l — индекс l-1


def level_of(xp: int) -> int:
    return bisect.bisect_right(_LEVEL_TABLE, max(0, int(xp)))


def level_progress(xp: int) -> tuple[int, int, int]:
    """(уровень, XP внутри уровня, XP до следующего уровня; 0 на максимальном)"""
    lvl = level_of(xp)
    cur = _LEVEL_TABLE[lvl - 1]
    return (lvl, xp - cur, 0) if lvl >= LEVEL_MAX else (lvl, xp - cur, _LEVEL_TABLE[lvl] - cur)


def level_reward(lvl: int) -> tuple[int, int]:
    """(монеты, билеты) за получение уровня. Суммарно дешевле, чем стоит нафармить XP кейсами — фармить невыгодно."""
    return 30 + 15 * lvl + (200 if lvl % 10 == 0 else 0), (1 if lvl % 3 == 0 else 0)


def max_bet(lvl: int) -> int:
    """Лимит ставки в мини-играх растёт с уровнем."""
    return min(GAME_MAX_BET_CAP, 1000 + 250 * lvl)


# XP даётся за ДЕЙСТВИЯ с затратами/риском. Мини-игры XP не дают (иначе фарм мелкими ставками).
_XP_RULES = {
    "open": lambda n, v: n * (8 + min(40, v // 100)),
    "free": lambda n, v: 12 * n,
    "ticket": lambda n, v: 9 * n,
    "daily": lambda n, v: 20,
    "sell": lambda n, v: min(30, 2 * n),
    "battle": lambda n, v: 15,
    "battle_win": lambda n, v: 25,
    "upgrade": lambda n, v: min(10, v // 10),          # XP пропорционален ценности (копеечные предметы XP не фармят)
    "upgrade_win": lambda n, v: min(30, v // 10),
    "contract": lambda n, v: min(15, v // 20),
    "gift": lambda n, v: 8,
    "trade": lambda n, v: 20,
    "spin": lambda n, v: 10,
    "market_list": lambda n, v: 0,                      # выставить и снять — бесплатно, поэтому XP не даём (квест — даём)
    "market_buy": lambda n, v: 10,
    "market_sell": lambda n, v: 15,
}
_EVENT_ALIAS = {"free": ("free", "open"), "ticket": ("open",)}        # события, засчитываемые в квесты
# «социальные» события почти ничего не стоят (можно крутить с альт-аккаунтом) → суммарный XP с них ограничен за сутки
_SOCIAL_XP = {"gift", "trade", "market_buy", "market_sell", "battle", "battle_win"}
SOCIAL_XP_CAP = 150


def _add_xp(c, uid: int, xp: int, now: float):
    """+XP игроку и в XP текущего сезона; за каждый новый уровень — награда и уведомление."""
    if xp <= 0:
        return
    row = c.execute("SELECT xp FROM users WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        return
    old = row["xp"]
    new = old + xp
    c.execute("UPDATE users SET xp=? WHERE user_id=?", (new, uid))
    no = _meta_get(c, "season_no")
    if no is not None:
        c.execute("INSERT INTO season_xp(season,user_id,xp) VALUES(?,?,?) "
                  "ON CONFLICT(season,user_id) DO UPDATE SET xp=xp+excluded.xp", (int(no), uid, xp))
    lo, ln = level_of(old), level_of(new)
    for lvl in range(lo + 1, ln + 1):
        coins, tk = level_reward(lvl)
        c.execute("UPDATE users SET coins=coins+?, tickets=tickets+? WHERE user_id=?", (coins, tk, uid))
        _notice(c, uid, f"⭐ <b>Уровень {lvl}!</b> +{coins} 🪙" + (" +1 🎫" if tk else ""), now)
    if ln > lo:
        _quest_progress(c, uid, "levelup", ln - lo, now)


def _emit(c, uid: int, event: str, amount: int = 1, value: int = 0, now: float | None = None):
    """Единая точка событий: начисляет XP и двигает прогресс квестов. Вызывается внутри транзакций."""
    now = now or time.time()
    rule = _XP_RULES.get(event)
    if rule:
        xp = rule(amount, value)
        if xp > 0 and event in _SOCIAL_XP:
            today = _day(now)
            u = c.execute("SELECT xp_day, xp_social FROM users WHERE user_id=?", (uid,)).fetchone()
            if u is not None:
                used = u["xp_social"] if u["xp_day"] == today else 0
                xp = max(0, min(xp, SOCIAL_XP_CAP - used))
                if xp:
                    c.execute("UPDATE users SET xp_day=?, xp_social=? WHERE user_id=?", (today, used + xp, uid))
        _add_xp(c, uid, xp, now)
    for ev in _EVENT_ALIAS.get(event, (event,)):
        _quest_progress(c, uid, ev, amount, now)


# ── квесты ────────────────────────────────────────────────────────────────────
def _pl(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


# (qid, событие, сложность, текст(n), [(цель, монеты, xp, билеты), ...])
_DAILY_Q = [
    ("d_open", "open", "easy", lambda n: f"Открой {n} {_pl(n, 'кейс', 'кейса', 'кейсов')}", [(2, 60, 20, 0), (3, 100, 30, 0), (5, 180, 50, 0)]),
    ("d_sell", "sell", "easy", lambda n: f"Продай {n} {_pl(n, 'предмет', 'предмета', 'предметов')}", [(2, 40, 15, 0), (4, 80, 25, 0)]),
    ("d_free", "free", "easy", lambda n: "Открой бесплатный кейс дня", [(1, 30, 10, 0)]),
    ("d_daily", "daily", "easy", lambda n: "Забери бонус дня", [(1, 30, 10, 0)]),
    ("d_spin", "spin", "easy", lambda n: "Крутани мини-колесо", [(1, 40, 15, 0)]),
    ("d_battle", "battle", "mid", lambda n: "Сыграй батл", [(1, 80, 30, 0)]),
    ("d_up", "upgrade", "mid", lambda n: "Попробуй апгрейд", [(1, 70, 25, 0)]),
    ("d_contract", "contract", "mid", lambda n: "Подпиши контракт", [(1, 90, 30, 0)]),
    ("d_game", "game", "mid", lambda n: f"Сыграй {n} {_pl(n, 'раунд', 'раунда', 'раундов')} в мини-играх", [(3, 60, 20, 0), (6, 110, 35, 0)]),
    ("d_gwin", "game_win", "mid", lambda n: f"Выиграй {n} {_pl(n, 'раунд', 'раунда', 'раундов')} в мини-играх", [(2, 80, 25, 0)]),
    ("d_blist", "market_list", "social", lambda n: "Выставь предмет на рынок", [(1, 50, 20, 0)]),
    ("d_bbuy", "market_buy", "social", lambda n: "Купи лот на рынке", [(1, 80, 30, 0)]),
    ("d_gift", "gift", "social", lambda n: "Сделай подарок", [(1, 60, 20, 0)]),
    ("d_trade", "trade", "social", lambda n: "Сделай обмен с игроком", [(1, 100, 40, 0)]),
    ("d_bwin", "battle_win", "social", lambda n: "Выиграй батл", [(1, 150, 50, 0)]),
]
_WEEKLY_Q = [
    ("w_open", "open", "easy", lambda n: f"Открой {n} {_pl(n, 'кейс', 'кейса', 'кейсов')}", [(15, 400, 120, 1), (30, 800, 200, 2)]),
    ("w_sell", "sell", "easy", lambda n: f"Продай {n} {_pl(n, 'предмет', 'предмета', 'предметов')}", [(10, 300, 80, 0), (20, 500, 120, 0)]),
    ("w_spin", "spin", "easy", lambda n: f"Крутани мини-колесо {n} {_pl(n, 'раз', 'раза', 'раз')}", [(8, 350, 100, 1)]),
    ("w_qd", "qdone_d", "easy", lambda n: f"Выполни {n} ежедневных заданий", [(8, 500, 150, 1)]),
    ("w_up", "upgrade_win", "mid", lambda n: f"Успешно улучши предмет {n} {_pl(n, 'раз', 'раза', 'раз')}", [(2, 500, 150, 1)]),
    ("w_contract", "contract", "mid", lambda n: f"Подпиши {n} {_pl(n, 'контракт', 'контракта', 'контрактов')}", [(3, 450, 120, 0)]),
    ("w_gwin", "game_win", "mid", lambda n: f"Выиграй {n} {_pl(n, 'раунд', 'раунда', 'раундов')} в мини-играх", [(10, 400, 120, 0)]),
    ("w_lvl", "levelup", "mid", lambda n: f"Получи {n} {_pl(n, 'уровень', 'уровня', 'уровней')}", [(2, 400, 0, 1)]),
    ("w_bwin", "battle_win", "social", lambda n: f"Выиграй {n} {_pl(n, 'батл', 'батла', 'батлов')}", [(2, 500, 150, 1)]),
    ("w_msell", "market_sell", "social", lambda n: f"Продай {n} {_pl(n, 'лот', 'лота', 'лотов')} на рынке", [(2, 500, 150, 1)]),
    ("w_trade", "trade", "social", lambda n: f"Сделай {n} {_pl(n, 'обмен', 'обмена', 'обменов')}", [(2, 400, 100, 0)]),
]
_QTEXT = {t[0]: t[3] for t in _DAILY_Q + _WEEKLY_Q}
_QTEXT["d_all"] = lambda n: "Все 3 задания дня"
_QTEXT["w_all"] = lambda n: "Все 3 задания недели — сундук"
_QCHEST = {"d": (150, 60, 1), "w": (1500, 300, 3)}      # монеты, XP, билеты за закрытие всей тройки


def quest_text(qid: str, n: int) -> str:
    return _QTEXT.get(qid, lambda k: qid)(n)


def _wkey(now: float) -> str:
    y, w, _ = datetime.fromtimestamp(now, _TZ).isocalendar()
    return f"{y}-W{w:02d}"


def _secs_to_monday(now: float) -> int:
    d = datetime.fromtimestamp(now, _TZ)
    nxt = (d + timedelta(days=7 - d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - d).total_seconds()))


def _qkeys(now: float):
    return (("d", _day(now)), ("w", _wkey(now)))


def _pick_quests(uid: int, period: str, pkey: str) -> list[tuple]:
    rnd = random.Random(f"{uid}:{period}:{pkey}")
    pool = list(_DAILY_Q if period == "d" else _WEEKLY_Q)
    rnd.shuffle(pool)
    first = next(t for t in pool if t[2] == "easy")             # хотя бы одно лёгкое — день никогда не «пустой»
    chosen, social = [first], 0
    for t in pool:
        if len(chosen) >= 3:
            break
        if t is first:
            continue
        if t[2] == "social":
            if social >= 1:
                continue
            social += 1
        chosen.append(t)
    rnd.shuffle(chosen)
    return [(t[0], t[1], *rnd.choice(t[4])) for t in chosen]    # (qid, event, target, coins, xp, tickets)


def _ensure_quests(c, uid: int, now: float):
    for period, pkey in _qkeys(now):
        if c.execute("SELECT 1 FROM quests WHERE user_id=? AND period=? AND pkey=? LIMIT 1", (uid, period, pkey)).fetchone():
            continue
        c.execute("DELETE FROM quests WHERE user_id=? AND period=? AND pkey<>?", (uid, period, pkey))
        for idx, (qid, ev, target, coins, xp, tk) in enumerate(_pick_quests(uid, period, pkey)):
            c.execute("INSERT INTO quests(user_id,period,pkey,idx,qid,event,target,progress,coins,xp,tickets,claimed) "
                      "VALUES(?,?,?,?,?,?,?,0,?,?,?,0)", (uid, period, pkey, idx, qid, ev, target, coins, xp, tk))
        coins, xp, tk = _QCHEST[period]
        c.execute("INSERT INTO quests(user_id,period,pkey,idx,qid,event,target,progress,coins,xp,tickets,claimed) "
                  "VALUES(?,?,?,3,?,?,3,0,?,?,?,0)", (uid, period, pkey, f"{period}_all", "qall", coins, xp, tk))


def _quest_progress(c, uid: int, event: str, amount: int, now: float):
    _ensure_quests(c, uid, now)
    for period, pkey in _qkeys(now):
        rows = c.execute("SELECT idx, progress, target FROM quests WHERE user_id=? AND period=? AND pkey=? AND idx<3 "
                         "AND event=? AND progress<target", (uid, period, pkey, event)).fetchall()
        for r in rows:
            new = min(r["target"], r["progress"] + amount)
            c.execute("UPDATE quests SET progress=? WHERE user_id=? AND period=? AND pkey=? AND idx=?",
                      (new, uid, period, pkey, r["idx"]))
            if new >= r["target"]:
                c.execute("UPDATE quests SET progress=MIN(target, progress+1) WHERE user_id=? AND period=? AND pkey=? AND idx=3",
                          (uid, period, pkey))
                if period == "d":
                    _quest_progress(c, uid, "qdone_d", 1, now)      # недельное «выполни N дневных заданий»


def quest_state(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _ensure_quests(c, uid, now)
        out = {}
        for period, pkey in _qkeys(now):
            out[period] = [dict(r) for r in c.execute(
                "SELECT * FROM quests WHERE user_id=? AND period=? AND pkey=? ORDER BY idx", (uid, period, pkey))]
    out["d_left"], out["w_left"] = _secs_to_midnight(now), _secs_to_monday(now)
    return out


def quest_summary(uid: int, now: float | None = None) -> dict:
    st = quest_state(uid, now)
    done_d = sum(1 for q in st["d"][:3] if q["progress"] >= q["target"])
    done_w = sum(1 for q in st["w"][:3] if q["progress"] >= q["target"])
    ready = sum(1 for p in ("d", "w") for q in st[p] if q["progress"] >= q["target"] and not q["claimed"])
    return {"done_d": done_d, "done_w": done_w, "ready": ready}


def _quest_claim_row(c, uid: int, r, now: float) -> dict:
    c.execute("UPDATE quests SET claimed=1 WHERE user_id=? AND period=? AND pkey=? AND idx=?",
              (uid, r["period"], r["pkey"], r["idx"]))
    c.execute("UPDATE users SET coins=coins+?, tickets=tickets+?, quests_done=quests_done+? WHERE user_id=?",
              (r["coins"], r["tickets"], 1 if r["idx"] < 3 else 0, uid))
    _add_xp(c, uid, r["xp"], now)
    return {"coins": r["coins"], "xp": r["xp"], "tickets": r["tickets"]}


def quest_claim(uid: int, period: str, idx: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _ensure_quests(c, uid, now)
        key = dict(_qkeys(now)).get(period)
        r = c.execute("SELECT * FROM quests WHERE user_id=? AND period=? AND pkey=? AND idx=?", (uid, period, key, idx)).fetchone()
        if r is None:
            return {"ok": False, "reason": "gone"}
        if r["progress"] < r["target"]:
            return {"ok": False, "reason": "incomplete"}
        if r["claimed"]:
            return {"ok": False, "reason": "claimed"}
        out = _quest_claim_row(c, uid, r, now)
        _check_ach(c, uid, now)
    return {"ok": True, **out}


def quest_claim_all(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    tot = {"coins": 0, "xp": 0, "tickets": 0, "n": 0}
    with _tx() as c:
        _ensure_quests(c, uid, now)
        for period, key in _qkeys(now):
            for r in c.execute("SELECT * FROM quests WHERE user_id=? AND period=? AND pkey=? AND progress>=target AND claimed=0 "
                               "ORDER BY idx", (uid, period, key)).fetchall():
                o = _quest_claim_row(c, uid, r, now)
                tot["coins"] += o["coins"]; tot["xp"] += o["xp"]; tot["tickets"] += o["tickets"]; tot["n"] += 1
        _check_ach(c, uid, now)
    return {"ok": tot["n"] > 0, **tot}


# ── мини-спин и билеты ────────────────────────────────────────────────────────
SPIN_COOLDOWN = 6 * 3600
SPIN_MAX = 2                  # заряды копятся, но не больше двух
# (тип, значение, вес %). Билет = бесплатный «Стартовый» кейс.
SPIN_PRIZES = [("coins", 10, 35.0), ("coins", 25, 28.0), ("coins", 50, 16.0), ("coins", 100, 9.0), ("ticket", 1, 6.0),
               ("coins", 250, 3.0), ("coins", 500, 1.0), ("coins", 2000, 0.35), ("coins", 10000, 0.05), ("xp", 25, 1.6)]
assert abs(sum(p[2] for p in SPIN_PRIZES) - 100) < 1e-9
TICKET_CASE = "starter"
TICKET_VALUE = CASES[TICKET_CASE]["ev"]
SPIN_EV = sum((v if k == "coins" else TICKET_VALUE if k == "ticket" else 0) * w / 100 for k, v, w in SPIN_PRIZES)


def _spin_accrue(n: int, ts: float, now: float) -> tuple[int, float]:
    if ts <= 0:
        return n, now                          # первый заход: заряд уже есть, отсчёт пошёл
    if n >= SPIN_MAX:
        return n, now                          # копилка полна — отсчёт стоит
    gained = int((now - ts) // SPIN_COOLDOWN)
    if gained <= 0:
        return n, ts
    n2 = min(SPIN_MAX, n + gained)
    return n2, (now if n2 >= SPIN_MAX else ts + gained * SPIN_COOLDOWN)


def spin_status(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        n, ts = _spin_accrue(u["spin_n"], u["spin_ts"], now)
        if (n, ts) != (u["spin_n"], u["spin_ts"]):
            c.execute("UPDATE users SET spin_n=?, spin_ts=? WHERE user_id=?", (n, ts, uid))
    return {"charges": n, "next_in": 0 if n >= SPIN_MAX else max(0, int(ts + SPIN_COOLDOWN - now))}


def do_spin(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        n, ts = _spin_accrue(u["spin_n"], u["spin_ts"], now)
        if n < 1:
            c.execute("UPDATE users SET spin_n=?, spin_ts=? WHERE user_id=?", (n, ts, uid))
            return {"ok": False, "next_in": max(0, int(ts + SPIN_COOLDOWN - now))}
        was_full = n >= SPIN_MAX
        r = _RNG.random() * 100
        acc, prize = 0.0, SPIN_PRIZES[-1]
        for p in SPIN_PRIZES:
            acc += p[2]
            if r < acc:
                prize = p
                break
        kind, val, _ = prize
        c.execute("UPDATE users SET spin_n=?, spin_ts=? WHERE user_id=?", (n - 1, now if was_full else ts, uid))
        if kind == "coins":
            c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (val, uid))
            if val >= 10000:
                c.execute("UPDATE users SET spin_jp=spin_jp+1 WHERE user_id=?", (uid,))
        elif kind == "ticket":
            c.execute("UPDATE users SET tickets=tickets+? WHERE user_id=?", (val, uid))
        _emit(c, uid, "spin", 1, 0, now)
        if kind == "xp":
            _add_xp(c, uid, val, now)
        _check_ach(c, uid, now)
        left, ts2 = n - 1, (now if was_full else ts)
    return {"ok": True, "kind": kind, "value": val, "left": left,
            "next_in": 0 if left >= SPIN_MAX else max(0, int(ts2 + SPIN_COOLDOWN - now))}


def open_ticket(uid: int, now: float | None = None) -> dict:
    """Билет открывает «Стартовый» кейс бесплатно."""
    now = now or time.time()
    with _tx() as c:
        u = _ensure(c, uid, None, now)
        if u["tickets"] < 1:
            return {"ok": False, "reason": "none"}
        c.execute("UPDATE users SET tickets=tickets-1, opened=opened+1 WHERE user_id=?", (uid,))
        item = roll_item(TICKET_CASE)
        inv_id = _record_drop(c, uid, "ticket", item, now)
        _update_best(c, uid, item)
        _emit(c, uid, "ticket", 1, 0, now)
        _check_ach(c, uid, now)
        left = u["tickets"] - 1
    return {"ok": True, "item": item, "inv_id": inv_id, "left": left}


# ── рынок игроков ─────────────────────────────────────────────────────────────
MARKET_FEE = 0.05
MARKET_TTL = 3 * 86400
MARKET_MAX_LOTS = 10
MARKET_PRICE_RANGE = (0.8, 1.5)        # цена лота относительно справочной (узкий коридор: рынок не должен быть обходом лимита подарков)
MARKET_PRESETS = (0.9, 1.0, 1.1, 1.25, 1.5)


def _market_escrow(c, uid: int) -> int:
    return sum(item_info(r["item"])["price"] for r in
               c.execute("SELECT item FROM market WHERE seller=? AND status='open'", (uid,)))


def _market_escrow_all(c) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in c.execute("SELECT seller, item FROM market WHERE status='open'"):
        out[r["seller"]] = out.get(r["seller"], 0) + item_info(r["item"])["price"]
    return out


def market_price_bounds(item: str) -> tuple[int, int]:
    base = item_info(item)["price"]
    return max(5, int(base * MARKET_PRICE_RANGE[0])), max(5, int(base * MARKET_PRICE_RANGE[1]))


def _market_expire(c, now: float):
    """Просроченные лоты возвращаются продавцам."""
    for r in c.execute("SELECT * FROM market WHERE status='open' AND created<?", (now - MARKET_TTL,)).fetchall():
        c.execute("UPDATE market SET status='expired', closed=? WHERE id=?", (now, r["id"]))
        _record_drop(c, r["seller"], "market", r["item"], now)
        _notice(c, r["seller"], f"⌛ Лот истёк, предмет вернулся в инвентарь: {item_info(r['item'])['name']}", now)


def market_list(uid: int, inv_id: int, price: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _ensure(c, uid, None, now)
        row = c.execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if row is None:
            return {"ok": False, "reason": "gone"}
        if _is_trophy(row["item"]):
            return {"ok": False, "reason": "trophy"}
        lo, hi = market_price_bounds(row["item"])
        if not (lo <= price <= hi):
            return {"ok": False, "reason": "price", "lo": lo, "hi": hi}
        if c.execute("SELECT COUNT(*) n FROM market WHERE seller=? AND status='open'", (uid,)).fetchone()["n"] >= MARKET_MAX_LOTS:
            return {"ok": False, "reason": "limit"}
        name = c.execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()["name"]
        _drop_inv(c, inv_id)                      # предмет уходит «в эскроу» — его нельзя продать/обменять, пока лот висит
        cur = c.execute("INSERT INTO market(seller,seller_name,item,price,created,status) VALUES(?,?,?,?,?,'open')",
                        (uid, name, row["item"], price, now))
        _emit(c, uid, "market_list", 1, price, now)
        _check_ach(c, uid, now)
    return {"ok": True, "lot": cur.lastrowid, "item": row["item"], "price": price}


def market_cancel(uid: int, lot_id: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        r = c.execute("SELECT * FROM market WHERE id=?", (lot_id,)).fetchone()
        if r is None or r["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if r["seller"] != uid:
            return {"ok": False, "reason": "not_yours"}
        c.execute("UPDATE market SET status='cancelled', closed=? WHERE id=?", (now, lot_id))
        inv_id = _record_drop(c, uid, "market", r["item"], now)
    return {"ok": True, "item": r["item"], "inv_id": inv_id}


def market_buy(uid: int, lot_id: int, name: str | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _market_expire(c, now)
        r = c.execute("SELECT * FROM market WHERE id=?", (lot_id,)).fetchone()
        if r is None or r["status"] != "open":
            return {"ok": False, "reason": "closed"}
        if r["seller"] == uid:
            return {"ok": False, "reason": "own"}
        u = _ensure(c, uid, name, now)
        price = r["price"]
        if u["coins"] < price:
            return {"ok": False, "reason": "money", "need": price - u["coins"]}
        base = item_info(r["item"])["price"]
        if price != base:                          # недооценка/переплата = передача ценности → считается в суточный лимит подарков
            giver = r["seller"] if price < base else uid
            amt = abs(price - base)
            left = GIVE_DAILY_CAP - _sent_last_day(c, giver, now)
            if amt > left:
                return {"ok": False, "reason": "cap", "left": max(0, left)}
            c.execute("INSERT INTO transfers(from_id,to_id,kind,value,ts) VALUES(?,?,?,?,?)",
                      (giver, uid if giver == r["seller"] else r["seller"], "market", amt, now))
        fee = int(price * MARKET_FEE)
        c.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (price, uid))
        c.execute("UPDATE users SET coins=coins+?, market_sales=market_sales+1 WHERE user_id=?", (price - fee, r["seller"]))
        c.execute("UPDATE users SET market_buys=market_buys+1 WHERE user_id=?", (uid,))
        c.execute("UPDATE market SET status='sold', buyer=?, closed=? WHERE id=?", (uid, now, lot_id))
        inv_id = _record_drop(c, uid, "market", r["item"], now)
        _notice(c, r["seller"], f"🏪 Твой лот продан: {item_info(r['item'])['name']} за {price:,} 🪙 "
                                f"(на руки {price - fee:,})".replace(",", " "), now)
        _emit(c, uid, "market_buy", 1, price, now)
        _emit(c, r["seller"], "market_sell", 1, price, now)
        _check_ach(c, uid, now)
        _check_ach(c, r["seller"], now)
    return {"ok": True, "item": r["item"], "price": price, "fee": fee, "inv_id": inv_id, "seller": r["seller"],
            "seller_name": r["seller_name"]}


MARKET_SORTS = {"new": ("🆕 Новые", "id DESC"), "cheap": ("💰 Дешёвые", "price ASC, id ASC"),
                "top": ("🔥 Дорогие", "price DESC, id ASC"), "deal": ("🎯 Выгодные", None)}
MARKET_PER_PAGE = 5


def market_browse(sort: str, page: int, viewer: int | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _market_expire(c, now)
    rows = [dict(r) for r in _db().execute("SELECT * FROM market WHERE status='open'")]
    if viewer is not None:
        rows = [r for r in rows if r["seller"] != viewer]
    if sort == "deal":
        rows.sort(key=lambda r: (r["price"] / max(1, item_info(r["item"])["price"]), r["id"]))
    elif sort == "cheap":
        rows.sort(key=lambda r: (r["price"], r["id"]))
    elif sort == "top":
        rows.sort(key=lambda r: (-r["price"], r["id"]))
    else:
        rows.sort(key=lambda r: -r["id"])
    pages = max(1, -(-len(rows) // MARKET_PER_PAGE))
    page = max(0, min(page, pages - 1))
    return {"lots": rows[page * MARKET_PER_PAGE:(page + 1) * MARKET_PER_PAGE], "page": page, "pages": pages, "total": len(rows)}


def market_get(lot_id: int) -> dict | None:
    r = _db().execute("SELECT * FROM market WHERE id=?", (lot_id,)).fetchone()
    return dict(r) if r else None


def market_mine(uid: int) -> dict:
    c = _db()
    return {"open": [dict(r) for r in c.execute("SELECT * FROM market WHERE seller=? AND status='open' ORDER BY id DESC", (uid,))],
            "sold": [dict(r) for r in c.execute("SELECT * FROM market WHERE seller=? AND status='sold' ORDER BY closed DESC LIMIT 5", (uid,))]}


# ── альбом и коллекции ────────────────────────────────────────────────────────
def _album_add(c, uid: int, item: str, now: float):
    c.execute("INSERT OR IGNORE INTO album(user_id,item,first_at) VALUES(?,?,?)", (uid, item, now))


def _by(pred) -> list[str]:
    return [k for k, v in ITEMS.items() if pred(k, v)]


def _keys(*ks):
    s = set(ks)
    return lambda k, v: k in s


_COL_DEFS = [
    ("ak", "🔫", "AK-47", lambda k, v: "AK-47" in v["name"]),
    ("awp", "🎯", "AWP", lambda k, v: v["name"].startswith("AWP")),
    ("m4", "🔫", "M4", lambda k, v: v["name"].startswith(("M4A1-S", "M4A4"))),
    ("knife", "🔪", "Ножи и перчатки", lambda k, v: v["name"].startswith("★")),
    ("pistol", "🔫", "Пистолеты", lambda k, v: k.startswith("cs_") and v["name"].startswith(("Glock", "USP", "Desert", "P250", "Five", "CZ75", "Tec-9", "Dual"))),
    ("rucars", "🚙", "Народные авто", _keys("car_granta", "car_niva", "car_kopeyka", "car_logan", "car_solaris", "car_polo", "car_rapid")),
    ("supercars", "🏎", "Суперкары и люкс-авто", _keys("car_aventador", "car_laferrari", "car_chiron", "car_urus", "car_911", "car_m5", "car_phantom", "car_g63", "car_rangerover", "car_cayenne")),
    ("moto", "🏍", "Мото, вода, воздух", _keys("car_vespa", "car_atv", "car_ninja", "car_harley", "car_jetski", "car_boat", "car_heli", "car_ebike", "car_scooter")),
    ("watch", "⌚", "Часы", _keys("lux_watch", "lux_awatch", "lux_rolex", "lux_omega", "lux_tag", "lux_patek", "lux_ap")),
    ("gold", "💎", "Золото и камни", _keys("lux_gold100", "lux_gold500", "lux_bar", "lux_diamond", "lux_emerald", "lux_pink", "fash_chanel_necklace")),
    ("estate", "🏠", "Недвижимость", _keys("lux_flat", "lux_cottage", "lux_penthouse", "lux_villa", "lux_island")),
    ("apple", "📱", "Apple", _keys("lux_iphone", "lux_ipad", "lux_airpods", "lux_awatch", "lux_mac", "lux_macstudio", "lux_xdr", "lux_vision")),
    ("sneakers", "👟", "Кроссовки", _keys("fash_dunk_panda", "fash_yeezy350", "fash_yeezy700", "fash_aj4", "fash_aj1_chicago", "fash_travis_aj", "fash_air_mag", "fash_aj1_dior", "fash_balenciaga", "fash_dior_b23", "fash_vans", "fash_converse", "fash_nb530", "fash_samba", "fash_puma", "fash_martens")),
    ("brands", "👜", "Люкс-бренды", _keys("fash_birkin30", "fash_chanel_flap", "fash_lv_neverfull", "fash_hermes_scarf", "fash_gucci_belt", "fash_prada_nylon", "fash_lv_supreme_trunk", "fash_birkin_himalaya", "fash_dior_couture", "fash_moncler")),
    ("retro", "🕹", "Ретро-консоли", _keys("game_nes_box", "game_gb_red", "game_pong", "game_pacman", "game_odyssey", "game_ps1_devkit", "game_ps_proto", "game_mario_sealed", "game_zelda_gold", "game_mario_wata")),
    ("pokemon", "🃏", "Покемоны", _keys("game_charizard", "game_pikachu_ill", "game_poke_box", "game_poke_stickers", "game_gb_red", "game_amiibo")),
    ("hardware", "🖥", "Железо геймера", _keys("game_rtx3060", "game_rtx4090", "game_pc_mid", "game_pc_4080", "game_oled_mon", "game_monitor144", "game_secretlab", "game_simrig", "game_valve_index", "lux_ssd", "game_ssd500")),
    ("junk", "🗑", "Музей хлама", _keys("lux_magnet", "lux_ticket", "lux_piggy", "car_pine", "fash_socks", "game_steamkey", "game_aa", "lux_bag", "cs_p250_sand", "car_wipers")),
    ("legends", "🔴", "Все легендарные", lambda k, v: v["rarity"] == "legend"),
    ("mythics", "🟡", "Все мифические", lambda k, v: v["rarity"] == "mythic"),
]


def _nice50(x: float) -> int:
    return int(round(x / 50.0) * 50)


COLLECTIONS: list[dict] = []
for _key, _icon, _name, _pred in _COL_DEFS:
    _items = _by(_pred)
    if len(_items) < 4:
        raise AssertionError(f"коллекция {_key}: в ней слишком мало предметов ({len(_items)})")
    _total = sum(ITEMS[i]["price"] for i in _items)
    COLLECTIONS.append({"key": _key, "icon": _icon, "name": _name, "items": _items,
                        "coins": max(200, min(20000, _nice50(0.04 * _total))),
                        "tickets": max(1, len(_items) // 5), "xp": 100 + 10 * len(_items)})
COL_BY_KEY = {c["key"]: c for c in COLLECTIONS}
ALBUM_MILESTONES = [(25, 1000, 2), (50, 3000, 4), (75, 8000, 6), (100, 25000, 10)]      # % каталога → монеты, билеты


def album_items(uid: int) -> set[str]:
    return {r["item"] for r in _db().execute("SELECT item FROM album WHERE user_id=?", (uid,))}


def album_state(uid: int) -> dict:
    have = album_items(uid)
    claimed = {r["col"] for r in _db().execute("SELECT col FROM col_claims WHERE user_id=?", (uid,))}
    cols = []
    for col in COLLECTIONS:
        n = sum(1 for i in col["items"] if i in have)
        cols.append({**col, "have": n, "total": len(col["items"]), "claimed": col["key"] in claimed,
                     "complete": n == len(col["items"])})
    owned_catalog = sum(1 for i in ITEMS if i in have)
    return {"cols": cols, "have": have, "owned": owned_catalog, "total": len(ITEMS), "claimed": claimed}


def album_claim(uid: int, col_key: str, now: float | None = None) -> dict:
    now = now or time.time()
    col = COL_BY_KEY.get(col_key)
    if col is None:
        return {"ok": False, "reason": "bad"}
    with _tx() as c:
        _ensure(c, uid, None, now)
        have = {r["item"] for r in c.execute("SELECT item FROM album WHERE user_id=?", (uid,))}
        if not all(i in have for i in col["items"]):
            return {"ok": False, "reason": "incomplete"}
        if c.execute("SELECT 1 FROM col_claims WHERE user_id=? AND col=?", (uid, col_key)).fetchone():
            return {"ok": False, "reason": "claimed"}
        c.execute("INSERT INTO col_claims VALUES(?,?,?)", (uid, col_key, now))
        c.execute("UPDATE users SET coins=coins+?, tickets=tickets+?, col_done=col_done+1 WHERE user_id=?",
                  (col["coins"], col["tickets"], uid))
        _add_xp(c, uid, col["xp"], now)
        _check_ach(c, uid, now)
    return {"ok": True, "coins": col["coins"], "tickets": col["tickets"], "xp": col["xp"]}


def album_milestone_claim(uid: int, pct: int, now: float | None = None) -> dict:
    now = now or time.time()
    ms = next((m for m in ALBUM_MILESTONES if m[0] == pct), None)
    if ms is None:
        return {"ok": False, "reason": "bad"}
    need = -(-len(ITEMS) * pct // 100)
    with _tx() as c:
        _ensure(c, uid, None, now)
        owned = c.execute("SELECT COUNT(*) n FROM album WHERE user_id=? AND item IN (%s)" % ",".join("?" * len(ITEMS)),
                          (uid, *ITEMS)).fetchone()["n"]
        if owned < need:
            return {"ok": False, "reason": "incomplete", "need": need, "have": owned}
        key = f"all{pct}"
        if c.execute("SELECT 1 FROM col_claims WHERE user_id=? AND col=?", (uid, key)).fetchone():
            return {"ok": False, "reason": "claimed"}
        c.execute("INSERT INTO col_claims VALUES(?,?,?)", (uid, key, now))
        c.execute("UPDATE users SET coins=coins+?, tickets=tickets+? WHERE user_id=?", (ms[1], ms[2], uid))
        _add_xp(c, uid, 50 + pct * 3, now)
        _check_ach(c, uid, now)
    return {"ok": True, "coins": ms[1], "tickets": ms[2]}


# ── сезонный пропуск ──────────────────────────────────────────────────────────
PASS_TIERS = 20
PASS_XP = 150                 # XP сезона на один уровень пропуска
PASS_PRICE = 4500
_PASS_PREMIUM_TICKETS = {3, 6, 9, 12, 15, 18, 20}


def pass_rewards(tier: int) -> dict:
    return {"free": {"coins": 20 + 5 * tier, "tickets": 1 if tier % 5 == 0 else 0},
            "prem": {"coins": 30 + 8 * tier, "tickets": 1 if tier in _PASS_PREMIUM_TICKETS else 0, "item": tier == PASS_TIERS}}


def pass_state(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _rollover(c, now)
        _ensure(c, uid, None, now)
        no, start = _season_state(c, now)
        row = c.execute("SELECT xp FROM season_xp WHERE season=? AND user_id=?", (no, uid)).fetchone()
        xp = row["xp"] if row else 0
        prem = c.execute("SELECT 1 FROM pass_premium WHERE user_id=? AND season=?", (uid, no)).fetchone() is not None
        claimed = {(r["tier"], r["track"]) for r in c.execute("SELECT tier, track FROM pass_claims WHERE user_id=? AND season=?", (uid, no))}
    tier = min(PASS_TIERS, xp // PASS_XP)
    left = max(0, int(start + SEASON_DAYS * 86400 - now))
    ready = sum(1 for t in range(1, tier + 1) for tr in (("free", "prem") if prem else ("free",)) if (t, tr) not in claimed)
    return {"season": no, "xp": xp, "tier": tier, "premium": prem, "claimed": claimed, "left": left, "ready": ready,
            "into": xp - tier * PASS_XP if tier < PASS_TIERS else 0}


def _pass_grant(c, uid: int, season: int, now: float) -> dict:
    """Выдаёт все ещё не полученные награды пройденных уровней. Используется и кнопкой, и автовыдачей в конце сезона."""
    row = c.execute("SELECT xp FROM season_xp WHERE season=? AND user_id=?", (season, uid)).fetchone()
    tier = min(PASS_TIERS, (row["xp"] if row else 0) // PASS_XP)
    prem = c.execute("SELECT 1 FROM pass_premium WHERE user_id=? AND season=?", (uid, season)).fetchone() is not None
    got = {"coins": 0, "tickets": 0, "items": 0, "n": 0, "tier": tier}
    for t in range(1, tier + 1):
        rw = pass_rewards(t)
        for track in (("free", "prem") if prem else ("free",)):
            if c.execute("SELECT 1 FROM pass_claims WHERE user_id=? AND season=? AND tier=? AND track=?", (uid, season, t, track)).fetchone():
                continue
            c.execute("INSERT INTO pass_claims VALUES(?,?,?,?,?)", (uid, season, t, track, now))
            r = rw[track]
            got["coins"] += r["coins"]; got["tickets"] += r["tickets"]; got["n"] += 1
            if track == "prem" and r.get("item"):
                _record_drop(c, uid, "pass", f"pass:{season}", now)
                got["items"] += 1
    if got["n"]:
        c.execute("UPDATE users SET coins=coins+?, tickets=tickets+?, pass_max=MAX(pass_max, ?) WHERE user_id=?",
                  (got["coins"], got["tickets"], tier, uid))
    return got


def pass_claim(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _rollover(c, now)
        _ensure(c, uid, None, now)
        no, _ = _season_state(c, now)
        got = _pass_grant(c, uid, no, now)
        _check_ach(c, uid, now)
    return {"ok": got["n"] > 0, **got}


def pass_buy(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        _rollover(c, now)
        u = _ensure(c, uid, None, now)
        no, _ = _season_state(c, now)
        if c.execute("SELECT 1 FROM pass_premium WHERE user_id=? AND season=?", (uid, no)).fetchone():
            return {"ok": False, "reason": "have"}
        if u["coins"] < PASS_PRICE:
            return {"ok": False, "reason": "money", "need": PASS_PRICE - u["coins"]}
        c.execute("UPDATE users SET coins=coins-?, pass_prem=pass_prem+1 WHERE user_id=?", (PASS_PRICE, uid))
        c.execute("INSERT INTO pass_premium VALUES(?,?,?)", (uid, no, now))
        _check_ach(c, uid, now)
    return {"ok": True, "price": PASS_PRICE}


def _pass_autoclaim(c, season: int, now: float):
    """Перед закрытием сезона автоматически выдаём всё не забранное — ничего не пропадает."""
    for r in c.execute("SELECT user_id FROM season_xp WHERE season=? AND xp>=?", (season, PASS_XP)).fetchall():
        got = _pass_grant(c, r["user_id"], season, now)
        if got["n"]:
            msg = (f"⭐ Сезонный пропуск завершён: автоматически выданы награды (+{got['coins']:,} 🪙"
                   + (f", 🎫 {got['tickets']}" if got["tickets"] else "") + ")").replace(",", " ")
            _notice(c, r["user_id"], msg, now)


# ── мини-игры ─────────────────────────────────────────────────────────────────
GAME_RTP = 0.95
GAME_MIN_BET = 10
GAME_MAX_BET_CAP = 25000
COIN_MULT = 1.9
DICE_MULT = {"u": math.floor(GAME_RTP * 36 / 15 * 100) / 100, "o": math.floor(GAME_RTP * 36 / 15 * 100) / 100,
             "s": math.floor(GAME_RTP * 6 * 100) / 100}          # меньше 7 / больше 7 / ровно 7
MINES_N = 16
MINES_OPTIONS = (1, 3, 5, 8)
MINES_MAX_MULT = 500.0
MINES_IDLE_AUTOCASH = 12 * 3600
CRASH_RATE = 0.08
CRASH_MAX = 1000.0
BET_PRESETS = (10, 50, 100, 250, 500, 1000, 2500, 5000)


def mines_mult(m: int, k: int) -> float:
    """Множитель после k безопасных клеток: честный (1/вероятность дожить) × RTP."""
    if k <= 0:
        return 1.0
    p = math.comb(MINES_N - m, k) / math.comb(MINES_N, k)
    return min(MINES_MAX_MULT, max(1.0, math.floor(GAME_RTP / p * 100) / 100))


def crash_point(u: float) -> float:
    """P(краш ≥ x) = RTP / x → ожидание любой стратегии «забрать на x» = RTP."""
    if u >= GAME_RTP or u <= 0:
        return 1.0 if u >= GAME_RTP else CRASH_MAX
    return min(CRASH_MAX, max(1.0, math.floor(GAME_RTP / u * 100) / 100))


def crash_mult(elapsed: float) -> float:
    return math.floor(math.exp(CRASH_RATE * max(0.0, elapsed)) * 100) / 100


def _check_bet(c, uid: int, bet: int, now: float) -> dict | None:
    u = _ensure(c, uid, None, now)
    lim = max_bet(level_of(u["xp"]))
    if bet < GAME_MIN_BET:
        return {"ok": False, "reason": "min", "min": GAME_MIN_BET}
    if bet > lim:
        return {"ok": False, "reason": "max", "max": lim}
    if u["coins"] < bet:
        return {"ok": False, "reason": "money", "need": bet - u["coins"]}
    if c.execute("SELECT 1 FROM game_state WHERE user_id=?", (uid,)).fetchone():
        return {"ok": False, "reason": "active"}
    return None


def _settle(c, uid: int, bet: int, payout: int, now: float, game: str, mult: float = 0.0):
    """Итог раунда: выплата (ставка уже списана при старте), статистика, события квестов."""
    win = payout > bet
    if payout:
        c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (payout, uid))
    c.execute("UPDATE users SET game_plays=game_plays+1, game_wins=game_wins+? WHERE user_id=?", (1 if win else 0, uid))
    if game == "mines" and win:
        c.execute("UPDATE users SET best_mines=MAX(best_mines, ?) WHERE user_id=?", (int(mult * 100), uid))
    if game == "crash" and win:
        c.execute("UPDATE users SET best_crash=MAX(best_crash, ?) WHERE user_id=?", (int(mult * 100), uid))
    _emit(c, uid, "game", 1, bet, now)
    if win:
        _emit(c, uid, "game_win", 1, bet, now)
    _check_ach(c, uid, now)


def coin_flip(uid: int, bet: int, side: str, now: float | None = None) -> dict:
    now = now or time.time()
    if side not in ("h", "t"):
        return {"ok": False, "reason": "bad"}
    with _tx() as c:
        err = _check_bet(c, uid, bet, now)
        if err:
            return err
        c.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (bet, uid))
        res = _RNG.choice(("h", "t"))
        payout = int(bet * COIN_MULT) if res == side else 0
        _settle(c, uid, bet, payout, now, "coin", COIN_MULT)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "result": res, "win": payout > 0, "payout": payout, "bet": bet, "coins": coins}


def dice_roll(uid: int, bet: int, mode: str, now: float | None = None) -> dict:
    now = now or time.time()
    if mode not in DICE_MULT:
        return {"ok": False, "reason": "bad"}
    with _tx() as c:
        err = _check_bet(c, uid, bet, now)
        if err:
            return err
        c.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (bet, uid))
        d1, d2 = _RNG.randint(1, 6), _RNG.randint(1, 6)
        s = d1 + d2
        win = (mode == "u" and s < 7) or (mode == "o" and s > 7) or (mode == "s" and s == 7)
        payout = int(bet * DICE_MULT[mode]) if win else 0
        _settle(c, uid, bet, payout, now, "dice", DICE_MULT[mode])
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "dice": (d1, d2), "sum": s, "win": win, "payout": payout, "bet": bet, "mult": DICE_MULT[mode], "coins": coins}


def game_active(uid: int) -> dict | None:
    r = _db().execute("SELECT * FROM game_state WHERE user_id=?", (uid,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["data"] = json.loads(d["data"])
    return d


def _game_set(c, uid: int, game: str, bet: int, data: dict, now: float):
    c.execute("INSERT OR REPLACE INTO game_state(user_id,game,bet,data,started) VALUES(?,?,?,?,?)",
              (uid, game, bet, json.dumps(data), now))


def mines_start(uid: int, bet: int, m: int, now: float | None = None) -> dict:
    now = now or time.time()
    if m not in MINES_OPTIONS:
        return {"ok": False, "reason": "bad"}
    with _tx() as c:
        err = _check_bet(c, uid, bet, now)
        if err:
            return err
        c.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (bet, uid))
        mines = sorted(_RNG.sample(range(MINES_N), m))
        _game_set(c, uid, "mines", bet, {"m": m, "mines": mines, "open": []}, now)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "bet": bet, "m": m, "coins": coins}


def mines_pick(uid: int, cell: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        g = c.execute("SELECT * FROM game_state WHERE user_id=? AND game='mines'", (uid,)).fetchone()
        if g is None:
            return {"ok": False, "reason": "none"}
        d, bet = json.loads(g["data"]), g["bet"]
        if not (0 <= cell < MINES_N) or cell in d["open"]:
            return {"ok": False, "reason": "bad", "state": d, "bet": bet}
        if cell in d["mines"]:
            c.execute("DELETE FROM game_state WHERE user_id=?", (uid,))
            _settle(c, uid, bet, 0, now, "mines")
            return {"ok": True, "boom": True, "state": d, "bet": bet, "cell": cell}
        d["open"].append(cell)
        k = len(d["open"])
        mult = mines_mult(d["m"], k)
        if k == MINES_N - d["m"]:                          # открыл все безопасные клетки — авто-выплата
            payout = int(bet * mult)
            c.execute("DELETE FROM game_state WHERE user_id=?", (uid,))
            _settle(c, uid, bet, payout, now, "mines", mult)
            return {"ok": True, "boom": False, "done": True, "payout": payout, "mult": mult, "state": d, "bet": bet}
        _game_set(c, uid, "mines", bet, d, g["started"])
    return {"ok": True, "boom": False, "done": False, "mult": mult, "state": d, "bet": bet}


def mines_cash(uid: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        g = c.execute("SELECT * FROM game_state WHERE user_id=? AND game='mines'", (uid,)).fetchone()
        if g is None:
            return {"ok": False, "reason": "none"}
        d, bet = json.loads(g["data"]), g["bet"]
        k = len(d["open"])
        if k == 0:
            return {"ok": False, "reason": "empty", "state": d, "bet": bet}
        mult = mines_mult(d["m"], k)
        payout = int(bet * mult)
        c.execute("DELETE FROM game_state WHERE user_id=?", (uid,))
        _settle(c, uid, bet, payout, now, "mines", mult)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "payout": payout, "mult": mult, "state": d, "bet": bet, "coins": coins}


def crash_start(uid: int, bet: int, now: float | None = None) -> dict:
    now = now or time.time()
    with _tx() as c:
        err = _check_bet(c, uid, bet, now)
        if err:
            return err
        c.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (bet, uid))
        cp = crash_point(_RNG.random())
        _game_set(c, uid, "crash", bet, {"cp": cp}, now)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "bet": bet, "coins": coins, "started": now}


def crash_status(uid: int, now: float | None = None) -> dict | None:
    now = now or time.time()
    g = game_active(uid)
    if g is None or g["game"] != "crash":
        return None
    mult = crash_mult(now - g["started"])
    return {"bet": g["bet"], "mult": mult, "busted": mult >= g["data"]["cp"], "cp": g["data"]["cp"], "started": g["started"]}


def crash_cash(uid: int, now: float | None = None) -> dict:
    """Забрать выигрыш: множитель считается по ВРЕМЕНИ на сервере в момент нажатия (кнопка могла «отстать»)."""
    now = now or time.time()
    with _tx() as c:
        g = c.execute("SELECT * FROM game_state WHERE user_id=? AND game='crash'", (uid,)).fetchone()
        if g is None:
            return {"ok": False, "reason": "none"}
        cp, bet = json.loads(g["data"])["cp"], g["bet"]
        mult = crash_mult(now - g["started"])
        c.execute("DELETE FROM game_state WHERE user_id=?", (uid,))
        if mult >= cp:
            _settle(c, uid, bet, 0, now, "crash")
            return {"ok": True, "bust": True, "cp": cp, "bet": bet}
        payout = int(bet * mult)
        _settle(c, uid, bet, payout, now, "crash", mult)
        coins = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()["coins"]
    return {"ok": True, "bust": False, "mult": mult, "payout": payout, "cp": cp, "bet": bet, "coins": coins}


def crash_settle_bust(uid: int, now: float | None = None) -> dict | None:
    """Вызывается циклом анимации (или при возвращении игрока): если раунд уже разбился — фиксируем проигрыш."""
    now = now or time.time()
    with _tx() as c:
        g = c.execute("SELECT * FROM game_state WHERE user_id=? AND game='crash'", (uid,)).fetchone()
        if g is None:
            return None
        cp, bet = json.loads(g["data"])["cp"], g["bet"]
        if crash_mult(now - g["started"]) < cp:
            return None
        c.execute("DELETE FROM game_state WHERE user_id=?", (uid,))
        _settle(c, uid, bet, 0, now, "crash")
    return {"cp": cp, "bet": bet}


def _game_cleanup(c, uid: int, now: float):
    """Брошенные раунды: краш давно разбился — проигрыш; мины простояли 12 ч — авто-выплата по текущему множителю."""
    g = c.execute("SELECT * FROM game_state WHERE user_id=?", (uid,)).fetchone()
    if g is None:
        return
    d, bet = json.loads(g["data"]), g["bet"]
    if g["game"] == "crash":
        if crash_mult(now - g["started"]) >= d["cp"]:
            c.execute("DELETE FROM game_state WHERE user_id=?", (uid,))
            _settle(c, uid, bet, 0, now, "crash")
            _notice(c, uid, f"🚀 Раунд краша завершился на ×{d['cp']:.2f} — ставка {bet:,} 🪙 проиграна".replace(",", " "), now)
    elif g["game"] == "mines" and now - g["started"] > MINES_IDLE_AUTOCASH:
        k = len(d["open"])
        payout = int(bet * mines_mult(d["m"], k)) if k else bet
        c.execute("DELETE FROM game_state WHERE user_id=?", (uid,))
        _settle(c, uid, bet, payout, now, "mines", mines_mult(d["m"], k))
        _notice(c, uid, f"💣 Брошенная партия в «Мины» закрыта автоматически: выплата {payout:,} 🪙".replace(",", " "), now)


# ── достижения нового блока ───────────────────────────────────────────────────
ACH.update({
    "quest_10": ("📋", "Исполнитель", "забрать награду за 10 заданий", lambda u, x: u["quests_done"] >= 10),
    "quest_100": ("🗂", "Трудяга", "забрать награду за 100 заданий", lambda u, x: u["quests_done"] >= 100),
    "lvl_10": ("⭐", "Десятка", "достичь 10 уровня", lambda u, x: level_of(u["xp"]) >= 10),
    "lvl_25": ("🌟", "Ветеран клуба", "достичь 25 уровня", lambda u, x: level_of(u["xp"]) >= 25),
    "lvl_50": ("💫", "Легенда клуба", "достичь 50 уровня", lambda u, x: level_of(u["xp"]) >= 50),
    "market_1": ("🏪", "Торговец", "продать первый лот на рынке", lambda u, x: u["market_sales"] >= 1),
    "market_25": ("🧳", "Купец", "продать 25 лотов на рынке", lambda u, x: u["market_sales"] >= 25),
    "col_1": ("📚", "Коллекционер", "собрать первый набор в альбоме", lambda u, x: u["col_done"] >= 1),
    "col_5": ("🗃", "Куратор", "собрать 5 наборов", lambda u, x: u["col_done"] >= 5),
    "col_all": ("🏛", "Хранитель музея", "собрать все наборы альбома", lambda u, x: u["col_done"] >= len(COLLECTIONS)),
    "game_50": ("🎲", "Азартный", "сыграть 50 раундов в мини-играх", lambda u, x: u["game_plays"] >= 50),
    "mines_x10": ("💣", "Сапёр", "забрать ×10 и больше в «Минах»", lambda u, x: u["best_mines"] >= 1000),
    "crash_x5": ("🚀", "Ракетчик", "забрать ×5 и больше в «Краше»", lambda u, x: u["best_crash"] >= 500),
    "spin_jp": ("🎰", "Джекпот колеса", "выиграть главный приз мини-спина", lambda u, x: u["spin_jp"] >= 1),
    "pass_prem": ("💎", "Премиум-игрок", "купить премиум сезонного пропуска", lambda u, x: u["pass_prem"] >= 1),
    "pass_20": ("🏅", "Покоритель пропуска", "пройти все 20 уровней пропуска", lambda u, x: u["pass_max"] >= PASS_TIERS),
})


def get_inv_item(uid: int, inv_id: int) -> str | None:
    r = _db().execute("SELECT item FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
    return r["item"] if r else None


# ══════════════════════════════════════════════════════════════════════════════
#  UI
# ══════════════════════════════════════════════════════════════════════════════
_BUSY: set[int] = set()          # игроки, у которых сейчас идёт анимация открытия


def _name_of(u) -> str:
    n = (getattr(u, "full_name", None) or "").strip() or (f"@{u.username}" if getattr(u, "username", None) else "")
    return (n or str(u.id))[:32]


def _kb(rows) -> Kb:
    return Kb(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row] for row in rows])


def _chances(c: dict) -> str:
    parts = []
    for t in RARITY_ORDER:
        w = c["weights"].get(t)
        if w:
            pct = w * 100
            parts.append(f"{RARITY[t][0]} {pct:.0f}%" if pct >= 1 else f"{RARITY[t][0]} {pct:.2f}%")
    return " · ".join(parts)


# ── экраны ────────────────────────────────────────────────────────────────────
_BANNER = {
    "junk": "🗑 Ну… хоть что-то",
    "common": "⚪ Обычный дроп",
    "rare": "🔵 Неплохо!",
    "epic": "🟣 <b>ЭПИЧЕСКИЙ ДРОП!</b>",
    "legend": "🔴🔥 <b>ЛЕГЕНДАРНЫЙ ДРОП!!</b> 🔥🔴",
    "mythic": "🟡✨ <b>МИФИЧЕСКИЙ ДРОП!!!</b> ✨🟡",
}


def drop_screen(uid: int, ckey: str, item: str, inv_id: int, coins: int, cost: int) -> tuple[str, Kb]:
    c, i = CASES[ckey], item_info(item)
    mult = i["price"] / cost
    delta = i["price"] - cost
    text = (
        f"{_BANNER[i['rarity']]}\n\n{_it(item)}\n"
        f"💰 {_fmt(i['price'])} 🪙 · ×{mult:.2f} от цены кейса "
        f"({'+' if delta >= 0 else '−'}{_fmt(abs(delta))})\n\n"
        f"🪙 Баланс: <b>{_fmt(coins)}</b>"
    )
    kb = _kb([
        [(f"💰 Продать за {_fmt(i['price'])}", f"cs:sell:{uid}:{inv_id}:0:-1")],
        [(f"🔁 Ещё раз · {_fmt(c['price'])}🪙", f"cs:open:{uid}:{ckey}"), ("📦 Кейсы", f"cs:cases:{uid}")],
        [("🎒 Инвентарь", f"cs:inv:{uid}:0"), ("🏠 Меню", f"cs:menu:{uid}")],
    ])
    return text, kb


def open5_screen(uid: int, ckey: str, drops: list, coins: int, cost: int) -> tuple[str, Kb]:
    c = CASES[ckey]
    drops = sorted(drops, key=lambda d: -item_info(d[0])["price"])
    total = sum(item_info(d[0])["price"] for d in drops)
    delta = total - cost
    text = (
        f"📦 <b>×{len(drops)} · {c['icon']} {c['name']}</b>\n\n"
        + "\n".join(f"{_it(i)} — {_fmt(item_info(i)['price'])}" for i, _ in drops)
        + f"\n\n💰 Выпало на <b>{_fmt(total)}</b> 🪙 при ставке {_fmt(cost)} "
        f"({'+' if delta >= 0 else '−'}{_fmt(abs(delta))})\n🪙 Баланс: <b>{_fmt(coins)}</b>"
    )
    kb = _kb([
        [(f"🔁 ×5 ещё · {_fmt(c['price'] * 5)}🪙", f"cs:open5:{uid}:{ckey}"), ("🎰 ×1", f"cs:open:{uid}:{ckey}")],
        [("🎒 Инвентарь", f"cs:inv:{uid}:0"), ("📦 Кейсы", f"cs:cases:{uid}")],
        [("🏠 Меню", f"cs:menu:{uid}")],
    ])
    return text, kb


def inv_screen(uid: int, page: int = 0, notice: str = "") -> tuple[str, Kb]:
    pg = inventory_page(uid, page)
    p = get_profile(uid)
    if not pg["total"]:
        return ((notice + "\n\n" if notice else "") + "🎒 <b>Инвентарь пуст</b>\nОткрой кейс — и сюда что-нибудь выпадет!",
                _kb([[("📦 Кейсы", f"cs:cases:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")]]))
    lines = [f"🎒 <b>ИНВЕНТАРЬ</b> · {pg['total']} шт. на {_fmt(p['inv_value'])} 🪙 · стр. {pg['page'] + 1}/{pg['pages']}"]
    if notice:
        lines.insert(0, notice + "\n")
    sell_row, line_no = [], 0
    for n, (inv_id, item) in enumerate(pg["items"], 1):
        lines.append(f"{n}. {_it(item)} — {_fmt(item_info(item)['price'])} 🪙")
        sell_row.append((f"💰 {n}", f"cs:sell:{uid}:{inv_id}:0:{pg['page']}"))
    rows = [sell_row[i:i + 3] for i in range(0, len(sell_row), 3)]
    nav = []
    if pg["page"] > 0:
        nav.append(("◀️", f"cs:inv:{uid}:{pg['page'] - 1}"))
    nav.append((f"{pg['page'] + 1}/{pg['pages']}", f"cs:inv:{uid}:{pg['page']}"))
    if pg["page"] < pg["pages"] - 1:
        nav.append(("▶️", f"cs:inv:{uid}:{pg['page'] + 1}"))
    rows.append(nav)
    rows.append([("🗑 Хлам", f"cs:bulk:{uid}:junk:0"), ("⚪🔵 До редкого", f"cs:bulk:{uid}:cheap:0"),
                 ("💎 Всё", f"cs:bulk:{uid}:all:0")])
    rows.append([("📦 Кейсы", f"cs:cases:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")])
    lines.append("\n<i>Кнопка «💰 N» продаёт предмет под этим номером</i>")
    return "\n".join(lines), _kb(rows)


# ── анимации ──────────────────────────────────────────────────────────────────
def _strip(ckey: str, center_item: str | None = None, width: int = 9) -> str:
    cells = [RARITY[item_info(roll_item(ckey))["rarity"]][0] for _ in range(width)]
    if center_item:
        cells[width // 2] = RARITY[item_info(center_item)["rarity"]][0]
        cells[width // 2] = f"【{cells[width // 2]}】"
    return "".join(cells)


async def _anim_battle(msg, res: dict):
    c = CASES[res["case"]]
    an, bn = _e(res["a_name"]), _e(res["b_name"])
    head = f"⚔️ <b>БАТЛ</b> · {c['icon']} {c['name']} · {_fmt(c['price'])} 🪙\n\n"
    pa, pb = item_info(res["a_item"])["price"], item_info(res["b_item"])["price"]
    for k in range(6):
        await _frame(msg, head + f"{an}\n{_strip(res['case'])}  ❓\n\n{bn}\n{_strip(res['case'])}  ❓\n\n"
                              f"<i>{'◐◓◑◒'[k % 4]} кейсы крутятся…</i>")
        await asyncio.sleep(0.6 + 0.1 * k)
    await _frame(msg, head + f"{an}\n{_it(res['a_item'])} — <b>{_fmt(pa)}</b> 🪙\n\n{bn}\n"
                            f"{_strip(res['case'])}  ❓\n\n<i>◓ у соперника ещё крутится…</i>")
    await asyncio.sleep(1.3)


def battle_result_text(res: dict) -> str:
    c = CASES[res["case"]]
    an, bn = _e(res["a_name"]), _e(res["b_name"])
    pa, pb = item_info(res["a_item"])["price"], item_info(res["b_item"])["price"]
    text = (f"⚔️ <b>БАТЛ</b> · {c['icon']} {c['name']} · {_fmt(c['price'])} 🪙\n\n"
            f"{an}\n{_it(res['a_item'])} — <b>{_fmt(pa)}</b> 🪙\n\n"
            f"{bn}\n{_it(res['b_item'])} — <b>{_fmt(pb)}</b> 🪙\n\n")
    if res["winner"] is None:
        return text + "🤝 <b>Ничья!</b> Каждый оставляет свой предмет."
    wn = an if res["winner"] == res["a"] else bn
    return text + f"🏆 <b>Победил {wn}!</b> Забирает оба предмета на <b>{_fmt(pa + pb)}</b> 🪙"


# ── обработчики ───────────────────────────────────────────────────────────────
async def _do_open5(cb: types.CallbackQuery, uid: int, name: str, ckey: str):
    if uid in _BUSY:
        await cb.answer("Погоди, кейс ещё открывается 🎰")
        return
    get_profile(uid, name)
    res = open_case(uid, ckey, times=5)
    if not res["ok"]:
        await cb.answer(f"Не хватает {_fmt(res['need'])} 🪙", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        c = CASES[ckey]
        for k in range(3):
            await _frame(cb.message, f"{c['icon']} <b>×5 · {c['name']}</b>\n\n"
                                    + "\n".join(_strip(ckey) for _ in range(5)) + f"\n\n<i>{'◐◓◑◒'[k]} открываем…</i>")
            await asyncio.sleep(0.6)
        text, kb = open5_screen(uid, ckey, res["drops"], res["coins"], res["cost"])
        await _edit(cb.message, _with_notices(uid, text), kb)
    finally:
        _BUSY.discard(uid)


async def _do_sell(cb, uid: int, inv_id: int, ok: int, page: int):
    item = get_inv_item(uid, inv_id)
    if item is None:
        await cb.answer("Этот предмет уже продан", show_alert=True)
        if page >= 0:
            t, k = inv_screen(uid, page)
            await _edit(cb.message, t, k)
        return
    price = item_info(item)["price"]
    if price >= CONFIRM_SELL_AT and not ok:
        await cb.answer()
        back = f"cs:inv:{uid}:{page}" if page >= 0 else f"cs:menu:{uid}"
        await _edit(cb.message, f"⚠️ Продать {_it(item)} за <b>{_fmt(price)}</b> 🪙?\nПредмет дорогой — точно?",
                    _kb([[("✅ Да, продать", f"cs:sell:{uid}:{inv_id}:1:{page}"), ("❌ Нет", back)]]))
        return
    r = sell_item(uid, inv_id)
    if not r["ok"]:
        await cb.answer("Этот предмет уже продан", show_alert=True)
        return
    await cb.answer(f"+{_fmt(r['price'])} 🪙")
    note = f"💰 Продано: {_it(item)} за <b>{_fmt(r['price'])}</b> 🪙"
    if page >= 0:
        t, k = inv_screen(uid, page, note)
    else:
        t = note + f"\n\n🪙 Баланс: <b>{_fmt(r['coins'])}</b>"
        k = _kb([[("📦 Кейсы", f"cs:cases:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")], [("🏠 Меню", f"cs:menu:{uid}")]])
    await _edit(cb.message, t, k)


async def _do_bulk(cb, uid: int, mode: str, ok: int):
    if mode not in _BULK:
        await cb.answer("Неизвестный режим", show_alert=True)
        return
    cnt, total = bulk_preview(uid, mode)
    if not cnt:
        await cb.answer("Нечего продавать в этой категории", show_alert=True)
        return
    if not ok:
        await cb.answer()
        await _edit(cb.message, f"⚠️ Продать <b>{cnt}</b> предм. ({_BULK[mode][0]}) за <b>{_fmt(total)}</b> 🪙?",
                    _kb([[("✅ Продать", f"cs:bulk:{uid}:{mode}:1"), ("❌ Отмена", f"cs:inv:{uid}:0")]]))
        return
    r = sell_bulk(uid, mode)
    await cb.answer(f"+{_fmt(r['total'])} 🪙")
    t, k = inv_screen(uid, 0, f"💰 Продано <b>{r['count']}</b> предм. за <b>{_fmt(r['total'])}</b> 🪙 · баланс {_fmt(r['coins'])}")
    await _edit(cb.message, t, k)


async def _do_daily(cb, uid: int, name: str):
    get_profile(uid, name)
    r = claim_daily(uid)
    if not r["ok"]:
        await cb.answer(f"Бонус уже получен. Следующий через {_human(r['wait'])}", show_alert=True)
        return
    await cb.answer(f"+{_fmt(r['amount'])} 🪙")
    t, k = menu_screen(uid, name, f"🎁 Бонус дня: +<b>{_fmt(r['amount'])}</b> 🪙 · серия {r['streak']} дн. 🔥")
    await _edit(cb.message, t, k)


def _battle_invite(uid: int, name: str, ckey: str, bid: str) -> tuple[str, Kb]:
    c = CASES[ckey]
    text = (f"⚔️ <b>БАТЛ!</b>\n{_e(name)} ставит <b>{_fmt(c['price'])}</b> 🪙 на кейс {c['icon']} <b>{c['name']}</b>\n\n"
            "Каждому выпадет предмет из этого кейса — <b>у кого дороже, забирает оба</b>.\n"
            f"Кто примет вызов? ⏳ {BATTLE_TTL // 60} мин")
    return text, _kb([[(f"⚔️ Принять · {_fmt(c['price'])}🪙", f"cs:bacc:{bid}"), ("✖️ Отмена", f"cs:bcan:{bid}")]])


async def _do_battle_new(cb, uid: int, name: str, ckey: str, fresh_message: bool = False):
    if ckey not in CASES:
        await cb.answer("Нет такого кейса", show_alert=True)
        return
    get_profile(uid, name)
    target = cb.message
    if fresh_message:
        target = await cb.message.reply("⚔️ Готовлю батл…")
    r = create_battle(uid, name, ckey, target.chat.id, target.message_id)
    if not r["ok"]:
        await cb.answer(f"Не хватает {_fmt(r['need'])} 🪙", show_alert=True)
        if fresh_message:
            try:
                await target.delete()
            except Exception:
                pass
        return
    await cb.answer("Батл создан")
    t, k = _battle_invite(uid, name, ckey, r["id"])
    await _edit(target, t, k)


async def _do_battle_accept(cb, bid: str):
    uid, name = cb.from_user.id, _name_of(cb.from_user)
    res = accept_battle(bid, uid, _dn(uid, name))
    if not res["ok"]:
        reason = res["reason"]
        if reason == "closed":
            await cb.answer("Этот батл уже закрыт", show_alert=True)
        elif reason == "self":
            await cb.answer("Свой же вызов принять нельзя 😄 Жди соперника", show_alert=True)
        elif reason == "expired":
            await cb.answer("Вызов истёк, ставка вернулась создателю", show_alert=True)
            await _edit(cb.message, "⌛ Вызов истёк — ставка возвращена.", None)
        else:
            await cb.answer(f"Не хватает {_fmt(res['need'])} 🪙", show_alert=True)
        return
    await cb.answer("Батл начался!")
    try:
        await asyncio.wait_for(_anim_battle(cb.message, res), ANIM_TIMEOUT)
    except Exception:
        logger.exception("анимация батла упала")
    kb = _kb([[("🔁 Свой батл на этот кейс", f"cs:brev:{res['case']}")]])
    await _edit(cb.message, battle_result_text(res), kb)


async def _do_battle_cancel(cb, bid: str):
    r = cancel_battle(bid, cb.from_user.id)
    if r["ok"]:
        await cb.answer("Батл отменён, ставка возвращена")
        await _edit(cb.message, "✖️ Батл отменён — ставка возвращена.", None)
    elif r["reason"] == "not_owner":
        await cb.answer("Отменить может только тот, кто создал батл", show_alert=True)
    else:
        await cb.answer("Этот батл уже закрыт", show_alert=True)


async def on_callback(cb: types.CallbackQuery):
    try:
        await _dispatch(cb)
    except Exception:
        logger.exception("cases_game: ошибка в callback %r", getattr(cb, "data", None))
        try:
            await cb.answer("Что-то пошло не так 😵 Попробуй /case", show_alert=True)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  v2 UI: экраны меню/кейсов/топа, апгрейд, контракт, подарки, обмен, достижения
# ══════════════════════════════════════════════════════════════════════════════
def _title_of(uid: int) -> str:
    r = _db().execute("SELECT title FROM users WHERE user_id=?", (uid,)).fetchone()
    return r["title"] if r else ""


def _tt(title_key: str) -> str:
    return f" «{ACH[title_key][1]}»" if title_key in ACH else ""


def _dn(uid: int, name: str) -> str:
    """Имя с выбранным титулом (для топа, батлов, обмена)."""
    return f"{name}{_tags(uid, _title_of(uid))}"


def _mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{_e(name)}</a>'


def _human_long(secs: int) -> str:
    d, h = secs // 86400, (secs % 86400) // 3600
    return f"{d} д {h} ч" if d else _human(secs)


def _pct(p: float) -> str:
    return f"{p * 100:.1f}%" if p < 0.1 else f"{p * 100:.0f}%"


def _with_notices(uid: int, text: str) -> str:
    n = pop_notices(uid)
    return f"{text}\n\n{n}" if n else text


# ── экраны ────────────────────────────────────────────────────────────────────
def cases_screen(uid: int) -> tuple[str, Kb]:
    p = get_profile(uid)
    fs = free_status(uid)
    deal = deal_case()
    dc, dprice, dav = CASES[deal], deal_price(deal), deal_available(uid)
    lines = ["📦 <b>КЕЙСЫ</b>", f"🪙 Баланс: <b>{_fmt(p['coins'])}</b>\n",
             "🎟 <b>Бесплатный кейс дня:</b> " + ("готов!" if fs["ready"] else f"через {_human(fs['wait'])}"),
             f"💥 <b>Предложение дня:</b> {dc['icon']} {dc['name']} за <b>{_fmt(dprice)}</b> 🪙 "
             f"(−{round(DEAL_DISCOUNT * 100)}%)" + ("" if dav else " — уже использовано"), ""]
    rows = [[("🎟 Бесплатный кейс ✅" if fs["ready"] else "🎟 Бесплатный кейс ⏳", f"cs:free:{uid}")]]
    for k in SHOP:
        c = CASES[k]
        best = max(item_info(i)["price"] for t in c["weights"] for i in c["pool"][t])
        lines.append(f"{c['icon']} <b>{c['name']}</b> — {_fmt(c['price'])} 🪙 · до {_fmt(best)} 🪙\n   <i>{c['desc']}</i>")
        mark = "💥 " if (k == deal and dav) else ""
        rows.append([(f"{mark}{c['icon']} {c['name']} · {_fmt(c['price'])}🪙", f"cs:case:{uid}:{k}")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines), _kb(rows)


def case_screen(uid: int, ckey: str) -> tuple[str, Kb]:
    c = CASES[ckey]
    p = get_profile(uid)
    items = sorted({i for t in c["weights"] for i in c["pool"][t]}, key=lambda i: -item_info(i)["price"])[:5]
    text = (
        f"{c['icon']} <b>{c['name']}</b> — {_fmt(c['price'])} 🪙\n<i>{c['desc']}</i>\n\n"
        f"🎲 Шансы: {_chances(c)}\n"
        f"📊 Средняя выплата ≈ {_fmt(c['ev'])} 🪙 ({c['ev'] / c['price'] * 100:.0f}% цены)\n\n"
        "🔝 Самое ценное:\n" + "\n".join(f"{_it(i)} — {_fmt(item_info(i)['price'])} 🪙" for i in items)
    )
    rows = []
    if ckey == deal_case():
        dp = deal_price(ckey)
        if deal_available(uid):
            text += f"\n\n💥 <b>Предложение дня:</b> −{round(DEAL_DISCOUNT * 100)}% → <b>{_fmt(dp)}</b> 🪙 (1 раз в день)"
            rows.append([(f"💥 Со скидкой · {_fmt(dp)}🪙", f"cs:opend:{uid}:{ckey}")])
        else:
            text += "\n\n💥 Предложение дня на этот кейс сегодня уже использовано"
    text += f"\n\n🪙 Баланс: <b>{_fmt(p['coins'])}</b>"
    rows += [
        [(f"🎰 Открыть · {_fmt(c['price'])}🪙", f"cs:open:{uid}:{ckey}")],
        [(f"📦 ×5 сразу · {_fmt(c['price'] * 5)}🪙", f"cs:open5:{uid}:{ckey}")],
        [("⬅️ Кейсы", f"cs:cases:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")],
    ]
    return text, _kb(rows)


def free_drop_screen(uid: int, item: str, inv_id: int) -> tuple[str, Kb]:
    i = item_info(item)
    text = (f"🎟 <b>Бесплатный кейс дня</b>\n\n{_BANNER[i['rarity']]}\n\n{_it(item)}\n"
            f"💰 {_fmt(i['price'])} 🪙 — на халяву! Следующий кейс — завтра.")
    kb = _kb([
        [(f"💰 Продать за {_fmt(i['price'])}", f"cs:sell:{uid}:{inv_id}:0:-1")],
        [("📦 Кейсы", f"cs:cases:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")],
        [("🏠 Меню", f"cs:menu:{uid}")],
    ])
    return _with_notices(uid, text), kb


def top_screen(uid: int, mode: str = "all", chat_id: int | None = None) -> tuple[str, Kb]:
    group = chat_id is not None and chat_id < 0
    note = ""
    if mode == "chat" and not group:
        mode, note = "all", "<i>Чат-топ работает в группах — показываю общий.</i>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    if mode == "season":
        se = season_info()
        lb = season_leaderboard(None)
        lines = [f"📅 <b>СЕЗОН {se['no']}</b> · осталось <b>{_human_long(se['left'])}</b>",
                 "<i>рейтинг по ПРИРОСТУ капитала — стартуют все с нуля, прогресс не сбрасывается</i>",
                 "🏆 Призы: Кубки сезона топ-3 (30 000 / 15 000 / 8 000 🪙)\n"]
        for d in lb[:10]:
            mark = medals[d["rank"] - 1] if d["rank"] <= 3 else f"{d['rank']}."
            me = " ← ты" if d["user_id"] == uid else ""
            lines.append(f"{mark} {_e(d['name'])}{_tags(d['user_id'], d['title'])} — <b>{'+' if d['growth'] >= 0 else '−'}{_fmt(abs(d['growth']))}</b> 🪙{me}")
        mine = next((d for d in lb if d["user_id"] == uid), None)
        if mine and mine["rank"] > 10:
            lines.append(f"…\n{mine['rank']}. {_e(mine['name'])} — <b>{'+' if mine['growth'] >= 0 else '−'}{_fmt(abs(mine['growth']))}</b> 🪙 ← ты")
        if se["last"]:
            lines.append(f"\n🏁 <b>Сезон {se['no'] - 1}:</b> " + " · ".join(
                f"{medals[r['place'] - 1]} {_e(r['name'])} (+{_fmt(r['growth'])})" for r in se["last"]))
    else:
        lb = leaderboard(None, chat_id if mode == "chat" else None)
        title = "💬 <b>ТОП ЭТОГО ЧАТА</b>" if mode == "chat" else "🏆 <b>ТОП КЕЙС-КЛУБА</b>"
        lines = [f"{title}\n<i>капитал = монеты + стоимость инвентаря</i>\n"]
        for d in lb[:10]:
            mark = medals[d["rank"] - 1] if d["rank"] <= 3 else f"{d['rank']}."
            me = " ← ты" if d["user_id"] == uid else ""
            lines.append(f"{mark} {_e(d['name'])}{_tags(d['user_id'], d['title'])} — <b>{_fmt(d['networth'])}</b> 🪙 · ⚔️ {d['bw']}:{d['bl']}{me}")
        mine = next((d for d in lb if d["user_id"] == uid), None)
        if mine and mine["rank"] > 10:
            lines.append(f"…\n{mine['rank']}. {_e(mine['name'])} — <b>{_fmt(mine['networth'])}</b> 🪙 ← ты")
        if not lb:
            lines.append("Пока никого. Будь первым — /case")
    tabs = [("🌍 Все", "all"), ("💬 Чат", "chat"), ("📅 Сезон", "season")]
    row = [(f"✔️ {t}" if m == mode else t, f"cs:top:{uid}:{m}") for t, m in tabs]
    return note + "\n".join(lines), _kb([row, [("🏠 Меню", f"cs:menu:{uid}")]])


def battle_pick_screen(uid: int) -> tuple[str, Kb]:
    p = get_profile(uid)
    rows = [[(f"{CASES[k]['icon']} {CASES[k]['name']} · {_fmt(CASES[k]['price'])}🪙", f"cs:bnew:{uid}:{k}")] for k in SHOP]
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    text = ("⚔️ <b>БАТЛ</b>\nВыбери кейс: ты ставишь его цену, соперник — столько же. Каждому выпадает предмет "
            f"из этого кейса — <b>у кого дороже, забирает оба</b>.\n\n🪙 Баланс: <b>{_fmt(p['coins'])}</b>")
    return text, _kb(rows)


def ach_screen(uid: int) -> tuple[str, Kb]:
    have = get_achievements(uid)
    cur = _title_of(uid)
    lines = [f"🏅 <b>ДОСТИЖЕНИЯ</b> · {len(have)}/{len(ACH)}",
             "<i>Выбранное достижение показывается титулом рядом с именем в топе, батлах и обмене.</i>\n"]
    btns = []
    for k, (icon, name, desc, _) in ACH.items():
        if k in have:
            lines.append(f"✅ {icon} <b>{name}</b> — {desc}" + (" ← титул" if k == cur else ""))
            btns.append((f"{'✔️ ' if k == cur else ''}{icon} {name}", f"cs:title:{uid}:{k}"))
        else:
            lines.append(f"🔒 {icon} {name} — {desc}")
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if cur:
        rows.append([("❎ Снять титул", f"cs:title:{uid}:-")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines), _kb(rows)


# ── универсальный экран выбора предмета ───────────────────────────────────────
def _pick_screen(owner: int, page: int, title: str, item_cb, page_cb, back_cb, *, asc: bool = False,
                 marked=frozenset(), footer: str = "", extra=(), empty: str = "🎒 Пусто") -> tuple[str, Kb]:
    pg = inventory_page(owner, page, asc=asc, no_trophy=True)
    if not pg["total"]:
        return f"{title}\n\n{empty}", _kb([[("⬅️ Назад", back_cb)]])
    lines = [f"{title} · стр. {pg['page'] + 1}/{pg['pages']}"]
    btns = []
    for n, (inv_id, item) in enumerate(pg["items"], 1):
        on = inv_id in marked
        lines.append(f"{n}. {'✅ ' if on else ''}{_it(item)} — {_fmt(item_info(item)['price'])} 🪙")
        btns.append((f"{'✅' if on else '👉'} {n}", item_cb(inv_id, pg["page"])))
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    nav = []
    if pg["page"] > 0:
        nav.append(("◀️", page_cb(pg["page"] - 1)))
    nav.append((f"{pg['page'] + 1}/{pg['pages']}", page_cb(pg["page"])))
    if pg["page"] < pg["pages"] - 1:
        nav.append(("▶️", page_cb(pg["page"] + 1)))
    rows.append(nav)
    rows.extend(extra)
    rows.append([("⬅️ Назад", back_cb)])
    if footer:
        lines.append("\n" + footer)
    return "\n".join(lines), _kb(rows)


# ── апгрейд ───────────────────────────────────────────────────────────────────
def up_pick_screen(uid: int, page: int = 0) -> tuple[str, Kb]:
    return _pick_screen(uid, page, "🚀 <b>АПГРЕЙД</b>: выбери предмет", lambda i, pg: f"cs:upi:{uid}:{i}",
                        lambda pg: f"cs:up:{uid}:{pg}", f"cs:menu:{uid}",
                        footer=f"<i>Шанс честный: {round(RTP * 100)}% × цена предмета / цена цели. Не повезёт — предмет сгорит.</i>",
                        empty="🎒 Нечем рисковать. Открой кейс!")


def up_options_screen(uid: int, inv_id: int) -> tuple[str, Kb]:
    item = get_inv_item(uid, inv_id)
    if item is None:
        return "Этот предмет уже не в инвентаре.", _kb([[("🚀 К апгрейду", f"cs:up:{uid}:0")]])
    opts = upgrade_options(item)
    if not opts:
        return (f"🚀 {_it(item)}\n\nДля этого предмета нет вариантов апгрейда.",
                _kb([[("🚀 Выбрать другой", f"cs:up:{uid}:0")]]))
    p0 = item_info(item)["price"]
    lines = [f"🚀 <b>АПГРЕЙД</b>\nРискуешь: {_it(item)} — {_fmt(p0)} 🪙\n"]
    rows = []
    for o in opts:
        t = item_info(o["target"])
        lines.append(f"×{o['mult']:.1f} → {_it(o['target'])} — {_fmt(t['price'])} 🪙 · шанс <b>{_pct(o['chance'])}</b>")
        label = f"🚀 ×{o['mult']:.1f} · {_pct(o['chance'])} → {_cut_name(t['name'])}"
        rows.append([(label, f"cs:upgo:{uid}:{inv_id}:{o['target']}")])
    rows.append([("⬅️ Другой предмет", f"cs:up:{uid}:0")])
    lines.append("\n<i>Не повезёт — предмет сгорит. Цели выбираются случайно при каждом открытии экрана.</i>")
    return "\n".join(lines), _kb(rows)


def _cut_name(s: str, n: int = 24) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


async def _anim_upgrade(msg, res: dict):
    cells = 10
    p, r, ok = res["chance"], res["r"], res["success"]
    wc = max(1, min(cells - 1, round(p * cells)))
    if ok:
        final = min(wc - 1, int(r / p * wc))
    else:
        final = wc + min(cells - wc - 1, int((r - p) / (1 - p) * (cells - wc)))
    bar = "🟩" * wc + "🟥" * (cells - wc)
    offs = [None, -31, -24, -18, -13, -9, -6, -3, -2, 2, -1, 0]
    delays = [0.8, 0.45, 0.45, 0.5, 0.6, 0.75, 0.9, 1.0, 1.0, 0.9, 0.9, 0.8]
    notes = ["заряжаем…", "набираем ход", "набираем ход", "летит", "летит", "замедляется", "замедляется",
             "почти…", "ну же…", "ещё чуть-чуть…", "дрожит…", "СТОП!"]
    head = (f"🚀 <b>АПГРЕЙД</b>\n{_it(res['src'])} → {_it(res['target'])}\n"
            f"шанс <b>{_pct(p)}</b> <i>(шкала условная)</i>\n\n")
    for k, off in enumerate(offs):
        pos = final if off is None and k == len(offs) - 1 else (final + (off or 0)) % cells
        if k == len(offs) - 1:
            pos = final
        pointer = "▫️" * pos + "🔻" + "▫️" * (cells - 1 - pos)
        await _frame(msg, head + f"{pointer}\n{bar}\n\n<i>{'◐◓◑◒'[k % 4]} {notes[k]}</i>")
        await asyncio.sleep(delays[k])


async def _do_upgrade(cb, uid: int, inv_id: int, target: str):
    if uid in _BUSY:
        await cb.answer("Погоди, ещё идёт действие 🎰")
        return
    res = upgrade_item(uid, inv_id, target)
    if not res["ok"]:
        await cb.answer("Не получилось: предмет уже не доступен для апгрейда", show_alert=True)
        t, k = up_pick_screen(uid, 0)
        await _edit(cb.message, t, k)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        try:
            await asyncio.wait_for(_anim_upgrade(cb.message, res), ANIM_TIMEOUT)
        except Exception:
            logger.exception("анимация апгрейда упала")
        if res["success"]:
            price = item_info(res["target"])["price"]
            text = (f"🟢 <b>УСПЕХ!</b> (шанс был {_pct(res['chance'])})\n\n{_it(res['target'])}\n"
                    f"💰 {_fmt(price)} 🪙 · ×{price / item_info(res['src'])['price']:.1f} к цене {_it(res['src'])}")
            rows = [[(f"💰 Продать за {_fmt(price)}", f"cs:sell:{uid}:{res['inv_id']}:0:-1")]]
        else:
            text = (f"🔴 <b>Не повезло…</b> (шанс был {_pct(res['chance'])})\n\n"
                    f"{_it(res['src'])} сгорел 🔥\nЦель была: {_it(res['target'])}")
            rows = []
        rows += [[("🚀 Ещё апгрейд", f"cs:up:{uid}:0"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")], [("🏠 Меню", f"cs:menu:{uid}")]]
        await _edit(cb.message, _with_notices(uid, text), _kb(rows))
    finally:
        _BUSY.discard(uid)


# ── контракт ──────────────────────────────────────────────────────────────────
def ct_screen(uid: int) -> tuple[str, Kb]:
    items = basket_items(uid)
    total = sum(item_info(i)["price"] for _, i in items)
    outcomes = " · ".join(f"×{m:g} — {_pct(w)}" for m, w in CONTRACT_OUTCOMES)
    lines = [f"🧾 <b>КОНТРАКТ</b>\nОбменяй <b>{CONTRACT_MIN}–{CONTRACT_MAX}</b> предметов на один случайный.\n",
             f"В контракте: <b>{len(items)}</b>/{CONTRACT_MAX} на <b>{_fmt(total)}</b> 🪙"]
    for n, (_, i) in enumerate(items, 1):
        lines.append(f"{n}. {_it(i)} — {_fmt(item_info(i)['price'])} 🪙")
    lines.append(f"\n🎲 Итоговая цена = сумма × случайный множитель:\n{outcomes}")
    if items:
        lines.append(f"📊 Средний возврат при такой сумме ≈ <b>{contract_expected_ratio(total) * 100:.0f}%</b>")
    rows = [[("➕ Выбрать предметы", f"cs:ctp:{uid}:0")]]
    if len(items) >= CONTRACT_MIN:
        rows.append([(f"🧾 Подписать · {len(items)} шт.", f"cs:ctg:{uid}")])
    if items:
        rows.append([("🧹 Очистить", f"cs:ctc:{uid}")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines), _kb(rows)


def ct_pick_screen(uid: int, page: int = 0) -> tuple[str, Kb]:
    items = basket_items(uid)
    total = sum(item_info(i)["price"] for _, i in items)
    return _pick_screen(uid, page, "🧾 <b>КОНТРАКТ</b>: отметь предметы", lambda i, pg: f"cs:ctt:{uid}:{i}:{pg}",
                        lambda pg: f"cs:ctp:{uid}:{pg}", f"cs:ct:{uid}", asc=True, marked={i for i, _ in items},
                        footer=f"🧾 Выбрано: <b>{len(items)}</b>/{CONTRACT_MAX} на {_fmt(total)} 🪙",
                        extra=[[("✅ Готово → к контракту", f"cs:ct:{uid}")]], empty="🎒 Пусто — нечего вкладывать")


async def _do_contract_sign(cb, uid: int):
    if uid in _BUSY:
        await cb.answer("Погоди, ещё идёт действие 🎰")
        return
    res = sign_contract(uid)
    if not res["ok"]:
        await cb.answer(f"Нужно от {CONTRACT_MIN} до {CONTRACT_MAX} предметов (сейчас {res.get('n', 0)})", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        try:
            for k in range(4):
                await _frame(cb.message, f"🧾 <b>КОНТРАКТ</b>\n\n{res['n']} предметов на <b>{_fmt(res['total'])}</b> 🪙\n\n"
                                        f"{'📄' * (k + 1)}{'▫️' * (3 - k)}\n<i>{'◐◓◑◒'[k]} подписываем и считаем…</i>")
                await asyncio.sleep(0.8)
        except Exception:
            logger.exception("анимация контракта упала")
        price = item_info(res["item"])["price"]
        ratio = price / res["total"]
        verdict = "🟢 в плюс!" if ratio > 1 else ("🟡 почти вернул" if ratio > 0.75 else "🔴 в минус")
        text = (f"🧾 <b>КОНТРАКТ ПОДПИСАН</b>\n{res['n']} предметов на {_fmt(res['total'])} 🪙 →\n\n"
                f"{_BANNER[item_info(res['item'])['rarity']]}\n{_it(res['item'])}\n"
                f"💰 {_fmt(price)} 🪙 · ×{ratio:.2f} от вложенного · {verdict}")
        rows = [[(f"💰 Продать за {_fmt(price)}", f"cs:sell:{uid}:{res['inv_id']}:0:-1")],
                [("🧾 Новый контракт", f"cs:ct:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")], [("🏠 Меню", f"cs:menu:{uid}")]]
        await _edit(cb.message, _with_notices(uid, text), _kb(rows))
    finally:
        _BUSY.discard(uid)


# ── подарки ───────────────────────────────────────────────────────────────────
def _give_err(r: dict) -> str:
    return {
        "self": "Себе нельзя 😄",
        "min": f"Минимальный перевод — {GIVE_MIN} 🪙",
        "no_target": "Этот игрок ещё не играл — пусть откроет /case",
        "money": f"Не хватает {_fmt(r.get('need', 0))} 🪙",
        "cap": f"Лимит подарков на сутки: осталось {_fmt(r.get('left', 0))} 🪙-стоимости",
        "gone": "Этого предмета уже нет в инвентаре",
        "trophy": "Кубки сезона нельзя передавать — это память о победе 🏆",
    }.get(r.get("reason"), "Не получилось")


def give_coin_screen(uid: int, tid: int, amount: int) -> tuple[str, Kb]:
    tname = user_name(tid) or str(tid)
    fee = int(amount * GIVE_FEE)
    text = (f"🎁 <b>Перевод монет</b>\n{_mention(tid, tname)} получит <b>{_fmt(amount - fee)}</b> 🪙 "
            f"(ты отдаёшь <b>{_fmt(amount)}</b>, комиссия {round(GIVE_FEE * 100)}% = {_fmt(fee)})\n"
            f"Лимит подарков на сутки: осталось {_fmt(give_left(uid))} 🪙")
    return text, _kb([[("✅ Перевести", f"cs:gc:{uid}:{tid}:{amount}:1"), ("❌ Отмена", f"cs:gx:{uid}")]])


def give_item_pick_screen(uid: int, tid: int, page: int = 0) -> tuple[str, Kb]:
    tname = user_name(tid) or str(tid)
    return _pick_screen(uid, page, f"🎁 <b>Что подарить</b> → {_e(tname)}", lambda i, pg: f"cs:gi:{uid}:{tid}:{i}:0",
                        lambda pg: f"cs:gip:{uid}:{tid}:{pg}", f"cs:gx:{uid}",
                        footer="<i>Монетами: <code>/give 500</code> ответом на сообщение игрока</i>",
                        empty="🎒 Дарить нечего")


async def _do_give_coins(cb, uid: int, tid: int, amount: int, ok: int):
    tname = user_name(tid)
    if tname is None:
        await cb.answer(_give_err({"reason": "no_target"}), show_alert=True)
        return
    if not ok:
        await cb.answer()
        t, k = give_coin_screen(uid, tid, amount)
        await _edit(cb.message, t, k)
        return
    r = give_coins(uid, tid, amount)
    if not r["ok"]:
        await cb.answer(_give_err(r), show_alert=True)
        return
    await cb.answer("Переведено!")
    me = user_name(uid) or str(uid)
    await _edit(cb.message, f"🎁 {_e(_dn(uid, me))} перевёл {_mention(tid, tname)} "
                            f"<b>{_fmt(r['got'])}</b> 🪙 (комиссия {_fmt(r['fee'])})", None)


async def _do_give_item(cb, uid: int, tid: int, inv_id: int, ok: int):
    tname = user_name(tid)
    item = get_inv_item(uid, inv_id)
    if tname is None or item is None:
        await cb.answer(_give_err({"reason": "no_target" if tname is None else "gone"}), show_alert=True)
        return
    if not ok:
        await cb.answer()
        price = item_info(item)["price"]
        await _edit(cb.message, f"🎁 Подарить {_it(item)} ({_fmt(price)} 🪙) → {_mention(tid, tname)}?\n"
                                f"Лимит подарков на сутки: осталось {_fmt(give_left(uid))} 🪙",
                    _kb([[("✅ Подарить", f"cs:gi:{uid}:{tid}:{inv_id}:1"), ("❌ Отмена", f"cs:gx:{uid}")]]))
        return
    r = give_item(uid, tid, inv_id)
    if not r["ok"]:
        await cb.answer(_give_err(r), show_alert=True)
        return
    await cb.answer("Подарено!")
    me = user_name(uid) or str(uid)
    await _edit(cb.message, f"🎁 {_e(_dn(uid, me))} подарил {_mention(tid, tname)}: {_it(r['item'])} "
                            f"({_fmt(r['price'])} 🪙)", None)


# ── обмен ─────────────────────────────────────────────────────────────────────
def trade_mine_screen(uid: int, tid: int, page: int = 0) -> tuple[str, Kb]:
    tname = user_name(tid) or str(tid)
    return _pick_screen(uid, page, f"🤝 <b>ОБМЕН</b> с {_e(tname)}: выбери <b>свой</b> предмет",
                        lambda i, pg: f"cs:trs:{uid}:{tid}:{i}", lambda pg: f"cs:trp:{uid}:{tid}:{pg}",
                        f"cs:gx:{uid}", empty="🎒 Тебе нечего предложить")


def trade_theirs_screen(uid: int, tid: int, myinv: int, page: int = 0) -> tuple[str, Kb]:
    tname = user_name(tid) or str(tid)
    mine = get_inv_item(uid, myinv)
    return _pick_screen(tid, page, f"🤝 Ты отдаёшь {_it(mine) if mine else '—'}. Что хочешь у <b>{_e(tname)}</b>?",
                        lambda i, pg: f"cs:trc:{uid}:{tid}:{myinv}:{i}",
                        lambda pg: f"cs:trq:{uid}:{tid}:{myinv}:{pg}", f"cs:trp:{uid}:{tid}:0",
                        empty="У игрока нет подходящих предметов")


def trade_offer(t: dict, a_item: str, b_item: str) -> tuple[str, Kb]:
    pa, pb = item_info(a_item)["price"], item_info(b_item)["price"]
    text = (f"🤝 <b>ОБМЕН</b>\n{_mention(t['a'], t['a_name'])} предлагает:\n{_it(a_item)} — {_fmt(pa)} 🪙\n\n"
            f"на предмет {_mention(t['b'], t['b_name'])}:\n{_it(b_item)} — {_fmt(pb)} 🪙\n\n"
            f"{_mention(t['b'], t['b_name'])}, принимаешь? ⏳ {TRADE_TTL // 60} мин")
    return text, _kb([[("✅ Принять", f"cs:tra:{t['id']}"), ("❌ Отклонить", f"cs:trd:{t['id']}"),
                       ("✖️ Отмена", f"cs:trx:{t['id']}")]])


async def _do_trade_create(cb, uid: int, tid: int, myinv: int, theirinv: int):
    me, tname = user_name(uid) or str(uid), user_name(tid)
    if tname is None:
        await cb.answer(_give_err({"reason": "no_target"}), show_alert=True)
        return
    r = create_trade(uid, _dn(uid, me), tid, _dn(tid, tname), myinv, theirinv, cb.message.chat.id, cb.message.message_id)
    if not r["ok"]:
        await cb.answer("Предмет уже изменился — выбери заново" if r["reason"] == "gone"
                        else _give_err(r), show_alert=True)
        return
    await cb.answer("Предложение отправлено")
    t = get_trade(r["id"])
    text, kb = trade_offer(t, r["a_item"], r["b_item"])
    await _edit(cb.message, text, kb)


async def _do_trade_answer(cb, tid_: str, kind: str):
    uid = cb.from_user.id
    if kind == "accept":
        r = accept_trade(tid_, uid)
        if r["ok"]:
            await cb.answer("Обмен состоялся!")
            await _edit(cb.message, f"🤝 <b>ОБМЕН СОСТОЯЛСЯ</b>\n{_mention(r['a'], r['a_name'])} получил {_it(r['b_item'])}\n"
                                    f"{_mention(r['b'], r['b_name'])} получил {_it(r['a_item'])}", None)
            return
        msg = {"closed": "Это предложение уже закрыто", "not_yours": "Принять может только адресат обмена",
               "expired": "Предложение истекло", "gone": "Один из предметов уже продан или передан — обмен отменён"}
        await cb.answer(msg.get(r["reason"], "Не получилось"), show_alert=True)
        if r["reason"] in ("expired", "gone"):
            await _edit(cb.message, "⌛ Обмен недействителен." if r["reason"] == "gone" else "⌛ Предложение истекло.", None)
        return
    r = close_trade(tid_, uid, "cancel" if kind == "cancel" else "decline")
    if r["ok"]:
        await cb.answer("Готово")
        await _edit(cb.message, "✖️ Обмен отменён инициатором." if kind == "cancel" else "❌ Обмен отклонён.", None)
    elif r["reason"] == "not_yours":
        await cb.answer("Отменить может только инициатор, отклонить — только адресат", show_alert=True)
    else:
        await cb.answer("Это предложение уже закрыто", show_alert=True)


# ── анимация открытия (обновлена: бесплатный кейс) ────────────────────────────
async def _anim_open(msg, ckey: str, drop: str):
    c = CASES[ckey]
    price_txt = "бесплатно" if c["price"] == 0 else f"{_fmt(c['price'])} 🪙"
    track = [roll_item(ckey) for _ in range(64)]
    center = 56
    track[center] = drop
    if item_info(drop)["rarity"] in ("junk", "common") and _RNG.random() < 0.5:      # «почти выпало»
        top_tier = max(c["weights"], key=lambda t: RARITY_ORDER.index(t))
        track[center + 1] = _RNG.choice(c["pool"][top_tier])
    positions = [8, 17, 26, 34, 41, 47, 51, 54, 55, 56]
    delays = [0.45, 0.45, 0.5, 0.55, 0.65, 0.8, 0.95, 1.1, 1.2, 0.9]
    notes = ["щёлк…", "крутится", "крутится", "крутится", "замедляется", "замедляется", "почти…", "тихо-тихо…",
             "ещё чуть-чуть…", "СТОП!"]
    for k, p in enumerate(positions):
        cells = []
        for d in range(-4, 5):
            em = RARITY[item_info(track[p + d])["rarity"]][0]
            cells.append(f"【{em}】" if d == 0 else em)
        cur = item_info(track[p])
        text = (f"{c['icon']} <b>{c['name']}</b> · {price_txt}\n\n"
                f"{''.join(cells)}\n"
                f"{'🔽' if k < len(positions) - 1 else '✅'} <b>{_e(cur['name'])}</b>\n\n"
                f"<i>{'◐◓◑◒'[k % 4]} {notes[k]}</i>")
        await _frame(msg, text)
        await asyncio.sleep(delays[k])
    r = item_info(drop)["rarity"]
    if r in ("epic", "legend", "mythic"):
        em = RARITY[r][0]
        for k in range(2):
            flash = (em * 5) if k == 0 else ("✨" + em) * 3
            await _frame(msg, f"{flash}\n\n{_it(drop)}\n\n{flash}")
            await asyncio.sleep(0.55)


async def _do_open(cb, uid: int, name: str, ckey: str, deal: bool = False):
    if ckey not in SHOP:
        await cb.answer("Нет такого кейса", show_alert=True)
        return
    if uid in _BUSY:
        await cb.answer("Погоди, кейс ещё открывается 🎰")
        return
    get_profile(uid, name)
    res = open_case(uid, ckey, 1, None, deal)
    if not res["ok"]:
        reason = res.get("reason")
        if reason == "deal_used":
            await cb.answer("Предложение дня уже использовано сегодня", show_alert=True)
        elif reason == "bad":
            await cb.answer("Это предложение уже не действует — кейс дня сменился", show_alert=True)
        else:
            b = maybe_bailout(uid)
            extra = "\n🆘 Но ты на нуле — загляни в меню за пособием." if b["given"] else ""
            await cb.answer(f"Не хватает {_fmt(res['need'])} 🪙{extra}", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        item, inv_id = res["drops"][0]
        try:
            await asyncio.wait_for(_anim_open(cb.message, ckey, item), ANIM_TIMEOUT)
        except Exception:
            logger.exception("анимация кейса упала — показываю дроп")
        text, kb = drop_screen(uid, ckey, item, inv_id, res["coins"], res["cost"])
        await _edit(cb.message, _with_notices(uid, text), kb)
    finally:
        _BUSY.discard(uid)


async def _do_free(cb, uid: int, name: str):
    if uid in _BUSY:
        await cb.answer("Погоди, кейс ещё открывается 🎰")
        return
    get_profile(uid, name)
    res = open_free(uid)
    if not res["ok"]:
        await cb.answer(f"Бесплатный кейс уже открыт. Следующий через {_human(res['wait'])}", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        try:
            await asyncio.wait_for(_anim_open(cb.message, "free", res["item"]), ANIM_TIMEOUT)
        except Exception:
            logger.exception("анимация бесплатного кейса упала")
        text, kb = free_drop_screen(uid, res["item"], res["inv_id"])
        await _edit(cb.message, text, kb)
    finally:
        _BUSY.discard(uid)


# ── команды ───────────────────────────────────────────────────────────────────
def _touch(user, chat_id) -> tuple[int, str]:
    uid, name = user.id, _name_of(user)
    get_profile(uid, name, None, getattr(user, "username", None) or "")
    touch_chat(uid, chat_id)
    return uid, name


async def cmd_case(message: types.Message):
    if not message.from_user:
        return
    uid, name = _touch(message.from_user, message.chat.id)
    text, kb = menu_screen(uid, name)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def cmd_daily(message: types.Message):
    if not message.from_user:
        return
    uid, name = _touch(message.from_user, message.chat.id)
    r = claim_daily(uid)
    if r["ok"]:
        txt = (f"🎁 <b>Бонус дня:</b> +<b>{_fmt(r['amount'])}</b> 🪙\n🔥 Серия: {r['streak']} дн. "
               f"(завтра будет больше, максимум на {DAILY_STREAK_MAX + 1}-й день)")
    else:
        txt = f"🎁 Бонус уже получен. Следующий через <b>{_human(r['wait'])}</b>."
    fs = free_status(uid)
    txt += "\n🎟 Бесплатный кейс дня ждёт тебя!" if fs["ready"] else ""
    txt = _with_notices(uid, txt)
    await message.answer(txt, parse_mode="HTML", reply_markup=_kb([[("🏠 Меню", f"cs:menu:{uid}"),
                                                                   ("📦 Кейсы", f"cs:cases:{uid}")]]))


async def cmd_inv(message: types.Message):
    if not message.from_user:
        return
    uid, _ = _touch(message.from_user, message.chat.id)
    text, kb = inv_screen(uid, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def cmd_ctop(message: types.Message):
    if not message.from_user:
        return
    uid, _ = _touch(message.from_user, message.chat.id)
    text, kb = top_screen(uid, "chat" if message.chat.id < 0 else "all", message.chat.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


def _resolve_target(message, args: list[str]) -> tuple[int | None, str]:
    uid = message.from_user.id
    tid = None
    rt = getattr(message, "reply_to_message", None)
    if rt is not None and rt.from_user is not None and not getattr(rt.from_user, "is_bot", False):
        tid = rt.from_user.id
    else:
        for a in args:
            if a.startswith("@") and len(a) > 1:
                tid = find_user_by_username(a)
                break
    if tid is None:
        return None, "Ответь командой на сообщение игрока или укажи его @username"
    if tid == uid:
        return None, "Себе нельзя 😄"
    if not user_exists(tid):
        return None, "Этот игрок ещё не играл — пусть откроет /case"
    return tid, ""


async def cmd_give(message: types.Message):
    if not message.from_user:
        return
    uid, _ = _touch(message.from_user, message.chat.id)
    args = (message.text or "").split()[1:]
    tid, err = _resolve_target(message, args)
    if err:
        await message.answer(f"🎁 {err}\n\nМонеты: <code>/give 500</code> · предмет: <code>/give</code> "
                             "(ответом на сообщение игрока)", parse_mode="HTML")
        return
    amount = next((int(a) for a in args if a.isdigit()), None)
    if amount is not None:
        pre = give_coins.__wrapped__ if False else None                    # (проверка без списания ниже)
        if amount < GIVE_MIN:
            await message.answer(_give_err({"reason": "min"}))
            return
        p = get_profile(uid)
        if p["coins"] < amount:
            await message.answer(_give_err({"reason": "money", "need": amount - p["coins"]}))
            return
        if amount > give_left(uid):
            await message.answer(_give_err({"reason": "cap", "left": give_left(uid)}))
            return
        text, kb = give_coin_screen(uid, tid, amount)
    else:
        text, kb = give_item_pick_screen(uid, tid, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def cmd_trade(message: types.Message):
    if not message.from_user:
        return
    uid, _ = _touch(message.from_user, message.chat.id)
    tid, err = _resolve_target(message, (message.text or "").split()[1:])
    if err:
        await message.answer(f"🤝 {err}", parse_mode="HTML")
        return
    text, kb = trade_mine_screen(uid, tid, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── диспетчер callback ────────────────────────────────────────────────────────
async def _dispatch(cb: types.CallbackQuery):
    parts = (cb.data or "").split(":")
    if len(parts) < 3 or cb.message is None:
        await cb.answer("Эта кнопка устарела", show_alert=True)
        return
    act = parts[1]
    chat_id = cb.message.chat.id
    # кнопки без владельца: нажать может любой (внутри проверяется роль)
    if act in ("bacc", "bcan", "brev", "tra", "trd", "trx"):
        touch_chat(cb.from_user.id, chat_id)
    if act == "bacc":
        return await _do_battle_accept(cb, parts[2])
    if act == "bcan":
        return await _do_battle_cancel(cb, parts[2])
    if act == "brev":                                    # реванш/свой батл — для любого нажавшего
        return await _do_battle_new(cb, cb.from_user.id, _dn(cb.from_user.id, _name_of(cb.from_user)), parts[2], fresh_message=True)
    if act == "tra":
        return await _do_trade_answer(cb, parts[2], "accept")
    if act == "trd":
        return await _do_trade_answer(cb, parts[2], "decline")
    if act == "trx":
        return await _do_trade_answer(cb, parts[2], "cancel")
    uid = int(parts[2])
    if cb.from_user.id != uid:
        await cb.answer("Это меню другого игрока. Своё — /case", show_alert=True)
        return
    name = _name_of(cb.from_user)
    get_profile(uid, name, None, getattr(cb.from_user, "username", None) or "")
    touch_chat(uid, chat_id)
    msg = cb.message
    if act == "menu":
        await cb.answer()
        t, k = menu_screen(uid, name)
    elif act == "cases":
        await cb.answer()
        t, k = cases_screen(uid)
    elif act == "case":
        if parts[3] not in SHOP:
            await cb.answer("Нет такого кейса", show_alert=True)
            return
        await cb.answer()
        t, k = case_screen(uid, parts[3])
    elif act == "open":
        return await _do_open(cb, uid, name, parts[3])
    elif act == "opend":
        return await _do_open(cb, uid, name, parts[3], deal=True)
    elif act == "open5":
        if parts[3] not in SHOP:
            await cb.answer("Нет такого кейса", show_alert=True)
            return
        return await _do_open5(cb, uid, name, parts[3])
    elif act == "free":
        return await _do_free(cb, uid, name)
    elif act == "inv":
        await cb.answer()
        t, k = inv_screen(uid, int(parts[3]))
    elif act == "sell":
        return await _do_sell(cb, uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act == "bulk":
        return await _do_bulk(cb, uid, parts[3], int(parts[4]))
    elif act == "daily":
        return await _do_daily(cb, uid, name)
    elif act == "top":
        await cb.answer()
        t, k = top_screen(uid, parts[3] if len(parts) > 3 else "all", chat_id)
    elif act == "prof":
        await cb.answer()
        t, k = profile_screen(uid, name)
    elif act == "battle":
        await cb.answer()
        t, k = battle_pick_screen(uid)
    elif act == "bnew":
        if parts[3] not in SHOP:
            await cb.answer("Нет такого кейса", show_alert=True)
            return
        return await _do_battle_new(cb, uid, _dn(uid, name), parts[3])
    elif act == "ach":
        await cb.answer()
        t, k = ach_screen(uid)
    elif act == "title":
        if set_title(uid, parts[3]):
            await cb.answer("Титул выбран" if parts[3] != "-" else "Титул снят")
        else:
            await cb.answer("Это достижение ещё не получено", show_alert=True)
        t, k = ach_screen(uid)
    elif act == "up":
        await cb.answer()
        t, k = up_pick_screen(uid, int(parts[3]))
    elif act == "upi":
        await cb.answer()
        t, k = up_options_screen(uid, int(parts[3]))
    elif act == "upgo":
        return await _do_upgrade(cb, uid, int(parts[3]), parts[4])
    elif act == "ct":
        await cb.answer()
        t, k = ct_screen(uid)
    elif act == "ctp":
        await cb.answer()
        t, k = ct_pick_screen(uid, int(parts[3]))
    elif act == "ctt":
        r = basket_toggle(uid, int(parts[3]))
        if not r["ok"]:
            await cb.answer("В контракт можно положить не больше 10 предметов" if r["reason"] == "full"
                            else "Этот предмет нельзя вложить", show_alert=True)
        else:
            await cb.answer("Добавлено" if r["added"] else "Убрано")
        t, k = ct_pick_screen(uid, int(parts[4]))
    elif act == "ctc":
        basket_clear(uid)
        await cb.answer("Корзина очищена")
        t, k = ct_screen(uid)
    elif act == "ctg":
        return await _do_contract_sign(cb, uid)
    elif act == "gx":
        await cb.answer("Отменено")
        t, k = "❌ Отменено.", None
    elif act == "gc":
        return await _do_give_coins(cb, uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act == "gip":
        await cb.answer()
        t, k = give_item_pick_screen(uid, int(parts[3]), int(parts[4]))
    elif act == "gi":
        return await _do_give_item(cb, uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act == "trp":
        await cb.answer()
        t, k = trade_mine_screen(uid, int(parts[3]), int(parts[4]))
    elif act == "trs":
        if get_inv_item(uid, int(parts[4])) is None:
            await cb.answer("Предмет уже не в инвентаре", show_alert=True)
            return
        await cb.answer()
        t, k = trade_theirs_screen(uid, int(parts[3]), int(parts[4]), 0)
    elif act == "trq":
        await cb.answer()
        t, k = trade_theirs_screen(uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act == "trc":
        return await _do_trade_create(cb, uid, int(parts[3]), int(parts[4]), int(parts[5]))
    elif act in _XACTS:
        return await _dispatch_x(cb, act, uid, name, parts)
    else:
        await cb.answer("Неизвестная кнопка", show_alert=True)
        return
    await _edit(msg, t, k)


# ══════════════════════════════════════════════════════════════════════════════
#  v4: защищённые правки сообщений (анимации не зависают при флуд-лимитах) и админ-панель
# ══════════════════════════════════════════════════════════════════════════════
try:
    from edit_guard import safe_edit as _guard_edit      # рядом должен лежать edit_guard.py
except Exception:                                        # без него — прежнее (менее надёжное) поведение
    _guard_edit = None
    logging.getLogger(__name__).warning("edit_guard.py не найден — анимации могут зависать при флуд-лимитах Telegram. "
                                        "Положите edit_guard.py рядом с остальными модулями.")


async def _edit(msg, text: str, kb: Kb | None = None, *, final: bool = True) -> bool:
    """Итоговая правка (по умолчанию): доставляется обязательно — ждёт флуд-паузу и повторяется."""
    if _guard_edit is not None:
        return await _guard_edit(msg, text, kb, final=final)
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return True
    except Exception as e:
        err = str(e).lower()
        if "retry after" in err:
            m = re.search(r"retry after (\d+)", err)
            await asyncio.sleep(min(int(m.group(1)) if m else 2, 5))
        elif "not modified" not in err:
            logger.warning("edit не удался: %s", e)
        return False


async def _frame(msg, text: str) -> bool:
    """Кадр анимации: необязательный — при перегрузке чата молча пропускается (анимация не виснет)."""
    return await _edit(msg, text, None, final=False)


def _tags(uid: int, title_key: str = "") -> str:
    """Теги рядом с именем: «🛡 Админ» (автоматически для админов) + выбранный титул."""
    return (" «🛡 Админ»" if is_admin(uid) else "") + _tt(title_key)


def _parse_amount(tok: str) -> int | None:
    """500, 1_000, 5k, 2.5k, 1m, 3к, 2м → число; иначе None."""
    t = tok.lower().replace("_", "").replace(",", ".")
    mult = 1
    if t.endswith(("k", "к")):
        mult, t = 1000, t[:-1]
    elif t.endswith(("m", "м")):
        mult, t = 1_000_000, t[:-1]
    try:
        v = float(t)
    except ValueError:
        return None
    if v < 0 or v != v or v == float("inf"):
        return None
    return int(round(v * mult))


_ADMIN_SUB = {"add": "add", "+": "add", "give": "add", "начислить": "add", "дать": "add",
              "take": "take", "-": "take", "снять": "take", "забрать": "take",
              "set": "set", "=": "set", "установить": "set",
              "item": "item", "предмет": "item", "find": "find", "поиск": "find",
              "info": "info", "инфо": "info", "log": "log", "лог": "log", "help": "help", "?": "help"}


def _admin_help() -> str:
    return ("🛡 <b>Админ-панель кейс-клуба</b>\n"
            "Цель — ответом на сообщение игрока, <code>@username</code> или числовой ID; без цели — ты сам.\n\n"
            "<code>/cadmin add 5000</code> — начислить монеты (можно 5k, 2.5k, 1m)\n"
            "<code>/cadmin take 500 @user</code> — снять (не ниже 0)\n"
            "<code>/cadmin set 10000</code> — установить баланс\n"
            "<code>/cadmin item howl</code> — выдать предмет (ключ или часть названия; <code>/cadmin item howl 3</code> — три штуки)\n"
            "<code>/cadmin find birkin</code> — найти предмет в каталоге\n"
            "<code>/cadmin info @user</code> — сведения об игроке\n"
            "<code>/cadmin log</code> — последние админ-действия")


def _admin_target(message, tokens: list[str]) -> tuple[int | None, str, str]:
    rt = getattr(message, "reply_to_message", None)
    if rt is not None and rt.from_user is not None and not getattr(rt.from_user, "is_bot", False):
        return rt.from_user.id, _name_of(rt.from_user), ""
    for tok in tokens:
        if tok.startswith("@") and len(tok) > 1:
            tid = find_user_by_username(tok)
            if tid is None:
                return None, "", f"{tok} не найден в базе — пусть игрок откроет /case или ответь командой на его сообщение"
            return tid, user_name(tid) or tok, ""
    for tok in tokens:
        if tok.isdigit() and len(tok) >= 5:
            return int(tok), user_name(int(tok)) or tok, ""
    me = message.from_user
    return me.id, _name_of(me), ""


async def cmd_cadmin(message: types.Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("⛔ Эта команда только для администратора.")
        return
    _touch(message.from_user, message.chat.id)
    args = (message.text or "").split()[1:]
    sub = _ADMIN_SUB.get(args[0].lower(), "?") if args else "help"
    rest = args[1:]

    async def say(t: str):
        await message.answer(t, parse_mode="HTML", disable_web_page_preview=True)

    if sub in ("help", "?"):
        return await say(_admin_help())
    if sub == "log":
        rows = admin_log_tail(10)
        if not rows:
            return await say("🛡 Журнал пуст.")
        lines = ["🛡 <b>Последние действия админов</b>"]
        for r in rows:
            when = datetime.fromtimestamp(r["ts"], _TZ).strftime("%d.%m %H:%M")
            lines.append(f"{when} · {r['admin_id']} → {r['target_id']} · {r['action']} {_e(r['detail'])}")
        return await say("\n".join(lines))
    if sub == "find":
        hits = admin_search_items(" ".join(rest))
        if not hits:
            return await say("Ничего не найдено. Пример: <code>/cadmin find howl</code>")
        return await say("🔎 <b>Каталог</b>\n" + "\n".join(
            f"{_it(k)} — {_fmt(ITEMS[k]['price'])} 🪙 · <code>{k}</code>" for k in hits))

    if sub in ("add", "take", "set"):
        amount = _parse_amount(rest[0]) if rest else None
        if amount is None or amount > ADMIN_MAX:
            return await say("Укажи сумму: <code>/cadmin add 5000</code> (можно 5k, 2.5k, 1m)")
        tid, tname, err = _admin_target(message, rest[1:])
        if err:
            return await say(f"⚠️ {_e(err)}")
        r = admin_adjust(uid, tid, sub, amount, tname)
        if not r["ok"]:
            return await say("⚠️ Не получилось применить.")
        who = _mention(tid, user_name(tid) or tname)
        head = {"add": f"<b>+{_fmt(amount)}</b> 🪙", "take": f"<b>−{_fmt(abs(r['delta']))}</b> 🪙",
                "set": f"баланс = <b>{_fmt(amount)}</b> 🪙"}[sub]
        clamp = " (снято сколько было — ниже 0 не опускается)" if sub == "take" and abs(r["delta"]) < amount else ""
        return await say(f"🛡 <b>Админ</b>: {head} → {who}{clamp}\nБаланс: {_fmt(r['old'])} → <b>{_fmt(r['new'])}</b> 🪙")

    if sub == "item":
        if not rest:
            return await say("Укажи предмет: <code>/cadmin item howl</code> (поиск: <code>/cadmin find howl</code>)")
        hits = admin_search_items(rest[0])
        if len(hits) != 1:
            if not hits:
                return await say("Такого предмета нет. Поиск: <code>/cadmin find часть_названия</code>")
            return await say("Нашлось несколько — уточни ключ:\n" + "\n".join(
                f"{_it(k)} — {_fmt(ITEMS[k]['price'])} 🪙 · <code>{k}</code>" for k in hits))
        count, tail = 1, rest[1:]
        if tail and tail[0].isdigit() and len(tail[0]) <= 2:
            count, tail = int(tail[0]), tail[1:]
        tid, tname, err = _admin_target(message, tail)
        if err:
            return await say(f"⚠️ {_e(err)}")
        r = admin_grant_item(uid, tid, hits[0], count, tname)
        if not r["ok"]:
            return await say("⚠️ Количество должно быть от 1 до 50.")
        return await say(f"🛡 <b>Админ</b>: выдано {_it(hits[0])} ×{count} → {_mention(tid, user_name(tid) or tname)}")

    if sub == "info":
        tid, tname, err = _admin_target(message, rest)
        if err:
            return await say(f"⚠️ {_e(err)}")
        if not user_exists(tid):
            return await say("Игрок ещё не играл.")
        p = get_profile(tid)
        return await say(
            f"🛡 <b>Игрок</b> {_mention(tid, p['name'])}{_tags(tid, p['title'])}\n"
            f"ID: <code>{tid}</code>{' · @' + _e(p['username']) if p.get('username') else ''}\n"
            f"🪙 {_fmt(p['coins'])} · 🎒 {p['inv_count']} шт. на {_fmt(p['inv_value'])} · 📈 капитал {_fmt(p['networth'])}\n"
            f"📦 открыто {p['opened']} · 💸 потрачено {_fmt(p['spent'])} · ⚔️ {p['bw']}:{p['bl']} · "
            f"🏅 {len(get_achievements(tid))}/{len(ACH)}")
    await say(_admin_help())


# ══════════════════════════════════════════════════════════════════════════════
#  v5 UI: меню, квесты, мини-спин, билеты, рынок, альбом, мини-игры, сезонный пропуск
# ══════════════════════════════════════════════════════════════════════════════
def _bar(cur: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▰" * width
    f = max(0, min(width, int(width * cur / total)))
    return "▰" * f + "▱" * (width - f)


def _prize_label(kind: str, val: int) -> str:
    if kind == "ticket":
        return "🎫 билет на «Стартовый» кейс"
    if kind == "xp":
        return f"⭐ {val} XP"
    emoji = "🎰" if val >= 10000 else "💎" if val >= 2000 else "💰" if val >= 250 else "🪙"
    return f"{emoji} {_fmt(val)} монет"


def _reward_txt(coins: int, xp: int = 0, tickets: int = 0) -> str:
    return f"+{_fmt(coins)}🪙" + (f" +{xp}XP" if xp else "") + (f" +{tickets}🎫" if tickets else "")


# ── меню и профиль ────────────────────────────────────────────────────────────
def menu_screen(uid: int, name: str, notice: str = "") -> tuple[str, Kb]:
    p = get_profile(uid, name)
    notes = [notice] if notice else []
    if p["refunded"]:
        notes.append(f"↩️ Батл никто не принял — ставка <b>{_fmt(p['refunded'])}</b> 🪙 возвращена")
    b = maybe_bailout(uid)
    if b["given"]:
        notes.append(f"🆘 Ты был на нуле — держи пособие <b>+{BAILOUT}</b> 🪙")
        p = get_profile(uid, name)
    elif b.get("broke"):
        notes.append(f"🆘 Ты на нуле. Пособие через <b>{_human(b['wait'])}</b> — или забери бонус дня")
    pn = pop_notices(uid)
    if pn:
        notes.append(pn)
    lb = leaderboard(None)
    rank = next((d["rank"] for d in lb if d["user_id"] == uid), len(lb))
    ds, fs = daily_status(uid), free_status(uid)
    se = season_info()
    sg = season_growth(uid)
    sp, qs, ps = spin_status(uid), quest_summary(uid), pass_state(uid)
    lvl, into, need = level_progress(p["xp"])
    text = (
        "🎰 <b>КЕЙС-КЛУБ</b>\n"
        f"👤 {_e(name)}{_tags(uid, p['title'])}\n\n"
        f"🪙 Баланс: <b>{_fmt(p['coins'])}</b>" + (f" · 🎫 билетов: <b>{p['tickets']}</b>" if p["tickets"] else "") + "\n"
        f"🎒 Инвентарь: {p['inv_count']} шт. на {_fmt(p['inv_value'])} 🪙" + (f" · 📌 лотов: {p['lots']}" if p["lots"] else "") + "\n"
        f"📈 Капитал: <b>{_fmt(p['networth'])}</b> 🪙 · место <b>#{rank}</b> из {len(lb)}\n"
        f"⭐ Уровень <b>{lvl}</b> · " + (f"{into}/{need} XP {_bar(into, need)}" if need else "максимум") + "\n"
        f"📅 Сезон {se['no']}: ещё {_human_long(se['left'])} · прирост <b>{'+' if sg >= 0 else '−'}{_fmt(abs(sg))}</b>\n\n"
        + ("🎁 <b>Бонус дня готов!</b>" if ds["ready"] else f"🎁 Бонус дня через {_human(ds['wait'])}")
        + (f" (серия {ds['streak']} дн.)" if ds["streak"] else "")
        + "\n" + ("🎟 <b>Бесплатный кейс готов!</b>" if fs["ready"] else f"🎟 Бесплатный кейс через {_human(fs['wait'])}")
        + "\n" + (f"🎡 <b>Мини-спин готов</b> (зарядов: {sp['charges']})" if sp["charges"] else f"🎡 Мини-спин через {_human(sp['next_in'])}")
        + f"\n📋 Квесты: дня {qs['done_d']}/3 · недели {qs['done_w']}/3" + (f" · <b>🎁 наград: {qs['ready']}</b>" if qs["ready"] else "")
        + ("\n\n" + "\n".join(notes) if notes else "")
    )
    rows = [
        [("📦 Кейсы", f"cs:cases:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")],
        [("🎟 Бесплатный ✅" if fs["ready"] else "🎟 Бесплатный ⏳", f"cs:free:{uid}"),
         ("🎁 Бонус дня ✅" if ds["ready"] else "🎁 Бонус дня ⏳", f"cs:daily:{uid}")],
        [(f"🎡 Спин ✅×{sp['charges']}" if sp["charges"] else "🎡 Спин ⏳", f"cs:sp:{uid}"),
         (f"📋 Квесты{' 🎁' + str(qs['ready']) if qs['ready'] else ''}", f"cs:qs:{uid}")],
        [("🚀 Апгрейд", f"cs:up:{uid}:0"), ("🧾 Контракт", f"cs:ct:{uid}")],
        [("🏪 Рынок", f"cs:mk:{uid}:new:0"), ("📚 Альбом", f"cs:al:{uid}:0")],
        [("🎮 Игры", f"cs:gm:{uid}"), (f"⭐ Пропуск {ps['tier']}/{PASS_TIERS}{' 🎁' if ps['ready'] else ''}", f"cs:ps:{uid}")],
        [("⚔️ Батл", f"cs:battle:{uid}"), ("🏆 Топ", f"cs:top:{uid}:all")],
        [("🏅 Достижения", f"cs:ach:{uid}"), ("📊 Профиль", f"cs:prof:{uid}")],
    ]
    if p["tickets"] > 0:
        rows.insert(2, [(f"🎫 Открыть по билету ({p['tickets']})", f"cs:tk:{uid}")])
    return text, _kb(rows)


def profile_screen(uid: int, name: str) -> tuple[str, Kb]:
    p = get_profile(uid, name)
    best = (f"{_it(p['best_item'])} — {_fmt(p['best_price'])} 🪙" if p["best_item"] else "—")
    have = get_achievements(uid)
    lvl, into, need = level_progress(p["xp"])
    text = (
        f"📊 <b>ПРОФИЛЬ</b> · {_e(name)}{_tags(uid, p['title'])}\n\n"
        f"⭐ Уровень <b>{lvl}</b> · " + (f"{into}/{need} XP {_bar(into, need)}" if need else "максимум") + "\n"
        f"🎮 Лимит ставки в мини-играх: {_fmt(max_bet(lvl))} 🪙 (растёт с уровнем)\n"
        f"🪙 Баланс: <b>{_fmt(p['coins'])}</b> · 🎫 билетов: {p['tickets']}\n"
        f"📈 Капитал: <b>{_fmt(p['networth'])}</b> 🪙\n"
        f"📦 Открыто кейсов: {p['opened']}\n"
        f"💸 Потрачено: {_fmt(p['spent'])} · 💰 Продано на: {_fmt(p['earned'])}\n"
        f"🏅 Лучший дроп: {best}\n"
        f"⚔️ Батлы: {p['bw']} побед / {p['bl']} поражений (лучшая серия {p['bstreak_best']})\n"
        f"🚀 Апгрейды: {p['up_win']} из {p['up_try']} успешных · 🧾 контрактов: {p['contracts']}\n"
        f"🤝 Обменов: {p['trades']} · 🎁 подарков: {p['gifts']}\n"
        f"🏪 Рынок: продано {p['market_sales']}, куплено {p['market_buys']}\n"
        f"🎲 Мини-игры: {p['game_plays']} раундов, {p['game_wins']} побед · 📋 заданий: {p['quests_done']}\n"
        f"🏅 Достижения: {len(have)}/{len(ACH)} · 🔥 серия бонуса: {p['streak']} дн.\n\n"
        f"<i>Бонус дня {DAILY_BASE}–{DAILY_BASE + DAILY_STEP * DAILY_STREAK_MAX} 🪙, "
        f"пособие {BAILOUT} 🪙 если на нуле (раз в {BAILOUT_COOLDOWN // 3600} ч)</i>"
    )
    return text, _kb([[("🏅 Достижения", f"cs:ach:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")]])


# ── квесты ────────────────────────────────────────────────────────────────────
def quests_screen(uid: int, notice: str = "") -> tuple[str, Kb]:
    st = quest_state(uid)
    lines = [notice + "\n" if notice else "", "📋 <b>КВЕСТЫ</b>"]
    btns = []
    for period, title, left in (("d", "Сегодня", st["d_left"]), ("w", "Неделя", st["w_left"])):
        lines.append(f"\n<b>{title}</b> · осталось {_human_long(left)}")
        for q in st[period]:
            done = q["progress"] >= q["target"]
            mark = "✅" if q["claimed"] else "🎁" if done else "▫️"
            txt = quest_text(q["qid"], q["target"])
            rew = _reward_txt(q["coins"], q["xp"], q["tickets"])
            lines.append(f"{mark} {'🎯 <b>' + txt + '</b>' if q['idx'] == 3 else txt} — {q['progress']}/{q['target']} · {rew}")
            if done and not q["claimed"]:
                btns.append((f"🎁 {txt[:30]}", f"cs:qc:{uid}:{period}:{q['idx']}"))
    rows = [[b] for b in btns[:6]]
    if len(btns) > 1:
        rows.insert(0, [(f"🎁 Забрать всё ({len(btns)})", f"cs:qca:{uid}")])
    rows.append([("🔄 Обновить", f"cs:qs:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines).strip("\n"), _kb(rows)


async def _do_quest_claim(cb, uid: int, period: str, idx: int):
    r = quest_claim(uid, period, idx)
    if not r["ok"]:
        await cb.answer({"incomplete": "Задание ещё не выполнено", "claimed": "Награда уже получена"}.get(r["reason"], "Задание устарело"),
                        show_alert=True)
        t, k = quests_screen(uid)
        await _edit(cb.message, t, k)
        return
    await cb.answer("Награда получена!")
    t, k = quests_screen(uid, f"🎁 Получено: {_reward_txt(r['coins'], r['xp'], r['tickets'])}")
    await _edit(cb.message, _with_notices(uid, t), k)


async def _do_quest_claim_all(cb, uid: int):
    r = quest_claim_all(uid)
    if not r["ok"]:
        await cb.answer("Пока нечего забирать", show_alert=True)
        return
    await cb.answer(f"+{r['coins']} 🪙")
    t, k = quests_screen(uid, f"🎁 Получено за {r['n']} зад.: {_reward_txt(r['coins'], r['xp'], r['tickets'])}")
    await _edit(cb.message, _with_notices(uid, t), k)


# ── мини-спин и билеты ────────────────────────────────────────────────────────
def spin_screen(uid: int, notice: str = "") -> tuple[str, Kb]:
    s = spin_status(uid)
    lines = [notice + "\n" if notice else "", "🎡 <b>МИНИ-СПИН</b>",
             f"Заряды: <b>{s['charges']}</b>/{SPIN_MAX} · новый заряд каждые {SPIN_COOLDOWN // 3600} ч"
             + ("" if s["charges"] >= SPIN_MAX else f" (следующий через {_human(s['next_in'])})"), "", "🎁 Призы:"]
    for kind, val, w in SPIN_PRIZES:
        lines.append(f"{_prize_label(kind, val)} — {w:g}%")
    lines.append(f"\n<i>В среднем ≈ {SPIN_EV:.0f} 🪙 за спин. Заряды копятся (до {SPIN_MAX}).</i>")
    btn = ("🎡 Крутить!", f"cs:spgo:{uid}") if s["charges"] else (f"⏳ Через {_human(s['next_in'])}", f"cs:sp:{uid}")
    return "\n".join(x for x in lines if x != ""), _kb([[btn], [("📋 Квесты", f"cs:qs:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")]])


async def _anim_spin(msg, kind: str, val: int):
    pool = ["🪙", "💰", "🎫", "⭐", "💎", "🪙", "🎰", "🪙"]
    for k in range(7):
        row = " ".join(_RNG.choice(pool) for _ in range(5))
        await _frame(msg, f"🎡 <b>МИНИ-СПИН</b>\n\n{row}\n          🔼\n\n<i>{'◐◓◑◒'[k % 4]} крутится…</i>")
        await asyncio.sleep(0.5 + 0.1 * k)


async def _do_spin(cb, uid: int, name: str):
    if uid in _BUSY:
        await cb.answer("Погоди, ещё идёт действие 🎰")
        return
    get_profile(uid, name)
    r = do_spin(uid)
    if not r["ok"]:
        await cb.answer(f"Заряда пока нет. Следующий через {_human(r['next_in'])}", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        try:
            await asyncio.wait_for(_anim_spin(cb.message, r["kind"], r["value"]), ANIM_TIMEOUT)
        except Exception:
            logger.exception("анимация спина упала")
        big = r["kind"] == "coins" and r["value"] >= 2000
        t, k = spin_screen(uid, f"{'🎉🎉 <b>ДЖЕКПОТ!</b> 🎉🎉' if big else '🎉 Выпало:'} {_prize_label(r['kind'], r['value'])}")
        await _edit(cb.message, _with_notices(uid, t), k)
    finally:
        _BUSY.discard(uid)


def ticket_drop_screen(uid: int, item: str, inv_id: int, left: int) -> tuple[str, Kb]:
    i = item_info(item)
    text = (f"🎫 <b>Билет → «{CASES[TICKET_CASE]['name']}»</b>\n\n{_BANNER[i['rarity']]}\n\n{_it(item)}\n"
            f"💰 {_fmt(i['price'])} 🪙 · билетов осталось: {left}")
    rows = [[(f"💰 Продать за {_fmt(i['price'])}", f"cs:sell:{uid}:{inv_id}:0:-1")]]
    if left:
        rows.append([(f"🎫 Ещё по билету ({left})", f"cs:tk:{uid}")])
    rows += [[("📦 Кейсы", f"cs:cases:{uid}"), ("🎒 Инвентарь", f"cs:inv:{uid}:0")], [("🏠 Меню", f"cs:menu:{uid}")]]
    return _with_notices(uid, text), _kb(rows)


async def _do_ticket(cb, uid: int, name: str):
    if uid in _BUSY:
        await cb.answer("Погоди, кейс ещё открывается 🎰")
        return
    get_profile(uid, name)
    r = open_ticket(uid)
    if not r["ok"]:
        await cb.answer("Билетов нет. Их дают за квесты, уровни, спин и пропуск", show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        try:
            await asyncio.wait_for(_anim_open(cb.message, TICKET_CASE, r["item"]), ANIM_TIMEOUT)
        except Exception:
            logger.exception("анимация билета упала")
        t, k = ticket_drop_screen(uid, r["item"], r["inv_id"], r["left"])
        await _edit(cb.message, t, k)
    finally:
        _BUSY.discard(uid)


# ── рынок игроков ─────────────────────────────────────────────────────────────
def market_screen(uid: int, sort: str = "new", page: int = 0, notice: str = "") -> tuple[str, Kb]:
    if sort not in MARKET_SORTS:
        sort = "new"
    b = market_browse(sort, page, viewer=uid)
    lines = [notice + "\n" if notice else "",
             f"🏪 <b>РЫНОК</b> · {MARKET_SORTS[sort][0]} · лотов: {b['total']} · стр. {b['page'] + 1}/{b['pages']}"]
    buy = []
    for n, lot in enumerate(b["lots"], 1):
        base = max(1, item_info(lot["item"])["price"])
        lines.append(f"{n}. {_it(lot['item'])} — <b>{_fmt(lot['price'])}</b> 🪙 <i>(×{lot['price'] / base:.2f} к справочной)</i> · {_e(_cut_name(lot['seller_name'] or '?', 14))}")
        buy.append((f"🛒 {n}", f"cs:mkb:{uid}:{lot['id']}:0"))
    if not b["lots"]:
        lines.append("Пока пусто. Выстави что-нибудь первым 👇")
    lines.append(f"\n<i>Комиссия {round(MARKET_FEE * 100)}% с продавца · лот живёт {MARKET_TTL // 86400} дня · цена от ×{MARKET_PRICE_RANGE[0]:g} до ×{MARKET_PRICE_RANGE[1]:g} справочной</i>")
    rows = [[(("✔️ " if k == sort else "") + t, f"cs:mk:{uid}:{k}:0") for k, (t, _) in MARKET_SORTS.items()][:4]]
    if buy:
        rows.append(buy)
    nav = []
    if b["page"] > 0:
        nav.append(("◀️", f"cs:mk:{uid}:{sort}:{b['page'] - 1}"))
    nav.append((f"{b['page'] + 1}/{b['pages']}", f"cs:mk:{uid}:{sort}:{b['page']}"))
    if b["page"] < b["pages"] - 1:
        nav.append(("▶️", f"cs:mk:{uid}:{sort}:{b['page'] + 1}"))
    rows.append(nav)
    rows.append([("📦 Выставить предмет", f"cs:mkl:{uid}:0"), ("🗂 Мои лоты", f"cs:mkm:{uid}")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines).strip("\n"), _kb(rows)


def market_pick_screen(uid: int, page: int = 0) -> tuple[str, Kb]:
    return _pick_screen(uid, page, "🏪 <b>Выставить на рынок</b>: выбери предмет", lambda i, pg: f"cs:mki:{uid}:{i}",
                        lambda pg: f"cs:mkl:{uid}:{pg}", f"cs:mk:{uid}:new:0",
                        footer="<i>Пока предмет на рынке, он «заморожен», но учитывается в капитале.</i>",
                        empty="🎒 Нечего выставлять")


def market_price_screen(uid: int, inv_id: int) -> tuple[str, Kb]:
    item = get_inv_item(uid, inv_id)
    if item is None:
        return "Этого предмета уже нет в инвентаре.", _kb([[("🏪 Рынок", f"cs:mk:{uid}:new:0")]])
    lo, hi = market_price_bounds(item)
    base = item_info(item)["price"]
    rows, lines = [], [f"🏪 <b>Цена лота</b>\n{_it(item)} · справочная {_fmt(base)} 🪙\n",
                       f"Допустимо: {_fmt(lo)} — {_fmt(hi)} 🪙. Тебе придёт цена минус {round(MARKET_FEE * 100)}%."]
    for i, m in enumerate(MARKET_PRESETS):
        price = max(lo, min(hi, round(base * m)))
        lines.append(f"×{m:g} → <b>{_fmt(price)}</b> 🪙 (получишь {_fmt(price - int(price * MARKET_FEE))})")
        rows.append([(f"×{m:g} · {_fmt(price)} 🪙", f"cs:mkp:{uid}:{inv_id}:{i}")])
    rows.append([("⬅️ Другой предмет", f"cs:mkl:{uid}:0")])
    return "\n".join(lines), _kb(rows)


def market_mine_screen(uid: int, notice: str = "") -> tuple[str, Kb]:
    m = market_mine(uid)
    lines = [notice + "\n" if notice else "", f"🗂 <b>МОИ ЛОТЫ</b> · {len(m['open'])}/{MARKET_MAX_LOTS}"]
    rows = []
    for n, lot in enumerate(m["open"], 1):
        left = max(0, int(lot["created"] + MARKET_TTL - time.time()))
        lines.append(f"{n}. {_it(lot['item'])} — {_fmt(lot['price'])} 🪙 · ещё {_human_long(left)}")
        rows.append([(f"❌ Снять №{n}", f"cs:mkx:{uid}:{lot['id']}")])
    if not m["open"]:
        lines.append("Активных лотов нет.")
    if m["sold"]:
        lines.append("\n💰 Недавно продано:")
        for lot in m["sold"]:
            lines.append(f"• {_it(lot['item'])} — {_fmt(lot['price'])} 🪙")
    rows.append([("📦 Выставить ещё", f"cs:mkl:{uid}:0"), ("🏪 Рынок", f"cs:mk:{uid}:new:0")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(x for x in lines if x != ""), _kb(rows)


_MK_ERR = {"closed": "Этот лот уже продан или снят", "own": "Свой лот купить нельзя 😄", "gone": "Предмета уже нет в инвентаре",
           "trophy": "Кубки и награды пропуска продавать нельзя", "limit": f"Не больше {MARKET_MAX_LOTS} лотов одновременно",
           "cap": "Лимит передачи ценности на сутки исчерпан — цена лота слишком сильно отличается от справочной"}


async def _do_market_buy(cb, uid: int, name: str, lot_id: int, ok: int):
    lot = market_get(lot_id)
    if lot is None or lot["status"] != "open":
        await cb.answer(_MK_ERR["closed"], show_alert=True)
        t, k = market_screen(uid)
        await _edit(cb.message, t, k)
        return
    if lot["seller"] == uid:
        await cb.answer(_MK_ERR["own"], show_alert=True)
        return
    if not ok:
        await cb.answer()
        base = item_info(lot["item"])["price"]
        await _edit(cb.message, f"🛒 Купить {_it(lot['item'])} за <b>{_fmt(lot['price'])}</b> 🪙?\n"
                                f"Справочная цена {_fmt(base)} 🪙 (×{lot['price'] / max(1, base):.2f}). Продавец: {_e(lot['seller_name'] or '?')}",
                    _kb([[("✅ Купить", f"cs:mkb:{uid}:{lot_id}:1"), ("❌ Отмена", f"cs:mk:{uid}:new:0")]]))
        return
    r = market_buy(uid, lot_id, name)
    if not r["ok"]:
        await cb.answer(_MK_ERR.get(r["reason"]) or f"Не хватает {_fmt(r.get('need', 0))} 🪙", show_alert=True)
        return
    await cb.answer("Куплено!")
    t, k = market_screen(uid, "new", 0, f"✅ Куплено: {_it(r['item'])} за <b>{_fmt(r['price'])}</b> 🪙")
    await _edit(cb.message, _with_notices(uid, t), k)


async def _do_market_list(cb, uid: int, inv_id: int, idx: int):
    item = get_inv_item(uid, inv_id)
    if item is None or not (0 <= idx < len(MARKET_PRESETS)):
        await cb.answer(_MK_ERR["gone"], show_alert=True)
        return
    lo, hi = market_price_bounds(item)
    price = max(lo, min(hi, round(item_info(item)["price"] * MARKET_PRESETS[idx])))
    r = market_list(uid, inv_id, price)
    if not r["ok"]:
        await cb.answer(_MK_ERR.get(r["reason"], "Не получилось"), show_alert=True)
        return
    await cb.answer("Лот выставлен")
    t, k = market_mine_screen(uid, f"✅ Выставлено: {_it(item)} за <b>{_fmt(price)}</b> 🪙")
    await _edit(cb.message, _with_notices(uid, t), k)


async def _do_market_cancel(cb, uid: int, lot_id: int):
    r = market_cancel(uid, lot_id)
    if not r["ok"]:
        await cb.answer(_MK_ERR["closed"], show_alert=True)
    else:
        await cb.answer("Лот снят — предмет вернулся в инвентарь")
    t, k = market_mine_screen(uid)
    await _edit(cb.message, t, k)


# ── альбом ────────────────────────────────────────────────────────────────────
ALBUM_PER_PAGE = 8


def album_screen(uid: int, page: int = 0, notice: str = "") -> tuple[str, Kb]:
    st = album_state(uid)
    cols = st["cols"]
    pages = max(1, -(-len(cols) // ALBUM_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lines = [notice + "\n" if notice else "", "📚 <b>АЛЬБОМ</b>",
             f"Открыто предметов: <b>{st['owned']}/{st['total']}</b> {_bar(st['owned'], st['total'])}",
             "<i>предмет засчитывается, как только он хоть раз побывал у тебя — продавать дубли и экспонаты можно</i>", ""]
    ms_btns = []
    for pct, coins, tk in ALBUM_MILESTONES:
        need = -(-st["total"] * pct // 100)
        key = f"all{pct}"
        if key in st["claimed"]:
            lines.append(f"✅ {pct}% каталога — получено")
        elif st["owned"] >= need:
            lines.append(f"🎁 {pct}% каталога — забери {_reward_txt(coins, 0, tk)}")
            ms_btns.append((f"🎁 {pct}% → +{_fmt(coins)}🪙", f"cs:alm:{uid}:{pct}"))
        else:
            lines.append(f"🔒 {pct}% каталога ({st['owned']}/{need}) · {_reward_txt(coins, 0, tk)}")
    lines.append("\n<b>Наборы:</b>")
    btns = []
    for col in cols[page * ALBUM_PER_PAGE:(page + 1) * ALBUM_PER_PAGE]:
        mark = "✅" if col["claimed"] else "🎁" if col["complete"] else "▫️"
        lines.append(f"{mark} {col['icon']} {col['name']} — {col['have']}/{col['total']}")
        btns.append((f"{mark} {col['icon']} {col['name']} {col['have']}/{col['total']}"[:40], f"cs:als:{uid}:{col['key']}"))
    rows = [[b] for b in ms_btns] + [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = []
    if page > 0:
        nav.append(("◀️", f"cs:al:{uid}:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", f"cs:al:{uid}:{page}"))
    if page < pages - 1:
        nav.append(("▶️", f"cs:al:{uid}:{page + 1}"))
    rows.append(nav)
    rows.append([("🏪 Рынок", f"cs:mk:{uid}:new:0"), ("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(lines).strip("\n"), _kb(rows)


def album_set_screen(uid: int, col_key: str, notice: str = "") -> tuple[str, Kb]:
    col = next((c for c in album_state(uid)["cols"] if c["key"] == col_key), None)
    if col is None:
        return "Такого набора нет.", _kb([[("📚 Альбом", f"cs:al:{uid}:0")]])
    have = album_items(uid)
    lines = [notice + "\n" if notice else "", f"{col['icon']} <b>{col['name']}</b> — {col['have']}/{col['total']} {_bar(col['have'], col['total'])}",
             f"Награда: {_reward_txt(col['coins'], col['xp'], col['tickets'])}", ""]
    for i in sorted(col["items"], key=lambda k: item_info(k)["price"]):
        lines.append(f"{'✅' if i in have else '⬜'} {_it(i)} — {_fmt(item_info(i)['price'])} 🪙")
    rows = []
    if col["claimed"]:
        lines.append("\n✅ Награда за набор получена")
    elif col["complete"]:
        rows.append([("🎁 Забрать награду", f"cs:alc:{uid}:{col_key}")])
    else:
        lines.append(f"\n<i>Не хватает: {col['total'] - col['have']}. Ищи на рынке, в обмене и в кейсах.</i>")
    rows.append([("🏪 Рынок", f"cs:mk:{uid}:new:0"), ("📚 Альбом", f"cs:al:{uid}:0")])
    return "\n".join(lines).strip("\n"), _kb(rows)


async def _do_album_claim(cb, uid: int, col_key: str):
    r = album_claim(uid, col_key)
    if not r["ok"]:
        await cb.answer({"incomplete": "Набор ещё не собран", "claimed": "Награда уже получена"}.get(r["reason"], "Не получилось"), show_alert=True)
        t, k = album_set_screen(uid, col_key)
        await _edit(cb.message, t, k)
        return
    await cb.answer("Набор собран!")
    t, k = album_set_screen(uid, col_key, f"🎉 Набор собран! {_reward_txt(r['coins'], r['xp'], r['tickets'])}")
    await _edit(cb.message, _with_notices(uid, t), k)


async def _do_album_milestone(cb, uid: int, pct: int):
    r = album_milestone_claim(uid, pct)
    if not r["ok"]:
        await cb.answer({"incomplete": "Ещё не набрано", "claimed": "Награда уже получена"}.get(r["reason"], "Не получилось"), show_alert=True)
        return
    await cb.answer(f"+{_fmt(r['coins'])} 🪙")
    t, k = album_screen(uid, 0, f"🎉 {pct}% каталога! {_reward_txt(r['coins'], 0, r['tickets'])}")
    await _edit(cb.message, _with_notices(uid, t), k)


# ── мини-игры ─────────────────────────────────────────────────────────────────
GAMES = {"coin": ("🪙", "Орёл или решка"), "dice": ("🎲", "Кубик"), "mines": ("💣", "Мины"), "crash": ("🚀", "Краш")}
GAME_HELP = {
    "coin": f"Угадай сторону монеты — выплата <b>×{COIN_MULT}</b>.",
    "dice": f"Бросаем две кости. «Меньше 7» и «Больше 7» — <b>×{DICE_MULT['u']}</b>, «Ровно 7» — <b>×{DICE_MULT['s']}</b>.",
    "mines": f"Поле {MINES_N} клеток, часть — мины. Каждая безопасная клетка растит множитель; забери выигрыш до взрыва. Больше мин — быстрее рост.",
    "crash": "Ракета набирает множитель. Успей нажать «Забрать» до краша — иначе ставка сгорит. Краш может случиться сразу.",
}
CRASH_TICK = 1.4
_CRASH_TASKS: set = set()
_DICE_FACES = "⚀⚁⚂⚃⚄⚅"


def _game_err(r: dict) -> str:
    return {"min": f"Минимальная ставка — {GAME_MIN_BET} 🪙",
            "max": f"Максимум на твоём уровне — {_fmt(r.get('max', 0))} 🪙 (лимит растёт с уровнем)",
            "money": f"Не хватает {_fmt(r.get('need', 0))} 🪙", "active": "У тебя уже идёт партия — сначала закончи её",
            "bad": "Неверные параметры"}.get(r.get("reason"), "Не получилось")


def games_hub_screen(uid: int, notice: str = "") -> tuple[str, Kb]:
    p = get_profile(uid)
    lvl = level_of(p["xp"])
    act = game_active(uid)
    lines = [notice + "\n" if notice else "", "🎮 <b>МИНИ-ИГРЫ</b>",
             f"🪙 Баланс: <b>{_fmt(p['coins'])}</b> · ставка {GAME_MIN_BET}–{_fmt(max_bet(lvl))} 🪙 (лимит растёт с уровнем)",
             f"Возврат ≈ {round(GAME_RTP * 100)}% — игровые монеты, вывода нет. Играй для удовольствия 🙂", ""]
    for k, (ic, nm) in GAMES.items():
        lines.append(f"{ic} <b>{nm}</b> — {GAME_HELP[k]}")
    rows = []
    if act:
        ic, nm = GAMES[act["game"]]
        lines.append(f"\n▶️ <b>Идёт партия:</b> {ic} {nm}, ставка {_fmt(act['bet'])} 🪙")
        rows.append([(f"▶️ Продолжить: {ic} {nm}", f"cs:gmr:{uid}")])
    rows.append([(f"{ic} {nm}", f"cs:gmg:{uid}:{k}") for k, (ic, nm) in list(GAMES.items())[:2]])
    rows.append([(f"{ic} {nm}", f"cs:gmg:{uid}:{k}") for k, (ic, nm) in list(GAMES.items())[2:]])
    rows.append([("📋 Квесты", f"cs:qs:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(x for x in lines if x != ""), _kb(rows)


def game_bet_screen(uid: int, game: str, notice: str = "") -> tuple[str, Kb]:
    p = get_profile(uid)
    lim = min(max_bet(level_of(p["xp"])), p["coins"])
    ic, nm = GAMES[game]
    lines = [notice + "\n" if notice else "", f"{ic} <b>{nm}</b>", GAME_HELP[game],
             f"\n🪙 Баланс: <b>{_fmt(p['coins'])}</b> · выбери ставку:"]
    bets = [b for b in BET_PRESETS if GAME_MIN_BET <= b <= lim]
    if lim >= GAME_MIN_BET and lim not in bets:
        bets.append(lim)
    btns = [((f"Макс {_fmt(b)}" if b == lim and b not in BET_PRESETS else _fmt(b)), f"cs:gmb:{uid}:{game}:{b}") for b in bets]
    if not btns:
        lines.append("\n⚠️ Не хватает монет даже на минимальную ставку.")
    rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    rows.append([("🎮 Игры", f"cs:gm:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(x for x in lines if x != ""), _kb(rows)


def game_options_screen(uid: int, game: str, bet: int) -> tuple[str, Kb]:
    ic, nm = GAMES[game]
    head = f"{ic} <b>{nm}</b> · ставка <b>{_fmt(bet)}</b> 🪙\n"
    if game == "coin":
        rows = [[("🦅 Орёл", f"cs:gmcoin:{uid}:{bet}:h"), ("🌕 Решка", f"cs:gmcoin:{uid}:{bet}:t")]]
        text = head + f"Выплата при угадывании: <b>{_fmt(int(bet * COIN_MULT))}</b> 🪙"
    elif game == "dice":
        rows = [[(f"📉 Меньше 7 ×{DICE_MULT['u']}", f"cs:gmdice:{uid}:{bet}:u")],
                [(f"🎯 Ровно 7 ×{DICE_MULT['s']}", f"cs:gmdice:{uid}:{bet}:s")],
                [(f"📈 Больше 7 ×{DICE_MULT['o']}", f"cs:gmdice:{uid}:{bet}:o")]]
        text = head + "Что выпадет на двух костях?"
    elif game == "mines":
        rows = [[(f"{m} 💣 · первая клетка ×{mines_mult(m, 1):.2f}", f"cs:gmmine:{uid}:{bet}:{m}")] for m in MINES_OPTIONS]
        text = head + f"Сколько мин на поле {MINES_N} клеток? Чем больше мин — тем быстрее растёт множитель."
    else:
        rows = [[("🚀 Старт!", f"cs:gmcrash:{uid}:{bet}")]]
        text = head + "Ракета стартует сразу после нажатия. Нажми «Забрать» вовремя!"
    rows.append([("💱 Другая ставка", f"cs:gmg:{uid}:{game}"), ("🎮 Игры", f"cs:gm:{uid}")])
    return text, _kb(rows)


def game_result_screen(uid: int, game: str, bet: int, body: str, coins: int) -> tuple[str, Kb]:
    ic, nm = GAMES[game]
    text = f"{ic} <b>{nm}</b> · ставка {_fmt(bet)} 🪙\n\n{body}\n\n🪙 Баланс: <b>{_fmt(coins)}</b>"
    p = get_profile(uid)
    rows = []
    if p["coins"] >= bet:
        rows.append([(f"🔁 Ещё раз · {_fmt(bet)}🪙", f"cs:gmb:{uid}:{game}:{bet}")])
    rows.append([("💱 Другая ставка", f"cs:gmg:{uid}:{game}"), ("🎮 Игры", f"cs:gm:{uid}")])
    rows.append([("🏠 Меню", f"cs:menu:{uid}")])
    return _with_notices(uid, text), _kb(rows)


async def _do_coin(cb, uid: int, name: str, bet: int, side: str):
    if uid in _BUSY:
        await cb.answer("Погоди, раунд ещё идёт 🎰")
        return
    get_profile(uid, name)
    r = coin_flip(uid, bet, side)
    if not r["ok"]:
        await cb.answer(_game_err(r), show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        for k in range(3):
            await _frame(cb.message, f"🪙 <b>Орёл или решка</b> · ставка {_fmt(bet)} 🪙\n\n{'🪙🌕🦅'[k % 3]} подбрасываем…")
            await asyncio.sleep(0.6)
        res = "🦅 Орёл" if r["result"] == "h" else "🌕 Решка"
        body = (f"{res} — 🟢 <b>ВЫИГРЫШ!</b> +{_fmt(r['payout'])} 🪙 (×{COIN_MULT})" if r["win"]
                else f"{res} — 🔴 не угадал, ставка проиграна")
        t, k = game_result_screen(uid, "coin", bet, body, r["coins"])
        await _edit(cb.message, t, k)
    finally:
        _BUSY.discard(uid)


async def _do_dice(cb, uid: int, name: str, bet: int, mode: str):
    if uid in _BUSY:
        await cb.answer("Погоди, раунд ещё идёт 🎰")
        return
    get_profile(uid, name)
    r = dice_roll(uid, bet, mode)
    if not r["ok"]:
        await cb.answer(_game_err(r), show_alert=True)
        return
    await cb.answer()
    _BUSY.add(uid)
    try:
        for k in range(3):
            await _frame(cb.message, f"🎲 <b>Кубик</b> · ставка {_fmt(bet)} 🪙\n\n{_RNG.choice(_DICE_FACES)} {_RNG.choice(_DICE_FACES)}  бросаем…")
            await asyncio.sleep(0.6)
        d1, d2 = r["dice"]
        pick = {"u": "меньше 7", "s": "ровно 7", "o": "больше 7"}[mode]
        faces = f"{_DICE_FACES[d1 - 1]} {_DICE_FACES[d2 - 1]} = <b>{r['sum']}</b>"
        body = (f"{faces}\nТы ставил: {pick} — 🟢 <b>ВЫИГРЫШ!</b> +{_fmt(r['payout'])} 🪙 (×{r['mult']})" if r["win"]
                else f"{faces}\nТы ставил: {pick} — 🔴 мимо, ставка проиграна")
        t, k = game_result_screen(uid, "dice", bet, body, r["coins"])
        await _edit(cb.message, t, k)
    finally:
        _BUSY.discard(uid)


def mines_screen(uid: int, notice: str = "", reveal: dict | None = None) -> tuple[str, Kb]:
    """Игровое поле. reveal — состояние завершённой партии (показываем все мины)."""
    if reveal is None:
        g = game_active(uid)
        if g is None or g["game"] != "mines":
            return games_hub_screen(uid, "Активной партии нет.")
        d, bet = g["data"], g["bet"]
    else:
        d, bet = reveal["state"], reveal["bet"]
    k, m = len(d["open"]), d["m"]
    mult = mines_mult(m, k)
    rows = []
    for r in range(4):
        row = []
        for ccol in range(4):
            i = r * 4 + ccol
            if i in d["open"]:
                row.append(("💎", f"cs:gmx:{uid}"))
            elif reveal is not None:
                row.append(("💣" if i in d["mines"] else "▫️", f"cs:gmx:{uid}"))
            else:
                row.append(("⬜", f"cs:gmcell:{uid}:{i}"))
        rows.append(row)
    head = f"💣 <b>МИНЫ</b> · ставка <b>{_fmt(bet)}</b> 🪙 · мин: {m}\n"
    if reveal is None:
        nxt = mines_mult(m, k + 1) if k < MINES_N - m else mult
        text = (head + f"Открыто: <b>{k}</b>/{MINES_N - m} · множитель <b>×{mult:.2f}</b> → {_fmt(int(bet * mult))} 🪙\n"
                f"Следующая клетка: ×{nxt:.2f}" + (f"\n\n{notice}" if notice else ""))
        rows.append([(f"💰 Забрать ×{mult:.2f} ({_fmt(int(bet * mult))}🪙)" if k else "👆 Выбери клетку", f"cs:gmout:{uid}" if k else f"cs:gmx:{uid}")])
    else:
        text = head + (notice or "")
        rows.append([(f"🔁 Ещё раз · {_fmt(bet)}🪙", f"cs:gmb:{uid}:mines:{bet}"), ("🎮 Игры", f"cs:gm:{uid}")])
    return text, _kb(rows)


async def _do_mines_start(cb, uid: int, name: str, bet: int, m: int):
    get_profile(uid, name)
    r = mines_start(uid, bet, m)
    if not r["ok"]:
        await cb.answer(_game_err(r), show_alert=True)
        return
    await cb.answer("Партия началась")
    t, k = mines_screen(uid)
    await _edit(cb.message, t, k)


async def _do_mines_cell(cb, uid: int, cell: int):
    r = mines_pick(uid, cell)
    if not r["ok"]:
        await cb.answer("Эта клетка уже открыта" if r["reason"] == "bad" else "Активной партии нет", show_alert=True)
        t, k = mines_screen(uid)
        await _edit(cb.message, t, k)
        return
    await cb.answer()
    if r["boom"]:
        t, k = mines_screen(uid, f"💥 <b>БАХ!</b> Мина! Ставка {_fmt(r['bet'])} 🪙 проиграна.", reveal={"state": r["state"], "bet": r["bet"]})
        await _edit(cb.message, _with_notices(uid, t), k)
    elif r["done"]:
        t, k = mines_screen(uid, f"🏆 <b>Все безопасные клетки открыты!</b> ×{r['mult']:.2f} → +{_fmt(r['payout'])} 🪙",
                            reveal={"state": r["state"], "bet": r["bet"]})
        await _edit(cb.message, _with_notices(uid, t), k)
    else:
        t, k = mines_screen(uid)
        await _edit(cb.message, t, k)


def crash_play_screen(uid: int, bet: int, mult: float) -> tuple[str, Kb]:
    text = (f"🚀 <b>КРАШ</b> · ставка <b>{_fmt(bet)}</b> 🪙\n\n"
            f"Множитель: <b>×{mult:.2f}</b> → {_fmt(int(bet * mult))} 🪙\n\n"
            "<i>Жми «Забрать», пока ракета не разбилась! Выплата считается по моменту нажатия.</i>")
    return text, _kb([[(f"💰 Забрать ×{mult:.2f}", f"cs:gmout:{uid}")]])


async def _crash_loop(msg, uid: int):
    """Обновляет множитель, пока раунд жив; при краше фиксирует проигрыш. Кадры — по возможности (флуд-защита)."""
    try:
        while True:
            await asyncio.sleep(CRASH_TICK)
            st = crash_status(uid)
            if st is None:
                return
            if st["busted"]:
                r = crash_settle_bust(uid)
                if r:
                    t, k = game_result_screen(uid, "crash", r["bet"], f"💥 <b>КРАШ на ×{r['cp']:.2f}!</b> Ты не успел — ставка проиграна.",
                                              get_profile(uid)["coins"])
                    await _edit(msg, t, k)
                return
            t, k = crash_play_screen(uid, st["bet"], st["mult"])
            await _edit(msg, t, k, final=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("цикл краша упал")


async def _do_crash_start(cb, uid: int, name: str, bet: int):
    get_profile(uid, name)
    r = crash_start(uid, bet)
    if not r["ok"]:
        await cb.answer(_game_err(r), show_alert=True)
        return
    await cb.answer("🚀 Взлетаем!")
    t, k = crash_play_screen(uid, bet, 1.0)
    await _edit(cb.message, t, k)
    task = asyncio.create_task(_crash_loop(cb.message, uid))
    _CRASH_TASKS.add(task)
    task.add_done_callback(_CRASH_TASKS.discard)


async def _do_game_out(cb, uid: int):
    g = game_active(uid)
    if g is None:
        await cb.answer("Активной партии нет", show_alert=True)
        t, k = games_hub_screen(uid)
        await _edit(cb.message, t, k)
        return
    if g["game"] == "mines":
        r = mines_cash(uid)
        if not r["ok"]:
            await cb.answer("Сначала открой хотя бы одну клетку", show_alert=True)
            return
        await cb.answer(f"+{_fmt(r['payout'])} 🪙")
        t, k = mines_screen(uid, f"💰 Забрано <b>×{r['mult']:.2f}</b> → <b>+{_fmt(r['payout'])}</b> 🪙", reveal={"state": r["state"], "bet": r["bet"]})
        await _edit(cb.message, _with_notices(uid, t), k)
        return
    r = crash_cash(uid)
    if not r["ok"]:
        await cb.answer("Раунд уже закончен", show_alert=True)
        return
    await cb.answer("Забрано!" if not r["bust"] else "Не успел…")
    if r["bust"]:
        body = f"💥 <b>КРАШ на ×{r['cp']:.2f}!</b> Ты не успел — ставка проиграна."
        coins = get_profile(uid)["coins"]
    else:
        body = f"💰 Забрал на <b>×{r['mult']:.2f}</b> (краш был бы на ×{r['cp']:.2f}) → <b>+{_fmt(r['payout'])}</b> 🪙"
        coins = r["coins"]
    t, k = game_result_screen(uid, "crash", r["bet"], body, coins)
    await _edit(cb.message, t, k)


async def _do_game_resume(cb, uid: int):
    g = game_active(uid)
    await cb.answer()
    if g is None:
        t, k = games_hub_screen(uid, "Активной партии нет.")
    elif g["game"] == "mines":
        t, k = mines_screen(uid)
    else:
        st = crash_status(uid)
        if st is None or st["busted"]:
            r = crash_settle_bust(uid)
            t, k = games_hub_screen(uid, "🚀 Раунд краша уже закончился — ставка проиграна." if r else "")
        else:
            t, k = crash_play_screen(uid, st["bet"], st["mult"])
            await _edit(cb.message, t, k)
            task = asyncio.create_task(_crash_loop(cb.message, uid))
            _CRASH_TASKS.add(task)
            task.add_done_callback(_CRASH_TASKS.discard)
            return
    await _edit(cb.message, t, k)


# ── сезонный пропуск ──────────────────────────────────────────────────────────
def pass_screen(uid: int, notice: str = "") -> tuple[str, Kb]:
    st = pass_state(uid)
    lines = [notice + "\n" if notice else "", f"⭐ <b>СЕЗОННЫЙ ПРОПУСК</b> · сезон {st['season']} · осталось {_human_long(st['left'])}",
             f"Уровень пропуска: <b>{st['tier']}/{PASS_TIERS}</b> " + (_bar(st["into"], PASS_XP) + f" · ещё {PASS_XP - st['into']} XP до следующего" if st["tier"] < PASS_TIERS else "— максимум"),
             f"XP сезона: {_fmt(st['xp'])} (+{PASS_XP} XP = новый уровень; XP дают открытия, батлы, апгрейды, обмены и квесты)",
             "💎 Премиум: " + ("<b>активен</b>" if st["premium"] else f"не куплен — {_fmt(PASS_PRICE)} 🪙 (в сумме награды премиума щедрее бесплатных в ~3 раза + предмет на 20 уровне)"), ""]
    for t in range(1, PASS_TIERS + 1):
        rw = pass_rewards(t)
        free_claimed, prem_claimed = (t, "free") in st["claimed"], (t, "prem") in st["claimed"]
        reached = t <= st["tier"]
        done = free_claimed and (prem_claimed or not st["premium"])
        mark = "✅" if (reached and done) else "🎁" if reached else "🔒"
        f, p = rw["free"], rw["prem"]
        lines.append(f"{mark} {t:>2} · 🆓 +{f['coins']}🪙{' +🎫' if f['tickets'] else ''} · 💎 +{p['coins']}🪙{' +🎫' if p['tickets'] else ''}{' +🎁 предмет' if p['item'] else ''}")
    rows = []
    if st["ready"]:
        rows.append([(f"🎁 Забрать всё ({st['ready']})", f"cs:psc:{uid}")])
    if not st["premium"]:
        rows.append([(f"💎 Премиум · {_fmt(PASS_PRICE)}🪙", f"cs:psb:{uid}:0")])
    rows.append([("📋 Квесты", f"cs:qs:{uid}"), ("🏠 Меню", f"cs:menu:{uid}")])
    return "\n".join(x for x in lines if x != ""), _kb(rows)


async def _do_pass_claim(cb, uid: int):
    r = pass_claim(uid)
    if not r["ok"]:
        await cb.answer("Пока нечего забирать", show_alert=True)
        return
    await cb.answer(f"+{_fmt(r['coins'])} 🪙")
    t, k = pass_screen(uid, f"🎁 Получено: +{_fmt(r['coins'])} 🪙" + (f" +{r['tickets']}🎫" if r["tickets"] else "")
                       + (" + предмет в инвентарь" if r["items"] else ""))
    await _edit(cb.message, _with_notices(uid, t), k)


async def _do_pass_buy(cb, uid: int, ok: int):
    if not ok:
        await cb.answer()
        await _edit(cb.message, f"💎 Купить премиум сезонного пропуска за <b>{_fmt(PASS_PRICE)}</b> 🪙?\n"
                                "Откроются премиум-награды всех уровней (в том числе уже пройденных) и предмет на 20 уровне.",
                    _kb([[("✅ Купить", f"cs:psb:{uid}:1"), ("❌ Отмена", f"cs:ps:{uid}")]]))
        return
    r = pass_buy(uid)
    if not r["ok"]:
        await cb.answer("Премиум уже куплен" if r["reason"] == "have" else f"Не хватает {_fmt(r.get('need', 0))} 🪙", show_alert=True)
        return
    await cb.answer("Премиум куплен!")
    t, k = pass_screen(uid, "💎 Премиум активирован!")
    await _edit(cb.message, _with_notices(uid, t), k)


# ── диспетчер новых кнопок и команды ──────────────────────────────────────────
_XACTS = {"qs", "qc", "qca", "sp", "spgo", "tk", "mk", "mkb", "mkl", "mki", "mkp", "mkm", "mkx", "al", "als", "alc", "alm",
          "gm", "gmg", "gmb", "gmcoin", "gmdice", "gmmine", "gmcell", "gmcrash", "gmout", "gmr", "gmx", "ps", "psc", "psb"}


async def _dispatch_x(cb, act: str, uid: int, name: str, parts: list[str]):
    msg = cb.message
    t = k = None
    if act == "qs":
        await cb.answer()
        t, k = quests_screen(uid)
    elif act == "qc":
        return await _do_quest_claim(cb, uid, parts[3], int(parts[4]))
    elif act == "qca":
        return await _do_quest_claim_all(cb, uid)
    elif act == "sp":
        await cb.answer()
        t, k = spin_screen(uid)
    elif act == "spgo":
        return await _do_spin(cb, uid, name)
    elif act == "tk":
        return await _do_ticket(cb, uid, name)
    elif act == "mk":
        await cb.answer()
        t, k = market_screen(uid, parts[3], int(parts[4]))
    elif act == "mkb":
        return await _do_market_buy(cb, uid, name, int(parts[3]), int(parts[4]))
    elif act == "mkl":
        await cb.answer()
        t, k = market_pick_screen(uid, int(parts[3]))
    elif act == "mki":
        await cb.answer()
        t, k = market_price_screen(uid, int(parts[3]))
    elif act == "mkp":
        return await _do_market_list(cb, uid, int(parts[3]), int(parts[4]))
    elif act == "mkm":
        await cb.answer()
        t, k = market_mine_screen(uid)
    elif act == "mkx":
        return await _do_market_cancel(cb, uid, int(parts[3]))
    elif act == "al":
        await cb.answer()
        t, k = album_screen(uid, int(parts[3]))
    elif act == "als":
        await cb.answer()
        t, k = album_set_screen(uid, parts[3])
    elif act == "alc":
        return await _do_album_claim(cb, uid, parts[3])
    elif act == "alm":
        return await _do_album_milestone(cb, uid, int(parts[3]))
    elif act == "gm":
        await cb.answer()
        t, k = games_hub_screen(uid)
    elif act == "gmg":
        if parts[3] not in GAMES:
            await cb.answer("Нет такой игры", show_alert=True)
            return
        await cb.answer()
        t, k = game_bet_screen(uid, parts[3])
    elif act == "gmb":
        if parts[3] not in GAMES:
            await cb.answer("Нет такой игры", show_alert=True)
            return
        if game_active(uid):
            await cb.answer("У тебя уже идёт партия — сначала закончи её", show_alert=True)
            return
        await cb.answer()
        t, k = game_options_screen(uid, parts[3], int(parts[4]))
    elif act == "gmcoin":
        return await _do_coin(cb, uid, name, int(parts[3]), parts[4])
    elif act == "gmdice":
        return await _do_dice(cb, uid, name, int(parts[3]), parts[4])
    elif act == "gmmine":
        return await _do_mines_start(cb, uid, name, int(parts[3]), int(parts[4]))
    elif act == "gmcell":
        return await _do_mines_cell(cb, uid, int(parts[3]))
    elif act == "gmcrash":
        return await _do_crash_start(cb, uid, name, int(parts[3]))
    elif act == "gmout":
        return await _do_game_out(cb, uid)
    elif act == "gmr":
        return await _do_game_resume(cb, uid)
    elif act == "gmx":
        await cb.answer()
        return
    elif act == "ps":
        await cb.answer()
        t, k = pass_screen(uid)
    elif act == "psc":
        return await _do_pass_claim(cb, uid)
    elif act == "psb":
        return await _do_pass_buy(cb, uid, int(parts[3]))
    await _edit(msg, t, k)


def _screen_cmd(builder):
    async def handler(message: types.Message):
        if not message.from_user:
            return
        uid, name = _touch(message.from_user, message.chat.id)
        text, kb = builder(uid, name)
        await message.answer(_with_notices(uid, text), parse_mode="HTML", reply_markup=kb)
    return handler


cmd_quests = _screen_cmd(lambda uid, name: quests_screen(uid))
cmd_market = _screen_cmd(lambda uid, name: market_screen(uid))
cmd_games = _screen_cmd(lambda uid, name: games_hub_screen(uid))
cmd_spin = _screen_cmd(lambda uid, name: spin_screen(uid))
cmd_pass = _screen_cmd(lambda uid, name: pass_screen(uid))
cmd_album = _screen_cmd(lambda uid, name: album_screen(uid))


CASES_HELP = ("🎰 <b>кейс-клуб</b>\n/case — меню: кейсы, инвентарь, бонус, апгрейд, контракты, топ, батлы\n"
              "/daily — бонус · /inv — инвентарь · /ctop — топ (в группе — по чату)\n"
              "/give 500 и /trade — ответом на сообщение игрока: подарок и обмен\n"
              "/quests · /spin · /market · /album · /games · /pass — быстрые переходы")


def register_cases_handlers(dp):
    _db()                                               # создаём БД/таблицы и мигрируем при старте
    dp.message.register(cmd_case, Command(*COMMANDS["menu"]))
    dp.message.register(cmd_daily, Command(*COMMANDS["daily"]))
    dp.message.register(cmd_inv, Command(*COMMANDS["inv"]))
    dp.message.register(cmd_ctop, Command(*COMMANDS["top"]))
    dp.message.register(cmd_give, Command(*COMMANDS["give"]))
    dp.message.register(cmd_trade, Command(*COMMANDS["trade"]))
    dp.message.register(cmd_cadmin, Command(*COMMANDS["admin"]))
    for key, fn in (("quests", cmd_quests), ("market", cmd_market), ("games", cmd_games), ("spin", cmd_spin),
                    ("pass", cmd_pass), ("album", cmd_album)):
        dp.message.register(fn, Command(*COMMANDS[key]))
    dp.callback_query.register(on_callback, F.data.startswith("cs:"))
    logger.info("🎰 cases_game зарегистрирован (/case, /daily, /inv, /ctop, /give, /trade), БД: %s", _DB_PATH)
