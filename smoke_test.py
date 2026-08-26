#!/usr/bin/env python3
"""
Быстрый smoke-test перед деплоем на bothost.

Не заменяет полноценные unit-тесты, но дёшево ловит именно те баги, которые
иначе всплывают только в проде — например, отсутствующий импорт функции из
db.py, из-за которого конкретная API-ручка падает с NameError только когда
пользователь реально её дёрнет.

Проверяет:
  1. Синтаксис всех .py файлов в репозитории (py_compile).
  2. Что config.py, db.py, music_engine.py, web_server.py вообще импортируются.
  3. Что init_db() отрабатывает и БД реально отвечает на запрос.
  4. Что web_server стартует и регистрирует все роуты без ошибок.
  5. Что /health и /api/search отвечают ожидаемым образом на живом сервере.

Запуск:  python scripts/smoke_test.py
Код выхода: 0 — всё ок (можно деплоить), 1 — что-то сломано (CI должен упасть).
"""
import asyncio
import compileall
import os
import sys
import tempfile
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

FAILED = []


def step(name):
    """Оборачивает шаг проверки: печатает результат, копит ошибки, но не
    прерывает остальные шаги — так в одном прогоне видно ВСЕ сломанные места,
    а не только первое."""
    def deco(fn):
        async def wrapper(*a, **kw):
            print(f"→ {name}...", end=" ", flush=True)
            try:
                result = fn(*a, **kw)
                if asyncio.iscoroutine(result):
                    result = await result
                print("OK")
                return result
            except Exception:
                print("FAILED")
                traceback.print_exc()
                FAILED.append(name)
                return None
        return wrapper
    return deco


@step("Синтаксис всех .py файлов (py_compile)")
def check_compile():
    ok = compileall.compile_dir(REPO_ROOT, quiet=1, rx=None)
    assert ok, "py_compile нашёл синтаксическую ошибку хотя бы в одном файле"


@step("Импорт config.py")
def check_config_import():
    import config  # noqa: F401


@step("Импорт db.py + init_db() + реальный запрос к БД")
def check_db():
    import db
    db.init_db()
    conn = db._db_connect()
    conn.execute("SELECT 1")
    conn.close()


@step("Импорт music_engine.py")
def check_music_engine():
    import music_engine
    assert music_engine.music_engine is not None


@step("web_server.py стартует и регистрирует все роуты без ошибок")
async def check_web_server_boots():
    import web_server
    await web_server.start_web_server()


@step("GET /health отвечает, БД в нём отмечена здоровой")
async def check_health_endpoint():
    import aiohttp
    port = os.environ["PORT"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/health") as r:
            assert r.status in (200, 503), f"неожиданный статус {r.status}"
            data = await r.json()
            assert data.get("checks", {}).get("db") is True, "БД не отвечает в /health"


@step("GET /api/search не падает даже без доступа к SoundCloud/YouTube")
async def check_search_endpoint():
    import aiohttp
    port = os.environ["PORT"]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/api/search?q=test") as r:
            assert r.status == 200, f"неожиданный статус {r.status}"
            data = await r.json()
            assert isinstance(data, list), "ответ /api/search должен быть JSON-массивом"


async def main():
    # Изолируем БД во временную директорию — не трогаем реальный music.db
    os.environ.setdefault("DB_DIR", tempfile.mkdtemp(prefix="music_bot_ci_"))
    # DEV_MODE нужен, чтобы web_server не отказывался стартовать без реального BOT_TOKEN
    os.environ.setdefault("DEV_MODE", "1")
    os.environ.setdefault("PORT", "8099")

    await check_compile()
    await check_config_import()
    await check_db()
    await check_music_engine()
    await check_web_server_boots()
    await asyncio.sleep(0.3)  # даём TCPSite время подняться
    await check_health_endpoint()
    await check_search_endpoint()

    print()
    if FAILED:
        print(f"❌ Провалено шагов: {len(FAILED)} — {', '.join(FAILED)}")
        sys.exit(1)
    print("✅ Все проверки прошли — можно деплоить")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
